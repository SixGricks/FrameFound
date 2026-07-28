"""Celery tasks for the processing pipeline.

Queues are segregated by latency class so bulk work never starves
user-visible work (learned in deployment when 8k metadata jobs delayed
thumbnails by hours):

  metadata - bulk extraction sweeps (high volume, moderate cost)
  visuals  - thumbnails/posters/waveforms (fast, user-visible)
  media    - proxy transcodes (heavy, long-running)
  default  - legacy name, still consumed so pre-rename queued jobs drain

Stage chain: extract_metadata -> generate_derivatives + generate_proxy.
Every task is idempotent; each execution writes a Job history row (the
processing dashboard's data source). Each task run creates and disposes its
own engine — asyncpg pools are event-loop-bound and each `asyncio.run` gets a
fresh loop. TODO(perf): persistent-loop workers if task volume demands it.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.celery_app import celery_app
from framefound.config import get_settings
from framefound.db.models import Asset, Job, Library
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


async def _with_asset(task_name: str, asset_id: uuid.UUID, handler: LoadedHandler) -> None:
    """Common shell: fresh engine, job-history row, load asset+library,
    resolve+validate the source path, run the handler."""
    engine = create_async_engine(get_settings().db_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            job = Job(task_name=task_name, asset_id=asset_id)
            db.add(job)
            await db.commit()
            try:
                asset = await db.get(Asset, asset_id)
                if asset is None:
                    log.warning("processing.asset_gone", asset_id=str(asset_id))
                    job.status = "skipped"
                    job.error = "Asset no longer exists"
                    return
                library = await db.get(Library, asset.library_id)
                if library is None:
                    job.status = "skipped"
                    job.error = "Library no longer exists"
                    return
                try:
                    path = safe_join(Path(library.root_path), asset.relative_path)
                except PathValidationError:
                    asset.processing_status = "path_rejected"
                    job.status = "failed"
                    job.error = "Path escaped the library root"
                    log.error("processing.path_rejected", asset_id=str(asset_id))
                    return
                if not path.is_file():
                    asset.availability = "missing"
                    job.status = "skipped"
                    job.error = "Original file is not reachable"
                    return
                await handler(db, asset, library, path)
                job.status = "succeeded"
            except Exception as exc:
                await db.rollback()
                job.status = "failed"
                job.error = str(exc)[:500]
                raise
            finally:
                job.finished_at = datetime.now(UTC)
                db.add(job)
                await db.commit()
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
    # Re-checked at run time so disabling proxies on a library immediately
    # no-ops any jobs already sitting in the queue.
    if asset.media_type != "video" or not library.generate_proxies:
        return
    await deriv.generate_video_proxy(db, get_settings().data_dir, asset, library, path)


@celery_app.task(
    name="framefound.extract_metadata",
    queue="metadata",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def extract_metadata(asset_id: str) -> None:
    """Extract technical + capture metadata for one asset (idempotent)."""
    try:
        asyncio.run(_with_asset("extract_metadata", uuid.UUID(asset_id), _extract))
    except Exception:
        log.error("metadata.failed", asset_id=asset_id, exc_info=True)
        raise


@celery_app.task(
    name="framefound.generate_derivatives",
    queue="visuals",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def generate_derivatives(asset_id: str) -> None:
    """Thumbnails, previews, posters, waveforms for one asset (idempotent)."""
    try:
        asyncio.run(_with_asset("generate_derivatives", uuid.UUID(asset_id), _visuals))
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
        asyncio.run(_with_asset("generate_proxy", uuid.UUID(asset_id), _proxy))
    except Exception:
        log.error("proxy.failed", asset_id=asset_id, exc_info=True)
        raise
