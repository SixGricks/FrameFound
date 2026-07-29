"""Remote-access config, DDNS safeguards, and the public-access gate."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.api.v1.remote_access import classify_client
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.ddns.providers import CloudflareProvider, DnsError, looks_like_global_key

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}


@pytest.fixture()
async def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "remote-test-secret")
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (
            await c.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield c
    await engine.dispose()
    get_settings.cache_clear()


def test_client_classification() -> None:
    assert classify_client("127.0.0.1") == "local"
    assert classify_client("192.168.1.50") == "lan"
    assert classify_client("10.0.0.8") == "lan"
    assert classify_client("100.101.102.103") == "tailnet"  # CGNAT / Tailscale
    assert classify_client("8.8.8.8") == "internet"
    assert classify_client(None) == "unknown"
    assert classify_client("not-an-ip") == "unknown"


def test_global_api_key_detection() -> None:
    assert looks_like_global_key("a" * 37)
    assert not looks_like_global_key("a" * 40)
    assert not looks_like_global_key("scoped-token_with-dashes")


def test_cloudflare_refuses_global_key() -> None:
    with pytest.raises(DnsError, match="scoped API token"):
        CloudflareProvider("0" * 37, "example.com")


async def test_defaults_are_local_and_private(client: AsyncClient) -> None:
    body = (await client.get("/api/v1/remote-access")).json()
    assert body["mode"] == "local"
    assert body["public_access_enabled"] is False
    assert body["ddns_configured"] is False


async def test_token_is_write_only(client: AsyncClient) -> None:
    resp = await client.put(
        "/api/v1/remote-access",
        json={
            "mode": "domain",
            "domain": "media.example.com",
            "ddns_provider": "cloudflare",
            "ddns_zone": "example.com",
            "ddns_record": "media.example.com",
            "ddns_token": "super-secret-scoped-token",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ddns_configured"] is True
    # The token must never come back out of the API.
    assert "super-secret-scoped-token" not in resp.text
    assert "ddns_token" not in body


async def test_stored_token_is_encrypted_at_rest(client: AsyncClient, tmp_path: Path) -> None:
    await client.put(
        "/api/v1/remote-access",
        json={
            "ddns_provider": "cloudflare",
            "ddns_zone": "example.com",
            "ddns_token": "plaintext-token-value",
        },
    )
    raw = (tmp_path / "test.db").read_bytes()
    assert b"plaintext-token-value" not in raw


async def test_kill_switch_disables_public_access(client: AsyncClient) -> None:
    await client.put(
        "/api/v1/remote-access", json={"mode": "domain", "public_access_enabled": True}
    )
    assert (await client.get("/api/v1/remote-access")).json()["public_access_enabled"] is True

    killed = await client.post("/api/v1/remote-access/disable-public")
    assert killed.status_code == 200
    assert killed.json()["public_access_enabled"] is False
    assert killed.json()["mode"] == "local"


async def test_test_dns_requires_configuration(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/remote-access/test-dns")
    assert resp.status_code == 400


async def test_health_endpoints_stay_open(client: AsyncClient) -> None:
    # Container health checks must work regardless of access mode.
    assert (await client.get("/healthz")).status_code == 200
