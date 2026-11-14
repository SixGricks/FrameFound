"""A slideshow render the queue would not take leaves nothing behind.

Found in final QA: with the queue down, each press of Render answered "could
not be queued" and still left a slideshow row marked queued that no worker
would ever pick up. After a reload the page listed one ghost per press."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Library, Slideshow
from framefound.processing import tasks

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}


@pytest.fixture()
async def site(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'catalogue.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "slideshow-queue-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        library = Library(name="Photos", root_path=str(tmp_path / "photos"))
        db.add(library)
        await db.flush()
        photos = [
            Asset(
                library_id=library.id,
                relative_path=f"IMG_000{n}.jpg",
                filename=f"IMG_000{n}.jpg",
                extension="jpg",
                media_type="image",
                size_bytes=1000,
                mtime=datetime(2026, 11, 1, tzinfo=UTC),
            )
            for n in range(1, 4)
        ]
        db.add_all(photos)
        await db.commit()

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        setup = await client.post(
            "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
        )
        assert setup.status_code == 201
        yield {"client": client, "factory": factory, "photos": [str(p.id) for p in photos]}
    await engine.dispose()
    get_settings.cache_clear()


def _queue_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(_slideshow_id: str) -> None:
        raise ConnectionError("redis is not running")

    monkeypatch.setattr(tasks.render_slideshow, "delay", down)


async def _shows(site: dict[str, Any]) -> list[Slideshow]:
    async with site["factory"]() as db:
        return list((await db.execute(select(Slideshow))).scalars().all())


async def test_a_render_the_queue_refuses_creates_no_slideshow(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _queue_down(monkeypatch)
    body = {"title": "Open house", "asset_ids": site["photos"]}

    for _press in range(2):
        refused = await site["client"].post("/api/v1/slideshows", json=body)
        assert refused.status_code == 503
        assert refused.json()["error"]["message"] == "The processing queue is unavailable"

    assert await _shows(site) == []
    listed = await site["client"].get("/api/v1/slideshows")
    assert listed.json() == []


async def test_a_render_the_queue_takes_is_stored_and_handed_to_the_worker(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    handed: list[str] = []
    monkeypatch.setattr(tasks.render_slideshow, "delay", handed.append)

    made = await site["client"].post(
        "/api/v1/slideshows", json={"title": "Open house", "asset_ids": site["photos"]}
    )

    assert made.status_code == 202, made.text
    shows = await _shows(site)
    assert [(str(s.id), s.status) for s in shows] == [(made.json()["id"], "pending")]
    assert handed == [made.json()["id"]]


async def test_a_refused_re_render_leaves_the_slideshow_as_it_was(
    site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finished video stays finished, and a failed one keeps its reason,
    rather than both turning into a render that never comes."""
    async with site["factory"]() as db:
        ready = Slideshow(title="Done", status="ready", asset_ids=site["photos"], segments_done=3)
        failed = Slideshow(
            title="Broke", status="failed", asset_ids=site["photos"], error="ffmpeg stopped"
        )
        db.add_all([ready, failed])
        await db.commit()
    _queue_down(monkeypatch)

    for show in (ready, failed):
        refused = await site["client"].post(f"/api/v1/slideshows/{show.id}/render")
        assert refused.status_code == 503

    after = {s.title: (s.status, s.error, s.segments_done) for s in await _shows(site)}
    assert after == {"Done": ("ready", None, 3), "Broke": ("failed", "ffmpeg stopped", 0)}
