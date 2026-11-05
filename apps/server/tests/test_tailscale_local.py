"""The Security page's Tailscale button, against a fake tailscaled.

What is pinned: the page can tell every state apart, a sign-in link comes
back from one press, a missing operator permission is explained rather than
reported as a bare 403, and only an admin may ask for a link.
"""

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.ddns import tailscale_local

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}

RUNNING = {
    "BackendState": "Running",
    "Version": "1.104.5",
    "Self": {
        "HostName": "framefound",
        "DNSName": "framefound.tail1234.ts.net.",
        "TailscaleIPs": ["100.101.102.103", "fd7a:115c:a1e0::1"],
    },
    "CurrentTailnet": {"Name": "dj@example.com"},
}


class FakeDaemon:
    """tailscaled's LocalAPI, as far as FrameFound uses it."""

    def __init__(self, state: str = "NeedsLogin", operator: bool = True) -> None:
        self.state = state
        self.auth_url = ""
        self.operator = operator
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        assert request.url.host == "local-tailscaled.sock", "the daemon refuses other hosts"
        if request.method == "GET" and request.url.path.endswith("/status"):
            if self.state == "Running":
                return httpx.Response(200, json=RUNNING)
            return httpx.Response(
                200,
                json={
                    "BackendState": self.state,
                    "AuthURL": self.auth_url,
                    "Self": {"HostName": "framefound"},
                },
            )
        if not self.operator:
            return httpx.Response(403, text="access denied")
        if request.url.path.endswith("/prefs"):
            if self.state == "Stopped":
                self.state = "Running"
            return httpx.Response(200, json={})
        if request.url.path.endswith("/login-interactive"):
            self.auth_url = "https://login.tailscale.com/a/abc123"
            return httpx.Response(204)
        return httpx.Response(404)


@pytest.fixture()
def daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., FakeDaemon]:
    def install(**kwargs: Any) -> FakeDaemon:
        fake = FakeDaemon(**kwargs)
        socket = tmp_path / "tailscaled.sock"
        socket.touch()
        monkeypatch.setattr(tailscale_local, "SOCKET", socket)
        monkeypatch.setattr(tailscale_local, "LOGIN_POLL_S", 0.01)
        monkeypatch.setattr(
            tailscale_local,
            "_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )
        return fake

    return install


async def test_no_socket_means_not_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tailscale_local, "SOCKET", tmp_path / "missing.sock")
    assert (await tailscale_local.status()).state == tailscale_local.NOT_INSTALLED


async def test_a_running_host_reports_its_names(daemon: Callable[..., FakeDaemon]) -> None:
    daemon(state="Running")
    found = await tailscale_local.status()
    assert found.state == "connected"
    assert found.dns_name == "framefound.tail1234.ts.net", "the trailing dot is dropped"
    assert found.host_name == "framefound" and found.tailnet == "dj@example.com"
    assert found.ips[0] == "100.101.102.103"


async def test_one_press_produces_a_sign_in_link(daemon: Callable[..., FakeDaemon]) -> None:
    fake = daemon(state="NeedsLogin")
    found = await tailscale_local.start_login()
    assert found.state == "needs_login"
    assert found.login_url == "https://login.tailscale.com/a/abc123"
    assert "POST /localapi/v0/login-interactive" in fake.calls


async def test_a_waiting_link_is_reused_not_replaced(daemon: Callable[..., FakeDaemon]) -> None:
    fake = daemon(state="NeedsLogin")
    fake.auth_url = "https://login.tailscale.com/a/already"
    found = await tailscale_local.start_login()
    assert found.login_url.endswith("/already")
    assert not any(call.startswith("POST") for call in fake.calls)


async def test_a_stopped_host_is_turned_back_on_without_a_sign_in(
    daemon: Callable[..., FakeDaemon],
) -> None:
    fake = daemon(state="Stopped")
    found = await tailscale_local.start_login()
    assert found.state == "connected"
    assert "POST /localapi/v0/login-interactive" not in fake.calls


async def test_a_missing_operator_is_explained(daemon: Callable[..., FakeDaemon]) -> None:
    daemon(state="NeedsLogin", operator=False)
    with pytest.raises(tailscale_local.TailscaleUnavailable, match="--operator"):
        await tailscale_local.start_login()


# --- over HTTP ------------------------------------------------------------


@pytest.fixture()
async def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'ts.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "ts-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    monkeypatch.setenv("FRAMEFOUND_HTTP_PORT", "8080")
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        assert (
            await http.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield http
    await engine.dispose()
    get_settings.cache_clear()


async def test_the_page_gets_bookmarkable_addresses(
    client: AsyncClient, daemon: Callable[..., FakeDaemon]
) -> None:
    daemon(state="Running")
    body = (await client.get("/api/v1/remote-access/tailscale")).json()
    assert body["state"] == "connected"
    assert body["url"] == "http://framefound.tail1234.ts.net:8080"
    assert body["short_url"] == "http://framefound:8080"
    assert body["mcp_url"] == "http://framefound.tail1234.ts.net:8080/api/mcp"


async def test_the_button_returns_the_link(
    client: AsyncClient, daemon: Callable[..., FakeDaemon]
) -> None:
    daemon(state="NeedsLogin")
    resp = await client.post("/api/v1/remote-access/tailscale/login")
    assert resp.status_code == 200
    assert resp.json()["login_url"] == "https://login.tailscale.com/a/abc123"


async def test_a_refusal_reaches_the_page_as_a_sentence(
    client: AsyncClient, daemon: Callable[..., FakeDaemon]
) -> None:
    daemon(state="NeedsLogin", operator=False)
    resp = await client.post("/api/v1/remote-access/tailscale/login")
    assert resp.status_code == 409
    assert "--operator" in resp.json()["error"]["message"]


async def test_signing_in_needs_a_session(
    client: AsyncClient, daemon: Callable[..., FakeDaemon]
) -> None:
    daemon(state="NeedsLogin")
    await client.post("/api/v1/auth/logout")
    assert (await client.post("/api/v1/remote-access/tailscale/login")).status_code == 401
