"""Duplicate grouping and the reclaimable-space arithmetic.

The arithmetic matters more than it looks: an operator decides what to delete
from these numbers, so an overstated saving is a data-loss hazard.
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
MB = 1024 * 1024


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "dupe-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override

    lib_root = tmp_path / "lib"
    lib_root.mkdir()

    async def add(
        name: str, size: int, phash: str | None = None, partial: str | None = None
    ) -> uuid.UUID:
        async with factory() as db:
            library = (await db.execute(__import__("sqlalchemy").select(Library))).scalars().first()
            if library is None:
                library = Library(name="L", root_path=str(lib_root))
                db.add(library)
                await db.flush()
            asset = Asset(
                library_id=library.id,
                relative_path=name,
                filename=name,
                extension="mp4",
                media_type="video",
                size_bytes=size,
                mtime=datetime.now(UTC),
                partial_hash=partial,
                availability="online",
            )
            db.add(asset)
            await db.flush()
            if phash:
                db.add(
                    Frame(asset_id=asset.id, ts_ms=0, relative_path=f"f/{name}.jpeg", phash=phash)
                )
            await db.commit()
            return asset.id

    # Three byte-identical copies at 100 MB, plus an unrelated file.
    for folder in ("edit/render.mp4", "backup/render.mp4", "archive/render.mp4"):
        await add(folder, 100 * MB, partial="aaaa")
    await add("unique.mp4", 40 * MB, partial="bbbb")
    # A master and its smaller export: different bytes, same picture.
    await add("master.mp4", 500 * MB, phash="ffff0000ffff0000", partial="cccc")
    await add("export.mp4", 80 * MB, phash="ffff0000ffff0000", partial="dddd")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"client": client}
    await engine.dispose()
    get_settings.cache_clear()


async def test_identical_copies_are_grouped(env: dict) -> None:
    body = (await env["client"].get("/api/v1/duplicates")).json()
    group = next(g for g in body["groups"] if g["count"] == 3)
    assert group["kind"] == "identical"
    assert group["size_bytes"] == 100 * MB
    # Three copies, keep one: two are reclaimable, not three.
    assert group["reclaimable_bytes"] == 200 * MB
    assert {m["relative_path"] for m in group["members"]} == {
        "edit/render.mp4",
        "backup/render.mp4",
        "archive/render.mp4",
    }


async def test_unique_files_are_never_reported(env: dict) -> None:
    body = (await env["client"].get("/api/v1/duplicates")).json()
    paths = {m["relative_path"] for g in body["groups"] for m in g["members"]}
    assert "unique.mp4" not in paths


async def test_min_size_filter_excludes_small_files(env: dict) -> None:
    body = (await env["client"].get("/api/v1/duplicates", params={"min_size_mb": 200})).json()
    assert body["groups"] == []


async def test_similar_mode_finds_master_and_export(env: dict) -> None:
    body = (await env["client"].get("/api/v1/duplicates", params={"kind": "similar"})).json()
    assert body["total_groups"] == 1
    group = body["groups"][0]
    assert group["kind"] == "similar"
    # Keeping the largest (the master) means only the export is reclaimable.
    assert group["reclaimable_bytes"] == 80 * MB


async def test_totals_match_the_groups(env: dict) -> None:
    body = (await env["client"].get("/api/v1/duplicates")).json()
    assert body["total_reclaimable_bytes"] == sum(g["reclaimable_bytes"] for g in body["groups"])


async def test_verification_reports_unverified_members(env: dict) -> None:
    body = (await env["client"].get("/api/v1/duplicates")).json()
    group = next(g for g in body["groups"] if g["count"] == 3)
    # Nothing has a full-content hash yet, so the UI must be able to say so.
    assert all(m["content_hash_verified"] is False for m in group["members"])
