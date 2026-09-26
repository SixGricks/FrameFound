"""Showcase: the best finished photographs, one per place (media/showcase.py).

Built for GELCO's calendar — fourteen masterpiece photographs from fourteen
courses — and general on purpose. The search ranks; the operator chooses;
the choice becomes an ordinary listing, so sending it is the export that
already exists: full-size files named after the place, a photo index and
contact sheets to choose from.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Literal

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from framefound.ai.embeddings import EmbeddingUnavailable, get_embedding_provider
from framefound.auth.deps import CurrentUser, DbDep, SettingsDep
from framefound.db.models import Asset, Derivative, Face, Frame, Listing, ListingItem
from framefound.media import photo_index
from framefound.media import showcase as showcase_lib

log = structlog.get_logger()

router = APIRouter(prefix="/showcase", tags=["showcase"])


class ShowcaseRequest(BaseModel):
    library_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    # What the finished work is: "golf course", "patio", "home exterior".
    subject: str = Field(default="golf course", max_length=80)
    # Anything else to push down, in words: "winter", "cart paths".
    avoid: str = Field(default="", max_length=200)
    count: int = Field(default=14, ge=1, le=60)
    alternates: int = Field(default=3, ge=1, le=8)
    orientation: Literal["landscape", "portrait", "any"] = "landscape"
    min_megapixels: float = Field(default=12.0, ge=0.0, le=100.0)
    allow_people: bool = False


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


class PlaceOut(BaseModel):
    key: str
    label: str
    picks: list[PickOut]


class ShowcaseResponse(BaseModel):
    places: list[PlaceOut]
    considered: int


class ShowcaseListingRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # In the order they should appear; each with the place it represents.
    picks: list[dict[str, str]] = Field(min_length=1, max_length=120)


def _prompt_vectors(subject: str, avoid: str) -> dict[str, list[list[float]]]:
    provider = get_embedding_provider()

    def encode(texts: list[str]) -> list[list[float]]:
        return [provider.embed_text(t).vector for t in texts if t.strip()]

    finish_pos, finish_neg = showcase_lib.finish_prompts(subject)
    vectors = {
        "quality_pos": encode(showcase_lib.QUALITY_POSITIVE),
        "quality_neg": encode(showcase_lib.QUALITY_NEGATIVE),
        "finish_pos": encode(finish_pos),
        "finish_neg": encode(finish_neg),
    }
    for key, texts in showcase_lib.clutter_prompts(subject).items():
        vectors[key] = encode(texts)
    avoid_terms = [a.strip() for a in avoid.split(",") if a.strip()]
    if avoid_terms:
        vectors["avoid"] = encode(avoid_terms)
    return vectors


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
        )
        for row in rows
    ]
    try:
        vectors = await asyncio.to_thread(_prompt_vectors, body.subject, body.avoid)
    except EmbeddingUnavailable as err:
        raise HTTPException(status_code=503, detail=str(err)) from err
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
    )
    log.info("showcase.searched", considered=considered, places=len(places))
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
                    )
                    for p in place.picks
                ],
            )
            for place in places
        ],
    )


@router.post("/listing", status_code=201)
async def create_showcase_listing(
    body: ShowcaseListingRequest, _user: CurrentUser, db: DbDep
) -> dict[str, str]:
    """The chosen photographs as a listing, in order, each named after its
    place — so the existing export sends them: full size, a photo index and
    contact sheets, files like `03-edgewood-country-club-gelco-calendar.jpg`."""
    asset_ids = []
    for pick in body.picks:
        try:
            asset_ids.append(uuid.UUID(pick.get("asset_id", "")))
        except ValueError:
            raise HTTPException(status_code=400, detail="Not an asset id") from None
    found = {
        row.id: row
        for row in (await db.execute(select(Asset).where(Asset.id.in_(asset_ids)))).scalars()
    }
    listing = Listing(name=body.name.strip(), file_suffix=photo_index.default_suffix(body.name))
    db.add(listing)
    await db.flush()
    position = 0
    for pick, asset_id in zip(body.picks, asset_ids, strict=True):
        asset = found.get(asset_id)
        if asset is None:
            continue
        place = " ".join(str(pick.get("place", "")).split())[:120]
        when = asset.captured_at.strftime("%B %Y") if asset.captured_at else ""
        db.add(
            ListingItem(
                listing_id=listing.id,
                asset_id=asset_id,
                position=position,
                slug=photo_index.clean_slug(place),
                caption=" — ".join(x for x in (place, when) if x),
                naming_source="confirmed",
            )
        )
        position += 1
    await db.commit()
    log.info("showcase.listing_created", listing_id=str(listing.id), photos=position)
    return {"listing_id": str(listing.id)}
