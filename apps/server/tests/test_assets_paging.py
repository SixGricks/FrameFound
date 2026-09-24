"""Paging through the catalogue must visit every asset exactly once.

The scanner commits 500 assets per transaction and Postgres's now() is fixed
per transaction, so "recent" order has runs of 500 identical keys. Offset
paging over ties is free to repeat or skip rows at every page boundary that
lands inside a run — Browse showed some photographs twice and others never.
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
from framefound.db.models import Asset, Library

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
SAME_INSTANT = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


@pytest.fixture()
async def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'paging.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "paging-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override
    async with factory() as db:
        library = Library(name="L", root_path=str(tmp_path))
        db.add(library)
        await db.flush()
        for i in range(23):
            db.add(
                Asset(
                    id=uuid.uuid4(),
                    library_id=library.id,
                    relative_path=f"batch/{i:02d}.jpg",
                    filename=f"{i:02d}.jpg",
                    extension="jpg",
                    media_type="image",
                    size_bytes=1000,  # ties for "size" too
                    mtime=SAME_INSTANT,
                    first_indexed_at=SAME_INSTANT,
                )
            )
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        assert (
            await http.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield http
    await engine.dispose()
    get_settings.cache_clear()


@pytest.mark.parametrize("sort", ["recent", "size", "captured"])
async def test_every_asset_appears_exactly_once_across_pages(
    client: AsyncClient, sort: str
) -> None:
    seen: list[str] = []
    for page in range(1, 6):
        body = (
            await client.get("/api/v1/assets", params={"sort": sort, "page": page, "page_size": 5})
        ).json()
        seen.extend(item["id"] for item in body["items"])
    assert len(seen) == 23
    assert len(set(seen)) == 23, "no asset repeated, none skipped"
