"""Two-factor enrolment, login enforcement, recovery codes, and sealing."""

from collections.abc import AsyncIterator
from pathlib import Path

import pyotp
import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.auth import totp
from framefound.auth.crypto import SecretUnavailable, seal, unseal
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}


@pytest.fixture()
async def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "totp-test-secret-key")
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
        resp = await c.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        assert resp.status_code == 201
        yield c
    await engine.dispose()
    get_settings.cache_clear()


async def _enable_totp(client: AsyncClient) -> tuple[str, list[str]]:
    start = await client.post("/api/v1/auth/totp/start", json={"password": ADMIN["password"]})
    assert start.status_code == 200, start.text
    secret = start.json()["secret"]
    code = pyotp.TOTP(secret).now()
    confirm = await client.post("/api/v1/auth/totp/confirm", json={"code": code})
    assert confirm.status_code == 200, confirm.text
    return secret, confirm.json()["recovery_codes"]


async def test_enrolment_requires_password(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/auth/totp/start", json={"password": "wrong"})
    assert resp.status_code == 403


async def test_totp_inactive_until_confirmed(client: AsyncClient) -> None:
    await client.post("/api/v1/auth/totp/start", json={"password": ADMIN["password"]})
    assert (await client.get("/api/v1/auth/me")).json()["totp_enabled"] is False
    # A password-only login still works while enrolment is pending.
    await client.post("/api/v1/auth/logout")
    assert (await client.post("/api/v1/auth/login", json=ADMIN)).status_code == 200


async def test_confirm_rejects_wrong_code(client: AsyncClient) -> None:
    await client.post("/api/v1/auth/totp/start", json={"password": ADMIN["password"]})
    resp = await client.post("/api/v1/auth/totp/confirm", json={"code": "000000"})
    assert resp.status_code == 400


async def test_login_requires_second_factor_once_enabled(client: AsyncClient) -> None:
    secret, _ = await _enable_totp(client)
    assert (await client.get("/api/v1/auth/me")).json()["totp_enabled"] is True
    await client.post("/api/v1/auth/logout")

    without = await client.post("/api/v1/auth/login", json=ADMIN)
    assert without.status_code == 401
    assert without.headers.get("X-FrameFound-Auth") == "totp_required"

    wrong = await client.post("/api/v1/auth/login", json={**ADMIN, "totp_code": "123456"})
    assert wrong.status_code == 401

    good = await client.post(
        "/api/v1/auth/login", json={**ADMIN, "totp_code": pyotp.TOTP(secret).now()}
    )
    assert good.status_code == 200


async def test_recovery_code_works_once(client: AsyncClient) -> None:
    _, codes = await _enable_totp(client)
    await client.post("/api/v1/auth/logout")

    first = await client.post("/api/v1/auth/login", json={**ADMIN, "totp_code": codes[0]})
    assert first.status_code == 200
    await client.post("/api/v1/auth/logout")

    # Single use: the same code must not work again.
    replay = await client.post("/api/v1/auth/login", json={**ADMIN, "totp_code": codes[0]})
    assert replay.status_code == 401


async def test_disable_requires_password_and_code(client: AsyncClient) -> None:
    secret, _ = await _enable_totp(client)
    bad = await client.post(
        "/api/v1/auth/totp/disable", json={"password": "wrong", "code": pyotp.TOTP(secret).now()}
    )
    assert bad.status_code == 403
    good = await client.post(
        "/api/v1/auth/totp/disable",
        json={"password": ADMIN["password"], "code": pyotp.TOTP(secret).now()},
    )
    assert good.status_code == 204
    assert (await client.get("/api/v1/auth/me")).json()["totp_enabled"] is False


async def test_sessions_listed_and_revocable(client: AsyncClient) -> None:
    sessions = (await client.get("/api/v1/auth/sessions")).json()
    assert len(sessions) == 1
    assert sessions[0]["current"] is True

    revoked = await client.post("/api/v1/auth/sessions/revoke-others")
    assert revoked.json()["revoked"] == 0  # only the current one exists
    # The current session must survive "revoke others".
    assert (await client.get("/api/v1/auth/me")).status_code == 200


def test_seal_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "key-one")
    get_settings.cache_clear()
    sealed = seal("super-secret-seed")
    assert "super-secret-seed" not in sealed
    assert unseal(sealed) == "super-secret-seed"
    get_settings.cache_clear()


def test_seal_unreadable_after_key_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "key-one")
    get_settings.cache_clear()
    sealed = seal("seed")
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "key-two")
    get_settings.cache_clear()
    with pytest.raises(SecretUnavailable):
        unseal(sealed)
    get_settings.cache_clear()


def test_totp_verify_rejects_malformed() -> None:
    secret = totp.new_secret()
    assert not totp.verify(secret, "abc")
    assert not totp.verify(secret, "12345")
    assert totp.verify(secret, pyotp.TOTP(secret).now())
