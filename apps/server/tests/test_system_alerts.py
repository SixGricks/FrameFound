"""System health and the alerts banner.

The NAS was unmounted for 39 days (Aug-Sep 2026) and nothing said so: the
health check measured the VM's own disk under the library's name, the
database volume was listed "unreachable" on every load (so the page read as
noise), and nothing that did know was shown anywhere the operator looked.
These tests pin the three things that would have caught it.
"""

import os
import time
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


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'sys.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "sys-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
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

    healthy_root = tmp_path / "healthy"
    healthy_root.mkdir()
    (healthy_root / "a.jpg").write_bytes(b"x")
    unmounted_root = tmp_path / "gelco"
    unmounted_root.mkdir()  # the mountpoint, with nothing mounted on it
    async with factory() as db:
        for name, root in (("Intel 2026", healthy_root), ("GELCO", unmounted_root)):
            library = Library(name=name, root_path=str(root))
            db.add(library)
            await db.flush()
            db.add(
                Asset(
                    library_id=library.id,
                    relative_path=f"{uuid.uuid4().hex}.jpg",
                    filename="a.jpg",
                    extension="jpg",
                    media_type="image",
                    size_bytes=1,
                    mtime=datetime.now(UTC),
                )
            )
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"client": client, "data": tmp_path / "data", "app": app}
    await engine.dispose()
    get_settings.cache_clear()


async def test_an_empty_mountpoint_is_reported_unreachable(env: dict) -> None:
    health = (await env["client"].get("/api/v1/system/health")).json()
    unreachable = {v["label"]: v for v in health["volumes"] if v["status"] == "unreachable"}
    # Healthy volumes on one disk are merged into one entry (by design), so
    # the property is about what is flagged: GELCO, and nothing else.
    assert list(unreachable) == ["GELCO"]
    assert "not mounted" in unreachable["GELCO"]["detail"]


async def test_no_permanent_false_alarm_for_the_database(env: dict) -> None:
    health = (await env["client"].get("/api/v1/system/health")).json()
    labels = [v["label"] for v in health["volumes"]]
    assert "Database" not in labels, "its volume is not mounted here; that is not an outage"
    assert all(v["status"] != "unreachable" for v in health["volumes"] if v["label"] != "GELCO")


async def test_the_banner_names_the_unreachable_library(env: dict) -> None:
    alerts = (await env["client"].get("/api/v1/system/alerts")).json()
    errors = [a for a in alerts if a["level"] == "error"]
    assert len(errors) == 1
    assert errors[0]["title"] == "GELCO is unreachable"
    assert errors[0]["href"] == "/health"


async def test_a_missing_backup_is_raised_and_a_fresh_one_clears_it(env: dict) -> None:
    alerts = (await env["client"].get("/api/v1/system/alerts")).json()
    assert any(a["title"] == "No backup yet" for a in alerts)
    health = (await env["client"].get("/api/v1/system/health")).json()
    assert health["backup"]["status"] == "missing"

    backups = env["data"] / "backups"
    backups.mkdir()
    archive = backups / "framefound-20260924T070000Z.tar.gz"
    archive.write_bytes(b"archive")
    health = (await env["client"].get("/api/v1/system/health")).json()
    assert health["backup"]["status"] == "ok"
    alerts = (await env["client"].get("/api/v1/system/alerts")).json()
    assert not any("backup" in a["title"].lower() for a in alerts)

    two_days_ago = time.time() - 48 * 3600
    os.utime(archive, (two_days_ago, two_days_ago))
    health = (await env["client"].get("/api/v1/system/health")).json()
    assert health["backup"]["status"] == "stale"


async def test_alerts_need_a_session(env: dict) -> None:
    async with AsyncClient(transport=ASGITransport(app=env["app"]), base_url="http://t") as anon:
        assert (await anon.get("/api/v1/system/alerts")).status_code == 401
