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
from datetime import UTC, datetime, timedelta
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


async def _job(
    db: AsyncSession, asset: Asset, status: str, n: int = 1, age: timedelta | None = None
) -> None:
    for _ in range(n):
        job = Job(task_name="transcribe_asset", asset_id=asset.id, status=status)
        if age is not None:
            job.started_at = datetime.now(UTC) - age
        db.add(job)
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
    rows = scanner.MAX_TRANSCRIBE_ATTEMPTS * scanner.TRANSCRIBE_ROWS_PER_ATTEMPT
    await _job(db, broken, "failed", n=rows)
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


async def test_one_queueing_with_its_retries_is_not_three_attempts(
    db: AsyncSession, queued: list[str]
) -> None:
    """One queueing writes a failed row per Celery try (the first plus two
    retries). Counting rows meant a single bad moment used every turn the
    file would ever get — the 555-job incident again, now permanent."""
    library = await _library(db)
    asset = await _asset(db, library, "one-bad-night.mp4")
    await _job(db, asset, "failed", n=scanner.TRANSCRIBE_ROWS_PER_ATTEMPT)
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == [str(asset.id)]


async def test_a_file_that_keeps_killing_the_worker_is_given_up_on(
    db: AsyncSession, queued: list[str]
) -> None:
    """Abandoned (reaped after an OOM kill) counts as an attempt, so the
    sweep stops queueing the file afresh. Ending the broker's own redelivery
    of it is the task's cap (test_transcription_qa covers that)."""
    library = await _library(db)
    asset = await _asset(db, library, "too-big.mov")
    rows = scanner.MAX_TRANSCRIBE_ATTEMPTS * scanner.TRANSCRIBE_ROWS_PER_ATTEMPT
    await _job(db, asset, "abandoned", n=rows)
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


async def test_the_clip_being_transcribed_right_now_is_not_queued_again(
    db: AsyncSession, queued: list[str]
) -> None:
    """With acks_late the in-flight message has left the queue list, so the
    queue reads idle mid-run; the sweep queued the same 3-hour file again."""
    library = await _library(db)
    asset = await _asset(db, library, "mid-run.mp4")
    await _job(db, asset, "running", age=timedelta(minutes=40))
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


async def test_a_dead_workers_row_does_not_hold_the_clip_for_ever(
    db: AsyncSession, queued: list[str]
) -> None:
    # Was ORPHAN_JOB_AGE: the sweep queued its own copy at the 6 h reap and
    # the broker's came back at 12 h (see the deploy test below).
    library = await _library(db)
    running = await _asset(db, library, "worker-died.mp4")
    reaped = await _asset(db, library, "worker-died-and-reaped.mp4")
    age = scanner.BROKER_REDELIVERY_AGE + timedelta(minutes=5)
    await _job(db, running, "running", age=age)
    await _job(db, reaped, "abandoned", age=age)
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert sorted(queued) == sorted([str(running.id), str(reaped.id)])


async def test_a_run_killed_by_a_deploy_waits_for_the_brokers_copy(
    db: AsyncSession, queued: list[str]
) -> None:
    """docker stop SIGKILLs a mid-run worker; its unacked message returns
    at the 12 h visibility timeout. The reaper calls the row abandoned at
    6 h, and the sweep queued a second copy then: the file was heard twice."""
    library = await _library(db)
    asset = await _asset(db, library, "killed-by-deploy.mp4")
    await _job(db, asset, "abandoned", age=scanner.ORPHAN_JOB_AGE + timedelta(hours=1))
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


def test_the_sweep_waits_exactly_as_long_as_the_broker() -> None:
    from framefound.celery_app import celery_app

    timeout = celery_app.conf.broker_transport_options["visibility_timeout"]
    assert scanner.BROKER_REDELIVERY_AGE.total_seconds() == timeout
    assert scanner.BROKER_REDELIVERY_AGE > scanner.ORPHAN_JOB_AGE


async def _transcript(db: AsyncSession, asset: Asset, model: str, segments: int) -> None:
    db.add(
        Transcript(
            asset_id=asset.id,
            language="",
            model_name=model,
            processed_at=BASE,
            segment_count=segments,
        )
    )
    await db.flush()


async def test_a_clip_the_old_code_heard_nothing_in_is_heard_again(
    db: AsyncSession, queued: list[str]
) -> None:
    """Migration 0027 wrote an empty row for every succeeded run without a
    transcript. Many were an auctioneer over a generator or a lav on stream
    2: exactly what the old filter and the stream-0 decode missed."""
    library = await _library(db)
    asset = await _asset(db, library, "auctioneer-over-generator.mp4")
    await _job(db, asset, "succeeded", age=timedelta(days=30))
    await _transcript(db, asset, scanner.LEGACY_NO_SPEECH_MODEL, 0)
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == [str(asset.id)]


async def test_a_current_run_that_heard_nothing_is_not_heard_again(
    db: AsyncSession, queued: list[str]
) -> None:
    # The re-listen replaces the placeholder with the real model's row, so
    # each legacy clip gets one more turn and then drops out.
    library = await _library(db)
    asset = await _asset(db, library, "music-bed.mp4")
    await _job(db, asset, "succeeded", age=timedelta(days=30))
    await _job(db, asset, "succeeded")
    await _transcript(db, asset, "faster-whisper/small", 0)
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


async def test_a_legacy_clip_that_keeps_failing_is_still_given_up_on(
    db: AsyncSession, queued: list[str]
) -> None:
    library = await _library(db)
    asset = await _asset(db, library, "legacy-broken.mp4")
    await _job(db, asset, "succeeded", age=timedelta(days=30))
    await _transcript(db, asset, scanner.LEGACY_NO_SPEECH_MODEL, 0)
    rows = scanner.MAX_TRANSCRIBE_ATTEMPTS * scanner.TRANSCRIBE_ROWS_PER_ATTEMPT
    await _job(db, asset, "failed", n=rows)
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == []


def test_the_sweep_and_the_backfill_agree_on_the_placeholder() -> None:
    import importlib.util

    path = Path(__file__).parents[1] / "migrations" / "versions" / "0027_legacy_no_speech.py"
    spec = importlib.util.spec_from_file_location("migration_0027_marker", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.LEGACY_MODEL == scanner.LEGACY_NO_SPEECH_MODEL


async def test_a_run_skipped_while_transcription_was_off_is_not_a_turn(
    db: AsyncSession, queued: list[str]
) -> None:
    library = await _library(db)
    asset = await _asset(db, library, "was-off.mp4")
    await _job(db, asset, "skipped")
    await db.commit()

    await scanner._requeue_missing_transcripts(db)
    assert queued == [str(asset.id)]


def test_the_scanner_agrees_with_the_task_about_retries() -> None:
    from framefound.processing.tasks import TRANSCRIBE_RETRIES

    assert scanner.TRANSCRIBE_ROWS_PER_ATTEMPT == TRANSCRIBE_RETRIES + 1


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
