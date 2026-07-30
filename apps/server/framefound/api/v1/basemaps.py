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

import re
from pathlib import Path

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from framefound.auth.deps import CurrentUser, SettingsDep, require_admin
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
