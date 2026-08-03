"""People: face clusters, naming, and review.

The operator chose **confirm before it counts**. So:

- Clustering proposes groups. Nothing is attributed to a named person until
  someone says so.
- A named person's new matches arrive as *suggestions* and need a thumbs-up.
- A thumbs-down is stored as a rejection for that person specifically, and is
  never offered again — the threshold is a heuristic, this is the guarantee.

The consequence is more clicking at the start and a catalogue that is never
confidently wrong, which is the right trade for something that puts one
person's photograph in another person's album.
"""

import asyncio
import io
import re
import uuid
from typing import Any, cast

import structlog
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.engine import CursorResult

from framefound.ai import people as people_lib
from framefound.auth.deps import CurrentUser, DbDep, SettingsDep, require_admin
from framefound.db.models import Asset, Face, Person
from framefound.media.maps_store import load_face_config, save_face_config

log = structlog.get_logger()

router = APIRouter(prefix="/people", tags=["people"])

UNNAMED_LABEL = "Unnamed person"


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:140]


class FaceOut(BaseModel):
    face_id: uuid.UUID
    asset_id: uuid.UUID
    frame_id: uuid.UUID
    filename: str
    # Normalised, so the UI can crop from whatever size it renders the frame at.
    box_x: float
    box_y: float
    box_w: float
    box_h: float
    detection_score: float
    similarity: float | None
    source: str


class PersonOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    named: bool
    confirmed_count: int
    pending_count: int
    cover: FaceOut | None


class PersonDetail(PersonOut):
    faces: list[FaceOut]
    # Faces a catalogue-wide sweep has offered but nobody has judged yet.
    suggestion_count: int = 0


class NameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class JudgementRequest(BaseModel):
    face_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class FaceSettingsOut(BaseModel):
    enabled: bool
    suggest_across_libraries: bool
    people_count: int
    faces_count: int
    unnamed_clusters: int


class FaceSettingsUpdate(BaseModel):
    enabled: bool | None = None
    suggest_across_libraries: bool | None = None


def _rows(result: object) -> int:
    """Affected-row count. SQLAlchemy types execute() as Result, which has no
    rowcount; an UPDATE actually returns a CursorResult."""
    return cast("CursorResult[Any]", result).rowcount or 0


def _face_out(face: Face, asset: Asset) -> FaceOut:
    return FaceOut(
        face_id=face.id,
        asset_id=face.asset_id,
        frame_id=face.frame_id,
        filename=asset.filename,
        box_x=face.box_x,
        box_y=face.box_y,
        box_w=face.box_w,
        box_h=face.box_h,
        detection_score=face.detection_score,
        similarity=face.similarity,
        source=face.source,
    )


@router.get("", response_model=list[PersonOut])
async def list_people(
    _user: CurrentUser,
    db: DbDep,
    named_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[PersonOut]:
    """People, biggest first. Unnamed clusters are included by default —
    they are the work queue, not clutter.

    Ordered by how many faces are actually *in* each group, not by
    `Person.face_count`, which counts only confirmed ones. That distinction
    used to make this endpoint useless for its stated purpose: every unnamed
    cluster has a confirmed count of zero, so on this deployment 2,205 of them
    tied at zero and the first hundred came back in whatever order the planner
    chose. The operator was shown a hundred arbitrary clusters rather than the
    hundred biggest — precisely inverting the triage this page exists for,
    since naming the group that appears in eighty photographs is worth more
    than naming one that appears in two.
    """
    totals = (
        select(Face.person_id.label("pid"), func.count().label("total"))
        .where(Face.person_id.is_not(None), Face.source != "rejected")
        .group_by(Face.person_id)
        .subquery()
    )
    stmt = (
        select(Person)
        .outerjoin(totals, totals.c.pid == Person.id)
        .order_by(func.coalesce(totals.c.total, 0).desc(), Person.created_at)
        .limit(limit)
    )
    if named_only:
        stmt = stmt.where(Person.name != "")
    people = (await db.execute(stmt)).scalars().all()
    if not people:
        return []

    ids = [p.id for p in people]
    counts = {
        (pid, source): n
        for pid, source, n in (
            await db.execute(
                select(Face.person_id, Face.source, func.count())
                .where(Face.person_id.in_(ids))
                .group_by(Face.person_id, Face.source)
            )
        ).all()
    }

    out: list[PersonOut] = []
    for person in people:
        cover = await _cover_for(db, person)
        out.append(
            PersonOut(
                id=person.id,
                name=person.name or UNNAMED_LABEL,
                slug=person.slug,
                named=bool(person.name),
                confirmed_count=counts.get((person.id, "confirmed"), 0),
                pending_count=counts.get((person.id, "detected"), 0),
                cover=cover,
            )
        )
    return out


async def _cover_for(db: DbDep, person: Person) -> FaceOut | None:
    """A representative face. Prefers a confirmed one — a cover taken from an
    unreviewed guess would make the card assert something nobody agreed to."""
    stmt = (
        select(Face, Asset)
        .join(Asset, Asset.id == Face.asset_id)
        .where(Face.person_id == person.id)
        .order_by(Face.source != "confirmed", Face.detection_score.desc())
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    return _face_out(row[0], row[1]) if row else None


class NameSuggestion(BaseModel):
    id: uuid.UUID
    name: str
    confirmed_count: int
    exact: bool
    cover: FaceOut | None


@router.get("/suggest/names", response_model=list[NameSuggestion])
async def suggest_names(
    _user: CurrentUser,
    db: DbDep,
    q: str = Query(default="", max_length=120),
    # Plain default rather than Query(): FastAPI infers the query parameter
    # either way, and the wrapper is what trips B008 on an optional UUID.
    exclude: uuid.UUID | None = None,
    limit: int = Query(default=8, ge=1, le=25),
) -> list[NameSuggestion]:
    """People whose name matches what is being typed.

    Clustering routinely produces several groups for the same person — a
    different haircut, a decade, a bad angle — so "Dad" already existing when
    you go to name a new cluster "Dad" is the normal case, not an error. Left
    to fend for itself the catalogue accumulates four people called Dad and no
    single album for any of them.

    Surfacing the match at the moment of typing lets the operator merge instead
    of duplicating, which is the only point at which they have the context to
    know it *is* the same person.

    Prefix matches rank above contains-matches, and the exact match is flagged
    so the UI can say "this already exists" rather than making them read.
    """
    term = q.strip().lower()
    if not term:
        return []

    stmt = select(Person).where(Person.name != "", func.lower(Person.name).contains(term))
    if exclude is not None:
        stmt = stmt.where(Person.id != exclude)
    people = (await db.execute(stmt.limit(limit * 4))).scalars().all()
    if not people:
        return []

    counts = await _confirmed_counts(db, [p.id for p in people])
    covers = await _covers(db, [p.cover_face_id for p in people if p.cover_face_id])

    def rank(person: Person) -> tuple[int, int, str]:
        lowered = person.name.lower()
        # Exact first, then prefix, then anywhere; bigger groups before smaller.
        tier = 0 if lowered == term else (1 if lowered.startswith(term) else 2)
        return (tier, -counts.get(person.id, 0), lowered)

    people = sorted(people, key=rank)[:limit]
    return [
        NameSuggestion(
            id=person.id,
            name=person.name,
            confirmed_count=counts.get(person.id, 0),
            exact=person.name.lower() == term,
            cover=covers.get(person.cover_face_id) if person.cover_face_id else None,
        )
        for person in people
    ]


async def _confirmed_counts(db: DbDep, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Face.person_id, func.count())
            .where(Face.person_id.in_(ids), Face.source == "confirmed")
            .group_by(Face.person_id)
        )
    ).all()
    return {row[0]: row[1] for row in rows}


async def _covers(db: DbDep, face_ids: list[uuid.UUID]) -> dict[uuid.UUID, FaceOut]:
    if not face_ids:
        return {}
    rows = (
        await db.execute(
            select(Face, Asset).join(Asset, Asset.id == Face.asset_id).where(Face.id.in_(face_ids))
        )
    ).all()
    return {row[0].id: _face_out(row[0], row[1]) for row in rows}


@router.get("/{person_id}", response_model=PersonDetail)
async def get_person(
    person_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    source: str | None = Query(default=None, pattern="^(confirmed|detected|rejected)$"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> PersonDetail:
    person = await db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person")

    stmt = (
        select(Face, Asset)
        .join(Asset, Asset.id == Face.asset_id)
        .where(Face.person_id == person_id)
        # Unreviewed first: the review queue is the reason to open this page.
        #
        # Within the queue, by similarity to this person — not by detection
        # score, which is what the detector thought of the *box* ("is that a
        # face at all"), and says nothing about whose face it is. Sorting a
        # review queue by it scattered the confident matches among the doubtful
        # ones at random, which is what forced the operator to open every image
        # in turn: with no usable order, there was no prefix worth confirming
        # in bulk. Ranked properly, one pass down the grid crosses a single
        # boundary from yes to no.
        .order_by(
            Face.source != "detected",
            func.coalesce(Face.similarity, 0.0).desc(),
            Face.detection_score.desc(),
        )
        .limit(limit)
    )
    if source:
        stmt = stmt.where(Face.source == source)
    rows = (await db.execute(stmt)).all()
    faces = [_face_out(face, asset) for face, asset in rows]

    suggestions = (
        await db.execute(
            select(func.count())
            .select_from(Face)
            .where(Face.suggested_person_id == person_id, Face.suggestion_state == "pending")
        )
    ).scalar_one()

    return PersonDetail(
        id=person.id,
        name=person.name or UNNAMED_LABEL,
        slug=person.slug,
        named=bool(person.name),
        confirmed_count=sum(1 for f in faces if f.source == "confirmed"),
        pending_count=sum(1 for f in faces if f.source == "detected"),
        cover=faces[0] if faces else None,
        faces=faces,
        suggestion_count=suggestions,
    )


@router.put("/{person_id}/name", response_model=PersonOut)
async def name_person(
    person_id: uuid.UUID, body: NameRequest, _user: CurrentUser, db: DbDep
) -> PersonOut:
    """Give a cluster a name.

    Naming is also the moment the cluster's own faces become confirmed: the
    operator has looked at the group and said who it is, and asking them to
    then tick each face individually would be asking the same question twice.
    """
    person = await db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person")

    name = body.name.strip()
    slug = slugify(name)
    clash = (
        await db.execute(select(Person).where(Person.slug == slug, Person.id != person_id))
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(
            status_code=409,
            detail=f"'{clash.name}' already uses that name. Merge them instead.",
        )

    person.name, person.slug = name, slug
    await db.execute(
        update(Face)
        .where(Face.person_id == person_id, Face.source == "detected")
        .values(source="confirmed")
    )
    await db.commit()
    await _relearn(db, person)
    log.info("people.named", person_id=str(person_id), name=name)
    return PersonOut(
        id=person.id,
        name=person.name,
        slug=person.slug,
        named=True,
        confirmed_count=person.face_count,
        pending_count=0,
        cover=await _cover_for(db, person),
    )


@router.post("/{person_id}/confirm", status_code=200)
async def confirm_faces(
    person_id: uuid.UUID, body: JudgementRequest, _user: CurrentUser, db: DbDep
) -> dict[str, int]:
    """Agree that these faces are this person."""
    person = await db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person")
    result = await db.execute(
        update(Face)
        .where(Face.id.in_(body.face_ids), Face.person_id == person_id)
        .values(source="confirmed")
    )
    await db.commit()
    await _relearn(db, person)
    return {"confirmed": _rows(result)}


@router.post("/{person_id}/reject", status_code=200)
async def reject_faces(
    person_id: uuid.UUID, body: JudgementRequest, _user: CurrentUser, db: DbDep
) -> dict[str, int]:
    """Say these faces are not this person.

    The face keeps its `person_id` while marked rejected. That looks odd but is
    deliberate: the pair is what must be remembered. Clearing the link would
    lose which person was ruled out, and the face would be offered again on the
    next pass — the exact nagging this design exists to prevent.
    """
    person = await db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person")
    result = await db.execute(
        update(Face)
        .where(Face.id.in_(body.face_ids), Face.person_id == person_id)
        .values(source="rejected", similarity=None)
    )
    await db.commit()
    await _relearn(db, person)
    return {"rejected": _rows(result)}


class BulkJudgement(BaseModel):
    """Either an explicit list of faces, or everything at or above a bar.

    The bar exists because a list cannot express what the operator means when
    they scroll a ranked grid and say "down to here". Sending ids would make
    the answer depend on how many faces the page happened to have loaded — 295
    unreviewed faces were never going to fit in one request, and a bulk action
    that silently covered only the first 200 is worse than one that refuses.
    Sending the bar instead lets the server settle every face that qualifies,
    loaded or not.
    """

    face_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=500)
    min_similarity: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def exactly_one(self) -> "BulkJudgement":
        if (self.face_ids is None) == (self.min_similarity is None):
            raise ValueError("Give either face_ids or min_similarity, not both and not neither")
        return self

    def restrict(self, id_column: Any, similarity_column: Any) -> Any:
        """The WHERE clause this judgement means."""
        if self.face_ids is not None:
            return id_column.in_(self.face_ids)
        return func.coalesce(similarity_column, 0.0) >= self.min_similarity


@router.post("/{person_id}/confirm-above", status_code=200)
async def confirm_above(
    person_id: uuid.UUID, body: BulkJudgement, _user: CurrentUser, db: DbDep
) -> dict[str, int]:
    """Agree with every unreviewed face at or above a similarity."""
    person = await db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person")
    result = await db.execute(
        update(Face)
        .where(
            Face.person_id == person_id,
            Face.source == "detected",
            body.restrict(Face.id, Face.similarity),
        )
        .values(source="confirmed")
    )
    await db.commit()
    await _relearn(db, person)
    confirmed = _rows(result)
    log.info(
        "people.confirmed_above",
        person_id=str(person_id),
        min_similarity=body.min_similarity,
        confirmed=confirmed,
    )
    return {"confirmed": confirmed}


class DiscoverResponse(BaseModel):
    found: int
    searched: int
    threshold: float


# One sweep offers at most this many faces. Not a limit on what could be found
# — a bar that admits 3,000 faces is a bar set wrong, and burying the operator
# under them would teach them to stop trusting the button. Sweeping again after
# confirming the good ones finds the next tranche against a better prototype,
# which is the whole point of learning as you go.
DISCOVERY_BATCH = 120


@router.post("/{person_id}/discover", response_model=DiscoverResponse)
async def discover_more(
    person_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    limit: int = Query(default=DISCOVERY_BATCH, ge=1, le=500),
) -> DiscoverResponse:
    """Search the whole catalogue for faces that could be this person.

    Clustering only ever compared a face to the group it landed in. This
    compares every ungrouped and un-named face to what the operator has
    actually confirmed, so the person's own corrections are what does the
    finding — and each accepted face sharpens the prototype the next sweep
    uses.
    """
    person = await db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person")
    if not person.name:
        raise HTTPException(
            status_code=400,
            detail="Name this group first — searching needs somebody to search for.",
        )
    if not person.prototype:
        raise HTTPException(
            status_code=400,
            detail="Confirm a few faces first; there is nothing yet to match against.",
        )

    floor = people_lib.discovery_threshold(person.threshold)
    unnamed = select(Person.id).where(Person.name == "").scalar_subquery()
    eligible = (
        Face.embedding.is_not(None),
        # Only faces that are nobody yet: loose, or in a cluster no one has
        # named. A face already attributed to a *named* person is somebody
        # else's answer and is not up for grabs here.
        or_(Face.person_id.is_(None), Face.person_id.in_(unnamed)),
        # Never re-offer what this person has already refused. A face standing
        # as a suggestion for someone else is left alone too: there is one
        # suggestion slot per face, and stealing it would silently drop a
        # review the operator has not done yet.
        or_(
            Face.suggested_person_id.is_(None),
            and_(
                Face.suggested_person_id == person_id,
                func.coalesce(Face.suggestion_state, "") != "rejected",
            ),
        ),
    )

    if db.get_bind().dialect.name == "postgresql":
        # Embeddings are L2-normalised, so cosine distance is 1 - similarity.
        distance = Face.embedding.cosine_distance(person.prototype)
        rows = (
            await db.execute(
                select(Face.id, distance.label("distance"))
                .where(*eligible, distance <= 1.0 - floor)
                .order_by(distance)
                .limit(limit)
            )
        ).all()
        scored = [(face_id, 1.0 - float(dist)) for face_id, dist in rows]
    else:  # tests / SQLite, which has no vector operators
        loose = (await db.execute(select(Face.id, Face.embedding).where(*eligible))).all()
        scored = sorted(
            (
                (face_id, sim)
                for face_id, vector in loose
                if (sim := people_lib.similarity(person.prototype, vector)) >= floor
            ),
            key=lambda pair: pair[1],
            reverse=True,
        )[:limit]

    searched = (
        await db.execute(
            select(func.count())
            .select_from(Face)
            .where(
                Face.embedding.is_not(None),
                or_(Face.person_id.is_(None), Face.person_id.in_(unnamed)),
            )
        )
    ).scalar_one()

    if scored:
        await db.execute(
            update(Face),
            [
                {
                    "id": face_id,
                    "suggested_person_id": person_id,
                    "suggested_similarity": sim,
                    "suggestion_state": "pending",
                }
                for face_id, sim in scored
            ],
        )
        await db.commit()

    log.info(
        "people.discovered",
        person_id=str(person_id),
        found=len(scored),
        searched=searched,
        threshold=floor,
    )
    return DiscoverResponse(found=len(scored), searched=searched, threshold=floor)


@router.get("/{person_id}/suggestions", response_model=list[FaceOut])
async def list_suggestions(
    person_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    limit: int = Query(default=200, ge=1, le=500),
) -> list[FaceOut]:
    """Faces a sweep thinks could be this person, best match first."""
    rows = (
        await db.execute(
            select(Face, Asset)
            .join(Asset, Asset.id == Face.asset_id)
            .where(Face.suggested_person_id == person_id, Face.suggestion_state == "pending")
            .order_by(Face.suggested_similarity.desc())
            .limit(limit)
        )
    ).all()
    out = []
    for face, asset in rows:
        item = _face_out(face, asset)
        item.similarity = face.suggested_similarity
        item.source = "suggested"
        out.append(item)
    return out


@router.post("/{person_id}/suggestions/accept", status_code=200)
async def accept_suggestions(
    person_id: uuid.UUID, body: BulkJudgement, _user: CurrentUser, db: DbDep
) -> dict[str, int]:
    """Take every suggestion at or above a similarity.

    Accepting is the only thing that moves a face: it leaves whatever cluster
    it was in and becomes a confirmed face of this person.
    """
    person = await db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person")
    result = await db.execute(
        update(Face)
        .where(
            Face.suggested_person_id == person_id,
            Face.suggestion_state == "pending",
            body.restrict(Face.id, Face.suggested_similarity),
        )
        .values(
            person_id=person_id,
            source="confirmed",
            similarity=Face.suggested_similarity,
            suggested_person_id=None,
            suggested_similarity=None,
            suggestion_state=None,
        )
    )
    await db.commit()
    await _relearn(db, person)
    return {"accepted": _rows(result)}


@router.post("/{person_id}/suggestions/reject", status_code=200)
async def reject_suggestions(
    person_id: uuid.UUID, body: JudgementRequest, _user: CurrentUser, db: DbDep
) -> dict[str, int]:
    """Say these suggested faces are not this person.

    The face stays exactly where it was — in its own cluster, or in none. Only
    the refusal is recorded, so the face remains available to be grouped and
    named as whoever it actually is, and no sweep offers it here again.
    """
    person = await db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="No such person")
    result = await db.execute(
        update(Face)
        .where(
            Face.id.in_(body.face_ids),
            Face.suggested_person_id == person_id,
            Face.suggestion_state == "pending",
        )
        .values(suggestion_state="rejected")
    )
    await db.commit()
    # Refusals sharpen the threshold as much as agreements do.
    await _relearn(db, person)
    return {"rejected": _rows(result)}


@router.post("/{person_id}/merge/{other_id}", status_code=200, dependencies=[require_admin])
async def merge_people(
    person_id: uuid.UUID, other_id: uuid.UUID, _user: CurrentUser, db: DbDep
) -> dict[str, int]:
    """Fold one cluster into another.

    Clustering splits a person whenever lighting or a haircut moves them far
    enough in embedding space, so merging is the single most common correction.
    Rejections move too: they were judgements about a face, and losing them
    would resurrect every mistake the operator already dismissed.
    """
    if person_id == other_id:
        raise HTTPException(status_code=400, detail="Cannot merge a person into themselves")
    target, source = await db.get(Person, person_id), await db.get(Person, other_id)
    if target is None or source is None:
        raise HTTPException(status_code=404, detail="No such person")

    moved = await db.execute(
        update(Face).where(Face.person_id == other_id).values(person_id=person_id)
    )
    # Pending and refused suggestions follow the same reasoning as rejections:
    # they are judgements about a face, and the person they were about is now
    # this person. Leaving them behind would let the FK null them out and hand
    # every dismissed near-miss straight back on the next sweep.
    await db.execute(
        update(Face)
        .where(Face.suggested_person_id == other_id, Face.person_id.is_distinct_from(person_id))
        .values(suggested_person_id=person_id)
    )
    # Except where the merge already answered the question: a face that is now
    # in this person's cluster does not need offering to them.
    await db.execute(
        update(Face)
        .where(Face.suggested_person_id.in_((other_id, person_id)), Face.person_id == person_id)
        .values(suggested_person_id=None, suggested_similarity=None, suggestion_state=None)
    )
    await db.delete(source)
    await db.commit()
    await _relearn(db, target)
    count = _rows(moved)
    log.info("people.merged", into=str(person_id), from_=str(other_id), faces=count)
    return {"faces_moved": count}


@router.delete("/{person_id}", status_code=204, dependencies=[require_admin])
async def forget_person(person_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> None:
    """Forget a person entirely: the name, the grouping, and the face vectors.

    A real delete, not a flag. Someone asking to be removed from a face
    database is entitled to actually be removed, and a soft delete would be a
    lie about that.
    """
    person = await db.get(Person, person_id)
    if person is None:
        return
    from sqlalchemy import delete as sql_delete

    await db.execute(sql_delete(Face).where(Face.person_id == person_id))
    await db.delete(person)
    await db.commit()
    log.info("people.forgotten", person_id=str(person_id))


async def _relearn(db: DbDep, person: Person) -> None:
    """Recompute a person's prototype and threshold from their judgements.

    Every judgement, including the ones made on faces that never joined this
    person. A refused suggestion is the most informative negative there is —
    it is a face the catalogue-wide search ranked *highly* and the operator
    still said no to, which is exactly the case a threshold learned only from
    cluster members never sees. Ignoring those would let each sweep re-propose
    the same kind of near-miss for ever, so the tool would feel like it was not
    listening.
    """
    rows = (
        await db.execute(
            select(Face.embedding, Face.source).where(
                Face.person_id == person.id, Face.embedding.is_not(None)
            )
        )
    ).all()
    confirmed = [e for e, s in rows if s == "confirmed" and e]
    rejected = [e for e, s in rows if s == "rejected" and e]

    refused = (
        (
            await db.execute(
                select(Face.embedding).where(
                    Face.suggested_person_id == person.id,
                    Face.suggestion_state == "rejected",
                    Face.embedding.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    rejected.extend(e for e in refused if e)

    person.prototype = people_lib.prototype_for(confirmed)
    person.threshold = people_lib.threshold_for(person.prototype, confirmed, rejected)
    person.face_count = len(confirmed)
    await db.commit()


@router.get("/settings/current", response_model=FaceSettingsOut)
async def face_settings(_user: CurrentUser, db: DbDep) -> FaceSettingsOut:
    config = await load_face_config(db)
    people_count = (await db.execute(select(func.count()).select_from(Person))).scalar_one()
    faces_count = (await db.execute(select(func.count()).select_from(Face))).scalar_one()
    unnamed = (
        await db.execute(select(func.count()).select_from(Person).where(Person.name == ""))
    ).scalar_one()
    return FaceSettingsOut(
        enabled=config.enabled,
        suggest_across_libraries=config.suggest_across_libraries,
        people_count=people_count,
        faces_count=faces_count,
        unnamed_clusters=unnamed,
    )


@router.put("/settings/current", response_model=FaceSettingsOut, dependencies=[require_admin])
async def update_face_settings(
    body: FaceSettingsUpdate, _user: CurrentUser, db: DbDep
) -> FaceSettingsOut:
    config = await load_face_config(db)
    fields = body.model_dump(exclude_unset=True)
    if "enabled" in fields:
        config.enabled = bool(fields["enabled"])
    if "suggest_across_libraries" in fields:
        config.suggest_across_libraries = bool(fields["suggest_across_libraries"])
    await save_face_config(db, config)
    log.info("people.settings_updated", enabled=config.enabled)
    return await face_settings(_user, db)


@router.get("/faces/{face_id}/crop-box", response_model=FaceOut)
async def face_crop_box(face_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> FaceOut:
    """Where a face sits in its frame, in normalised coordinates."""
    row = (
        await db.execute(
            select(Face, Asset).join(Asset, Asset.id == Face.asset_id).where(Face.id == face_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No such face")
    return _face_out(row[0], row[1])


# A little air around the box. Detectors crop tight to the features, and a
# portrait with no forehead is genuinely hard to recognise.
CROP_PADDING = 0.35
MAX_CROP_SIZE = 512


@router.get("/faces/{face_id}/crop")
async def face_crop(  # type: ignore[no-untyped-def]
    face_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    size: int = Query(default=192, ge=32, le=MAX_CROP_SIZE),
):
    """The face itself, cropped out of the frame it was found in.

    This replaces a client-side crop that was wrong in two independent ways,
    and the reason it moved to the server is that both were geometry bugs that
    looked fine until someone studied the grid.

    The first was the source image. Faces are detected in *sampled frames*, and
    161 of the 307 faces on the reference deployment came from frames partway
    through a video. The UI was cropping from the asset's thumbnail — a
    different picture entirely — so more than half the grid showed whatever
    happened to be at that spot in an unrelated frame. That is the "random
    objects" an operator sees, and no amount of detector tuning would have
    fixed it, because detection was never the problem: those faces average
    0.73 confidence.

    The second was `object-fit: cover` on the tile. The stored box is
    normalised against the whole frame, but `cover` crops the image to fill a
    square before any of the box maths applies, so the coordinates no longer
    referred to what was on screen.

    Still no crop is *stored*. It is computed per request from a frame that is
    already on disk, which keeps the promise that mattered — there is no second
    copy of everyone's face to leak — while removing the client's need to know
    any geometry at all.
    """
    from PIL import Image

    from framefound.db.models import Frame

    row = (
        await db.execute(
            select(Face, Frame).join(Frame, Frame.id == Face.frame_id).where(Face.id == face_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No such face")
    face, frame = row[0], row[1]

    path = settings.data_dir / frame.relative_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="The frame image is no longer on disk")

    def render() -> bytes:
        with Image.open(path) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            # Normalised -> pixels, in the frame this face was actually found in.
            cx = (face.box_x + face.box_w / 2) * width
            cy = (face.box_y + face.box_h / 2) * height
            # One square side from the larger edge, so the crop never distorts
            # the face — a box normalised against differing width and height is
            # not square in pixels even when it looks square in the numbers.
            half = max(face.box_w * width, face.box_h * height) * (1 + CROP_PADDING * 2) / 2
            box = (
                int(max(0, cx - half)),
                int(max(0, cy - half)),
                int(min(width, cx + half)),
                int(min(height, cy + half)),
            )
            crop = rgb.crop(box)
            if crop.width == 0 or crop.height == 0:
                raise ValueError("degenerate crop")
            crop = crop.resize((size, size), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            crop.save(buffer, format="JPEG", quality=85)
            return buffer.getvalue()

    try:
        payload = await asyncio.to_thread(render)
    except (OSError, ValueError) as exc:
        log.warning("people.crop_failed", face_id=str(face_id), error=str(exc)[:200])
        raise HTTPException(status_code=422, detail="That frame could not be read") from None

    return Response(
        content=payload,
        media_type="image/jpeg",
        # A face's box never changes once detected, so this is safe to hold on
        # to. Private: it is a picture of somebody.
        headers={"Cache-Control": "private, max-age=86400"},
    )


# --- naming faces where you actually see them -----------------------------
#
# The People page is a review queue: it shows one person's faces and asks
# whether they belong. That is the right shape for bulk work and the wrong
# shape for the moment an operator is *looking at a photograph* and can see
# who is in it. Making them memorise a face, navigate to People, find the
# right cluster and confirm is asking them to hold context the picture was
# already showing them.
#
# So: boxes over the photograph, click one, say who it is.
#
# Everything here assumes the person is **already known**. A face that matches
# nobody is the exception, not the default — by the time a catalogue has been
# used for a week most faces in it belong to someone already named. The
# suggestion is therefore offered first and naming somebody new is the
# secondary path, which is the inverse of how the review queue works.


class FaceGuess(BaseModel):
    person_id: uuid.UUID
    name: str
    similarity: float
    # True when the match clears that person's own learned threshold, so the
    # UI can offer a one-click "yes" instead of a list to choose from.
    confident: bool


class FaceInPhoto(BaseModel):
    face_id: uuid.UUID
    box_x: float
    box_y: float
    box_w: float
    box_h: float
    detection_score: float
    source: str
    # Who this face is already attributed to, if anyone.
    person_id: uuid.UUID | None
    person_name: str
    # Best guesses, strongest first. Empty when nobody is named yet.
    guesses: list[FaceGuess]


class FacesInPhoto(BaseModel):
    asset_id: uuid.UUID
    faces: list[FaceInPhoto]
    note: str


# How many alternatives to offer under the top guess. Three is enough to catch
# "it proposed my brother, I want me"; a longer list is a worse experience than
# typing the name.
MAX_GUESSES = 3


@router.get("/faces/in-asset/{asset_id}", response_model=FacesInPhoto)
async def faces_in_asset(asset_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> FacesInPhoto:
    """Every face found in one photograph, with a guess at who each one is.

    Guesses are scored against each *named* person's prototype. Unnamed
    clusters are deliberately excluded: "is this Unnamed person 7?" is not a
    question anybody can answer, and offering it would bury the useful options.
    """
    faces = (
        (await db.execute(select(Face).where(Face.asset_id == asset_id).order_by(Face.box_x)))
        .scalars()
        .all()
    )
    if not faces:
        return FacesInPhoto(asset_id=asset_id, faces=[], note="No faces were found here.")

    people = (await db.execute(select(Person).where(Person.name != ""))).scalars().all()
    named = {p.id: p for p in people}
    with_prototype = [p for p in people if p.prototype]

    out: list[FaceInPhoto] = []
    for face in faces:
        guesses: list[FaceGuess] = []
        if face.embedding:
            scored = [
                (people_lib.similarity(face.embedding, person.prototype), person)
                for person in with_prototype
            ]
            scored.sort(key=lambda pair: pair[0], reverse=True)
            guesses = [
                FaceGuess(
                    person_id=person.id,
                    name=person.name,
                    similarity=round(score, 4),
                    confident=score >= person.threshold,
                )
                for score, person in scored[:MAX_GUESSES]
                if score > 0
            ]

        owner = named.get(face.person_id) if face.person_id else None
        # Detectors return boxes that run past the edge of the frame — a face
        # at the top of a group shot came back at y=-0.03 on real data. Clamped
        # here so every consumer can treat these as drawable coordinates rather
        # than each discovering the same thing.
        box_x = min(max(face.box_x, 0.0), 1.0)
        box_y = min(max(face.box_y, 0.0), 1.0)
        out.append(
            FaceInPhoto(
                face_id=face.id,
                box_x=box_x,
                box_y=box_y,
                box_w=min(face.box_w, 1.0 - box_x),
                box_h=min(face.box_h, 1.0 - box_y),
                detection_score=face.detection_score,
                source=face.source,
                person_id=face.person_id,
                person_name=owner.name if owner else "",
                guesses=guesses,
            )
        )

    # Counted on *name*, not on person_id. A face sitting in an unnamed
    # cluster has been grouped but not identified, and calling that "known"
    # would tell the operator the work is done when the only thing anyone can
    # do with it is put a name to it.
    unknown = sum(1 for f in out if not f.person_name)
    if not with_prototype:
        note = "Nobody is named yet, so there is nothing to compare against."
    elif unknown:
        note = f"{unknown} of {len(out)} not yet attributed."
    else:
        note = "Everyone here is already named."
    return FacesInPhoto(asset_id=asset_id, faces=out, note=note)


class AssignFaceRequest(BaseModel):
    # Exactly one of these. `person_id` attributes to somebody who exists;
    # `name` is the escape hatch for a face that belongs to nobody yet, and
    # matches an existing name rather than making a duplicate.
    person_id: uuid.UUID | None = None
    name: str = Field(default="", max_length=120)


class AssignFaceResponse(BaseModel):
    face_id: uuid.UUID
    person_id: uuid.UUID
    name: str
    created: bool
    confirmed_count: int


@router.post("/faces/{face_id}/assign", response_model=AssignFaceResponse)
async def assign_face(
    face_id: uuid.UUID, body: AssignFaceRequest, _user: CurrentUser, db: DbDep
) -> AssignFaceResponse:
    """Say who a face is, from the photograph.

    Counts as a **confirmation**, not a suggestion: the operator is looking at
    the picture and naming the person in it, which is the strongest evidence
    this system ever receives. That is consistent with confirm-before-it-counts
    rather than a departure from it — the confirmation is the click.

    Every assignment re-learns the person, so the next photograph is guessed
    better. This is the training loop the operator asked for: correcting it is
    how it improves, and doing that where the faces are visible is what makes
    the correction cheap enough to bother with.
    """
    face = await db.get(Face, face_id)
    if face is None:
        raise HTTPException(status_code=404, detail="No such face")

    created = False
    if body.person_id is not None:
        person = await db.get(Person, body.person_id)
        if person is None:
            raise HTTPException(status_code=404, detail="No such person")
    else:
        wanted = body.name.strip()
        if not wanted:
            raise HTTPException(status_code=400, detail="Give a person or a name")
        # Match on slug so "Dad", "dad" and "DAD" are one person rather than
        # three — the same rule the naming autocomplete follows.
        slug = slugify(wanted)
        person = (
            await db.execute(select(Person).where(Person.slug == slug, Person.slug != ""))
        ).scalar_one_or_none()
        if person is None:
            person = Person(name=wanted, slug=slug, threshold=people_lib.DEFAULT_THRESHOLD)
            db.add(person)
            await db.flush()
            created = True

    previous = face.person_id
    face.person_id = person.id
    face.source = "confirmed"
    face.similarity = None
    if person.cover_face_id is None:
        person.cover_face_id = face.id
    await db.commit()

    await _relearn(db, person)
    # The person this face used to belong to has lost evidence, so their
    # prototype is now wrong until it is recomputed. Skipping this is how a
    # cluster keeps matching faces it no longer contains.
    if previous and previous != person.id:
        stale = await db.get(Person, previous)
        if stale is not None:
            await _relearn(db, stale)

    log.info(
        "people.face_assigned",
        face_id=str(face_id),
        person=person.name,
        created=created,
        moved_from=str(previous) if previous else None,
    )
    return AssignFaceResponse(
        face_id=face_id,
        person_id=person.id,
        name=person.name,
        created=created,
        confirmed_count=person.face_count,
    )
