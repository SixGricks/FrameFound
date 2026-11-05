"""The 2026-11 QA review of transcription: every video with talking gets a
transcript, the transcript stays linked to its video, and nothing invented
gets in.

Each test here fails against the code as it was before the review.
"""

import asyncio
import importlib.util
import shutil
import subprocess
import wave
from array import array
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from framefound.ai import transcription as ai_transcription
from framefound.ai.transcription import SpeechSegment, TranscriptionResult, keep_segment
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Derivative, Job, Library, Transcript
from framefound.media.subtitles import build_srt
from framefound.processing import ffmpeg
from framefound.processing import tasks as task_module

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}

SPEECH = TranscriptionResult(
    language="en",
    language_probability=0.97,
    duration_s=20.0,
    model_name="fake/test",
    segments=[
        SpeechSegment(0.0, 3.5, "Starting bid on the tractor is five thousand."),
        SpeechSegment(3.5, 7.25, "Do I hear fifty-five?"),
    ],
)
SILENCE = TranscriptionResult(
    language="en", language_probability=0.4, duration_s=20.0, model_name="fake/test"
)


class FakeProvider:
    def __init__(self) -> None:
        self.result = SPEECH
        self.heard: list[Path] = []

    def transcribe(self, source: Path) -> TranscriptionResult:
        self.heard.append(source)
        return self.result


# --- the speech filter ------------------------------------------------------


def test_real_speech_with_one_doubtful_signal_is_kept() -> None:
    """An auctioneer over a generator: low confidence, but clearly speech.
    The old filter dropped a cue if *either* signal fired."""
    assert keep_segment("Sold to number forty-two", avg_logprob=-1.2, no_speech_prob=0.1)
    assert keep_segment("Sold to number forty-two", avg_logprob=-0.3, no_speech_prob=0.7)


def test_a_cue_that_is_both_unsure_and_silent_is_dropped() -> None:
    assert not keep_segment("mm", avg_logprob=-1.4, no_speech_prob=0.8)


@pytest.mark.parametrize(
    "text", ["Thank you for watching!", "Thanks for watching.", "Please subscribe"]
)
def test_known_hallucinations_are_dropped_however_confident(text: str) -> None:
    assert not keep_segment(text, avg_logprob=-0.1, no_speech_prob=0.01)


def test_a_repetition_loop_is_dropped() -> None:
    # Was expected at avg_logprob -0.2: the ratio alone dropped it. The ratio
    # is the whole window's (see keep_segment), so it now needs doubt too.
    assert not keep_segment("the the the the the", -1.3, 0.0, compression_ratio=3.1)


def test_an_auctioneers_chant_is_kept_though_it_compresses_well() -> None:
    """Bid calling is repetitive by nature and compresses past 2.4 at every
    temperature; decoded confidently, it is the speech the catalogue is for."""
    chant = "Fifty, fifty, fifty, now fifty-five, fifty-five, do I hear sixty"
    assert keep_segment(chant, avg_logprob=-0.3, no_speech_prob=0.02, compression_ratio=2.9)


def test_plain_speech_sharing_a_repetitive_window_is_kept() -> None:
    # faster-whisper stamps the window's ratio on every cue cut from it.
    text = "This farm has two bank barns and a tenant house."
    assert keep_segment(text, avg_logprob=-0.25, no_speech_prob=0.05, compression_ratio=2.7)


def test_a_phrase_inside_real_speech_is_not_mistaken_for_one() -> None:
    assert keep_segment("Thank you for watching the farm with us today", -0.2, 0.05)


# --- every audio stream is heard --------------------------------------------


def test_several_audio_streams_are_mixed_for_whisper(tmp_path: Path) -> None:
    argv = ffmpeg.speech_mix_args(tmp_path / "in.mxf", tmp_path / "out.wav", streams=4)
    graph = argv[argv.index("-filter_complex") + 1]
    assert graph.startswith("[0:a:0][0:a:1][0:a:2][0:a:3]amix=inputs=4")
    assert argv[argv.index("-ar") + 1] == "16000"
    assert argv[-1].endswith("out.wav")


def test_the_summed_streams_are_limited_before_the_wav(tmp_path: Path) -> None:
    """Unity-gain amix of a mic and its safety copy goes past full scale."""
    argv = ffmpeg.speech_mix_args(tmp_path / "in.mxf", tmp_path / "out.wav", streams=2)
    graph = argv[argv.index("-filter_complex") + 1]
    assert "amix=inputs=2:normalize=0,alimiter=" in graph
    assert graph.endswith("[speech]")


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_mic_and_its_safety_copy_do_not_clip(tmp_path: Path) -> None:
    """Two identical tones at 0.9 of full scale sum to 1.8 without a
    limiter; the WAV Whisper hears must not be flattened against the rails."""
    src = tmp_path / "two-copies.mkv"
    tone = "sine=frequency=440:sample_rate=48000:duration=2,volume=0.9"
    subprocess.run(  # noqa: S603 - fixed binary, argv form, no shell
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", tone, "-f", "lavfi", "-i", tone]
        + ["-map", "0:a", "-map", "1:a", "-c:a", "pcm_s16le", str(src)],
        check=True,
    )
    dst = tmp_path / "speech.wav"
    ffmpeg.extract_speech_audio(src, dst, streams=2)
    with wave.open(str(dst)) as wav:
        samples = array("h", wav.readframes(wav.getnframes()))
    railed = sum(1 for s in samples if s >= 32767 or s <= -32768)
    assert max(abs(s) for s in samples) > 16000  # still loud, just not clipped
    assert railed == 0


def test_one_audio_stream_is_mapped_without_a_filter(tmp_path: Path) -> None:
    argv = ffmpeg.speech_mix_args(tmp_path / "in.mp4", tmp_path / "out.wav", streams=1)
    assert "-filter_complex" not in argv
    assert argv[argv.index("-map") + 1] == "0:a:0"


# --- the pipeline, end to end with a fake model ------------------------------


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "transcribe-qa-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    get_settings.cache_clear()
    provider = FakeProvider()
    monkeypatch.setattr(ai_transcription, "_provider", provider)
    # No ffprobe in the test environment: one stream unless a test says not.
    monkeypatch.setattr(ffmpeg, "audio_stream_count", lambda _path: 1)

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    (lib_root / "Ledgerock walk – drone.mp4").write_bytes(b"fake-video-bytes")
    async with factory() as db:
        library = Library(name="L", root_path=str(lib_root))
        db.add(library)
        await db.flush()
        asset = Asset(
            library_id=library.id,
            relative_path="Ledgerock walk – drone.mp4",
            filename="Ledgerock walk – drone.mp4",
            extension="mp4",
            media_type="video",
            audio_codec="aac",
            size_bytes=16,
            mtime=datetime.now(UTC),
        )
        db.add(asset)
        await db.commit()
        asset_id = asset.id

    yield {"factory": factory, "asset_id": asset_id, "provider": provider, "url": db_url}
    await engine.dispose()
    get_settings.cache_clear()


async def _run(env: dict) -> None:
    await task_module._with_asset("transcribe_asset", env["asset_id"], task_module._transcribe)


@pytest.fixture()
async def client(env: dict) -> AsyncIterator[AsyncClient]:
    from framefound.main import create_app

    app = create_app()

    async def session_override() -> AsyncIterator:
        async with env["factory"]() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        await ac.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        await ac.post("/api/v1/auth/login", json=ADMIN)
        yield ac


async def test_no_speech_is_recorded_so_it_is_not_mistaken_for_pending(
    env: dict, client: AsyncClient
) -> None:
    env["provider"].result = SILENCE
    await _run(env)
    async with env["factory"]() as db:
        transcript = (await db.execute(select(Transcript))).scalar_one()
        assert transcript.segment_count == 0
    detail = await client.get(f"/api/v1/assets/{env['asset_id']}")
    assert detail.json()["transcript_state"] == "no_speech"


async def test_a_rerun_that_hears_nothing_clears_the_old_words(env: dict) -> None:
    await _run(env)
    env["provider"].result = SILENCE
    await _run(env)
    async with env["factory"]() as db:
        transcript = (await db.execute(select(Transcript))).scalar_one()
        assert transcript.segment_count == 0 and transcript.full_text == ""
        subtitles = (
            await db.execute(select(Derivative).where(Derivative.kind == "subtitle"))
        ).all()
        assert subtitles == []


async def test_transcript_state_follows_the_work(env: dict, client: AsyncClient) -> None:
    url = f"/api/v1/assets/{env['asset_id']}"
    assert (await client.get(url)).json()["transcript_state"] == "queued"
    async with env["factory"]() as db:
        db.add(Job(task_name="transcribe_asset", asset_id=env["asset_id"], status="failed"))
        await db.commit()
    assert (await client.get(url)).json()["transcript_state"] == "failed"
    await _run(env)
    assert (await client.get(url)).json()["transcript_state"] == "speech"


async def _add_job(env: dict, status: str, age: timedelta = timedelta(minutes=5)) -> None:
    async with env["factory"]() as db:
        db.add(
            Job(
                task_name="transcribe_asset",
                asset_id=env["asset_id"],
                status=status,
                started_at=datetime.now(UTC) - age,
            )
        )
        await db.commit()


async def _transcribe_jobs(env: dict) -> list[tuple[str, str | None]]:
    async with env["factory"]() as db:
        rows = await db.execute(
            select(Job.status, Job.error)
            .where(Job.task_name == "transcribe_asset")
            .order_by(Job.started_at)
        )
        return [(status, error) for status, error in rows.all()]


async def _deliver(env: dict) -> None:
    """The Celery task as a worker runs it (it calls asyncio.run itself)."""
    await asyncio.to_thread(task_module.transcribe_asset, str(env["asset_id"]))


async def test_a_clip_heard_by_the_old_code_reads_no_speech_not_queued(
    env: dict, client: AsyncClient
) -> None:
    """Before every run wrote a row, a clip that heard nothing left only a
    succeeded job. "Queued" promised a transcript no job would ever bring."""
    await _add_job(env, "succeeded", age=timedelta(days=30))
    detail = await client.get(f"/api/v1/assets/{env['asset_id']}")
    assert detail.json()["transcript_state"] == "no_speech"


async def test_a_running_job_is_queued_until_the_broker_cannot_bring_it_back(
    env: dict, client: AsyncClient
) -> None:
    # Was ORPHAN_JOB_AGE; the broker still redelivers a killed run until its
    # visibility timeout, and the sweep waits for that copy just as long.
    from framefound.scanner.__main__ import BROKER_REDELIVERY_AGE

    url = f"/api/v1/assets/{env['asset_id']}"
    await _add_job(env, "running", age=BROKER_REDELIVERY_AGE + timedelta(hours=1))
    assert (await client.get(url)).json()["transcript_state"] == "failed"
    await _add_job(env, "running", age=timedelta(minutes=10))
    assert (await client.get(url)).json()["transcript_state"] == "queued"


async def test_a_run_killed_by_a_deploy_reads_queued_not_failed(
    env: dict, client: AsyncClient
) -> None:
    """Reaped to abandoned at 6 h, the page said "failed, reprocess it",
    and a reprocess then ran beside the broker's own copy at 12 h."""
    from framefound.scanner.__main__ import BROKER_REDELIVERY_AGE, ORPHAN_JOB_AGE

    url = f"/api/v1/assets/{env['asset_id']}"
    await _add_job(env, "abandoned", age=BROKER_REDELIVERY_AGE + timedelta(hours=1))
    assert (await client.get(url)).json()["transcript_state"] == "failed"
    await _add_job(env, "abandoned", age=ORPHAN_JOB_AGE + timedelta(minutes=30))
    assert (await client.get(url)).json()["transcript_state"] == "queued"


async def test_a_run_with_transcription_off_is_skipped_not_succeeded(
    env: dict, client: AsyncClient
) -> None:
    """Recorded as succeeded it read as "heard nothing", and the sweep never
    came back to the clip once transcription was switched on."""
    async with env["factory"]() as db:
        library = (await db.execute(select(Library))).scalar_one()
        library.transcribe_enabled = False
        await db.commit()
    await _deliver(env)
    assert env["provider"].heard == []
    assert await _transcribe_jobs(env) == [("skipped", "Transcription is off for this library")]
    url = f"/api/v1/assets/{env['asset_id']}"
    assert (await client.get(url)).json()["transcript_state"] == "off"

    async with env["factory"]() as db:
        library = (await db.execute(select(Library))).scalar_one()
        library.transcribe_enabled = True
        await db.commit()
    # Never listened, so the sweep will queue it — not "no speech".
    assert (await client.get(url)).json()["transcript_state"] == "queued"


async def test_a_file_that_keeps_killing_the_worker_stops_being_redelivered(
    env: dict,
) -> None:
    """An OOM kill requeues the message on the worker-lost path, where
    Celery's retry count never moves; each death leaves a "running" row."""
    for _ in range(task_module.TRANSCRIBE_MAX_WORKER_DEATHS):
        await _add_job(env, "running", age=timedelta(minutes=30))
    await _deliver(env)
    assert env["provider"].heard == []
    status, error = (await _transcribe_jobs(env))[-1]
    assert status == "failed" and error is not None and "died" in error


async def test_one_lost_worker_does_not_give_the_file_up(env: dict) -> None:
    # A deploy that kills a run mid-file is one death, not a poison file.
    for _ in range(task_module.TRANSCRIBE_MAX_WORKER_DEATHS - 1):
        await _add_job(env, "abandoned", age=timedelta(hours=7))
    await _add_job(env, "running", age=timedelta(days=3))  # outside the window
    await _deliver(env)
    assert len(env["provider"].heard) == 1
    assert (await _transcribe_jobs(env))[-1][0] == "succeeded"


def test_the_broker_outwaits_the_longest_transcription() -> None:
    """kombu's default one-hour visibility timeout redelivered every longer
    transcription while it was still running, in a loop."""
    from framefound.celery_app import celery_app
    from framefound.scanner.__main__ import ORPHAN_JOB_AGE

    timeout = celery_app.conf.broker_transport_options["visibility_timeout"]
    assert timeout > ORPHAN_JOB_AGE.total_seconds()
    assert timeout > ffmpeg.PROXY_TIMEOUT_S
    assert timeout > ffmpeg.SPEECH_TIMEOUT_S


def _migration_0027() -> object:
    path = Path(__file__).parents[1] / "migrations" / "versions" / "0027_legacy_no_speech.py"
    spec = importlib.util.spec_from_file_location("migration_0027", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(env: dict, step: str) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = sa.create_engine(env["url"].replace("+aiosqlite", ""))
    try:
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            getattr(_migration_0027(), step)()
    finally:
        engine.dispose()


async def test_the_backfill_records_exactly_the_clips_heard_before(
    env: dict, client: AsyncClient
) -> None:
    async with env["factory"]() as db:
        library = (await db.execute(select(Library))).scalar_one()
        off = Library(name="Off", root_path=str(library.root_path), transcribe_enabled=False)
        db.add(off)
        await db.flush()

        def asset(name: str, lib: Library, audio_codec: str | None = "aac") -> Asset:
            row = Asset(
                library_id=lib.id,
                relative_path=name,
                filename=name,
                extension="mp4",
                media_type="video",
                audio_codec=audio_codec,
                size_bytes=1,
                mtime=datetime.now(UTC),
            )
            db.add(row)
            return row

        spoken = asset("spoken.mp4", library)
        silent_video = asset("no-audio.mp4", library, audio_codec=None)
        library_off = asset("off.mp4", off)
        only_failed = asset("failed.mp4", library)
        await db.flush()
        db.add(Transcript(asset_id=spoken.id, language="en", model_name="m", segment_count=4))
        for target, status in [
            (env["asset_id"], "succeeded"),  # the legacy music bed
            (spoken.id, "succeeded"),
            (silent_video.id, "succeeded"),
            (library_off.id, "succeeded"),
            (only_failed.id, "failed"),
        ]:
            db.add(Job(task_name="transcribe_asset", asset_id=target, status=status))
        await db.commit()

    await asyncio.to_thread(_run_migration, env, "upgrade")

    async with env["factory"]() as db:
        rows = (await db.execute(select(Transcript))).scalars().all()
        by_asset = {row.asset_id: row for row in rows}
    assert set(by_asset) == {env["asset_id"], spoken.id}
    legacy = by_asset[env["asset_id"]]
    assert (legacy.segment_count, legacy.full_text, legacy.language) == (0, "", "")
    assert legacy.model_name == "legacy/no-speech"
    assert by_asset[spoken.id].segment_count == 4
    # Was "no_speech": stated as fact for clips the old filter and the
    # stream-0 decode missed words in. The sweep hears these again.
    detail = await client.get(f"/api/v1/assets/{env['asset_id']}")
    assert detail.json()["transcript_state"] == "queued"

    await asyncio.to_thread(_run_migration, env, "downgrade")
    async with env["factory"]() as db:
        left = (await db.execute(select(Transcript.asset_id))).scalars().all()
    assert left == [spoken.id]


async def test_multitrack_audio_is_mixed_before_whisper_hears_it(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    mixed: list[tuple[Path, int]] = []

    def fake_extract(src: Path, dst: Path, streams: int) -> None:
        dst.write_bytes(b"RIFF")
        mixed.append((dst, streams))

    monkeypatch.setattr(ffmpeg, "audio_stream_count", lambda _path: 3)
    monkeypatch.setattr(ffmpeg, "extract_speech_audio", fake_extract)
    await _run(env)
    assert mixed and mixed[0][1] == 3
    assert env["provider"].heard == [mixed[0][0]]
    assert not mixed[0][0].exists()  # the scratch WAV does not outlive the run


async def test_captions_download_as_srt_and_vtt(env: dict, client: AsyncClient) -> None:
    await _run(env)
    srt = await client.get(f"/api/v1/assets/{env['asset_id']}/subtitles?format=srt")
    assert srt.status_code == 200
    assert srt.text.startswith("1\n00:00:00,000 --> 00:00:03,500\nStarting bid")
    disposition = srt.headers["content-disposition"]
    assert 'filename="Ledgerock walk _ drone.srt"' in disposition
    assert "filename*=UTF-8''Ledgerock%20walk%20%E2%80%93%20drone.srt" in disposition

    vtt = await client.get(f"/api/v1/assets/{env['asset_id']}/subtitles?format=vtt")
    assert vtt.text.startswith("WEBVTT") and "00:00:03.500 --> 00:00:07.250" in vtt.text
    assert (
        await client.get(f"/api/v1/assets/{env['asset_id']}/subtitles?format=docx")
    ).status_code == 422


async def test_reprocess_also_retranscribes(
    env: dict, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    class Capture:
        @staticmethod
        def delay(asset_id: str) -> None:
            sent.append(asset_id)

    monkeypatch.setattr(task_module, "generate_derivatives", Capture)
    monkeypatch.setattr(task_module, "generate_proxy", Capture)
    monkeypatch.setattr(task_module, "transcribe_asset", Capture)
    response = await client.post(f"/api/v1/assets/{env['asset_id']}/reprocess")
    assert response.status_code == 202
    assert sent.count(str(env["asset_id"])) == 3


@pytest.mark.parametrize(("heard", "state"), [(SPEECH, "speech"), (SILENCE, "no_speech")])
async def test_hearing_a_legacy_clip_again_replaces_the_placeholder(
    env: dict, client: AsyncClient, heard: TranscriptionResult, state: str
) -> None:
    """One more listen per legacy clip, then it drops out of the sweep: the
    re-run's row carries the real model's name, not the placeholder's."""
    from framefound.scanner.__main__ import LEGACY_NO_SPEECH_MODEL

    async with env["factory"]() as db:
        db.add(
            Transcript(
                asset_id=env["asset_id"],
                language="",
                model_name=LEGACY_NO_SPEECH_MODEL,
                segment_count=0,
            )
        )
        await db.commit()
    await _add_job(env, "succeeded", age=timedelta(days=30))
    url = f"/api/v1/assets/{env['asset_id']}"
    assert (await client.get(url)).json()["transcript_state"] == "queued"

    env["provider"].result = heard
    await _deliver(env)
    async with env["factory"]() as db:
        transcript = (await db.execute(select(Transcript))).scalar_one()
    assert transcript.model_name == "fake/test"
    assert (await client.get(url)).json()["transcript_state"] == state


def test_srt_numbers_cues_and_uses_commas() -> None:
    text = build_srt([SpeechSegment(61.5, 62.0, "Sold.")])
    assert text == "1\n00:01:01,500 --> 00:01:02,000\nSold.\n"
