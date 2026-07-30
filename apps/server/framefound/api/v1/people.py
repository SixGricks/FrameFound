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

import re
import uuid
from typing import Any, cast

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult

from framefound.ai import people as people_lib
from framefound.auth.deps import CurrentUser, DbDep, require_admin
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
    they are the work queue, not clutter."""
    stmt = select(Person).order_by(Person.face_count.desc()).limit(limit)
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
        .order_by(Face.source != "detected", Face.detection_score.desc())
        .limit(limit)
    )
    if source:
        stmt = stmt.where(Face.source == source)
    rows = (await db.execute(stmt)).all()
    faces = [_face_out(face, asset) for face, asset in rows]

    return PersonDetail(
        id=person.id,
        name=person.name or UNNAMED_LABEL,
        slug=person.slug,
        named=bool(person.name),
        confirmed_count=sum(1 for f in faces if f.source == "confirmed"),
        pending_count=sum(1 for f in faces if f.source == "detected"),
        cover=faces[0] if faces else None,
        faces=faces,
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
    """Recompute a person's prototype and threshold from their judgements."""
    rows = (
        await db.execute(
            select(Face.embedding, Face.source).where(
                Face.person_id == person.id, Face.embedding.is_not(None)
            )
        )
    ).all()
    confirmed = [e for e, s in rows if s == "confirmed" and e]
    rejected = [e for e, s in rows if s == "rejected" and e]

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
    """Where a face sits in its frame, so the UI can crop it client-side.

    No crop is stored or served: the frame is already available through the
    media endpoint, and keeping a second copy of everyone's face would double
    the most sensitive data in the system for nothing.
    """
    row = (
        await db.execute(
            select(Face, Asset).join(Asset, Asset.id == Face.asset_id).where(Face.id == face_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No such face")
    return _face_out(row[0], row[1])
