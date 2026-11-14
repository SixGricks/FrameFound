"""Demo mode (`FRAMEFOUND_LOCAL_AI_DEMO=1`) and the seed (local_models/demo.py; L1 S12, D23, P30).

The demo is the AI models page against made-up models, for review in a
browser on a machine that cannot run one. These tests drive it as a
reviewer does: through the real app with no fake handed in — whatever stands
in for Hugging Face, the software and the model server is what demo mode
builds for itself, and the jobs run on the demo's own lane inside the
process. The only things a test changes are the demo's pauses (so a download
does not take a quarter of a minute) and, for the lane's own test, its minute.

Starting a program or reaching a real address fails the test. So does a demo
that can be switched on where a real install runs.
"""

import asyncio
import json
import runpy
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound import local_models
from framefound.api.v1 import local_models as api
from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import AppSetting, LocalAiJob, LocalModel
from framefound.local_models import (
    ask,
    demo,
    fake,
    files,
    jobs,
    library,
    preflight,
    registry,
    settings_store,
)
from framefound.local_models.ask import HttpModelServer
from framefound.local_models.deps import deps_for
from framefound.local_models.hub import HttpHub, HubError
from framefound.local_models.kinds import LlamaCpp, kinds
from framefound.local_models.settings_store import LocalModelsConfig
from framefound.processing import local_ai_tasks
from framefound.storage import stores

URL = "/api/v1/local-models"
ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
FIRST = "mlx-community/gemma-4-e4b-it-4bit"
TEXT_ONLY = "mlx-community/gemma-4-12B-it-OptiQ-4bit"
VISION = "google/demo-vision-gguf"
SEED = Path(__file__).resolve().parents[3] / "infrastructure" / "scripts" / "seed_dev.py"

_OURS = (
    "FRAMEFOUND_BULK_DIR",
    "FRAMEFOUND_PROXY_DIR",
    "FRAMEFOUND_RUNTIMES_DIR",
    "FRAMEFOUND_LOCAL_AI_DEMO",
    "FRAMEFOUND_DOMAIN",
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
    # The demo's one model server is made afresh for each test.
    monkeypatch.setattr(ask, "_FAKE", None)
    # A demo download or Test as fast as the machine allows.
    monkeypatch.setattr(demo, "SECONDS_PER_CHUNK", 0.0)
    monkeypatch.setattr(demo, "ANSWER_SECONDS", 0.0)

    def clear() -> None:
        get_settings.cache_clear()
        stores._CACHE.clear()
        api._STATUS.clear()
        api._LOOKING.clear()

    clear()
    # The lane is one per process: a test in another file that switched demo
    # mode on may have left one running, and "nothing of the demo runs" could
    # then not be told apart from "something did". Start from none.
    assert demo.stop(), "a demo lane left by an earlier test did not stop"
    yield
    # The lane must not outlive the test that started it.
    assert demo.stop(), "the demo's lane did not stop"
    clear()


def _settings(tmp_path: Path, **more: Any) -> Settings:
    return Settings(data_dir=tmp_path / "data", models_dir=tmp_path / "models", **more)


# --- the demo cannot be reached on a real install -----------------------------------


def test_with_the_flag_off_nothing_of_the_demo_is_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for settings in (_settings(tmp_path), _settings(tmp_path, runtimes_dir=tmp_path / "rt")):
        assert local_models.availability(settings).mode != "demo"
        assert not {"demo", "demo-gguf"} & set(kinds(settings))
        made = deps_for(settings)
        assert isinstance(made.hub_for(None), HttpHub)
        assert isinstance(made.server, HttpModelServer)
        assert not isinstance(made.run, fake.FakeRunner)

    # Without a queue, waking the lane says so; nothing runs in its place.
    def down() -> None:
        raise ConnectionError("no queue")

    monkeypatch.setattr(local_ai_tasks.local_ai_tick, "delay", down)
    assert local_ai_tasks.wake(_settings(tmp_path, runtimes_dir=tmp_path / "rt")) is False
    assert local_ai_tasks.wake(_settings(tmp_path)) is False
    assert not demo.running()


def test_the_demo_cannot_be_switched_on_where_runtimes_are_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(local_ai_tasks.local_ai_tick, "delay", lambda: sent.append("tick"))
    real_installs = (
        _settings(tmp_path, local_ai_demo=True, runtimes_dir=tmp_path / "runtimes"),
        _settings(tmp_path, local_ai_demo=True, runtimes_dir=None, domain="media.example.com"),
    )
    for settings in real_installs:
        # The flag is ignored, and said to be: no kind, hub, model server or lane of the demo.
        assert local_models.availability(settings).mode != "demo"
        assert not {"demo", "demo-gguf"} & set(kinds(settings))
        made = deps_for(settings)
        assert isinstance(made.hub_for(None), HttpHub)
        assert isinstance(made.server, HttpModelServer)
        assert not isinstance(made.run, fake.FakeRunner)
        # demo.py refuses to act at all, wherever it is called from.
        with pytest.raises(RuntimeError, match="Demo mode is not on"):
            demo.deps(settings)
        assert demo.wake_inline(settings) is False
        demo.keep_going(settings)
        assert not demo.running()
    # Where runtimes are configured the queue is woken as on any Mac; with only a
    # domain set the install cannot run models and the queue is woken in vain.
    assert local_ai_tasks.wake(real_installs[0]) is True
    assert sent == ["tick"]
    assert not demo.running()


def test_in_demo_mode_the_stand_ins_are_the_fakes_and_the_lane_is_inside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(
        tmp_path,
        local_ai_demo=True,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'demo.db').as_posix()}",
    )
    made = deps_for(settings)
    # Both programs have a stand-in, in the real kinds' order: MLX's first.
    assert list(made.kinds) == ["demo", "demo-gguf"]
    assert isinstance(made.kinds["demo-gguf"], fake.DemoGgufKind)
    assert isinstance(made.hub_for(None), fake.FakeHub)
    assert made.server is ask.server_for(settings)
    assert isinstance(made.run, fake.FakeRunner)
    # The package index is never asked: the demo's software says its own version.
    with pytest.raises(HubError):
        made.fetch_json("https://pypi.org/pypi/mlx-vlm/json")
    # No queue is ever sent to; the demo's own lane is started instead.
    monkeypatch.setattr(local_ai_tasks.local_ai_tick, "delay", _never_sent)
    asyncio.run(_schema(settings.db_url))
    assert local_ai_tasks.wake(settings) is True
    assert demo.stop()


def _never_sent() -> None:
    raise AssertionError("demo mode sent a message to the queue")


async def _schema(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


# --- the click path, in a browser's place ------------------------------------------


class Review:
    """The review copy: the real app in demo mode, signed in as its administrator."""

    def __init__(self, client: AsyncClient, factory: async_sessionmaker[AsyncSession]) -> None:
        self.client = client
        self.factory = factory

    async def overview(self) -> dict[str, Any]:
        api._STATUS.clear()
        answer = await self.client.get(URL)
        assert answer.status_code == 200, answer.text
        found: dict[str, Any] = answer.json()
        return found

    async def until(
        self, done: Callable[[dict[str, Any]], bool], what: str, seconds: float = 30.0
    ) -> dict[str, Any]:
        """Read the page until `done` says yes, as the page's own polling does."""
        give_up = asyncio.get_running_loop().time() + seconds
        while True:
            page = await self.overview()
            if done(page):
                return page
            if asyncio.get_running_loop().time() > give_up:
                # What moves is said first: the rest of the page is long and never the reason.
                moving = {key: page.get(key) for key in ("models", "jobs", "runtimes")}
                raise AssertionError(f"{what} never happened: {json.dumps(moving)[:4000]}")
            await asyncio.sleep(0.05)

    async def installed(
        self, version: str, name: str = "demo", what: str = "the install"
    ) -> dict[str, Any]:
        """Read the page until the program is at `version` and its install has ended.

        The version is in place a moment before its job is written as done,
        and until then the page refuses the next install ("is already being
        installed") and a roll back. A test that goes on at the first sight
        of the version fails whenever that moment is long: a slow disk, a
        busy machine.
        """
        return await self.until(
            lambda p: (
                _runtime(p, name)["installed"] == version
                and not any(
                    job["kind"] == "runtime_install" and job["state"] in jobs.ACTIVE
                    for job in p["jobs"]
                )
            ),
            what,
        )

    async def post(self, path: str, body: Any = None, status: int = 200) -> dict[str, Any]:
        answer = await self.client.post(f"{URL}{path}", json=body)
        assert answer.status_code == status, answer.text
        found: dict[str, Any] = answer.json()
        return found


def _model(page: dict[str, Any], repo_id: str) -> dict[str, Any] | None:
    return next((m for m in page["models"] if m["repo_id"] == repo_id), None)


def _runtime(page: dict[str, Any], name: str = "demo") -> dict[str, Any]:
    """One row of "Software that runs models": the demo's MLX stand-in unless named."""
    (found,) = [row for row in page["runtimes"] if row["name"] == name]
    assert isinstance(found, dict)
    return found


@pytest.fixture()
async def review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Review]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'ui.db').as_posix()}"
    for name, value in {
        "FRAMEFOUND_SETUP_TOKEN": TEST_SETUP_TOKEN,
        "FRAMEFOUND_SECRET_KEY": "local-models-demo-secret",
        "FRAMEFOUND_DATABASE_URL": url,
        "FRAMEFOUND_MEDIA_ROOT": str(tmp_path / "media"),
        "FRAMEFOUND_DATA_DIR": str(tmp_path / "data"),
        "FRAMEFOUND_MODELS_DIR": str(tmp_path / "models"),
        "FRAMEFOUND_LOCAL_AI_DEMO": "1",
        "FRAMEFOUND_MIN_FREE_GB": "0",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        setup = await client.post(
            "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
        )
        assert setup.status_code == 201, setup.text
        yield Review(client, factory)
        assert demo.stop()
    await engine.dispose()


async def test_the_whole_page_works_in_demo_mode_with_nothing_handed_in(
    review: Review, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = await review.overview()
    assert (page["mode"], page["note"], page["can_manage"]) == ("demo", demo.DEMO_NOTE, True)
    assert _runtime(page)["name"] == "demo" and _runtime(page)["installed"] is None

    # Install the software, then update it to the newest, then go back.
    await review.post("/runtimes/demo/install", {"version": "1.8"}, status=202)
    await review.installed("1.8")
    await review.post("/runtimes/demo/install", {"version": None}, status=202)
    page = await review.installed("2.0", what="the update")
    assert _runtime(page)["previous"] == "1.8"

    # The check: one card that can be added, refused three ways in the real words.
    card = await review.post("/preflight", {"name": FIRST})
    assert card["ok"] is True and card["verdict"] == preflight.CAN_BE_ADDED
    for refused in (
        "allenai/demo-needs-code",
        "nvidia/demo-gated",
        "example-lab/some-model",
    ):
        said = await review.post("/preflight", {"name": refused})
        assert said["ok"] is False and said["problems"], refused

    # Download: the bar is the real size, the disk holds a few kilobytes marked demo.
    added = await review.post(
        "/models",
        {"name": FIRST, "revision": card["revision"], "accept_warnings": True},
        status=202,
    )
    page = await review.until(
        lambda p: (_model(p, FIRST) or {}).get("state") == "installed", "the download"
    )
    model = _model(page, FIRST)
    assert model is not None and model["demo"] is True and model["runtime"] == "demo"
    assert model["size_bytes"] > 4 * 1024**3
    folder = next((tmp_path / "models" / "library").rglob("manifest.json")).parent
    manifest = files.read_manifest(folder)
    assert manifest is not None and manifest.demo is True
    assert sum(p.stat().st_size for p in folder.iterdir()) < 200_000

    # Cancel a slow download, then Resume it.
    monkeypatch.setattr(demo, "SECONDS_PER_CHUNK", 0.1)
    other = await review.post("/preflight", {"name": TEXT_ONLY})
    slow = await review.post(
        "/models",
        {"name": TEXT_ONLY, "revision": other["revision"], "accept_warnings": True},
        status=202,
    )
    await review.post(f"/jobs/{slow['job']['id']}/cancel")
    page = await review.until(
        lambda p: (_model(p, TEXT_ONLY) or {}).get("state") == "incomplete", "the cancel"
    )
    monkeypatch.setattr(demo, "SECONDS_PER_CHUNK", 0.0)
    await review.post(f"/models/{slow['id']}/download", status=202)
    await review.until(
        lambda p: (_model(p, TEXT_ONLY) or {}).get("state") == "installed", "the resume"
    )

    # The Looker, then a Test: an answer, its times and its memory, all made up.
    role = await review.client.put(f"{URL}/roles/looker", json={"model_id": added["id"]})
    assert role.status_code == 200, role.text
    await review.post(f"/models/{added['id']}/test", status=202)
    page = await review.until(
        lambda p: ((_model(p, FIRST) or {}).get("test") or {}).get("state") == "done", "the test"
    )
    model = _model(page, FIRST)
    assert model is not None
    test = model["test"]
    assert test["kind"] == "picture" and test["answer"] and test["demo"] is True
    assert test["seconds"] is not None and test["runtime"] == "demo"
    # A made-up figure, below the one the check judged by: the pill does not move.
    assert test["memory_gb"] is not None and test["memory_gb"] < model["memory_gb"]
    assert model["memory_measured"] is False
    assert page["roles"][0]["model_id"] == added["id"] and page["roles"][0]["ready"] is True

    # Remove: refused while it has the role, then done.
    refused = await review.client.delete(f"{URL}/models/{added['id']}")
    assert refused.status_code == 409
    cleared = await review.client.put(f"{URL}/roles/looker", json={"model_id": None})
    assert cleared.status_code == 200, cleared.text
    removed = await review.client.delete(f"{URL}/models/{added['id']}")
    assert removed.status_code == 200, removed.text

    # Back to the version before.
    back = await review.post("/runtimes/demo/rollback")
    assert back["installed"] == "1.8"

    page = await review.overview()
    assert [m["repo_id"] for m in page["models"]] == [TEXT_ONLY]
    assert all(job["state"] in jobs.FINISHED for job in page["jobs"])


async def test_the_second_program_installs_updates_and_rolls_back_in_demo_mode(
    review: Review,
) -> None:
    """L6 1.3 item 2, through the API: the demo's llama.cpp, with nothing handed in."""
    page = await review.overview()
    llama = _runtime(page, "demo-gguf")
    assert (llama["label"], llama["demo"], llama["installed"]) == (
        fake.DemoGgufKind.label,
        True,
        None,
    )
    assert llama["version_hint"] == "0.9.0 or a daily build such as b12318"
    assert llama["update_note"] == LlamaCpp.update_note

    # Install it by its numbered version: it names the daily build behind it.
    await review.post("/runtimes/demo-gguf/install", {"version": "v0.9.0"}, status=202)
    page = await review.installed("0.9.0", "demo-gguf")
    assert _runtime(page, "demo-gguf")["installed_detail"] == "build b12110"
    # A version that is neither is refused in llama.cpp's own words.
    wrong = await review.client.post(
        f"{URL}/runtimes/demo-gguf/install", json={"version": "latest please"}
    )
    assert wrong.status_code == 400 and "daily build such as b12318" in wrong.text

    # Update to the newest it says is published, then go back.
    await review.post("/runtimes/demo-gguf/install", {"version": None}, status=202)
    page = await review.installed(fake.DemoGgufKind.newest, "demo-gguf", "the update")
    assert _runtime(page, "demo-gguf")["previous"] == "0.9.0"
    back = await review.post("/runtimes/demo-gguf/rollback")
    assert (back["installed"], back["installed_detail"]) == ("0.9.0", "build b12110")
    assert back["update_available"] is True

    # A daily build by its number: never offered an update to a numbered version.
    await review.post("/runtimes/demo-gguf/install", {"version": "B12318"}, status=202)
    page = await review.installed("b12318", "demo-gguf", "the daily build")
    daily = _runtime(page, "demo-gguf")
    assert (daily["installed_detail"], daily["update_available"]) == (None, False)
    # The MLX stand-in was never touched.
    assert _runtime(page)["installed"] is None


async def test_checking_a_gguf_repository_in_demo_mode_lists_its_builds(review: Review) -> None:
    card = await review.post("/preflight", {"name": VISION})

    assert card["ok"] is True and card["runtime"]["name"] == "demo-gguf"
    # Smallest first, with one suggestion; the build named is the suggested one.
    builds = {v["id"]: v for v in card["variants"]}
    assert list(builds) == ["Q4_K_M", "Q8_0", "F16"]
    assert [v["id"] for v in card["variants"] if v["recommended"]] == ["Q4_K_M"]
    assert card["variant"] == "Q4_K_M" and builds["Q4_K_M"]["fits"] == "day"
    # The picture reader is F16 over BF16; the other builds and the file that
    # is not a .gguf are left out.
    parts = {f["path"]: f["part"] for f in builds["Q4_K_M"]["files"]}
    assert parts == {
        "demo-vision-Q4_K_M.gguf": "model",
        "mmproj-demo-vision-F16.gguf": "pictures",
        "README.md": "text",
        "LICENSE": "text",
    }
    # Its program is not installed yet: said in the stand-in's name, not refused.
    assert any(fake.DemoGgufKind.label in warning for warning in card["warnings"])

    # Another build: the figures are that build's.
    other = await review.post("/preflight", {"name": VISION, "variant": "Q8_0"})
    assert other["variant"] == "Q8_0"
    assert other["size_bytes"] > card["size_bytes"] and other["memory_gb"] > card["memory_gb"]

    # Every GGUF repository of demo_data can be checked, refused ones in their own words.
    only_projector = await review.post("/preflight", {"name": "google/demo-projector-only"})
    assert only_projector["ok"] is False
    assert only_projector["problems"] == [
        "This repository holds only the part that reads pictures, not a model."
    ]
    many = await review.post("/preflight", {"name": "example-gguf/demo-many-builds-GGUF"})
    assert many["ok"] is False and many["revision"]  # an unapproved converter, but read


async def test_a_gguf_build_downloads_tests_and_is_asked_about_in_demo_mode(
    review: Review,
) -> None:
    await review.post("/runtimes/demo-gguf/install", {"version": "0.9.0"}, status=202)
    await review.installed("0.9.0", "demo-gguf")
    card = await review.post("/preflight", {"name": VISION})

    # Download, with no build named: the suggested one, in a folder of its own.
    added = await review.post(
        "/models", {"name": VISION, "revision": card["revision"], "variant": None}, status=202
    )
    page = await review.until(
        lambda p: (_model(p, VISION) or {}).get("state") == "installed", "the download"
    )
    model = _model(page, VISION)
    assert model is not None
    assert (model["runtime"], model["variant"], model["demo"]) == ("demo-gguf", "Q4_K_M", True)
    assert (model["needs_compare"], model["compared"]) == (True, False)

    # Test: made up, and the memory it reports is that build's, under the check's figure.
    await review.post(f"/models/{added['id']}/test", status=202)
    page = await review.until(
        lambda p: ((_model(p, VISION) or {}).get("test") or {}).get("state") == "done", "the test"
    )
    model = _model(page, VISION)
    assert model is not None
    test = model["test"]
    assert test["kind"] == "picture" and test["runtime"] == "demo-gguf" and test["demo"] is True
    assert test["memory_gb"] is not None and test["memory_gb"] < model["memory_gb"]

    # The Looker: asked first, because nobody compared it; given on a yes.
    asked = await review.client.put(f"{URL}/roles/looker", json={"model_id": added["id"]})
    assert asked.status_code == 428, asked.text
    assert asked.json()["error"]["code"] == "confirmation_required"
    assert "has not been through Compare" in asked.json()["error"]["message"]
    given = await review.client.put(
        f"{URL}/roles/looker", json={"model_id": added["id"], "skip_compare": True}
    )
    assert given.status_code == 200, given.text
    page = await review.overview()
    looker = next(role for role in page["roles"] if role["role"] == "looker")
    # The card says "not compared yet".
    assert (looker["model_id"], looker["compared"]) == (added["id"], False)
    assert all(job["state"] in jobs.FINISHED for job in page["jobs"])


async def test_the_demo_lane_looks_again_while_something_waits_and_then_stops(
    review: Review, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(demo, "TICK_S", 0.1)
    settings = get_settings()
    # An install the runner itself held back until a moment from now.
    async with review.factory() as db:
        job = await jobs.create(
            db,
            "runtime_install",
            subject="Demo runtime (not a real model) 1.5",
            payload={"name": "demo", "version": "1.5"},
        )
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == job.id)
            .values(state="waiting", not_before=datetime.now(UTC) + timedelta(seconds=0.4))
        )
        await db.commit()

    assert local_ai_tasks.wake(settings) is True
    await asyncio.sleep(0.2)
    assert demo.running(), "the lane gave up on a job that was still waiting"
    # The software is in place a moment before its job is written as done.
    page = await review.until(
        lambda p: (
            _runtime(p)["installed"] == "1.5"
            and all(j["state"] in jobs.FINISHED for j in p["jobs"])
        ),
        "the held job",
    )
    assert [j["state"] for j in page["jobs"]] == ["done"]
    # Nothing is left to wait for: the lane ends by itself.
    for _ in range(100):
        if not demo.running():
            break
        await asyncio.sleep(0.05)
    assert not demo.running()


async def test_a_job_that_ends_while_the_page_is_read_is_listed_once(
    review: Review, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page reads the active jobs, then the finished ones; a job that ends in
    between was listed twice, both times as done."""
    async with review.factory() as db:
        job = await jobs.create(
            db,
            "runtime_install",
            subject="Demo runtime (not a real model) 1.5",
            payload={"name": "demo", "version": "1.5"},
            not_before=datetime.now(UTC) + timedelta(hours=1),
        )
        await db.commit()
    recent = jobs.recent

    async def ends_in_between(db: AsyncSession, limit: int = 15) -> list[LocalAiJob]:
        reads = db.scalars
        count = 0

        async def scalars(*args: Any, **kwargs: Any) -> Any:
            nonlocal count
            count += 1
            if count == 2:  # after the active read, before the finished one
                async with review.factory() as other:
                    await other.execute(
                        update(LocalAiJob)
                        .where(LocalAiJob.id == job.id)
                        .values(state="done", finished_at=datetime.now(UTC))
                    )
                    await other.commit()
            return await reads(*args, **kwargs)

        monkeypatch.setattr(db, "scalars", scalars)
        found = await recent(db, limit)
        assert count == 2, "jobs.recent no longer reads twice; this test no longer races it"
        return found

    monkeypatch.setattr(jobs, "recent", ends_in_between)
    page = await review.overview()
    assert [(j["id"], j["state"]) for j in page["jobs"]] == [(str(job.id), "done")]


async def test_reading_the_page_starts_the_lane_after_a_restart(review: Review) -> None:
    """A job left queued (the API restarted under it) is run once somebody opens the page."""
    async with review.factory() as db:
        await jobs.create(
            db,
            "runtime_install",
            subject="Demo runtime (not a real model) 1.6",
            payload={"name": "demo", "version": "1.6"},
        )
        await db.commit()
    assert not demo.running()
    await review.until(lambda p: _runtime(p)["installed"] == "1.6", "the queued job")


async def test_a_page_read_again_and_again_with_nothing_to_do_does_not_keep_starting_the_lane(
    review: Review, monkeypatch: pytest.MonkeyPatch
) -> None:
    looks: list[int] = []
    run_next = jobs.run_next

    async def counted(*args: Any, **kwargs: Any) -> bool:
        looks.append(1)
        return await run_next(*args, **kwargs)

    monkeypatch.setattr(jobs, "run_next", counted)

    for _ in range(5):  # the page, polled
        await review.overview()
        for _ in range(100):  # each time, until the lane it may have started has ended
            if not demo.running():
                break
            await asyncio.sleep(0.02)
        assert not demo.running()
    # The first read looked, as after a restart it must; the rest waited for beat's minute.
    assert len(looks) == 1

    # New work is not held to that minute: asking for it wakes the lane at once.
    await review.post("/runtimes/demo/install", {"version": "1.7"}, status=202)
    await review.until(lambda p: _runtime(p)["installed"] == "1.7", "the install")
    assert len(looks) > 1


# --- the seed ---------------------------------------------------------------------


def _run_seed(root: Path, monkeypatch: pytest.MonkeyPatch, *more: str) -> None:
    """`python infrastructure/scripts/seed_dev.py <root> …`, in this process."""
    url = f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}"
    # The seed puts these in the environment itself; set here so they go back after.
    for name, value in {
        "FRAMEFOUND_DATABASE_URL": url,
        "FRAMEFOUND_DATA_DIR": str(root / "data"),
        "FRAMEFOUND_MEDIA_ROOT": str(root / "media"),
        "FRAMEFOUND_MODELS_DIR": str(root / "models"),
        "FRAMEFOUND_LOCAL_AI_DEMO": "1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", [str(SEED), str(root), *more])
    # The script runs its own event loop; here it must have a thread of its own.
    failed: list[BaseException] = []

    def run() -> None:
        try:
            runpy.run_path(str(SEED), run_name="__main__")
        except BaseException as err:  # noqa: BLE001 - handed back to the test to raise
            failed.append(err)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(300)
    assert not worker.is_alive(), "the seed did not finish"
    if failed:
        raise failed[0]
    get_settings.cache_clear()


async def test_the_seed_fills_every_section_of_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "ff-ui"
    root.mkdir()
    await _schema(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")

    before = datetime.now(UTC)
    _run_seed(root, monkeypatch)
    ran = datetime.now(UTC)
    assert "Added 4 demo models" in capsys.readouterr().out

    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "local-models-demo-secret")
    get_settings.cache_clear()
    engine = create_async_engine(get_settings().db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            setup = await client.post(
                "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
            )
            assert setup.status_code == 201, setup.text
            page = await Review(client, factory).overview()
            assert demo.stop()
    finally:
        await engine.dispose()

    assert page["mode"] == "demo"
    models = {m["repo_id"]: m for m in page["models"]}
    assert set(models) == {
        demo.LOOKER_MODEL,
        demo.EDITOR_MODEL,
        demo.DROPPED_MODEL,
        demo.GGUF_MODEL,
    }
    looker, editor, dropped, gguf = (
        models[demo.LOOKER_MODEL],
        models[demo.EDITOR_MODEL],
        models[demo.DROPPED_MODEL],
        models[demo.GGUF_MODEL],
    )
    # Installed models, on this machine, each marked as made up.
    assert looker["state"] == editor["state"] == "installed"
    assert looker["on_this_machine"] is True and editor["on_this_machine"] is True
    assert all(m["demo"] for m in models.values())
    # Who does what: both roles held, the Editor only overnight.
    roles = {r["role"]: r for r in page["roles"]}
    assert roles["looker"]["model_id"] == looker["id"] and roles["looker"]["ready"] is True
    assert roles["editor"]["model_id"] == editor["id"]
    assert editor["fits"] == "night"
    # The software: in use, one to go back to, a newer one to update to.
    runtime = _runtime(page)
    assert (runtime["installed"], runtime["previous"], runtime["latest"]) == ("1.9", "1.8", "2.0")
    assert runtime["update_available"] is True
    # The second program: not installed, and needed by the GGUF build, which
    # is the suggested one and has no role (giving it one asks a question first).
    assert [r["name"] for r in page["runtimes"]] == ["demo", "demo-gguf"]
    llama = _runtime(page, "demo-gguf")
    assert (llama["installed"], llama["models"]) == (None, 1)
    assert (gguf["runtime"], gguf["variant"], gguf["runtime_installed"]) == (
        "demo-gguf",
        "Q4_K_M",
        False,
    )
    assert gguf["roles"] == [] and gguf["needs_compare"] is True
    # A finished Test that looked at a photograph of the catalogue.
    test = looker["test"]
    assert test["state"] == "done" and test["kind"] == "picture" and test["answer"]
    assert test["sample"]["asset_id"] is not None and test["memory_gb"] is not None
    # A job in progress: the Editor's Test, waiting for the overnight window.
    waiting = editor["test"]
    assert waiting["state"] == "waiting" and waiting["not_before"]
    assert "overnight window" in waiting["message"]
    # A download that dropped, kept to be resumed.
    assert dropped["state"] == "incomplete"
    assert dropped["error"].startswith("The connection dropped at 7.8 of 15.6 GB.")
    states = [(j["kind"], j["state"]) for j in page["jobs"]]
    assert ("test", "waiting") in states and ("download", "failed") in states
    assert states.count(("runtime_install", "done")) == 2
    # Every time on the page is one the seed lived through, whatever the hour it
    # ran: nothing ended in the future, or before it was asked for.
    for job in page["jobs"]:
        times = [job["created_at"], job["started_at"], job["finished_at"]]
        asked, started, ended = (
            None if at is None else datetime.fromisoformat(at).astimezone(UTC) for at in times
        )
        assert asked is not None and before <= asked <= ran, job
        if ended is not None:
            assert started is not None and asked <= started <= ended <= ran, job
    # The waiting Test never started; Recent activity reads newest first, in the
    # order the seed did things (the dropped download last of all).
    assert waiting["state"] == "waiting"
    [held] = [j for j in page["jobs"] if j["state"] == "waiting"]
    assert held["started_at"] is None and held["finished_at"] is None
    ended_jobs = [j for j in page["jobs"] if j["finished_at"] is not None]
    assert [j["kind"] for j in ended_jobs] == [
        "download",
        "download",
        "test",
        "download",
        "download",
        "runtime_install",
        "runtime_install",
    ]
    assert len({j["finished_at"] for j in ended_jobs}) == len(ended_jobs)


def _local(moment: datetime | None) -> datetime | None:
    """A time read from the table (UTC, without its zone on SQLite) on this machine's clock."""
    return None if moment is None else moment.replace(tzinfo=UTC).astimezone().replace(tzinfo=None)


async def _waiting(factory: async_sessionmaker[AsyncSession]) -> list[LocalAiJob]:
    async with factory() as db:
        found = await db.scalars(select(LocalAiJob).where(LocalAiJob.state == "waiting"))
        return list(found.all())


async def _before_the_gguf_build(
    factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Make a seeded catalogue the one a seed from before the GGUF build left:
    no row for the build, no job of its, none of its files, and no note that
    a seed ever added it."""
    async with factory() as db:
        gguf = await db.scalar(select(LocalModel).where(LocalModel.repo_id == demo.GGUF_MODEL))
        assert gguf is not None
        library.remove(registry.snapshot(gguf), settings)
        await db.execute(delete(LocalAiJob).where(LocalAiJob.model_id == gguf.id))
        await db.delete(gguf)
        await db.execute(delete(AppSetting).where(AppSetting.key == demo.SEEDED_KEY))
        await db.commit()
    assert not list((settings.models_dir / "library").rglob("Q4_K_M/manifest.json"))


async def _removed_by_a_reviewer(
    factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Remove on the AI models page: the build's files and its row go (its jobs
    stay as history, as the page leaves them)."""
    async with factory() as db:
        gguf = await db.scalar(select(LocalModel).where(LocalModel.repo_id == demo.GGUF_MODEL))
        assert gguf is not None
        library.remove(registry.snapshot(gguf), settings)
        await registry.delete_model(db, gguf.id)
        await db.commit()


async def _models(factory: async_sessionmaker[AsyncSession]) -> list[str]:
    async with factory() as db:
        return sorted((await db.scalars(select(LocalModel.repo_id))).all())


async def test_the_seed_adds_its_models_once_and_puts_its_waiting_test_back(
    tmp_path: Path,
) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'ui.db').as_posix()}"
    settings = _settings(tmp_path, local_ai_demo=True, database_url=url)
    await _schema(url)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # An empty catalogue: the Test is shown the drawn sample instead.
        first = await demo.seed(factory, settings, now=datetime(2026, 11, 12, 9, 0))
        again = await demo.seed(factory, settings, now=datetime(2026, 11, 12, 9, 5))
        async with factory() as db:
            count = len((await db.scalars(select(LocalModel))).all())
        [waiting] = await _waiting(factory)
        assert first.startswith("Added 4 demo models") and again == demo.ALREADY_SEEDED
        assert count == 4
        # Asked for in the morning, it waits for that evening's window, and has
        # never started (it is not run to be made to wait).
        assert _local(waiting.not_before) == datetime(2026, 11, 12, 21, 30)
        assert waiting.started_at is None and waiting.beat_at is None
        assert waiting.message.startswith("Waiting for the overnight window (starts 21:30)")

        # The window opened and the demo's lane ran it: seeding again puts one back,
        # and adds no model.
        async with factory() as db:
            await db.execute(
                update(LocalAiJob)
                .where(LocalAiJob.id == waiting.id)
                .values(state="done", not_before=None, finished_at=datetime.now(UTC))
            )
            await db.commit()
        later = await demo.seed(factory, settings, now=datetime(2026, 11, 13, 21, 45))
        [back] = await _waiting(factory)
        async with factory() as db:
            count = len((await db.scalars(select(LocalModel))).all())
        assert later == demo.WAITING_AGAIN and count == 4
        # Asked for in the evening, it waits for the next night's window.
        assert back.id != waiting.id and _local(back.not_before) == datetime(2026, 11, 14, 21, 30)
        assert await demo.seed(factory, settings) == demo.ALREADY_SEEDED
        assert len(await _waiting(factory)) == 1

        # A catalogue seeded before the GGUF build existed gets it, and only it.
        await _before_the_gguf_build(factory, settings)
        assert await demo.seed(factory, settings) == demo.GGUF_AGAIN
        repos = await _models(factory)
        assert len(repos) == 4 and demo.GGUF_MODEL in repos
        assert await demo.seed(factory, settings) == demo.ALREADY_SEEDED

        # With no overnight window the Editor would never wait for one: none is left.
        async with factory() as db:
            await db.execute(update(LocalAiJob).values(state="cancelled"))
            await db.commit()
            await settings_store.save_config(
                db, LocalModelsConfig(night_start="21:30", night_end="21:30")
            )
            await db.commit()
        assert await demo.seed(factory, settings) == demo.NO_WAIT
        assert await _waiting(factory) == []
        # Outside demo mode the seed refuses to make anything.
        with pytest.raises(RuntimeError):
            await demo.seed(factory, _settings(tmp_path, database_url=url))
    finally:
        await engine.dispose()


async def test_seeding_a_reviewers_catalogue_again_runs_none_of_the_work_waiting_there(
    tmp_path: Path,
) -> None:
    """The seed's own process ran the lane on a catalogue it had already filled,
    to download the GGUF build. The lane takes the most urgent job first,
    whoever asked for it: a reviewer's queued draft, look or review was run
    there, or ended as failed ("This version of FrameFound does not know how
    to do this.") where that process had not loaded its kind."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'ui.db').as_posix()}"
    settings = _settings(tmp_path, local_ai_demo=True, database_url=url)
    await _schema(url)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await demo.seed(factory, settings, now=datetime(2026, 11, 12, 9, 0))
        await _before_the_gguf_build(factory, settings)
        # What a reviewer left: a draft asked for with a button (so more urgent
        # than anything the seed asks for), a review of a folder, a look held
        # back until a time that has come, and one still held.
        due = datetime.now(UTC) - timedelta(hours=1)
        async with factory() as db:
            looker = await db.scalar(
                select(LocalModel).where(LocalModel.repo_id == demo.LOOKER_MODEL)
            )
            assert looker is not None
            db.add_all(
                [
                    LocalAiJob(kind="draft_cut", subject="Pete Dye, a quick 30", priority=0),
                    LocalAiJob(kind="look_folder", subject="GELCO", model_id=looker.id),
                    LocalAiJob(
                        kind="clip_look",
                        subject="DJI_0265_D.mp4",
                        model_id=looker.id,
                        state="waiting",
                        message="Paused.",
                        not_before=due,
                    ),
                    LocalAiJob(
                        kind="listing_look",
                        subject="130 Davis Rd Auction",
                        model_id=looker.id,
                        state="waiting",
                        not_before=datetime.now(UTC) + timedelta(hours=8),
                    ),
                ]
            )
            await db.commit()

        async def left() -> dict[Any, tuple[Any, ...]]:
            async with factory() as db:
                rows = (await db.scalars(select(LocalAiJob))).all()
            return {
                row.id: (row.kind, row.state, row.message, row.started_at, row.finished_at)
                for row in rows
            }

        before = await left()
        assert sum(1 for kind, state, *_rest in before.values() if state in jobs.ACTIVE) == 5

        said = await demo.seed(factory, settings, now=datetime(2026, 11, 13, 9, 0))

        after = await left()
        # Every job that was there is as it was: none run, none failed, none released.
        assert {job_id: after[job_id] for job_id in before} == before
        # The one new job is the build's download, asked for and left for the review server.
        [(kind, state, _message, started, ended)] = [
            row for job_id, row in after.items() if job_id not in before
        ]
        assert (kind, state, started, ended) == ("download", "queued", None, None)
        assert said == demo.GGUF_AGAIN and "waiting its turn" in said
        async with factory() as db:
            gguf = await db.scalar(select(LocalModel).where(LocalModel.repo_id == demo.GGUF_MODEL))
        assert gguf is not None and gguf.state == registry.DOWNLOADING
        assert not list((tmp_path / "models" / "library").rglob("Q4_K_M/manifest.json"))

        # The review server's lane then does it, as when the page is next opened
        # (with the reviewer's own jobs out of the way: they are not this test's).
        async with factory() as db:
            await db.execute(
                delete(LocalAiJob).where(LocalAiJob.kind != "download", LocalAiJob.kind != "test")
            )
            await db.commit()
        for _ in range(10):
            if not await jobs.run_next(factory, demo.deps(settings, slow=False)):
                break
        async with factory() as db:
            gguf = await db.scalar(select(LocalModel).where(LocalModel.repo_id == demo.GGUF_MODEL))
        assert gguf is not None and gguf.state == registry.INSTALLED
        assert list((tmp_path / "models" / "library").rglob("Q4_K_M/manifest.json"))
    finally:
        await engine.dispose()


async def test_a_gguf_build_a_reviewer_removed_or_refused_is_not_put_back(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'ui.db').as_posix()}"
    settings = _settings(tmp_path, local_ai_demo=True, database_url=url)
    await _schema(url)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    morning = datetime(2026, 11, 13, 9, 0)
    three = sorted([demo.LOOKER_MODEL, demo.EDITOR_MODEL, demo.DROPPED_MODEL])
    try:
        await demo.seed(factory, settings, now=datetime(2026, 11, 12, 9, 0))

        # Removed on the page: the next seed says nothing was added, and adds nothing.
        await _removed_by_a_reviewer(factory, settings)
        assert await demo.seed(factory, settings, now=morning) == demo.ALREADY_SEEDED
        assert await _models(factory) == three

        # A catalogue from before the build, whose reviewer has blocked the
        # build's publisher: its own rules stand, and no model is claimed.
        async with factory() as db:
            await db.execute(delete(AppSetting).where(AppSetting.key == demo.SEEDED_KEY))
            await db.commit()
            await settings_store.save_config(db, LocalModelsConfig(blocked_added=["google"]))
        assert await demo.seed(factory, settings, now=morning) == demo.ALREADY_SEEDED
        assert await _models(factory) == three

        # Unblocked, it is tried again; and with no Test of the Editor left
        # waiting, the one sentence says both things that were done.
        async with factory() as db:
            await settings_store.save_config(db, LocalModelsConfig())
            await db.execute(
                update(LocalAiJob)
                .where(LocalAiJob.state == "waiting")
                .values(state="done", not_before=None, finished_at=datetime.now(UTC))
            )
            await db.commit()
        said = await demo.seed(factory, settings, now=morning)
        assert said == f"{demo.GGUF_AGAIN} {demo.WAITING_TOO}"
        assert await _models(factory) == sorted([*three, demo.GGUF_MODEL])
        [waiting] = await _waiting(factory)
        assert waiting.kind == "test"
        assert _local(waiting.not_before) == datetime(2026, 11, 13, 21, 30)

        # A catalogue filled by a seed that kept no note (the build is there,
        # the note is not): it is noted now, so removing it later is kept too.
        async with factory() as db:
            await db.execute(delete(AppSetting).where(AppSetting.key == demo.SEEDED_KEY))
            await db.commit()
        assert await demo.seed(factory, settings, now=morning) == demo.ALREADY_SEEDED
        await _removed_by_a_reviewer(factory, settings)
        assert await demo.seed(factory, settings, now=morning) == demo.ALREADY_SEEDED
        assert await _models(factory) == three
    finally:
        await engine.dispose()


async def test_the_seeded_gguf_build_is_installed_in_a_folder_of_its_own(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'ui.db').as_posix()}"
    settings = _settings(tmp_path, local_ai_demo=True, database_url=url)
    await _schema(url)
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await demo.seed(factory, settings, now=datetime(2026, 11, 12, 9, 0))
        async with factory() as db:
            gguf = await db.scalar(select(LocalModel).where(LocalModel.repo_id == demo.GGUF_MODEL))
    finally:
        await engine.dispose()
    assert gguf is not None
    assert (gguf.state, gguf.runtime, gguf.variant) == ("installed", "demo-gguf", "Q4_K_M")
    [manifest_path] = (tmp_path / "models" / "library").rglob("Q4_K_M/manifest.json")
    manifest = files.read_manifest(manifest_path.parent)
    assert manifest is not None and manifest.demo is True
    assert sorted(entry.path for entry in manifest.files) == [
        "LICENSE",
        "README.md",
        "demo-vision-Q4_K_M.gguf",
        "mmproj-demo-vision-F16.gguf",
    ]


def test_no_demo_model_comes_from_a_blocked_publisher() -> None:
    from framefound.local_models import policy

    blocked = {name.casefold() for name in policy.DEFAULT_BLOCKED}
    for repo_id in (demo.LOOKER_MODEL, demo.EDITOR_MODEL, demo.DROPPED_MODEL, demo.GGUF_MODEL):
        publisher = repo_id.split("/")[0].casefold()
        assert publisher not in blocked
        assert publisher in {"mlx-community", "google", "allenai", "nvidia", "apple"}
