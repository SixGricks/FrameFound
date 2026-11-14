"""Asking the Looker for a clip's look (processing/colour_jobs.py; design L4 S6).

The requests go through the real API, and the job runs on the real lane:
`jobs.run_next` is awaited directly (no queue, no worker), the real `ask()`
stands in front of `fake.FakeModelServer`, and the Looker is L1's fixture
model, shrunk to a few kilobytes and placed in a library under `tmp_path`.
Waking the lane is a list's `append` (or False for "the queue is down").
The clip is rows and drawn stills; nothing runs ffmpeg, starts a process or
reaches a network — trying to fails the test.

The last test runs demo mode as a reviewer would, with nothing handed in:
the demo's own seed, its own model server and its own lane in the process.
"""

import ast
import asyncio
import json
import sqlite3
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.api.v1 import clip_looks as api
from framefound.api.v1.local_models import QUEUE_DOWN, get_deps
from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, ClipLook, Frame, Library, LocalAiJob, ShotWindow
from framefound.local_models import fake, jobs, library, preflight, registry, runtimes
from framefound.local_models.deps import Deps
from framefound.local_models.kinds import MlxVlm
from framefound.local_models.settings_store import LocalModelsConfig
from framefound.media import clip_look, compare, develop
from framefound.processing import colour_jobs, local_ai_tasks, shots
from framefound.storage import stores

Factory = async_sessionmaker[AsyncSession]

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
SECRET = "colour-jobs-secret"
LOOKER = "mlx-community/gemma-4-e4b-it-4bit"
VERSION = "0.7.12"
AFTERNOON = datetime(2026, 11, 14, 14, 0)
BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)
FOLDER = "Brookside Country Club/2026/Drone"
# The middle shot is the longest: its middle (9 s) is the still the model is shown.
WINDOWS = ((0, 4000), (4000, 14000), (14000, 20000))
REFERENCE = 9000
MIDDLES = (2000, 9000, 17000)

_OURS = (
    "FRAMEFOUND_BULK_DIR",
    "FRAMEFOUND_PROXY_DIR",
    "FRAMEFOUND_MODELS_DIR",
    "FRAMEFOUND_RUNTIMES_DIR",
    "FRAMEFOUND_LOCAL_AI_DEMO",
    "FRAMEFOUND_DOMAIN",
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
    monkeypatch.setenv("FRAMEFOUND_MIN_FREE_GB", "0")
    # Progress written, and Cancel looked for, at every step.
    monkeypatch.setattr(jobs, "PROGRESS_EVERY_S", 0.0)
    get_settings.cache_clear()
    stores._CACHE.clear()
    api.clear_learned()
    yield
    fake.FakeModelServer.answer_for("ColourSuggestion", None)
    api.clear_learned()
    get_settings.cache_clear()
    stores._CACHE.clear()


class Clock:
    """The Mac's wall clock, naive, moved by the test."""

    def __init__(self, at: datetime = AFTERNOON) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class Site:
    """A Mac with MLX-VLM installed, a model library, a data folder and a Looker."""

    def __init__(self, tmp_path: Path) -> None:
        self.data = tmp_path / "data"
        self.models = tmp_path / "models"
        self.runtimes = tmp_path / "runtimes"
        for folder in (self.data, self.models, self.runtimes):
            folder.mkdir()
        self.hub = fake.FakeHub.from_fixtures(sleep=lambda _seconds: None)
        self.hub.shrink(LOOKER)
        self.settings = Settings(
            data_dir=self.data,
            models_dir=self.models,
            runtimes_dir=self.runtimes,
            secret_key=SECRET,
            min_free_gb=0,
        )
        self.clock = Clock()
        self.server = fake.FakeModelServer(sleep=lambda _seconds: None)
        runner = fake.FakeRunner(
            {
                "uv venv": (0, ""),
                "uv pip install": (0, ""),
                "uv pip freeze": (0, ""),
                "python": lambda argv: (0, f"{Path(argv[0]).parents[2].name}\n"),
            },
            make_venvs=True,
        )
        runtimes.install(MlxVlm(), VERSION, self.runtimes, run=runner)
        runtimes.activate(self.runtimes, "mlx-vlm", VERSION)
        # Every wake-up of the lane; `queue_up = False` is "the queue is down".
        self.woken: list[int] = []
        self.queue_up = True

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
        found = preflight.preflight(
            self.hub,
            LOOKER,
            config=LocalModelsConfig(),
            kinds={"mlx-vlm": MlxVlm()},
            installed={"mlx-vlm": VERSION},
            free_bytes=None,
            min_free_bytes=0,
        )
        async with factory() as db:
            model = await registry.add_model(db, found, None)
            model.state = registry.INSTALLED
            await registry.set_role(db, "looker", model, pending=False, user_id=None)
            await db.commit()
            files = registry.snapshot(model)
        library.download(self.hub, files, settings=self.settings)
        return model.id


def _scene(warmth: int) -> Image.Image:
    y, x = np.mgrid[0:270, 0:480]
    arr = np.stack(
        [40 + x * 160 / 480 + warmth, 50 + y * 150 / 270, 90 + (x + y) * 60 / 750 - warmth],
        axis=-1,
    )
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), "RGB")


def _asset(library_id: uuid.UUID, relative: str) -> Asset:
    return Asset(
        library_id=library_id,
        relative_path=relative,
        filename=relative.rsplit("/", 1)[-1],
        extension=relative.rsplit(".", 1)[-1].lower(),
        media_type="video",
        size_bytes=1,
        mtime=BASE,
        duration_s=20.0,
        availability="online",
    )


def _still(data: Path, asset_id: uuid.UUID, ts: int, warmth: int) -> None:
    path = data / shots.still_relpath(asset_id, ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    _scene(warmth).save(path, "JPEG", quality=95)


async def _clip(factory: Factory, data: Path, library_id: uuid.UUID, name: str) -> Asset:
    """A drone clip with three shots and their middle stills."""
    async with factory() as db:
        clip = _asset(library_id, f"{FOLDER}/{name}")
        db.add(clip)
        await db.flush()
        for idx, (start, end) in enumerate(WINDOWS):
            db.add(
                ShotWindow(
                    asset_id=clip.id,
                    idx=idx,
                    start_ms=start,
                    end_ms=end,
                    move="orbit",
                    light="golden",
                    look={"brightness": 0.41, "warmth": -0.05},
                )
            )
        await db.commit()
    for warmth, ts in zip((30, 20, -10), MIDDLES, strict=True):
        _still(data, clip.id, ts, warmth)
    return clip


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'colour.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", SECRET)
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    site = Site(tmp_path)

    def wake(_settings: Any = None) -> bool:
        site.woken.append(1)
        return site.queue_up

    monkeypatch.setattr(local_ai_tasks, "wake", wake)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_deps] = site.deps
    async with factory() as db:
        lib = Library(name="GELCO", root_path=str(tmp_path / "GELCO"))
        db.add(lib)
        await db.commit()
    clip = await _clip(factory, site.data, lib.id, "DJI_0639.MP4")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        setup = await http.post(
            "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
        )
        assert setup.status_code == 201, setup.text
        yield {
            "http": http,
            "factory": factory,
            "site": site,
            "library": lib,
            "clip": clip,
            "database": tmp_path / "colour.db",
        }
    await engine.dispose()
    get_settings.cache_clear()


async def _drain(env: dict[str, Any], limit: int = 10) -> None:
    """What the lane does when woken: run jobs until none is queued."""
    for _ in range(limit):
        if not await jobs.run_next(env["factory"], env["site"].deps()):
            return
    raise AssertionError("the lane never ran out of work")


async def _looks(env: dict[str, Any], asset: Asset | None = None) -> dict[str, Any]:
    answer = await env["http"].get(f"/api/v1/clip-looks/{(asset or env['clip']).id}")
    assert answer.status_code == 200, answer.text
    found: dict[str, Any] = answer.json()
    return found


def _model_tile(body: dict[str, Any]) -> dict[str, Any]:
    (tile,) = [c for c in body["candidates"] if c["source"] == "model"]
    assert isinstance(tile, dict)
    return tile


async def _suggest(env: dict[str, Any], asset: Asset | None = None) -> httpx.Response:
    response: httpx.Response = await env["http"].post(
        f"/api/v1/clip-looks/{(asset or env['clip']).id}/suggest"
    )
    return response


async def _rows(env: dict[str, Any], model: type[Any]) -> list[Any]:
    async with env["factory"]() as db:
        return list(await db.scalars(select(model)))


def _texts(body: dict[str, Any]) -> str:
    return "\n".join(
        part["text"] for part in body["messages"][0]["content"] if part["type"] == "text"
    )


# --- asking, and what is stored ------------------------------------------------------


async def test_a_suggestion_is_stored_with_the_model_that_made_it(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.looker(env["factory"])
    asked = await _suggest(env)
    assert asked.status_code == 202, asked.text
    job = asked.json()["job"]
    assert (job["state"], job["can_cancel"]) == ("queued", True)
    assert site.woken == [1]
    # While it waits its turn the page says so.
    waiting = await _looks(env)
    assert waiting["suggest"]["job"]["id"] == job["id"]
    assert _model_tile(waiting)["available"] is False

    await _drain(env)
    (request,) = site.server.requests
    # One still, the reference, with what was measured about its shot.
    pictures = [p for p in request["messages"][0]["content"] if p["type"] != "text"]
    assert len(pictures) == 1
    said = _texts(request)
    assert clip_look.INSTRUCTIONS in said
    assert "brightness 0.41" in said and "light golden" in said and "move orbit" in said
    assert request["response_format"]["json_schema"]["name"] == "ColourSuggestion"

    body = await _looks(env)
    tile = _model_tile(body)
    assert tile["available"] is True and tile["measured_at_ms"] == REFERENCE
    assert tile["provenance"]["model"] == LOOKER
    assert tile["provenance"]["prompt_version"] == clip_look.PROMPT_VERSION
    assert tile["provenance"]["runtime"] == "mlx-vlm"
    # The fake model server marks everything it says as made up.
    assert tile["demo"] is True
    for key, value in tile["recipe"].items():
        low, high = clip_look.LIMITS[key]
        assert low <= value <= high and abs(value) >= clip_look.DEAD_BAND
    # Done, and nothing chosen for anybody.
    assert body["suggest"]["job"] is None
    assert body["chosen"] is None
    (row,) = await _rows(env, ClipLook)
    assert (row.kind, row.source, row.still_ts_ms) == ("suggested", "model", REFERENCE)
    assert len(row.features) == len(compare.FEATURE_NAMES)
    (done,) = await _rows(env, LocalAiJob)
    assert (done.state, done.model, done.prompt_version) == ("done", LOOKER, "colour-1")
    assert done.result == {"asset_id": str(env["clip"].id)}

    # Asked again, the suggestion is replaced, never doubled.
    assert (await _suggest(env)).status_code == 202
    await _drain(env)
    assert [r.kind for r in await _rows(env, ClipLook)] == ["suggested"]


async def test_a_second_request_for_the_same_clip_is_refused_while_one_waits(
    env: dict[str, Any],
) -> None:
    await env["site"].looker(env["factory"])
    assert (await _suggest(env)).status_code == 202
    again = await _suggest(env)
    assert again.status_code == 409
    assert again.json()["error"]["message"] == api.ON_ITS_WAY
    assert len(await _rows(env, LocalAiJob)) == 1


async def test_an_overnight_only_model_leaves_the_job_waiting_not_failed(
    env: dict[str, Any],
) -> None:
    site: Site = env["site"]
    model_id = await site.looker(env["factory"])
    async with env["factory"]() as db:
        # Larger than the daytime budget (8 GB), within the overnight one (20 GB).
        model = await registry.get_model(db, model_id)
        assert model is not None
        model.memory_gb = 12.0
        await db.commit()
    # Not refused: it is asked for now and answered tonight.
    assert (await _suggest(env)).status_code == 202
    await _drain(env)
    assert site.server.requests == []
    job = (await _looks(env))["suggest"]["job"]
    assert job["state"] == "waiting" and job["not_before"].endswith("Z")
    assert job["message"].startswith("Waiting for the overnight window (starts 21:30)")
    assert job["can_cancel"] is True
    assert await _rows(env, ClipLook) == []

    site.clock.at = AFTERNOON.replace(hour=22)
    await _drain(env)
    assert len(site.server.requests) == 1
    assert _model_tile(await _looks(env))["available"] is True


async def test_an_answer_that_is_not_sliders_stores_nothing(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.looker(env["factory"])
    fake.FakeModelServer.answer_for("ColourSuggestion", lambda _body: "It looks lovely as it is.")
    assert (await _suggest(env)).status_code == 202
    await _drain(env)
    # Asked, then asked once more for the form; nothing after that.
    assert len(site.server.requests) == 2
    assert await _rows(env, ClipLook) == []
    body = await _looks(env)
    job = body["suggest"]["job"]
    assert (job["state"], job["message"]) == ("failed", colour_jobs.NOT_SLIDERS)
    # What the model wrote is under Details, for the administrator.
    assert "lovely" in job["detail"]
    assert _model_tile(body)["note"] == api.NOT_SUGGESTED_YET

    # Suggest again: this time it answers, and the failure is no longer said.
    fake.FakeModelServer.answer_for(
        "ColourSuggestion",
        lambda _body: json.dumps(
            {
                "exposure": 0.3,
                "contrast": 0.01,
                "temperature": 0.1,
                "tint": 0.0,
                "saturation": 0.0,
                "vibrance": 0.2,
                "darken_shadows": 0.0,
                "white_balance": 0.6,
                "reason": "A grey morning, so lifted and warmed.",
            }
        ),
    )
    assert (await _suggest(env)).status_code == 202
    await _drain(env)
    body = await _looks(env)
    assert body["suggest"]["job"] is None
    tile = _model_tile(body)
    # The small contrast is inside the dead band and goes.
    assert tile["recipe"] == {
        "exposure": 0.3,
        "temperature": 0.1,
        "vibrance": 0.2,
        "auto_wb": 0.6,
    }
    assert tile["reason"] == "A grey morning, so lifted and warmed."


async def test_a_blank_still_is_never_shown_to_the_model(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.looker(env["factory"])
    async with env["factory"]() as db:
        # Shots found, but no still on disk, no sampled frame and no proxy.
        bare = _asset(env["library"].id, f"{FOLDER}/DJI_0700.MP4")
        db.add(bare)
        await db.flush()
        db.add(ShotWindow(asset_id=bare.id, idx=0, start_ms=0, end_ms=6000, move="static"))
        await db.commit()
    refused = await _suggest(env, bare)
    assert refused.status_code == 409
    assert refused.json()["error"]["message"] == api.NO_STILL

    # A job that reaches the lane anyway (asked for with the folder before the
    # still went) fails in words and asks nothing.
    async with env["factory"]() as db:
        await colour_jobs.queue_clip(db, bare, user_id=None)
        await db.commit()
    await _drain(env)
    assert site.server.requests == []
    (job,) = await _rows(env, LocalAiJob)
    assert (job.state, job.message) == ("failed", colour_jobs.NO_PICTURE)


async def test_the_queue_being_down_leaves_nothing_waiting(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.looker(env["factory"])
    site.queue_up = False
    for answer in (
        await _suggest(env),
        await env["http"].post(
            "/api/v1/clip-looks/folder/suggest", json={"asset_id": str(env["clip"].id)}
        ),
    ):
        assert answer.status_code == 503
        assert answer.json()["error"]["message"] == QUEUE_DOWN
    assert await _rows(env, LocalAiJob) == []
    assert (await _looks(env))["suggest"]["job"] is None


async def test_suggest_is_refused_in_words_where_it_cannot_be_answered(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # No model has the Looker role.
    refused = await _suggest(env)
    assert refused.status_code == 409
    assert refused.json()["error"]["message"] == api.NO_LOOKER
    # The container server: no model can run at all.
    site: Site = env["site"]
    monkeypatch.setattr(site, "settings", Settings(data_dir=site.data, secret_key=SECRET))
    for answer in (
        await _suggest(env),
        await env["http"].post(
            "/api/v1/clip-looks/folder/suggest", json={"asset_id": str(env["clip"].id)}
        ),
    ):
        assert answer.status_code == 409
        assert answer.json()["error"]["message"] == api.CANNOT_SUGGEST_HERE
    assert await _rows(env, LocalAiJob) == []
    assert site.woken == []


async def test_a_folder_request_skips_clips_already_looked_at(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.looker(env["factory"])
    lib = env["library"].id
    chosen = await _clip(env["factory"], site.data, lib, "DJI_0640.MP4")
    waiting = await _clip(env["factory"], site.data, lib, "DJI_0641.MP4")
    async with env["factory"]() as db:
        # Sampled frames only: still something to show a model.
        framed = _asset(lib, f"{FOLDER}/DJI_0642.MP4")
        nothing = _asset(lib, f"{FOLDER}/DJI_0643.MP4")
        inside = _asset(lib, f"{FOLDER}/Raw/DJI_0644.MP4")
        db.add_all([framed, nothing, inside])
        await db.flush()
        db.add(Frame(asset_id=framed.id, ts_ms=5000, relative_path="frames/5000.jpg"))
        db.add(ClipLook(asset_id=chosen.id, kind="chosen", source="none"))
        await colour_jobs.queue_clip(db, waiting, user_id=None)
        await db.commit()

    asked = await env["http"].post(
        "/api/v1/clip-looks/folder/suggest", json={"asset_id": str(env["clip"].id)}
    )
    assert asked.status_code == 202, asked.text
    assert asked.json() == {"queued": 2, "already": 2, "no_still": 1}
    async with env["factory"]() as db:
        queued = {
            subject: priority
            for subject, priority in (
                await db.execute(
                    select(LocalAiJob.subject, LocalAiJob.priority).where(
                        LocalAiJob.requested_by.is_not(None)
                    )
                )
            ).all()
        }
    assert queued == {str(env["clip"].id): 5, str(framed.id): 5}
    assert site.woken == [1]

    # Asked again at once: everything is on its way already.
    again = await env["http"].post(
        "/api/v1/clip-looks/folder/suggest", json={"asset_id": str(framed.id)}
    )
    assert again.json() == {"queued": 0, "already": 4, "no_still": 1}
    assert site.woken == [1]


async def test_cancelling_a_waiting_suggestion_cancels_it(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.looker(env["factory"])
    assert (await _suggest(env)).status_code == 202
    url = f"/api/v1/clip-looks/{env['clip'].id}/suggest/cancel"
    cancelled = await env["http"].post(url)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["job"]["state"] == "cancelled"
    assert cancelled.json()["job"]["can_cancel"] is False
    await _drain(env)
    assert site.server.requests == []
    assert (await _looks(env))["suggest"]["job"] is None
    again = await env["http"].post(url)
    assert again.status_code == 409
    assert again.json()["error"]["message"] == api.NOTHING_WAITING
    # And Suggest can be pressed again.
    assert (await _suggest(env)).status_code == 202


async def test_cancel_pressed_while_the_model_answers_stores_nothing(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    site: Site = env["site"]
    await site.looker(env["factory"])
    server = site.server
    answer = server.chat

    def press_cancel(body: dict[str, Any], **more: Any) -> dict[str, Any]:
        with sqlite3.connect(env["database"]) as conn:
            conn.execute("UPDATE local_ai_jobs SET cancel_requested = 1 WHERE state = 'running'")
        return answer(body, **more)

    monkeypatch.setattr(server, "chat", press_cancel)
    assert (await _suggest(env)).status_code == 202
    await _drain(env)
    # It answered after Cancel: the card must not show a look nobody wants.
    assert len(server.requests) == 1
    assert await _rows(env, ClipLook) == []
    (job,) = await _rows(env, LocalAiJob)
    assert job.state == "cancelled"
    assert (await _looks(env))["suggest"]["job"] is None


async def _worker_died(env: dict[str, Any]) -> None:
    """The clip's waiting job, as a worker that took it and then died leaves it."""
    async with env["factory"]() as db:
        job = await db.scalar(select(LocalAiJob).where(LocalAiJob.state.in_(jobs.ACTIVE)))
        assert job is not None
        job.state, job.started_at = "running", datetime(2026, 1, 1, tzinfo=UTC)
        job.beat_at = job.started_at
        await db.commit()


async def test_a_request_whose_worker_died_does_not_stand_in_the_way(env: dict[str, Any]) -> None:
    site: Site = env["site"]
    await site.looker(env["factory"])
    assert (await _suggest(env)).status_code == 202
    await _worker_died(env)
    # The page says it stopped, and Suggest agrees: it is asked for again.
    assert (await _looks(env))["suggest"]["job"]["state"] == "failed"
    again = await _suggest(env)
    assert again.status_code == 202, again.text
    assert sorted(row.state for row in await _rows(env, LocalAiJob)) == ["failed", "queued"]

    # The folder's Suggest asks for such a clip again too, not "already".
    await _worker_died(env)
    folder = await env["http"].post(
        "/api/v1/clip-looks/folder/suggest", json={"asset_id": str(env["clip"].id)}
    )
    assert folder.status_code == 202, folder.text
    assert folder.json() == {"queued": 1, "already": 0, "no_still": 0}
    states = sorted(row.state for row in await _rows(env, LocalAiJob))
    assert states == ["failed", "failed", "queued"]


async def test_a_clip_asked_for_meanwhile_is_left_alone_and_the_rest_are_asked(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    site: Site = env["site"]
    await site.looker(env["factory"])
    other = await _clip(env["factory"], site.data, env["library"].id, "DJI_0640.MP4")
    queue = colour_jobs.queue_clip

    async def raced(db: AsyncSession, asset: Asset, **more: Any) -> LocalAiJob:
        # A single-clip Suggest (or a second press) got there first.
        if asset.id == env["clip"].id:
            raise jobs.JobRefused(jobs.ALREADY)
        return await queue(db, asset, **more)

    monkeypatch.setattr(colour_jobs, "queue_clip", raced)
    asked = await env["http"].post(
        "/api/v1/clip-looks/folder/suggest", json={"asset_id": str(env["clip"].id)}
    )
    assert asked.status_code == 202, asked.text
    assert asked.json() == {"queued": 1, "already": 1, "no_still": 0}
    assert [job.subject for job in await _rows(env, LocalAiJob)] == [str(other.id)]


# --- adopt: the one way a model's colour becomes a suggestion ---------------------------


async def test_adopt_narrows_what_it_is_handed_and_names_what_a_lut_cannot_carry(
    env: dict[str, Any],
) -> None:
    still = _scene(10)
    provenance = {"model": LOOKER, "prompt_version": "look-2", "variant": ""}
    async with env["factory"]() as db:
        await colour_jobs.adopt(
            db,
            env["clip"].id,
            {"exposure": 3.0, "tint": 0.01, "window_pull": 0.5, "auto_wb": 0.5},
            reason="  Bright   and even. ",
            still_ts_ms=REFERENCE,
            still=still,
            provenance=provenance,
        )
        await db.commit()
        row = await colour_jobs.adopt(
            db,
            env["clip"].id,
            {"exposure": 3.0, "tint": 0.01, "window_pull": 0.5, "auto_wb": 0.5},
            reason="Bright and even.",
            still_ts_ms=REFERENCE,
            still=still,
            provenance=provenance,
            demo=True,
        )
        await db.commit()
    # Held to what a model may suggest; the crumb dropped; the rest named.
    assert row.recipe == {"exposure": 1.0, "auto_wb": 0.5}
    assert row.left_out == ["window_pull"]
    assert row.measured == develop.measure(still, row.recipe)
    assert row.reason == "Bright and even." and row.demo is True
    assert [r.kind for r in await _rows(env, ClipLook)] == ["suggested"]

    # A shadow lift reads the room, so a LUT cannot carry it: it is named,
    # never clamped to nothing and dropped in silence.
    async with env["factory"]() as db:
        row = await colour_jobs.adopt(
            db,
            env["clip"].id,
            {"exposure": 0.3, "shadows": 0.4},
            reason="Lift the dark corners.",
            still_ts_ms=REFERENCE,
            still=still,
            provenance=provenance,
        )
        await db.commit()
    assert row.recipe == {"exposure": 0.3}
    assert row.left_out == ["shadows"]
    # Darkening is carried, held to what a model may suggest.
    assert colour_jobs.narrowed({"shadows": -0.9}) == ({"shadows": -0.6}, ())


# --- the worker ----------------------------------------------------------------------------


def test_the_worker_that_runs_the_lane_knows_the_colour_kinds() -> None:
    # The API imports colour_jobs itself, so every other test here passes
    # whether or not the worker does; without this line the Mac's worker
    # ends every clip_look and listing_look job "does not know how to do this".
    source = Path(local_ai_tasks.__file__).read_text(encoding="utf-8")
    imported = {
        (node.module, alias.name)
        for node in ast.parse(source).body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert ("framefound.processing", "colour_jobs") in imported


# --- demo mode -----------------------------------------------------------------------------


async def test_in_demo_mode_a_suggestion_arrives_marked_as_made_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        from framefound.local_models import ask, demo
    except ImportError:  # pragma: no cover - only while L1's demo is being built
        pytest.skip("local_models/demo.py (L1 S12) is not in this tree yet")
    url = f"sqlite+aiosqlite:///{(tmp_path / 'demo.db').as_posix()}"
    for name, value in {
        "FRAMEFOUND_SETUP_TOKEN": TEST_SETUP_TOKEN,
        "FRAMEFOUND_SECRET_KEY": SECRET,
        "FRAMEFOUND_DATABASE_URL": url,
        "FRAMEFOUND_MEDIA_ROOT": str(tmp_path / "media"),
        "FRAMEFOUND_DATA_DIR": str(tmp_path / "data"),
        "FRAMEFOUND_MODELS_DIR": str(tmp_path / "models"),
        "FRAMEFOUND_LOCAL_AI_DEMO": "1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(ask, "_FAKE", None)
    monkeypatch.setattr(demo, "SECONDS_PER_CHUNK", 0.0)
    monkeypatch.setattr(demo, "ANSWER_SECONDS", 0.0)
    get_settings.cache_clear()
    assert demo.stop(), "a demo lane left by an earlier test did not stop"
    settings = get_settings()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    from framefound.main import create_app

    app = create_app()
    # The database is the test's; nothing that stands in for the outside world is.
    app.dependency_overrides[get_session] = session
    try:
        # The demo's own seed gives the Looker its role, by daylight.
        await demo.seed(factory, settings, now=datetime(2026, 11, 12, 9, 0))
        async with factory() as db:
            lib = Library(name="GELCO", root_path=str(tmp_path / "GELCO"))
            db.add(lib)
            await db.commit()
        clip = await _clip(factory, settings.data_dir, lib.id, "DJI_0639.MP4")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            setup = await http.post(
                "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
            )
            assert setup.status_code == 201, setup.text
            asked = await http.post(f"/api/v1/clip-looks/{clip.id}/suggest")
            assert asked.status_code == 202, asked.text
            give_up = asyncio.get_running_loop().time() + 30
            while True:
                body = (await http.get(f"/api/v1/clip-looks/{clip.id}")).json()
                if _model_tile(body)["available"]:
                    break
                job = body["suggest"]["job"]
                assert job is None or job["state"] != "failed", job
                assert asyncio.get_running_loop().time() < give_up, body
                await asyncio.sleep(0.05)
            assert body["suggest"]["mode"] == "demo"
            tile = _model_tile(body)
            assert tile["demo"] is True and tile["provenance"]["model"] == demo.LOOKER_MODEL
            cube = await http.get(
                f"/api/v1/clip-looks/{clip.id}/look.cube", params={"source": "model"}
            )
            if tile["recipe"]:
                assert cube.text.splitlines()[1].startswith("# DEMONSTRATION")
    finally:
        assert demo.stop(), "the demo's lane did not stop"
        await engine.dispose()
        get_settings.cache_clear()
