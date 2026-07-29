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


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity for the L2-normalised vectors we store."""
    if not a or not b:
        return 0.0
    return float(sum(x * y for x, y in zip(a, b, strict=False)))


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
    # Chain the downstream stages now that duration/dimensions are known.
    generate_derivatives.delay(str(asset.id))
    if asset.media_type == "video" and library.generate_proxies:
        generate_proxy.delay(str(asset.id))
    has_audio = asset.media_type == "audio" or asset.audio_codec is not None
    if has_audio and library.transcribe_enabled:
        transcribe_asset.delay(str(asset.id))
    # Images are sampled too (one frame at ts=0) so stills and motion share
    # one vector table — see docs/data-model.md §frames.
    sample_frames.delay(str(asset.id))


async def _visuals(db: AsyncSession, asset: Asset, library: Library, path: Path) -> None:
    await deriv.generate_visuals(db, get_settings().data_dir, asset, path)


class ProcessingPaused(Exception):
    """Raised when a stage cannot run yet for an environmental reason (disk
    space). Retrying immediately would spin, so the task exits quietly and the
    work is picked up by a later scan or a manual reprocess."""


async def _proxy(db: AsyncSession, asset: Asset, library: Library, path: Path) -> None:
    # Re-checked at run time so disabling proxies on a library immediately
    # no-ops any jobs already sitting in the queue.
    if asset.media_type != "video" or not library.generate_proxies:
        return
    await deriv.generate_video_proxy(db, get_settings().data_dir, asset, library, path)


async def _transcribe(db: AsyncSession, asset: Asset, library: Library, path: Path) -> None:
    from sqlalchemy import delete, select

    from framefound.ai.transcription import get_transcription_provider
    from framefound.db.models import Transcript, TranscriptSegment
    from framefound.media.subtitles import build_vtt, find_sidecar, import_sidecar

    if not library.transcribe_enabled:
        return
    has_audio = asset.media_type == "audio" or asset.audio_codec is not None
    if not has_audio:
        return

    # A hand-authored sidecar beats ASR: import it and skip transcription.
    result = None
    sidecar = await asyncio.to_thread(find_sidecar, path)
    if sidecar is not None:
        result = await asyncio.to_thread(import_sidecar, sidecar)
        if result is not None:
            log.info("transcript.sidecar_imported", asset_id=str(asset.id), file=sidecar.name)
    if result is None:
        provider = get_transcription_provider()
        result = await asyncio.to_thread(provider.transcribe, path)
    if not result.segments:
        # Music beds and silent tracks legitimately yield nothing. Recording an
        # empty transcript would only pollute search results.
        log.info("transcript.no_speech", asset_id=str(asset.id))
        return

    existing = (
        await db.execute(select(Transcript).where(Transcript.asset_id == asset.id))
    ).scalar_one_or_none()
    version = 1
    if existing is not None:
        version = existing.version + 1
        await db.execute(
            delete(TranscriptSegment).where(TranscriptSegment.transcript_id == existing.id)
        )
        await db.delete(existing)
        await db.flush()

    transcript = Transcript(
        asset_id=asset.id,
        language=result.language,
        language_confidence=result.language_probability,
        model_name=result.model_name,
        full_text=" ".join(seg.text for seg in result.segments),
        segment_count=len(result.segments),
        version=version,
    )
    db.add(transcript)
    await db.flush()
    for seg in result.segments:
        db.add(
            TranscriptSegment(
                transcript_id=transcript.id,
                start_ms=int(seg.start_s * 1000),
                end_ms=int(seg.end_s * 1000),
                text=seg.text,
                confidence=seg.confidence,
            )
        )

    # Subtitle sidecar as a derivative (served with the proxy player).
    subtitle = await deriv._upsert(db, asset.id, "subtitle", "vtt")
    vtt_path = deriv._abs(get_settings().data_dir, subtitle)
    vtt_path.write_text(build_vtt(result.segments), encoding="utf-8")
    await deriv._finish(db, subtitle, vtt_path)
    await db.commit()
    log.info(
        "transcript.created",
        asset_id=str(asset.id),
        language=result.language,
        segments=len(result.segments),
    )


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
    except deriv.OutOfSpace as exc:
        log.warning("derivatives.paused_low_disk", asset_id=asset_id, reason=str(exc))
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
    except deriv.OutOfSpace as exc:
        log.warning("proxy.paused_low_disk", asset_id=asset_id, reason=str(exc))
    except Exception:
        log.error("proxy.failed", asset_id=asset_id, exc_info=True)
        raise


async def _sample_frames(db: AsyncSession, asset: Asset, library: Library, path: Path) -> None:
    from sqlalchemy import delete

    from framefound.db.models import Frame
    from framefound.media.phash import dhash
    from framefound.processing import scenes

    data_dir = get_settings().data_dir
    if asset.media_type == "image":
        # A still is a one-frame asset: reuse its preview so search covers
        # photos and motion through the same table.
        thumb_rel = deriv.derivative_relpath(asset.id, "thumbnail", "webp")
        if not (data_dir / thumb_rel).is_file():
            return
        plan = [(0.0, False)]
        sources = {0.0: data_dir / thumb_rel}
    else:
        if asset.media_type != "video":
            return
        duration = asset.duration_s or 0.0
        # Prefer the proxy as the decode source: it is local, 1080p, and
        # H.264, where the original may be 4K on a network share.
        proxy_rel = deriv.derivative_relpath(asset.id, "proxy", "mp4")
        proxy = data_dir / proxy_rel
        has_proxy = proxy.is_file()
        decode_from = proxy if has_proxy else path

        scene_times: list[float] = []
        if scenes.should_scene_detect(duration, has_proxy):
            scene_times = await asyncio.to_thread(scenes.detect_scene_timestamps, decode_from)
        else:
            log.info(
                "frames.interval_only",
                asset_id=str(asset.id),
                reason="no proxy and full decode would be slower than it is worth",
            )
        plan = scenes.plan_samples(duration, scene_times)
        sources = {}
        path = decode_from  # extract frames from the same source we planned on

    await db.execute(delete(Frame).where(Frame.asset_id == asset.id))
    await db.flush()

    scene_counter = 0
    written = 0
    for ts, is_scene in plan:
        ts_ms = int(ts * 1000)
        rel = f"frames/{str(asset.id)[:2]}/{asset.id}/{ts_ms:09d}.jpeg"
        target = data_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if ts in sources:
            rel = str(sources[ts].relative_to(data_dir)).replace("\\", "/")
            target = sources[ts]
        else:
            try:
                await asyncio.to_thread(scenes.extract_frame, path, target, ts)
            except Exception as exc:
                # One unreadable timestamp must not fail the whole asset.
                log.warning("frames.timestamp_skipped", ts=ts, reason=str(exc)[:120])
                continue
        if is_scene:
            scene_counter += 1
        db.add(
            Frame(
                asset_id=asset.id,
                ts_ms=ts_ms,
                scene_number=scene_counter if is_scene else None,
                is_scene_change=is_scene,
                relative_path=rel,
                phash=await asyncio.to_thread(dhash, target),
            )
        )
        written += 1
    await db.commit()
    log.info("frames.sampled", asset_id=str(asset.id), frames=written, scenes=scene_counter)
    if written:
        embed_frames.delay(str(asset.id))


@celery_app.task(
    name="framefound.sample_frames",
    queue="vision",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def sample_frames(asset_id: str) -> None:
    """Scene-detect and sample frames for visual search (idempotent)."""
    try:
        asyncio.run(_with_asset("sample_frames", uuid.UUID(asset_id), _sample_frames))
    except Exception:
        log.error("frames.failed", asset_id=asset_id, exc_info=True)
        raise


async def _embed_frames(db: AsyncSession, asset: Asset, library: Library, path: Path) -> None:
    from sqlalchemy import select

    from framefound.ai.embeddings import EmbeddingUnavailable, get_embedding_provider
    from framefound.db.models import Frame

    provider = get_embedding_provider()
    data_dir = get_settings().data_dir
    frames = (
        (
            await db.execute(
                select(Frame).where(Frame.asset_id == asset.id, Frame.embedding.is_(None))
            )
        )
        .scalars()
        .all()
    )
    embedded = 0
    for frame in frames:
        image = data_dir / frame.relative_path
        if not image.is_file():
            continue
        try:
            result = await asyncio.to_thread(provider.embed_image, image)
        except EmbeddingUnavailable as exc:
            # A single unreadable frame must not fail the asset; a missing
            # runtime should stop the task so it can be retried after a fix.
            if "not installed" in str(exc):
                raise
            continue
        frame.embedding = result.vector
        frame.embedding_model = result.model_name
        embedded += 1
    await db.commit()
    if embedded:
        log.info("embeddings.created", asset_id=str(asset.id), frames=embedded)


@celery_app.task(
    name="framefound.embed_frames",
    queue="vision",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def embed_frames(asset_id: str) -> None:
    """Compute CLIP vectors for an asset's sampled frames (idempotent)."""
    try:
        asyncio.run(_with_asset("embed_frames", uuid.UUID(asset_id), _embed_frames))
    except Exception:
        log.error("embeddings.failed", asset_id=asset_id, exc_info=True)
        raise


@celery_app.task(
    name="framefound.infer_locations",
    queue="vision",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def infer_locations(library_id: str) -> None:
    """Lend GPS positions from located assets to unlocated neighbours.

    Only fills gaps: an asset that already has coordinates — from EXIF or a
    person — is never touched.
    """
    from sqlalchemy import select

    from framefound.db.models import Frame
    from framefound.media.geo import LocationCandidate, best_candidate

    async def run() -> None:
        engine = create_async_engine(get_settings().db_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                lib = uuid.UUID(library_id)
                located = (
                    (
                        await db.execute(
                            select(Asset).where(
                                Asset.library_id == lib,
                                Asset.gps_lat.is_not(None),
                                Asset.captured_at.is_not(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                unlocated = (
                    (
                        await db.execute(
                            select(Asset).where(
                                Asset.library_id == lib,
                                Asset.gps_lat.is_(None),
                                Asset.captured_at.is_not(None),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not located or not unlocated:
                    log.info(
                        "locations.nothing_to_infer",
                        located=len(located),
                        unlocated=len(unlocated),
                    )
                    return

                async def first_vector(asset_id: uuid.UUID) -> list[float] | None:
                    frame = (
                        await db.execute(
                            select(Frame)
                            .where(Frame.asset_id == asset_id, Frame.embedding.is_not(None))
                            .order_by(Frame.ts_ms)
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    return frame.embedding if frame else None

                anchors = [(a, await first_vector(a.id)) for a in located]
                anchors = [(a, v) for a, v in anchors if v]
                filled = 0
                for subject in unlocated:
                    subject_vector = await first_vector(subject.id)
                    if not subject_vector:
                        continue
                    candidates = [
                        LocationCandidate(
                            asset_id=str(anchor.id),
                            lat=anchor.gps_lat or 0.0,
                            lon=anchor.gps_lon or 0.0,
                            captured_at=anchor.captured_at,  # type: ignore[arg-type]
                            similarity=_cosine(subject_vector, vector),
                        )
                        for anchor, vector in anchors
                    ]
                    chosen = best_candidate(subject.captured_at, candidates)  # type: ignore[arg-type]
                    if chosen is None:
                        continue
                    source, confidence = chosen
                    subject.gps_lat = source.lat
                    subject.gps_lon = source.lon
                    subject.gps_source = "inferred"
                    subject.gps_confidence = confidence
                    subject.gps_inferred_from = uuid.UUID(source.asset_id)
                    filled += 1
                await db.commit()
                log.info("locations.inferred", library=library_id, filled=filled)
        finally:
            await engine.dispose()

    asyncio.run(run())


@celery_app.task(
    name="framefound.index_visual_batch",
    queue="vision",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def index_visual_batch(asset_ids: list[str]) -> None:
    """Sample frames and embed them for many assets in one engine.

    Per-task setup (engine, pool, job row) costs ~3 s, which is noise beside a
    proxy transcode but dominates when the real work is a thumbnail lookup and
    a 288 ms embed. Batching amortises it across the group and keeps the
    library-wide index within about an hour instead of a working day.
    """

    async def run() -> None:
        engine = create_async_engine(get_settings().db_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                job = Job(task_name="index_visual_batch")
                db.add(job)
                await db.commit()
                done = 0
                for raw_id in asset_ids:
                    try:
                        asset = await db.get(Asset, uuid.UUID(raw_id))
                        if asset is None:
                            continue
                        library = await db.get(Library, asset.library_id)
                        if library is None:
                            continue
                        path = safe_join(Path(library.root_path), asset.relative_path)
                        if not path.is_file():
                            asset.availability = "missing"
                            continue
                        await _sample_frames(db, asset, library, path)
                        await _embed_frames(db, asset, library, path)
                        done += 1
                    except Exception:
                        # One bad asset must not sink the batch.
                        await db.rollback()
                        log.warning("visual_batch.asset_failed", asset_id=raw_id)
                job.status = "succeeded"
                job.finished_at = datetime.now(UTC)
                db.add(job)
                await db.commit()
                log.info("visual_batch.done", requested=len(asset_ids), indexed=done)
        finally:
            await engine.dispose()

    asyncio.run(run())


@celery_app.task(
    name="framefound.transcribe_asset",
    queue="transcribe",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def transcribe_asset(asset_id: str) -> None:
    """Speech-to-text with timestamped segments + VTT sidecar (idempotent)."""
    try:
        asyncio.run(_with_asset("transcribe_asset", uuid.UUID(asset_id), _transcribe))
    except Exception:
        log.error("transcribe.failed", asset_id=asset_id, exc_info=True)
        raise
