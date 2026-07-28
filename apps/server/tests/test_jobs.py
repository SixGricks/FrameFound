"""Job-history recording by the task shell, and the processing report.

File-backed SQLite + FRAMEFOUND_DATABASE_URL: the task shell builds its own
engine from settings exactly as in production — no monkeypatching.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Job, Library
from framefound.processing import tasks as task_module

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "jobs-test-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", db_url)
    get_settings.cache_clear()

    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    lib_root = tmp_path / "lib"
    lib_root.mkdir()
    async with factory() as db:
        library = Library(name="L", root_path=str(lib_root))
        db.add(library)
        await db.flush()
        asset = Asset(
            library_id=library.id,
            relative_path="target.jpg",  # file absent until a test creates it
            filename="target.jpg",
            extension="jpg",
            media_type="image",
            size_bytes=10,
            mtime=datetime.now(UTC),
        )
        db.add(asset)
        await db.commit()
        asset_id = asset.id

    yield {"factory": factory, "asset_id": asset_id, "tmp": tmp_path}
    await engine.dispose()
    get_settings.cache_clear()


async def test_shell_records_skip_for_missing_file(env: dict) -> None:
    async def handler(db: AsyncSession, asset, library, path) -> None:  # pragma: no cover
        raise AssertionError("handler must not run for a missing file")

    await task_module._with_asset("unit_test_task", env["asset_id"], handler)

    async with env["factory"]() as db:
        job = (await db.execute(select(Job))).scalar_one()
        assert job.task_name == "unit_test_task"
        assert job.status == "skipped"
        assert job.finished_at is not None
        asset = await db.get(Asset, env["asset_id"])
        assert asset.availability == "missing"


async def test_shell_records_success_and_failure(env: dict) -> None:
    (env["tmp"] / "lib" / "target.jpg").write_bytes(b"data")

    async def ok(db: AsyncSession, asset, library, path) -> None:
        return None

    async def boom(db: AsyncSession, asset, library, path) -> None:
        raise RuntimeError("synthetic failure")

    await task_module._with_asset("ok_task", env["asset_id"], ok)
    with pytest.raises(RuntimeError):
        await task_module._with_asset("boom_task", env["asset_id"], boom)

    async with env["factory"]() as db:
        jobs = {j.task_name: j for j in (await db.execute(select(Job))).scalars().all()}
    assert jobs["ok_task"].status == "succeeded"
    assert jobs["boom_task"].status == "failed"
    assert "synthetic failure" in (jobs["boom_task"].error or "")


async def test_processing_report_endpoint(env: dict) -> None:
    from framefound.main import create_app

    app = create_app()

    async def override() -> AsyncIterator[AsyncSession]:
        async with env["factory"]() as session:
            yield session

    app.dependency_overrides[get_session] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
        )
        assert resp.status_code == 201
        report = await client.get("/api/v1/system/processing")
        assert report.status_code == 200
        body = report.json()
        assert "queue_depths" in body
        assert body["assets_by_status"].get("pending") == 1
        assert isinstance(body["recent_failures"], list)


def test_tasks_routed_to_latency_queues() -> None:
    assert task_module.extract_metadata.queue == "metadata"
    assert task_module.generate_derivatives.queue == "visuals"
    assert task_module.generate_proxy.queue == "media"
