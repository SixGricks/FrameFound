"""Recursive scan and reconciliation engine.

Design constraints (docs/architecture.md, brief §5):
- Bounded memory: the filesystem walk is a generator consumed in batches; the
  "which assets went missing" question is answered by `last_verified_at`
  watermarks, never by holding every path in memory.
- Restart-safe: a scan can always simply run again; every step is an upsert.
- Pause/resume/cancel: the scan row's status is re-read between batches, so
  API-driven control takes effect mid-scan.
- Originals are only ever read. Missing files are *flagged*, never deleted;
  an unreachable mount flags the whole library `unmounted` and aborts.
"""

import fnmatch
import os
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path

import anyio
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import Asset, Library, Scan
from framefound.media.detect import (
    SUPPORTED_EXTENSIONS,
    extension_of,
    guess_mime,
    media_type_for,
)
from framefound.scanner.identity import partial_hash
from framefound.scanner.paths import safe_join
from framefound.scanner.stability import looks_at_rest

log = structlog.get_logger()

Enqueue = Callable[[uuid.UUID], None]

BATCH_SIZE = 500
PAUSE_POLL_SECONDS = 2.0


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _excluded(rel_posix: str, globs: list[str]) -> bool:
    parts = rel_posix.split("/")
    return any(
        fnmatch.fnmatch(rel_posix, g) or any(fnmatch.fnmatch(p, g) for p in parts) for g in globs
    )


def _walk(
    root: Path, include_exts: set[str], exclude_globs: list[str]
) -> Iterator[tuple[str, os.stat_result]]:
    """Yield (relative_posix_path, stat) for candidate files, depth-first,
    skipping hidden entries, excluded globs, and symlinked directories
    (symlinks could escape the validated root)."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    rel = os.path.relpath(entry.path, root).replace(os.sep, "/")
                    if _excluded(rel, exclude_globs):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif (
                        entry.is_file(follow_symlinks=False)
                        and extension_of(entry.name) in include_exts
                    ):
                        try:
                            yield rel, entry.stat()
                        except OSError:
                            continue
        except OSError:
            log.warning("scan.dir_unreadable", path=str(current))
            continue


async def mark_library_unmounted(db: AsyncSession, library_id: uuid.UUID) -> int:
    result = await db.execute(
        update(Asset)
        .where(Asset.library_id == library_id, Asset.availability == "online")
        .values(availability="unmounted")
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def _index_new_file(
    db: AsyncSession,
    library: Library,
    root: Path,
    rel: str,
    st: os.stat_result,
    scan_time: datetime,
) -> tuple[Asset | None, str]:
    """Insert a new asset — or re-bind a moved one (same size + partial hash,
    old path gone). Returns (asset needing processing | None, action)."""
    abs_path = safe_join(root, rel)
    try:
        phash = await anyio.to_thread.run_sync(partial_hash, abs_path)
    except OSError:
        return None, "deferred"

    candidates = (
        await db.execute(
            select(Asset).where(
                Asset.library_id == library.id,
                Asset.size_bytes == st.st_size,
                Asset.partial_hash == phash,
            )
        )
    ).scalars()
    for candidate in candidates:
        old_abs = root / candidate.relative_path
        if not old_abs.exists():
            candidate.relative_path = rel
            candidate.filename = Path(rel).name
            candidate.mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC)
            candidate.availability = "online"
            candidate.last_verified_at = scan_time
            return None, "moved"

    media_type = media_type_for(rel)
    if media_type is None:  # pragma: no cover - filtered by the walk already
        return None, "deferred"
    asset = Asset(
        library_id=library.id,
        relative_path=rel,
        filename=Path(rel).name,
        extension=extension_of(rel),
        mime_type=guess_mime(rel),
        media_type=media_type,
        size_bytes=st.st_size,
        mtime=datetime.fromtimestamp(st.st_mtime, tz=UTC),
        partial_hash=phash,
        availability="online",
        processing_status="pending",
        last_verified_at=scan_time,
    )
    db.add(asset)
    await db.flush()
    return asset, "new"


async def _apply_change(
    db: AsyncSession,
    root: Path,
    asset: Asset,
    st: os.stat_result,
    scan_time: datetime,
) -> Asset | None:
    abs_path = safe_join(root, asset.relative_path)
    try:
        asset.partial_hash = await anyio.to_thread.run_sync(partial_hash, abs_path)
    except OSError:
        return None
    asset.size_bytes = st.st_size
    asset.mtime = datetime.fromtimestamp(st.st_mtime, tz=UTC)
    asset.content_hash = None  # stale; recomputed on demand
    asset.availability = "online"
    asset.processing_status = "pending"
    asset.last_verified_at = scan_time
    return asset


async def _await_while_paused(db: AsyncSession, scan: Scan) -> str:
    """Block while paused; return the current status once actionable."""
    while True:
        await db.commit()
        await db.refresh(scan)
        if scan.status != "paused":
            return scan.status
        await anyio.sleep(PAUSE_POLL_SECONDS)


async def run_scan(
    db: AsyncSession,
    scan: Scan,
    library: Library,
    enqueue: Enqueue,
    *,
    batch_size: int = BATCH_SIZE,
    min_quiet_seconds: float = 60.0,
) -> None:
    """Execute one scan/reconciliation over a library. Safe to re-run."""
    root = Path(library.root_path)
    scan.started_at = _now()
    scan.status = "running"
    await db.commit()

    if not root.is_dir():
        affected = await mark_library_unmounted(db, library.id)
        scan.status = "failed"
        scan.error = "Library folder is not reachable. Existing entries were kept."
        scan.finished_at = _now()
        await db.commit()
        log.warning("scan.unmounted", library=library.name, assets_flagged=affected)
        return

    include_exts = (
        {e.lower().lstrip(".") for e in library.include_extensions}
        if library.include_extensions
        else set(SUPPORTED_EXTENSIONS)
    )
    walker = _walk(root, include_exts, list(library.exclude_globs))
    scan_time = scan.started_at
    to_enqueue: list[uuid.UUID] = []

    while True:
        status = await _await_while_paused(db, scan)
        if status == "cancelled":
            scan.finished_at = _now()
            await db.commit()
            log.info("scan.cancelled", library=library.name)
            return

        batch = await anyio.to_thread.run_sync(lambda: list(islice(walker, batch_size)))
        if not batch:
            break

        rels = [rel for rel, _ in batch]
        existing_rows = (
            await db.execute(
                select(Asset).where(Asset.library_id == library.id, Asset.relative_path.in_(rels))
            )
        ).scalars()
        existing = {a.relative_path: a for a in existing_rows}
        now_epoch = time.time()

        for rel, st in batch:
            scan.files_seen += 1
            asset = existing.get(rel)
            if asset is None:
                if not looks_at_rest(st.st_size, st.st_mtime, now_epoch, min_quiet_seconds):
                    scan.files_deferred += 1
                    continue
                new_asset, action = await _index_new_file(db, library, root, rel, st, scan_time)
                if action == "new":
                    scan.files_new += 1
                elif action == "moved":
                    scan.files_moved += 1
                else:
                    scan.files_deferred += 1
                if new_asset is not None:
                    to_enqueue.append(new_asset.id)
            else:
                stored_mtime = _as_utc(asset.mtime).timestamp()
                changed = asset.size_bytes != st.st_size or abs(stored_mtime - st.st_mtime) > 1.0
                if changed:
                    if not looks_at_rest(st.st_size, st.st_mtime, now_epoch, min_quiet_seconds):
                        scan.files_deferred += 1
                        continue
                    updated = await _apply_change(db, root, asset, st, scan_time)
                    if updated is None:
                        scan.files_deferred += 1
                    else:
                        scan.files_changed += 1
                        to_enqueue.append(updated.id)
                else:
                    asset.last_verified_at = scan_time
                    if asset.availability != "online":
                        asset.availability = "online"

        await db.commit()  # progress counters + batch rows become visible
        for asset_id in to_enqueue:
            enqueue(asset_id)
        to_enqueue.clear()

    # Anything online that this scan never touched is gone from disk: flag,
    # never delete — the catalog survives NAS hiccups and human mistakes.
    missing = await db.execute(
        update(Asset)
        .where(
            Asset.library_id == library.id,
            Asset.availability == "online",
            (Asset.last_verified_at.is_(None)) | (Asset.last_verified_at < scan_time),
        )
        .values(availability="missing")
    )
    scan.files_missing = int(missing.rowcount or 0)  # type: ignore[attr-defined]
    scan.status = "completed"
    scan.finished_at = _now()
    library.last_scan_at = scan.finished_at
    await db.commit()
    log.info(
        "scan.completed",
        library=library.name,
        seen=scan.files_seen,
        new=scan.files_new,
        changed=scan.files_changed,
        moved=scan.files_moved,
        missing=scan.files_missing,
        deferred=scan.files_deferred,
    )
