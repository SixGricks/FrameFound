"""Scanner service: watches libraries, runs queued and scheduled scans.

Loop responsibilities:
1. Claim and execute pending scans (created by the API or by schedules).
2. Create scheduled scans when a library's interval has elapsed.
3. Maintain watchdog observers for watcher-enabled libraries and process
   stability-gated candidates they surface.
4. Confirm departures — paths a watchdog event claims are gone — and flag the
   assets behind them `missing` once a stat agrees.
5. Re-queue work that failed and was never looked at again.
"""

import asyncio
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import structlog
from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.engine import session_factory
from framefound.db.models import Asset, Job, Library, Scan
from framefound.logging import configure_logging
from framefound.scanner import scan as scan_engine
from framefound.scanner.stability import is_file_stable
from framefound.scanner.watcher import WatchQueue, observe_file, start_observer

log = structlog.get_logger()

POLL_SECONDS = 5.0
WATCH_MIN_AGE_SECONDS = 10.0
# Departures wait far longer than arrivals. Applications that save by
# replacing a file emit delete-then-create within a second or two, and a
# network mount can blink; a minute of patience costs nothing and avoids
# flapping assets between `online` and `missing`.
DEPART_MIN_AGE_SECONDS = 60.0
# Transcription is the slowest stage by far, so the sweep hands over a
# small batch and lets the next pass carry on rather than filling the
# queue with hours of work at once.
TRANSCRIBE_BATCH = 25
# After this many failures an asset is left alone. A file ffmpeg cannot
# open will never succeed, and retrying it forever starves everything else.
MAX_TRANSCRIBE_ATTEMPTS = 3


def _make_enqueue() -> scan_engine.Enqueue:
    try:
        from framefound.processing.tasks import extract_metadata

        def enqueue(asset_id: uuid.UUID) -> None:
            try:
                extract_metadata.delay(str(asset_id))
            except Exception:
                # Queue down: asset stays `pending`; the requeue pass retries.
                log.warning("scanner.enqueue_failed", asset_id=str(asset_id))

        return enqueue
    except Exception:  # pragma: no cover - celery not installed (dev)
        log.warning("scanner.queue_unavailable")
        return lambda asset_id: None


async def _run_pending_scans(db: AsyncSession, enqueue: scan_engine.Enqueue) -> None:
    pending = (await db.execute(select(Scan).where(Scan.status == "pending"))).scalars().all()
    for queued_scan in pending:
        library = await db.get(Library, queued_scan.library_id)
        if library is None or not library.enabled:
            queued_scan.status = "cancelled"
            continue
        log.info("scan.starting", library=library.name)
        await scan_engine.run_scan(db, queued_scan, library, enqueue)
    await db.commit()


async def _schedule_due_scans(db: AsyncSession) -> None:
    libraries = (await db.execute(select(Library).where(Library.enabled.is_(True)))).scalars().all()
    for library in libraries:
        if library.scan_interval_minutes is None:
            continue
        active = (
            await db.execute(
                select(Scan.id).where(
                    Scan.library_id == library.id,
                    Scan.status.in_(("pending", "running", "paused")),
                )
            )
        ).first()
        if active is not None:
            continue
        last = library.last_scan_at
        due = last is None or datetime.now(UTC) - (
            last if last.tzinfo else last.replace(tzinfo=UTC)
        ) >= timedelta(minutes=library.scan_interval_minutes)
        if due:
            db.add(Scan(library_id=library.id, status="pending"))
    await db.commit()


async def _drain_watch_queue(
    db: AsyncSession, queue: WatchQueue, enqueue: scan_engine.Enqueue
) -> None:
    for candidate in queue.due(WATCH_MIN_AGE_SECONDS):
        library = await db.get(Library, candidate.library_id)
        if library is None or not library.enabled:
            continue
        root = Path(library.root_path)
        abs_path = root / candidate.relative_path
        later = observe_file(abs_path)
        if later is None:
            continue  # vanished; reconciliation will flag it if it was indexed
        if not is_file_stable(candidate.first, later):
            queue.requeue(candidate, later)
            continue
        st = os.stat(abs_path)
        existing = (
            await db.execute(
                select(Asset).where(
                    Asset.library_id == library.id,
                    Asset.relative_path == candidate.relative_path,
                )
            )
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        if existing is None:
            asset, action = await scan_engine._index_new_file(
                db, library, root, candidate.relative_path, st, now
            )
        else:
            asset = await scan_engine._apply_change(db, root, existing, st, now)
            action = "changed" if asset else "deferred"
        await db.commit()
        if asset is not None:
            enqueue(asset.id)
        log.info("watcher.processed", path=candidate.relative_path, action=action)


async def _drain_departures(db: AsyncSession, queue: WatchQueue) -> None:
    """Flag assets whose files are confirmed gone.

    The watchdog event is only the prompt to look; the stat is what decides.
    Assets are flagged `missing`, never deleted — a NAS that unmounts mid-edit
    would otherwise take the catalogue with it, and the metadata is worth
    keeping even when the file is genuinely gone for good.
    """
    for departure in queue.departures_due(DEPART_MIN_AGE_SECONDS):
        library = await db.get(Library, departure.library_id)
        if library is None or not library.enabled:
            continue
        abs_path = Path(library.root_path) / departure.relative_path
        if abs_path.exists():
            continue  # spurious event, or it came back before we looked

        where = [Asset.library_id == library.id, Asset.availability == "online"]
        if departure.is_directory:
            prefix = departure.relative_path.rstrip("/")
            where.append(Asset.relative_path.startswith(f"{prefix}/"))
        else:
            where.append(Asset.relative_path == departure.relative_path)

        result = await db.execute(update(Asset).where(*where).values(availability="missing"))
        await db.commit()
        flagged = cast("CursorResult[Any]", result).rowcount
        if flagged:
            log.info(
                "watcher.departed",
                path=departure.relative_path,
                directory=departure.is_directory,
                assets_flagged=flagged,
            )


async def _queue_busy(name: str) -> bool:
    """True when a queue still has messages — a backlog means workers are
    busy, not that jobs were lost, so requeueing would only create duplicates
    (learned in deployment: 32k duplicate messages)."""
    try:
        import redis.asyncio as aioredis

        from framefound.config import get_settings

        client = aioredis.from_url(  # type: ignore[no-untyped-call]
            get_settings().redis_url, socket_connect_timeout=2
        )
        try:
            return int(await client.llen(name)) > 0
        finally:
            await client.aclose()
    except Exception:
        return True  # broker unknown: assume busy, never duplicate


async def _metadata_queue_busy() -> bool:
    return await _queue_busy("metadata")


async def _requeue_stuck_assets(db: AsyncSession, enqueue: scan_engine.Enqueue) -> None:
    """Assets left `pending` for >10 min with an idle queue (worker crash,
    broker restart) get re-enqueued — the metadata task is idempotent."""
    if await _metadata_queue_busy():
        return
    cutoff = datetime.now(UTC) - timedelta(minutes=10)
    stuck = (
        (
            await db.execute(
                select(Asset.id).where(
                    Asset.processing_status == "pending",
                    Asset.availability == "online",
                    Asset.first_indexed_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for asset_id in stuck:
        enqueue(asset_id)


async def _requeue_missing_transcripts(db: AsyncSession) -> None:
    """Re-queue audio that should have been transcribed and was not.

    Found in deployment: 555 transcription jobs failed on a models-directory
    permission problem, the retries were exhausted inside Celery, and nothing
    ever looked again. The permission issue was fixed weeks of wall-clock
    earlier and the backlog simply sat there — the catalogue reported 12 of 52
    audio assets transcribed and nothing was wrong enough to notice.

    Absence of a transcript is not enough to act on: a music bed legitimately
    produces none, and re-queueing on that basis would loop forever. A
    *succeeded* job is the real signal that an asset has had its turn,
    whatever the outcome. Assets that have failed repeatedly are left alone so
    one broken file cannot occupy the queue.
    """
    if await _queue_busy("transcribe"):
        return

    had_a_turn = select(Job.asset_id).where(
        Job.task_name == "transcribe_asset", Job.status == "succeeded"
    )
    failed_often = (
        select(Job.asset_id)
        .where(Job.task_name == "transcribe_asset", Job.status == "failed")
        .group_by(Job.asset_id)
        .having(func.count() >= MAX_TRANSCRIBE_ATTEMPTS)
    )
    candidates = (
        (
            await db.execute(
                select(Asset.id)
                .join(Library, Library.id == Asset.library_id)
                .where(
                    Library.enabled.is_(True),
                    Library.transcribe_enabled.is_(True),
                    Asset.availability == "online",
                    Asset.processing_status == "ready",
                    or_(Asset.media_type == "audio", Asset.audio_codec.is_not(None)),
                    Asset.id.not_in(had_a_turn),
                    Asset.id.not_in(failed_often),
                )
                .limit(TRANSCRIBE_BATCH)
            )
        )
        .scalars()
        .all()
    )
    if not candidates:
        return

    try:
        from framefound.processing.tasks import transcribe_asset
    except Exception:
        log.warning("scanner.transcribe_unavailable")
        return
    for asset_id in candidates:
        transcribe_asset.delay(str(asset_id))
    log.info("scanner.transcripts_requeued", count=len(candidates))


async def main() -> None:
    configure_logging()
    log.info("scanner.started")
    enqueue = _make_enqueue()
    factory = session_factory()
    queue = WatchQueue()
    observers: dict[uuid.UUID, object] = {}
    last_requeue = 0.0

    while True:
        try:
            async with factory() as db:
                await _run_pending_scans(db, enqueue)
                await _schedule_due_scans(db)

                watch_libs = (
                    (
                        await db.execute(
                            select(Library).where(
                                Library.enabled.is_(True), Library.watcher_enabled.is_(True)
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                for library in watch_libs:
                    if library.id not in observers:
                        started = start_observer(library.id, Path(library.root_path), queue)
                        if started is not None:
                            observers[library.id] = started

                await _drain_watch_queue(db, queue, enqueue)
                await _drain_departures(db, queue)

                if time.time() - last_requeue > 300:
                    await _requeue_stuck_assets(db, enqueue)
                    await _requeue_missing_transcripts(db)
                    last_requeue = time.time()
        except Exception:
            log.error("scanner.loop_error", exc_info=True)
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
