"""Test fixtures: in-memory SQLite database, app with overridden session.

The schema is created lazily inside the first request's event loop —
aiosqlite connections are bound to the loop that created them, so creating
tables on a separate loop would break under TestClient.
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from framefound.auth.router import login_limiter
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session

TEST_SETUP_TOKEN = "test-setup-token-123"


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FastAPI]:
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    login_limiter._buckets.clear()  # module-level limiter persists across tests

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # one shared in-memory DB across connections
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    schema_ready = False

    async def override_session() -> AsyncIterator[AsyncSession]:
        nonlocal schema_ready
        if not schema_ready:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            schema_ready = True
        async with factory() as session:
            yield session

    from framefound.main import create_app

    application = create_app()
    application.dependency_overrides[get_session] = override_session
    yield application
    get_settings.cache_clear()


@pytest.fixture()
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
