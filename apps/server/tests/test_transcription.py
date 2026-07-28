"""Transcription pipeline with a fake provider + transcript/search endpoints."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai import transcription as ai_transcription
from framefound.ai.transcription import SpeechSegment, TranscriptionResult
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Derivative, Library, Transcript, TranscriptSegment
from framefound.media.subtitles import build_vtt
from framefound.processing import tasks as task_module

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}

FAKE_RESULT = TranscriptionResult(
    language="en",
    language_probability=0.98,
    duration_s=30.0,
    model_name="fake/test",
    segments=[
        SpeechSegment(0.0, 4.2, "Welcome to the auction preview."),
        SpeechSegment(4.2, 9.8, "The starting bid will be announced on site."),
        SpeechSegment(9.8, 14.0, "Settlement will be on or before thirty days."),
    ],
)


class FakeProvider:
    def transcribe(self, source: Path) -> TranscriptionResult:
        return FAKE_RESULT


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "transcribe-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    get_settings.cache_clear()
    monkeypatch.setattr(ai_transcription, "_provider", FakeProvider())

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    (lib_root / "sermon.mp3").write_bytes(b"fake-audio-bytes")
    async with factory() as db:
        library = Library(name="L", root_path=str(lib_root))
        db.add(library)
        await db.flush()
        asset = Asset(
            library_id=library.id,
            relative_path="sermon.mp3",
            filename="sermon.mp3",
            extension="mp3",
            media_type="audio",
            size_bytes=16,
            mtime=datetime.now(UTC),
        )
        db.add(asset)
        await db.commit()
        asset_id = asset.id

    yield {"factory": factory, "asset_id": asset_id, "tmp": tmp_path}
    await engine.dispose()
    get_settings.cache_clear()


async def _run_transcribe(env: dict) -> None:
    await task_module._with_asset("transcribe_asset", env["asset_id"], task_module._transcribe)


async def test_transcribe_creates_transcript_segments_and_vtt(env: dict) -> None:
    await _run_transcribe(env)

    async with env["factory"]() as db:
        transcript = (await db.execute(select(Transcript))).scalar_one()
        assert transcript.language == "en"
        assert transcript.segment_count == 3
        assert "starting bid" in transcript.full_text
        segments = (await db.execute(select(TranscriptSegment))).scalars().all()
        assert len(segments) == 3
        assert segments[0].start_ms == 0 and segments[1].start_ms == 4200

        subtitle = (
            await db.execute(select(Derivative).where(Derivative.kind == "subtitle"))
        ).scalar_one()
        assert subtitle.status == "ready"
        vtt = (env["tmp"] / "data" / subtitle.relative_path).read_text(encoding="utf-8")
        assert vtt.startswith("WEBVTT")
        assert "00:00:04.200 --> 00:00:09.800" in vtt


async def test_retranscribe_replaces_and_bumps_version(env: dict) -> None:
    await _run_transcribe(env)
    await _run_transcribe(env)
    async with env["factory"]() as db:
        transcript = (await db.execute(select(Transcript))).scalar_one()
        assert transcript.version == 2
        assert len((await db.execute(select(TranscriptSegment))).scalars().all()) == 3


async def test_search_returns_timestamped_hits(env: dict) -> None:
    await _run_transcribe(env)

    from framefound.main import create_app

    app = create_app()

    async def override() -> AsyncIterator[AsyncSession]:
        async with env["factory"]() as session:
            yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})

        resp = await client.get("/api/v1/search", params={"q": "starting bid"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["transcript_hits"]) == 1
        hit = body["transcript_hits"][0]
        assert hit["start_ms"] == 4200
        assert "starting bid" in hit["text"]

        # Filename search side of the same endpoint.
        resp = await client.get("/api/v1/search", params={"q": "sermon"})
        assert len(resp.json()["filename_hits"]) == 1

        # Transcript endpoint.
        resp = await client.get(f"/api/v1/assets/{env['asset_id']}/transcript")
        assert resp.status_code == 200
        assert resp.json()["segment_count"] == 3


def test_vtt_formatting() -> None:
    vtt = build_vtt([SpeechSegment(3661.5, 3663.25, "Hello there.")])
    assert "01:01:01.500 --> 01:01:03.250" in vtt
    assert vtt.endswith("Hello there.\n") or "Hello there." in vtt


def test_transcribe_task_routed_to_transcribe_queue() -> None:
    assert task_module.transcribe_asset.queue == "transcribe"
