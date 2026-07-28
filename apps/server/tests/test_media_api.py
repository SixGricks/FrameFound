"""Media endpoint authorization: session, signed URLs, and denial paths.

Runs everything in one event loop (httpx ASGITransport) so the SQLite
StaticPool connection stays loop-consistent while we seed rows directly.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Derivative, Library
from framefound.media.signing import sign_media_url

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> AsyncIterator[dict]:
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "media-test-secret")
    get_settings.cache_clear()

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override

    # Seed: one asset with one ready thumbnail derivative on disk.
    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    asset_id = uuid.uuid4()
    rel = f"derivatives/{str(asset_id)[:2]}/{asset_id}/thumbnail.webp"
    thumb_abs = tmp_path / "data" / rel
    thumb_abs.parent.mkdir(parents=True)
    thumb_abs.write_bytes(b"webp-bytes-here")
    async with factory() as db:
        library = Library(name="L", root_path=str(lib_root))
        db.add(library)
        await db.flush()
        db.add(
            Asset(
                id=asset_id,
                library_id=library.id,
                relative_path="a.jpg",
                filename="a.jpg",
                extension="jpg",
                media_type="image",
                size_bytes=10,
                mtime=datetime.now(UTC),
            )
        )
        db.add(
            Derivative(
                asset_id=asset_id,
                kind="thumbnail",
                relative_path=rel,
                media_format="webp",
                status="ready",
            )
        )
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"client": client, "asset_id": asset_id}

    await engine.dispose()
    get_settings.cache_clear()


async def _sign_in(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
    assert resp.status_code == 201, resp.text


async def test_media_requires_auth(env: dict) -> None:
    resp = await env["client"].get(f"/api/v1/media/{env['asset_id']}/thumbnail")
    assert resp.status_code == 401


async def test_media_with_session(env: dict) -> None:
    client = env["client"]
    await _sign_in(client)
    resp = await client.get(f"/api/v1/media/{env['asset_id']}/thumbnail")
    assert resp.status_code == 200
    assert resp.content == b"webp-bytes-here"
    assert resp.headers["content-type"] == "image/webp"


async def test_signed_url_without_session(env: dict) -> None:
    client = env["client"]
    await _sign_in(client)
    urls = (await client.get(f"/api/v1/assets/{env['asset_id']}/urls")).json()["urls"]
    assert "thumbnail" in urls
    client.cookies.clear()  # prove no session is needed
    resp = await client.get(urls["thumbnail"])
    assert resp.status_code == 200
    assert resp.content == b"webp-bytes-here"


async def test_tampered_signature_rejected(env: dict) -> None:
    client = env["client"]
    expires, sig = sign_media_url("wrong-secret", env["asset_id"], "thumbnail")
    resp = await client.get(f"/api/v1/media/{env['asset_id']}/thumbnail?exp={expires}&sig={sig}")
    assert resp.status_code == 403


async def test_missing_derivative_404(env: dict) -> None:
    client = env["client"]
    await _sign_in(client)
    resp = await client.get(f"/api/v1/media/{env['asset_id']}/proxy")
    assert resp.status_code == 404
