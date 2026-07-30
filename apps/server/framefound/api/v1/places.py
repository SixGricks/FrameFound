"""Places: located assets grouped into the shoots they came from.

Read-only and computed on demand. Persisting clusters would mean invalidating
them every time a scan adds a file or an inference fills a position, and the
whole located set is small enough that recomputing is cheaper than keeping a
cache honest.
"""

import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from framefound.auth.deps import CurrentUser, DbDep, require_admin
from framefound.db.models import Asset, Derivative
from framefound.media import places as places_lib
from framefound.media.geocoding import cache_key as geocode_key
from framefound.media.geocoding import reverse_geocode_many
from framefound.media.maps_store import MapsConfig, load_maps_config, save_maps_config

log = structlog.get_logger()

router = APIRouter(prefix="/places", tags=["places"])


class PlaceOut(BaseModel):
    name: str
    named_from: str  # folder | geocode | unknown
    lat: float
    lon: float
    radius_km: float
    asset_count: int
    inferred_count: int
    first_captured_at: datetime | None
    last_captured_at: datetime | None
    cover_asset_id: uuid.UUID | None


class MapConfigOut(BaseModel):
    """What the page needs to decide whether it can draw a basemap.

    The browser key is returned only to an authenticated session. It is public
    by nature — the Maps JS API reads it from the page — but keeping it out of
    the built bundle means an unauthenticated visitor cannot lift it.
    """

    basemap_enabled: bool
    browser_key: str
    geocoding_ready: bool
    provider: str
    style_url: str
    library_url: str
    stylesheet_url: str


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

    named = [(place, places_lib.name_for(place)) for place in clustered]

    # Only clusters the folder structure could not name are worth a lookup —
    # and only when the operator has turned geocoding on. Folder names are
    # street addresses; a gazetteer would answer with the county.
    addresses: dict[str, str] = {}
    maps = await load_maps_config(db)
    unnamed = [p for p, name in named if name == places_lib.UNKNOWN_NAME]
    if unnamed and maps.geocoding_ready and maps.geocode_unnamed_places:
        try:
            addresses = await reverse_geocode_many(
                db, [(p.lat, p.lon) for p in unnamed], maps.geocoding_key()
            )
        except Exception:
            # A geocoding outage must not take the page down with it.
            log.warning("places.geocode_unavailable", exc_info=True)

    out = []
    for place, name in named:
        times = [m.captured_at for m in place.members if m.captured_at]
        cover = next(
            (m.asset_id for m in place.members if uuid.UUID(m.asset_id) in with_thumbnails),
            None,
        )
        named_from = "folder"
        if name == places_lib.UNKNOWN_NAME:
            looked_up = addresses.get(geocode_key(place.lat, place.lon), "")
            name, named_from = (looked_up, "geocode") if looked_up else (name, "unknown")
        out.append(
            PlaceOut(
                name=name,
                named_from=named_from,
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


@router.get("/map-config", response_model=MapConfigOut)
async def map_config(_user: CurrentUser, db: DbDep) -> MapConfigOut:
    maps = await load_maps_config(db)
    ready = maps.basemap_ready
    return MapConfigOut(
        basemap_enabled=ready,
        # Only Google needs a key in the page, and only once the basemap is
        # actually on. MapLibre needs none at all when the tiles are yours.
        browser_key=(maps.browser_key() if ready and maps.provider == "google" else ""),
        geocoding_ready=maps.geocoding_ready,
        provider=maps.provider if ready else "none",
        style_url=maps.style_url if ready and maps.provider == "maplibre" else "",
        library_url=maps.library_url,
        stylesheet_url=maps.stylesheet_url,
    )


class MapsSettingsOut(BaseModel):
    """Never returns either key — only whether one is present.

    The browser key is readable through /map-config by an authenticated
    session because the page cannot load Maps without it. This settings view
    is about configuration state, so it reports presence and nothing more.
    """

    basemap_enabled: bool
    browser_key_configured: bool
    geocoding_key_configured: bool
    geocode_unnamed_places: bool
    provider: str
    style_url: str
    library_url: str
    stylesheet_url: str


class MapsSettingsUpdate(BaseModel):
    basemap_enabled: bool | None = None
    geocode_unnamed_places: bool | None = None
    provider: str | None = Field(default=None, pattern="^(none|maplibre|google)$")
    style_url: str | None = Field(default=None, max_length=2000)
    library_url: str | None = Field(default=None, max_length=2000)
    stylesheet_url: str | None = Field(default=None, max_length=2000)
    # Empty string clears a key; omitted leaves it untouched.
    browser_key: str | None = None
    geocoding_key: str | None = None


def _maps_out(config: MapsConfig) -> MapsSettingsOut:
    return MapsSettingsOut(
        basemap_enabled=config.basemap_enabled,
        browser_key_configured=bool(config.browser_key_sealed),
        geocoding_key_configured=bool(config.geocoding_key_sealed),
        geocode_unnamed_places=config.geocode_unnamed_places,
        provider=config.provider,
        style_url=config.style_url,
        library_url=config.library_url,
        stylesheet_url=config.stylesheet_url,
    )


@router.get("/maps-settings", response_model=MapsSettingsOut)
async def get_maps_settings(_user: CurrentUser, db: DbDep) -> MapsSettingsOut:
    return _maps_out(await load_maps_config(db))


@router.put("/maps-settings", response_model=MapsSettingsOut, dependencies=[require_admin])
async def update_maps_settings(
    body: MapsSettingsUpdate, _user: CurrentUser, db: DbDep
) -> MapsSettingsOut:
    """Turning the basemap on is an outbound-traffic decision, so it is
    admin-only and off until someone chooses it."""
    config = await load_maps_config(db)
    fields = body.model_dump(exclude_unset=True)
    if "basemap_enabled" in fields:
        config.basemap_enabled = bool(fields["basemap_enabled"])
    if "geocode_unnamed_places" in fields:
        config.geocode_unnamed_places = bool(fields["geocode_unnamed_places"])
    if "provider" in fields:
        config.provider = str(fields["provider"])
    for url_field in ("style_url", "library_url", "stylesheet_url"):
        if url_field in fields:
            value = (fields[url_field] or "").strip()
            # Only http(s). A javascript: or data: URL here would be injected
            # straight into a <script src> on every operator's browser.
            if value and not value.startswith(("http://", "https://", "/")):
                raise HTTPException(
                    status_code=400,
                    detail=f"{url_field} must be an http(s) URL or an absolute path",
                )
            setattr(config, url_field, value)
    if "browser_key" in fields:
        config.with_browser_key((fields["browser_key"] or "").strip())
    if "geocoding_key" in fields:
        config.with_geocoding_key((fields["geocoding_key"] or "").strip())
    await save_maps_config(db, config)
    log.info(
        "maps.settings_updated",
        basemap_enabled=config.basemap_enabled,
        browser_key=bool(config.browser_key_sealed),
        geocoding_key=bool(config.geocoding_key_sealed),
    )
    return _maps_out(config)
