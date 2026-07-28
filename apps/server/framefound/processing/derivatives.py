"""Derivative generation orchestration.

Each kind is attempted independently: a failed proxy never blocks a poster.
Results land in the `derivatives` table with per-kind status; files live under
<data_dir>/derivatives/<id[:2]>/<asset_id>/<kind>.<ext> (relative paths in the
DB — the data volume is relocatable).
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import Asset, Derivative, Library
from framefound.processing import ffmpeg
from framefound.processing.thumbnails import ThumbnailError, make_image_derivative

log = structlog.get_logger()

THUMBNAIL_EDGE = 512
PREVIEW_EDGE = 2048


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
    path = data_dir / derivative.relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def generate_visuals(db: AsyncSession, data_dir: Path, asset: Asset, source: Path) -> None:
    """Thumbnail/preview (image), poster+thumbnail (video), waveform (audio)."""
    import asyncio

    if asset.media_type == "image":
        for kind, edge in (("thumbnail", THUMBNAIL_EDGE), ("preview", PREVIEW_EDGE)):
            derivative = await _upsert(db, asset.id, kind, "webp")
            target = _abs(data_dir, derivative)
            try:
                size = await asyncio.to_thread(make_image_derivative, source, target, edge)
                await _finish(db, derivative, target, size_hint=size)
            except ThumbnailError as err:
                await _fail(db, derivative, str(err))

    elif asset.media_type == "video":
        poster = await _upsert(db, asset.id, "poster", "jpeg")
        poster_path = _abs(data_dir, poster)
        # A frame ~10% in dodges black lead-ins; the floor must never pass the
        # end of the clip (sub-second Premiere previews exist in the wild).
        duration = asset.duration_s or 0.0
        at = min(max(duration * 0.10, min(1.0, duration * 0.5)), 30.0) if duration else 0.0
        try:
            await asyncio.to_thread(ffmpeg.extract_poster, source, poster_path, at)
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

    derivative = await _upsert(db, asset.id, "proxy", "mp4")
    target = _abs(data_dir, derivative)
    try:
        codec = await asyncio.to_thread(
            ffmpeg.transcode_proxy, source, target, library.proxy_resolution
        )
        await _finish(db, derivative, target, codec=codec)
        log.info("proxy.generated", asset_id=str(asset.id), codec=codec)
    except ffmpeg.FfmpegError as err:
        await _fail(db, derivative, str(err))
