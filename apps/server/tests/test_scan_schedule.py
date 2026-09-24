"""Scheduled scans: once per interval, and promptly when a share comes back.

A failed scan never sets last_scan_at, so a scheduler that measured from
the last success found an unreachable library due again on every
five-second pass — a failed scan row per tick for as long as the share was
down. And when the share came back, nothing hurried: the library waited out
its interval with its files still flagged missing.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.db.base import Base
from framefound.db.models import Library, Scan
from framefound.scanner import __main__ as scanner


@pytest.fixture()
async def db(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'sched.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _library(
    db: AsyncSession, root: Path, *, reachable: bool, last_scan: datetime | None
) -> Library:
    root.mkdir(parents=True, exist_ok=True)
    if reachable:
        (root / "a.jpg").write_bytes(b"x")
    library = Library(
        name=root.name, root_path=str(root), scan_interval_minutes=1440, last_scan_at=last_scan
    )
    db.add(library)
    await db.commit()
    return library


async def _scans(db: AsyncSession, library: Library) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(Scan).where(Scan.library_id == library.id)
        )
    ).scalar_one()


async def test_a_failed_scan_is_not_retried_every_tick(db: AsyncSession, tmp_path: Path) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=3)
    library = await _library(db, tmp_path / "gelco", reachable=False, last_scan=long_ago)
    db.add(
        Scan(
            library_id=library.id,
            status="failed",
            error="Library folder is empty",
            created_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    await db.commit()

    for _ in range(3):  # three passes of the scanner loop
        await scanner._schedule_due_scans(db)
    assert await _scans(db, library) == 1, "one attempt per interval, not one per tick"


async def test_a_share_that_came_back_is_rescanned_promptly(
    db: AsyncSession, tmp_path: Path
) -> None:
    library = await _library(
        db, tmp_path / "gelco", reachable=True, last_scan=datetime.now(UTC) - timedelta(hours=2)
    )
    db.add(
        Scan(
            library_id=library.id,
            status="failed",
            error="Library folder is empty",
            created_at=datetime.now(UTC) - timedelta(minutes=10),
        )
    )
    await db.commit()

    await scanner._schedule_due_scans(db)
    pending = (
        await db.execute(
            select(Scan).where(Scan.library_id == library.id, Scan.status == "pending")
        )
    ).scalars()
    assert len(list(pending)) == 1


async def test_a_share_still_down_waits_its_interval(db: AsyncSession, tmp_path: Path) -> None:
    library = await _library(
        db, tmp_path / "gelco", reachable=False, last_scan=datetime.now(UTC) - timedelta(hours=2)
    )
    db.add(
        Scan(
            library_id=library.id,
            status="failed",
            created_at=datetime.now(UTC) - timedelta(minutes=10),
        )
    )
    await db.commit()

    await scanner._schedule_due_scans(db)
    assert await _scans(db, library) == 1


async def test_an_ordinary_library_is_scanned_when_its_interval_passes(
    db: AsyncSession, tmp_path: Path
) -> None:
    library = await _library(
        db, tmp_path / "intel", reachable=True, last_scan=datetime.now(UTC) - timedelta(days=2)
    )
    await scanner._schedule_due_scans(db)
    assert await _scans(db, library) == 1

    await scanner._schedule_due_scans(db)
    assert await _scans(db, library) == 1, "the pending scan is not doubled"


async def test_a_manual_library_is_never_scheduled(db: AsyncSession, tmp_path: Path) -> None:
    library = await _library(db, tmp_path / "manual", reachable=True, last_scan=None)
    library.scan_interval_minutes = None
    await db.commit()

    await scanner._schedule_due_scans(db)
    assert await _scans(db, library) == 0


async def test_a_scan_interrupted_by_a_restart_runs_again(db: AsyncSession, tmp_path: Path) -> None:
    """A deploy or reboot mid-scan left the row "running" forever, and a
    running scan blocks every later one of its library, scheduled or pressed."""
    library = await _library(db, tmp_path / "intel", reachable=True, last_scan=None)
    db.add(Scan(library_id=library.id, status="running", files_seen=3000, files_new=1315))
    db.add(Scan(library_id=library.id, status="paused", files_seen=10))
    db.add(Scan(library_id=library.id, status="completed", files_seen=9000))
    await db.commit()

    await scanner._requeue_interrupted_scans(db)
    rows = (await db.execute(select(Scan.status, Scan.files_seen, Scan.files_new))).all()
    assert sorted(rows) == [("completed", 9000, 0), ("paused", 10, 0), ("pending", 0, 0)], (
        "the interrupted scan starts over; a pause is the operator's and stays"
    )
