"""Celery tasks for the processing pipeline.

Queues are segregated by latency class so bulk work never starves
user-visible work (learned in deployment when 8k metadata jobs delayed
thumbnails by hours):

  metadata  - bulk extraction sweeps (high volume, moderate cost)
  visuals   - thumbnails/posters/waveforms (fast, user-visible)
  media     - proxy transcodes (heavy, long-running)
  frames    - sampling stills out of video. Long assumed IO-bound on a
              "5.2 MB/s share"; measured otherwise — that was a single-stream
              figure, three parallel readers pull 17 MB/s from the same NAS,
              and the stage is decode-bound. Shares worker-media (FFmpeg-shaped)
  vision    - CLIP embeddings (~300 ms each, CPU-bound)
  transcribe- speech to text (minutes each)
  default   - legacy name, still consumed so pre-rename queued jobs drain

The split between frames/vision/transcribe was learned three times over: each
time two latency classes shared a worker, the slow one starved the fast one and
the catalogue silently stopped gaining searchable data.

Stage chain: extract_metadata -> generate_derivatives + generate_proxy.
Every task is idempotent; each execution writes a Job history row (the
processing dashboard's data source). Each task run creates and disposes its
own engine — asyncpg pools are event-loop-bound and each `asyncio.run` gets a
fresh loop. TODO(perf): persistent-loop workers if task volume demands it.
"""

import asyncio
import bisect
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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

# The short name callers hand to `_with_asset`, not the Celery route name.
# Named rather than repeated so the two cannot drift into a comparison that
# silently never matches.
METADATA_TASK = "extract_metadata"


def _as_epoch(when: datetime | None) -> float:
    """Seconds since the epoch, treating a naive timestamp as UTC.

    Capture times arrive from EXIF, which is frequently naive. Sorting and
    bisecting need one comparable scale, and guessing local time would shift
    every window by the offset.
    """
    if when is None:
        return 0.0
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp()


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
                # Move the asset OUT of "processing". Without this a failed
                # extraction leaves it there permanently: the status is set on
                # the way in and only cleared on success, so "processing" comes
                # to mean both "in flight" and "died and nobody noticed". On
                # this deployment that hid 230 assets behind a counter that
                # looked like work in progress for three days, when every one
                # of them had failed deterministically and been retried to
                # exhaustion.
                # `task_name` is the short form the callers pass in
                # ("extract_metadata"), not the Celery route name.
                if task_name == METADATA_TASK:
                    stale = await db.get(Asset, asset_id)
                    if stale is not None:
                        stale.processing_status = "metadata_failed"
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
        pixels = (asset.width or 0) * (asset.height or 0)
        if scenes.should_scene_detect(duration, has_proxy, pixels):
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
        # Face detection reads the sampled frames off local disk, so it can run
        # as soon as they exist — it does not wait on embeddings. Chained here
        # rather than after embedding because the two are independent and
        # serialising them would double the time to a usable People page.
        detect_faces.delay(str(asset.id))


@celery_app.task(
    name="framefound.sample_frames",
    # `frames`, not `vision`. Sampling decodes video off the share — 60-100 s
    # per asset at the measured 5.2 MB/s — while embedding the resulting JPEGs
    # takes ~300 ms. Sharing a queue meant 6,271 sampling jobs starved the
    # embedding work behind them and the catalogue stopped gaining searchable
    # frames entirely. Same latency-class rule, one level further down.
    queue="frames",
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


async def _verify_hash(db: AsyncSession, asset: Asset, library: Library, path: Path) -> None:
    from framefound.scanner.identity import full_hash

    asset.content_hash = await asyncio.to_thread(full_hash, path)
    await db.commit()
    log.info("verify.content_hash", asset_id=str(asset.id))


@celery_app.task(
    name="framefound.verify_content_hash",
    queue="metadata",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 2},
)
def verify_content_hash(asset_id: str) -> None:
    """Full BLAKE3 of the whole file.

    Deliberately on demand rather than during scans: reading every byte of a
    multi-terabyte archive over SMB is not something to do routinely, but it
    is exactly what you want before deleting a suspected duplicate.
    """
    try:
        asyncio.run(_with_asset("verify_content_hash", uuid.UUID(asset_id), _verify_hash))
    except Exception:
        log.error("verify.failed", asset_id=asset_id, exc_info=True)
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

    Shape matters here. The naive form (compare every unlocated asset against
    every located one) is 4k x 5k x 512 float operations on a library this
    size, which does not finish and does not fit in memory. Two things make it
    tractable, both falling out of constraints `geo` already imposes:

    - `confidence_for` scores anything beyond a two-hour gap at zero, so
      anchors are sorted by capture time and only the slice inside that window
      is ever considered. On real footage that is a handful, not thousands.
    - Embeddings are L2-normalised at write time, so cosine similarity is a
      plain dot product — one matrix multiply against the slice.
    """
    import numpy as np
    from sqlalchemy import func, select

    from framefound.db.models import Frame
    from framefound.media.geo import (
        MAX_TIME_GAP_S,
        LocationCandidate,
        best_candidate,
    )

    async def run() -> None:
        engine = create_async_engine(get_settings().db_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                lib = uuid.UUID(library_id)

                # One pass for every asset's earliest embedded frame, rather
                # than a query per asset. Expressed as a min(ts_ms) join rather
                # than DISTINCT ON so it runs on SQLite too and the logic stays
                # testable without a Postgres fixture.
                earliest = (
                    select(Frame.asset_id.label("asset_id"), func.min(Frame.ts_ms).label("ts_ms"))
                    .join(Asset, Asset.id == Frame.asset_id)
                    .where(
                        Asset.library_id == lib,
                        Frame.embedding.is_not(None),
                        Asset.captured_at.is_not(None),
                    )
                    .group_by(Frame.asset_id)
                    .subquery()
                )
                vector_rows = (
                    await db.execute(
                        select(Frame.asset_id, Frame.embedding).join(
                            earliest,
                            (Frame.asset_id == earliest.c.asset_id)
                            & (Frame.ts_ms == earliest.c.ts_ms),
                        )
                    )
                ).all()
                vectors = {row[0]: row[1] for row in vector_rows if row[1]}
                if not vectors:
                    log.info("locations.no_embeddings", library=library_id)
                    return

                # Anchors are EXIF-positioned assets only. Letting an inferred
                # position anchor another inference would chain guess onto
                # guess with no decay in confidence — a second-generation
                # result could score as highly as one standing on real data.
                # Every inference stays exactly one hop from ground truth.
                located = (
                    (
                        await db.execute(
                            select(Asset)
                            .where(
                                Asset.library_id == lib,
                                Asset.gps_lat.is_not(None),
                                Asset.gps_source.is_distinct_from("inferred"),
                                Asset.captured_at.is_not(None),
                                Asset.id.in_(vectors.keys()),
                            )
                            .order_by(Asset.captured_at)
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
                                Asset.id.in_(vectors.keys()),
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

                # Anchors as one float32 matrix, ordered by capture time so a
                # bisect gives the time window directly.
                anchor_matrix = np.asarray([vectors[a.id] for a in located], dtype=np.float32)
                anchor_times = [_as_epoch(a.captured_at) for a in located]

                filled = 0
                for subject in unlocated:
                    when = _as_epoch(subject.captured_at)
                    lo = bisect.bisect_left(anchor_times, when - MAX_TIME_GAP_S)
                    hi = bisect.bisect_right(anchor_times, when + MAX_TIME_GAP_S)
                    if lo >= hi:
                        continue  # nothing shot near this in time

                    window = anchor_matrix[lo:hi]
                    subject_vector = np.asarray(vectors[subject.id], dtype=np.float32)
                    # Vectors are unit length, so the dot product is cosine.
                    similarities = window @ subject_vector

                    chosen = best_candidate(
                        subject.captured_at,  # type: ignore[arg-type]
                        [
                            LocationCandidate(
                                asset_id=str(located[lo + i].id),
                                lat=located[lo + i].gps_lat or 0.0,
                                lon=located[lo + i].gps_lon or 0.0,
                                captured_at=located[lo + i].captured_at,  # type: ignore[arg-type]
                                similarity=float(similarity),
                            )
                            for i, similarity in enumerate(similarities)
                        ],
                    )
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
                log.info(
                    "locations.inferred",
                    library=library_id,
                    anchors=len(located),
                    considered=len(unlocated),
                    filled=filled,
                )
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


@celery_app.task(
    name="framefound.detect_faces",
    queue="vision",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def detect_faces(asset_id: str) -> None:
    """Find faces in an asset's sampled frames and match them to known people.

    Runs on `vision` beside embedding: both are short CPU passes over frames
    that are already on local disk, so neither starves the other.

    Matching is against *confirmed* people only, and a face the operator has
    already rejected for a person is never offered again for that person —
    the threshold is a heuristic, the rejection record is the guarantee.
    """
    import numpy as np
    from sqlalchemy import select

    from framefound.ai.faces import FaceModelUnavailable, get_face_provider
    from framefound.db.models import Face, Frame, Person
    from framefound.media.maps_store import load_face_config

    async def run() -> None:
        engine = create_async_engine(get_settings().db_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                if not (await load_face_config(db)).active:
                    return  # switched off; do nothing and leave no trace

                aid = uuid.UUID(asset_id)
                frames = (
                    (
                        await db.execute(
                            select(Frame)
                            .where(Frame.asset_id == aid)
                            .order_by(Frame.ts_ms)
                            .limit(12)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not frames:
                    return

                # Already done? Detection is idempotent but re-running it would
                # duplicate every face row.
                seen = (
                    await db.execute(select(Face.id).where(Face.asset_id == aid).limit(1))
                ).first()
                if seen is not None:
                    return

                provider = get_face_provider()
                data_dir = get_settings().data_dir
                found = 0
                for frame in frames:
                    image = data_dir / frame.relative_path
                    if not image.is_file():
                        continue
                    try:
                        detected = await asyncio.to_thread(provider.detect, image)
                    except FaceModelUnavailable:
                        log.warning("faces.model_unavailable")
                        return
                    # `hit` not `face`: the matching loop below binds `face`
                    # to a Face row, and sharing the name confuses the reader
                    # as much as it confused the type checker.
                    for hit in detected:
                        db.add(
                            Face(
                                frame_id=frame.id,
                                asset_id=aid,
                                box_x=hit.x,
                                box_y=hit.y,
                                box_w=hit.w,
                                box_h=hit.h,
                                detection_score=hit.score,
                                embedding=hit.embedding,
                                source="detected",
                            )
                        )
                        found += 1
                await db.commit()

                if not found:
                    log.info("faces.none_found", asset_id=asset_id)
                    return

                # Match the new faces against named people.
                named = (
                    (
                        await db.execute(
                            select(Person).where(Person.prototype.is_not(None), Person.name != "")
                        )
                    )
                    .scalars()
                    .all()
                )
                new_faces = (
                    (
                        await db.execute(
                            select(Face).where(Face.asset_id == aid, Face.person_id.is_(None))
                        )
                    )
                    .scalars()
                    .all()
                )
                rejected = {
                    (str(f.person_id), _round_key(f.embedding))
                    for f in (await db.execute(select(Face).where(Face.source == "rejected")))
                    .scalars()
                    .all()
                    if f.person_id and f.embedding
                }

                matched = 0
                for face in new_faces:
                    if not face.embedding:
                        continue
                    vector = np.asarray(face.embedding, dtype="float32")
                    best, best_score = None, 0.0
                    for person in named:
                        if (str(person.id), _round_key(face.embedding)) in rejected:
                            continue  # already told no for this person
                        score = float(vector @ np.asarray(person.prototype, dtype="float32"))
                        if score >= person.threshold and score > best_score:
                            best, best_score = person, score
                    if best is not None:
                        face.person_id = best.id
                        face.similarity = round(best_score, 4)
                        matched += 1
                await db.commit()
                log.info(
                    "faces.detected", asset_id=asset_id, faces=found, matched_to_people=matched
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


def _round_key(embedding: list[float] | None) -> str:
    """A stable key for a face vector, so a rejection can be looked up.

    Rounded because the same face re-embedded is not bit-identical, and an
    exact key would let a rejected face slip back in on a re-run.
    """
    if not embedding:
        return ""
    return ",".join(f"{v:.3f}" for v in embedding[:16])


@celery_app.task(
    name="framefound.fetch_basemap",
    queue="media",
    # No autoretry: extraction reads a lot from a remote archive and retrying
    # automatically would burn bandwidth on a loop. Failures are reported and
    # re-requested by hand.
    max_retries=0,
)
def fetch_basemap(name: str, bbox: str) -> None:
    """Pull one region out of the Protomaps planet archive.

    Extraction rather than download: the planet is 125 GB and this host has
    tens of gigabytes free. PMTiles is addressable by HTTP range request, so
    `pmtiles extract` fetches only the tiles inside the bounding box — which is
    the reason the format was chosen over an mbtiles + tileserver stack.

    Written to a `.part` file and renamed on success, so an interrupted run
    never leaves something that looks like a usable basemap.
    """
    import shutil
    import subprocess

    from framefound.api.v1.basemaps import PLANET_URL

    settings = get_settings()
    directory = settings.data_dir / "basemaps"
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{name}.pmtiles"
    partial = directory / f"{name}.pmtiles.part"

    if final.exists():
        return
    if shutil.which("pmtiles") is None:
        log.error("basemap.tool_missing", name=name)
        raise RuntimeError("The pmtiles tool is not installed in this image")

    partial.unlink(missing_ok=True)
    log.info("basemap.extract_started", name=name, bbox=bbox)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, argv form, no shell
            [  # noqa: S607 - resolved from PATH in our own image
                "pmtiles",
                "extract",
                PLANET_URL,
                str(partial),
                f"--bbox={bbox}",
                # Zoom 14 is the point where a basemap stops helping and starts
                # costing: street names are readable and the archive is a
                # fraction of what full zoom would be.
                "--maxzoom=14",
            ],
            capture_output=True,
            timeout=4 * 3600,
            check=False,
        )
        if completed.returncode != 0:
            tail = completed.stderr.decode("utf-8", "replace")[-400:]
            raise RuntimeError(tail or "pmtiles extract failed")
        if not partial.is_file() or partial.stat().st_size < 1024:
            raise RuntimeError("extraction produced no usable archive")
        partial.rename(final)
        log.info("basemap.extract_finished", name=name, bytes=final.stat().st_size)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        log.error("basemap.extract_failed", name=name, error=str(exc)[:300])
        raise


@celery_app.task(
    name="framefound.cluster_faces",
    queue="vision",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 1},
)
def cluster_unassigned_faces(limit: int = 2000) -> None:
    """Group faces that belong to nobody yet into people.

    Detection assigns a face to an existing *named* person when it matches.
    Everything else lands unassigned, and without this it would stay that way —
    the People page would be permanently empty no matter how many faces were
    found. (`cluster_faces` existed and nothing called it, which is the same
    shape of bug as detection never being wired.)

    New clusters are created unnamed. That is the whole point: the operator
    supplies names, the system only proposes groupings.
    """
    from sqlalchemy import func, select

    from framefound.ai import people as people_lib
    from framefound.db.models import Face, Person
    from framefound.media.maps_store import load_face_config

    async def run() -> None:
        engine = create_async_engine(get_settings().db_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                if not (await load_face_config(db)).active:
                    return

                loose = (
                    (
                        await db.execute(
                            select(Face)
                            .where(Face.person_id.is_(None), Face.embedding.is_not(None))
                            .limit(limit)
                        )
                    )
                    .scalars()
                    .all()
                )
                if len(loose) < people_lib.MIN_CLUSTER_SIZE:
                    return

                vectors = [
                    people_lib.FaceVector(
                        face_id=str(f.id), embedding=f.embedding or [], asset_id=str(f.asset_id)
                    )
                    for f in loose
                ]
                by_id = {str(f.id): f for f in loose}
                clusters = people_lib.cluster_faces(vectors)

                created = 0
                for cluster in clusters:
                    # A single stray detection is usually a false positive; it
                    # stays unassigned rather than becoming a "person" of one.
                    if len(cluster.members) < people_lib.MIN_CLUSTER_SIZE:
                        continue
                    person = Person(
                        name="",
                        slug="",
                        prototype=people_lib.prototype_for([m.embedding for m in cluster.members]),
                        threshold=people_lib.DEFAULT_THRESHOLD,
                        face_count=0,
                    )
                    db.add(person)
                    await db.flush()
                    for member in cluster.members:
                        face = by_id.get(member.face_id)
                        if face is not None:
                            face.person_id = person.id
                    person.cover_face_id = uuid.UUID(cluster.members[0].face_id)
                    created += 1
                await db.commit()

                total = (await db.execute(select(func.count()).select_from(Person))).scalar_one()
                log.info(
                    "faces.clustered",
                    considered=len(loose),
                    groups_created=created,
                    people_total=total,
                )
        finally:
            await engine.dispose()

    asyncio.run(run())


@celery_app.task(
    name="framefound.render_slideshow",
    queue="media",
    # No autoretry: a render is minutes of CPU, and the failures worth having
    # are deterministic (a missing preview, pacing that cannot fit its
    # transitions). Retrying automatically would burn the media queue on a loop.
    max_retries=0,
)
def render_slideshow(slideshow_id: str) -> None:
    """Render one stored slideshow to an MP4 in the data directory.

    Driven piece by piece rather than in one call so `segments_done` advances
    while it runs: a forty-photograph render is minutes of work, and "is it
    doing anything?" has to be answerable without reading the logs.
    """
    import shutil

    from framefound.db.models import Slideshow
    from framefound.media import pipeline
    from framefound.processing.derivatives import ensure_space

    async def run() -> None:
        settings = get_settings()
        engine = create_async_engine(settings.db_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                show = await db.get(Slideshow, uuid.UUID(slideshow_id))
                if show is None:
                    log.warning("slideshow.gone", slideshow_id=slideshow_id)
                    return

                show.status = "rendering"
                show.error = None
                show.segments_done = 0
                await db.commit()

                workdir = settings.data_dir / "renders" / "work" / str(show.id)
                try:
                    ensure_space(settings.data_dir)
                    spec = await _slideshow_spec(db, show)
                    pipeline.check_sources(spec)
                    await asyncio.to_thread(pipeline.choose_encoder, spec)

                    workdir.mkdir(parents=True, exist_ok=True)
                    pieces = pipeline.plan_pieces(spec, workdir)
                    for piece in pieces:
                        await asyncio.to_thread(pipeline.run_piece, piece)
                        if piece.kind == "body":
                            show.segments_done = piece.index + 1
                            await db.commit()

                    output = settings.data_dir / "renders" / f"{show.id}.mp4"
                    result = await asyncio.to_thread(pipeline.stitch, spec, pieces, workdir, output)
                except Exception as exc:
                    await db.rollback()
                    show.status = "failed"
                    show.error = str(exc)[:500]
                    await db.commit()
                    # The working directory is deliberately left in place: after
                    # a failure the pieces that did render are the evidence for
                    # which one broke and how.
                    log.error("slideshow.failed", slideshow_id=slideshow_id, error=str(exc)[:300])
                    raise
                else:
                    show.status = "ready"
                    show.relative_path = f"renders/{show.id}.mp4"
                    show.duration_seconds = result.seconds
                    show.size_bytes = result.size_bytes
                    show.error = None
                    await db.commit()
                    shutil.rmtree(workdir, ignore_errors=True)
                    log.info(
                        "slideshow.rendered",
                        slideshow_id=slideshow_id,
                        slides=len(spec.slides),
                        seconds=result.seconds,
                        megabytes=round(result.size_bytes / 1024**2, 1),
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


async def _slideshow_spec(db: AsyncSession, show: Any) -> Any:
    """Build the render parameters from the stored selection and theme.

    Previews are resolved in the order the selection was saved. A photograph
    without one is left out of the list rather than substituted, so
    `check_sources` can report how many are missing instead of silently
    rendering a shorter slideshow than was asked for.
    """
    from sqlalchemy import select

    from framefound.db.models import Derivative
    from framefound.media.render import RenderSpec, Slide, alternate_directions
    from framefound.media.theming import get_theme
    from framefound.scanner.paths import PathValidationError, safe_join

    settings = get_settings()
    theme = get_theme(show.theme)
    options = dict(show.settings or {})

    asset_ids = [uuid.UUID(a) for a in show.asset_ids]
    rows = (
        await db.execute(
            select(Derivative.asset_id, Derivative.relative_path).where(
                Derivative.asset_id.in_(asset_ids),
                Derivative.kind == "preview",
                Derivative.status == "ready",
            )
        )
    ).all()
    by_asset = {row[0]: row[1] for row in rows}
    # Order follows the stored selection, not the database's, and a photograph
    # without a preview keeps its *place* as an empty path rather than being
    # dropped or shuffled to the end. `check_sources` then refuses the whole
    # render and says how many are missing, which is the honest outcome: the
    # operator asked for these photographs in this order.
    paths = [str(settings.data_dir / by_asset[a]) if a in by_asset else "" for a in asset_ids]

    hold = float(options.get("hold_seconds", theme.hold_seconds))
    transition = float(options.get("transition_seconds", theme.transition_seconds))
    directions = alternate_directions(len(paths))

    audio_path = ""
    audio_rel = str(options.get("audio_relpath", "") or "")
    if audio_rel:
        try:
            # The operator supplies their own licensed tracks; the path is
            # theirs, so it is validated against the data directory rather than
            # trusted.
            audio_path = str(safe_join(settings.data_dir, audio_rel))
        except PathValidationError:
            log.warning("slideshow.audio_rejected", slideshow_id=str(show.id), path=audio_rel)

    return RenderSpec(
        width=int(options.get("width", 1920)),
        height=int(options.get("height", 1080)),
        fps=int(options.get("fps", 30)),
        transition_seconds=transition,
        saturation=theme.saturation,
        contrast=theme.contrast,
        brightness=theme.brightness,
        audio_path=audio_path,
        slides=[
            Slide(path=path, seconds=hold, direction=directions[i]) for i, path in enumerate(paths)
        ],
    )


@celery_app.task(
    name="framefound.export_listing_zip",
    queue="media",
    # Deterministic work on local files: a failure will fail the same way
    # again, so retrying automatically would only burn the media queue.
    max_retries=0,
)
def export_listing_zip(listing_id: str, max_edge: int = 3840, quality: int = 85) -> None:
    """Write a listing's images as a numbered, room-named zip.

    The filenames are the product: MLS galleries display in upload order, so
    `01_front_exterior.jpg` sorting first *is* the feature. Numbering is
    contiguous over the images that actually export — a gallery with a hole
    in its sequence reads as a mistake, so an unreadable file is skipped,
    named in `export_error`, and the rest close ranks.
    """
    import io
    import zipfile

    from PIL import Image, ImageCms, ImageOps

    from framefound.db.models import AssetEdit, AssetInpaint, Listing, ListingItem
    from framefound.media import develop as develop_lib

    def _load_sky(name: str) -> Any:
        from PIL import Image

        sky_path = get_settings().data_dir / "skies" / name
        if "/" in name or "\\" in name or ".." in name or not sky_path.is_file():
            log.warning("listing.export_sky_missing", name=name)
            return None
        return Image.open(sky_path)

    def _mask_for(image: Any) -> Any:
        from framefound.ai.embeddings import EmbeddingUnavailable

        try:
            from framefound.ai import skyseg

            return skyseg.sky_mask(image)
        except EmbeddingUnavailable:
            # Degrade to colour-only rather than fail the export.
            log.warning("listing.export_no_segmentation")
            return None

    def to_jpeg(path: Path, recipe: dict[str, Any] | None, is_inpaint: bool = False) -> bytes:
        with Image.open(path) as img:
            if is_inpaint:
                # An inpaint result was orientation-applied and flattened to
                # sRGB when the chain started; doing either again would be
                # wrong, not merely wasteful.
                image = img.convert("RGB")
            else:
                # Camera orientation lives in EXIF; a sideways kitchen is not
                # a feature. Then flatten any embedded profile to sRGB — MLS
                # portals assume it, and a ProPhoto JPEG goes dull the moment
                # they do.
                image = ImageOps.exif_transpose(img) or img
                icc = image.info.get("icc_profile")
                if icc:
                    with contextlib.suppress(Exception):
                        converted = ImageCms.profileToProfile(
                            image,
                            ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                            ImageCms.createProfile("sRGB"),
                            outputMode="RGB",
                        )
                        if converted is not None:
                            image = converted
                image = image.convert("RGB")
            width, height = image.size
            longest = max(width, height)
            if longest > max_edge:
                scale = max_edge / longest
                image = image.resize(
                    (round(width * scale), round(height * scale)), Image.Resampling.LANCZOS
                )
            # The develop recipe, applied after the resize because every
            # adjustment is per-pixel and scale-free — same maths, quarter
            # the pixels. This is the moment "what you saw in the editor"
            # becomes "what the zip contains". Sky replacement rides along:
            # same segmentation, same compositor, same feathering as the
            # preview the operator approved.
            if recipe:
                image = develop_lib.render(image, recipe, load_sky=_load_sky, mask_for=_mask_for)
            out = io.BytesIO()
            image.save(out, "JPEG", quality=quality, optimize=True)
            return out.getvalue()

    async def run() -> None:
        from sqlalchemy import select

        from framefound.ai.rooms import ROOM_LABELS

        settings = get_settings()
        engine = create_async_engine(settings.db_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                listing = await db.get(Listing, uuid.UUID(listing_id))
                if listing is None:
                    log.warning("listing.gone", listing_id=listing_id)
                    return
                listing.export_status = "exporting"
                await db.commit()

                rows = (
                    await db.execute(
                        select(ListingItem, Asset, Library)
                        .join(Asset, Asset.id == ListingItem.asset_id)
                        .join(Library, Library.id == Asset.library_id)
                        .where(
                            ListingItem.listing_id == listing.id,
                            Asset.media_type == "image",
                        )
                        .order_by(ListingItem.position, ListingItem.created_at)
                    )
                ).all()

                # Latest develop recipe per asset, one query for the set.
                edit_rows = (
                    await db.execute(
                        select(AssetEdit)
                        .where(AssetEdit.asset_id.in_([a.id for _, a, _l in rows]))
                        .order_by(AssetEdit.asset_id, AssetEdit.version)
                    )
                ).scalars()
                recipes: dict[uuid.UUID, dict[str, Any]] = {}
                for edit in edit_rows:  # ascending versions: last write wins
                    recipes[edit.asset_id] = develop_lib.clean_recipe(edit.recipe)

                # Object-removal results replace the original as the source.
                inpaint_rows = (
                    await db.execute(
                        select(AssetInpaint)
                        .where(
                            AssetInpaint.asset_id.in_([a.id for _, a, _l in rows]),
                            AssetInpaint.status == "ready",
                        )
                        .order_by(AssetInpaint.asset_id, AssetInpaint.version)
                    )
                ).scalars()
                inpaint_paths: dict[uuid.UUID, str] = {}
                for row_i in inpaint_rows:  # ascending: newest version wins
                    if row_i.relative_path:
                        inpaint_paths[row_i.asset_id] = row_i.relative_path

                out_dir = settings.data_dir / "exports" / "listings"
                out_dir.mkdir(parents=True, exist_ok=True)
                zip_path = out_dir / f"{listing.id}.zip"
                skipped: list[str] = []
                written = 0
                try:
                    # JPEGs do not compress again; ZIP_STORED skips the wasted CPU.
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as archive:
                        for item, asset, library in rows:
                            try:
                                inpainted = inpaint_paths.get(asset.id)
                                if inpainted and (settings.data_dir / inpainted).is_file():
                                    path = settings.data_dir / inpainted
                                    from_inpaint = True
                                else:
                                    path = safe_join(Path(library.root_path), asset.relative_path)
                                    from_inpaint = False
                                data = await asyncio.to_thread(
                                    to_jpeg, path, recipes.get(asset.id), from_inpaint
                                )
                            except Exception:
                                skipped.append(asset.filename)
                                log.warning(
                                    "listing.export_skip",
                                    listing_id=listing_id,
                                    filename=asset.filename,
                                )
                                continue
                            written += 1
                            slug = item.room if item.room in ROOM_LABELS else "photo"
                            archive.writestr(f"{written:02d}_{slug}.jpg", data)
                    if not written:
                        raise RuntimeError("No image in this listing could be read")
                except Exception as exc:
                    await db.rollback()
                    zip_path.unlink(missing_ok=True)
                    listing.export_status = "failed"
                    listing.export_error = str(exc)[:500]
                    await db.commit()
                    log.error("listing.export_failed", listing_id=listing_id, error=str(exc)[:300])
                    raise
                else:
                    listing.export_status = "ready"
                    listing.export_relpath = f"exports/listings/{listing.id}.zip"
                    listing.exported_at = datetime.now(UTC)
                    listing.export_error = (
                        (
                            f"{len(skipped)} could not be read and were left out: "
                            + ", ".join(skipped[:5])
                        )[:500]
                        if skipped
                        else None
                    )
                    await db.commit()
                    log.info(
                        "listing.exported",
                        listing_id=listing_id,
                        images=written,
                        skipped=len(skipped),
                        megabytes=round(zip_path.stat().st_size / 1024**2, 1),
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


@celery_app.task(
    name="framefound.inpaint_asset",
    queue="media",
    # Deterministic per input; retrying an 18-second model run on the same
    # pixels produces the same result or the same failure.
    max_retries=0,
)
def inpaint_asset(inpaint_id: str) -> None:
    """Run one queued object removal.

    The base is the previous inpaint result when there is one, otherwise the
    original — orientation-applied and flattened to sRGB exactly once, on
    first entry into the chain, so every later round and every render reads
    pixels that already agree about which way is up.
    """
    import base64
    import io as io_module

    from PIL import Image, ImageCms, ImageOps

    from framefound.ai.inpaint import remove_region
    from framefound.db.models import AssetInpaint

    async def run() -> None:
        import numpy as np

        settings = get_settings()
        engine = create_async_engine(settings.db_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                from sqlalchemy import select

                row = await db.get(AssetInpaint, uuid.UUID(inpaint_id))
                if row is None:
                    log.warning("inpaint.gone", inpaint_id=inpaint_id)
                    return
                row.status = "running"
                await db.commit()

                try:
                    asset = await db.get(Asset, row.asset_id)
                    if asset is None:
                        raise RuntimeError("Asset no longer exists")

                    previous = (
                        await db.execute(
                            select(AssetInpaint)
                            .where(
                                AssetInpaint.asset_id == row.asset_id,
                                AssetInpaint.status == "ready",
                                AssetInpaint.version < row.version,
                            )
                            .order_by(AssetInpaint.version.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()

                    # Resolve the base path on the loop, decode off it.
                    if previous is not None and previous.relative_path:
                        base_path = settings.data_dir / previous.relative_path
                        first_round = False
                    else:
                        library = await db.get(Library, asset.library_id)
                        if library is None:
                            raise RuntimeError("Library no longer exists")
                        base_path = safe_join(Path(library.root_path), asset.relative_path)
                        first_round = True

                    def work() -> str:
                        with Image.open(base_path) as img:
                            if first_round:
                                image = ImageOps.exif_transpose(img) or img
                                icc = image.info.get("icc_profile")
                                if icc:
                                    with contextlib.suppress(Exception):
                                        converted = ImageCms.profileToProfile(
                                            image,
                                            ImageCms.ImageCmsProfile(io_module.BytesIO(icc)),
                                            ImageCms.createProfile("sRGB"),
                                            outputMode="RGB",
                                        )
                                        if converted is not None:
                                            image = converted
                                image = image.convert("RGB")
                            else:
                                image = img.convert("RGB")

                            png = base64.b64decode(row.mask_meta["png_base64"])
                            with Image.open(io_module.BytesIO(png)) as m:
                                mask_img = m.convert("L").resize(
                                    image.size, Image.Resampling.BILINEAR
                                )
                            mask = np.asarray(mask_img, dtype=np.float32) / 255.0

                            result = remove_region(image, mask)
                            out_dir = settings.data_dir / "inpaint" / str(asset.id)
                            out_dir.mkdir(parents=True, exist_ok=True)
                            relpath = f"inpaint/{asset.id}/v{row.version}.jpg"
                            result.save(settings.data_dir / relpath, "JPEG", quality=95)
                            return relpath

                    row.relative_path = await asyncio.to_thread(work)
                except Exception as exc:
                    await db.rollback()
                    row.status = "failed"
                    row.error = str(exc)[:500]
                    await db.commit()
                    log.error("inpaint.failed", inpaint_id=inpaint_id, error=str(exc)[:300])
                    raise
                else:
                    row.status = "ready"
                    row.error = None
                    await db.commit()
                    log.info(
                        "inpaint.done",
                        asset_id=str(row.asset_id),
                        version=row.version,
                    )
        finally:
            await engine.dispose()

    asyncio.run(run())


@celery_app.task(
    name="framefound.ai_edit_listing",
    # media rather than metadata: sky compositing needs the segmentation
    # model, and worker-media mounts the models cache.
    queue="media",
    # Network-bound and idempotent-ish (a rerun writes new recipe versions,
    # which is the operator pressing the button again). No auto-retry: a
    # failing API fails the same way, and the per-photo loop already
    # continues past individual failures.
    max_retries=0,
)
def ai_edit_listing(listing_id: str, sky_name: str | None = None, mode: str = "ai") -> None:
    """Auto-edit a listing's photographs.

    mode "ai": the recipe-picker judges each photograph. mode "preset": the
    tuned listing preset, no network at all. Either way, when the operator
    chose a sky it is composited wherever segmentation finds enough sky —
    interiors pass through untouched, which is what makes one choice safe
    across a whole shoot.

    Sequential by design: the point is per-photo judgment, not throughput,
    and one preview in flight at a time keeps the operator's API bill and
    rate limits boring. Failures on individual photos are logged and
    skipped - 40 edited and 2 skipped beats 0 edited and an exception.
    """
    from PIL import Image, ImageOps

    from framefound.ai import recipe_picker
    from framefound.db.models import AssetEdit, Listing, ListingItem
    from framefound.media import develop as develop_lib
    from framefound.media.maps_store import load_ai_edit_config

    def _sky_fraction_for(source: Path) -> float:
        """How much sky a photograph has, or 0.0 when segmentation is not
        installed — the sky is then simply not added, and the colour edit
        still lands."""
        try:
            from framefound.ai import skyseg

            with Image.open(source) as img:
                image = ImageOps.exif_transpose(img) or img
                small = image.convert("RGB")
                small.thumbnail((768, 768), Image.Resampling.BILINEAR)
                return skyseg.sky_fraction(small)
        except Exception:
            return 0.0

    async def run() -> None:
        from sqlalchemy import func, select

        settings = get_settings()
        engine = create_async_engine(settings.db_url)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                listing = await db.get(Listing, uuid.UUID(listing_id))
                if listing is None:
                    log.warning("ai_edit.listing_gone", listing_id=listing_id)
                    return
                config = await load_ai_edit_config(db)
                use_ai = mode == "ai" and config.ready
                api_key = config.api_key() if use_ai else ""

                rows = (
                    await db.execute(
                        select(ListingItem, Asset, Library)
                        .join(Asset, Asset.id == ListingItem.asset_id)
                        .join(Library, Library.id == Asset.library_id)
                        .where(
                            ListingItem.listing_id == listing.id,
                            Asset.media_type == "image",
                        )
                        .order_by(ListingItem.position)
                    )
                ).all()

                edited = skipped = 0
                for _item, asset, library in rows:
                    try:
                        path = safe_join(Path(library.root_path), asset.relative_path)

                        def build_preview(source: Path = path) -> bytes:
                            with Image.open(source) as img:
                                image = ImageOps.exif_transpose(img) or img
                                return recipe_picker.preview_bytes(image)

                        if use_ai:
                            preview = await asyncio.to_thread(build_preview)
                            picked = await asyncio.to_thread(
                                recipe_picker.pick_recipe, preview, api_key, config.model
                            )
                            recipe = dict(picked["recipe"])
                        else:
                            recipe = dict(develop_lib.LISTING_PRESET)

                        if sky_name:
                            fraction = await asyncio.to_thread(_sky_fraction_for, path)
                            if fraction >= 0.04:
                                recipe["sky"] = {"name": sky_name}
                        version = (
                            await db.execute(
                                select(func.coalesce(func.max(AssetEdit.version), 0)).where(
                                    AssetEdit.asset_id == asset.id
                                )
                            )
                        ).scalar_one() + 1
                        db.add(
                            AssetEdit(
                                asset_id=asset.id,
                                version=version,
                                recipe=develop_lib.clean_recipe(recipe),
                            )
                        )
                        await db.commit()
                        edited += 1
                    except Exception as exc:
                        await db.rollback()
                        skipped += 1
                        log.warning(
                            "ai_edit.photo_skipped",
                            filename=asset.filename,
                            error=str(exc)[:200],
                        )
                log.info(
                    "ai_edit.finished",
                    listing_id=listing_id,
                    edited=edited,
                    skipped=skipped,
                )
        finally:
            await engine.dispose()

    asyncio.run(run())
