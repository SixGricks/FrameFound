"""Derivative generation orchestration.

Each kind is attempted independently: a failed proxy never blocks a poster.
Results land in the `derivatives` table with per-kind status; files live under
<data_dir>/derivatives/<id[:2]>/<asset_id>/<kind>.<ext> (relative paths in the
DB — the data volume is relocatable). Proxies alone may hang off a separate
proxy store instead, under the same relative path (storage/stores.py), as may
the other bulky kinds: slideshow renders, listing exports, basemaps.
"""

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import Asset, Derivative, Library
from framefound.processing import ffmpeg
from framefound.processing.thumbnails import ThumbnailError, make_image_derivative
from framefound.storage import stores
from framefound.storage.worker_roots import NotHere

log = structlog.get_logger()

THUMBNAIL_EDGE = 512
PREVIEW_EDGE = 2048
# How long a writer waits for a move of its kind to finish before giving up
# with the move's own message. Longer than the move waits for writers
# (moves.WRITER_WAIT_S), so the two never give up on each other at once.
MOVE_WAIT_S = 30 * 60.0
MOVE_POLL_S = 5.0


class OutOfSpace(RuntimeError):
    """Free space on the derivative store is below the configured floor."""


class StoreUnavailable(OutOfSpace):
    """A kind's own store (storage/stores.py) is not connected, or is in the
    last steps of being moved.

    The same kind of pause as a full disk — nothing wrong with the clip or
    the slideshow, and the work can be done once the store is back — so it
    shares the exception the task wrappers already treat as "wait", not
    "fail"."""


class ProxyStoreUnavailable(StoreUnavailable):
    """The same, for the proxy store: the first kind to have one, and the
    name the proxy code already catches."""


def free_gb(data_dir: Path) -> float | None:
    import shutil

    try:
        return shutil.disk_usage(data_dir).free / 1024**3
    except OSError:
        return None


def ensure_space(data_dir: Path) -> None:
    """Refuse to generate when the store is nearly full.

    Filling the disk would take Postgres down with it, so generation stops
    while there is still headroom. Derivatives are disposable — pausing them
    costs previews, never catalog integrity or originals.
    """
    from framefound.config import get_settings

    floor = get_settings().min_free_gb
    available = free_gb(data_dir)
    if available is not None and available < floor:
        raise OutOfSpace(
            f"Only {available:.1f} GB free for previews; generation pauses below {floor:.0f} GB"
        )


def ensure_store(kind: str) -> None:
    """Refuse to write `kind` (storage/stores.py) while its store is away or
    being moved. Asked before anything is created: `mkdir` on a share that is
    not mounted would start filling the empty folder it mounts on."""
    # A worker on another machine with no setting for where the server keeps
    # this kind hands the job back (celery_app.FrameFoundTask) rather than
    # write where the server no longer looks.
    elsewhere = stores.not_here(kind)
    if elsewhere is not None:
        raise NotHere(elsewhere)
    problem = stores.write_problem(kind)
    if problem is not None:
        raise (ProxyStoreUnavailable if kind == "proxies" else StoreUnavailable)(problem)


def store_ready(kind: str, sleep: Callable[[float], None] = time.sleep) -> None:
    """`ensure_store`, after waiting out a move of `kind` that is in its last
    steps (storage/moves.py).

    That pause is short — the time it takes to copy what changed during the
    long copy — and nothing queues a proxy a second time, so a clip that
    reached the front of the queue just then waits rather than being
    dropped; a finished render waits to be put where renders now live.
    Blocking: tasks call it in a thread. A store that is away is not waited
    for; that can last days, and raises at once."""
    from framefound.config import get_settings

    data_dir = get_settings().data_dir
    waited = 0.0
    while waited < MOVE_WAIT_S and stores.read_state(data_dir).is_paused(kind):
        sleep(MOVE_POLL_S)
        waited += MOVE_POLL_S
    ensure_store(kind)


def derivative_relpath(asset_id: uuid.UUID, kind: str, ext: str) -> str:
    aid = str(asset_id)
    return f"derivatives/{aid[:2]}/{aid}/{kind}.{ext}"


async def _upsert(db: AsyncSession, asset_id: uuid.UUID, kind: str, ext: str) -> Derivative:
    derivative = (
        await db.execute(
            select(Derivative).where(Derivative.asset_id == asset_id, Derivative.kind == kind)
        )
    ).scalar_one_or_none()
    if derivative is None:
        derivative = Derivative(
            asset_id=asset_id,
            kind=kind,
            relative_path=derivative_relpath(asset_id, kind, ext),
            media_format=ext,
        )
        db.add(derivative)
        await db.flush()
    else:
        # Format may change between releases (e.g. poster webp -> jpeg);
        # regeneration adopts the current target.
        derivative.relative_path = derivative_relpath(asset_id, kind, ext)
        derivative.media_format = ext
    derivative.status = "pending"
    derivative.error = None
    return derivative


async def _finish(
    db: AsyncSession,
    derivative: Derivative,
    abs_path: Path,
    *,
    codec: str | None = None,
    size_hint: tuple[int, int] | None = None,
) -> None:
    derivative.status = "ready"
    derivative.generated_at = datetime.now(UTC)
    derivative.size_bytes = abs_path.stat().st_size
    derivative.codec = codec
    if size_hint:
        derivative.width, derivative.height = size_hint
    await db.commit()


async def _fail(db: AsyncSession, derivative: Derivative, message: str) -> None:
    derivative.status = "failed"
    derivative.error = message[:500]
    await db.commit()


def _abs(data_dir: Path, derivative: Derivative) -> Path:
    if stores.is_proxy(derivative.relative_path):
        # Never mkdir into a proxy store that is not there: on an unmounted
        # share that would start filling the folder it mounts on.
        ensure_store("proxies")
    path = stores.derivative_path(data_dir, derivative.relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def generate_visuals(db: AsyncSession, data_dir: Path, asset: Asset, source: Path) -> None:
    """Thumbnail/preview (image), poster+thumbnail (video), waveform (audio)."""
    import asyncio

    ensure_space(data_dir)

    if asset.media_type == "image":
        for kind, edge in (("thumbnail", THUMBNAIL_EDGE), ("preview", PREVIEW_EDGE)):
            derivative = await _upsert(db, asset.id, kind, "webp")
            target = _abs(data_dir, derivative)
            try:
                size = await asyncio.to_thread(make_image_derivative, source, target, edge)
                await _finish(db, derivative, target, size_hint=size)
            except ThumbnailError as err:
                await _fail(db, derivative, str(err))

    elif asset.media_type == "video" and asset.extension == "braw":
        from framefound.processing.braw import braw_decoder, extract_poster_braw

        poster = await _upsert(db, asset.id, "poster", "jpeg")
        poster_path = _abs(data_dir, poster)
        if braw_decoder() is None:
            await _fail(db, poster, "BRAW decoder is not installed on this server")
            return
        try:
            await asyncio.to_thread(extract_poster_braw, source, poster_path)
            await _finish(db, poster, poster_path)
        except ffmpeg.FfmpegError as err:
            await _fail(db, poster, str(err))
            return
        thumb = await _upsert(db, asset.id, "thumbnail", "webp")
        thumb_path = _abs(data_dir, thumb)
        try:
            size = await asyncio.to_thread(
                make_image_derivative, poster_path, thumb_path, THUMBNAIL_EDGE
            )
            await _finish(db, thumb, thumb_path, size_hint=size)
        except ThumbnailError as err:
            await _fail(db, thumb, str(err))

    elif asset.media_type == "video":
        poster = await _upsert(db, asset.id, "poster", "jpeg")
        poster_path = _abs(data_dir, poster)
        # A frame ~10% in dodges black lead-ins; the floor must never pass the
        # end of the clip (sub-second Premiere previews exist in the wild).
        duration = asset.duration_s or 0.0
        at = min(max(duration * 0.10, min(1.0, duration * 0.5)), 30.0) if duration else 0.0
        try:
            try:
                await asyncio.to_thread(ffmpeg.extract_poster, source, poster_path, at)
            except ffmpeg.FfmpegError:
                if at <= 0:
                    raise
                # Seek overshot (1-2 frame clips): take the first frame instead.
                await asyncio.to_thread(ffmpeg.extract_poster, source, poster_path, 0.0)
            await _finish(db, poster, poster_path)
        except ffmpeg.FfmpegError as err:
            await _fail(db, poster, str(err))
            return  # thumbnail derives from the poster
        thumb = await _upsert(db, asset.id, "thumbnail", "webp")
        thumb_path = _abs(data_dir, thumb)
        try:
            size = await asyncio.to_thread(
                make_image_derivative, poster_path, thumb_path, THUMBNAIL_EDGE
            )
            await _finish(db, thumb, thumb_path, size_hint=size)
        except ThumbnailError as err:
            await _fail(db, thumb, str(err))

    elif asset.media_type == "audio":
        derivative = await _upsert(db, asset.id, "waveform", "png")
        target = _abs(data_dir, derivative)
        try:
            await asyncio.to_thread(ffmpeg.render_waveform, source, target)
            await _finish(db, derivative, target)
        except ffmpeg.FfmpegError as err:
            await _fail(db, derivative, str(err))


async def generate_video_proxy(
    db: AsyncSession, data_dir: Path, asset: Asset, library: Library, source: Path
) -> None:
    import asyncio

    # Before the row is touched: a store that is away must not turn a ready
    # proxy's record into a pending one.
    await asyncio.to_thread(store_ready, "proxies")
    ensure_space(stores.proxy_root(data_dir))
    derivative = await _upsert(db, asset.id, "proxy", "mp4")
    target = _abs(data_dir, derivative)
    if asset.extension == "braw":
        # Full-clip BRAW decode on CPU is impractical; revisit in the GPU phase.
        await _fail(db, derivative, "BRAW proxies arrive with the GPU upgrade")
        return
    try:
        codec = await asyncio.to_thread(
            ffmpeg.transcode_proxy, source, target, library.proxy_resolution
        )
        placed = stores.derivative_path(data_dir, derivative.relative_path)
        if placed != target:
            # A transcode can run for hours, and proxies were moved to another
            # folder during this one (storage/moves.py). A move waits for a
            # file that is still growing, but one that had not yet written a
            # byte looks finished. The proxy follows the others: recorded as
            # ready where it was made, it would be a proxy that never plays.
            await asyncio.to_thread(store_ready, "proxies")
            await asyncio.to_thread(stores.put_in_place, target, placed)
            target = placed
        await _finish(db, derivative, target, codec=codec)
        log.info("proxy.generated", asset_id=str(asset.id), codec=codec)
    except ffmpeg.FfmpegError as err:
        await _fail(db, derivative, str(err))
