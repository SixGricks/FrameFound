"""Showcase: the best photographs, one place at a time (media/showcase.py).

Built for GELCO's calendar — the best finished work of each course, a few
crew and construction shots, a company group photo — and general on
purpose. The search ranks; the operator chooses; the choice becomes an
ordinary listing, so Lightroom's "Import FrameFound listings…" and the
listing export both work on it. A showcase listing names its files by
place ("ledgerock-03-fall-drone"), and several searches can add to one
listing, so a whole shortlist lands in Lightroom as one collection.
"""

import asyncio
import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from framefound.ai.embeddings import EmbeddingUnavailable, get_embedding_provider
from framefound.auth.deps import CurrentUser, DbDep, SettingsDep
from framefound.db.models import Asset, Derivative, Face, Frame, Listing, ListingItem
from framefound.media import photo_index
from framefound.media import showcase as showcase_lib

log = structlog.get_logger()

router = APIRouter(prefix="/showcase", tags=["showcase"])

Kind = Literal["finished", "crew", "construction", "group"]


class ShowcaseRequest(BaseModel):
    library_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    # What the finished work is: "golf course", "patio", "home exterior".
    subject: str = Field(default="golf course", max_length=80)
    # Anything else to push down, in words: "winter", "cart paths".
    avoid: str = Field(default="", max_length=200)
    count: int = Field(default=14, ge=1, le=60)
    # Options per place, the best included. A place offers fewer when it has
    # fewer good photographs (showcase.OPTION_FLOOR).
    alternates: int = Field(default=3, ge=1, le=20)
    orientation: Literal["landscape", "portrait", "any"] = "landscape"
    min_megapixels: float = Field(default=12.0, ge=0.0, le=100.0)
    allow_people: bool = False
    # What to look for: the finished work, a crew in a scenic setting,
    # dramatic construction, or a company group photo.
    kind: Kind = "finished"
    # Only these places (folder names, as /showcase/places lists them);
    # empty for every place.
    places: list[str] = Field(default_factory=list, max_length=200)
    # Leave out what these listings already hold (and near-duplicates of
    # it): a second batch to review, not the first one again.
    exclude_listing_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)


class PickOut(BaseModel):
    asset_id: uuid.UUID
    filename: str
    relative_path: str
    width: int
    height: int
    megapixels: float
    captured_at: datetime | None
    score: float
    parts: dict[str, float]
    season: str
    source: str


class PlaceOut(BaseModel):
    key: str
    label: str
    picks: list[PickOut]


class ShowcaseResponse(BaseModel):
    places: list[PlaceOut]
    considered: int


class PlaceCount(BaseModel):
    key: str
    label: str
    photos: int


class ShowcasePickIn(BaseModel):
    asset_id: uuid.UUID
    place: str = Field(max_length=120)
    kind: Kind = "finished"
    season: str = Field(default="", max_length=12)
    source: str = Field(default="", max_length=12)


class ShowcaseListingRequest(BaseModel):
    # A new listing by this name — or, with listing_id, add to that one, so
    # several searches (finished work, crew, construction, a group photo)
    # make one shortlist.
    name: str = Field(default="", max_length=200)
    listing_id: uuid.UUID | None = None
    # A later batch numbers on from earlier ones — ledgerock-09 after a
    # first batch's ledgerock-08 — so two batches' files never share a name.
    continue_numbering_from: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    # In the order they should appear.
    picks: list[ShowcasePickIn] = Field(min_length=1, max_length=600)


def _prompt_vectors(subject: str, avoid: str, kind: str) -> dict[str, list[list[float]]]:
    provider = get_embedding_provider()

    def encode(texts: list[str]) -> list[list[float]]:
        return [provider.embed_text(t).vector for t in texts if t.strip()]

    finish_pos, finish_neg = showcase_lib.finish_prompts(subject)
    focus_pos, focus_neg = showcase_lib.focus_prompts(kind, subject)
    vectors = {
        "quality_pos": encode(showcase_lib.QUALITY_POSITIVE),
        "quality_neg": encode(showcase_lib.QUALITY_NEGATIVE),
        "finish_pos": encode(finish_pos),
        "finish_neg": encode(finish_neg),
        "light_pos": encode(showcase_lib.LIGHT_POSITIVE),
        "light_neg": encode(showcase_lib.LIGHT_NEGATIVE),
        "focus_pos": encode(focus_pos),
        "focus_neg": encode(focus_neg),
    }
    for key, texts in showcase_lib.clutter_prompts(subject).items():
        vectors[key] = encode(texts)
    avoid_terms = [a.strip() for a in avoid.split(",") if a.strip()]
    if avoid_terms:
        vectors["avoid"] = encode(avoid_terms)
    return vectors


def _place_key(label: str) -> str:
    return showcase_lib.place_of(f"{label}/x")[0]


@router.get("/places", response_model=list[PlaceCount])
async def list_places(
    _user: CurrentUser,
    db: DbDep,
    library_ids: Annotated[list[uuid.UUID] | None, Query()] = None,
) -> list[PlaceCount]:
    """The places a showcase can be limited to: top-level folders with
    photographs, merged by name ("Forest gate" / "Forest Gate NJ"), without
    the collection folders ("Photo Exports") whose photographs are placed by
    GPS instead."""
    stmt = select(Asset.relative_path).where(
        Asset.media_type == "image", Asset.relative_path.contains("/")
    )
    if library_ids:
        stmt = stmt.where(Asset.library_id.in_(library_ids))
    folders: dict[str, int] = {}
    for (path,) in (await db.execute(stmt)).all():
        folder = path.split("/", 1)[0]
        folders[folder] = folders.get(folder, 0) + 1
    merged: dict[str, PlaceCount] = {}
    for folder, count in folders.items():
        if showcase_lib.is_generic(f"{folder}/x"):
            continue
        key, label = showcase_lib.place_of(f"{folder}/x")
        entry = merged.setdefault(key, PlaceCount(key=key, label=label, photos=0))
        entry.photos += count
    return sorted(merged.values(), key=lambda p: p.label.lower())


@router.post("", response_model=ShowcaseResponse)
async def search_showcase(
    body: ShowcaseRequest, _user: CurrentUser, db: DbDep, settings: SettingsDep
) -> ShowcaseResponse:
    stmt = (
        select(
            Asset.id,
            Asset.relative_path,
            Asset.filename,
            Asset.width,
            Asset.height,
            Asset.gps_lat,
            Asset.gps_lon,
            Asset.captured_at,
            Asset.camera_make,
            Frame.embedding,
        )
        .join(Frame, Frame.asset_id == Asset.id)
        .where(Asset.media_type == "image", Frame.embedding.is_not(None))
    )
    if body.library_ids:
        stmt = stmt.where(Asset.library_id.in_(body.library_ids))
    rows = (await db.execute(stmt)).all()
    if not rows:
        raise HTTPException(status_code=404, detail="No catalogued photographs to choose from")
    ids = [row.id for row in rows]
    faces: dict[uuid.UUID, int] = {
        asset_id: int(n)
        for asset_id, n in (
            await db.execute(
                select(Face.asset_id, func.count())
                .where(Face.asset_id.in_(ids))
                .group_by(Face.asset_id)
            )
        ).all()
    }
    thumbs: dict[uuid.UUID, str] = {
        asset_id: path
        for asset_id, path in (
            await db.execute(
                select(Derivative.asset_id, Derivative.relative_path).where(
                    Derivative.asset_id.in_(ids), Derivative.kind == "thumbnail"
                )
            )
        ).all()
    }
    photos = [
        showcase_lib.Photo(
            asset_id=str(row.id),
            relative_path=row.relative_path,
            filename=row.filename,
            width=row.width or 0,
            height=row.height or 0,
            embedding=list(row.embedding),
            gps=(row.gps_lat, row.gps_lon) if row.gps_lat is not None else None,
            faces=int(faces.get(row.id, 0)),
            captured_at=row.captured_at.isoformat() if row.captured_at else None,
            thumbnail=thumbs.get(row.id),
            camera_make=row.camera_make or "",
        )
        for row in rows
    ]
    try:
        vectors = await asyncio.to_thread(_prompt_vectors, body.subject, body.avoid, body.kind)
    except EmbeddingUnavailable as err:
        raise HTTPException(status_code=503, detail=str(err)) from err
    only = {_place_key(label) for label in body.places if label.strip()} or None
    shown: set[str] | None = None
    if body.exclude_listing_ids:
        shown = {
            str(asset_id)
            for (asset_id,) in (
                await db.execute(
                    select(ListingItem.asset_id).where(
                        ListingItem.listing_id.in_(body.exclude_listing_ids)
                    )
                )
            ).all()
        }
    # Off the event loop: thousands of vectors and a few hundred thumbnails.
    places, considered = await asyncio.to_thread(
        showcase_lib.rank,
        photos,
        vectors,
        count=body.count,
        alternates=body.alternates,
        orientation=body.orientation,
        min_megapixels=body.min_megapixels,
        thumbnail_root=settings.data_dir,
        allow_people=body.allow_people,
        kind=body.kind,
        only_places=only,
        exclude=shown,
    )
    log.info("showcase.searched", considered=considered, places=len(places), kind=body.kind)
    return ShowcaseResponse(
        considered=considered,
        places=[
            PlaceOut(
                key=place.key,
                label=place.label,
                picks=[
                    PickOut(
                        asset_id=uuid.UUID(p.photo.asset_id),
                        filename=p.photo.filename,
                        relative_path=p.photo.relative_path,
                        width=p.photo.width,
                        height=p.photo.height,
                        megapixels=round(p.photo.width * p.photo.height / 1e6, 1),
                        captured_at=(
                            datetime.fromisoformat(p.photo.captured_at)
                            if p.photo.captured_at
                            else None
                        ),
                        score=round(p.score, 3),
                        parts=p.parts,
                        season=p.tags.get("season", ""),
                        source=p.tags.get("source", ""),
                    )
                    for p in place.picks
                ],
            )
            for place in places
        ],
    )


def _caption(place: str, pick: ShowcasePickIn, when: datetime | None) -> str:
    """ "LedgeRock — Fall · Drone · October 2023" — or with "Crew" or
    "Construction" when that is what it shows."""
    words = [w.capitalize() for w in (pick.season, pick.source) if w]
    if pick.kind in ("crew", "construction"):
        words.append(pick.kind.capitalize())
    if when:
        words.append(when.strftime("%B %Y"))
    return " — ".join(x for x in (place, " · ".join(words)) if x)


@router.post("/listing", status_code=201)
async def create_showcase_listing(
    body: ShowcaseListingRequest, _user: CurrentUser, db: DbDep
) -> dict[str, str | int]:
    """The chosen photographs as a listing, or added to one, in order. Each
    item's slug is its whole file name, place first and numbered within its
    place ("ledgerock-03-fall-drone") — the name its copy gets in
    Lightroom — and its caption says where, when and what."""
    if body.listing_id is not None:
        found_listing = await db.get(Listing, body.listing_id)
        if found_listing is None:
            raise HTTPException(status_code=404, detail="No such listing")
        listing = found_listing
    else:
        if not body.name.strip():
            raise HTTPException(status_code=400, detail="Name the listing")
        listing = Listing(
            name=body.name.strip(),
            file_suffix=photo_index.default_suffix(body.name),
            file_naming="place",
        )
        db.add(listing)
        await db.flush()

    existing = (
        await db.execute(
            select(ListingItem.asset_id, ListingItem.slug, ListingItem.position).where(
                ListingItem.listing_id == listing.id
            )
        )
    ).all()
    already = {row.asset_id for row in existing}
    position = max((row.position for row in existing), default=-1) + 1
    # Numbering continues within each place and kind across searches — and
    # across batches: a second search or batch adds ledgerock-09, not a
    # second ledgerock-01.
    earlier = (
        [
            slug
            for (slug,) in (
                await db.execute(
                    select(ListingItem.slug).where(
                        ListingItem.listing_id.in_(body.continue_numbering_from)
                    )
                )
            ).all()
        ]
        if body.continue_numbering_from
        else []
    )
    numbers: dict[str, int] = {}
    for slug in [row.slug for row in existing] + earlier:
        found = re.match(r"^(.*?)-(\d{2,3})(?:-|$)", slug or "")
        if found:
            head = found.group(1)
            numbers[head] = max(numbers.get(head, 0), int(found.group(2)))

    assets = {
        row.id: row
        for row in (
            await db.execute(select(Asset).where(Asset.id.in_([p.asset_id for p in body.picks])))
        ).scalars()
    }
    added = 0
    for pick in body.picks:
        asset = assets.get(pick.asset_id)
        if asset is None or asset.id in already:
            continue  # gone, or already in this listing (a crew shot can also rank as finished)
        place = " ".join(pick.place.split())[:120]
        head = showcase_lib.stem_head(place, pick.kind)
        numbers[head] = numbers.get(head, 0) + 1
        tags = {"season": pick.season, "source": pick.source, "kind": pick.kind}
        db.add(
            ListingItem(
                listing_id=listing.id,
                asset_id=asset.id,
                position=position,
                slug=showcase_lib.file_stem(place, numbers[head], tags)[:80],
                caption=_caption(place, pick, asset.captured_at)[:300],
                naming_source="confirmed",
            )
        )
        already.add(asset.id)
        position += 1
        added += 1
    await db.commit()
    log.info("showcase.listing_saved", listing_id=str(listing.id), added=added)
    return {"listing_id": str(listing.id), "added": added}
