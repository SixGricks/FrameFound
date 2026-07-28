"""Celery tasks for the processing pipeline.

Stage chain: extract_metadata -> generate_derivatives (thumbs/posters/
waveforms, default queue) -> generate_proxy (FFmpeg transcode, media queue,
videos only). Every task is idempotent; a failed optional stage records its
error on the derivative row and never blocks the others.

Each task run creates and disposes its own engine — asyncpg pools are
event-loop-bound and each `asyncio.run` gets a fresh loop.
TODO(perf): move workers to a persistent-loop model if task volume demands it.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.celery_app import celery_app
from framefound.config import get_settings
from framefound.db.models import Asset, Library
from framefound.processing import derivatives as deriv
from framefound.processing.probe import probe_media
from framefound.scanner.paths import PathValidationError, safe_join

log = structlog.get_logger()

_ASSET_FIELDS = (
    "duration_s",
    "width",
    "height",
    "fps",
    "video_codec",
    "audio_codec",
    "sample_rate",
    "channels",
    "bitrate",
    "orientation",
    "camera_make",
    "camera_model",
    "lens",
    "focal_length_mm",
    "aperture_f",
    "shutter_speed",
    "iso",
    "gps_lat",
    "gps_lon",
    "captured_at",
)

LoadedHandler = Callable[[AsyncSession, Asset, Library, Path], Awaitable[None]]


async def _with_asset(asset_id: uuid.UUID, handler: LoadedHandler) -> None:
    """Common shell: fresh engine, load asset+library, resolve+validate path."""
    engine = create_async_engine(get_settings().db_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            asset = await db.get(Asset, asset_id)
            if asset is None:
                log.warning("processing.asset_gone", asset_id=str(asset_id))
                return
            library = await db.get(Library, asset.library_id)
            if library is None:
                return
            try:
                path = safe_join(Path(library.root_path), asset.relative_path)
            except PathValidationError:
                asset.processing_status = "path_rejected"
                await db.commit()
                log.error("processing.path_rejected", asset_id=str(asset_id))
                return
            if not path.is_file():
                asset.availability = "missing"
                await db.commit()
                return
            await handler(db, asset, library, path)
    finally:
        await engine.dispose()


async def _extract(db: AsyncSession, asset: Asset, library: Library, path: Path) -> None:
    asset.processing_status = "processing"
    await db.commit()
    fields = await asyncio.to_thread(probe_media, path, asset.media_type)
    for name in _ASSET_FIELDS:
        if name in fields:
            setattr(asset, name, fields[name])
    asset.processing_status = "ready"
    await db.commit()
    log.info("metadata.extracted", asset_id=str(asset.id), fields=sorted(fields.keys()))
    # Chain the visual stages now that duration/dimensions are known.
    generate_derivatives.delay(str(asset.id))
    if asset.media_type == "video" and library.generate_proxies:
        generate_proxy.delay(str(asset.id))


async def _visuals(db: AsyncSession, asset: Asset, library: Library, path: Path) -> None:
    await deriv.generate_visuals(db, get_settings().data_dir, asset, path)


async def _proxy(db: AsyncSession, asset: Asset, library: Library, path: Path) -> None:
    if asset.media_type != "video":
        return
    await deriv.generate_video_proxy(db, get_settings().data_dir, asset, library, path)


@celery_app.task(
    name="framefound.extract_metadata",
    queue="default",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def extract_metadata(asset_id: str) -> None:
    """Extract technical + capture metadata for one asset (idempotent)."""
    try:
        asyncio.run(_with_asset(uuid.UUID(asset_id), _extract))
    except Exception:
        log.error("metadata.failed", asset_id=asset_id, exc_info=True)
        raise


@celery_app.task(
    name="framefound.generate_derivatives",
    queue="default",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def generate_derivatives(asset_id: str) -> None:
    """Thumbnails, previews, posters, waveforms for one asset (idempotent)."""
    try:
        asyncio.run(_with_asset(uuid.UUID(asset_id), _visuals))
    except Exception:
        log.error("derivatives.failed", asset_id=asset_id, exc_info=True)
        raise


@celery_app.task(
    name="framefound.generate_proxy",
    queue="media",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def generate_proxy(asset_id: str) -> None:
    """Browser-playable H.264 proxy for one video asset (idempotent)."""
    try:
        asyncio.run(_with_asset(uuid.UUID(asset_id), _proxy))
    except Exception:
        log.error("proxy.failed", asset_id=asset_id, exc_info=True)
        raise
