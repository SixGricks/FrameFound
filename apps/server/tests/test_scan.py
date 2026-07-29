"""Scan engine tests against a real temp directory + in-memory SQLite."""

import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from framefound.db.base import Base
from framefound.db.models import Asset, Library, Scan
from framefound.scanner.scan import run_scan

OLD = time.time() - 3600


@pytest.fixture()
async def db() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def write_old(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (OLD, OLD))


async def make_library(db: AsyncSession, root: Path) -> Library:
    root.mkdir(parents=True, exist_ok=True)
    library = Library(name=f"lib-{uuid.uuid4().hex[:8]}", root_path=str(root))
    db.add(library)
    await db.commit()
    return library


async def scan_once(db: AsyncSession, library: Library, enqueued: list[uuid.UUID]) -> Scan:
    scan = Scan(library_id=library.id, status="pending")
    db.add(scan)
    await db.commit()
    await run_scan(db, scan, library, enqueued.append)
    return scan


async def test_initial_scan_indexes_supported_files(db: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "lib"
    write_old(root / "a.jpg", b"jpegdata")
    write_old(root / "sub" / "b.mp4", b"mp4data")
    write_old(root / "notes.txt", b"ignore me")
    write_old(root / ".hidden" / "c.jpg", b"hidden")
    library = await make_library(db, root)

    enqueued: list[uuid.UUID] = []
    scan = await scan_once(db, library, enqueued)

    assert scan.status == "completed"
    assert scan.files_seen == 2
    assert scan.files_new == 2
    assets = (await db.execute(select(Asset))).scalars().all()
    assert {a.relative_path for a in assets} == {"a.jpg", "sub/b.mp4"}
    assert all(a.partial_hash for a in assets)
    assert all(a.availability == "online" for a in assets)
    assert len(enqueued) == 2


async def test_rescan_is_idempotent(db: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "lib"
    write_old(root / "a.jpg", b"data")
    library = await make_library(db, root)
    enqueued: list[uuid.UUID] = []
    await scan_once(db, library, enqueued)
    second = await scan_once(db, library, enqueued)

    assert second.files_new == 0
    assert second.files_changed == 0
    assert len(enqueued) == 1  # only the first scan enqueued work


async def test_changed_file_reprocessed(db: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "lib"
    write_old(root / "a.jpg", b"version-one")
    library = await make_library(db, root)
    enqueued: list[uuid.UUID] = []
    await scan_once(db, library, enqueued)

    write_old(root / "a.jpg", b"version-two-longer")
    scan = await scan_once(db, library, enqueued)

    assert scan.files_changed == 1
    asset = (await db.execute(select(Asset))).scalar_one()
    assert asset.size_bytes == len(b"version-two-longer")
    assert asset.processing_status == "pending"
    assert len(enqueued) == 2


async def test_missing_file_flagged_never_deleted(db: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "lib"
    write_old(root / "a.jpg", b"data")
    write_old(root / "b.jpg", b"other")
    library = await make_library(db, root)
    enqueued: list[uuid.UUID] = []
    await scan_once(db, library, enqueued)

    (root / "a.jpg").unlink()
    scan = await scan_once(db, library, enqueued)

    assert scan.files_missing == 1
    assets = {a.relative_path: a for a in (await db.execute(select(Asset))).scalars()}
    assert len(assets) == 2  # nothing deleted from the catalog
    assert assets["a.jpg"].availability == "missing"
    assert assets["b.jpg"].availability == "online"


async def test_moved_file_keeps_asset_identity(db: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "lib"
    write_old(root / "old" / "clip.mp4", b"same-bytes-here")
    library = await make_library(db, root)
    enqueued: list[uuid.UUID] = []
    await scan_once(db, library, enqueued)
    original_id = (await db.execute(select(Asset))).scalar_one().id

    new_home = root / "sorted" / "2026" / "clip.mp4"
    new_home.parent.mkdir(parents=True)
    (root / "old" / "clip.mp4").rename(new_home)
    os.utime(new_home, (OLD, OLD))
    scan = await scan_once(db, library, enqueued)

    assert scan.files_moved == 1
    assert scan.files_new == 0
    asset = (await db.execute(select(Asset))).scalar_one()
    assert asset.id == original_id  # identity survives reorganization
    assert asset.relative_path == "sorted/2026/clip.mp4"
    assert len(enqueued) == 1  # a move needs no reprocessing


async def test_fresh_file_deferred(db: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "lib"
    root.mkdir()
    (root / "uploading.mp4").write_bytes(b"still-copying")  # mtime = now
    library = await make_library(db, root)
    enqueued: list[uuid.UUID] = []
    scan = await scan_once(db, library, enqueued)

    assert scan.files_deferred == 1
    assert (await db.execute(select(Asset))).scalar_one_or_none() is None


async def test_unreachable_root_flags_unmounted(db: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "lib"
    write_old(root / "a.jpg", b"data")
    library = await make_library(db, root)
    enqueued: list[uuid.UUID] = []
    await scan_once(db, library, enqueued)

    import shutil

    shutil.rmtree(root)  # simulates the NAS mount disappearing
    scan = await scan_once(db, library, enqueued)

    assert scan.status == "failed"
    assert scan.error is not None
    asset = (await db.execute(select(Asset))).scalar_one()
    assert asset.availability == "unmounted"


async def test_include_extensions_filter(db: AsyncSession, tmp_path: Path) -> None:
    root = tmp_path / "lib"
    write_old(root / "a.jpg", b"img")
    write_old(root / "b.mp4", b"vid")
    root.mkdir(exist_ok=True)
    library = Library(name="filtered", root_path=str(root), include_extensions=["mp4"])
    db.add(library)
    await db.commit()
    enqueued: list[uuid.UUID] = []
    scan = await scan_once(db, library, enqueued)

    assert scan.files_new == 1
    assert (await db.execute(select(Asset))).scalar_one().relative_path == "b.mp4"


async def test_move_between_libraries_keeps_identity(db: AsyncSession, tmp_path: Path) -> None:
    """Media reorganised into a DIFFERENT library must keep its asset — and
    therefore its transcripts, thumbnails and embeddings — not be rebuilt."""
    source_root = tmp_path / "archive-a"
    dest_root = tmp_path / "archive-b"
    write_old(source_root / "clip.mp4", b"identical-bytes-for-move")
    dest_root.mkdir(parents=True)

    source = await make_library(db, source_root)
    dest = await make_library(db, dest_root)
    enqueued: list[uuid.UUID] = []
    await scan_once(db, source, enqueued)
    original_id = (await db.execute(select(Asset))).scalar_one().id

    # The editor drags the file from one library's folder to the other's.
    moved_to = dest_root / "2026" / "clip.mp4"
    moved_to.parent.mkdir(parents=True)
    (source_root / "clip.mp4").rename(moved_to)
    os.utime(moved_to, (OLD, OLD))

    scan = await scan_once(db, dest, enqueued)

    assert scan.files_moved == 1
    assert scan.files_new == 0
    asset = (await db.execute(select(Asset))).scalar_one()
    assert asset.id == original_id  # same asset, all derived data intact
    assert asset.library_id == dest.id
    assert asset.relative_path == "2026/clip.mp4"
    assert len(enqueued) == 1  # a move needs no reprocessing


async def test_duplicate_in_another_library_is_not_a_move(db: AsyncSession, tmp_path: Path) -> None:
    """Identical bytes that still exist in BOTH places are two real assets."""
    root_a = tmp_path / "lib-a"
    root_b = tmp_path / "lib-b"
    write_old(root_a / "copy.jpg", b"same-content-both-places")
    write_old(root_b / "copy.jpg", b"same-content-both-places")

    lib_a = await make_library(db, root_a)
    lib_b = await make_library(db, root_b)
    enqueued: list[uuid.UUID] = []
    await scan_once(db, lib_a, enqueued)
    scan = await scan_once(db, lib_b, enqueued)

    assert scan.files_new == 1
    assert scan.files_moved == 0
    assert len((await db.execute(select(Asset))).scalars().all()) == 2
