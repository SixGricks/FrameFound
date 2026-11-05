"""Showcase limited to a capture-date window: "the courses we shot in October".

The ranking itself is tested in test_showcase.py. What matters here is which
photographs reach it, and which places are offered, once a window is set.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Frame, Library

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}

SHOTS = {
    # place folder, file, capture time (None = the camera recorded none)
    "ledgerock-sep": ("Ledgerock", "sep.jpg", datetime(2026, 9, 30, 21, 0, tzinfo=UTC)),
    "ledgerock-oct": ("Ledgerock", "oct.jpg", datetime(2026, 10, 1, 0, 30, tzinfo=UTC)),
    "stonewall-oct": ("Stonewall", "oct.jpg", datetime(2026, 10, 31, 23, 59, tzinfo=UTC)),
    "stonewall-nov": ("Stonewall", "nov.jpg", datetime(2026, 11, 1, 0, 0, tzinfo=UTC)),
    "forest-undated": ("Forest Gate", "undated.jpg", None),
}


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'sw.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "showcase-window-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    # Rank is replaced by a recorder: which photographs reached it.
    seen: list[set[str]] = []

    def record(photos, vectors, **kwargs):  # type: ignore[no-untyped-def]
        seen.append({photo.relative_path for photo in photos})
        return [], len(photos)

    from framefound.api.v1 import showcase as showcase_api

    monkeypatch.setattr(showcase_api.showcase_lib, "rank", record)
    monkeypatch.setattr(showcase_api, "_prompt_vectors", lambda *args: {})

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override
    async with factory() as db:
        library = Library(name="GELCO", root_path=str(tmp_path))
        db.add(library)
        await db.flush()
        for folder, name, when in SHOTS.values():
            asset = Asset(
                library_id=library.id,
                relative_path=f"{folder}/{name}",
                filename=name,
                extension="jpg",
                media_type="image",
                size_bytes=1,
                mtime=datetime(2026, 11, 1, tzinfo=UTC),
                captured_at=when,
                width=6000,
                height=4000,
            )
            db.add(asset)
            await db.flush()
            db.add(
                Frame(
                    asset_id=asset.id,
                    ts_ms=0,
                    relative_path=f"frames/{uuid.uuid4()}.jpg",
                    embedding=[1.0, 0.0, 0.0],
                )
            )
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"client": client, "seen": seen}
    await engine.dispose()
    get_settings.cache_clear()


async def test_without_a_window_everything_is_considered(env: dict) -> None:
    resp = await env["client"].post("/api/v1/showcase", json={})
    assert resp.status_code == 200
    assert len(env["seen"][-1]) == len(SHOTS)


async def test_a_window_keeps_whole_days_at_both_ends(env: dict) -> None:
    resp = await env["client"].post(
        "/api/v1/showcase", json={"captured_from": "2026-10-01", "captured_to": "2026-10-31"}
    )
    assert resp.status_code == 200
    assert env["seen"][-1] == {"Ledgerock/oct.jpg", "Stonewall/oct.jpg"}, (
        "the first minute of the first day and the last minute of the last are in; "
        "the evening before and the midnight after are out; undated photographs are out"
    )


async def test_one_end_of_the_window_is_enough(env: dict) -> None:
    await env["client"].post("/api/v1/showcase", json={"captured_from": "2026-11-01"})
    assert env["seen"][-1] == {"Stonewall/nov.jpg"}


async def test_an_empty_window_says_so(env: dict) -> None:
    resp = await env["client"].post(
        "/api/v1/showcase", json={"captured_from": "2025-01-01", "captured_to": "2025-01-31"}
    )
    assert resp.status_code == 404
    assert "window" in resp.json()["error"]["message"]


async def test_a_backwards_window_is_refused(env: dict) -> None:
    resp = await env["client"].post(
        "/api/v1/showcase", json={"captured_from": "2026-10-31", "captured_to": "2026-10-01"}
    )
    assert resp.status_code == 422


async def test_the_place_list_narrows_to_the_window(env: dict) -> None:
    every = (await env["client"].get("/api/v1/showcase/places")).json()
    october = (
        await env["client"].get(
            "/api/v1/showcase/places",
            params={"captured_from": "2026-10-01", "captured_to": "2026-10-31"},
        )
    ).json()
    assert {p["label"] for p in every} == {"Ledgerock", "Stonewall", "Forest Gate"}
    assert {(p["label"], p["photos"]) for p in october} == {("Ledgerock", 1), ("Stonewall", 1)}
