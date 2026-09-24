"""The visual repair sweep.

Found in review (2026-09): 152 photographs and 439 videos were `ready` with
no frames — outside visual search, face detection, and the room labels that
order a listing — because nothing looked at an asset again once metadata
succeeded. The sweep is what finds them; these tests pin what it must and
must not re-queue, above all that it never touches a library whose share is
down, where a retry would only mark the asset missing.
"""

import types
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Derivative, Frame, Job, Library
from framefound.scanner import __main__ as scanner

OLD = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


@pytest.fixture()
async def db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[AsyncSession]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'vr.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "vr-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    get_settings.cache_clear()


@pytest.fixture()
def queued(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """What the sweep hands to Celery, per task, with idle queues."""
    sent: dict[str, list[str]] = {"thumbs": [], "frames": [], "vectors": []}

    def fake(bucket: str) -> type:
        class Task:
            @staticmethod
            def delay(asset_id: str) -> None:
                sent[bucket].append(asset_id)

        return Task

    module = types.ModuleType("framefound.processing.tasks")
    module.generate_derivatives = fake("thumbs")  # type: ignore[attr-defined]
    module.sample_frames = fake("frames")  # type: ignore[attr-defined]
    module.embed_frames = fake("vectors")  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "framefound.processing.tasks", module)

    async def idle(_name: str) -> bool:
        return False

    monkeypatch.setattr(scanner, "_queue_busy", idle)
    return sent


async def _library(db: AsyncSession, root: Path, *, populated: bool = True) -> Library:
    root.mkdir(parents=True, exist_ok=True)
    if populated:
        (root / "something.jpg").write_bytes(b"x")
    library = Library(name=f"L-{uuid.uuid4().hex[:6]}", root_path=str(root))
    db.add(library)
    await db.flush()
    return library


async def _asset(db: AsyncSession, library: Library, name: str, **kwargs: object) -> Asset:
    fields: dict = {
        "media_type": "image",
        "availability": "online",
        "processing_status": "ready",
        "first_indexed_at": OLD,
    }
    fields.update(kwargs)
    asset = Asset(
        library_id=library.id,
        relative_path=name,
        filename=name,
        extension=name.rsplit(".", 1)[-1],
        size_bytes=1000,
        mtime=OLD,
        **fields,  # type: ignore[arg-type]
    )
    db.add(asset)
    await db.flush()
    return asset


def _thumb(asset: Asset) -> Derivative:
    return Derivative(
        asset_id=asset.id,
        kind="thumbnail",
        relative_path=f"t/{asset.id}.webp",
        media_format="webp",
        status="ready",
    )


async def test_a_still_with_a_thumbnail_and_no_frames_is_resampled(
    db: AsyncSession, queued: dict[str, list[str]], tmp_path: Path
) -> None:
    """The race's signature: thumbnail present, frames never written."""
    library = await _library(db, tmp_path / "lib")
    asset = await _asset(db, library, "kitchen.jpg")
    db.add(_thumb(asset))
    db.add(Job(task_name="sample_frames", asset_id=asset.id, status="succeeded", started_at=OLD))
    await db.commit()

    await scanner._requeue_missing_visuals(db)
    assert queued == {"thumbs": [], "frames": [str(asset.id)], "vectors": []}


async def test_an_asset_without_a_thumbnail_gets_derivatives_first(
    db: AsyncSession, queued: dict[str, list[str]], tmp_path: Path
) -> None:
    library = await _library(db, tmp_path / "lib")
    asset = await _asset(db, library, "clip.mp4", media_type="video")
    await db.commit()

    await scanner._requeue_missing_visuals(db)
    assert queued["thumbs"] == [str(asset.id)]
    assert queued["frames"] == []


async def test_frames_without_vectors_are_embedded(
    db: AsyncSession, queued: dict[str, list[str]], tmp_path: Path
) -> None:
    library = await _library(db, tmp_path / "lib")
    asset = await _asset(db, library, "porch.jpg")
    db.add(_thumb(asset))
    db.add(Frame(asset_id=asset.id, ts_ms=0, relative_path="f.jpg", embedding=None))
    await db.commit()

    await scanner._requeue_missing_visuals(db)
    assert queued == {"thumbs": [], "frames": [], "vectors": [str(asset.id)]}


async def test_a_complete_asset_is_left_alone(
    db: AsyncSession, queued: dict[str, list[str]], tmp_path: Path
) -> None:
    library = await _library(db, tmp_path / "lib")
    asset = await _asset(db, library, "done.jpg")
    db.add(_thumb(asset))
    db.add(Frame(asset_id=asset.id, ts_ms=0, relative_path="f.jpg", embedding=[0.1] * 512))
    await db.commit()

    await scanner._requeue_missing_visuals(db)
    assert queued == {"thumbs": [], "frames": [], "vectors": []}


async def test_a_library_whose_share_is_down_is_never_touched(
    db: AsyncSession, queued: dict[str, list[str]], tmp_path: Path
) -> None:
    """An empty mountpoint: every retry would find no original and flag the
    asset missing, turning an outage into a catalogue full of false
    deletions."""
    library = await _library(db, tmp_path / "unmounted", populated=False)
    await _asset(db, library, "a.jpg")
    await db.commit()

    await scanner._requeue_missing_visuals(db)
    assert queued == {"thumbs": [], "frames": [], "vectors": []}


async def test_a_file_that_keeps_failing_is_given_up_on(
    db: AsyncSession, queued: dict[str, list[str]], tmp_path: Path
) -> None:
    library = await _library(db, tmp_path / "lib")
    asset = await _asset(db, library, "corrupt.mp4", media_type="video")
    db.add(_thumb(asset))
    for _ in range(scanner.MAX_VISUAL_ATTEMPTS):
        db.add(
            Job(task_name="sample_frames", asset_id=asset.id, status="succeeded", started_at=OLD)
        )
    await db.commit()

    await scanner._requeue_missing_visuals(db)
    assert queued["frames"] == []


async def test_work_that_may_still_be_in_flight_is_not_duplicated(
    db: AsyncSession, queued: dict[str, list[str]], tmp_path: Path
) -> None:
    library = await _library(db, tmp_path / "lib")
    asset = await _asset(db, library, "fresh-job.jpg")
    db.add(_thumb(asset))
    db.add(Job(task_name="sample_frames", asset_id=asset.id, status="running"))  # now
    await db.commit()

    await scanner._requeue_missing_visuals(db)
    assert queued["frames"] == []


async def test_a_brand_new_asset_gets_the_pipeline_first(
    db: AsyncSession, queued: dict[str, list[str]], tmp_path: Path
) -> None:
    library = await _library(db, tmp_path / "lib")
    await _asset(db, library, "just-scanned.jpg", first_indexed_at=datetime.now(UTC))
    await db.commit()

    await scanner._requeue_missing_visuals(db)
    assert queued == {"thumbs": [], "frames": [], "vectors": []}


async def test_busy_queues_are_left_undisturbed(
    db: AsyncSession,
    queued: dict[str, list[str]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = await _library(db, tmp_path / "lib")
    await _asset(db, library, "waiting.jpg")
    await db.commit()

    async def busy(name: str) -> bool:
        return name == "frames"

    monkeypatch.setattr(scanner, "_queue_busy", busy)
    await scanner._requeue_missing_visuals(db)
    assert queued == {"thumbs": [], "frames": [], "vectors": []}


async def test_the_batch_is_bounded(
    db: AsyncSession, queued: dict[str, list[str]], tmp_path: Path
) -> None:
    library = await _library(db, tmp_path / "lib")
    for i in range(scanner.VISUALS_BATCH + 5):
        await _asset(db, library, f"clip{i}.mp4", media_type="video")
    await db.commit()

    await scanner._requeue_missing_visuals(db)
    assert len(queued["thumbs"]) == scanner.VISUALS_BATCH
