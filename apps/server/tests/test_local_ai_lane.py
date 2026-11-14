"""The `local_ai` lane (processing/local_ai_tasks.py): its one task, who consumes
it in the containers and on the Mac, its beat entry, and waking it.

A queue nothing consumes is work that silently never happens; one consumed
twice over would run two model jobs at once. Both deployments are read as
data here (docker-compose.yml, `services.launch`), as test_native_install.py
does for every other lane.
"""

import asyncio
import importlib.util
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from framefound import celery_app as celery_module
from framefound import local_models
from framefound.celery_app import LOCAL_AI_TICK, celery_app, local_ai_beat
from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.models import LocalAiJob
from framefound.local_models import jobs
from framefound.native import envfile, services
from framefound.native.layout import Layout
from framefound.processing import local_ai_tasks
from framefound.storage import stores

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("FRAMEFOUND_RUNTIMES_DIR", "FRAMEFOUND_LOCAL_AI_DEMO", "FRAMEFOUND_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    stores._CACHE.clear()
    yield
    get_settings.cache_clear()
    stores._CACHE.clear()


@pytest.fixture()
def layout(tmp_path: Path) -> Layout:
    return Layout(home=tmp_path / "FrameFound", source=REPO)


@pytest.fixture()
def env(layout: Layout) -> dict[str, str]:
    return envfile.parse(envfile.initial(layout, "/opt/homebrew/opt/postgresql@17/bin"))


def _queues(argv: list[str]) -> list[str]:
    return argv[argv.index("-Q") + 1].split(",")


def test_the_tick_is_on_local_ai_and_nothing_else_is() -> None:
    from framefound.processing import tasks  # noqa: F401 - registers every task

    on_lane = sorted(
        name
        for name, task in celery_app.tasks.items()
        if getattr(task, "queue", None) == "local_ai"
    )
    assert on_lane == ["framefound.local_ai_tick"]
    assert local_ai_tasks.local_ai_tick.max_retries == 0


def test_local_ai_runs_one_job_at_a_time_whatever_the_settings(
    layout: Layout, env: dict[str, str]
) -> None:
    tuned = {
        **env,
        "FRAMEFOUND_AI_WORKERS": "4",
        "FRAMEFOUND_LOCAL_AI_WORKERS": "4",
        "FRAMEFOUND_MEDIA_WORKERS": "4",
    }
    argv = services.launch("worker-local-ai", layout, tuned).argv
    assert "--concurrency=1" in argv
    assert _queues(argv) == ["local_ai"]
    # The only consumer on the Mac: no other lane takes it.
    takers = [
        name
        for name in services.LANES
        if "local_ai" in _queues(services.launch(name, layout, env).argv)
    ]
    assert takers == ["worker-local-ai"]


def test_the_local_ai_worker_gives_way_to_everything_else(layout: Layout) -> None:
    names = [service.name for service in services.SERVICES]
    assert names.index("model-server") < names.index("worker-local-ai") < names.index("backup")
    worker = services.BY_NAME["worker-local-ai"]
    assert worker.nice == max(service.nice for service in services.SERVICES)
    assert worker.waits_for == services.DB_AND_QUEUE
    # A restart must not wait an hour for a download: it reads "stopped" and resumes.
    assert worker.exit_timeout_s == 120
    plist = services.agent(worker, layout)
    assert plist["KeepAlive"] is True and plist["LowPriorityBackgroundIO"] is True
    assert "worker-local-ai" in services.APP_SERVICES


def test_the_containers_consume_the_lane_in_the_transcription_worker_alone() -> None:
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    lanes = re.findall(r'"-Q", "([a-z_,]+)", "--concurrency=(\d+)"', compose)
    takers = [(queues, concurrency) for queues, concurrency in lanes if "local_ai" in queues]
    assert takers == [("transcribe,local_ai", "1")]


def test_the_tick_is_scheduled_only_where_models_can_run(tmp_path: Path) -> None:
    containers = Settings(data_dir=tmp_path)
    assert not local_models.availability(containers).ok
    assert local_ai_beat(containers) == {}
    mac = Settings(data_dir=tmp_path, runtimes_dir=tmp_path / "runtimes")
    entry = local_ai_beat(mac)
    assert entry == {"local-ai-tick": LOCAL_AI_TICK}
    tick = entry["local-ai-tick"]
    assert tick["task"] == "framefound.local_ai_tick" and tick["schedule"] == 60.0
    # Ticks do not pile up behind a long download.
    assert tick["options"]["expires"] < tick["schedule"]
    # A copy: changing the entry handed out never changes the template.
    tick["options"]["expires"] = 1
    assert LOCAL_AI_TICK["options"]["expires"] == 50
    # The review copy runs its jobs inside the API (P30): no tick, and one that
    # arrives anyway does nothing (it builds no stand-ins of its own).
    demo = Settings(data_dir=tmp_path, local_ai_demo=True)
    assert local_models.availability(demo).mode == "demo"
    assert local_ai_beat(demo) == {}
    assert asyncio.run(local_ai_tasks._tick(demo)) is False


def _schedule_at_start(monkeypatch: pytest.MonkeyPatch, **env: str) -> dict[str, Any]:
    """The beat schedule `celery_app.py` builds when a process starts with `env`.

    The module is run again under another name, so the app every task is bound
    to is left as it is (and is made the current app again afterwards)."""
    for name in ("FRAMEFOUND_RUNTIMES_DIR", "FRAMEFOUND_LOCAL_AI_DEMO"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    spec = importlib.util.spec_from_file_location(
        "framefound._celery_app_at_start", celery_module.__file__
    )
    assert spec is not None and spec.loader is not None
    started = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(started)
    finally:
        celery_app.set_current()
        get_settings.cache_clear()
    schedule: dict[str, Any] = dict(started.celery_app.conf.beat_schedule)
    return schedule


def test_beat_is_given_the_tick_where_models_can_run_and_nowhere_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Read from the schedule beat starts with, not from the helper: a Mac whose
    beat lacked the tick would never start overnight work, never notice a
    Resume from another tab, never reap a dead job and never unload."""
    containers = _schedule_at_start(monkeypatch)
    assert "tag-suggest-sweep" in containers and "local-ai-tick" not in containers
    mac = _schedule_at_start(monkeypatch, FRAMEFOUND_RUNTIMES_DIR=str(tmp_path / "runtimes"))
    assert mac["local-ai-tick"] == LOCAL_AI_TICK
    demo = _schedule_at_start(monkeypatch, FRAMEFOUND_LOCAL_AI_DEMO="true")
    assert "local-ai-tick" not in demo


def test_waking_the_lane_says_whether_the_queue_took_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent: list[str] = []
    mac = Settings(data_dir=tmp_path, runtimes_dir=tmp_path / "runtimes")
    monkeypatch.setattr(local_ai_tasks.local_ai_tick, "delay", lambda: sent.append("tick"))
    assert local_ai_tasks.wake(mac) is True
    assert sent == ["tick"]

    def down() -> None:
        raise ConnectionError("no queue")

    monkeypatch.setattr(local_ai_tasks.local_ai_tick, "delay", down)
    assert local_ai_tasks.wake(mac) is False
    # The review copy has no queue: nothing is sent, and the demo's own lane
    # (inside this process) is started instead, so the job is not given up.
    from framefound.local_models import demo as demo_mode

    monkeypatch.setattr(local_ai_tasks.local_ai_tick, "delay", lambda: sent.append("demo"))
    demo = Settings(
        data_dir=tmp_path,
        local_ai_demo=True,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'demo.db').as_posix()}",
    )
    try:
        assert local_ai_tasks.wake(demo) is True and sent == ["tick"]
    finally:
        assert demo_mode.stop()


def test_a_tick_in_the_containers_closes_what_it_finds_and_returns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D26: nothing should ever be queued there; if something is, it is said, at once."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'tick.db').as_posix()}"

    async def make() -> Any:
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            job = await jobs.create(db, "runtime_install", subject="MLX-VLM 0.7.13")
            await db.commit()
            job_id = job.id
        await engine.dispose()
        return job_id

    async def read(job_id: Any) -> LocalAiJob:
        engine = create_async_engine(url)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            found = await db.get(LocalAiJob, job_id)
        await engine.dispose()
        assert found is not None
        return found

    job_id = asyncio.run(make())
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    woken: list[bool] = []
    monkeypatch.setattr(local_ai_tasks, "wake", lambda settings=None: woken.append(True))
    local_ai_tasks.local_ai_tick()
    closed = asyncio.run(read(job_id))
    assert (closed.state, closed.message) == ("failed", local_models.UNAVAILABLE)
    assert woken == []
