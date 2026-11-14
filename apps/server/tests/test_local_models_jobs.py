"""The work table of local AI and its runner (local_models/jobs.py), and L1's
four kinds of work (local_models/handlers.py).

`jobs.run_next` is awaited directly: no queue, no worker. Hugging Face is
`fake.FakeHub` (models shrunk to a few kilobytes, so a download really passes
its checks), the model server is `fake.FakeModelServer`, the installer is
`fake.FakeRunner`, and the clock is a `Clock` the test moves. The database is
file-backed SQLite. A fixture makes starting a process or reaching a real
address fail the test.
"""

import asyncio
import io
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound import local_models
from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.models import Asset, Derivative, Library, LocalAiJob, LocalModel
from framefound.local_models import (
    fake,
    handlers,
    jobs,
    library,
    preflight,
    registry,
    runtimes,
    schedule,
)
from framefound.local_models.ask import NotNow
from framefound.local_models.deps import Deps
from framefound.local_models.kinds import MlxVlm
from framefound.local_models.settings_store import LocalModelsConfig, load_config, save_config
from framefound.storage import stores

Factory = async_sessionmaker[AsyncSession]

SECRET = "not-a-real-secret-key"
FIRST = "mlx-community/gemma-4-e4b-it-4bit"
TEXT_ONLY = "mlx-community/gemma-4-12B-it-OptiQ-4bit"
VERSION = "0.7.12"
NOON = datetime(2026, 11, 12, 12, 0)

_OURS = (
    "FRAMEFOUND_BULK_DIR",
    "FRAMEFOUND_PROXY_DIR",
    "FRAMEFOUND_MODELS_DIR",
    "FRAMEFOUND_DATA_DIR",
    "FRAMEFOUND_RUNTIMES_DIR",
    "FRAMEFOUND_LOCAL_AI_DEMO",
    "FRAMEFOUND_DOMAIN",
    "FRAMEFOUND_SECRET_KEY",
    "FRAMEFOUND_MIN_FREE_GB",
)


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test may start a program or reach a real address; trying to is a failure."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a test tried to start a process or reach a real address")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr("socket.create_connection", refuse)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    for name in _OURS:
        monkeypatch.delenv(name, raising=False)
    # The secret that seals a stored token is read from the process's settings.
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", SECRET)
    get_settings.cache_clear()
    stores._CACHE.clear()
    yield
    get_settings.cache_clear()
    stores._CACHE.clear()


@pytest.fixture(autouse=True)
def _kinds_restored() -> Iterator[None]:
    """A test may register kinds of its own; L1's four are what is left after it."""
    before = dict(jobs._KINDS)
    yield
    jobs._KINDS.clear()
    jobs._KINDS.update(before)


@pytest.fixture()
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Progress written, and Cancel looked for, at once rather than every two seconds."""
    monkeypatch.setattr(jobs, "PROGRESS_EVERY_S", 0.01)


@pytest.fixture()
async def factory(tmp_path: Path) -> AsyncIterator[Factory]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class Clock:
    """The Mac's wall clock, naive, moved by the test."""

    def __init__(self, at: datetime = NOON) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class Site:
    """A Mac: its data and models folders, its runtimes folder, and a share at `nas`."""

    def __init__(self, tmp_path: Path) -> None:
        self.data = tmp_path / "data"
        self.models = tmp_path / "models"
        self.runtimes = tmp_path / "runtimes"
        self.nas = tmp_path / "nas"
        for folder in (self.data, self.models, self.runtimes, self.nas):
            folder.mkdir()
        self.hub = fake.FakeHub.from_fixtures(sleep=lambda _seconds: None)
        self.hub.shrink(FIRST)
        self.hub.shrink(TEXT_ONLY)
        self.settings = self.make()
        self.clock = Clock()
        self.server = fake.FakeModelServer(sleep=lambda _seconds: None)
        self.run = self.runner()

    def make(self, **more: Any) -> Settings:
        values: dict[str, Any] = {
            "data_dir": self.data,
            "models_dir": self.models,
            "runtimes_dir": self.runtimes,
            "secret_key": SECRET,
            "min_free_gb": 0,
        }
        values.update(more)
        return Settings(**values)

    def library_on_the_nas(self) -> None:
        stores.mark_bulk_root(self.nas)
        self.settings = self.make(bulk_dir=self.nas)

    def nas_drops(self) -> None:
        (self.nas / "models" / stores.BULK_STORE_MARKER).unlink()

    @staticmethod
    def runner() -> fake.FakeRunner:
        def prints_its_version(argv: list[str]) -> tuple[int, str]:
            return 0, f"{Path(argv[0]).parents[2].name}\n"

        return fake.FakeRunner(
            {
                "uv venv": (0, ""),
                "uv pip install": (0, ""),
                "uv pip freeze": (0, ""),
                "python": prints_its_version,
            },
            make_venvs=True,
        )

    def install_mlx_vlm(self, version: str = VERSION) -> None:
        runtimes.install(MlxVlm(), version, self.runtimes, run=self.run)
        runtimes.activate(self.runtimes, "mlx-vlm", version)

    def deps(self, **more: Any) -> Deps:
        values: dict[str, Any] = {
            "settings": self.settings,
            "hub_for": self.hub.with_token,
            "fetch_json": lambda _url, **_more: {"info": {"version": "0.7.13"}},
            "server": self.server,
            "kinds": {"mlx-vlm": MlxVlm()},
            "run": self.run,
            "uv": "uv",
            "sleep": lambda _seconds: None,
            "now": self.clock,
            "system": "Darwin",
            "machine": "arm64",
        }
        values.update(more)
        return Deps(**values)

    async def add(
        self,
        factory: Factory,
        repo: str = FIRST,
        *,
        role: str | None = None,
        state: str = registry.INSTALLED,
        download: bool = True,
    ) -> uuid.UUID:
        """A model checked and added as the page would; its files in the library."""
        found = preflight.preflight(
            self.hub,
            repo,
            config=LocalModelsConfig(),
            kinds={"mlx-vlm": MlxVlm()},
            installed={"mlx-vlm": VERSION},
            free_bytes=None,
            min_free_bytes=0,
        )
        async with factory() as db:
            model = await registry.add_model(db, found, None)
            model.state = state
            if role is not None:
                await registry.set_role(db, role, model, pending=False, user_id=None)
            await db.commit()
            files = registry.snapshot(model)
        if download:
            library.download(self.hub, files, settings=self.settings)
        return model.id

    async def files(self, factory: Factory, model_id: uuid.UUID) -> library.ModelFiles:
        async with factory() as db:
            model = await registry.get_model(db, model_id)
            assert model is not None
            return registry.snapshot(model)


@pytest.fixture()
def site(tmp_path: Path) -> Site:
    made = Site(tmp_path)
    made.install_mlx_vlm()
    return made


async def queue(factory: Factory, kind: str, **more: Any) -> uuid.UUID:
    values: dict[str, Any] = {"subject": f"{kind} {uuid.uuid4().hex[:6]}"}
    values.update(more)
    async with factory() as db:
        job = await jobs.create(db, kind, **values)
        await db.commit()
        return job.id


async def row(factory: Factory, job_id: uuid.UUID) -> LocalAiJob:
    async with factory() as db:
        found = await db.get(LocalAiJob, job_id)
        assert found is not None
        return found


async def model_row(factory: Factory, model_id: uuid.UUID) -> LocalModel:
    async with factory() as db:
        found = await registry.get_model(db, model_id)
        assert found is not None
        return found


async def drain(factory: Factory, deps: Deps, limit: int = 20) -> None:
    """What the lane does when woken: run jobs until none is queued."""
    for _ in range(limit):
        if not await jobs.run_next(factory, deps):
            return
    raise AssertionError("the lane never ran out of work")


def kind(
    name: str,
    handler: Callable[[jobs.JobContext, LocalAiJob], Any],
    *,
    needs_model: bool = False,
) -> None:
    jobs.register(name, handler, needs_model=needs_model, title=lambda job: f"Doing {job.subject}")


def utc(local: datetime) -> datetime:
    return local.astimezone(UTC)


def stored(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


# --- the table and the runner ---------------------------------------------------------


async def test_the_more_urgent_job_runs_first(factory: Factory, site: Site) -> None:
    ran: list[str] = []

    async def note(_ctx: jobs.JobContext, job: LocalAiJob) -> dict[str, Any]:
        ran.append(job.subject)
        return {}

    kind("note", note)
    await queue(factory, "note", subject="background", priority=9)
    await queue(factory, "note", subject="asked for, first", priority=5)
    await queue(factory, "note", subject="watched", priority=0)
    await queue(factory, "note", subject="asked for, second", priority=5)
    await drain(factory, site.deps())
    assert ran == ["watched", "asked for, first", "asked for, second", "background"]


async def test_two_runners_cannot_run_the_same_job(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[str] = []

    async def slow(_ctx: jobs.JobContext, job: LocalAiJob) -> dict[str, Any]:
        ran.append(job.subject)
        await asyncio.sleep(0.05)
        return {}

    # Both runners reach the claim together, so both see the same queued row.
    together = asyncio.Barrier(2)
    real_load = jobs.load_config

    async def load_then_wait(db: AsyncSession) -> LocalModelsConfig:
        config = await real_load(db)
        await together.wait()
        return config

    monkeypatch.setattr(jobs, "load_config", load_then_wait)
    kind("slow", slow)
    job_id = await queue(factory, "slow", subject="only once")
    first, second = await asyncio.gather(
        jobs.run_next(factory, site.deps()), jobs.run_next(factory, site.deps())
    )
    assert ran == ["only once"]
    assert (await row(factory, job_id)).state == "done"
    # The runner that lost the claim says there may be more to look at; the
    # one that ran it says nothing is left.
    assert sorted([first, second]) == [False, True]


async def test_a_second_delivery_does_nothing(factory: Factory, site: Site) -> None:
    ran: list[str] = []

    async def once(_ctx: jobs.JobContext, job: LocalAiJob) -> dict[str, Any]:
        ran.append(job.subject)
        return {"ok": True}

    kind("once", once)
    job_id = await queue(factory, "once")
    assert await jobs.run_next(factory, site.deps()) is False
    assert await jobs.run_next(factory, site.deps()) is False
    assert len(ran) == 1
    done = await row(factory, job_id)
    assert (done.state, done.result, done.message) == ("done", {"ok": True}, "")
    assert done.finished_at is not None and done.started_at is not None


async def test_a_job_is_asked_for_once_while_it_is_active(factory: Factory, site: Site) -> None:
    async def nothing(_ctx: jobs.JobContext, _job: LocalAiJob) -> None:
        return None

    kind("nothing", nothing)
    await queue(factory, "nothing", subject="the same thing")
    with pytest.raises(jobs.JobRefused, match="already waiting its turn"):
        await queue(factory, "nothing", subject="the same thing")
    # Not unique: allowed. Finished: allowed again.
    await queue(factory, "nothing", subject="the same thing", unique=False)
    await drain(factory, site.deps())
    await queue(factory, "nothing", subject="the same thing")
    with pytest.raises(ValueError, match="never registered"):
        await queue(factory, "no such kind")


async def test_cancel_is_seen_within_a_tick(factory: Factory, site: Site, quick: None) -> None:
    started = asyncio.Event()
    stopped_by: list[str] = []

    async def long(ctx: jobs.JobContext, _job: LocalAiJob) -> dict[str, Any]:
        def work(report: Callable[[str, int, int], None], should_stop: Callable[[], bool]) -> None:
            for done in range(10_000):
                report("working", done, 10_000)
                if should_stop():
                    stopped_by.append("should_stop")
                    raise jobs.Cancelled()
                time.sleep(0.005)

        started.set()
        await ctx.run_blocking(work)
        return {}

    kind("long", long)
    job_id = await queue(factory, "long")
    running = asyncio.ensure_future(jobs.run_next(factory, site.deps()))
    await started.wait()
    await asyncio.sleep(0.05)
    async with factory() as db:
        asked = await jobs.request_cancel(db, job_id)
        await db.commit()
    assert asked.state == "running" and asked.cancel_requested
    await asyncio.wait_for(running, timeout=10)
    ended = await row(factory, job_id)
    assert stopped_by == ["should_stop"]
    assert ended.state == "cancelled" and ended.message == jobs.CANCELLED
    assert ended.phase == "working" and 0 < ended.done < 10_000  # where it got to is kept


async def test_a_job_cancelled_before_its_turn_never_runs(factory: Factory, site: Site) -> None:
    ran: list[str] = []

    async def note(_ctx: jobs.JobContext, job: LocalAiJob) -> None:
        ran.append(job.subject)

    kind("note", note)
    job_id = await queue(factory, "note")
    async with factory() as db:
        assert (await jobs.request_cancel(db, job_id)).state == "cancelled"
        await db.commit()
    await drain(factory, site.deps())
    assert ran == []
    async with factory() as db:
        with pytest.raises(jobs.JobRefused, match="already finished"):
            await jobs.request_cancel(db, job_id)
        with pytest.raises(jobs.NoSuchJob):
            await jobs.request_cancel(db, uuid.uuid4())


async def test_a_job_whose_worker_died_reads_as_stopped(factory: Factory, site: Site) -> None:
    async def never(_ctx: jobs.JobContext, _job: LocalAiJob) -> None:
        raise AssertionError("a dead job is not run again by itself")

    kind("dead", never)
    job_id = await queue(factory, "dead")
    beat = utc(site.clock() - timedelta(seconds=jobs.STALE_AFTER_S + 5))
    async with factory() as db:
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == job_id)
            .values(state="running", started_at=beat, beat_at=beat)
        )
        await db.commit()
    # The page reads it as stopped before the lane has said so…
    seen = await row(factory, job_id)
    assert jobs.shown(seen, now=site.clock()) == ("failed", jobs.STOPPED)
    # A minute earlier it was still alive.
    assert jobs.shown(seen, now=site.clock() - timedelta(seconds=60)) == ("running", "")
    # …and the lane's next tick writes it down.
    assert await jobs.run_next(factory, site.deps()) is False
    reaped = await row(factory, job_id)
    assert (reaped.state, reaped.message) == ("failed", jobs.STOPPED)
    assert reaped.finished_at is not None


async def test_a_job_waiting_on_a_long_answer_does_not_read_as_dead(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ask() can outlast STALE_AFTER_S (a five-minute load). The runner's own
    heartbeat keeps the row alive; the handler does nothing for it."""
    monkeypatch.setattr(jobs, "HEARTBEAT_S", 0.01)
    seen: list[tuple[str, str]] = []
    reaped: list[bool] = []

    async def long_answer(ctx: jobs.JobContext, job: LocalAiJob) -> dict[str, Any]:
        for _ in range(3):
            site.clock.advance(jobs.STALE_AFTER_S * 0.8)
            await asyncio.sleep(0.1)  # the answer is still being written
            seen.append(jobs.shown(await row(ctx.factory, job.id), now=site.clock()))
        # A second runner (a tick from beat) looks meanwhile, and reaps nothing.
        reaped.append(await jobs.run_next(ctx.factory, site.deps()))
        return {}

    kind("long_answer", long_answer, needs_model=True)
    job_id = await queue(factory, "long_answer")
    await jobs.run_next(factory, site.deps())
    assert seen == [("running", "")] * 3
    assert reaped == [False]
    assert (await row(factory, job_id)).state == "done"


async def test_a_handler_that_says_later_is_run_again_with_its_progress_kept(
    factory: Factory, site: Site, quick: None
) -> None:
    calls: list[int] = []

    async def batch(ctx: jobs.JobContext, _job: LocalAiJob) -> dict[str, Any]:
        calls.append(len(calls))
        if len(calls) == 1:
            await ctx.progress(phase="looking", done=3, total=10)
            raise jobs.Later("A more urgent job went first.")
        if len(calls) == 2:
            raise jobs.Later(
                "Stopped for the day at 05:30 — carries on at 21:30.",
                not_before=site.clock() + timedelta(hours=1),
            )
        return {"looked_at": 10}

    kind("batch", batch, needs_model=True)
    job_id = await queue(factory, "batch")
    assert await jobs.run_next(factory, site.deps()) is True
    again = await row(factory, job_id)
    assert (again.state, again.message) == ("queued", "A more urgent job went first.")
    assert (again.phase, again.done, again.total) == ("looking", 3, 10)
    first_start = again.started_at
    assert first_start is not None and again.finished_at is None

    assert await jobs.run_next(factory, site.deps()) is False
    waiting = await row(factory, job_id)
    assert waiting.state == "waiting" and waiting.message.startswith("Stopped for the day")
    assert stored(waiting.not_before) == utc(site.clock() + timedelta(hours=1))
    assert (waiting.done, waiting.started_at) == (3, first_start)

    assert await jobs.run_next(factory, site.deps()) is False  # not yet
    assert calls == [0, 1]
    site.clock.advance(3601)
    await jobs.run_next(factory, site.deps())
    done = await row(factory, job_id)
    assert (done.state, done.result, calls) == ("done", {"looked_at": 10}, [0, 1, 2])


async def test_a_handler_can_fail_in_its_own_words(factory: Factory, site: Site) -> None:
    async def picky(_ctx: jobs.JobContext, _job: LocalAiJob) -> None:
        raise jobs.Failed("No picture could be read from this clip.", detail="ffmpeg said no")

    async def broken(_ctx: jobs.JobContext, _job: LocalAiJob) -> None:
        raise KeyError("a bug")

    kind("picky", picky)
    kind("broken", broken)
    picky_id = await queue(factory, "picky")
    broken_id = await queue(factory, "broken")
    await drain(factory, site.deps())
    said = await row(factory, picky_id)
    assert (said.state, said.message, said.detail) == (
        "failed",
        "No picture could be read from this clip.",
        "ffmpeg said no",
    )
    crashed = await row(factory, broken_id)
    assert (crashed.state, crashed.message) == ("failed", "It failed unexpectedly (KeyError).")


async def test_a_kind_this_worker_does_not_know_is_closed_plainly(
    factory: Factory, site: Site
) -> None:
    async def gone(_ctx: jobs.JobContext, _job: LocalAiJob) -> None:
        raise AssertionError("never run")

    kind("from_a_newer_release", gone)
    job_id = await queue(factory, "from_a_newer_release")
    del jobs._KINDS["from_a_newer_release"]
    await drain(factory, site.deps())
    closed = await row(factory, job_id)
    assert (closed.state, closed.message) == ("failed", jobs.UNKNOWN_KIND)
    assert jobs.title_of(closed) == closed.subject


async def test_on_a_server_without_runtimes_every_job_is_closed_with_the_reason(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory, state=registry.DOWNLOADING, download=False)
    download = await queue(factory, "download", model_id=model_id)
    later = await queue(factory, "test", model_id=model_id, not_before=NOON + timedelta(hours=9))
    containers = site.deps(settings=Settings(data_dir=site.data, models_dir=site.models), kinds={})
    assert await jobs.run_next(factory, containers) is False
    for job_id in (download, later):
        closed = await row(factory, job_id)
        assert (closed.state, closed.message) == ("failed", local_models.UNAVAILABLE)
    # The model does not read as downloading for ever.
    model = await model_row(factory, model_id)
    assert (model.state, model.error) == (registry.INCOMPLETE, local_models.UNAVAILABLE)
    assert site.server.requests == [] and site.hub.fetches == 0


async def test_waiting_jobs_are_released_and_counted_but_tonight_stays_tonight(
    factory: Factory, site: Site
) -> None:
    """Resume, or a new budget, releases what the runner held back itself (the
    window, a pause). A job its requester asked for tonight is their choice:
    no change of settings starts it in the afternoon."""

    async def note(_ctx: jobs.JobContext, _job: LocalAiJob) -> None:
        return None

    kind("note", note)
    at_night = NOON + timedelta(hours=9, minutes=30)
    tonight = await queue(factory, "note", not_before=at_night)
    now = await queue(factory, "note")
    held = await queue(factory, "note")
    async with factory() as db:
        # What the runner writes for a job the window held back.
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == held)
            .values(state="waiting", not_before=utc(at_night), message="Waiting for the window")
        )
        await db.commit()
    async with factory() as db:
        assert await jobs.depth(db) == 3
        assert [job.id for job in await jobs.recent(db)] == [tonight, now, held]
        assert (await db.get(LocalAiJob, tonight)).state == "waiting"  # type: ignore[union-attr]
        assert await jobs.release_waiting(db, site.clock()) == 1
        await db.commit()
    await drain(factory, site.deps())
    assert (await row(factory, now)).state == "done"
    assert (await row(factory, held)).state == "done"
    still = await row(factory, tonight)
    assert still.state == "waiting" and stored(still.not_before) == utc(at_night)

    # Put back in the queue by any other way, it still does not start early.
    async with factory() as db:
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == tonight)
            .values(state="queued", not_before=None)
        )
        await db.commit()
    await drain(factory, site.deps())
    still = await row(factory, tonight)
    assert still.state == "waiting" and stored(still.not_before) == utc(at_night)

    site.clock.at = at_night
    await drain(factory, site.deps())
    async with factory() as db:
        assert await jobs.depth(db) == 0
        assert {job.state for job in await jobs.recent(db)} == {"done"}


async def test_a_batch_knows_when_a_more_urgent_job_is_waiting(
    factory: Factory, site: Site
) -> None:
    answers: list[bool] = []

    async def batch(ctx: jobs.JobContext, _job: LocalAiJob) -> None:
        answers.append(await ctx.should_yield())
        await queue(ctx.factory, "note", priority=9)
        answers.append(await ctx.should_yield())
        await queue(ctx.factory, "note", priority=0)
        answers.append(await ctx.should_yield())

    async def note(_ctx: jobs.JobContext, _job: LocalAiJob) -> None:
        return None

    kind("batch", batch)
    kind("note", note)
    await queue(factory, "batch", priority=5)
    await jobs.run_next(factory, site.deps())
    assert answers == [False, False, True]


# --- pause and the overnight window ---------------------------------------------------


async def test_an_overnight_job_waits_and_says_until_when(factory: Factory, site: Site) -> None:
    model_id = await site.add(factory)
    async with factory() as db:
        await db.execute(update(LocalModel).where(LocalModel.id == model_id).values(memory_gb=10.0))
        await db.commit()
    job_id = await queue(factory, "test", model_id=model_id, priority=0)
    await drain(factory, site.deps())
    waiting = await row(factory, job_id)
    assert waiting.state == "waiting"
    assert waiting.message == (
        "Waiting for the overnight window (starts 21:30): it needs about 10 GB and the "
        "daytime budget is 8 GB."
    )
    assert stored(waiting.not_before) == utc(datetime(2026, 11, 12, 21, 30))
    assert site.server.requests == []
    site.clock.at = datetime(2026, 11, 12, 21, 30)
    await drain(factory, site.deps())
    done = await row(factory, job_id)
    assert done.state == "done" and done.result is not None
    assert done.result["structured_ok"] is True


async def test_a_paused_model_job_waits_and_a_download_does_not(
    factory: Factory, site: Site
) -> None:
    tested = await site.add(factory)
    fetched = await site.add(factory, TEXT_ONLY, state=registry.DOWNLOADING, download=False)
    async with factory() as db:
        config = await load_config(db)
        config.paused = True
        await save_config(db, config)
    test_id = await queue(factory, "test", model_id=tested, priority=0)
    download_id = await queue(factory, "download", model_id=fetched, priority=5)
    await drain(factory, site.deps())
    held = await row(factory, test_id)
    assert (held.state, held.message) == ("waiting", schedule.PAUSED)
    assert (await row(factory, download_id)).state == "done"
    assert (await model_row(factory, fetched)).state == registry.INSTALLED
    assert site.server.requests == []  # no load was started only to be refused
    # Resume: the page releases what was waiting, and it runs.
    async with factory() as db:
        config = await load_config(db)
        config.paused = False
        await save_config(db, config)
        await jobs.release_waiting(db)
        await db.commit()
    await drain(factory, site.deps())
    assert (await row(factory, test_id)).state == "done"


async def test_a_model_that_may_not_run_now_waits_and_one_that_never_can_fails(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory)
    site.server.down = True
    job_id = await queue(factory, "test", model_id=model_id)
    await drain(factory, site.deps())
    waiting = await row(factory, job_id)
    assert waiting.state == "waiting" and "not answering" in waiting.message
    assert stored(waiting.not_before) == utc(site.clock() + timedelta(seconds=60))

    async with factory() as db:
        await db.execute(update(LocalModel).where(LocalModel.id == model_id).values(memory_gb=99.0))
        await jobs.release_waiting(db)
        await db.commit()
    site.server.down = False
    await drain(factory, site.deps())
    never = await row(factory, job_id)
    assert never.state == "failed" and "more than the overnight budget" in never.message


async def test_the_sweep_unloads_a_model_held_past_its_window(factory: Factory, site: Site) -> None:
    model_id = await site.add(factory)
    await queue(factory, "test", model_id=model_id)
    await drain(factory, site.deps())
    assert site.server.status()["state"] == "ready"  # type: ignore[index]
    async with factory() as db:
        config = await load_config(db)
        config.paused = True
        await save_config(db, config)
    # Nothing is queued; the tick unloads it anyway.
    assert await jobs.run_next(factory, site.deps()) is False
    assert site.server.unloads == 1


# --- download ---------------------------------------------------------------------------


async def test_a_download_installs_the_model(factory: Factory, site: Site, quick: None) -> None:
    model_id = await site.add(factory, state=registry.DOWNLOADING, download=False)
    job_id = await queue(factory, "download", model_id=model_id, subject=f"{FIRST}@475b9088")
    await drain(factory, site.deps())
    done = await row(factory, job_id)
    files = await site.files(factory, model_id)
    size = sum(file.size for file in files.files)
    assert done.state == "done" and done.result == {"bytes": size}
    assert (done.phase, done.done, done.total) == ("downloading", size, size)
    assert jobs.title_of(done) == "Downloading gemma-4-e4b-it-4bit"
    model = await model_row(factory, model_id)
    assert (model.state, model.error) == (registry.INSTALLED, None)
    assert library.presence(files, site.settings).in_library is True


async def test_a_dropped_download_says_where_it_stopped_and_resumes(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory, state=registry.DOWNLOADING, download=False)
    site.hub.fail_after(3000)
    first = await queue(factory, "download", model_id=model_id)
    await drain(factory, site.deps())
    failed = await row(factory, first)
    assert failed.state == "failed"
    assert failed.message.startswith("The connection dropped at 0.0 of 0.0 GB.")
    assert failed.message.endswith("What was downloaded is kept.")
    model = await model_row(factory, model_id)
    assert (model.state, model.error) == (registry.INCOMPLETE, failed.message)
    fetched = site.hub.fetches
    # Resume carries on from what arrived.
    again = await queue(factory, "download", model_id=model_id)
    await drain(factory, site.deps())
    assert (await row(factory, again)).state == "done"
    assert (await model_row(factory, model_id)).state == registry.INSTALLED
    resumed = [call for call in site.hub.calls[fetched:] if call[0] == "fetch"]
    assert any(call[4] > 0 for call in resumed)  # a file picked up part-way


async def test_a_cancelled_download_keeps_what_it_has(
    factory: Factory, site: Site, quick: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(library, "REPORT_EVERY_S", 0.01)
    model_id = await site.add(factory, state=registry.DOWNLOADING, download=False)
    site.hub.chunk_bytes = 64
    site.hub._sleep = time.sleep
    site.hub.slow(0.01)
    job_id = await queue(factory, "download", model_id=model_id)
    running = asyncio.ensure_future(jobs.run_next(factory, site.deps()))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if (await row(factory, job_id)).done > 0:
            break
    async with factory() as db:
        await jobs.request_cancel(db, job_id)
        await db.commit()
    await asyncio.wait_for(running, timeout=20)
    ended = await row(factory, job_id)
    assert ended.state == "cancelled"
    assert ended.message.startswith("Cancelled. Resume carries on from")
    model = await model_row(factory, model_id)
    assert (model.state, model.error) == (registry.INCOMPLETE, ended.message)


async def test_a_download_cancelled_before_its_turn_leaves_the_model_resumable(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory, state=registry.DOWNLOADING, download=False)
    job_id = await queue(factory, "download", model_id=model_id)
    async with factory() as db:
        await jobs.request_cancel(db, job_id)
        await db.commit()
    model = await model_row(factory, model_id)
    assert (model.state, model.error) == (
        registry.INCOMPLETE,
        "Cancelled. Resume carries on from 0.0 GB.",
    )


async def test_a_download_whose_worker_died_leaves_the_model_resumable(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory, state=registry.DOWNLOADING, download=False)
    job_id = await queue(factory, "download", model_id=model_id)
    long_ago = utc(site.clock() - timedelta(minutes=10))
    async with factory() as db:
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == job_id)
            .values(state="running", started_at=long_ago, beat_at=long_ago, done=2 * 1024**3)
        )
        await db.commit()
    await jobs.run_next(factory, site.deps())
    model = await model_row(factory, model_id)
    assert model.state == registry.INCOMPLETE
    assert model.error == handlers.DOWNLOAD_STOPPED.format(done="2.0")


async def test_a_token_that_can_no_longer_be_read_is_said_plainly(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory, state=registry.DOWNLOADING, download=False)
    async with factory() as db:
        config = await load_config(db)
        config.hf_token_sealed = "sealed-under-another-key"
        await save_config(db, config)
    job_id = await queue(factory, "download", model_id=model_id)
    await drain(factory, site.deps())
    assert (await row(factory, job_id)).message == handlers.TOKEN_UNREADABLE
    assert (await model_row(factory, model_id)).error == handlers.TOKEN_UNREADABLE
    assert site.hub.fetches == 0


# --- place: a role's copy on this machine ------------------------------------------------


async def give(factory: Factory, role: str, model_id: uuid.UUID) -> uuid.UUID:
    """As the page does: line the model up for the role, and queue its copy."""
    async with factory() as db:
        model = await registry.get_model(db, model_id)
        await registry.set_role(db, role, model, pending=True, user_id=None)
        job = await jobs.create(
            db, "place", subject=f"{model_id}", model_id=model_id, payload={"role": role}
        )
        await db.commit()
        return job.id


async def test_a_role_changes_hands_once_the_copy_is_here_and_the_old_copy_goes(
    factory: Factory, site: Site, quick: None
) -> None:
    site.library_on_the_nas()
    old = await site.add(factory, TEXT_ONLY, role="editor")
    old_files = await site.files(factory, old)
    library.place(old_files, settings=site.settings)
    new = await site.add(factory)
    new_files = await site.files(factory, new)
    job_id = await give(factory, "editor", new)
    await drain(factory, site.deps())
    done = await row(factory, job_id)
    assert done.state == "done" and done.result == {"role": "editor", "given": True}
    assert done.phase == "copying"
    async with factory() as db:
        editor = (await registry.roles(db))["editor"]
        assert (editor.model_id, editor.pending_model_id) == (new, None)
    assert library.presence(new_files, site.settings).on_this_machine
    assert not library.presence(old_files, site.settings).on_this_machine  # nothing runs it
    assert library.presence(old_files, site.settings).in_library  # the library keeps it
    assert site.server.unloads == 1


async def test_a_copy_that_fails_leaves_the_role_where_it_was(factory: Factory, site: Site) -> None:
    site.library_on_the_nas()
    old = await site.add(factory, TEXT_ONLY, role="editor")
    new = await site.add(factory)
    site.nas_drops()
    job_id = await give(factory, "editor", new)
    await drain(factory, site.deps())
    failed = await row(factory, job_id)
    assert failed.state == "failed" and library.ROLE_STAYS in failed.message
    async with factory() as db:
        editor = (await registry.roles(db))["editor"]
        assert (editor.model_id, editor.pending_model_id) == (old, None)


async def test_a_copy_the_operator_changed_their_mind_about_is_not_kept(
    factory: Factory, site: Site
) -> None:
    site.library_on_the_nas()
    new = await site.add(factory)
    files = await site.files(factory, new)
    job_id = await give(factory, "looker", new)
    async with factory() as db:
        await registry.set_role(db, "looker", None, pending=False, user_id=None)
        await db.commit()
    await drain(factory, site.deps())
    assert (await row(factory, job_id)).result == {"role": "looker", "given": False}
    assert not library.presence(files, site.settings).on_this_machine


async def test_a_copy_cancelled_before_its_turn_stops_the_role_waiting(
    factory: Factory, site: Site
) -> None:
    site.library_on_the_nas()
    new = await site.add(factory)
    job_id = await give(factory, "looker", new)
    async with factory() as db:
        await jobs.request_cancel(db, job_id)
        await db.commit()
        assert (await registry.roles(db))["looker"].pending_model_id is None


async def test_a_role_given_while_the_library_moves_still_gets_its_copy(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move gives each role model a copy here from the roles it reads just before
    its switch. A role given after that read gets none from the move, so the job
    makes it itself once it sees the library has gone."""
    model_id = await site.add(factory)  # the library is on this machine: nothing is copied
    files = await site.files(factory, model_id)
    real_place = library.place
    calls: list[str] = []

    def place_then_move(model: library.ModelFiles, **more: Any) -> None:
        real_place(model, **more)
        calls.append("place")
        if len(calls) == 1:
            # The move's switch lands now: the library is on the NAS.
            target = site.nas / "models"
            shutil.copytree(site.models / library.LIBRARY, target / library.LIBRARY)
            stores.mark_store(target, stores.marker_for(library.KIND))
            stores.set_override(site.data, library.KIND, target)

    monkeypatch.setattr(library, "place", place_then_move)
    job_id = await give(factory, "looker", model_id)
    await drain(factory, site.deps())
    assert (await row(factory, job_id)).state == "done"
    assert calls == ["place", "place"]
    assert library.copies_to_this_machine(site.settings)
    assert library.presence(files, site.settings).on_this_machine
    library.serving_dir(files, need_local=True, settings=site.settings)


async def test_without_a_move_the_copy_is_made_once(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_id = await site.add(factory)
    real_place = library.place
    calls: list[str] = []

    def counted(model: library.ModelFiles, **more: Any) -> None:
        calls.append("place")
        real_place(model, **more)

    monkeypatch.setattr(library, "place", counted)
    await give(factory, "looker", model_id)
    await drain(factory, site.deps())
    assert calls == ["place"]


async def test_a_copy_waits_out_a_move_in_its_last_steps(factory: Factory, site: Site) -> None:
    model_id = await site.add(factory)
    stores.pause(site.data, library.KIND)
    waited: list[float] = []

    def sleep(seconds: float) -> None:
        waited.append(seconds)
        if len(waited) == 3:
            stores.resume(site.data, library.KIND)

    job_id = await give(factory, "looker", model_id)
    await drain(factory, site.deps(sleep=sleep))
    assert (await row(factory, job_id)).state == "done"
    assert waited == [library.MOVE_POLL_S] * 3


# --- runtime install ----------------------------------------------------------------------


async def test_a_runtime_install_switches_to_the_new_version(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch, quick: None
) -> None:
    monkeypatch.setattr(handlers, "_which", lambda _name: "/opt/homebrew/bin/uv")
    job_id = await queue(
        factory, "runtime_install", subject="MLX-VLM 0.7.13", payload={"name": "mlx-vlm"}
    )
    await drain(factory, site.deps())
    done = await row(factory, job_id)
    assert (done.state, done.result) == ("done", {"version": "0.7.13"})
    assert jobs.title_of(done) == "Installing MLX-VLM 0.7.13"
    installed = runtimes.describe(site.runtimes, "mlx-vlm")
    assert (installed.current, installed.previous) == ("0.7.13", VERSION)
    assert site.server.unloads == 1


async def test_a_runtime_install_that_cannot_happen_says_why(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "_which", lambda _name: None)
    no_uv = await queue(
        factory, "runtime_install", payload={"name": "mlx-vlm", "version": "0.7.13"}
    )
    await drain(factory, site.deps())
    assert (await row(factory, no_uv)).message == handlers.NO_UV

    monkeypatch.setattr(handlers, "_which", lambda _name: "/usr/local/bin/uv")
    intel = await queue(
        factory, "runtime_install", payload={"name": "mlx-vlm", "version": "0.7.13"}
    )
    await drain(factory, site.deps(machine="x86_64"))
    said = await row(factory, intel)
    assert said.state == "failed"
    assert said.message == MlxVlm().supported_here("Darwin", "x86_64")

    broken = Site.runner()
    broken.answer("uv pip install", (1, "", "no matching distribution"))
    fails = await queue(
        factory, "runtime_install", payload={"name": "mlx-vlm", "version": "0.7.13"}
    )
    await drain(factory, site.deps(run=broken))
    failed = await row(factory, fails)
    assert failed.state == "failed" and runtimes.UNCHANGED in failed.message
    assert failed.detail and "no matching distribution" in failed.detail
    assert runtimes.describe(site.runtimes, "mlx-vlm").current == VERSION


# --- test ----------------------------------------------------------------------------------


async def test_a_test_keeps_the_answer_the_time_the_memory_and_what_answered(
    factory: Factory, site: Site
) -> None:
    site.server.memory_gb = 6.1
    model_id = await site.add(factory)
    job_id = await queue(factory, "test", model_id=model_id, subject=f"{FIRST}@475b9088")
    await drain(factory, site.deps())
    done = await row(factory, job_id)
    assert done.state == "done" and done.result is not None
    result = done.result
    assert result["kind"] == "picture" and result["structured_ok"] is True
    assert set(result["answer"]) == {"caption", "tags"}
    assert result["sample"] == {
        "asset_id": None,
        "media_type": "image",
        "filename": None,
        "label": handlers.DRAWN,
    }
    assert result["memory_gb"] == 6.1 and result["memory_source"] == "runtime"
    assert result["ran_from"] == "this_machine" and result["attempts"] == 1
    assert result["model"] == {"repo_id": FIRST, "revision_short": "475b908"}
    # Provenance and figures on the job's own row (13.3).
    assert (done.model, done.runtime, done.runtime_version, done.prompt_version) == (
        FIRST,
        "mlx-vlm",
        VERSION,
        handlers.TEST_PROMPT_VERSION,
    )
    assert done.model_revision == "475b9088d29754a3379866cf5aeb6b41acd313c2"
    assert done.seconds == result["seconds"] and done.memory_gb == 6.1
    assert jobs.title_of(done) == "Testing gemma-4-e4b-it-4bit"
    model = await model_row(factory, model_id)
    assert model.measured_memory_gb == 6.1 and model.last_loaded_with == f"mlx-vlm/{VERSION}"
    # What the model was shown: the drawn picture, as a JPEG.
    body = site.server.requests[0]
    assert body["response_format"]["json_schema"]["name"] == "TestLook"


async def test_an_answer_in_prose_is_a_finding_not_an_error(factory: Factory, site: Site) -> None:
    model_id = await site.add(factory)
    site.server.prose_first(times=2)
    job_id = await queue(factory, "test", model_id=model_id)
    await drain(factory, site.deps())
    done = await row(factory, job_id)
    assert done.state == "done" and done.result is not None
    assert done.result["structured_ok"] is False and done.result["answer"] is None
    assert done.result["raw"] and done.result["attempts"] == 2
    assert (done.model, done.runtime, done.prompt_version) == (
        FIRST,
        "mlx-vlm",
        handlers.TEST_PROMPT_VERSION,
    )
    # What the runtime was seen to be is kept, though the answer was not usable.
    assert (await model_row(factory, model_id)).last_loaded_with == f"mlx-vlm/{VERSION}"


async def test_a_model_that_would_not_load_fails_with_its_last_words(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory)
    site.server.refuse("load_failed")
    job_id = await queue(factory, "test", model_id=model_id)
    await drain(factory, site.deps())
    failed = await row(factory, job_id)
    assert failed.state == "failed" and failed.message


async def test_a_model_that_does_not_read_pictures_is_given_words(
    factory: Factory, site: Site
) -> None:
    async with factory() as db:
        config = await load_config(db)
        config.day_budget_gb = 12.0
        await save_config(db, config)
    model_id = await site.add(factory, TEXT_ONLY)
    job_id = await queue(factory, "test", model_id=model_id)
    await drain(factory, site.deps())
    result = (await row(factory, job_id)).result
    assert result is not None and result["kind"] == "text"
    assert result["sample"]["label"] == handlers.WORDS
    assert len(result["answer"]["order"]) == 3
    content = site.server.requests[0]["messages"][0]["content"]
    assert [part["type"] for part in content] == ["text"]


async def test_a_test_shows_the_newest_photograph_in_the_catalogue(
    factory: Factory, site: Site
) -> None:
    from PIL import Image

    relative = "derivatives/ab/photo/preview.jpg"
    path = site.data / relative
    path.parent.mkdir(parents=True)
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 10, 10)).save(out, "JPEG")
    path.write_bytes(out.getvalue())
    async with factory() as db:
        shelf = Library(name="GELCO", root_path=str(site.data / "media"))
        db.add(shelf)
        await db.flush()
        photo = Asset(
            library_id=shelf.id,
            relative_path="DJI_0639.JPG",
            filename="DJI_0639.JPG",
            extension="jpg",
            media_type="image",
            size_bytes=1000,
            mtime=datetime(2026, 11, 1, tzinfo=UTC),
            processing_status="ready",
        )
        db.add(photo)
        await db.flush()
        db.add(
            Derivative(
                asset_id=photo.id,
                kind="preview",
                relative_path=relative,
                media_format="jpeg",
                status="ready",
            )
        )
        await db.commit()
        photo_id = photo.id
    model_id = await site.add(factory)
    job_id = await queue(factory, "test", model_id=model_id)
    chosen = await queue(
        factory, "test", model_id=model_id, payload={"asset_id": str(photo_id)}, unique=False
    )
    gone = await queue(
        factory, "test", model_id=model_id, payload={"asset_id": str(uuid.uuid4())}, unique=False
    )
    await drain(factory, site.deps())
    newest = (await row(factory, job_id)).result
    assert newest is not None and newest["sample"] == {
        "asset_id": str(photo_id),
        "media_type": "image",
        "filename": "DJI_0639.JPG",
        "label": handlers.NEWEST,
    }
    picked = (await row(factory, chosen)).result
    assert picked is not None and picked["sample"]["label"] == handlers.CHOSEN
    missing = await row(factory, gone)
    assert (missing.state, missing.message) == ("failed", handlers.NO_SUCH_PICTURE)


def test_the_drawn_picture_is_a_jpeg_a_model_may_be_shown() -> None:
    from framefound.local_models.ask import MAX_PICTURE_BYTES

    jpeg = handlers.drawn_picture()
    assert jpeg[:3] == b"\xff\xd8\xff" and len(jpeg) <= MAX_PICTURE_BYTES


def test_l1s_four_kinds_are_registered_and_only_the_test_needs_a_model() -> None:
    assert set(jobs.registered()) >= {"download", "place", "runtime_install", "test"}
    needs = {
        name: jobs._KINDS[name].needs_model
        for name in ("download", "place", "runtime_install", "test")
    }
    assert needs == {"download": False, "place": False, "runtime_install": False, "test": True}


async def test_progress_from_another_thread_reaches_the_row(
    factory: Factory, site: Site, quick: None
) -> None:
    lines: list[str] = []
    gate = threading.Event()

    async def install(ctx: jobs.JobContext, job: LocalAiJob) -> None:
        def work(_report: Callable[[str, int, int], None], _stop: Callable[[], bool]) -> None:
            ctx.say("Installing 0.7.13 — a few minutes…")
            gate.wait(5)

        waiting = asyncio.ensure_future(ctx.run_blocking(work))
        for _ in range(100):
            await asyncio.sleep(0.02)
            seen = await row(ctx.factory, job.id)
            if seen.message:
                lines.append(seen.message)
                break
        gate.set()
        await waiting

    kind("install_like", install)
    await queue(factory, "install_like")
    await drain(factory, site.deps())
    assert lines == ["Installing 0.7.13 — a few minutes…"]


async def test_a_job_row_is_found_by_its_newest_first(factory: Factory, site: Site) -> None:
    async def note(_ctx: jobs.JobContext, _job: LocalAiJob) -> None:
        return None

    kind("note", note)
    first = await queue(factory, "note")
    await drain(factory, site.deps())
    site.clock.advance(5)
    second = await queue(factory, "note")
    await drain(factory, site.deps())
    async with factory() as db:
        ordered = [job.id for job in await jobs.recent(db, limit=1)]
        everything = (await db.scalars(select(LocalAiJob.id))).all()
    assert ordered == [second] and set(everything) == {first, second}


# --- Cancel, whatever the work is doing when it is pressed ------------------------------


def tidying(name: str, handler: Callable[[jobs.JobContext, LocalAiJob], Any]) -> list[str]:
    """Register `name` with an `abandoned` hook; the reasons it was called with."""
    tidied: list[str] = []

    async def tidy(_db: AsyncSession, _job: LocalAiJob, why: jobs.Why) -> None:
        tidied.append(why)

    jobs.register(name, handler, needs_model=False, title=lambda job: job.subject, abandoned=tidy)
    return tidied


@pytest.mark.parametrize("ending", ["later", "not_now"])
async def test_a_cancel_the_handler_never_saw_still_ends_the_job(
    factory: Factory, site: Site, ending: str
) -> None:
    """Pressed while a Test waited inside ask() (it never looks), which then
    came back "busy"; or while a batch was on an item, which then gave way. The
    job does not go back to wait and run again as if nobody had pressed it."""
    started, pressed = asyncio.Event(), asyncio.Event()
    calls: list[int] = []

    async def held(_ctx: jobs.JobContext, _job: LocalAiJob) -> None:
        calls.append(1)
        started.set()
        await pressed.wait()
        if ending == "later":
            raise jobs.Later("A more urgent job went first.")
        raise NotNow("busy", "The model server is busy with another answer.")

    tidied = tidying("held", held)
    job_id = await queue(factory, "held")
    running = asyncio.ensure_future(jobs.run_next(factory, site.deps()))
    await started.wait()
    async with factory() as db:
        asked = await jobs.request_cancel(db, job_id)
        await db.commit()
    assert asked.state == "running" and asked.cancel_requested
    pressed.set()
    await asyncio.wait_for(running, timeout=10)

    ended = await row(factory, job_id)
    assert (ended.state, ended.message, ended.not_before) == ("cancelled", jobs.CANCELLED, None)
    assert ended.finished_at is not None
    assert tidied == ["cancelled"]
    site.clock.advance(3600)
    await drain(factory, site.deps())
    assert calls == [1]


async def test_a_cancel_left_on_a_queued_job_is_honoured_not_cleared(
    factory: Factory, site: Site
) -> None:
    """A row back in the queue with Cancel pressed (put there beside a run's
    end): the next claim ends it, rather than wiping the Cancel and running it."""
    ran: list[str] = []

    async def note(_ctx: jobs.JobContext, job: LocalAiJob) -> None:
        ran.append(job.subject)

    tidied = tidying("note", note)
    job_id = await queue(factory, "note")
    async with factory() as db:
        await db.execute(
            update(LocalAiJob).where(LocalAiJob.id == job_id).values(cancel_requested=True)
        )
        await db.commit()
    await drain(factory, site.deps())
    ended = await row(factory, job_id)
    assert (ended.state, ended.message, ran, tidied) == (
        "cancelled",
        jobs.CANCELLED,
        [],
        ["cancelled"],
    )


@pytest.mark.parametrize("blocked_in", ["run_blocking", "a_thread"])
async def test_a_cancel_the_work_cannot_answer_ends_the_job_and_frees_the_lane(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch, blocked_in: str
) -> None:
    """A read from a share that hung never returns and never looks at Cancel;
    the heartbeat alone would keep the row alive for ever, and the lane — one
    job at a time — would hold every download and test behind it."""
    monkeypatch.setattr(jobs, "HEARTBEAT_S", 0.01)
    monkeypatch.setattr(jobs, "PROGRESS_EVERY_S", 0.01)
    monkeypatch.setattr(jobs, "CANCEL_GRACE_S", 0.1)
    monkeypatch.setattr(jobs, "LET_GO_S", 0.05)
    hung = threading.Event()
    started = asyncio.Event()
    ran: list[str] = []

    async def stuck(ctx: jobs.JobContext, _job: LocalAiJob) -> dict[str, Any]:
        started.set()
        if blocked_in == "run_blocking":
            await ctx.run_blocking(lambda _report, _should_stop: hung.wait(30))
        else:
            await asyncio.to_thread(hung.wait, 30)  # no checkpoint at all
        return {}

    async def note(_ctx: jobs.JobContext, job: LocalAiJob) -> None:
        ran.append(job.subject)

    tidied = tidying("stuck", stuck)
    kind("note", note)
    stuck_id = await queue(factory, "stuck", priority=0)
    await queue(factory, "note", subject="behind it")
    try:
        running = asyncio.ensure_future(jobs.run_next(factory, site.deps()))
        await started.wait()
        await asyncio.sleep(0.05)
        # Without Cancel it is simply slow: still running, and alive.
        seen = await row(factory, stuck_id)
        assert jobs.shown(seen, now=site.clock()) == ("running", "")
        async with factory() as db:
            await jobs.request_cancel(db, stuck_id)
            await db.commit()
        await asyncio.wait_for(running, timeout=10)
        ended = await row(factory, stuck_id)
        assert (ended.state, ended.message) == ("cancelled", jobs.LEFT_BEHIND)
        assert tidied == ["cancelled"]
        await drain(factory, site.deps())
        assert ran == ["behind it"]
    finally:
        hung.set()


async def test_a_download_that_crashes_does_not_leave_the_model_downloading(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Something its handler did not expect (a folder it may not read): the kind
    still tidies up, so Test, a role and Remove are not refused for ever."""
    model_id = await site.add(factory, state=registry.INCOMPLETE, download=False)

    def denied(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(library, "download", denied)
    job_id = await queue(factory, "download", model_id=model_id)
    await drain(factory, site.deps())
    failed = await row(factory, job_id)
    assert (failed.state, failed.message) == ("failed", "It failed unexpectedly (PermissionError).")
    model = await model_row(factory, model_id)
    assert model.state == registry.INCOMPLETE
    assert model.error == (
        "It failed unexpectedly (PermissionError). What was downloaded is kept; Resume carries "
        "on from 0.0 GB."
    )


async def test_a_copy_that_crashes_stops_the_role_waiting(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    site.library_on_the_nas()
    old = await site.add(factory, TEXT_ONLY, role="editor")
    new = await site.add(factory)

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise KeyError("a bug")

    monkeypatch.setattr(library, "place", broken)
    await give(factory, "editor", new)
    await drain(factory, site.deps())
    async with factory() as db:
        editor = (await registry.roles(db))["editor"]
    # Nothing is left to cancel, so nothing may be left waiting: Remove would refuse.
    assert (editor.model_id, editor.pending_model_id) == (old, None)


# --- a share that does not answer -----------------------------------------------------


async def a_catalogued_photo(factory: Factory, site: Site) -> uuid.UUID:
    """A ready photograph with a ready preview, as the catalogue has it."""
    relative = "derivatives/cd/photo/preview.jpg"
    path = site.data / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(handlers.drawn_picture())
    async with factory() as db:
        shelf = Library(name="GELCO", root_path=str(site.data / "media"))
        db.add(shelf)
        await db.flush()
        photo = Asset(
            library_id=shelf.id,
            relative_path="DJI_0640.JPG",
            filename="DJI_0640.JPG",
            extension="jpg",
            media_type="image",
            size_bytes=1000,
            mtime=datetime(2026, 11, 1, tzinfo=UTC),
            processing_status="ready",
        )
        db.add(photo)
        await db.flush()
        db.add(
            Derivative(
                asset_id=photo.id,
                kind="preview",
                relative_path=relative,
                media_format="jpeg",
                status="ready",
            )
        )
        await db.commit()
        return photo.id


async def test_a_picture_store_that_does_not_answer_does_not_hold_a_test(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo_id = await a_catalogued_photo(factory, site)
    model_id = await site.add(factory)
    hung = threading.Event()

    def never_answers(*_args: Any) -> bytes:
        hung.wait(30)
        raise AssertionError("the store answered after all")

    monkeypatch.setattr(handlers, "SAMPLE_WAIT_S", 0.05)
    monkeypatch.setattr(handlers, "_read_picture", never_answers)
    newest = await queue(factory, "test", model_id=model_id)
    chosen = await queue(
        factory, "test", model_id=model_id, payload={"asset_id": str(photo_id)}, unique=False
    )
    try:
        await asyncio.wait_for(drain(factory, site.deps()), timeout=20)
    finally:
        hung.set()
    drawn = await row(factory, newest)
    assert drawn.state == "done" and drawn.result is not None
    assert drawn.result["sample"]["label"] == handlers.DRAWN
    refused = await row(factory, chosen)
    assert (refused.state, refused.message) == (
        "failed",
        f"The picture of DJI_0640.JPG could not be read: {handlers.SAMPLE_SILENT}",
    )


# --- a copy a Test was reading from ---------------------------------------------------


async def test_a_copy_kept_for_a_test_goes_when_the_test_ends(factory: Factory, site: Site) -> None:
    """Giving a role away leaves the old holder's copy while a Test reads from
    it (the page asks `jobs.in_use`); the Test removes it when it is over. A
    model that has a role keeps its copy."""
    site.library_on_the_nas()
    spare = await site.add(factory)
    editor = await site.add(factory, TEXT_ONLY, role="editor")
    spare_files = await site.files(factory, spare)
    editor_files = await site.files(factory, editor)
    library.place(spare_files, settings=site.settings)
    library.place(editor_files, settings=site.settings)
    spare_test = await queue(factory, "test", model_id=spare)

    async with factory() as db:
        assert not await jobs.in_use(db, spare, site.clock())
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == spare_test)
            .values(state="running", beat_at=utc(site.clock()))
        )
        await db.commit()
        assert await jobs.in_use(db, spare, site.clock())
        # A running job whose worker died is not using anything.
        assert not await jobs.in_use(db, spare, site.clock() + timedelta(minutes=5))
        await db.execute(
            update(LocalAiJob).where(LocalAiJob.id == spare_test).values(state="queued")
        )
        await db.commit()
    await queue(factory, "test", model_id=editor)
    await drain(factory, site.deps())

    assert (await row(factory, spare_test)).state == "done"
    assert not library.presence(spare_files, site.settings).on_this_machine
    assert library.presence(spare_files, site.settings).in_library
    assert library.presence(editor_files, site.settings).on_this_machine
