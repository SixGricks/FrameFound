"""Asset browsing endpoints + signed media URLs + admin reprocessing."""

import math
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import Select, case, func, select
from sqlalchemy.sql.elements import UnaryExpression

from framefound.auth.deps import CurrentUser, DbDep, SettingsDep, require_admin
from framefound.db.models import (
    Asset,
    AssetTag,
    Derivative,
    Library,
    Look,
    ShotWindow,
    Tag,
    catalogued,
)
from framefound.looking import store as look_store
from framefound.media import folders
from framefound.media.folders import FolderError
from framefound.media.signing import SigningError, sign_media_url

router = APIRouter(prefix="/assets", tags=["assets"])

NEEDS_LIBRARY = "Choose a library to browse a folder."
# The most folders one level lists. A level with more (a library that is one
# flat folder per day for ten years) says so instead of sending them all.
MAX_FOLDERS = 500

# The orders `GET /assets` offers; `Asset.id` follows each to break ties.
SORTS: dict[str, tuple[UnaryExpression[Any], ...]] = {
    "recent": (Asset.first_indexed_at.desc(),),
    "captured": (Asset.captured_at.desc().nullslast(),),
    "captured_asc": (Asset.captured_at.asc().nullslast(),),
    "name": (Asset.filename.asc(),),
    "size": (Asset.size_bytes.desc(),),
    # Best first; what no model has an opinion on yet goes last rather than
    # reading as the worst. Among equals, the newest capture. NULLS LAST is
    # spelled out: PostgreSQL puts NULLs first in a descending order.
    "score": (
        Asset.look_score.desc().nullslast(),
        Asset.captured_at.desc().nullslast(),
    ),
}


class AssetSummary(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    relative_path: str
    filename: str
    media_type: str
    size_bytes: int
    mtime: datetime
    availability: str
    processing_status: str
    duration_s: float | None
    width: int | None
    height: int | None
    captured_at: datetime | None
    # What a local model made of it (framefound/looking): its score, 0-100,
    # and one line about it. For a clip, its best shot's. `looked_at` is set
    # only once nothing in it still needs looking at.
    look_score: int | None = None
    look_caption: str | None = None
    looked_at: datetime | None = None

    model_config = {"from_attributes": True}


class LookOut(BaseModel):
    """What a local model said of a photograph or one shot, and who said it.

    Model text, to be shown as text. `whole` is true for a photograph (or a
    clip judged from one picture), false for a shot, with its times.
    `complete`: on an asset, whether nothing in it still needs looking at
    (every shot of a clip); on a shot, always true. `demo`: made up by the
    demonstration, and the page says so."""

    score: int | None
    caption: str
    reason: str
    whole: bool
    start_ms: int | None
    end_ms: int | None
    complete: bool
    looked_at: datetime
    model: str
    model_revision: str
    revision_short: str
    variant: str
    runtime: str
    runtime_version: str
    prompt_version: str
    demo: bool

    model_config = {"protected_namespaces": ()}


class AssetDetail(AssetSummary):
    extension: str
    mime_type: str | None
    partial_hash: str | None
    content_hash: str | None
    fps: float | None
    video_codec: str | None
    audio_codec: str | None
    sample_rate: int | None
    channels: int | None
    bitrate: int | None
    orientation: int | None
    camera_make: str | None
    camera_model: str | None
    lens: str | None
    focal_length_mm: float | None
    aperture_f: float | None
    shutter_speed: str | None
    iso: int | None
    gps_lat: float | None
    gps_lon: float | None
    gps_source: str | None
    gps_confidence: float | None
    rating: int | None
    favorite: bool
    archived: bool
    title: str | None
    description: str | None
    custom_fields: dict[str, Any]
    first_indexed_at: datetime
    # none (no audio) | speech | no_speech | queued | failed | off. Lets the
    # page say *why* there are no words beside a clip instead of nothing.
    transcript_state: str = "none"
    # A local model's look (framefound/looking): a photograph's, or a clip's
    # best shot's. Null when nothing of it has been looked at.
    look: LookOut | None = None


class AssetPage(BaseModel):
    items: list[AssetSummary]
    total: int
    page: int
    page_size: int


@router.get("", response_model=AssetPage)
async def list_assets(
    _user: CurrentUser,
    db: DbDep,
    library_id: uuid.UUID | None = None,
    media_type: str | None = Query(default=None, pattern="^(image|video|audio)$"),
    availability: str | None = Query(default=None, pattern="^(online|missing|unmounted)$"),
    status: str | None = Query(default=None, max_length=30),
    previewable: bool = Query(
        default=False, description="Only assets that already have a thumbnail"
    ),
    tag: str | None = Query(
        default=None, max_length=140, description="Tag slug; confirmed tags only"
    ),
    include_suggested_tags: bool = Query(
        default=False, description="With `tag`, also match assets where it is only suggested"
    ),
    sort: str = Query(default="recent", pattern="^(recent|captured|captured_asc|name|size|score)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    folder: str | None = Query(
        default=None,
        description='A folder of `library_id` ("" is its top); absent = no folder filter',
    ),
    deep: bool = Query(
        default=False, description="With `folder`, also everything in the folders inside it"
    ),
) -> AssetPage:
    query = select(Asset).where(catalogued())
    if library_id is not None:
        query = query.where(Asset.library_id == library_id)
    if folder is not None:
        # A path means nothing without its library: the same folder name is
        # in every library that came off the same card.
        if library_id is None:
            raise HTTPException(400, NEEDS_LIBRARY)
        try:
            query = query.where(*(folders.under(folder) if deep else folders.directly_in(folder)))
        except FolderError as err:
            raise HTTPException(400, str(err)) from err
    if media_type is not None:
        query = query.where(Asset.media_type == media_type)
    if availability is not None:
        query = query.where(Asset.availability == availability)
    if status is not None:
        query = query.where(Asset.processing_status == status)
    if previewable:
        ready_thumbs = select(Derivative.asset_id).where(
            Derivative.kind == "thumbnail", Derivative.status == "ready"
        )
        query = query.where(Asset.id.in_(ready_thumbs))

    if tag:
        # Confirmed by default. A suggestion is a claim awaiting judgement, and
        # a browse filter that silently mixed the two would make the catalogue
        # look more certain than it is.
        wanted = (
            ("manual", "confirmed", "suggested")
            if include_suggested_tags
            else (
                "manual",
                "confirmed",
            )
        )
        tagged = (
            select(AssetTag.asset_id)
            .join(Tag, Tag.id == AssetTag.tag_id)
            .where(Tag.slug == tag, AssetTag.source.in_(wanted))
        )
        query = query.where(Asset.id.in_(tagged))

    order = SORTS[sort]

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    # Asset.id breaks ties. The scanner commits 500 assets per transaction and
    # Postgres's now() is per-transaction, so "recent" alone has runs of 500
    # identical keys — and offset paging over ties repeats or skips rows at
    # every page boundary that falls inside one.
    rows = (
        await db.execute(
            query.order_by(*order, Asset.id).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars()
    return AssetPage(
        items=[AssetSummary.model_validate(a) for a in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


# --------------------------------------------------------------- folders
#
# Browse's folder level: one level of a library at a time, answered from the
# catalogue (what the scanner wrote), never from the disk — so it works with
# the share away, and a path from the URL is only ever compared, never opened.
# Registered before `/{asset_id}` for the reason given at `/near` below.


class FolderCrumb(BaseModel):
    name: str
    path: str


class FolderCounts(BaseModel):
    """`items` is every catalogued asset (audio too); `photos` and `clips`
    are the images and the videos among them."""

    items: int = 0
    photos: int = 0
    clips: int = 0


class FolderInside(FolderCounts):
    # Photographs and clips with nothing left for a local model to look at
    # (`assets.looked_at` set): a clip counts once every shot has been.
    looked: int = 0


class FolderRow(FolderInside):
    name: str
    path: str
    # The latest capture time beneath the folder (else the file's mtime).
    newest: datetime | None = None


class FolderLevel(BaseModel):
    library_id: uuid.UUID
    library_name: str
    path: str
    crumbs: list[FolderCrumb]
    folders: list[FolderRow]
    here: FolderCounts
    inside: FolderInside
    truncated: bool


def _looked() -> Any:
    """1 for a photograph or clip with nothing left to look at, else 0.

    Audio is never looked at, so it is never counted, whatever the column says.
    """
    return case(
        (Asset.media_type.in_(("image", "video")) & Asset.looked_at.is_not(None), 1), else_=0
    )


def folder_level_query(library_id: uuid.UUID, path: str, dialect: str) -> Select[Any]:
    """One row per folder directly inside `path`, plus one named "" for the
    files directly in it, each with what it holds; by name, "" first.

    Grouped through a subquery (see `folders.child_name` for why PostgreSQL
    needs that). Raises FolderError for a path that is not a folder.
    """
    beneath = (
        select(
            folders.child_name(path, dialect).label("name"),
            Asset.media_type.label("media_type"),
            func.coalesce(Asset.captured_at, Asset.mtime).label("taken"),
            _looked().label("looked"),
        )
        .where(Asset.library_id == library_id, catalogued(), *folders.under(path))
        .subquery()
    )
    return (
        select(
            beneath.c.name,
            func.count().label("items"),
            func.sum(case((beneath.c.media_type == "image", 1), else_=0)).label("photos"),
            func.sum(case((beneath.c.media_type == "video", 1), else_=0)).label("clips"),
            func.sum(beneath.c.looked).label("looked"),
            func.max(beneath.c.taken).label("newest"),
        )
        .group_by(beneath.c.name)
        .order_by(beneath.c.name)
    )


@router.get("/folders", response_model=FolderLevel)
async def folder_level(
    _user: CurrentUser,
    db: DbDep,
    library_id: uuid.UUID | None = None,
    path: str = Query(default="", description='A folder of the library; "" is its top'),
) -> FolderLevel:
    """One level of a library: the folders directly inside `path`, each with
    what is beneath it, and the counts for `path` itself. A path nothing is
    catalogued under is an empty level, not an error: a folder renamed on
    the share since the last scan still opens and says so."""
    if library_id is None:
        raise HTTPException(400, NEEDS_LIBRARY)
    try:
        path = folders.clean_folder(path)
    except FolderError as err:
        raise HTTPException(400, str(err)) from err
    library = await db.get(Library, library_id)
    if library is None:
        raise HTTPException(404, "No such library.")

    dialect = db.get_bind().dialect.name
    # One past the cap, plus the "" row, tells "exactly 500" from "more".
    rows = (
        await db.execute(folder_level_query(library_id, path, dialect).limit(MAX_FOLDERS + 2))
    ).all()
    here = FolderCounts()
    children: list[FolderRow] = []
    for name, items, photos, clips, looked, newest in rows:
        if name == "":
            here = FolderCounts(items=items, photos=photos or 0, clips=clips or 0)
            continue
        children.append(
            FolderRow(
                name=name,
                path=f"{path}/{name}" if path else name,
                items=items,
                photos=photos or 0,
                clips=clips or 0,
                looked=looked or 0,
                newest=newest,
            )
        )

    total = (
        await db.execute(
            select(
                func.count(),
                func.sum(case((Asset.media_type == "image", 1), else_=0)),
                func.sum(case((Asset.media_type == "video", 1), else_=0)),
                func.sum(_looked()),
            ).where(Asset.library_id == library_id, catalogued(), *folders.under(path))
        )
    ).one()
    return FolderLevel(
        library_id=library.id,
        library_name=library.name,
        path=path,
        crumbs=[FolderCrumb(name=name, path=crumb) for name, crumb in folders.crumbs(path)],
        folders=children[:MAX_FOLDERS],
        here=here,
        inside=FolderInside(
            items=total[0], photos=total[1] or 0, clips=total[2] or 0, looked=total[3] or 0
        ),
        truncated=len(children) > MAX_FOLDERS,
    )


# `/near` is registered before `/{asset_id}`: FastAPI matches in declaration
# order, so a literal path placed after a parameterised one is unreachable —
# `near` gets parsed as an asset UUID and rejected before the handler runs.
# This was live for a while; tests/test_places_api.py now guards it.
class NearbyAsset(BaseModel):
    asset_id: uuid.UUID
    filename: str
    media_type: str
    distance_km: float
    gps_lat: float
    gps_lon: float
    gps_source: str | None
    gps_confidence: float | None
    captured_at: datetime | None


@router.get("/near", response_model=list[NearbyAsset])
async def assets_near(
    _user: CurrentUser,
    db: DbDep,
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    radius_km: float = Query(default=1.0, gt=0, le=500),
    library_id: uuid.UUID | None = None,
    limit: int = Query(default=60, ge=1, le=200),
) -> list[NearbyAsset]:
    """Everything shot within a radius, closest first.

    A bounding box narrows the candidates using the lat/lon indexes, then an
    exact haversine check trims the corners the box included.
    """
    from framefound.media.geo import bounding_box, haversine_km

    min_lat, max_lat, min_lon, max_lon = bounding_box(lat, lon, radius_km)
    query = select(Asset).where(
        catalogued(),
        Asset.gps_lat.is_not(None),
        Asset.gps_lat.between(min_lat, max_lat),
        Asset.gps_lon.between(min_lon, max_lon),
    )
    if library_id is not None:
        query = query.where(Asset.library_id == library_id)

    # Nearest first, in SQL: a planar approximation of the distance is
    # monotonic with the real one at these radii. The query used to take the
    # first limit×4 rows of the box in whatever order the planner returned
    # them and sort those, so on a shoot with hundreds of frames in one field
    # "closest" was neither complete nor stable between calls.
    cos_lat = math.cos(math.radians(lat))
    dlat = Asset.gps_lat - lat
    dlon = (Asset.gps_lon - lon) * cos_lat
    query = query.order_by(dlat * dlat + dlon * dlon).limit(limit * 2)

    scored = []
    for asset in (await db.execute(query)).scalars():
        distance = haversine_km(lat, lon, asset.gps_lat or 0.0, asset.gps_lon or 0.0)
        if distance <= radius_km:
            scored.append((distance, asset))
    scored.sort(key=lambda row: row[0])

    return [
        NearbyAsset(
            asset_id=asset.id,
            filename=asset.filename,
            media_type=asset.media_type,
            distance_km=round(distance, 3),
            gps_lat=asset.gps_lat or 0.0,
            gps_lon=asset.gps_lon or 0.0,
            gps_source=asset.gps_source,
            gps_confidence=asset.gps_confidence,
            captured_at=asset.captured_at,
        )
        for distance, asset in scored[:limit]
    ]


@router.get("/{asset_id}", response_model=AssetDetail)
async def get_asset(asset_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> AssetDetail:
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    detail = AssetDetail.model_validate(asset)
    detail.transcript_state = await _transcript_state(db, asset)
    detail.look = await _asset_look(db, asset)
    return detail


def _look_out(look: Look, *, complete: bool) -> LookOut:
    return LookOut(
        score=look.score,
        caption=look.caption,
        reason=look.reason,
        whole=look.start_ms is None,
        start_ms=look.start_ms,
        end_ms=look.end_ms,
        complete=complete,
        looked_at=look.created_at,
        model=look.model,
        model_revision=look.model_revision,
        revision_short=look.model_revision[:7],
        variant=look.variant,
        runtime=look.runtime,
        runtime_version=look.runtime_version,
        prompt_version=look.prompt_version,
        demo=look.demo,
    )


async def _asset_look(db: DbDep, asset: Asset) -> LookOut | None:
    """The look the asset page shows: the one the roll-up wrote onto the
    asset's own row (`look_score`, `look_caption`), which search, culling and
    the grid read. Which look that is, is the store's answer and nobody
    else's (`store.best_current`): a photograph's look; a clip's
    best-scoring shot whose times are still one of its windows (of equals,
    the earliest), or its look of the whole while it has no windows. A look
    left over from before a re-analysis moved the shots is not shown."""
    best = await look_store.best_current(db, asset)
    return None if best is None else _look_out(best, complete=asset.looked_at is not None)


class ShotOut(BaseModel):
    """One shot of a clip, as the motion analysis measured it, and what a
    local model said of it (null until it has looked)."""

    idx: int
    start_ms: int
    end_ms: int
    # The middle still's moment: the page shows /media/{id}/frame?ts=<still_ms>.
    still_ms: int
    move: str
    direction: str | None
    smooth: float
    energy: str
    light: str | None
    sky: str | None
    # What is in it, most certain first (the catalogue's guess, not a model's).
    subjects: list[str]
    look: LookOut | None


# The most subjects a shot lists; the analysis keeps a longer tail of weak guesses.
SHOT_SUBJECTS = 5


@router.get("/{asset_id}/shots", response_model=list[ShotOut])
async def get_shots(asset_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> list[ShotOut]:
    """A clip's shots in order, each with its look; [] for a photograph or a
    clip whose shots have not been found. A look is joined on the shot's
    times, never on the window's id, which every re-analysis replaces.
    Every shot shows its own current look (`store.current`): nothing is
    chosen among them here, which is the asset's look above."""
    from framefound.processing.shots import window_moments

    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    if asset.media_type != "video":
        return []
    windows = (
        await db.scalars(
            select(ShotWindow).where(ShotWindow.asset_id == asset_id).order_by(ShotWindow.idx)
        )
    ).all()
    if not windows:
        return []
    looks = await look_store.current(db, asset_id)
    found: list[ShotOut] = []
    for window in windows:
        look = looks.get((window.start_ms, window.end_ms))
        subjects = window.subjects if isinstance(window.subjects, dict) else {}
        named = sorted(subjects, key=lambda name: (-float(subjects[name] or 0.0), name))
        measured = window.look if isinstance(window.look, dict) else {}
        sky = measured.get("sky")
        found.append(
            ShotOut(
                idx=window.idx,
                start_ms=window.start_ms,
                end_ms=window.end_ms,
                still_ms=window_moments(window.start_ms, window.end_ms)[1],
                move=window.move,
                direction=window.direction,
                smooth=window.smooth,
                energy=window.energy,
                light=window.light,
                sky=sky if isinstance(sky, str) else None,
                subjects=named[:SHOT_SUBJECTS],
                look=None if look is None else _look_out(look, complete=True),
            )
        )
    return found


async def _transcript_state(db: DbDep, asset: Asset) -> str:
    from framefound.db.models import Job, Library, Transcript

    if asset.media_type != "audio" and asset.audio_codec is None:
        return "none"
    from datetime import UTC

    from framefound.scanner.__main__ import BROKER_REDELIVERY_AGE, LEGACY_NO_SPEECH_MODEL

    row = (
        await db.execute(
            select(Transcript.segment_count, Transcript.model_name).where(
                Transcript.asset_id == asset.id
            )
        )
    ).first()
    # Migration 0027's placeholder is not a verdict: the old code heard
    # nothing in clips the current filter and stream mix do hear words in,
    # and the sweep queues each one again. Read it like no row at all.
    legacy = row is not None and row.model_name == LEGACY_NO_SPEECH_MODEL
    if row is not None and not legacy:
        return "speech" if row.segment_count > 0 else "no_speech"
    library = await db.get(Library, asset.library_id)
    if library is not None and not library.transcribe_enabled:
        return "off"

    last = (
        await db.execute(
            select(Job.status, Job.started_at)
            .where(Job.asset_id == asset.id, Job.task_name == "transcribe_asset")
            .order_by(Job.started_at.desc())
            .limit(1)
        )
    ).first()
    if last is None:
        return "queued"  # never tried: the scanner's sweep will queue it
    status, started = last
    if status in ("running", "abandoned"):
        # Still coming until the broker can no longer redeliver it: a run
        # killed by a deploy returns when the visibility timeout runs out,
        # and the sweep waits for that copy too. Saying "failed, reprocess"
        # in that window invited the manual run that heard the file twice.
        started = started if started.tzinfo else started.replace(tzinfo=UTC)
        return "queued" if datetime.now(UTC) - started < BROKER_REDELIVERY_AGE else "failed"
    if status == "succeeded":
        # The old code's run, kept as 0027's placeholder: the sweep hears it
        # again. Otherwise a run that finished and left no row: before every
        # run recorded an empty transcript, this was how a clip that heard
        # nothing looked, and "queued" promised a transcript no job would
        # bring (the sweep counts a succeeded run as the asset's turn).
        return "queued" if legacy else "no_speech"
    if status == "failed":
        return "failed"
    # "skipped" while the library is now on: the run never listened, so the
    # sweep picks the asset up again (it only counts succeeded runs).
    return "queued"


class TranscriptSegmentOut(BaseModel):
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None
    confidence: float | None

    model_config = {"from_attributes": True}


class TranscriptOut(BaseModel):
    language: str
    language_confidence: float | None
    model_name: str
    processed_at: datetime
    segment_count: int
    segments: list[TranscriptSegmentOut]


@router.get("/{asset_id}/transcript", response_model=TranscriptOut)
async def get_transcript(asset_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> TranscriptOut:
    from sqlalchemy.orm import selectinload

    from framefound.db.models import Transcript

    transcript = (
        await db.execute(
            select(Transcript)
            .where(Transcript.asset_id == asset_id)
            .options(selectinload(Transcript.segments))
        )
    ).scalar_one_or_none()
    if transcript is None:
        raise HTTPException(404, "No transcript is available for this item")
    return TranscriptOut(
        language=transcript.language,
        language_confidence=transcript.language_confidence,
        model_name=transcript.model_name,
        processed_at=transcript.processed_at,
        segment_count=transcript.segment_count,
        segments=[TranscriptSegmentOut.model_validate(s) for s in transcript.segments],
    )


@router.get("/{asset_id}/subtitles")
async def download_subtitles(
    asset_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    format: str = Query(default="srt", pattern="^(srt|vtt)$"),
) -> Response:
    """The transcript as a caption file to download: SRT for Premiere and
    YouTube, VTT for the web. Built from the stored segments, so it always
    matches what search and the transcript panel show."""
    from sqlalchemy.orm import selectinload

    from framefound.ai.transcription import SpeechSegment
    from framefound.db.models import Transcript
    from framefound.media.subtitles import build_srt, build_vtt

    asset = await db.get(Asset, asset_id)
    transcript = (
        await db.execute(
            select(Transcript)
            .where(Transcript.asset_id == asset_id)
            .options(selectinload(Transcript.segments))
        )
    ).scalar_one_or_none()
    if asset is None or transcript is None or not transcript.segments:
        raise HTTPException(404, "No transcript is available for this item")
    cues = [
        SpeechSegment(start_s=s.start_ms / 1000, end_s=s.end_ms / 1000, text=s.text)
        for s in transcript.segments
    ]
    body = build_srt(cues) if format == "srt" else build_vtt(cues)
    stem = asset.filename.rsplit(".", 1)[0] or "transcript"
    # ASCII-only fallback name plus the RFC 5987 one, so a filename with an
    # accent or an em dash survives on both Windows and macOS browsers.
    ascii_name = "".join(ch if ch.isascii() and ch not in '"\\' else "_" for ch in stem)
    return Response(
        content=body,
        media_type="application/x-subrip" if format == "srt" else "text/vtt",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}.{format}"; '
                f"filename*=UTF-8''{quote(stem)}.{format}"
            )
        },
    )


@router.get("/{asset_id}/nearby", response_model=list[NearbyAsset])
async def assets_near_asset(
    asset_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    radius_km: float = Query(default=1.0, gt=0, le=500),
    limit: int = Query(default=60, ge=1, le=200),
) -> list[NearbyAsset]:
    """Everything shot near this asset — 'what else did we get here?'"""
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    if asset.gps_lat is None or asset.gps_lon is None:
        raise HTTPException(404, "This item has no location")
    nearby = await assets_near(
        _user, db, lat=asset.gps_lat, lon=asset.gps_lon, radius_km=radius_km, limit=limit + 1
    )
    return [hit for hit in nearby if hit.asset_id != asset_id][:limit]


class FrameOut(BaseModel):
    ts_ms: int
    is_scene_change: bool
    scene_number: int | None
    url: str


@router.get("/{asset_id}/scenes", response_model=list[FrameOut])
async def get_scenes(asset_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> list[FrameOut]:
    """Sampled frames for the timeline strip (scene changes flagged)."""
    from framefound.db.models import Frame

    # Three columns, not the row: a long clip has up to 240 frames, each
    # carrying a 512-float embedding the strip never looks at.
    frames = (
        await db.execute(
            select(Frame.ts_ms, Frame.is_scene_change, Frame.scene_number)
            .where(Frame.asset_id == asset_id)
            .order_by(Frame.ts_ms)
        )
    ).all()
    return [
        FrameOut(
            ts_ms=ts_ms,
            is_scene_change=is_scene_change,
            scene_number=scene_number,
            url=f"/api/v1/media/{asset_id}/frame?ts={ts_ms}",
        )
        for ts_ms, is_scene_change, scene_number in frames
    ]


class MediaUrls(BaseModel):
    """Signed, short-lived URLs for each ready derivative of an asset."""

    urls: dict[str, str]
    expires_in_seconds: int


@router.get("/{asset_id}/urls", response_model=MediaUrls)
async def signed_media_urls(
    asset_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    ttl: int = Query(default=3600, ge=60, le=86400),
) -> MediaUrls:
    if await db.get(Asset, asset_id) is None:
        raise HTTPException(404, "Asset not found")
    kinds = (
        (
            await db.execute(
                select(Derivative.kind).where(
                    Derivative.asset_id == asset_id, Derivative.status == "ready"
                )
            )
        )
        .scalars()
        .all()
    )
    urls: dict[str, str] = {}
    for kind in kinds:
        try:
            expires, sig = sign_media_url(settings.secret_key, asset_id, kind, ttl)
        except SigningError as err:
            raise HTTPException(503, "Media links are not configured on this server") from err
        urls[kind] = f"/api/v1/media/{asset_id}/{kind}?exp={expires}&sig={sig}"
    return MediaUrls(urls=urls, expires_in_seconds=ttl)


@router.post("/{asset_id}/reprocess", status_code=202, dependencies=[require_admin])
async def reprocess_asset(asset_id: uuid.UUID, db: DbDep, _user: CurrentUser) -> dict[str, str]:
    """Re-run derivative generation for one asset (admin)."""
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    try:
        from framefound.processing.tasks import generate_derivatives, generate_proxy

        generate_derivatives.delay(str(asset_id))
        if asset.media_type == "video":
            generate_proxy.delay(str(asset_id))
        if asset.media_type == "audio" or asset.audio_codec is not None:
            # Reprocess is how a better model or a fixed share gets a second
            # listen; the sweep only ever re-queues files never heard at all.
            from framefound.processing.tasks import transcribe_asset

            transcribe_asset.delay(str(asset_id))
    except Exception as err:  # queue down
        raise HTTPException(503, "Processing queue is not available right now") from err
    return {"status": "queued"}
