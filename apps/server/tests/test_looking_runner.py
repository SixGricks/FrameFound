"""The Looker's work on L1's lane (design L3 S6, S7): a folder review start
to finish (`look_folder`), making its clips ready (`look_prepare`), and new
footage looked at as it arrives (`look_ingest`, the pipeline's `nudge`, the
scanner's `ensure_ingest`).

The review is started as the API will start it (`reviews.start`) and run on
the real lane: `jobs.run_next` is awaited directly (no queue, no worker),
the real `ask()` stands in front of `fake.FakeModelServer`, and the Looker
is L1's fixture model, shrunk to a few kilobytes and placed in a library
under `tmp_path`. Nothing but `Deps` is handed in. Pictures are small JPEGs
at the paths the pipeline writes. Nothing starts a process or reaches a
network: trying to fails the test.

What happens "while the model answers" — a Cancel, a person's Test, the
clock passing 05:30, the worker dying — is done from the fake server's
`sleep`, which runs in the thread `ask` hands the request to, on the
test's own event loop.
"""

import ast
import asyncio
import json
import subprocess
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai.embeddings import EmbeddingResult
from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.models import (
    Asset,
    AssetTag,
    Derivative,
    Frame,
    Job,
    Library,
    LocalAiJob,
    LocalAiRole,
    LocalModel,
    Look,
    LookRequest,
    ShotWindow,
    Tag,
    User,
)
from framefound.local_models import (
    Availability,
    fake,
    jobs,
    library,
    preflight,
    registry,
    runtimes,
)
from framefound.local_models import settings_store as local_ai_settings
from framefound.local_models.ask import Answer, NotNow
from framefound.local_models.deps import Deps
from framefound.local_models.kinds import MlxVlm
from framefound.local_models.settings_store import LocalModelsConfig
from framefound.looking import PREPARE_EVERY_S, PREPARE_STALLED_S, demo, reviews, runner, store
from framefound.looking.prompt import PROMPT_VERSION
from framefound.processing import local_ai_tasks, shots
from framefound.processing import tasks as task_module
from framefound.scanner import __main__ as scanner
from framefound.storage import stores

Factory = async_sessionmaker[AsyncSession]

SECRET = "looking-runner-secret"
LOOKER = "mlx-community/gemma-4-e4b-it-4bit"
# Another build this Mac could be given as its Looker.
OTHER = "mlx-community/gemma-4-12B-it-OptiQ-4bit"
# One that reads pictures, so it may look at new footage (OTHER reads text only).
SECOND = "mlx-community/demo-large-4bit"
VERSION = "0.7.12"
# The Mac's wall clock, naive, as L1's Deps.now gives it.
AFTERNOON = datetime(2026, 11, 14, 14, 0)
NIGHT = datetime(2026, 11, 14, 23, 0)
MORNING = datetime(2026, 11, 15, 5, 40)
NEXT_EVENING = datetime(2026, 11, 15, 21, 31)
BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)
FOLDER = "Pete Dye"
WINDOWS = ((0, 4000), (4000, 9000))
KINDS = ("mlx-vlm",)
ANSWER = {
    "caption": "A green with a flag, seen from above.",
    "score": 82,
    "reason": "Steady, well exposed, nothing in the way.",
    "matches": [{"tag": "green", "confidence": 0.9}],
    "new_tags": [],
}

_OURS = (
    "FRAMEFOUND_BULK_DIR",
    "FRAMEFOUND_PROXY_DIR",
    "FRAMEFOUND_LOCAL_AI_DEMO",
    "FRAMEFOUND_DOMAIN",
)


class WorkerDied(BaseException):
    """The worker process went away mid-answer: nothing of the runner's own
    catches it, so the job row is left `running`, as after a real restart."""


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
    # Progress written, and Cancel looked for, at every step.
    monkeypatch.setattr(jobs, "PROGRESS_EVERY_S", 0.0)
    monkeypatch.setattr(local_ai_tasks, "wake", lambda _settings=None: True)
    fake.FakeModelServer.answer_for("LookAnswer", lambda _body: json.dumps(ANSWER))
    get_settings.cache_clear()
    stores._CACHE.clear()
    yield
    fake.FakeModelServer.answer_for("LookAnswer", None)
    get_settings.cache_clear()
    stores._CACHE.clear()


class Clock:
    """The Mac's wall clock, naive, moved by the test."""

    def __init__(self, at: datetime = AFTERNOON) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


class Site:
    """A Mac with MLX-VLM installed, a model library, a data folder and a Looker.

    `during[n]` runs inside the n-th question the Looker is asked (1-based,
    counting only the review's questions), in the server's thread; `soon`
    runs a coroutine on the test's loop from there and waits for it."""

    def __init__(self, tmp_path: Path, settings: Settings, loop: asyncio.AbstractEventLoop) -> None:
        self.settings = settings
        self.loop = loop
        self.hub = fake.FakeHub.from_fixtures(sleep=lambda _seconds: None)
        self.hub.shrink(LOOKER)
        self.clock = Clock()
        self.during: dict[int, Callable[[], None]] = {}
        self.looks_asked = 0
        self.server = fake.FakeModelServer(sleep=self._answering)
        self.server.slow(0.001)  # so `_answering` is called on every question
        runner_ = fake.FakeRunner(
            {
                "uv venv": (0, ""),
                "uv pip install": (0, ""),
                "uv pip freeze": (0, ""),
                "python": lambda argv: (0, f"{Path(argv[0]).parents[2].name}\n"),
            },
            make_venvs=True,
        )
        assert settings.runtimes_dir is not None
        runtimes.install(MlxVlm(), VERSION, settings.runtimes_dir, run=runner_)
        runtimes.activate(settings.runtimes_dir, "mlx-vlm", VERSION)

    def _answering(self, _seconds: float) -> None:
        body = self.server.requests[-1]
        if body["response_format"]["json_schema"]["name"] != "LookAnswer":
            return
        self.looks_asked += 1
        action = self.during.get(self.looks_asked)
        if action is not None:
            action()

    def soon(self, work: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(work, self.loop).result(timeout=60)

    def deps(self) -> Deps:
        return Deps(
            settings=self.settings,
            hub_for=self.hub.with_token,
            fetch_json=lambda _url, **_more: {},
            server=self.server,
            kinds={"mlx-vlm": MlxVlm()},
            sleep=lambda _seconds: None,
            now=self.clock,
            system="Darwin",
            machine="arm64",
        )

    async def looker(self, factory: Factory) -> uuid.UUID:
        """The model checked and added as the page would, its files in the
        library, given the Looker role."""
        return await self.model(factory, LOOKER, looker=True)

    async def model(self, factory: Factory, repo: str, *, looker: bool = False) -> uuid.UUID:
        """A model checked and added as the page would, its files in the library."""
        self.hub.shrink(repo)
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
            model.state = registry.INSTALLED
            if looker:
                await registry.set_role(db, "looker", model, pending=False, user_id=None)
            await db.commit()
            files = registry.snapshot(model)
        library.download(self.hub, files, settings=self.settings)
        return model.id


def _jpeg(path: Path, shade: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), (shade, 120, 255 - shade)).save(path, "JPEG", quality=90)


async def _asset(
    env: dict[str, Any],
    relative: str,
    media_type: str,
    derived: tuple[str, ...],
    *,
    windows: tuple[tuple[int, int], ...] = (),
    stills: bool = True,
) -> uuid.UUID:
    data: Path = env["settings"].data_dir
    async with env["factory"]() as db:
        asset = Asset(
            library_id=env["library"],
            relative_path=relative,
            filename=relative.rsplit("/", 1)[-1],
            extension=relative.rsplit(".", 1)[-1].lower(),
            media_type=media_type,
            size_bytes=1000,
            mtime=BASE,
            availability="online",
            first_indexed_at=BASE,
            duration_s=10.0 if media_type == "video" else None,
        )
        db.add(asset)
        await db.flush()
        for kind in derived:
            rel = f"derivatives/{asset.id}/{kind}.jpeg"
            db.add(
                Derivative(
                    asset_id=asset.id,
                    kind=kind,
                    relative_path=rel,
                    media_format="jpeg",
                    status="ready",
                )
            )
            if kind in ("preview", "thumbnail", "poster"):
                _jpeg(data / rel, 40)
        for idx, (start, end) in enumerate(windows):
            db.add(
                ShotWindow(
                    asset_id=asset.id,
                    idx=idx,
                    start_ms=start,
                    end_ms=end,
                    move="orbit",
                    light="golden",
                )
            )
            if stills:
                for moment in set(shots.window_moments(start, end)):
                    _jpeg(data / shots.still_relpath(asset.id, moment), 90 + idx)
        await db.commit()
        return asset.id


async def _photo(env: dict[str, Any], name: str) -> uuid.UUID:
    return await _asset(env, f"{FOLDER}/{name}", "image", ("thumbnail", "preview"))


async def _clip(env: dict[str, Any], name: str, *, stills: bool = True) -> uuid.UUID:
    return await _asset(
        env,
        f"{FOLDER}/{name}",
        "video",
        ("poster", "thumbnail", "proxy", "motion"),
        windows=WINDOWS,
        stills=stills,
    )


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'runner.db').as_posix()}"
    for folder in ("data", "models", "runtimes"):
        (tmp_path / folder).mkdir()
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("FRAMEFOUND_RUNTIMES_DIR", str(tmp_path / "runtimes"))
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", SECRET)
    monkeypatch.setenv("FRAMEFOUND_MIN_FREE_GB", "0")
    get_settings.cache_clear()
    settings = get_settings()
    engine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "connect")
    def _keys_are_kept(connection: Any, _record: Any) -> None:
        # PostgreSQL always enforces foreign keys; SQLite only when asked.
        # Without this a look written for a file removed a moment ago is
        # accepted here and refused in production.
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    site = Site(tmp_path, settings, asyncio.get_running_loop())
    model_id = await site.looker(factory)
    async with factory() as db:
        lib = Library(name="GELCO", root_path=str(tmp_path / "GELCO"))
        admin = User(email="owner@example.com", password_hash="x", role="admin")
        db.add_all([lib, admin])
        await db.commit()
    env: dict[str, Any] = {
        "factory": factory,
        "site": site,
        "settings": settings,
        "library": lib.id,
        "admin": admin,
        "model_id": model_id,
    }
    # The catalogue's vocabulary: "green", confirmed on a photograph elsewhere.
    elsewhere = await _asset(env, "Other/IMG_9000.JPG", "image", ())
    async with factory() as db:
        tag = Tag(name="green", slug="green")
        db.add(tag)
        await db.flush()
        db.add(AssetTag(asset_id=elsewhere, tag_id=tag.id, source="confirmed"))
        await db.commit()
    env["clip"] = await _clip(env, "DJI_0001.MP4")
    env["photos"] = [await _photo(env, "IMG_0001.JPG"), await _photo(env, "IMG_0002.JPG")]
    yield env
    await engine.dispose()
    get_settings.cache_clear()


# --- helpers ---------------------------------------------------------------------------


async def _start(
    env: dict[str, Any],
    *,
    when: str = "now",
    redo: bool = False,
    folder: str = FOLDER,
    prepare: bool = True,
) -> uuid.UUID:
    async with env["factory"]() as db:
        request = await reviews.start(
            db,
            env["admin"],
            library_id=env["library"],
            folder=folder,
            when=when,  # type: ignore[arg-type]
            model_id=None,
            redo=redo,
            prepare=prepare,
            now=env["site"].clock(),
            known_kinds=KINDS,
        )
        await db.commit()
        return request.id


async def _drain(env: dict[str, Any], limit: int = 20) -> None:
    """What the lane does when woken: run jobs until none is queued."""
    for _ in range(limit):
        if not await jobs.run_next(env["factory"], env["site"].deps()):
            return
    raise AssertionError("the lane never ran out of work")


async def _request(env: dict[str, Any], request_id: uuid.UUID) -> LookRequest:
    async with env["factory"]() as db:
        found = await db.get(LookRequest, request_id)
        assert found is not None
        return found


async def _job(env: dict[str, Any], job_id: uuid.UUID | None) -> LocalAiJob:
    assert job_id is not None
    async with env["factory"]() as db:
        found = await db.get(LocalAiJob, job_id)
        assert found is not None
        return found


async def _look_jobs(env: dict[str, Any]) -> int:
    async with env["factory"]() as db:
        found = await db.scalar(
            select(func.count()).select_from(LocalAiJob).where(LocalAiJob.kind == "look_folder")
        )
        return int(found or 0)


async def _shown(env: dict[str, Any], request_id: uuid.UUID) -> tuple[str, str]:
    request = await _request(env, request_id)
    look_job = await _job(env, request.look_job_id)
    return reviews.shown(request, look_job, None, env["site"].clock())


async def _looks(env: dict[str, Any], *, ok: bool | None = None) -> list[Look]:
    async with env["factory"]() as db:
        query = select(Look).order_by(Look.created_at)
        if ok is not None:
            query = query.where(Look.ok.is_(ok))
        return list((await db.scalars(query)).all())


def _questions(env: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        body
        for body in env["site"].server.requests
        if body["response_format"]["json_schema"]["name"] == "LookAnswer"
    ]


# --- a review, start to finish -----------------------------------------------------------


async def test_a_review_looks_at_everything_new_and_then_is_done(env: dict[str, Any]) -> None:
    request_id = await _start(env)
    await _drain(env)

    request = await _request(env, request_id)
    assert (request.state, request.done, request.total, request.failed) == ("done", 4, 4, 0)
    assert request.message == "Finished: 4 of 4 were looked at."
    assert await _shown(env, request_id) == ("done", request.message)
    job = await _job(env, request.look_job_id)
    assert job.state == "done"
    assert job.result == {"looked": 4, "failed": 0, "tags": 3, "new_tags": 0, "held": 0}
    assert job.model == LOOKER and job.prompt_version == PROMPT_VERSION  # who answered

    # Two photographs and two shots, each asked once, from its own pictures,
    # and never told the folder's or the file's name.
    questions = _questions(env)
    assert len(questions) == 4
    pictures = sorted(
        sum(part["type"] != "text" for part in body["messages"][0]["content"]) for body in questions
    )
    assert pictures == [1, 1, 3, 3]
    said = json.dumps(questions)
    assert FOLDER not in said and "IMG_0001" not in said and "DJI_0001" not in said

    async with env["factory"]() as db:
        model = await registry.get_model(db, env["model_id"])
        assert model is not None
        key = store.key_for(model)
        for asset_id in [env["clip"], *env["photos"]]:
            asset = await db.get(Asset, asset_id)
            assert asset is not None and asset.looked_at is not None
            assert (asset.looked_by, asset.look_score) == (key, 82)
        rows = (
            await db.execute(
                select(AssetTag.asset_id, AssetTag.source, AssetTag.origin, AssetTag.at_ms)
                .join(Tag)
                .where(Tag.slug == "green", AssetTag.source == "suggested")
            )
        ).all()
    # Suggestions from the local model, never facts; the clip's opens at its shot.
    assert {(r.asset_id, r.origin, r.at_ms) for r in rows} == {
        (env["photos"][0], "local", None),
        (env["photos"][1], "local", None),
        (env["clip"], "local", 2000),
    }


async def test_asking_again_finds_nothing_new(env: dict[str, Any]) -> None:
    await _start(env)
    await _drain(env)
    for redo in (False, True):  # the same model and question: redo adds nothing either
        with pytest.raises(reviews.ReviewRefused) as refused:
            await _start(env, redo=redo)
        assert str(refused.value) == reviews.NOTHING_NEW
    assert len(_questions(env)) == 4


async def test_a_review_carries_on_where_it_stopped_after_a_restart(env: dict[str, Any]) -> None:
    site: Site = env["site"]

    def die() -> None:
        raise WorkerDied()

    site.during[2] = die
    request_id = await _start(env)
    with pytest.raises(WorkerDied):
        await jobs.run_next(env["factory"], site.deps())
    request = await _request(env, request_id)
    assert (await _job(env, request.look_job_id)).state == "running"
    assert request.done == 1

    # Two minutes later the next tick finds it stopped, and runs the same row again.
    site.clock.advance(jobs.STALE_AFTER_S + 10)
    await _drain(env)
    request = await _request(env, request_id)
    assert (request.state, request.done, request.failed) == ("done", 4, 0)
    # The first item was not asked again; the one cut off was.
    assert len(_questions(env)) == 5
    assert len(await _looks(env)) == 4
    assert await _look_jobs(env) == 1


async def test_a_review_that_kills_its_worker_every_time_is_not_restarted_for_ever(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]

    def die() -> None:
        raise WorkerDied()

    site.during[2] = die
    site.during[3] = die  # the same item again, straight after the restart
    request_id = await _start(env)
    for _ in range(2):
        with pytest.raises(WorkerDied):
            await jobs.run_next(env["factory"], site.deps())
        site.clock.advance(jobs.STALE_AFTER_S + 10)
    await _drain(env)
    request = await _request(env, request_id)
    job = await _job(env, request.look_job_id)
    assert (job.state, job.message) == ("failed", jobs.STOPPED)
    assert await _shown(env, request_id) == ("failed", jobs.STOPPED)
    assert len(_questions(env)) == 3


async def test_a_tonight_review_stops_when_the_window_closes_even_if_the_model_fits_by_day(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    site.clock.at = NIGHT

    def morning() -> None:
        site.clock.at = MORNING

    site.during[1] = morning
    request_id = await _start(env, when="tonight")
    await _drain(env)

    request = await _request(env, request_id)
    job = await _job(env, request.look_job_id)
    said = "Stopped for the day at 05:30 — 1 of 4 done. Carries on at 21:30."
    assert (job.state, job.message) == ("waiting", said)
    assert await _shown(env, request_id) == ("waiting", said)
    assert len(_questions(env)) == 1

    site.clock.advance(3600)  # still the day: nothing runs
    await _drain(env)
    assert len(_questions(env)) == 1

    site.clock.at = NEXT_EVENING
    await _drain(env)
    request = await _request(env, request_id)
    assert (request.state, request.done) == ("done", 4)
    assert await _look_jobs(env) == 1


async def test_a_paused_review_waits_and_says_so(env: dict[str, Any]) -> None:
    async with env["factory"]() as db:
        config = await local_ai_settings.load_config(db)
        await local_ai_settings.save_config(db, replace(config, paused=True))
    request_id = await _start(env)
    await _drain(env)
    assert await _shown(env, request_id) == ("paused", reviews.PAUSED_SHOWN)
    assert _questions(env) == []

    async with env["factory"]() as db:
        config = await local_ai_settings.load_config(db)
        await local_ai_settings.save_config(db, replace(config, paused=False))
        await jobs.release_waiting(db, env["site"].clock())
        await db.commit()
    await _drain(env)
    assert (await _request(env, request_id)).state == "done"


async def test_a_more_urgent_job_runs_between_two_items(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    made: list[uuid.UUID] = []

    async def ask_for_a_test() -> None:
        async with env["factory"]() as db:
            test = await jobs.create(
                db, "test", subject=f"{LOOKER}@test", model_id=env["model_id"], priority=0
            )
            await db.commit()
            made.append(test.id)

    site.during[1] = lambda: site.soon(ask_for_a_test())
    request_id = await _start(env)
    request = await _request(env, request_id)

    assert await jobs.run_next(env["factory"], site.deps()) is True
    review = await _job(env, request.look_job_id)
    assert (review.state, review.message) == ("queued", runner.STEPPED_ASIDE)
    assert (await _request(env, request_id)).done == 1

    await jobs.run_next(env["factory"], site.deps())  # the Test, between two items
    assert (await _job(env, made[0])).state == "done"
    assert (await _job(env, request.look_job_id)).state == "queued"

    await _drain(env)
    assert (await _request(env, request_id)).state == "done"
    assert len(_questions(env)) == 4
    assert await _look_jobs(env) == 1


async def test_one_bad_item_does_not_stop_the_review(env: dict[str, Any]) -> None:
    env["site"].server.refuse("died")
    request_id = await _start(env)
    await _drain(env)

    request = await _request(env, request_id)
    assert (request.state, request.done, request.failed) == ("done", 3, 1)
    assert request.message == (
        "Finished: 3 of 4 were looked at. 1 could not be looked at; reviewing the folder "
        "again tries them again."
    )
    (failure,) = await _looks(env, ok=False)
    assert failure.note.startswith("The model stopped unexpectedly")

    # A new review is how the failed item is tried again.
    again = await _start(env)
    await _drain(env)
    assert (await _request(env, again)).done == 1


async def test_five_failures_in_a_row_stop_it_with_the_reason(env: dict[str, Any]) -> None:
    for name in ("IMG_0003.JPG", "IMG_0004.JPG"):
        await _photo(env, name)
    env["site"].server.refuse("died", times=5)
    request_id = await _start(env)
    await _drain(env)

    request = await _request(env, request_id)
    (first, *_rest) = await _looks(env, ok=False)
    said = runner.in_a_row(first.note)
    assert said.startswith("Stopped: the model failed on 5 items in a row — The model stopped")
    assert (request.state, request.message, request.failed) == ("failed", said, 5)
    job = await _job(env, request.look_job_id)
    assert (job.state, job.message) == ("failed", said)
    assert len(_questions(env)) == 5  # the sixth item was never asked


async def test_a_model_that_will_not_load_stops_the_review_at_once(env: dict[str, Any]) -> None:
    env["site"].server.refuse("load_failed")
    request_id = await _start(env)
    await _drain(env)
    request = await _request(env, request_id)
    assert request.state == "failed"
    assert request.message == runner.could_not_use("It would not load.")
    # Not the item's fault: nothing is recorded against it, so it is asked again later.
    assert await _looks(env) == []
    assert len(_questions(env)) == 1


async def test_a_looker_whose_program_is_not_installed_fails_the_review_saying_so(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that can never run as things stand (its program is not
    installed) ends the review `failed` with the reason, at once. The
    rollback before that write expires the review row; reading its id from
    it then would be I/O the async session cannot do, and the review was
    left reading "active" for ever."""
    said = "Demo llama.cpp is not installed. Install it under Software that runs models."

    async def no_program(*_args: Any, **_kwargs: Any) -> Answer:
        raise NotNow("no_runtime", said)

    monkeypatch.setattr(runner, "ask", no_program)
    request_id = await _start(env)
    await _drain(env)
    request = await _request(env, request_id)
    assert (request.state, request.message) == ("failed", said)
    assert request.finished_at is not None
    assert (await _job(env, request.look_job_id)).state == "failed"
    assert await _looks(env) == []


async def test_a_shot_with_no_still_is_recorded_as_not_looked_at(env: dict[str, Any]) -> None:
    bare = await _clip(env, "DJI_0002.MP4", stills=False)
    request_id = await _start(env)
    await _drain(env)

    request = await _request(env, request_id)
    assert (request.state, request.done, request.failed) == ("done", 4, 2)
    async with env["factory"]() as db:
        notes = (await db.scalars(select(Look.note).where(Look.asset_id == bare))).all()
        clip = await db.get(Asset, bare)
    assert notes == [runner.NO_SHOT_PICTURE] * 2
    assert clip is not None and clip.looked_at is None
    assert len(_questions(env)) == 4  # the model was never shown nothing


async def test_cancel_keeps_what_was_done(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    request_id = await _start(env)

    async def cancel() -> None:
        async with env["factory"]() as db:
            await reviews.cancel(db, request_id)
            await db.commit()

    site.during[1] = lambda: site.soon(cancel())
    await _drain(env)

    request = await _request(env, request_id)
    assert (await _job(env, request.look_job_id)).state == "cancelled"
    assert await _shown(env, request_id) == (
        "cancelled",
        "Cancelled. 1 of 4 were looked at; their results are kept.",
    )
    (kept,) = await _looks(env)
    assert kept.ok and kept.current
    assert len(_questions(env)) == 1


async def test_a_long_answer_does_not_make_the_job_look_dead(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One answer outlasting the "worker died" limit: the runner's heartbeat
    keeps the row alive; the handler does nothing for it."""
    monkeypatch.setattr(jobs, "HEARTBEAT_S", 0.01)
    site: Site = env["site"]
    request_id = await _start(env)
    request = await _request(env, request_id)
    seen: list[str] = []
    reaped: list[bool] = []

    def slow_answer() -> None:
        for _ in range(3):
            site.clock.advance(jobs.STALE_AFTER_S * 0.8)
            time.sleep(0.2)  # the answer is still being written
            row = site.soon(_job(env, request.look_job_id))
            seen.append(jobs.shown(row, site.clock())[0])
        # A second runner (a tick from beat) looks meanwhile, and reaps nothing.
        reaped.append(site.soon(jobs.run_next(env["factory"], site.deps())))

    site.during[1] = slow_answer
    await _drain(env)
    assert seen == ["running"] * 3
    assert reaped == [False]
    assert (await _request(env, request_id)).state == "done"


async def test_a_review_whose_looker_was_taken_away_fails_saying_so(env: dict[str, Any]) -> None:
    request_id = await _start(env)
    async with env["factory"]() as db:
        await db.execute(delete(LocalAiRole))
        await db.commit()
    await _drain(env)
    request = await _request(env, request_id)
    assert (request.state, request.message) == ("failed", reviews.NO_LOOKER)
    assert (await _job(env, request.look_job_id)).state == "failed"
    assert _questions(env) == []


async def test_a_file_removed_while_the_model_answers_leaves_nothing_of_it_behind(
    env: dict[str, Any],
) -> None:
    """The scanner finds a file gone and removes its asset while the model is
    still answering about it. The database refuses the look (the fixture
    keeps foreign keys, as PostgreSQL does); the review starts again from
    the top on the same row and finishes with what is left."""
    site: Site = env["site"]
    gone = env["photos"][0]

    async def remove() -> None:
        async with env["factory"]() as db:
            await db.execute(delete(Asset).where(Asset.id == gone))
            await db.commit()

    # In path order: the clip's two shots, then the photographs.
    site.during[3] = lambda: site.soon(remove())
    request_id = await _start(env)
    await _drain(env)

    request = await _request(env, request_id)
    assert (request.state, request.done, request.failed, request.total) == ("done", 3, 0, 3)
    assert request.message == "Finished: 3 of 3 were looked at."
    assert len(_questions(env)) == 4  # the answer about the removed file was thrown away
    looks = await _looks(env)
    assert len(looks) == 3 and gone not in {look.asset_id for look in looks}
    async with env["factory"]() as db:
        left = await db.scalar(
            select(func.count()).select_from(AssetTag).where(AssetTag.asset_id == gone)
        )
    assert left == 0
    assert await _look_jobs(env) == 1
    assert (await _job(env, request.look_job_id)).state == "done"


async def test_an_answer_from_a_model_that_is_no_longer_the_looker_is_not_recorded(
    env: dict[str, Any],
) -> None:
    """The Looker role is given to another model while a review runs. The
    next answer comes from the new one, under a key this run does not ask
    with: it is not recorded, and the review starts again from the top under
    the new Looker, on the same row."""
    site: Site = env["site"]
    other = await site.model(env["factory"], OTHER)

    async def hand_over() -> None:
        async with env["factory"]() as db:
            model = await registry.get_model(db, other)
            assert model is not None
            await registry.set_role(db, "looker", model, pending=False, user_id=None)
            await db.commit()

    site.during[1] = lambda: site.soon(hand_over())
    request_id = await _start(env)
    await _drain(env)

    request = await _request(env, request_id)
    assert (request.state, request.done, request.failed) == ("done", 4, 0)
    async with env["factory"]() as db:
        keys = {}
        for model_id in (env["model_id"], other):
            model = await registry.get_model(db, model_id)
            assert model is not None
            keys[model.repo_id] = store.key_for(model)
    looks = await _looks(env)
    # The first answer was the old Looker's, asked while it still was; the
    # other three are the new one's, each stored under its own key.
    assert sorted((look.model, look.key) for look in looks) == sorted(
        [(LOOKER, keys[LOOKER])] + [(OTHER, keys[OTHER])] * 3
    )
    # Five questions for four looks: the one answered across the change was asked again.
    assert len(_questions(env)) == 5
    assert await _look_jobs(env) == 1


async def test_one_row_of_work_carries_a_review_through_a_yield_a_restart_and_a_night(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    site.clock.at = NIGHT

    async def ask_for_a_test() -> None:
        async with env["factory"]() as db:
            await jobs.create(
                db, "test", subject=f"{LOOKER}@test", model_id=env["model_id"], priority=0
            )
            await db.commit()

    def die() -> None:
        raise WorkerDied()

    def morning() -> None:
        site.clock.at = MORNING

    site.during[1] = lambda: site.soon(ask_for_a_test())
    site.during[2] = die
    site.during[4] = morning
    request_id = await _start(env, when="tonight")
    counts: list[int] = []

    with pytest.raises(WorkerDied):
        await _drain(env)  # item 1; steps aside; the Test; item 2 is cut off
    counts.append(await _look_jobs(env))
    site.clock.advance(jobs.STALE_AFTER_S + 10)
    await _drain(env)  # restarted: items 2 and 3; the window closes
    counts.append(await _look_jobs(env))
    request = await _request(env, request_id)
    assert (await _job(env, request.look_job_id)).state == "waiting"
    site.clock.at = NEXT_EVENING
    await _drain(env)
    counts.append(await _look_jobs(env))

    request = await _request(env, request_id)
    assert (request.state, request.done) == ("done", 4)
    assert counts == [1, 1, 1]


async def test_in_demo_mode_the_demo_servers_answers_become_made_up_looks(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """D25: on the demo's lane (and only there) an answer the demo server gave
    is replaced by `looking.demo`'s look: stock words, a reason that says so,
    tags only from the vocabulary, and the look marked made up."""
    monkeypatch.setattr(runner, "availability", lambda _settings=None: Availability("demo"))
    request_id = await _start(env)
    await _drain(env)
    assert (await _request(env, request_id)).state == "done"
    looks = await _looks(env, ok=True)
    assert len(looks) == 4
    for look in looks:
        assert look.demo and look.reason == demo.REASON
        assert look.caption in (
            demo.SHOT_CAPTIONS if look.start_ms is not None else demo.PHOTO_CAPTIONS
        )
        assert {tag["name"] for tag in (look.answer or {})["tags"]} <= {"green"}


def test_the_worker_that_runs_the_lane_knows_the_look_kind() -> None:
    # This file imports the runner itself, so every other test here passes
    # whether or not the worker does; without the lane's import the Mac's
    # worker ends every review "does not know how to do this".
    source = Path(local_ai_tasks.__file__).read_text(encoding="utf-8")
    imported = {
        (node.module, alias.name)
        for node in ast.parse(source).body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert ("framefound.looking", "runner") in imported
    assert {"look_folder", "look_ingest", "look_prepare"} <= set(jobs.registered())


# --- making clips ready ----------------------------------------------------------------


class Pipeline:
    """The ordinary pipeline's queues, as `.delay` sees them: what was asked for."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.copies: list[tuple[str, bool]] = []
        self.analyses: list[str] = []
        self.down = False
        self.depth: int | None = 0

        def copy(asset_id: str, requested: bool = False) -> None:
            if self.down:
                raise ConnectionError("the broker went away")
            self.copies.append((asset_id, requested))

        def analyse(asset_id: str) -> None:
            if self.down:
                raise ConnectionError("the broker went away")
            self.analyses.append(asset_id)

        monkeypatch.setattr(task_module.generate_proxy, "delay", copy)
        monkeypatch.setattr(task_module.analyze_motion, "delay", analyse)
        monkeypatch.setattr(task_module.describe_shots, "delay", lambda _asset_id: None)
        monkeypatch.setattr(runner, "queue_depth", lambda _name, _settings=None: self.depth)


async def _unready(env: dict[str, Any]) -> tuple[uuid.UUID, uuid.UUID]:
    """A clip with only its poster (it needs a playable copy) and one with a
    copy whose shots have not been found."""
    copy = await _asset(env, f"{FOLDER}/DJI_0003.MP4", "video", ("poster", "thumbnail"))
    shotless = await _asset(
        env, f"{FOLDER}/DJI_0004.MP4", "video", ("poster", "thumbnail", "proxy")
    )
    return copy, shotless


async def _made_ready(env: dict[str, Any], asset_id: uuid.UUID) -> None:
    """What the pipeline leaves once a clip has its copy and its shots."""
    data: Path = env["settings"].data_dir
    async with env["factory"]() as db:
        kinds = set(
            (await db.scalars(select(Derivative.kind).where(Derivative.asset_id == asset_id))).all()
        )
        for kind in ("proxy", "motion"):
            if kind not in kinds:
                db.add(
                    Derivative(
                        asset_id=asset_id,
                        kind=kind,
                        relative_path=f"derivatives/{asset_id}/{kind}.jpeg",
                        media_format="jpeg",
                        status="ready",
                    )
                )
        for idx, (start, end) in enumerate(WINDOWS):
            db.add(ShotWindow(asset_id=asset_id, idx=idx, start_ms=start, end_ms=end, move="pan"))
            for moment in set(shots.window_moments(start, end)):
                _jpeg(data / shots.still_relpath(asset_id, moment), 60 + idx)
        await db.commit()


async def _prepare_job(env: dict[str, Any], request_id: uuid.UUID) -> LocalAiJob:
    request = await _request(env, request_id)
    return await _job(env, request.prepare_job_id)


async def test_clips_are_queued_for_a_copy_once_and_only_once(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = Pipeline(monkeypatch)
    copy, shotless = await _unready(env)
    request_id = await _start(env)
    await _drain(env)

    # The copy is asked for by name (made even where the library has copies
    # off); its arrival queues its own analysis, so only the other is analysed.
    assert pipeline.copies == [(str(copy), True)]
    assert pipeline.analyses == [str(shotless)]
    request = await _request(env, request_id)
    assert (request.prep_state, request.prep_total, request.prep_ready) == ("running", 2, 0)
    assert request.prep_queued_at is not None
    preparing = await _prepare_job(env, request_id)
    assert (preparing.state, preparing.message) == ("waiting", "Getting 2 clips ready (2 to go).")

    for _ in range(3):  # it looks again, and again: nothing is asked for twice
        env["site"].clock.advance(runner.PREPARE_WAIT.total_seconds() + 1)
        await _drain(env)
    assert pipeline.copies == [(str(copy), True)]
    assert pipeline.analyses == [str(shotless)]
    assert (await _prepare_job(env, request_id)).state == "waiting"


async def test_looking_starts_with_what_is_ready_and_waits_for_the_rest(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    Pipeline(monkeypatch)
    copy, shotless = await _unready(env)
    request_id = await _start(env)
    await _drain(env)

    # Everything already ready was looked at meanwhile; the review waits.
    request = await _request(env, request_id)
    assert (request.state, request.done) == ("active", 4)
    look_job = await _job(env, request.look_job_id)
    assert (look_job.state, look_job.message) == ("waiting", runner.waiting_for_clips(2))
    assert len(_questions(env)) == 4

    await _made_ready(env, shotless)
    env["site"].clock.advance(runner.PREPARE_WAIT.total_seconds() + 1)
    await _drain(env)
    request = await _request(env, request_id)
    assert (request.state, request.done, request.prep_ready) == ("active", 6, 1)
    assert (await _prepare_job(env, request_id)).message == "Getting 2 clips ready (1 to go)."

    await _made_ready(env, copy)
    env["site"].clock.advance(runner.PREPARE_WAIT.total_seconds() + 1)
    await _drain(env)
    request = await _request(env, request_id)
    assert (request.state, request.done, request.failed, request.stills_only) == ("done", 8, 0, 0)
    assert (request.prep_state, request.prep_ready, request.prep_gave_up) == ("done", 2, 0)
    assert request.message == "Finished: 8 of 8 were looked at."
    assert (await _prepare_job(env, request_id)).state == "done"
    # Every shot of the clips made ready was asked about, each once.
    assert len(_questions(env)) == 8


async def test_clips_that_never_become_ready_are_judged_from_one_still_and_counted(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = Pipeline(monkeypatch)
    copy = await _asset(env, f"{FOLDER}/DJI_0003.MP4", "video", ("poster", "thumbnail"))
    request_id = await _start(env)
    await _drain(env)
    site: Site = env["site"]

    # Hours with nothing made ready, but the queue's depth cannot be read:
    # the clip may still be on its way, so it is not given up on.
    pipeline.depth = None
    site.clock.advance(PREPARE_STALLED_S + 60)
    await _drain(env)
    request = await _request(env, request_id)
    assert (request.state, request.prep_state) == ("active", "running")

    # Nothing waits on the queues either: it cannot be made ready.
    pipeline.depth = 0
    site.clock.advance(runner.PREPARE_WAIT.total_seconds() + 1)
    await _drain(env)
    request = await _request(env, request_id)
    assert (request.prep_state, request.prep_gave_up) == ("done", 1)
    assert (request.state, request.done, request.stills_only) == ("done", 5, 1)
    assert request.message == (
        "Finished: 5 of 5 were looked at. 1 clip could not be made ready and was judged "
        "from one still each."
    )
    async with env["factory"]() as db:
        (whole,) = (await db.scalars(select(Look).where(Look.asset_id == copy))).all()
    assert (whole.start_ms, whole.end_ms, whole.ok) == (None, None, True)
    # From its one poster, not three stills.
    assert sorted(
        sum(part["type"] != "text" for part in body["messages"][0]["content"])
        for body in _questions(env)
    ) == [1, 1, 1, 3, 3]


async def test_a_queue_that_is_down_judges_the_clips_from_one_still(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = Pipeline(monkeypatch)
    pipeline.down = True
    await _asset(env, f"{FOLDER}/DJI_0003.MP4", "video", ("poster", "thumbnail"))
    request_id = await _start(env)
    await _drain(env)

    preparing = await _prepare_job(env, request_id)
    assert (preparing.state, preparing.message) == ("failed", runner.QUEUE_DOWN)
    request = await _request(env, request_id)
    assert (request.state, request.prep_state, request.stills_only, request.done) == (
        "done",
        "done",
        1,
        5,
    )


class AskedForEver(BaseException):
    """The model was asked more questions than the folder holds items: the
    review would never end. Raised from inside the question so the test
    fails instead of hanging."""


def _watch(env: dict[str, Any], request_id: list[uuid.UUID], items: int) -> list[tuple[int, int]]:
    """What the review's own row says while each question is asked (done,
    failed, total), and a stop once more questions are asked than `items`."""
    site: Site = env["site"]
    seen: list[tuple[int, int]] = []

    def note() -> None:
        request = site.soon(_request(env, request_id[0]))
        seen.append((request.done + request.failed, request.total))

    def stop() -> None:
        raise AskedForEver()

    for n in range(1, items + 1):
        site.during[n] = note
    for n in range(items + 1, items + 40):
        site.during[n] = stop
    return seen


def _progress(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Every (done, total) a look says on its progress bar, as it says it —
    including the moment just after an answer is recorded, which the
    review's own row is never read at."""
    said: list[tuple[int, int]] = []
    original = jobs.JobContext.progress

    async def spy(
        self: jobs.JobContext, *, phase: str = "", done: int = 0, total: int = 0, message: str = ""
    ) -> None:
        if phase == "looking":
            said.append((done, total))
        await original(self, phase=phase, done=done, total=total, message=message)

    monkeypatch.setattr(jobs.JobContext, "progress", spy)
    return said


async def _more_urgent(env: dict[str, Any], made: list[uuid.UUID]) -> None:
    """A Test of the Looker queued ahead of everything: a review steps aside for it."""
    async with env["factory"]() as db:
        test = await jobs.create(
            db, "test", subject=f"{LOOKER}@test", model_id=env["model_id"], priority=0
        )
        await db.commit()
        made.append(test.id)


async def test_a_clip_with_shots_on_record_but_no_ready_analysis_file_is_looked_at_once_by_shot(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Seen on a demonstration copy: clips with shot windows but no ready
    # analysis file were judged from one still in the last pass, that look
    # did not count for a clip with shots, and the review asked about them
    # again and again ("20 of 8") and never ended. Such a clip is made ready
    # first like any other (analysed again, or copied first); should it
    # never become ready — or the review not make clips ready at all — its
    # shots on record are what is looked at in the last pass, each once.
    pipeline = Pipeline(monkeypatch)
    site: Site = env["site"]
    no_copy = await _asset(
        env, f"{FOLDER}/DJI_0440_D.mp4", "video", ("poster", "thumbnail"), windows=WINDOWS
    )
    no_file = await _asset(
        env, f"{FOLDER}/DJI_0445_D.mp4", "video", ("poster", "thumbnail", "proxy"), windows=WINDOWS
    )
    failed_file = await _asset(
        env, f"{FOLDER}/DJI_0446_D.mp4", "video", ("poster", "proxy"), windows=WINDOWS
    )
    async with env["factory"]() as db:
        db.add(
            Derivative(
                asset_id=failed_file,
                kind="motion",
                relative_path=f"derivatives/{failed_file}/motion.json",
                media_format="json",
                status="failed",
            )
        )
        await db.commit()
    # Two photographs and the ready clip's two shots; then two shots each of three.
    ready, items = 4, 10
    request_ids: list[uuid.UUID] = []
    seen = _watch(env, request_ids, items)

    for prepare in (True, False):
        site.looks_asked = 0
        seen.clear()
        request_ids[:] = [await _start(env, prepare=prepare)]
        try:
            await _drain(env)
            if prepare:
                # They are sent to be made ready, and the review waits.
                assert pipeline.copies == [(str(no_copy), True)]
                assert pipeline.analyses == [str(no_file), str(failed_file)]
                request = await _request(env, request_ids[0])
                assert (request.state, request.done, request.total) == ("active", ready, ready)
                # Hours with nothing made ready and nothing on the queues.
                site.clock.advance(PREPARE_STALLED_S + 60)
                await _drain(env)
                site.clock.advance(runner.PREPARE_WAIT.total_seconds() + 1)
                await _drain(env)
        except AskedForEver:
            pytest.fail(f"the review asked about the same items again (prepare={prepare})")

        request = await _request(env, request_ids[0])
        assert (request.state, request.done, request.total, request.failed) == (
            "done",
            items,
            items,
            0,
        ), prepare
        # By their shots, not from one still each.
        assert request.stills_only == 0
        assert request.message == f"Finished: {items} of {items} were looked at."
        assert all(progress <= total for progress, total in seen), seen
        if prepare:
            assert (request.prep_state, request.prep_gave_up) == ("done", 3)
        else:
            assert request.prepare_job_id is None
        async with env["factory"]() as db:
            for clip in (no_copy, no_file, failed_file):
                looks = await store.current(db, clip)
                assert sorted(looks) == list(WINDOWS), clip
                asset = await db.get(Asset, clip)
                assert asset is not None and asset.looked_at is not None
            # Undo it, so the second review (nothing made ready) has it all to do.
            await db.execute(delete(AssetTag).where(AssetTag.source == "suggested"))
            await db.execute(delete(Look))
            await db.execute(update(Asset).values(looked_at=None, looked_by=None))
            await db.commit()
        pipeline.copies.clear()
        pipeline.analyses.clear()
    # Not made ready the second time, so nothing was asked of the pipeline.
    assert (pipeline.copies, pipeline.analyses) == ([], [])
    assert len(_questions(env)) == 2 * items


async def test_an_answer_that_never_counts_is_not_asked_for_again_and_the_review_ends(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The guard behind the fix above: should the rule that hands items out
    # and the rule that counts a look ever disagree again, for any item, a
    # review still asks about each item once, counts an answer that did not
    # count as not looked at, never reads more done than its total, and
    # ends. It steps aside after its first answer, so the run that carries
    # on remembers nothing of it but what the ledger says. Looking at new
    # footage, which has no review, ends the same way.
    async def never(_db: AsyncSession, _item: Any) -> bool:
        return False

    monkeypatch.setattr(store, "_is_item", never)
    site: Site = env["site"]
    said = _progress(monkeypatch)
    items = 4  # two photographs and two shots
    request_ids: list[uuid.UUID] = []
    seen = _watch(env, request_ids, items)
    note_first = site.during[1]
    urgent: list[uuid.UUID] = []

    def first_then_something_more_urgent() -> None:
        note_first()
        site.soon(_more_urgent(env, urgent))

    site.during[1] = first_then_something_more_urgent
    request_ids.append(await _start(env))
    try:
        await _drain(env)
    except AskedForEver:
        pytest.fail("the review asked about an item it had already been answered about")

    assert (await _job(env, urgent[0])).state == "done"  # it did step aside
    request = await _request(env, request_ids[0])
    assert (request.state, request.done, request.failed, request.total) == ("done", 0, items, items)
    assert request.message == (
        "Finished: 0 of 4 were looked at. 4 could not be looked at; reviewing the folder "
        "again tries them again."
    )
    assert all(progress <= total for progress, total in seen), seen
    assert said and all(done <= total == items for done, total in said), said
    failures = await _looks(env, ok=False)
    assert [look.note for look in failures] == [runner.NOT_AGAIN] * items
    # The answers themselves are kept, though none speaks for its item.
    kept = await _looks(env, ok=True)
    assert len(kept) == items and not any(look.current for look in kept)
    assert len(_questions(env)) == items

    # New footage: no review row to read from, the same end.
    site.during[1] = note_first
    async with env["factory"]() as db:
        await db.execute(delete(AssetTag).where(AssetTag.source == "suggested"))
        await db.execute(delete(Look))
        await db.commit()
    await _switch_on(env)
    env["site"].looks_asked = 0
    for n in range(items + 1, items + 40):
        env["site"].during[n] = env["site"].during[items + 1]
    async with env["factory"]() as db:
        lib = await db.get(Library, env["library"])
        assert lib is not None
        assert await runner._create_ingest(db, lib)
        await db.commit()
    try:
        await _drain(env)
    except AskedForEver:
        pytest.fail("new footage was asked about again after its answer was recorded")
    (job,) = await _ingest_jobs(env)
    assert job.state == "done"
    assert job.result is not None
    assert (job.result["looked"], job.result["failed"]) == (0, items)
    assert len(_questions(env)) == 2 * items


async def test_an_item_handed_out_again_after_its_answer_counted_is_not_asked_again(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The guard's other half: an answer that counted, whose item is handed
    # out again all the same — here a review that looks again, asking under
    # one key while what it is told is stored under another. The run does
    # not ask a second time: each item moves to "could not be looked at"
    # once, and the review ends.
    real = store.key_for

    def elsewhere(model: Any, prompt_version: str = PROMPT_VERSION) -> str:
        return real(model, prompt_version) + "+elsewhere"

    monkeypatch.setattr(store, "key_for", elsewhere)
    said = _progress(monkeypatch)
    items = 4
    request_ids: list[uuid.UUID] = []
    seen = _watch(env, request_ids, items)
    request_ids.append(await _start(env, redo=True))
    try:
        await _drain(env)
    except AskedForEver:
        pytest.fail("the review asked again about an item whose answer had counted")

    request = await _request(env, request_ids[0])
    assert (request.state, request.done, request.failed, request.total) == ("done", 0, items, items)
    assert all(progress <= total for progress, total in seen), seen
    assert said and all(done <= total for done, total in said), said
    assert [look.note for look in await _looks(env, ok=False)] == [runner.NOT_AGAIN] * items
    # What the model said still speaks for each item.
    kept = await _looks(env, ok=True)
    assert len(kept) == items and all(look.current for look in kept)
    assert len(_questions(env)) == items


async def test_a_review_that_looks_again_asks_a_new_looker_about_everything(
    env: dict[str, Any],
) -> None:
    # A review that looks again (redo) with whoever is the Looker, the role
    # handed to another model after two answers. The run starts again under
    # the new model's key and asks it about every item — the two the old
    # Looker answered too, which is what looking again means. Nothing is
    # counted as not looked at.
    site: Site = env["site"]
    other = await site.model(env["factory"], OTHER)

    async def hand_over() -> None:
        async with env["factory"]() as db:
            model = await registry.get_model(db, other)
            assert model is not None
            await registry.set_role(db, "looker", model, pending=False, user_id=None)
            await db.commit()

    site.during[2] = lambda: site.soon(hand_over())
    request_id = await _start(env, redo=True)
    await _drain(env)

    request = await _request(env, request_id)
    assert (request.state, request.failed) == ("done", 0)
    # Two answers from the old Looker and four from the new one.
    assert request.done == request.total == 6
    assert await _looks(env, ok=False) == []
    async with env["factory"]() as db:
        model = await registry.get_model(db, other)
        assert model is not None
        new_key = store.key_for(model)
        speaking = [
            look
            for asset_id in (env["clip"], *env["photos"])
            for look in (await store.current(db, asset_id)).values()
        ]
    assert len(speaking) == 4 and {look.key for look in speaking} == {new_key}
    # The third was asked across the change and asked again; then all four.
    assert len(_questions(env)) == 2 + 1 + 4


async def test_a_file_that_arrives_during_a_pass_joins_the_total_before_it_is_counted(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Something that becomes lookable while a pass runs (a photograph copied
    # into the folder, a clip made ready) is asked about in the same pass.
    # The total is counted again as soon as what is done passes it, so the
    # bar never reads "5 of 4" — not even for the moment after the answer.
    said = _progress(monkeypatch)
    site: Site = env["site"]
    site.during[1] = lambda: site.soon(_photo(env, "IMG_0003.JPG"))
    request_id = await _start(env)
    await _drain(env)

    request = await _request(env, request_id)
    assert (request.state, request.done, request.failed, request.total) == ("done", 5, 0, 5)
    assert request.message == "Finished: 5 of 5 were looked at."
    assert (4, 4) in said  # it was counted as four to begin with
    assert all(done <= total for done, total in said), said
    assert len(_questions(env)) == 5


async def test_a_drive_that_is_away_is_not_held_against_the_clips(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = Pipeline(monkeypatch)
    away = "The drive for playable copies is not connected. Reconnect it and try again."
    gone = {"proxies": False}
    monkeypatch.setattr(
        stores, "away", lambda kind, _settings=None: away if gone.get(kind) else None
    )
    await _asset(env, f"{FOLDER}/DJI_0003.MP4", "video", ("poster", "thumbnail"))
    request_id = await _start(env)
    await _drain(env)
    assert len(pipeline.copies) == 1

    # Away for a whole night: the make-ready job says so, and waits.
    gone["proxies"] = True
    site: Site = env["site"]
    for _ in range(3):
        site.clock.advance(3 * 3600)
        await _drain(env)
    preparing = await _prepare_job(env, request_id)
    assert (preparing.state, preparing.message) == ("waiting", away)

    # Back again, with nothing on the queues: the hours it was away do not
    # count as hours in which nothing became ready.
    gone["proxies"] = False
    site.clock.advance(runner.PREPARE_WAIT.total_seconds() + 1)
    await _drain(env)
    request = await _request(env, request_id)
    assert (request.state, request.prep_state, request.prep_gave_up) == ("active", "running", 0)
    assert request.stills_only == 0
    assert (await _prepare_job(env, request_id)).message == "Getting 1 clip ready (1 to go)."
    assert len(pipeline.copies) == 1


async def test_a_review_waiting_for_clips_whose_drive_is_away_says_which_drive(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's line read "Waiting for 1 clip to be made ready." while
    nothing was making it: the drive the copy is written to was unplugged,
    which only the make-ready job's own row said."""
    pipeline = Pipeline(monkeypatch)
    away = "The proxy store is not connected. Reconnect the drive or share and try again."
    gone = {"proxies": True}
    monkeypatch.setattr(
        stores, "away", lambda kind, _settings=None: away if gone.get(kind) else None
    )
    await _asset(env, f"{FOLDER}/DJI_0003.MP4", "video", ("poster", "thumbnail"))
    request_id = await _start(env)
    await _drain(env)

    async def reads() -> tuple[str, str]:
        async with env["factory"]() as db:
            request = await db.get(LookRequest, request_id)
            assert request is not None
            found = await reviews.review(db, request, env["site"].clock())
        return found["state"], found["message"]

    # Everything ready was looked at; the clip is not being made.
    request = await _request(env, request_id)
    assert (request.state, request.done) == ("active", 4) and pipeline.copies == []
    assert (await _job(env, request.look_job_id)).message == runner.waiting_for_clips(1)
    assert await reads() == ("waiting", away)

    # Plugged back in: the copy is asked for, and the review says it waits for it.
    gone["proxies"] = False
    env["site"].clock.advance(runner.PREPARE_WAIT.total_seconds() + 1)
    await _drain(env)
    assert len(pipeline.copies) == 1
    assert await reads() == ("waiting", runner.waiting_for_clips(1))


async def test_a_clip_that_arrives_while_the_folder_is_made_ready_is_asked_for_too(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pipeline = Pipeline(monkeypatch)
    first = await _asset(env, f"{FOLDER}/DJI_0003.MP4", "video", ("poster", "thumbnail"))
    request_id = await _start(env)
    await _drain(env)
    assert pipeline.copies == [(str(first), True)]
    site: Site = env["site"]

    # Copied into the folder after the review began. Where the library makes
    # no copies of its own, nobody else would ever ask for this one.
    late = await _asset(env, f"{FOLDER}/DJI_0005.MP4", "video", ("poster", "thumbnail"))
    site.clock.advance(PREPARE_EVERY_S + 1)
    await _drain(env)
    assert pipeline.copies == [(str(first), True), (str(late), True)]
    request = await _request(env, request_id)
    assert (request.prep_total, request.prep_ready) == (2, 0)
    assert (await _prepare_job(env, request_id)).message == "Getting 2 clips ready (2 to go)."

    for _ in range(2):  # and only once, however often it looks again
        site.clock.advance(PREPARE_EVERY_S + 1)
        await _drain(env)
    assert len(pipeline.copies) == 2 and pipeline.analyses == []

    await _made_ready(env, first)
    await _made_ready(env, late)
    site.clock.advance(runner.PREPARE_WAIT.total_seconds() + 1)
    await _drain(env)
    request = await _request(env, request_id)
    assert (request.state, request.done, request.stills_only) == ("done", 8, 0)
    assert (request.prep_state, request.prep_total, request.prep_ready) == ("done", 2, 2)


async def test_a_review_that_ends_takes_its_make_ready_job_with_it(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A make-ready job left waiting after its review ended made the next
    review of the folder be refused as "already running"."""
    Pipeline(monkeypatch)
    clip = await _asset(env, f"{FOLDER}/DJI_0003.MP4", "video", ("poster", "thumbnail"))
    first = await _start(env)
    await _drain(env)  # four looked at; both jobs wait
    site: Site = env["site"]

    # The make-ready job looks (nothing yet) and waits two minutes more; the
    # clip becomes ready; the review's five minutes are up first.
    site.clock.advance(250)
    await _drain(env)
    await _made_ready(env, clip)
    site.clock.advance(60)
    await _drain(env)
    request = await _request(env, first)
    assert (request.state, request.done, request.prep_state) == ("done", 6, "done")
    preparing = await _prepare_job(env, first)
    assert (preparing.state, preparing.message) == ("done", runner.PREPARE_OVER)

    # At once, the folder again, with more to make ready and more to look at.
    await _asset(env, f"{FOLDER}/DJI_0005.MP4", "video", ("poster", "thumbnail"))
    await _photo(env, "IMG_0003.JPG")
    second = await _start(env)
    assert (await _prepare_job(env, second)).id != preparing.id

    # This one fails at its first question, while its clip is still on its way.
    site.server.refuse("load_failed")
    await _drain(env)
    request = await _request(env, second)
    assert (request.state, request.prep_state) == ("failed", "done")
    preparing = await _prepare_job(env, second)
    assert (preparing.state, preparing.message) == ("done", runner.PREPARE_OVER)
    # The model is put right and Start pressed again straight away.
    third = await _start(env)
    assert (await _prepare_job(env, third)).state == "queued"


async def test_a_drive_that_does_not_answer_does_not_hold_the_lane(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A network share that hangs does not fail a look at it: the look never
    returns. Made on the lane's event loop it stopped the job's heartbeat,
    and every other local-AI job with it."""
    pipeline = Pipeline(monkeypatch)
    back = threading.Event()
    looked: list[str] = []

    def hung(kind: str, _settings: Settings | None = None) -> str | None:
        looked.append(kind)
        back.wait(30)  # what a share that hung does to whoever asks it
        return None

    monkeypatch.setattr(stores, "away", hung)
    monkeypatch.setattr(runner, "SHARE_WAIT_S", 0.3)
    await _asset(env, f"{FOLDER}/DJI_0003.MP4", "video", ("poster", "thumbnail"))
    request_id = await _start(env)
    site: Site = env["site"]
    beats = 0

    async def heartbeat() -> None:
        nonlocal beats
        while True:
            await asyncio.sleep(0.01)
            beats += 1

    beating = asyncio.ensure_future(heartbeat())
    try:
        began = time.monotonic()
        await _drain(env)
        # The loop went on turning while the drive was asked, and the job
        # gave up on it in its own time, not the drive's.
        assert beats >= 5 and time.monotonic() - began < 20
        preparing = await _prepare_job(env, request_id)
        assert (preparing.state, preparing.message) == ("waiting", runner.COPIES_SILENT)
        assert pipeline.copies == []

        # It looks again while the first look is still unanswered: no
        # second thread is left waiting on the share.
        site.clock.advance(runner.PREPARE_WAIT.total_seconds() + 1)
        await _drain(env)
        assert looked == ["proxies"]
        assert (await _prepare_job(env, request_id)).message == runner.COPIES_SILENT
    finally:
        back.set()
        beating.cancel()

    # The share comes back: the clips are asked for as if nothing had happened.
    for _ in range(200):
        if runner._store_asked is None or runner._store_asked.done():
            break
        await asyncio.sleep(0.01)
    site.clock.advance(runner.PREPARE_WAIT.total_seconds() + 1)
    await _drain(env)
    assert len(pipeline.copies) == 1
    assert (await _prepare_job(env, request_id)).message == "Getting 1 clip ready (1 to go)."


class _Broker:
    """Redis, as far as `queue_depth` asks it."""

    def __init__(self, waiting: int, handed_out: list[bytes]) -> None:
        self.waiting = waiting
        self.handed_out = handed_out

    def llen(self, _name: str) -> int:
        return self.waiting

    def hvals(self, name: str) -> list[bytes]:
        assert name == "unacked"  # kombu's name for it
        return self.handed_out

    def close(self) -> None:
        pass


def _handed(queue: str) -> bytes:
    """A task a worker has been handed and has not finished, as kombu keeps it."""
    return json.dumps([{"body": "…", "headers": {}}, "", queue]).encode()


def test_a_copy_being_made_still_counts_as_on_its_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tasks are acknowledged late, so a copy being made (or one whose worker
    was restarted under it, which the broker hands out again hours later) is
    not on the queue's list. Read as "nothing on its way", its clip was
    given up on and judged from one still while the copy was still coming."""
    import redis

    broker = _Broker(0, [_handed("media"), _handed("local_ai"), _handed("vision")])
    monkeypatch.setattr(redis.Redis, "from_url", lambda *_args, **_kwargs: broker)
    assert runner.queue_depth("media") == 1
    assert runner.queue_depth("frames") == 0  # another queue's work is not this one's

    # Waiting and being made add up; one that cannot be read may be a clip's.
    broker.waiting, broker.handed_out = 3, [_handed("frames"), b"not what kombu writes"]
    assert runner.queue_depth("frames") == 5
    assert runner.queue_depth("media") == 4

    def down(*_args: Any, **_kwargs: Any) -> Any:
        raise ConnectionError("the broker went away")

    monkeypatch.setattr(redis.Redis, "from_url", down)
    assert runner.queue_depth("media") is None  # unknown, which the job reads as busy


# --- new footage -------------------------------------------------------------------------


def _counting(woken: list[bool]) -> Callable[..., bool]:
    """`local_ai_tasks.wake`, remembering that it was called."""

    def wake(_settings: Settings | None = None) -> bool:
        woken.append(True)
        return True

    return wake


class _Provider:
    """CLIP, as far as the embedding stage needs it."""

    def embed_image(self, _path: Path) -> EmbeddingResult:
        return EmbeddingResult(vector=[1.0] + [0.0] * 511, model_name="fake-clip")


async def _switch_on(env: dict[str, Any], *, since: datetime = BASE - timedelta(days=1)) -> None:
    async with env["factory"]() as db:
        found = await db.get(Library, env["library"])
        assert found is not None
        found.look_at_ingest = True
        found.look_since = since
        await db.commit()


async def _sampled(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, asset_id: uuid.UUID
) -> None:
    """A frame of the file sampled and waiting to be embedded, and CLIP to do it."""
    monkeypatch.setattr("framefound.ai.embeddings.get_embedding_provider", lambda: _Provider())
    data: Path = env["settings"].data_dir
    async with env["factory"]() as db:
        sampled = await db.scalar(
            select(func.count()).select_from(Frame).where(Frame.asset_id == asset_id)
        )
        relative = f"frames/{asset_id}-{sampled}.jpg"
        _jpeg(data / relative, 30)
        db.add(Frame(asset_id=asset_id, ts_ms=1000 * int(sampled or 0), relative_path=relative))
        await db.commit()


async def _embedded(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, asset_id: uuid.UUID
) -> None:
    """The embedding stage, as the pipeline runs it for a newly sampled file."""
    await _sampled(env, monkeypatch, asset_id)
    async with env["factory"]() as db:
        asset = await db.get(Asset, asset_id)
        lib = await db.get(Library, env["library"])
        assert asset is not None and lib is not None
        await task_module._embed_frames(db, asset, lib, Path("unused"))


async def _described(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, clip: uuid.UUID) -> None:
    """The shot-description stage, which ends a clip's way through the pipeline."""

    async def described(*_args: Any) -> int:
        return len(WINDOWS)

    monkeypatch.setattr(shots, "describe_shots", described)
    async with env["factory"]() as db:
        asset = await db.get(Asset, clip)
        lib = await db.get(Library, env["library"])
        assert asset is not None and lib is not None
        await task_module._describe_shots(db, asset, lib, Path("unused"))


async def _as_a_worker_runs_it(
    env: dict[str, Any], task_name: str, asset_id: uuid.UUID, handler: Any
) -> str:
    """A stage inside the shell every pipeline task runs in (`_with_asset`),
    which writes how the task ended on the stage's own session once the
    handler has returned. How the task's row reads afterwards."""
    async with env["factory"]() as db:
        asset = await db.get(Asset, asset_id)
        lib = await db.get(Library, env["library"])
        assert asset is not None and lib is not None
        original = Path(lib.root_path) / asset.relative_path
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"the original")
    await task_module._with_asset(task_name, asset_id, handler)
    async with env["factory"]() as db:
        rows = (
            await db.scalars(
                select(Job).where(Job.task_name == task_name, Job.asset_id == asset_id)
            )
        ).all()
    (row,) = rows
    return str(row.status)


async def _ingest_jobs(env: dict[str, Any]) -> list[LocalAiJob]:
    async with env["factory"]() as db:
        return list(
            (
                await db.scalars(
                    select(LocalAiJob)
                    .where(LocalAiJob.kind == "look_ingest")
                    .order_by(LocalAiJob.created_at)
                )
            ).all()
        )


async def _count(env: dict[str, Any], table: type[Any]) -> int:
    async with env["factory"]() as db:
        return int(await db.scalar(select(func.count()).select_from(table)) or 0)


async def test_new_footage_is_looked_at_when_the_library_is_switched_on(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    Pipeline(monkeypatch)
    woken: list[bool] = []
    monkeypatch.setattr(local_ai_tasks, "wake", _counting(woken))
    await _switch_on(env)

    await _embedded(env, monkeypatch, env["photos"][0])
    await _embedded(env, monkeypatch, env["photos"][1])  # one job is enough for both
    await _described(env, monkeypatch, env["clip"])
    (job,) = await _ingest_jobs(env)
    assert (job.state, job.role, job.priority, job.subject) == ("queued", "looker", 9, "GELCO")
    assert job.payload == {"library_id": str(env["library"])}
    assert woken == [True]

    await _drain(env)
    (job,) = await _ingest_jobs(env)
    assert job.state == "done"
    assert job.result == {"looked": 4, "failed": 0, "tags": 3, "new_tags": 0, "held": 0}
    assert len(_questions(env)) == 4
    looks = await _looks(env)
    assert len(looks) == 4 and all(look.ok and look.request_id is None for look in looks)
    # Not a review: nothing for the Processing page's review list.
    assert await _count(env, LookRequest) == 0

    # A file embedded again, already looked at: no job for nothing. The next
    # file of the dump starts another.
    await _embedded(env, monkeypatch, env["photos"][0])
    assert len(await _ingest_jobs(env)) == 1
    later = await _photo(env, "IMG_0003.JPG")
    await _embedded(env, monkeypatch, later)
    await _drain(env)
    assert [job.state for job in await _ingest_jobs(env)] == ["done", "done"]
    assert len(_questions(env)) == 5


async def test_new_footage_is_left_alone_when_it_is_not(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    Pipeline(monkeypatch)
    await _embedded(env, monkeypatch, env["photos"][0])
    await _described(env, monkeypatch, env["clip"])
    async with env["factory"]() as db:
        assert await runner.ensure_ingest(db, env["site"].clock()) == 0
    assert await _ingest_jobs(env) == []

    # Switched on now: what came in before the switch is not swept up.
    await _switch_on(env, since=BASE + timedelta(days=1))
    await _embedded(env, monkeypatch, env["photos"][1])
    async with env["factory"]() as db:
        await scanner._ensure_ingest_looks(db)
    assert await _ingest_jobs(env) == []
    await _drain(env)
    assert _questions(env) == []


async def test_the_hook_never_fails_the_stage_that_called_it(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    Pipeline(monkeypatch)
    await _switch_on(env)

    async def refused(db: AsyncSession, *_args: Any, **_kwargs: Any) -> Any:
        # The database refuses the job's row (here: a job of no kind), in the
        # hook's own transaction. The session will take nothing more until
        # it is rolled back — and it is the stage's session, on which the
        # task's shell still has to write that the task succeeded.
        db.add(LocalAiJob(kind=None, subject="GELCO"))
        await db.flush()
        raise AssertionError("the table took a job of no kind")

    async def described(*_args: Any) -> int:
        return len(WINDOWS)

    monkeypatch.setattr(shots, "describe_shots", described)
    monkeypatch.setattr(jobs, "create", refused)
    await _sampled(env, monkeypatch, env["photos"][0])
    embed, describe = task_module._embed_frames, task_module._describe_shots
    # The stage still succeeds and says so, its own work kept.
    assert await _as_a_worker_runs_it(env, "embed_frames", env["photos"][0], embed) == "succeeded"
    async with env["factory"]() as db:
        embedded = await db.scalar(
            select(func.count()).select_from(Frame).where(Frame.embedding.is_not(None))
        )
    assert embedded == 1
    assert await _as_a_worker_runs_it(env, "describe_shots", env["clip"], describe) == "succeeded"

    # Nor can the looking code be reached at all.
    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(runner, "nudge", broken)
    await _sampled(env, monkeypatch, env["photos"][1])
    assert await _as_a_worker_runs_it(env, "embed_frames", env["photos"][1], embed) == "succeeded"
    assert await _ingest_jobs(env) == []


async def test_a_reanalysed_clip_is_looked_at_again(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    Pipeline(monkeypatch)
    await _switch_on(env)
    await _described(env, monkeypatch, env["clip"])
    await _drain(env)
    assert len(_questions(env)) == 4

    data: Path = env["settings"].data_dir
    moved = ((0, 4000), (4000, 7000), (7000, 9000))  # the first shot is found again as it was

    async def analysed(db: AsyncSession, asset: Asset, *_args: Any) -> int:
        await db.execute(delete(ShotWindow).where(ShotWindow.asset_id == asset.id))
        for idx, (start, end) in enumerate(moved):
            db.add(ShotWindow(asset_id=asset.id, idx=idx, start_ms=start, end_ms=end, move="orbit"))
            for moment in set(shots.window_moments(start, end)):
                _jpeg(data / shots.still_relpath(asset.id, moment), 20 + idx)
        await db.commit()
        return len(moved)

    monkeypatch.setattr(shots, "analyse_motion", analysed)
    async with env["factory"]() as db:
        clip = await db.get(Asset, env["clip"])
        lib = await db.get(Library, env["library"])
        assert clip is not None and lib is not None
        await task_module._analyze_motion(db, clip, lib, Path("unused"))
    await _described(env, monkeypatch, env["clip"])
    await _drain(env)

    # Only the two shots whose times moved were asked about.
    assert len(_questions(env)) == 6
    async with env["factory"]() as db:
        clip = await db.get(Asset, env["clip"])
        spans = {
            (look.start_ms, look.end_ms) for look in (await store.current(db, env["clip"])).values()
        }
    assert clip is not None and clip.looked_at is not None
    assert spans == set(moved)


async def test_the_scanner_starts_what_a_lost_hook_missed(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    Pipeline(monkeypatch)
    woken: list[bool] = []
    monkeypatch.setattr(local_ai_tasks, "wake", _counting(woken))
    await _switch_on(env)

    async def sweep() -> None:
        async with env["factory"]() as db:
            await scanner._ensure_ingest_looks(db)

    await sweep()
    await sweep()  # one job per library, however often the sweep runs
    (job,) = await _ingest_jobs(env)
    assert job.payload == {"library_id": str(env["library"])}
    assert woken == [True]

    # A job whose worker died does not hold the library: it is ended, and
    # another takes its place.
    site: Site = env["site"]

    def die() -> None:
        raise WorkerDied()

    site.during[1] = die
    with pytest.raises(WorkerDied):
        await jobs.run_next(env["factory"], site.deps())
    site.clock.advance(jobs.STALE_AFTER_S + 10)
    async with env["factory"]() as db:
        assert await runner.ensure_ingest(db, site.clock()) == 1
        await db.commit()
    first, second = await _ingest_jobs(env)
    assert (first.state, second.state) == ("failed", "queued")

    await _drain(env)
    await sweep()  # nothing is left waiting: no job
    assert len(await _ingest_jobs(env)) == 2
    assert len(_questions(env)) == 5  # the one cut off was asked again

    # With nobody in the Looker role, a job could only fail saying so.
    await _photo(env, "IMG_0003.JPG")
    async with env["factory"]() as db:
        await db.execute(delete(LocalAiRole))
        await db.commit()
    await sweep()
    assert len(await _ingest_jobs(env)) == 2


async def test_a_looker_that_will_not_load_is_not_tried_again_by_every_file_and_every_sweep(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A model that will not load is no file's fault, so nothing is recorded
    against the file — which left it waiting, and every file that arrived
    and every five-minute scanner pass made another job that loaded the
    same broken model again, without end."""
    Pipeline(monkeypatch)
    site: Site = env["site"]
    monkeypatch.setattr(runner, "wall_clock", site.clock)
    await _switch_on(env)

    async def sweep() -> None:
        async with env["factory"]() as db:
            await scanner._ensure_ingest_looks(db)

    site.server.refuse("load_failed")
    await _embedded(env, monkeypatch, env["photos"][0])
    await _drain(env)
    (job,) = await _ingest_jobs(env)
    assert job.state == "failed"
    assert job.message == f"{runner.could_not_use('It would not load.')} {runner.HELD_FOR}"
    assert runner.HELD_FOR == "Looking at new footage is tried again in about 6 hours."
    assert await _looks(env) == []  # nothing held against the file

    # The rest of the card arrives, and the scanner passes twice.
    await _embedded(env, monkeypatch, env["photos"][1])
    await _described(env, monkeypatch, env["clip"])
    await sweep()
    site.clock.advance(300)
    await sweep()
    await _drain(env)
    assert len(await _ingest_jobs(env)) == 1
    assert len(_questions(env)) == 1  # the model was asked to load once

    # Hours later it is tried once more; it loads now, and everything is looked at.
    site.clock.advance(runner.INGEST_HOLD.total_seconds())
    await sweep()
    await _drain(env)
    first, second = await _ingest_jobs(env)
    assert (first.state, second.state) == ("failed", "done")
    assert second.result == {"looked": 4, "failed": 0, "tags": 3, "new_tags": 0, "held": 0}


async def test_the_wait_ends_as_soon_as_the_looker_has_answered_something(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    Pipeline(monkeypatch)
    site: Site = env["site"]
    monkeypatch.setattr(runner, "wall_clock", site.clock)
    await _switch_on(env)

    # Its program was removed: it may not run as things stand, and asking
    # again a minute later would get the same answer.
    site.server.refuse("no_runtime")
    await _embedded(env, monkeypatch, env["photos"][0])
    await _drain(env)
    (job,) = await _ingest_jobs(env)
    assert job.state == "failed" and job.message.endswith(runner.HELD_FOR)
    async with env["factory"]() as db:
        assert await runner.ensure_ingest(db, site.clock()) == 0

    # The program is put back and the model passes a Test: every answer is
    # noted on the model's row. (Its time is set by hand: SQLite's clock
    # counts whole seconds, and this test takes less than one.)
    async with env["factory"]() as db:
        await registry.note_loaded(db, env["model_id"], "mlx-vlm", VERSION)
        await db.execute(
            update(LocalModel)
            .where(LocalModel.id == env["model_id"])
            .values(updated_at=datetime.now(UTC) + timedelta(minutes=1))
        )
        await db.commit()
    await _embedded(env, monkeypatch, env["photos"][1])  # the next file to arrive
    first, second = await _ingest_jobs(env)
    assert (first.state, second.state) == ("failed", "queued")
    await _drain(env)
    assert len(await _looks(env, ok=True)) == 4  # both photographs, and the clip's two shots


async def test_a_looker_that_may_not_look_starts_no_job_for_new_footage(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    Pipeline(monkeypatch)
    await _switch_on(env)
    async with env["factory"]() as db:
        model = await registry.get_model(db, env["model_id"])
        assert model is not None
        model.state = registry.INCOMPLETE  # its files are no longer whole
        await db.commit()

    await _embedded(env, monkeypatch, env["photos"][0])
    await _described(env, monkeypatch, env["clip"])
    async with env["factory"]() as db:
        await scanner._ensure_ingest_looks(db)
        assert await runner.ensure_ingest(db, env["site"].clock()) == 0
    # A job could only fail saying so, once for every file of the card.
    assert await _ingest_jobs(env) == []


async def test_new_footage_that_stops_its_worker_every_time_is_left_alone_after_three_tries(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that dies writes nothing, so a job that killed its worker
    every time it ran (a model too large for the memory left) was replaced
    at every five-minute scanner pass, to kill it again, without end."""
    Pipeline(monkeypatch)
    site: Site = env["site"]
    monkeypatch.setattr(runner, "wall_clock", site.clock)
    await _switch_on(env)

    def die() -> None:
        raise WorkerDied()

    async def sweep() -> int:
        async with env["factory"]() as db:
            made = await runner.ensure_ingest(db, site.clock())
            await db.commit()
            return made

    async def dies_at_its_first_question() -> None:
        site.during[site.looks_asked + 1] = die
        with pytest.raises(WorkerDied):
            await jobs.run_next(env["factory"], site.deps())
        site.clock.advance(jobs.STALE_AFTER_S + 10)

    assert await sweep() == 1
    # Twice it is taken for a restart, and another job takes its place at once.
    for _ in range(2):
        await dies_at_its_first_question()
        assert await sweep() == 1
    # The third time in a row with nothing looked at, it is not.
    await dies_at_its_first_question()
    assert await sweep() == 0
    ended = await _ingest_jobs(env)
    assert [job.state for job in ended] == ["failed", "failed", "failed"]
    assert [job.message for job in ended[:2]] == [jobs.STOPPED, jobs.STOPPED]
    assert ended[2].message == (
        "It stopped without finishing 3 times in a row, each time before it had looked at "
        "anything. Looking at new footage is tried again in about 6 hours."
    )
    # A file that arrives meanwhile starts nothing either.
    await _embedded(env, monkeypatch, env["photos"][0])
    assert len(await _ingest_jobs(env)) == 3
    assert await _looks(env) == []  # nothing is held against any file

    # The Processing page says why files are waiting, and until when.
    async with env["factory"]() as db:
        (line,) = (await reviews.listing(db, site.clock()))["ingest"]
    assert (line["state"], line["waiting"]) == ("failed", 4)
    assert line["message"] == (
        "Looking at new footage stopped after something went wrong, and is tried again "
        "in about 6 hours."
    )

    # The Looker answers something else meanwhile (a review, a Test, another
    # library's footage), and every answer is noted on its row. That says
    # nothing about what stops the worker here: the library is still left
    # alone. It used to lift the hold, so a Looker busy elsewhere had the
    # scanner make this job again at every pass, to stop the worker again.
    async with env["factory"]() as db:
        await registry.note_loaded(db, env["model_id"], "mlx-vlm", VERSION)
        await db.execute(
            update(LocalModel)
            .where(LocalModel.id == env["model_id"])
            .values(updated_at=datetime.now(UTC) + timedelta(minutes=1))
        )
        await db.commit()
    assert await sweep() == 0
    await _embedded(env, monkeypatch, env["photos"][1])
    assert len(await _ingest_jobs(env)) == 3
    async with env["factory"]() as db:
        (line,) = (await reviews.listing(db, site.clock()))["ingest"]
    assert line["state"] == "failed" and line["retry_at"] is not None

    # Six hours on it is tried once; it dies the same way, and is held at once.
    site.clock.advance(runner.INGEST_HOLD.total_seconds())
    assert await sweep() == 1
    await dies_at_its_first_question()
    assert await sweep() == 0
    assert len(_questions(env)) == 4

    # The next time the model has room: everything is looked at.
    site.clock.advance(runner.INGEST_HOLD.total_seconds())
    assert await sweep() == 1
    await _drain(env)
    assert (await _ingest_jobs(env))[-1].state == "done"
    assert len(await _looks(env, ok=True)) == 4
    assert await sweep() == 0


async def test_a_new_looker_is_not_held_for_the_stops_of_the_one_it_replaced(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Looker was too large and stopped the worker three times: the
    library is held. The way out is to give the role to a smaller model, and
    that ends the hold. But the count of stops was the library's, not the
    model's, so the new Looker's first stop — any restart while it loaded —
    made four, and the library was held six hours for a model that had
    stopped once."""
    Pipeline(monkeypatch)
    site: Site = env["site"]
    monkeypatch.setattr(runner, "wall_clock", site.clock)
    await _switch_on(env)

    def die() -> None:
        raise WorkerDied()

    async def sweep() -> int:
        async with env["factory"]() as db:
            made = await runner.ensure_ingest(db, site.clock())
            await db.commit()
            return made

    async def dies_at_its_first_question() -> None:
        site.during[site.looks_asked + 1] = die
        with pytest.raises(WorkerDied):
            await jobs.run_next(env["factory"], site.deps())
        site.clock.advance(jobs.STALE_AFTER_S + 10)

    assert await sweep() == 1
    for _ in range(2):
        await dies_at_its_first_question()
        assert await sweep() == 1
    await dies_at_its_first_question()
    assert await sweep() == 0  # held: three in a row under this Looker

    # The role goes to another model: the hold is over, and so is the count.
    other = await site.model(env["factory"], SECOND)
    async with env["factory"]() as db:
        model = await registry.get_model(db, other)
        assert model is not None
        await registry.set_role(db, "looker", model, pending=False, user_id=None)
        await db.commit()
    assert await sweep() == 1
    # Its worker is restarted while it answers its first question: a
    # restart, and another job takes its place at once.
    await dies_at_its_first_question()
    assert await sweep() == 1
    fourth = (await _ingest_jobs(env))[3]
    assert (fourth.state, fourth.message) == ("failed", jobs.STOPPED)
    assert reviews.HELD not in fourth.payload
    async with env["factory"]() as db:
        (line,) = (await reviews.listing(db, site.clock()))["ingest"]
    assert line["state"] == "waiting" and line.get("retry_at") is None

    await _drain(env)
    assert (await _ingest_jobs(env))[-1].state == "done"
    assert {look.model for look in await _looks(env, ok=True)} == {SECOND}

    # Three in a row under the new Looker would hold it all the same: the
    # count is each model's own, not gone.
    await _photo(env, "IMG_0003.JPG")
    assert await sweep() == 1
    for _ in range(2):
        await dies_at_its_first_question()
        assert await sweep() == 1
    await dies_at_its_first_question()
    assert await sweep() == 0


async def test_a_worker_that_stops_after_something_was_looked_at_is_only_a_restart(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three restarts in a row are not a job that cannot run: each time an
    item had been finished first, so the count starts again and the library
    is never left alone."""
    Pipeline(monkeypatch)
    site: Site = env["site"]
    monkeypatch.setattr(runner, "wall_clock", site.clock)
    await _switch_on(env)

    def die() -> None:
        raise WorkerDied()

    async def sweep() -> int:
        async with env["factory"]() as db:
            made = await runner.ensure_ingest(db, site.clock())
            await db.commit()
            return made

    assert await sweep() == 1
    for _ in range(3):
        # One item is looked at; the worker goes away during the next.
        site.during[site.looks_asked + 2] = die
        with pytest.raises(WorkerDied):
            await jobs.run_next(env["factory"], site.deps())
        site.clock.advance(jobs.STALE_AFTER_S + 10)
        assert await sweep() == 1
    await _drain(env)
    states = [job.state for job in await _ingest_jobs(env)]
    assert states == ["failed", "failed", "failed", "done"]
    assert all(job.message == jobs.STOPPED for job in (await _ingest_jobs(env))[:3])
    assert len(await _looks(env, ok=True)) == 4


async def test_new_footage_keeps_its_figures_and_its_new_tag_allowance_across_stepping_aside(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    Pipeline(monkeypatch)
    site: Site = env["site"]
    # Every answer proposes a tag the catalogue has never had; one job may add one.
    monkeypatch.setattr(runner, "MAX_NEW_TAGS_PER_REVIEW", 1)
    proposed = iter(("fairway", "bunker", "clubhouse", "pond", "cart", "tee"))
    fake.FakeModelServer.answer_for(
        "LookAnswer",
        lambda _body: json.dumps(
            {**ANSWER, "new_tags": [{"tag": next(proposed), "confidence": 0.9}]}
        ),
    )
    await _switch_on(env)
    for photo in env["photos"]:
        await _embedded(env, monkeypatch, photo)
    await _described(env, monkeypatch, env["clip"])

    async def ask_for_a_test() -> None:
        async with env["factory"]() as db:
            await jobs.create(
                db, "test", subject=f"{LOOKER}@test", model_id=env["model_id"], priority=0
            )
            await db.commit()

    site.during[1] = lambda: site.soon(ask_for_a_test())
    assert await jobs.run_next(env["factory"], site.deps()) is True
    (job,) = await _ingest_jobs(env)
    assert (job.state, job.message) == ("queued", runner.STEPPED_ASIDE)

    await _drain(env)  # the Test, then the same row again
    (job,) = await _ingest_jobs(env)
    assert job.state == "done"
    # The whole job's figures, not only what it did since it stepped aside;
    # and the one new tag it was allowed, not one for each time it ran.
    assert (job.result["looked"], job.result["failed"]) == (4, 0)
    assert (job.result["new_tags"], job.result["held"]) == (1, 3)
    async with env["factory"]() as db:
        names = set((await db.scalars(select(Tag.name))).all())
    assert names == {"green", "fairway"}


async def test_switching_a_library_off_stops_its_looking_before_the_next_file(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The switch is how somebody at the Mac makes the Looker stop (it is
    slowing an Adobe session): it is read between two files, not only when
    the job starts."""
    Pipeline(monkeypatch)
    site: Site = env["site"]
    await _switch_on(env)
    for photo in env["photos"]:
        await _embedded(env, monkeypatch, photo)
    await _described(env, monkeypatch, env["clip"])

    async def switch_off() -> None:
        async with env["factory"]() as db:
            found = await db.get(Library, env["library"])
            assert found is not None
            found.look_at_ingest = False
            found.look_since = None
            await db.commit()

    site.during[1] = lambda: site.soon(switch_off())
    await _drain(env)
    (job,) = await _ingest_jobs(env)
    assert job.state == "done"
    # What was being answered is kept; nothing more was asked.
    assert job.result == {
        "looked": 1,
        "failed": 0,
        "tags": 1,
        "new_tags": 0,
        "held": 0,
        "off": True,
    }
    assert len(_questions(env)) == 1


async def test_in_demo_mode_new_footage_never_starts_the_lane_from_the_pipeline(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """In demo mode waking the lane starts it inside the calling process
    (`local_ai_tasks.wake` → `demo.wake_inline`). The API runs it; a
    pipeline worker or the scanner must only leave the job for it."""
    Pipeline(monkeypatch)
    woken: list[bool] = []
    monkeypatch.setattr(local_ai_tasks, "wake", _counting(woken))
    monkeypatch.setattr(runner, "availability", lambda _settings=None: Availability("demo"))
    await _switch_on(env)

    await _embedded(env, monkeypatch, env["photos"][0])
    (job,) = await _ingest_jobs(env)
    assert job.state == "queued"
    async with env["factory"]() as db:
        found = await db.get(LocalAiJob, job.id)
        assert found is not None
        await db.delete(found)
        await db.commit()
    async with env["factory"]() as db:
        await scanner._ensure_ingest_looks(db)  # the scanner's way in
    assert len(await _ingest_jobs(env)) == 1
    assert woken == []


async def test_where_models_cannot_run_a_whole_pipeline_writes_no_local_ai_job(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The containers and a remote worker: no FRAMEFOUND_RUNTIMES_DIR. Even a
    library switched on gets no job from any stage or from the scanner."""
    Pipeline(monkeypatch)
    await _switch_on(env)
    monkeypatch.delenv("FRAMEFOUND_RUNTIMES_DIR")
    get_settings.cache_clear()
    monkeypatch.setattr(shots, "analyse_motion", lambda *_args: _zero())

    for photo in env["photos"]:
        await _embedded(env, monkeypatch, photo)
    await _embedded(env, monkeypatch, env["clip"])
    async with env["factory"]() as db:
        clip = await db.get(Asset, env["clip"])
        lib = await db.get(Library, env["library"])
        assert clip is not None and lib is not None
        await task_module._analyze_motion(db, clip, lib, Path("unused"))
    await _described(env, monkeypatch, env["clip"])
    async with env["factory"]() as db:
        await scanner._ensure_ingest_looks(db)
        assert await runner.ensure_ingest(db, env["site"].clock()) == 0

    assert await _count(env, LocalAiJob) == 0
    assert await _count(env, LookRequest) == 0


async def _zero() -> int:
    return 0
