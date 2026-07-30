"""Celery tasks for the processing pipeline.

Queues are segregated by latency class so bulk work never starves
user-visible work (learned in deployment when 8k metadata jobs delayed
thumbnails by hours):

  metadata  - bulk extraction sweeps (high volume, moderate cost)
  visuals   - thumbnails/posters/waveforms (fast, user-visible)
  media     - proxy transcodes (heavy, long-running)
  frames    - sampling stills out of video (IO-bound, ~60-100 s each on a
              5.2 MB/s share) — shares worker-media, which is FFmpeg-shaped
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
