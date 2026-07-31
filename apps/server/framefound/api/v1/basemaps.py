"""Basemaps served by FrameFound itself.

The usual advice for self-hosted maps is "run a tile server", which means
another service, another few hundred megabytes of RAM, and another thing to
keep alive. On a host that is already over-committed that is a poor trade for
a background image.

PMTiles avoids all of it. A whole region is **one file**, addressed by HTTP
range requests — the same mechanism this application already implements for
video scrubbing. So the basemap is a download into the data directory and an
endpoint that serves byte ranges out of it. No tile server, no database, no
extra container, nothing to monitor.

Once a file is present, nothing about the map leaves the network: the tiles are
local and MapLibre renders them in the browser.
"""

import asyncio
import re
from pathlib import Path
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from framefound.auth.deps import CurrentUser, DbDep, SettingsDep, require_admin
from framefound.media.streaming import range_file_response

log = structlog.get_logger()

router = APIRouter(prefix="/basemaps", tags=["basemaps"])

# Where downloaded extracts live. Under data_dir so `manage.sh backup` already
# knows about it and a NAS cache drive can hold it.
SUBDIR = "basemaps"
# Filenames are operator-supplied; keep them boring so nothing can traverse.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MAX_BYTES = 32 * 1024**3  # a planet extract is ~100 GB; refuse to fill the disk

# The Protomaps planet build on Source Cooperative. Verified reachable and
# range-capable (206 + PMTiles magic); ~125 GB, which is far more than this
# host has free — hence extraction rather than download.
#
# An earlier version of this file guessed per-region URLs like
# `.../extracts/us-northeast.pmtiles`. They 404. There is no pre-built regional
# extract at a stable URL; the supported route is to pull a bounding box out of
# the planet archive over range requests, which is what PMTiles is designed for
# and why the format was chosen in the first place.
PLANET_URL = "https://data.source.coop/protomaps/openstreetmap/v4.pmtiles"

# Bounding boxes, west,south,east,north. Generous by a little: a map that stops
# at the state line looks broken when a job sits just over it.
CATALOGUE = [
    {
        "name": "pennsylvania",
        "label": "Pennsylvania (+ surrounding counties)",
        "bbox": "-80.9,39.4,-74.4,42.5",
        "approx_gb": 0.6,
    },
    {
        "name": "us-northeast",
        "label": "US Northeast (PA, NJ, NY, MD, DE, New England)",
        "bbox": "-81.0,38.4,-66.9,45.1",
        "approx_gb": 2.0,
    },
    {
        "name": "us",
        "label": "Continental United States",
        "bbox": "-125.0,24.4,-66.9,49.4",
        "approx_gb": 12.0,
    },
]


class Basemap(BaseModel):
    name: str
    label: str
    installed: bool
    size_gb: float | None
    approx_gb: float | None
    tiles_url: str


class BasemapList(BaseModel):
    basemaps: list[Basemap]
    installed_count: int
    note: str


class DownloadRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # west,south,east,north. Only needed for a region not in the catalogue.
    bbox: str = Field(default="", max_length=80)


def _basemap_dir(settings: SettingsDep) -> Path:
    path = Path(settings.data_dir) / SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(name: str) -> str:
    lowered = name.strip().lower()
    if not NAME_RE.match(lowered):
        raise HTTPException(
            status_code=400,
            detail="Name must be lowercase letters, digits, dot, dash or underscore",
        )
    return lowered


@router.get("", response_model=BasemapList)
async def list_basemaps(_user: CurrentUser, settings: SettingsDep) -> BasemapList:
    directory = _basemap_dir(settings)
    on_disk = {p.stem: p for p in directory.glob("*.pmtiles")}

    entries: list[Basemap] = []
    for item in CATALOGUE:
        name = str(item["name"])
        path = on_disk.pop(name, None)
        entries.append(
            Basemap(
                name=name,
                label=str(item["label"]),
                installed=path is not None,
                size_gb=round(path.stat().st_size / 1024**3, 2) if path else None,
                approx_gb=float(item["approx_gb"]),  # type: ignore[arg-type]
                tiles_url=f"/api/v1/basemaps/{name}/tiles.pmtiles",
            )
        )
    # Anything the operator put there by hand still shows up and still works.
    for name, path in sorted(on_disk.items()):
        entries.append(
            Basemap(
                name=name,
                label=f"{name} (added manually)",
                installed=True,
                size_gb=round(path.stat().st_size / 1024**3, 2),
                approx_gb=None,
                tiles_url=f"/api/v1/basemaps/{name}/tiles.pmtiles",
            )
        )

    return BasemapList(
        basemaps=entries,
        installed_count=sum(1 for e in entries if e.installed),
        note=(
            "A basemap is one file served straight out of your data directory. "
            "No tile server, no extra container, and once it is downloaded "
            "nothing about the map leaves your network."
        ),
    )


@router.get("/{name}/tiles.pmtiles")
async def serve_tiles(name: str, request: Request, _user: CurrentUser, settings: SettingsDep):  # type: ignore[no-untyped-def]
    """Serve byte ranges out of a PMTiles archive.

    MapLibre's pmtiles protocol asks for small ranges — a header, then a
    directory, then individual tiles — which is exactly what the video
    scrubbing path already does. Reusing it means no new streaming code and no
    new class of bug.
    """
    path = _basemap_dir(settings) / f"{_safe_name(name)}.pmtiles"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="That basemap is not installed")
    return range_file_response(request, path, "application/octet-stream")


@router.post("/download", status_code=202, dependencies=[require_admin])
async def download_basemap(
    body: DownloadRequest, _user: CurrentUser, settings: SettingsDep
) -> dict[str, str]:
    """Extract a region from the planet archive. Admin-only: it writes to disk.

    Extraction, not download: the planet is 125 GB and this host has 42 GB
    free, but PMTiles is addressable by range request so a bounding box can be
    pulled without fetching the rest.
    """
    name = _safe_name(body.name)
    known = {str(item["name"]): str(item["bbox"]) for item in CATALOGUE}
    bbox = (body.bbox or "").strip() or known.get(name, "")
    if not _valid_bbox(bbox):
        raise HTTPException(status_code=400, detail="Need a bounding box as west,south,east,north")

    target = _basemap_dir(settings) / f"{name}.pmtiles"
    if target.exists():
        return {"status": "already installed", "name": name}

    try:
        from framefound.processing.tasks import fetch_basemap

        fetch_basemap.delay(name, bbox)
    except Exception:
        raise HTTPException(status_code=503, detail="The processing queue is unavailable") from None
    log.info("basemap.extract_queued", name=name, bbox=bbox)
    return {"status": "extracting", "name": name}


def _valid_bbox(bbox: str) -> bool:
    """west,south,east,north within real coordinate ranges, correctly ordered."""
    parts = bbox.split(",")
    if len(parts) != 4:
        return False
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError:
        return False
    return -180 <= west < east <= 180 and -90 <= south < north <= 90


@router.delete("/{name}", status_code=204, dependencies=[require_admin])
async def delete_basemap(name: str, _user: CurrentUser, settings: SettingsDep) -> None:
    path = _basemap_dir(settings) / f"{_safe_name(name)}.pmtiles"
    # Regenerable by re-downloading, so this is a genuine delete rather than a
    # flag — but it is admin-only and explicit.
    path.unlink(missing_ok=True)
    log.info("basemap.deleted", name=name)


async def probe_url(url: str) -> int | None:
    """Content length, for showing a size before committing to a download."""
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.head(url)
        return int(response.headers.get("content-length", 0)) or None
    except Exception:
        return None


# --- serving the map ------------------------------------------------------
#
# Downloading an archive was only half the feature. MapLibre cannot render a
# `.pmtiles` file on its own: it needs a **style** describing what to draw, and
# a tile source it understands. Without both, an operator downloads 360 MB,
# sees no change on Places, and is given no reason why. That is what happened.
#
# Tiles are unpacked here rather than in the browser. The alternative is the
# `pmtiles://` protocol, which needs a second JavaScript library fetched from a
# CDN before a single tile can be read — an odd dependency for a feature whose
# entire selling point is that the map keeps working without the internet.
# Reading the archive server-side means the browser asks for ordinary
# `{z}/{x}/{y}` tiles and needs nothing it does not already have.

TILE_CACHE_SECONDS = 86400
# `pmtiles extract` was run with --maxzoom=14. Asking the archive for 15+
# returns nothing, so the map must be told to overzoom the last real level
# rather than go blank exactly when somebody leans in to look.
BASEMAP_MAXZOOM = 14


def _archive(settings: SettingsDep, name: str) -> Path:
    path = _basemap_dir(settings) / f"{_safe_name(name)}.pmtiles"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="That basemap is not installed")
    return path


@router.get("/{name}/tiles/{z}/{x}/{y}.mvt")
async def serve_tile(  # type: ignore[no-untyped-def]
    name: str, z: int, x: int, y: int, _user: CurrentUser, settings: SettingsDep
):
    """One vector tile, read straight out of the archive.

    A miss returns 204 rather than 404. PMTiles stores no tile where there is
    nothing to draw, which is the normal case over ocean or outside the
    extracted bounding box; answering 404 makes MapLibre log an error for every
    empty tile and turns a working map into a console full of noise.
    """
    from pmtiles.reader import MmapSource, Reader

    path = _archive(settings, name)

    def read() -> bytes | None:
        with open(path, "rb") as handle:
            reader = Reader(MmapSource(handle))
            # `get(z, x, y)`, not a packed tile id — the id form belongs to
            # the writer side of this library.
            tile: bytes | None = reader.get(z, x, y)
            return tile

    try:
        payload = await asyncio.to_thread(read)
    except Exception as exc:  # noqa: BLE001 - a corrupt archive should 404, not 500
        log.warning("basemap.tile_failed", name=name, z=z, x=x, y=y, error=str(exc)[:200])
        raise HTTPException(status_code=404, detail="That tile could not be read") from None

    if not payload:
        return Response(status_code=204)

    return Response(
        content=payload,
        media_type="application/vnd.mapbox-vector-tile",
        headers={
            # The archive is immutable once extracted, so this is safe to hold.
            "Cache-Control": f"private, max-age={TILE_CACHE_SECONDS}",
            # PMTiles stores vector tiles gzipped and returns them that way.
            "Content-Encoding": "gzip",
        },
    )


def _layer(
    ident: str,
    source_layer: str,
    kind: str,
    paint: dict[str, Any],
    filt: list[Any] | None = None,
    minzoom: int = 0,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": ident,
        "type": kind,
        "source": "framefound",
        "source-layer": source_layer,
        "paint": paint,
        "minzoom": minzoom,
    }
    if filt:
        out["filter"] = filt
    return out


@router.get("/{name}/style.json")
async def serve_style(
    name: str, request: Request, _user: CurrentUser, settings: SettingsDep
) -> dict[str, Any]:
    """A MapLibre style for one installed basemap.

    Deliberately geometry only — coastline, water, landuse, roads, buildings,
    boundaries — and no labels. Labels need font glyphs, which means either
    hosting a few megabytes of PBFs or fetching them from somebody else's
    server, and the second would quietly undo the promise this whole feature
    exists to keep. Rivers and roads are what a photograph needs behind it;
    place names can follow once the glyphs are served locally.

    Colours are the application's own palette rather than a cartographic one:
    the map is a backdrop for photo markers, and a bright basemap would compete
    with them.

    Layer names follow the Protomaps v4 schema, which is what the extract came
    from.
    """
    _archive(settings, name)
    base = str(request.base_url).rstrip("/")
    tiles = f"{base}/api/v1/basemaps/{_safe_name(name)}/tiles/{{z}}/{{x}}/{{y}}.mvt"

    return {
        "version": 8,
        "name": f"FrameFound — {name}",
        "sources": {
            "framefound": {
                "type": "vector",
                "tiles": [tiles],
                "minzoom": 0,
                "maxzoom": BASEMAP_MAXZOOM,
                "attribution": "© OpenStreetMap contributors",
            }
        },
        "layers": [
            {"id": "bg", "type": "background", "paint": {"background-color": "#1b1a17"}},
            _layer("earth", "earth", "fill", {"fill-color": "#26241f"}),
            _layer("landuse", "landuse", "fill", {"fill-color": "#2b2f26", "fill-opacity": 0.7}),
            _layer("natural", "natural", "fill", {"fill-color": "#2c3227", "fill-opacity": 0.6}),
            _layer("water", "water", "fill", {"fill-color": "#1d2b38"}),
            _layer("rivers", "physical_line", "line", {"line-color": "#1d2b38", "line-width": 1.2}),
            _layer(
                "boundaries",
                "boundaries",
                "line",
                {"line-color": "#4a453c", "line-width": 0.8, "line-dasharray": [3, 2]},
            ),
            _layer(
                "roads-minor",
                "roads",
                "line",
                {"line-color": "#3a3630", "line-width": 0.8},
                minzoom=11,
            ),
            _layer(
                "roads-major",
                "roads",
                "line",
                {"line-color": "#554e43", "line-width": 1.8},
                filt=["in", "kind", "highway", "major_road"],
            ),
            _layer(
                "buildings",
                "buildings",
                "fill",
                {"fill-color": "#332f29", "fill-opacity": 0.8},
                minzoom=13,
            ),
        ],
    }


class UseBasemapResponse(BaseModel):
    provider: str
    style_url: str
    note: str


@router.post("/{name}/use", response_model=UseBasemapResponse, dependencies=[require_admin])
async def use_basemap(
    name: str, _user: CurrentUser, db: DbDep, settings: SettingsDep
) -> UseBasemapResponse:
    """Point Places at this basemap.

    Exists because downloading an archive and *using* one were two unconnected
    settings and nothing said so — 360 MB arrived, Places looked identical, and
    there was no error to search for. An operator who downloads a map has
    already expressed the intent; making them then find Security → Maps and
    hand-enter a provider and a style URL is a puzzle, not a configuration step.
    """
    from framefound.media.maps_store import load_maps_config, save_maps_config

    _archive(settings, name)
    config = await load_maps_config(db)
    config.provider = "maplibre"
    config.basemap_enabled = True
    config.style_url = f"/api/v1/basemaps/{_safe_name(name)}/style.json"
    await save_maps_config(db, config)
    log.info("basemap.selected", name=name)
    return UseBasemapResponse(
        provider=config.provider,
        style_url=config.style_url,
        note=f"Places now draws on {name}. Nothing about the map leaves your network.",
    )
