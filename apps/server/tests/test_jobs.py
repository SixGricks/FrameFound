"""Job-history recording by the task shell, and the processing report.

File-backed SQLite + FRAMEFOUND_DATABASE_URL: the task shell builds its own
engine from settings exactly as in production — no monkeypatching.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from celery.exceptions import Reject
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Job, Library
from framefound.processing import tasks as task_module
from framefound.storage import worker_roots

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
    # The library is plainly there (it has other files); this one is gone.
    (env["tmp"] / "lib" / "neighbour.jpg").write_bytes(b"still here")

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


async def test_shell_does_not_call_a_file_missing_when_the_share_is_down(env: dict) -> None:
    """An empty library folder is a failed mount, not a deletion: skip the
    work, say why, and leave the asset's availability alone."""

    async def handler(db: AsyncSession, asset, library, path) -> None:  # pragma: no cover
        raise AssertionError("handler must not run without the original")

    await task_module._with_asset("unit_test_task", env["asset_id"], handler)

    async with env["factory"]() as db:
        job = (await db.execute(select(Job))).scalar_one()
        assert job.status == "skipped"
        assert job.error is not None and "not mounted" in job.error
        asset = await db.get(Asset, env["asset_id"])
        assert asset.availability == "online"


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


def test_the_operators_own_jobs_stay_on_the_server() -> None:
    """Exports and renders have their own queue, which only the server's
    worker-media consumes: a worker on another machine takes bulk ingest
    (`media` is proxies alone) and never an export somebody is waiting on."""
    for task in (
        task_module.export_listing_zip,
        task_module.render_slideshow,
        task_module.inpaint_asset,
        task_module.ai_edit_listing,
        task_module.fetch_basemap,
        task_module.collect_training_pairs,
    ):
        assert task.queue == "exports", task.name
    media_tasks = [
        t.name
        for t in task_module.celery_app.tasks.values()
        if getattr(t, "queue", None) == "media"
    ]
    assert media_tasks == ["framefound.generate_proxy"]


# --- A worker on another machine (docs/remote-workers.md) -------------------


async def _as_remote_worker(
    env: dict, monkeypatch: pytest.MonkeyPatch, *, marker: bool = True
) -> None:
    """Store the library under a server path the test machine maps back to
    its real folder, as the Mac mini maps /media/<share> to /Volumes/<share>."""
    async with env["factory"]() as db:
        library = (await db.execute(select(Library))).scalar_one()
        library.root_path = "/media/share"
        await db.commit()
    data = env["tmp"] / "data"
    data.mkdir(exist_ok=True)
    if marker:
        (data / ".framefound-data-store").write_text("store")
    monkeypatch.setenv("FRAMEFOUND_ROOT_MAP", f"/media/share={env['tmp'] / 'lib'}")
    monkeypatch.setenv("FRAMEFOUND_WORKER_NAME", "mac-mini")
    monkeypatch.setattr(worker_roots, "HAND_BACK_PAUSE_S", 0.0)
    get_settings.cache_clear()


async def test_remote_worker_reads_through_the_root_map(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _as_remote_worker(env, monkeypatch)
    (env["tmp"] / "lib" / "target.jpg").write_bytes(b"data")
    seen: list[Path] = []

    async def handler(db: AsyncSession, asset, library, path) -> None:
        seen.append(path)

    await task_module._with_asset("ok_task", env["asset_id"], handler)

    assert seen == [(env["tmp"] / "lib" / "target.jpg").resolve()]
    async with env["factory"]() as db:
        job = (await db.execute(select(Job))).scalar_one()
    assert job.status == "succeeded"
    assert job.worker == "mac-mini"


async def test_remote_worker_never_calls_a_file_missing(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same evidence that makes the server flag a file missing — the
    library folder is there, the file is not — is only a skip on another
    machine, where it as easily means that machine's mount or mapping."""
    await _as_remote_worker(env, monkeypatch)
    (env["tmp"] / "lib" / "neighbour.jpg").write_bytes(b"still here")

    async def handler(db: AsyncSession, asset, library, path) -> None:  # pragma: no cover
        raise AssertionError("handler must not run for an unreachable file")

    await task_module._with_asset("unit_test_task", env["asset_id"], handler)

    async with env["factory"]() as db:
        job = (await db.execute(select(Job))).scalar_one()
        asset = await db.get(Asset, env["asset_id"])
    assert job.status == "skipped"
    assert "mac-mini" in (job.error or "")
    assert asset.availability == "online"


async def _handed_back(env: dict, why: str) -> Job:
    """Run the shell expecting a hand-back: the message goes back to its
    queue (Reject, requeue) and the Job row says which machine and why."""

    async def handler(db: AsyncSession, asset, library, path) -> None:  # pragma: no cover
        raise AssertionError(f"handler must not run: {why}")

    with pytest.raises(Reject) as raised:
        await task_module._with_asset("unit_test_task", env["asset_id"], handler)
    assert raised.value.requeue is True
    async with env["factory"]() as db:
        job = (await db.execute(select(Job))).scalar_one()
        asset = await db.get(Asset, env["asset_id"])
    assert job.status == "skipped"
    assert job.finished_at is not None
    assert (job.error or "").startswith("Handed back by mac-mini")
    assert asset.availability == "online"
    return job


async def test_remote_worker_hands_back_without_the_data_store(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _as_remote_worker(env, monkeypatch, marker=False)
    (env["tmp"] / "lib" / "target.jpg").write_bytes(b"data")
    job = await _handed_back(env, "without the shared store")
    assert "not mounted" in (job.error or "")


async def test_remote_worker_hands_back_a_library_it_has_no_mapping_for(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _as_remote_worker(env, monkeypatch)
    monkeypatch.setenv("FRAMEFOUND_ROOT_MAP", f"/media/other={env['tmp'] / 'lib'}")
    get_settings.cache_clear()
    job = await _handed_back(env, "for an unmapped library")
    assert "root_map" in (job.error or "")


async def test_remote_worker_hands_back_when_its_share_is_down(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library folder is empty on this machine — its mount, most likely.
    The server may see the share fine, so the job goes back for it."""
    await _as_remote_worker(env, monkeypatch)
    job = await _handed_back(env, "with the share down")
    assert "not mounted" in (job.error or "")


async def test_remote_worker_hands_back_what_it_only_finds_out_inside_the_stage(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server keeps proxies somewhere this machine has no setting for,
    and the stage finds that out only when it asks for the store
    (`deriv.ensure_store` raises NotHere). Recorded as a failure, every
    proxy job the laptop took would count against its asset while the queue
    quietly handed it back."""
    await _as_remote_worker(env, monkeypatch)
    (env["tmp"] / "lib" / "target.jpg").write_bytes(b"data")

    async def handler(db: AsyncSession, asset, library, path) -> None:
        asset.processing_status = "half-done"  # work the hand-back must not keep
        raise worker_roots.NotHere("The server keeps proxies in /Volumes/Photos/proxies")

    with pytest.raises(Reject) as raised:
        await task_module._with_asset("generate_proxy", env["asset_id"], handler)

    assert raised.value.requeue is True
    async with env["factory"]() as db:
        job = (await db.execute(select(Job))).scalar_one()
        asset = await db.get(Asset, env["asset_id"])
    assert job.status == "skipped" and job.finished_at is not None
    assert (job.error or "").startswith("Handed back by mac-mini")
    assert "/Volumes/Photos/proxies" in (job.error or "")
    assert asset.processing_status != "half-done"


async def test_processing_report_counts_jobs_per_machine(env: dict) -> None:
    from framefound.main import create_app

    async with env["factory"]() as db:
        db.add_all(
            [
                Job(task_name="embed_frames", status="succeeded"),
                Job(task_name="embed_frames", status="succeeded", worker="mac-mini"),
                Job(task_name="transcribe_asset", status="failed", worker="mac-mini"),
            ]
        )
        await db.commit()

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
        body = (await client.get("/api/v1/system/processing")).json()
    assert body["workers_last_hour"] == {
        "server": {"succeeded": 1},
        "mac-mini": {"succeeded": 1, "failed": 1},
    }
    assert body["jobs_last_hour"] == {"succeeded": 2, "failed": 1}
    assert body["recent_failures"][0]["worker"] == "mac-mini"
