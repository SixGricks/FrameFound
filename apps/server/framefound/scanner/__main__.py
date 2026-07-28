"""Scanner service: watches libraries, runs queued and scheduled scans.

Loop responsibilities:
1. Claim and execute pending scans (created by the API or by schedules).
2. Create scheduled scans when a library's interval has elapsed.
3. Maintain watchdog observers for watcher-enabled libraries and process
   stability-gated candidates they surface.
"""

import asyncio
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.engine import session_factory
from framefound.db.models import Asset, Library, Scan
from framefound.logging import configure_logging
from framefound.scanner import scan as scan_engine
from framefound.scanner.stability import is_file_stable
from framefound.scanner.watcher import WatchQueue, observe_file, start_observer

log = structlog.get_logger()

POLL_SECONDS = 5.0
WATCH_MIN_AGE_SECONDS = 10.0


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


async def _requeue_stuck_assets(db: AsyncSession, enqueue: scan_engine.Enqueue) -> None:
    """Assets left `pending` for >10 min (queue outage, worker crash) get
    re-enqueued — the metadata task is idempotent, duplicates are harmless."""
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

                if time.time() - last_requeue > 300:
                    await _requeue_stuck_assets(db, enqueue)
                    last_requeue = time.time()
        except Exception:
            log.error("scanner.loop_error", exc_info=True)
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
