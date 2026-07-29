"""The transcript retry sweep.

Written after 555 transcription jobs failed on a models-directory permission
problem, exhausted their Celery retries, and were never looked at again. The
permission fault was fixed long before anyone noticed the backlog, because
nothing was watching for work that had quietly stopped.

The interesting cases are the ones that must NOT be re-queued: a music bed
produces no transcript legitimately, and a file ffmpeg cannot open will fail
forever.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Job, Library, Transcript
from framefound.scanner import __main__ as scanner

BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


@pytest.fixture()
async def db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[AsyncSession]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'tr.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "tr-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()
    get_settings.cache_clear()


async def _library(db: AsyncSession, *, transcribe: bool = True, enabled: bool = True) -> Library:
    library = Library(
        name="L", root_path="/media/l", transcribe_enabled=transcribe, enabled=enabled
    )
    db.add(library)
    await db.flush()
    return library


async def _asset(db: AsyncSession, library: Library, name: str, **kwargs: object) -> Asset:
    defaults: dict = {
        "media_type": "video",
        "audio_codec": "aac",
        "availability": "online",
        "processing_status": "ready",
    }
    defaults.update(kwargs)
    asset = Asset(
        library_id=library.id,
        relative_path=name,
        filename=name,
        extension="mp4",
        size_bytes=1000,
        mtime=BASE,
        **defaults,  # type: ignore[arg-type]
    )
    db.add(asset)
    await db.flush()
    return asset


async def _job(db: AsyncSession, asset: Asset, status: str, n: int = 1) -> None:
    for _ in range(n):
        db.add(Job(task_name="transcribe_asset", asset_id=asset.id, status=status))
    await db.flush()


@pytest.fixture()
def queued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture what the sweep hands to Celery, with an idle queue."""
    sent: list[str] = []

    class FakeTask:
        @staticmethod
        def delay(asset_id: str) -> None:
            sent.append(asset_id)

    import types

    module = types.ModuleType("framefound.processing.tasks")
    module.transcribe_asset = FakeTask  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules, "framefound.processing.tasks", module)

    async def idle(_name: str) -> bool:
        return False

    monkeypatch.setattr(scanner, "_queue_busy", idle)
    return sent


async def test_audio_that_never_got_a_turn_is_requeued(db: AsyncSession, queued: list[str]) -> None:
    library = await _library(db)
    asset = await _asset(db, library, "talk.mp4")
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == [str(asset.id)]


async def test_a_music_bed_is_not_requeued_forever(db: AsyncSession, queued: list[str]) -> None:
    """No speech means no transcript row, legitimately. A succeeded job is
    what proves the asset had its turn — absence of a transcript is not."""
    library = await _library(db)
    asset = await _asset(db, library, "music.mp4")
    await _job(db, asset, "succeeded")
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


async def test_an_asset_that_already_has_a_transcript_is_left_alone(
    db: AsyncSession, queued: list[str]
) -> None:
    library = await _library(db)
    asset = await _asset(db, library, "done.mp4")
    await _job(db, asset, "succeeded")
    db.add(
        Transcript(
            asset_id=asset.id, language="en", model_name="base", processed_at=BASE, segment_count=3
        )
    )
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


async def test_a_repeatedly_failing_file_is_given_up_on(
    db: AsyncSession, queued: list[str]
) -> None:
    library = await _library(db)
    broken = await _asset(db, library, "broken.mp4")
    await _job(db, broken, "failed", n=scanner.MAX_TRANSCRIBE_ATTEMPTS)
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


async def test_a_file_that_failed_a_couple_of_times_is_retried(
    db: AsyncSession, queued: list[str]
) -> None:
    """The whole point: the 555 failures were an environment fault, not bad
    files, and they must get another chance once it is fixed."""
    library = await _library(db)
    asset = await _asset(db, library, "was-broken.mp4")
    await _job(db, asset, "failed", n=scanner.MAX_TRANSCRIBE_ATTEMPTS - 1)
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == [str(asset.id)]


async def test_libraries_with_transcription_off_are_skipped(
    db: AsyncSession, queued: list[str]
) -> None:
    library = await _library(db, transcribe=False)
    await _asset(db, library, "ignored.mp4")
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


async def test_silent_video_is_not_queued(db: AsyncSession, queued: list[str]) -> None:
    library = await _library(db)
    await _asset(db, library, "silent.mp4", audio_codec=None)
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


async def test_offline_assets_are_not_queued(db: AsyncSession, queued: list[str]) -> None:
    library = await _library(db)
    await _asset(db, library, "gone.mp4", availability="missing")
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


async def test_assets_still_awaiting_metadata_are_not_queued(
    db: AsyncSession, queued: list[str]
) -> None:
    # audio_codec is only known after metadata extraction, so anything not
    # yet `ready` would be judged on incomplete information.
    library = await _library(db)
    await _asset(db, library, "pending.mp4", processing_status="pending")
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


async def test_a_busy_queue_is_left_undisturbed(
    db: AsyncSession, queued: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backlog means the workers are busy, not that work was lost.
    Re-queueing on top of it is how 32k duplicate messages happened once."""
    library = await _library(db)
    await _asset(db, library, "waiting.mp4")
    await db.commit()

    async def busy(_name: str) -> bool:
        return True

    monkeypatch.setattr(scanner, "_queue_busy", busy)
    await scanner._requeue_missing_transcripts(db)
    assert queued == []


async def test_the_batch_is_bounded(db: AsyncSession, queued: list[str]) -> None:
    library = await _library(db)
    for i in range(scanner.TRANSCRIBE_BATCH + 10):
        await _asset(db, library, f"clip{i}.mp4")
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert len(queued) == scanner.TRANSCRIBE_BATCH


async def test_a_disabled_library_is_skipped(db: AsyncSession, queued: list[str]) -> None:
    library = await _library(db, enabled=False)
    await _asset(db, library, "paused.mp4")
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


async def test_audio_only_assets_count_even_without_a_codec_field(
    db: AsyncSession, queued: list[str]
) -> None:
    library = await _library(db)
    asset = await _asset(db, library, "voice.wav", media_type="audio", audio_codec=None)
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == [str(asset.id)]


async def test_the_sweep_survives_celery_being_unavailable(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = await _library(db)
    await _asset(db, library, "clip.mp4")
    await db.commit()

    async def idle(_name: str) -> bool:
        return False

    monkeypatch.setattr(scanner, "_queue_busy", idle)
    monkeypatch.setitem(__import__("sys").modules, "framefound.processing.tasks", None)
    await scanner._requeue_missing_transcripts(db)  # must not raise
