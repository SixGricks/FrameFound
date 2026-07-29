"""Places: located assets grouped into the shoots they came from.

Read-only and computed on demand. Persisting clusters would mean invalidating
them every time a scan adds a file or an inference fills a position, and the
whole located set is small enough that recomputing is cheaper than keeping a
cache honest.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import select

from framefound.auth.deps import CurrentUser, DbDep
from framefound.db.models import Asset, Derivative
from framefound.media import places as places_lib

router = APIRouter(prefix="/places", tags=["places"])


class PlaceOut(BaseModel):
    name: str
    lat: float
    lon: float
    radius_km: float
    asset_count: int
    inferred_count: int
    first_captured_at: datetime | None
    last_captured_at: datetime | None
    cover_asset_id: uuid.UUID | None


@router.get("", response_model=list[PlaceOut])
async def list_places(
    _user: CurrentUser,
    db: DbDep,
    library_id: uuid.UUID | None = None,
    radius_km: float = Query(default=places_lib.DEFAULT_RADIUS_KM, gt=0, le=50),
    include_inferred: bool = Query(
        default=True, description="Include assets whose position was inferred"
    ),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[PlaceOut]:
    query = select(
        Asset.id,
        Asset.gps_lat,
        Asset.gps_lon,
        Asset.relative_path,
        Asset.captured_at,
        Asset.gps_source,
    ).where(
        Asset.gps_lat.is_not(None),
        Asset.gps_lon.is_not(None),
        Asset.availability == "online",
    )
    if library_id is not None:
        query = query.where(Asset.library_id == library_id)
    if not include_inferred:
        query = query.where(Asset.gps_source.is_distinct_from("inferred"))

    rows = (await db.execute(query)).all()
    located = [
        places_lib.LocatedAsset(
            asset_id=str(row[0]),
            lat=row[1],
            lon=row[2],
            relative_path=row[3],
            captured_at=row[4],
            inferred=row[5] == "inferred",
        )
        for row in rows
    ]
    clustered = places_lib.cluster(located, radius_km)[:limit]

    # One query for every candidate cover rather than one per place: the
    # cover is only there to give the card a picture, not to be exact.
    candidates = {uuid.UUID(m.asset_id) for place in clustered for m in place.members}
    with_thumbnails = set(
        (
            await db.execute(
                select(Derivative.asset_id).where(
                    Derivative.asset_id.in_(candidates),
                    Derivative.kind == "thumbnail",
                    Derivative.status == "ready",
                )
            )
        )
        .scalars()
        .all()
    )

    out = []
    for place in clustered:
        times = [m.captured_at for m in place.members if m.captured_at]
        cover = next(
            (m.asset_id for m in place.members if uuid.UUID(m.asset_id) in with_thumbnails),
            None,
        )
        out.append(
            PlaceOut(
                name=places_lib.name_for(place),
                lat=round(place.lat, 6),
                lon=round(place.lon, 6),
                radius_km=round(place.radius_km, 3),
                asset_count=len(place.members),
                inferred_count=sum(1 for m in place.members if m.inferred),
                first_captured_at=min(times) if times else None,
                last_captured_at=max(times) if times else None,
                cover_asset_id=uuid.UUID(cover) if cover else None,
            )
        )
    return out
