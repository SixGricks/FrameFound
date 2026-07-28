"""Celery tasks for the processing pipeline (M2: metadata extraction).

Tasks are idempotent: re-running extraction on an already-processed asset just
refreshes its fields. Each task run creates and disposes its own engine —
asyncpg pools are event-loop-bound and each `asyncio.run` gets a fresh loop.
TODO(perf): move workers to a persistent-loop model if task volume demands it.
"""

import asyncio
import uuid
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from framefound.celery_app import celery_app
from framefound.config import get_settings
from framefound.db.models import Asset, Library
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


async def _extract(asset_id: uuid.UUID) -> None:
    engine = create_async_engine(get_settings().db_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            asset = await db.get(Asset, asset_id)
            if asset is None:
                log.warning("metadata.asset_gone", asset_id=str(asset_id))
                return
            library = await db.get(Library, asset.library_id)
            if library is None:
                return
            try:
                path = safe_join(Path(library.root_path), asset.relative_path)
            except PathValidationError:
                asset.processing_status = "metadata_failed"
                await db.commit()
                log.error("metadata.path_rejected", asset_id=str(asset_id))
                return
            if not path.is_file():
                asset.availability = "missing"
                await db.commit()
                return

            asset.processing_status = "processing"
            await db.commit()
            fields = await asyncio.to_thread(probe_media, path, asset.media_type)
            for name in _ASSET_FIELDS:
                if name in fields:
                    setattr(asset, name, fields[name])
            asset.processing_status = "ready"
            await db.commit()
            log.info("metadata.extracted", asset_id=str(asset_id), fields=sorted(fields.keys()))
    finally:
        await engine.dispose()


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
        asyncio.run(_extract(uuid.UUID(asset_id)))
    except Exception:
        log.error("metadata.failed", asset_id=asset_id, exc_info=True)
        raise
