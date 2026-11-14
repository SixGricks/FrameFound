"""The AI models page's API, part one (api/v1/local_models.py; design L1 6.1–6.8, 6.11).

Through the real app and its routes. Hugging Face is `fake.FakeHub` (models
shrunk to a few kilobytes so a download really passes its checks), the model
server is `fake.FakeModelServer`, the installer `fake.FakeRunner`, the clock a
`Clock` the test moves: all handed in through the one `get_deps` override.
The queue is a list: `wake` records that it was called, and a test runs the
lane itself (`jobs.run_next`) when it wants the work done. The database is
file-backed SQLite. Starting a process or reaching a real address fails the
test.
"""

import asyncio
import io
import json
import subprocess
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.api.v1 import local_models as api
from framefound.auth import service
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import (
    AppSetting,
    Asset,
    AuditLog,
    Derivative,
    Library,
    LocalAiJob,
    LocalModel,
)
from framefound.local_models import (
    UNAVAILABLE,
    comparisons,
    fake,
    files,
    gate,
    handlers,
    jobs,
    library,
    llama_release,
    policy,
    preflight,
    registry,
    runtimes,
)
from framefound.local_models.ask import RECORD_DAMAGED
from framefound.local_models.deps import Deps
from framefound.local_models.hub import HubError
from framefound.local_models.kinds import LlamaCpp, MlxVlm, RuntimeKind
from framefound.processing import local_ai_tasks
from framefound.storage import stores

Factory = async_sessionmaker[AsyncSession]

URL = "/api/v1/local-models"
ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
READER = {"email": "reader@example.com", "password": "another-strong-password"}
TOKEN = "not-a-real-token"  # noqa: S105 - a stand-in, never a real one
FIRST = "mlx-community/gemma-4-e4b-it-4bit"
FIRST_BUILD = "475b9088d29754a3379866cf5aeb6b41acd313c2"
TEXT_ONLY = "mlx-community/gemma-4-12B-it-OptiQ-4bit"
TEXT_ONLY_BUILD = "de9e183868050b34bf6b4272518500ffcebf15b3"
STRANGER = "example-lab/some-model"
LARGE = "mlx-community/demo-large-4bit"
LARGE_BUILD = "9" * 40
VERSION = "0.7.12"
NOON = datetime(2026, 11, 12, 12, 0)

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
    # This machine's memory is the test's to say (Windows has no sysconf).
    monkeypatch.setattr(api, "_physical_memory_gb", lambda: None)

    def clear() -> None:
        get_settings.cache_clear()
        stores._CACHE.clear()
        api._STATUS.clear()
        api._LOOKING.clear()

    clear()
    yield
    clear()


class Clock:
    """The Mac's wall clock, naive, moved by the test."""

    def __init__(self, at: datetime = NOON) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


class CountingServer(fake.FakeModelServer):
    """The fake model server, counting how often it is asked what is loaded."""

    def __init__(self) -> None:
        super().__init__(sleep=lambda _seconds: None)
        self.asked = 0

    def status(self) -> dict[str, Any] | None:
        self.asked += 1
        return super().status()


class Site:
    """A Mac running FrameFound, as the API sees it."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        client: AsyncClient,
        app: Any,
        factory: Factory,
    ) -> None:
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.client = client
        self.app = app
        self.factory = factory
        self.runtimes = tmp_path / "runtimes"
        self.nas = tmp_path / "nas"
        self.hub = fake.FakeHub.from_fixtures(sleep=lambda _seconds: None)
        for repo in (FIRST, TEXT_ONLY):
            self.hub.shrink(repo)
        self.server = CountingServer()
        self.clock = Clock()
        self.kinds: dict[str, RuntimeKind] = {"mlx-vlm": MlxVlm()}
        self.woken: list[bool] = []
        self.queue_up = True
        self.run = fake.FakeRunner()
        self.machine = "arm64"
        self.index: Any = lambda _url, **_more: {"info": {"version": "0.7.13"}}

    def deps(self) -> Deps:
        return Deps(
            settings=get_settings(),
            hub_for=self.hub.with_token,
            fetch_json=lambda url, **more: self.index(url, **more),
            server=self.server,
            kinds=self.kinds,
            run=self.run,
            sleep=lambda _seconds: None,
            now=self.clock,
            system="Darwin",
            machine=self.machine,
        )

    def wake(self, _settings: Any = None) -> bool:
        self.woken.append(self.queue_up)
        return self.queue_up

    def install_mlx_vlm(self) -> None:
        runtimes.install(MlxVlm(), VERSION, self.runtimes, run=installer())
        runtimes.activate(self.runtimes, "mlx-vlm", VERSION)

    def unconfigured(self) -> None:
        """The containers: no runtimes folder."""
        self.monkeypatch.delenv("FRAMEFOUND_RUNTIMES_DIR")
        self.kinds = {}
        get_settings.cache_clear()

    def library_on_the_nas(self) -> None:
        stores.mark_bulk_root(self.nas)
        self.monkeypatch.setenv("FRAMEFOUND_BULK_DIR", str(self.nas))
        get_settings.cache_clear()
        stores._CACHE.clear()

    def nas_drops(self) -> None:
        (self.nas / "models" / stores.BULK_STORE_MARKER).unlink()
        stores._CACHE.clear()

    async def drain(self) -> None:
        """What the lane does when woken: run jobs until none is queued."""
        for _ in range(20):
            if not await jobs.run_next(self.factory, self.deps()):
                return
        raise AssertionError("the lane never ran out of work")

    async def overview(self, client: AsyncClient | None = None) -> dict[str, Any]:
        api._STATUS.clear()
        answer = await (client or self.client).get(URL)
        assert answer.status_code == 200, answer.text
        found: dict[str, Any] = answer.json()
        return found

    async def add(self, repo: str = FIRST, build: str = FIRST_BUILD, **more: Any) -> dict[str, Any]:
        answer = await self.client.post(
            f"{URL}/models", json={"name": repo, "revision": build, **more}
        )
        assert answer.status_code == 202, answer.text
        found: dict[str, Any] = answer.json()
        return found

    async def installed(self, repo: str = FIRST, build: str = FIRST_BUILD) -> str:
        """A model added through the page and downloaded by the lane; its id."""
        added = await self.add(repo, build, accept_warnings=True)
        await self.drain()
        return str(added["id"])

    async def needs(self, model_id: str, memory_gb: float) -> None:
        """What a model is taken to need: the real build's figure, where the
        files a test downloads are shrunk to a few kilobytes."""
        async with self.factory() as db:
            await db.execute(
                update(LocalModel)
                .where(LocalModel.id == uuid.UUID(model_id))
                .values(memory_gb=memory_gb)
            )
            await db.commit()

    async def events(self) -> list[str]:
        async with self.factory() as db:
            rows = await db.scalars(select(AuditLog.event).order_by(AuditLog.id))
            return [event for event in rows if event.startswith("local_models.")]

    async def models(self) -> list[LocalModel]:
        async with self.factory() as db:
            return list((await db.scalars(select(LocalModel))).all())

    async def job_rows(self) -> list[LocalAiJob]:
        async with self.factory() as db:
            return list((await db.scalars(select(LocalAiJob))).all())

    async def held_back(self, subject: str, until: datetime) -> uuid.UUID:
        """A test the runner held back itself (the window, a pause) until `until`."""
        async with self.factory() as db:
            job = await jobs.create(db, "test", subject=subject)
            await db.execute(
                update(LocalAiJob)
                .where(LocalAiJob.id == job.id)
                .values(state="waiting", not_before=until.astimezone(UTC))
            )
            await db.commit()
            return job.id

    async def reader(self) -> AsyncClient:
        async with self.factory() as db:
            await service.create_user(db, READER["email"], READER["password"], "user")
            await db.commit()
        client = AsyncClient(transport=ASGITransport(app=self.app), base_url="http://t")
        signed_in = await client.post("/api/v1/auth/login", json=READER)
        assert signed_in.status_code == 200, signed_in.text
        return client


@pytest.fixture()
async def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Site]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'catalogue.db').as_posix()}"
    for folder in ("data", "models", "runtimes", "nas"):
        (tmp_path / folder).mkdir()
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "local-models-page-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRAMEFOUND_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("FRAMEFOUND_RUNTIMES_DIR", str(tmp_path / "runtimes"))
    monkeypatch.setenv("FRAMEFOUND_MIN_FREE_GB", "0")
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
    app.dependency_overrides[get_session] = session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        made = Site(tmp_path, monkeypatch, client, app, factory)
        app.dependency_overrides[api.get_deps] = made.deps
        monkeypatch.setattr(local_ai_tasks, "wake", made.wake)
        setup = await client.post(
            "/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN}
        )
        assert setup.status_code == 201, setup.text
        made.install_mlx_vlm()
        yield made
    await engine.dispose()


def installer() -> fake.FakeRunner:
    """`uv` and a runtime's own check, answering as they do when an install goes well."""

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


def message(answer: httpx.Response) -> str:
    found: str = answer.json()["error"]["message"]
    return found


# --- where models cannot run ------------------------------------------------------------


async def test_with_nothing_configured_the_page_says_models_run_only_on_a_mac(
    site: Site,
) -> None:
    site.unconfigured()
    page = await site.overview()
    assert page["mode"] == "unavailable" and page["note"] == UNAVAILABLE
    assert page["can_manage"] is False
    assert (page["roles"], page["models"], page["runtimes"], page["jobs"]) == ([], [], [], [])
    assert page["server"]["state"] == "down" and page["library"] is None
    # The settings and lists still read (the page has nothing to show them on).
    assert page["publishers"]["makers"] == list(policy.DEFAULT_MAKERS)
    # Nothing else was asked.
    assert site.server.asked == 0 and site.hub.calls == []


async def test_on_a_server_without_runtimes_every_write_is_refused_plainly(site: Site) -> None:
    site.unconfigured()
    somebody = uuid.uuid4()
    for method, path, body in (
        ("PUT", "/settings", {"paused": True}),
        ("PUT", "/publishers", {"makers": ["google"]}),
        ("PUT", "/token", {"token": TOKEN}),
        ("POST", "/preflight", {"name": FIRST}),
        ("POST", "/models", {"name": FIRST, "revision": FIRST_BUILD}),
        ("POST", f"/models/{somebody}/download", None),
        ("DELETE", f"/models/{somebody}", None),
        ("POST", f"/jobs/{somebody}/cancel", None),
        ("PUT", "/roles/looker", {"model_id": str(somebody)}),
        ("PUT", "/roles/looker", {"model_id": None}),
        ("POST", f"/models/{somebody}/test", {"asset_id": None}),
        ("POST", "/runtimes/mlx-vlm/check", None),
        ("POST", "/runtimes/mlx-vlm/install", {"version": None}),
        ("POST", "/runtimes/mlx-vlm/rollback", None),
        ("POST", "/server/unload", None),
    ):
        answer = await site.client.request(method, URL + path, json=body)
        assert answer.status_code == 409, path
        assert message(answer) == UNAVAILABLE, path
    assert await site.events() == [] and site.hub.calls == [] and site.woken == []
    async with site.factory() as db:
        assert await db.get(AppSetting, "local_models") is None


# --- looking ------------------------------------------------------------------------------


async def test_the_overview_draws_the_whole_page_without_asking_the_network(
    site: Site,
) -> None:
    model_id = await site.installed()
    site.hub.calls.clear()
    page = await site.overview()
    assert site.hub.calls == []
    assert page["mode"] == "ready" and page["note"] is None and page["can_manage"] is True
    assert page["machine"] == {"system": "Darwin", "machine": "arm64", "memory_gb": None}
    assert page["now"] == {
        "local_time": "2026-11-12T12:00:00",
        "overnight": False,
        "budget_gb": 8.0,
        "changes_at": "2026-11-12T21:30:00",
    }
    assert page["settings"]["paused"] is False
    assert page["settings"]["hf_token_configured"] is False
    blocked = page["publishers"]["blocked"]
    assert blocked[0] == {"name": policy.DEFAULT_BLOCKED[0], "built_in": True}
    assert all(entry["built_in"] for entry in blocked)
    assert page["server"]["state"] == "idle"
    assert page["library"]["where"] == "local" and page["library"]["connected"] is True
    assert page["library"]["copies_to_this_machine"] is False

    [model] = page["models"]
    assert model["id"] == model_id and model["name"] == "gemma-4-e4b-it-4bit"
    assert model["revision_short"] == FIRST_BUILD[:7]
    assert model["url"] == f"https://huggingface.co/{FIRST}/tree/{FIRST_BUILD}"
    assert (model["publisher_is"], model["maker"]) == ("converter", "google")
    assert [step["repo_id"] for step in model["built_from"]] == [
        "google/gemma-4-E4B-it",
        "google/gemma-4-E4B",
    ]
    assert model["approved"] is True and model["approval_problem"] is None
    assert (model["state"], model["in_library"], model["on_this_machine"]) == (
        "installed",
        True,
        True,
    )
    assert model["fits"] == "day" and model["memory_measured"] is False
    assert model["takes"] == ["text", "image"]
    assert (model["runtime_label"], model["runtime_installed"], model["runtime_problem"]) == (
        "MLX-VLM",
        True,
        None,
    )
    assert model["role_problems"] == {"looker": None, "editor": None}
    assert model["job"] is None and model["test"] is None
    assert model["added_by"] == ADMIN["email"]

    looker, editor = page["roles"]
    assert (looker["role"], looker["title"], looker["model_id"]) == ("looker", "Looker", None)
    assert editor["description"].startswith("Reads a brief")

    [runtime] = page["runtimes"]
    assert (runtime["name"], runtime["label"], runtime["installed"]) == (
        "mlx-vlm",
        "MLX-VLM",
        VERSION,
    )
    assert runtime["supported"] is True and runtime["update_available"] is False

    [download] = page["jobs"]
    assert (download["kind"], download["state"], download["title"]) == (
        "download",
        "done",
        "Downloading gemma-4-e4b-it-4bit",
    )
    assert download["subject"] == f"{FIRST}@{FIRST_BUILD[:8]}"
    assert download["requested_by"] == ADMIN["email"] and download["can_cancel"] is False


async def test_the_overview_says_what_the_model_server_has_loaded(site: Site) -> None:
    model_id = await site.installed()
    async with site.factory() as db:
        model = await registry.get_model(db, uuid.UUID(model_id))
        assert model is not None
        folder = library.library_dir(registry.snapshot(model))
    site.server.chat(
        {"model": str(folder), "messages": [{"role": "user", "content": "ok"}]},
        idle_s=300,
        timeout_s=300,
    )
    loaded = (await site.overview())["server"]
    assert loaded["state"] == "ready" and loaded["model_id"] == model_id
    assert loaded["model_name"] == "gemma-4-e4b-it-4bit"

    site.server.down = True
    assert (await site.overview())["server"] == {
        "state": "down",
        "model_id": None,
        "model_name": None,
        "runtime": None,
        "runtime_version": None,
        "memory_gb": None,
        "loaded_for_s": None,
        "unloads_in_s": None,
        "problem": None,
    }


async def test_a_role_says_whether_its_model_is_ready(site: Site) -> None:
    model_id = uuid.UUID(await site.installed())
    async with site.factory() as db:
        model = await registry.get_model(db, model_id)
        await registry.set_role(db, "looker", model, pending=False, user_id=None)
        await db.commit()
    looker = (await site.overview())["roles"][0]
    assert looker["model_id"] == str(model_id) and looker["ready"] is True
    assert looker["problem"] is None and looker["assigned_at"] is not None

    # With the software gone, the role says so.
    (site.runtimes / "mlx-vlm" / "state.json").unlink()
    looker = (await site.overview())["roles"][0]
    assert (looker["ready"], looker["problem"]) == (False, "MLX-VLM is not installed.")


async def test_a_reader_can_look_but_not_change(site: Site) -> None:
    model_id = await site.installed()
    async with site.factory() as db:
        job = await jobs.create(db, "test", subject="something", model_id=uuid.UUID(model_id))
        job.detail = f"Traceback in {site.tmp}"
        await db.commit()
    reader = await site.reader()
    try:
        page = await site.overview(reader)
        assert page["can_manage"] is False and len(page["models"]) == 1
        assert page["library"]["path"] is None
        assert json.dumps(str(site.tmp))[1:-1] not in json.dumps(page)
        admin_page = await site.overview()
        assert admin_page["library"]["path"] == str(site.tmp / "models" / "library")
        assert json.dumps(str(site.tmp))[1:-1] in json.dumps(admin_page)
        for method, path, body in (
            ("PUT", "/settings", {"paused": True}),
            ("PUT", "/publishers", {"makers": ["google"]}),
            ("PUT", "/token", {"token": TOKEN}),
            ("POST", "/preflight", {"name": FIRST}),
            ("POST", "/models", {"name": FIRST, "revision": FIRST_BUILD}),
            ("POST", f"/models/{model_id}/download", None),
            ("DELETE", f"/models/{model_id}", None),
            ("POST", f"/jobs/{job.id}/cancel", None),
            ("PUT", "/roles/looker", {"model_id": model_id}),
            ("PUT", "/roles/editor", {"model_id": None}),
            ("POST", f"/models/{model_id}/test", None),
            ("POST", "/runtimes/mlx-vlm/check", None),
            ("POST", "/runtimes/mlx-vlm/install", {"version": "0.7.13"}),
            ("POST", "/runtimes/mlx-vlm/rollback", None),
            ("POST", "/server/unload", None),
        ):
            answer = await reader.request(method, URL + path, json=body)
            assert answer.status_code == 403, path
        # The drawn sample is anyone's to look at.
        drawn = await reader.get(f"{URL}/sample.jpg")
        assert drawn.status_code == 200 and drawn.content[:3] == b"\xff\xd8\xff"
    finally:
        await reader.aclose()
    assert len(await site.models()) == 1
    assert [row.state for row in await site.job_rows() if row.kind == "test"] == ["queued"]


# --- settings, lists, token ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "said"),
    [
        ({"night_start": "9pm"}, "Give the time as hours and minutes, like 21:30."),
        ({"night_end": "25:00"}, "Give the time as hours and minutes, like 21:30."),
        ({"night_start": "05:30"}, api.WINDOW_NEVER_OPENS),
        ({"day_budget_gb": 0}, api.BAD_BUDGET),
        ({"night_budget_gb": 600}, api.BAD_BUDGET),
        ({"day_budget_gb": "a lot"}, api.BAD_BUDGET),
        ({"day_budget_gb": 24}, api.NIGHT_SMALLER),
        ({"idle_unload_minutes": 0}, api.BAD_IDLE),
        ({"idle_unload_minutes": 2.5}, api.BAD_IDLE),
        ({"paused": "yes"}, api.NOT_YES_OR_NO),
    ],
)
async def test_each_setting_is_refused_in_its_own_words(
    site: Site, body: dict[str, Any], said: str
) -> None:
    answer = await site.client.put(f"{URL}/settings", json=body)
    assert answer.status_code == 400 and message(answer) == said
    assert await site.events() == []


async def test_a_budget_leaves_macos_some_memory(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "_physical_memory_gb", lambda: 32.0)
    answer = await site.client.put(f"{URL}/settings", json={"night_budget_gb": 30})
    assert answer.status_code == 400
    assert message(answer) == (
        "This Mac has 32 GB of memory. Leave some for macOS: choose 28 GB or less."
    )
    saved = await site.client.put(f"{URL}/settings", json={"night_budget_gb": 28})
    assert saved.status_code == 200 and saved.json()["settings"]["night_budget_gb"] == 28


async def test_changing_the_window_lets_waiting_work_try_again(site: Site) -> None:
    held = await site.held_back("later", NOON + timedelta(hours=9))
    # Asked for tonight by its requester: a new window does not start it now.
    async with site.factory() as db:
        tonight = await jobs.create(
            db, "test", subject="tonight", not_before=NOON + timedelta(hours=9)
        )
        await db.commit()
    answer = await site.client.put(
        f"{URL}/settings", json={"night_start": "22:00", "idle_unload_minutes": 10}
    )
    assert answer.status_code == 200, answer.text
    assert answer.json()["settings"]["night_start"] == "22:00"
    assert answer.json()["now"]["changes_at"] == "2026-11-12T22:00:00"
    states = {row.id: row.state for row in await site.job_rows()}
    assert (states[held], states[tonight.id]) == ("queued", "waiting")
    assert site.woken == [True]
    assert await site.events() == ["local_models.settings_updated"]
    async with site.factory() as db:
        audit = (await db.scalars(select(AuditLog).where(AuditLog.event.like("local%")))).one()
    assert audit.detail == {"changed": ["night_start", "idle_unload_minutes"]}


async def test_pausing_unloads_the_model_and_resuming_releases_the_waiting(
    site: Site,
) -> None:
    paused = await site.client.put(f"{URL}/settings", json={"paused": True})
    assert paused.status_code == 200, paused.text
    shown = paused.json()["settings"]
    assert shown["paused"] is True and shown["paused_by"] == ADMIN["email"]
    assert shown["paused_at"] is not None
    assert site.server.unloads == 1 and site.woken == []

    held = await site.held_back("held", NOON + timedelta(minutes=5))
    resumed = await site.client.put(f"{URL}/settings", json={"paused": False})
    assert resumed.json()["settings"]["paused_at"] is None
    assert [row.state for row in await site.job_rows() if row.id == held] == ["queued"]
    assert site.woken == [True] and site.server.unloads == 1
    assert await site.events() == ["local_models.paused", "local_models.resumed"]


async def test_blocking_a_publisher_stops_its_models_but_removes_nothing(site: Site) -> None:
    model_id = uuid.UUID(await site.installed())
    async with site.factory() as db:
        model = await registry.get_model(db, model_id)
        await registry.set_role(db, "looker", model, pending=False, user_id=None)
        await db.commit()

    answer = await site.client.put(
        f"{URL}/publishers", json={"makers": ["allenai", "nvidia", "apple"]}
    )
    assert answer.status_code == 200, answer.text
    [lost] = answer.json()["no_longer_approved"]
    assert (lost["repo_id"], lost["roles"]) == (FIRST, ["looker"])
    assert "google" in lost["reason"]
    page = await site.overview()
    [model_row] = page["models"]
    assert model_row["approved"] is False and model_row["approval_problem"] == lost["reason"]
    assert page["roles"][0]["problem"] == f"No longer approved: {lost['reason']}"
    assert page["roles"][0]["model_id"] == str(model_id)  # the role is not taken away

    back = await site.client.put(
        f"{URL}/publishers", json={"makers": ["google", "allenai", "nvidia", "apple"]}
    )
    assert back.json()["no_longer_approved"] == []
    assert (await site.overview())["models"][0]["approved"] is True
    async with site.factory() as db:
        audits = (
            await db.scalars(
                select(AuditLog).where(AuditLog.event == "local_models.publishers_updated")
            )
        ).all()
    assert [audit.detail for audit in audits] == [
        {"makers": {"added": [], "removed": ["google"]}},
        {"makers": {"added": ["google"], "removed": []}},
    ]


async def test_a_built_in_blocked_publisher_cannot_be_approved(site: Site) -> None:
    blocked = policy.DEFAULT_BLOCKED[0]
    answer = await site.client.put(f"{URL}/publishers", json={"makers": ["google", blocked]})
    assert answer.status_code == 400
    assert message(answer) == f"{blocked} is built in to the blocked list and cannot be approved."
    added = await site.client.put(f"{URL}/publishers", json={"blocked_added": ["example-lab"]})
    shown = added.json()["publishers"]["blocked"]
    assert shown[-1] == {"name": "example-lab", "built_in": False}
    assert sum(not entry["built_in"] for entry in shown) == 1


async def test_the_token_is_stored_sealed_and_never_returned(site: Site) -> None:
    for token, said in (
        ("not a token", api.TOKEN_SPACES),
        ("x" * 301, api.TOKEN_TOO_LONG),
        ("tökén", api.TOKEN_ODD),
    ):
        refused = await site.client.put(f"{URL}/token", json={"token": token})
        assert refused.status_code == 400 and message(refused) == said
    saved = await site.client.put(f"{URL}/token", json={"token": f"  {TOKEN} "})
    assert saved.status_code == 200 and saved.json() == {"hf_token_configured": True}
    page = await site.overview()
    assert page["settings"]["hf_token_configured"] is True
    assert TOKEN not in json.dumps(page)
    async with site.factory() as db:
        stored = await db.get(AppSetting, "local_models")
        assert stored is not None and TOKEN not in json.dumps(stored.value)
        audits = (await db.scalars(select(AuditLog))).all()
        assert all(TOKEN not in json.dumps(audit.detail) for audit in audits)

    removed = await site.client.put(f"{URL}/token", json={"token": ""})
    assert removed.json() == {"hf_token_configured": False}
    assert (await site.overview())["settings"]["hf_token_configured"] is False
    assert await site.events() == ["local_models.token_saved", "local_models.token_removed"]


# --- the check --------------------------------------------------------------------------------


async def test_a_check_answers_the_card_and_never_downloads(site: Site) -> None:
    answer = await site.client.post(
        f"{URL}/preflight", json={"name": f"https://huggingface.co/{FIRST}"}
    )
    assert answer.status_code == 200, answer.text
    card = answer.json()
    assert (card["repo_id"], card["revision"], card["revision_short"]) == (
        FIRST,
        FIRST_BUILD,
        FIRST_BUILD[:7],
    )
    assert card["ok"] is True and card["verdict"] == preflight.CAN_BE_ADDED
    assert [check["key"] for check in card["checks"]] == [
        "publisher",
        "built_from",
        "licence",
        "access",
        "files",
        "size",
        "memory",
        "runtime",
    ]
    assert card["runtime"] == {"name": "mlx-vlm", "label": "MLX-VLM", "installed": True}
    assert (card["access"], card["fits"], card["takes"]) == ("open", "day", ["text", "image"])
    assert card["problems"] == [] and card["variants"] == [] and card["variant"] is None
    assert card["existing"] is None
    assert site.hub.fetches == 0
    assert await site.models() == [] and await site.events() == []


async def test_a_refused_model_is_still_a_full_card(site: Site) -> None:
    answer = await site.client.post(f"{URL}/preflight", json={"name": STRANGER})
    assert answer.status_code == 200, answer.text
    card = answer.json()
    assert card["ok"] is False and card["verdict"] == card["problems"][0]
    assert "not one of your approved publishers" in card["verdict"]
    assert card["warnings"] == [] and len(card["checks"]) == 8
    assert {check["key"]: check["tone"] for check in card["checks"]}["publisher"] == "bad"


async def test_a_check_says_what_is_already_installed(site: Site) -> None:
    model_id = await site.installed()
    card = (await site.client.post(f"{URL}/preflight", json={"name": FIRST})).json()
    assert card["ok"] is False and card["verdict"] == preflight.ALREADY_INSTALLED
    assert card["existing"] == {
        "model_id": model_id,
        "revision_short": FIRST_BUILD[:7],
        "variant": "",  # an MLX repository is one build
        "same_build": True,
    }


async def test_a_name_that_cannot_be_checked_is_refused_with_the_reason(site: Site) -> None:
    async def asked(name: str) -> httpx.Response:
        return await site.client.post(f"{URL}/preflight", json={"name": name})

    bad = await asked("not a model name")
    assert bad.status_code == 400 and "the publisher, a slash" in message(bad)
    missing = await asked("google/no-such-model")
    assert missing.status_code == 404 and "no model called" in message(missing)
    site.hub.fail_info(STRANGER)
    away = await asked(STRANGER)
    assert away.status_code == 503 and "could not be reached" in message(away)
    site.hub.payloads["example-lab/old-name"] = site.hub.payloads[TEXT_ONLY]
    moved = await asked("example-lab/old-name")
    assert moved.status_code == 409 and TEXT_ONLY in message(moved)


# --- adding, resuming, cancelling ----------------------------------------------------------------


async def test_the_download_is_checked_again_at_the_revision_that_was_shown(
    site: Site,
) -> None:
    other = "1" * 40
    refused = await site.client.post(f"{URL}/models", json={"name": FIRST, "revision": other})
    assert refused.status_code == 404 and await site.models() == []
    assert ("info", FIRST, other) in site.hub.calls
    not_a_build = await site.client.post(f"{URL}/models", json={"name": FIRST, "revision": "abc"})
    assert not_a_build.status_code == 400 and message(not_a_build) == api.NOT_A_BUILD

    added = await site.add()
    assert ("info", FIRST, FIRST_BUILD) in site.hub.calls
    assert added["state"] == "downloading" and added["job"]["state"] == "queued"
    assert added["job"]["kind"] == "download" and added["job"]["can_cancel"] is True
    assert site.woken == [True] and site.hub.fetches == 0
    await site.drain()
    [model] = (await site.overview())["models"]
    assert (model["state"], model["in_library"], model["job"]) == ("installed", True, None)
    assert await site.events() == ["local_models.model_added"]
    async with site.factory() as db:
        audit = (await db.scalars(select(AuditLog).where(AuditLog.event.like("local%")))).one()
    assert audit.detail["revision"] == FIRST_BUILD and audit.detail["via"] == "converter"


async def test_a_model_that_only_fits_overnight_asks_once_before_downloading(
    site: Site,
) -> None:
    asked = await site.client.post(f"{URL}/models", json={"name": LARGE, "revision": LARGE_BUILD})
    assert asked.status_code == 428
    assert "overnight" in message(asked) and message(asked).endswith(api.DOWNLOAD_ANYWAY)
    assert await site.models() == []
    added = await site.add(LARGE, LARGE_BUILD, accept_warnings=True)
    assert added["fits"] == "night" and added["memory_gb"] == 18.8


async def test_adding_the_same_build_twice_is_refused(site: Site) -> None:
    await site.add()
    again = await site.client.post(f"{URL}/models", json={"name": FIRST, "revision": FIRST_BUILD})
    assert again.status_code == 409 and message(again) == preflight.ALREADY_INSTALLED
    assert len(await site.models()) == 1


async def test_a_queue_that_is_down_leaves_nothing_waiting(site: Site) -> None:
    site.queue_up = False
    answer = await site.client.post(f"{URL}/models", json={"name": FIRST, "revision": FIRST_BUILD})
    assert answer.status_code == 503 and message(answer) == api.QUEUE_DOWN
    assert await site.models() == [] and await site.job_rows() == []
    assert await site.events() == []


async def test_a_cancelled_download_says_where_resume_carries_on_and_finishes(
    site: Site,
) -> None:
    added = await site.add()
    cancelled = await site.client.post(f"{URL}/jobs/{added['job']['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    [model] = (await site.overview())["models"]
    assert model["state"] == "incomplete"
    assert model["error"] == "Cancelled. Resume carries on from 0.0 GB."

    resumed = await site.client.post(f"{URL}/models/{added['id']}/download")
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["state"] == "downloading" and resumed.json()["job"] is not None
    twice = await site.client.post(f"{URL}/models/{added['id']}/download")
    assert twice.status_code == 409 and message(twice) == api.ALREADY_DOWNLOADING
    await site.drain()
    assert (await site.overview())["models"][0]["state"] == "installed"
    done = await site.client.post(f"{URL}/models/{added['id']}/download")
    assert done.status_code == 409 and message(done) == api.ALREADY_HERE

    finished = await site.client.post(f"{URL}/jobs/{added['job']['id']}/cancel")
    assert finished.status_code == 409 and message(finished) == jobs.FINISHED_ALREADY
    unknown = await site.client.post(f"{URL}/jobs/{uuid.uuid4()}/cancel")
    assert unknown.status_code == 404 and message(unknown) == api.NO_SUCH_JOB
    assert await site.events() == [
        "local_models.model_added",
        "local_models.job_cancelled",
        "local_models.download_resumed",
    ]


async def test_a_running_job_is_asked_to_stop_and_says_so(site: Site) -> None:
    added = await site.add()
    async with site.factory() as db:
        job = await db.get(LocalAiJob, uuid.UUID(added["job"]["id"]))
        assert job is not None
        job.state = "running"
        job.started_at = job.beat_at = NOON.astimezone(UTC)
        await db.commit()
    asked = await site.client.post(f"{URL}/jobs/{added['job']['id']}/cancel")
    assert asked.status_code == 200
    assert (asked.json()["state"], asked.json()["cancelling"], asked.json()["can_cancel"]) == (
        "running",
        True,
        False,
    )


async def test_a_model_that_is_gone_from_the_library_can_be_downloaded_again(
    site: Site,
) -> None:
    model_id = await site.installed()
    folder = site.tmp / "models" / "library" / "mlx-community"
    for file in folder.rglob("manifest.json"):
        file.unlink()
    [model] = (await site.overview())["models"]
    assert model["in_library"] is False
    again = await site.client.post(f"{URL}/models/{model_id}/download")
    assert again.status_code == 202, again.text
    await site.drain()
    assert (await site.overview())["models"][0]["in_library"] is True


# --- removing --------------------------------------------------------------------------------


async def test_removing_a_model_frees_its_files_and_keeps_its_jobs_as_history(
    site: Site,
) -> None:
    model_id = await site.installed()
    folder = site.tmp / "models" / "library" / "mlx-community"
    assert any(folder.rglob("*.safetensors"))
    answer = await site.client.delete(f"{URL}/models/{model_id}")
    assert answer.status_code == 200, answer.text
    assert answer.json()["freed_bytes"] > 0
    assert not any(path.is_file() for path in folder.parent.rglob("*"))
    assert await site.models() == []
    [download] = await site.job_rows()
    assert download.model_id is None and download.state == "done"
    gone = await site.client.delete(f"{URL}/models/{model_id}")
    assert gone.status_code == 404
    assert await site.events() == ["local_models.model_added", "local_models.model_removed"]


async def test_a_model_with_a_role_or_a_download_under_way_is_not_removed(site: Site) -> None:
    added = await site.add()
    downloading = await site.client.delete(f"{URL}/models/{added['id']}")
    assert downloading.status_code == 409 and message(downloading) == api.BEING_DOWNLOADED
    await site.drain()
    async with site.factory() as db:
        model = await registry.get_model(db, uuid.UUID(added["id"]))
        await registry.set_role(db, "looker", model, pending=False, user_id=None)
        await db.commit()
    held = await site.client.delete(f"{URL}/models/{added['id']}")
    assert held.status_code == 409
    assert message(held) == (
        "It is the Looker. Give that role to another model, or take it away, first."
    )
    assert len(await site.models()) == 1


async def test_a_library_that_is_not_connected_stops_adding_and_removing(site: Site) -> None:
    site.library_on_the_nas()
    model_id = await site.installed()
    never = await site.add(TEXT_ONLY, TEXT_ONLY_BUILD, accept_warnings=True)
    await site.client.post(f"{URL}/jobs/{never['job']['id']}/cancel")
    site.nas_drops()

    reader = await site.reader()
    try:
        shown = (await site.overview(reader))["library"]
    finally:
        await reader.aclose()
    assert shown["connected"] is False and shown["where"] == "bulk"
    assert shown["copies_to_this_machine"] is True
    assert shown["problem"] is not None and str(site.nas) not in shown["problem"]
    models = {model["id"]: model for model in (await site.overview())["models"]}
    assert models[model_id]["in_library"] is None

    removing = await site.client.delete(f"{URL}/models/{model_id}")
    assert removing.status_code == 503 and message(removing) == api.CANNOT_REMOVE
    adding = await site.client.post(f"{URL}/models", json={"name": FIRST, "revision": FIRST_BUILD})
    assert adding.status_code == 503
    resuming = await site.client.post(f"{URL}/models/{never['id']}/download")
    assert resuming.status_code == 503
    # A model none of whose files ever reached the library goes regardless.
    dropped = await site.client.delete(f"{URL}/models/{never['id']}")
    assert dropped.status_code == 200, dropped.text
    assert [model.repo_id for model in await site.models()] == [FIRST]


# --- everything that changes something is written down -------------------------------------


async def test_every_change_leaves_an_audit_row(site: Site) -> None:
    client = site.client
    await client.put(f"{URL}/settings", json={"day_budget_gb": 6})
    await client.put(f"{URL}/settings", json={"paused": True})
    await client.put(f"{URL}/settings", json={"paused": False})
    await client.put(f"{URL}/publishers", json={"converters": ["mlx-community", "example-conv"]})
    await client.put(f"{URL}/token", json={"token": TOKEN})
    await client.put(f"{URL}/token", json={"token": ""})
    added = await site.add()
    await client.post(f"{URL}/jobs/{added['job']['id']}/cancel")
    await client.post(f"{URL}/models/{added['id']}/download")
    await site.drain()
    await client.delete(f"{URL}/models/{added['id']}")
    # Looking and checking change nothing and write nothing.
    await client.get(URL)
    await client.post(f"{URL}/preflight", json={"name": FIRST})
    assert await site.events() == [
        "local_models.settings_updated",
        "local_models.paused",
        "local_models.resumed",
        "local_models.publishers_updated",
        "local_models.token_saved",
        "local_models.token_removed",
        "local_models.model_added",
        "local_models.job_cancelled",
        "local_models.download_resumed",
        "local_models.model_removed",
    ]


# --- the Processing page ----------------------------------------------------------------------


async def test_the_processing_page_counts_local_ai_jobs_only_where_models_can_run(
    site: Site,
) -> None:
    await site.add()
    depths = (await site.client.get("/api/v1/system/processing")).json()["queue_depths"]
    assert depths["local_ai"] == 1
    site.unconfigured()
    depths = (await site.client.get("/api/v1/system/processing")).json()["queue_depths"]
    assert "local_ai" not in depths


# --- part two: who does what, Test, the software, the model in memory (6.9–6.13) -------------


def local_copies(site: Site) -> list[Path]:
    """The manifests of the copies kept on this machine for roles."""
    return sorted((site.tmp / "models" / "local-ai").rglob("manifest.json"))


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


async def test_the_whole_click_path_from_check_to_removal(site: Site) -> None:
    site.library_on_the_nas()
    card = (await site.client.post(f"{URL}/preflight", json={"name": FIRST})).json()
    assert card["ok"] is True
    added = await site.add(FIRST, card["revision"])
    await site.drain()
    model_id = added["id"]

    # The library is on the NAS, so the Looker's model is copied here first.
    given = await site.client.put(f"{URL}/roles/looker", json={"model_id": model_id})
    assert given.status_code == 202, given.text
    looker = given.json()
    assert (looker["model_id"], looker["pending_model_id"]) == (None, model_id)
    assert looker["pending_model_name"] == "gemma-4-e4b-it-4bit"
    assert looker["job"]["kind"] == "place" and looker["job"]["state"] == "queued"
    assert looker["job"]["title"] == "Copying gemma-4-e4b-it-4bit to this machine for the Looker"
    await site.drain()
    looker = (await site.overview())["roles"][0]
    assert (looker["model_id"], looker["pending_model_id"], looker["ready"]) == (
        model_id,
        None,
        True,
    )
    assert len(local_copies(site)) == 1

    tested = await site.client.post(f"{URL}/models/{model_id}/test", json={"asset_id": None})
    assert tested.status_code == 202, tested.text
    assert tested.json()["state"] == "queued" and tested.json()["kind"] is None
    await site.drain()
    test = (await site.overview())["models"][0]["test"]
    assert (test["state"], test["kind"], test["structured_ok"]) == ("done", "picture", True)
    assert test["sample"] == {
        "asset_id": None,
        "media_type": "image",
        "filename": None,
        "label": handlers.DRAWN,
    }
    assert set(test["answer"]) == {"caption", "tags"} and test["raw"]
    assert test["model"] == {"repo_id": FIRST, "revision_short": FIRST_BUILD[:7]}
    assert (test["runtime"], test["runtime_version"], test["prompt_version"]) == (
        "mlx-vlm",
        VERSION,
        handlers.TEST_PROMPT_VERSION,
    )
    assert test["ran_from"] == "this_machine" and test["seconds"] is not None
    # What the model was shown can be looked at.
    drawn = await site.client.get(f"{URL}/sample.jpg")
    assert drawn.status_code == 200 and drawn.headers["content-type"] == "image/jpeg"
    assert "max-age=86400" in drawn.headers["cache-control"]

    held = await site.client.delete(f"{URL}/models/{model_id}")
    assert held.status_code == 409 and message(held).startswith("It is the Looker.")
    cleared = await site.client.put(f"{URL}/roles/looker", json={"model_id": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["model_id"] is None and local_copies(site) == []
    removed = await site.client.delete(f"{URL}/models/{model_id}")
    assert removed.status_code == 200, removed.text
    assert await site.events() == [
        "local_models.model_added",
        "local_models.role_assigned",
        "local_models.model_tested",
        "local_models.role_cleared",
        "local_models.model_removed",
    ]


async def test_a_role_is_given_at_once_when_the_library_is_on_this_machine(
    site: Site,
) -> None:
    model_id = await site.installed()
    given = await site.client.put(f"{URL}/roles/editor", json={"model_id": model_id})
    assert given.status_code == 200, given.text
    editor = given.json()
    assert (editor["model_id"], editor["pending_model_id"], editor["job"]) == (
        model_id,
        None,
        None,
    )
    assert editor["ready"] is True and editor["assigned_at"] is not None
    assert [row.kind for row in await site.job_rows()] == ["download"]
    # Asking again changes nothing and writes nothing.
    again = await site.client.put(f"{URL}/roles/editor", json={"model_id": model_id})
    assert again.status_code == 200
    async with site.factory() as db:
        audit = (
            await db.scalars(select(AuditLog).where(AuditLog.event == "local_models.role_assigned"))
        ).one()
    assert audit.detail == {
        "role": "editor",
        "from": None,
        "to": f"{FIRST}@{FIRST_BUILD[:8]}",
        "copy": False,
        "compare": "not_needed",
    }


async def test_a_role_is_refused_in_words_when_its_model_cannot_hold_it(site: Site) -> None:
    text_only = await site.installed(TEXT_ONLY, TEXT_ONLY_BUILD)
    downloading = await site.add()

    async def give(role: str, model_id: Any) -> httpx.Response:
        return await site.client.put(f"{URL}/roles/{role}", json={"model_id": model_id})

    no_pictures = await give("looker", text_only)
    assert no_pictures.status_code == 409 and message(no_pictures) == "It does not read pictures."
    still = await give("editor", downloading["id"])
    assert still.status_code == 409 and message(still) == registry.STILL_DOWNLOADING
    unknown = await give("narrator", text_only)
    assert unknown.status_code == 404 and message(unknown) == api.NO_SUCH_ROLE
    for nobody in (str(uuid.uuid4()), "not-an-id"):
        missing = await give("editor", nobody)
        assert missing.status_code == 404 and message(missing) == api.NO_SUCH_MODEL
    assert [e for e in await site.events() if "role" in e] == []


async def test_the_old_model_keeps_the_role_until_the_new_ones_copy_is_whole(
    site: Site,
) -> None:
    site.library_on_the_nas()
    first = await site.installed()
    second = await site.installed(TEXT_ONLY, TEXT_ONLY_BUILD)
    await site.client.put(f"{URL}/roles/editor", json={"model_id": first})
    await site.drain()
    assert len(local_copies(site)) == 1

    moving = await site.client.put(f"{URL}/roles/editor", json={"model_id": second})
    assert moving.status_code == 202, moving.text
    editor = moving.json()
    assert (editor["model_id"], editor["pending_model_id"]) == (first, second)
    assert editor["ready"] is True  # the old model still does the work
    taken = await site.client.put(f"{URL}/roles/editor", json={"model_id": first})
    assert taken.status_code == 409 and message(taken) == api.ROLE_TAKEN
    twice = await site.client.put(f"{URL}/roles/editor", json={"model_id": second})
    assert twice.status_code == 409 and message(twice) == api.ALREADY_COPYING

    await site.drain()
    editor = (await site.overview())["roles"][1]
    assert (editor["model_id"], editor["pending_model_id"]) == (second, None)
    # The first model holds no role now: its copy on this machine is gone.
    [copy] = local_copies(site)
    assert TEXT_ONLY.rpartition("/")[2] in str(copy)


async def test_a_role_given_to_a_model_already_here_frees_the_old_holders_copy(
    site: Site,
) -> None:
    site.library_on_the_nas()
    first = await site.installed()
    second = await site.installed(TEXT_ONLY, TEXT_ONLY_BUILD)
    await site.client.put(f"{URL}/roles/looker", json={"model_id": first})
    await site.client.put(f"{URL}/roles/editor", json={"model_id": second})
    await site.drain()
    assert len(local_copies(site)) == 2

    # The first model is on this machine already (it is the Looker): no copy, no wait.
    back = await site.client.put(f"{URL}/roles/editor", json={"model_id": first})
    assert back.status_code == 200, back.text
    assert (back.json()["model_id"], back.json()["job"]) == (first, None)
    [copy] = local_copies(site)
    assert FIRST.rpartition("/")[2] in str(copy)


async def test_a_missing_copy_is_made_again_and_taking_the_role_away_frees_it(
    site: Site,
) -> None:
    site.library_on_the_nas()
    model_id = await site.installed()
    await site.client.put(f"{URL}/roles/looker", json={"model_id": model_id})
    await site.drain()
    [copy] = local_copies(site)
    copy.unlink()
    looker = (await site.overview())["roles"][0]
    assert (looker["ready"], looker["problem"]) == (False, api.NOT_ON_THIS_MAC)

    # Copy again: the same model, which keeps the role while it is copied.
    again = await site.client.put(f"{URL}/roles/looker", json={"model_id": model_id})
    assert again.status_code == 202, again.text
    assert (again.json()["model_id"], again.json()["pending_model_id"]) == (model_id, model_id)
    await site.drain()
    assert (await site.overview())["roles"][0]["ready"] is True

    unloads = site.server.unloads
    away = await site.client.put(f"{URL}/roles/looker", json={"model_id": None})
    assert away.status_code == 200, away.text
    assert (away.json()["model_id"], away.json()["problem"]) == (None, None)
    assert local_copies(site) == [] and site.server.unloads == unloads + 1
    # The model stays in the library.
    [model] = (await site.overview())["models"]
    assert model["in_library"] is True


async def test_taking_a_role_away_cancels_a_copy_that_has_not_started(site: Site) -> None:
    site.library_on_the_nas()
    model_id = await site.installed()
    given = await site.client.put(f"{URL}/roles/looker", json={"model_id": model_id})
    job_id = given.json()["job"]["id"]
    away = await site.client.put(f"{URL}/roles/looker", json={"model_id": None})
    assert away.status_code == 200
    assert (away.json()["pending_model_id"], away.json()["job"]) == (None, None)
    [copy] = [row for row in await site.job_rows() if row.kind == "place"]
    assert (str(copy.id), copy.state) == (job_id, "cancelled")
    assert local_copies(site) == []


async def test_a_queue_that_is_down_leaves_no_role_waiting(site: Site) -> None:
    site.library_on_the_nas()
    model_id = await site.installed()
    site.queue_up = False
    answer = await site.client.put(f"{URL}/roles/looker", json={"model_id": model_id})
    assert answer.status_code == 503 and message(answer) == api.QUEUE_DOWN
    looker = (await site.overview())["roles"][0]
    assert (looker["model_id"], looker["pending_model_id"]) == (None, None)
    assert [row.kind for row in await site.job_rows()] == ["download"]
    assert await site.events() == ["local_models.model_added"]


async def test_a_role_that_needs_a_copy_waits_for_a_library_that_is_not_connected(
    site: Site,
) -> None:
    site.library_on_the_nas()
    model_id = await site.installed()
    site.nas_drops()
    answer = await site.client.put(f"{URL}/roles/looker", json={"model_id": model_id})
    assert answer.status_code == 503 and "model library" in message(answer)
    assert str(site.nas) not in message(answer)
    assert [row.kind for row in await site.job_rows()] == ["download"]


async def test_a_role_given_while_the_library_moves_is_copied_once_the_move_ends(
    site: Site,
) -> None:
    model_id = await site.installed()
    data = get_settings().data_dir
    stores.pause(data, library.KIND)  # a move of the library, in its last steps
    stores._CACHE.clear()
    given = await site.client.put(f"{URL}/roles/looker", json={"model_id": model_id})
    assert given.status_code == 202, given.text
    looker = given.json()
    # The role is the model's at once, and its copy is lined up behind the move.
    assert (looker["model_id"], looker["pending_model_id"]) == (model_id, model_id)
    assert looker["job"]["kind"] == "place"
    stores.resume(data, library.KIND)
    stores._CACHE.clear()
    await site.drain()
    looker = (await site.overview())["roles"][0]
    assert (looker["model_id"], looker["pending_model_id"], looker["ready"]) == (
        model_id,
        None,
        True,
    )
    [copy] = [row for row in await site.job_rows() if row.kind == "place"]
    assert copy.state == "done" and copy.result == {"role": "looker", "given": True}


async def test_a_test_of_a_model_that_only_fits_overnight_waits_and_says_until_when(
    site: Site,
) -> None:
    model_id = await site.installed(TEXT_ONLY, TEXT_ONLY_BUILD)
    await site.needs(model_id, 10.1)  # its real size (the download here is shrunk)
    asked = await site.client.post(f"{URL}/models/{model_id}/test")
    assert asked.status_code == 202, asked.text
    await site.drain()
    [model] = (await site.overview())["models"]
    test = model["test"]
    assert test["state"] == "waiting" and "21:30" in test["message"]
    assert utc(test["not_before"]) == datetime(2026, 11, 12, 21, 30).astimezone(UTC)
    assert site.server.requests == []  # nothing was loaded by day

    cancelled = await site.client.post(f"{URL}/jobs/{test['job_id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"

    # Overnight, the text-only model is asked its question in words.
    site.clock.at = datetime(2026, 11, 12, 22, 0)
    again = await site.client.post(f"{URL}/models/{model_id}/test", json={})
    assert again.status_code == 202, again.text
    await site.drain()
    test = (await site.overview())["models"][0]["test"]
    assert (test["state"], test["kind"], test["sample"]["label"]) == (
        "done",
        "text",
        handlers.WORDS,
    )
    assert len(test["answer"]["order"]) == 3


async def test_a_test_is_refused_in_words_when_it_could_not_run(site: Site) -> None:
    async def test(model_id: Any, body: Any = None) -> httpx.Response:
        return await site.client.post(f"{URL}/models/{model_id}/test", json=body)

    unknown = await test(uuid.uuid4())
    assert unknown.status_code == 404
    added = await site.add()
    model_id = added["id"]
    downloading = await test(model_id)
    assert downloading.status_code == 409 and message(downloading) == registry.STILL_DOWNLOADING
    await site.client.post(f"{URL}/jobs/{added['job']['id']}/cancel")
    incomplete = await test(model_id)
    assert incomplete.status_code == 409 and message(incomplete) == registry.NOT_COMPLETE
    await site.client.post(f"{URL}/models/{model_id}/download")
    await site.drain()

    (site.runtimes / "mlx-vlm" / "state.json").unlink()
    no_software = await test(model_id)
    assert no_software.status_code == 409
    assert message(no_software) == "Install MLX-VLM first (Software that runs models, below)."
    site.install_mlx_vlm()

    await site.client.put(f"{URL}/publishers", json={"makers": ["allenai", "nvidia", "apple"]})
    refused = await test(model_id)
    assert refused.status_code == 409
    assert message(refused).startswith("It is no longer approved: ")
    await site.client.put(
        f"{URL}/publishers", json={"makers": ["google", "allenai", "nvidia", "apple"]}
    )

    await site.needs(model_id, 5.8)  # its real size (the download here is shrunk)
    await site.client.put(f"{URL}/settings", json={"day_budget_gb": 4, "night_budget_gb": 5})
    too_big = await test(model_id)
    assert too_big.status_code == 409 and "more than the overnight budget" in message(too_big)
    await site.client.put(f"{URL}/settings", json={"night_budget_gb": 20, "day_budget_gb": 8})

    for picture in (str(uuid.uuid4()), "not-an-id"):
        no_picture = await test(model_id, {"asset_id": picture})
        assert no_picture.status_code == 404 and message(no_picture) == handlers.NO_SUCH_PICTURE

    site.queue_up = False
    down = await test(model_id)
    assert down.status_code == 503 and message(down) == api.QUEUE_DOWN
    assert [row for row in await site.job_rows() if row.kind == "test"] == []
    site.queue_up = True

    first = await test(model_id)
    assert first.status_code == 202
    second = await test(model_id)
    assert second.status_code == 409 and message(second) == api.TEST_WAITING
    assert (await site.events()).count("local_models.model_tested") == 1


async def test_a_test_can_be_asked_about_a_chosen_picture(site: Site) -> None:
    from PIL import Image

    relative = "derivatives/ab/photo/preview.jpg"
    path = site.tmp / "data" / relative
    path.parent.mkdir(parents=True)
    out = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 10, 10)).save(out, "JPEG")
    path.write_bytes(out.getvalue())
    async with site.factory() as db:
        shelf = Library(name="GELCO", root_path=str(site.tmp / "media"))
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
        photo_id = str(photo.id)
    model_id = await site.installed()
    asked = await site.client.post(f"{URL}/models/{model_id}/test", json={"asset_id": photo_id})
    assert asked.status_code == 202, asked.text
    await site.drain()
    test = (await site.overview())["models"][0]["test"]
    assert test["state"] == "done", test["message"]
    assert test["sample"] == {
        "asset_id": photo_id,
        "media_type": "image",
        "filename": "DJI_0639.JPG",
        "label": handlers.CHOSEN,
    }
    async with site.factory() as db:
        audit = (
            await db.scalars(select(AuditLog).where(AuditLog.event == "local_models.model_tested"))
        ).one()
    assert audit.detail["asset_id"] == photo_id


async def test_a_version_whose_program_has_gone_is_installed_afresh_as_doctor_says(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor's remedy for a version in use that lost its program, followed on the page."""
    from framefound.native import doctor

    monkeypatch.setattr(handlers, "_which", lambda _name: "/opt/homebrew/bin/uv")
    site.run = installer()
    folder = runtimes.version_dir(site.runtimes, "mlx-vlm", VERSION)
    program = MlxVlm().program(folder)
    assert program is not None
    program.unlink()  # the venv's interpreter, deleted by hand
    said = doctor._runtime_line(site.runtimes, MlxVlm(), "Darwin", "arm64")
    assert said is not None and not said.ok
    assert said.detail.endswith(
        f"Software that runs models › Install a particular version › {VERSION} installs it afresh"
    )

    again = await site.client.post(f"{URL}/runtimes/mlx-vlm/install", json={"version": VERSION})
    assert again.status_code == 202, again.text
    await site.drain()

    said = doctor._runtime_line(site.runtimes, MlxVlm(), "Darwin", "arm64")
    assert said is not None and said.ok and said.detail.startswith(VERSION), said
    assert MlxVlm().program(folder) is not None
    [runtime] = (await site.overview())["runtimes"]
    assert (runtime["installed"], runtime["problem"]) == (VERSION, None)
    # A version in use that is whole is still not installed twice.
    same = await site.client.post(f"{URL}/runtimes/mlx-vlm/install", json={"version": VERSION})
    assert same.status_code == 409 and message(same) == f"{VERSION} is already the version in use."


async def test_the_software_is_installed_updated_and_rolled_back(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "_which", lambda _name: "/opt/homebrew/bin/uv")
    site.run = installer()
    nothing_kept = await site.client.post(f"{URL}/runtimes/mlx-vlm/rollback")
    assert nothing_kept.status_code == 409
    assert message(nothing_kept) == runtimes.NOTHING_TO_GO_BACK_TO

    checked = await site.client.post(f"{URL}/runtimes/mlx-vlm/check")
    assert checked.status_code == 200, checked.text
    shown = checked.json()
    assert (shown["installed"], shown["latest"], shown["update_available"]) == (
        VERSION,
        "0.7.13",
        True,
    )
    assert shown["latest_problem"] is None and shown["latest_checked_at"] is not None

    update = await site.client.post(f"{URL}/runtimes/mlx-vlm/install", json={"version": None})
    assert update.status_code == 202, update.text
    job = update.json()["job"]
    assert (job["kind"], job["subject"], job["title"]) == (
        "runtime_install",
        "MLX-VLM 0.7.13",
        "Installing MLX-VLM 0.7.13",
    )
    assert update.json()["installed"] == VERSION  # nothing is switched until it is whole
    twice = await site.client.post(f"{URL}/runtimes/mlx-vlm/install", json={"version": "0.7.14"})
    assert twice.status_code == 409 and message(twice) == "MLX-VLM is already being installed."
    meanwhile = await site.client.post(f"{URL}/runtimes/mlx-vlm/rollback")
    assert meanwhile.status_code == 409 and "is being installed" in message(meanwhile)

    await site.drain()
    [runtime] = (await site.overview())["runtimes"]
    assert (runtime["installed"], runtime["previous"], runtime["update_available"]) == (
        "0.7.13",
        VERSION,
        False,
    )
    same = await site.client.post(f"{URL}/runtimes/mlx-vlm/install", json={"version": "0.7.13"})
    assert same.status_code == 409 and message(same) == "0.7.13 is already the version in use."
    nonsense = await site.client.post(
        f"{URL}/runtimes/mlx-vlm/install", json={"version": "the newest please"}
    )
    assert nonsense.status_code == 400 and message(nonsense) == "That is not a version number."

    unloads = site.server.unloads
    back = await site.client.post(f"{URL}/runtimes/mlx-vlm/rollback")
    assert back.status_code == 200, back.text
    assert (back.json()["installed"], back.json()["previous"]) == (VERSION, "0.7.13")
    assert site.server.unloads == unloads + 1
    async with site.factory() as db:
        audits = (
            await db.scalars(select(AuditLog).where(AuditLog.event.like("local_models.runtime%")))
        ).all()
    assert [(audit.event, audit.detail) for audit in audits] == [
        ("local_models.runtime_install_started", {"name": "mlx-vlm", "version": "0.7.13"}),
        ("local_models.runtime_rolled_back", {"name": "mlx-vlm", "from": "0.7.13", "to": VERSION}),
    ]


async def test_the_software_is_refused_in_words_when_it_cannot_be_installed(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    for call in ("check", "install", "rollback"):
        unknown = await site.client.post(f"{URL}/runtimes/llama-cpp/{call}")
        assert unknown.status_code == 404 and message(unknown) == api.NO_SUCH_RUNTIME

    def index_down(_url: str, **_more: Any) -> Any:
        raise HubError("unreachable", "The address could not be reached.")

    site.index = index_down
    checked = await site.client.post(f"{URL}/runtimes/mlx-vlm/check")
    assert checked.status_code == 503 and message(checked) == api.INDEX_UNREACHABLE
    [runtime] = (await site.overview())["runtimes"]
    assert (runtime["latest"], runtime["latest_problem"]) == (None, api.INDEX_UNREACHABLE)
    newest = await site.client.post(f"{URL}/runtimes/mlx-vlm/install", json={"version": None})
    assert newest.status_code == 503 and message(newest) == api.INDEX_UNREACHABLE

    monkeypatch.setattr(handlers, "_which", lambda _name: None)
    no_uv = await site.client.post(f"{URL}/runtimes/mlx-vlm/install", json={"version": "0.7.13"})
    assert no_uv.status_code == 409 and message(no_uv) == handlers.NO_UV
    monkeypatch.setattr(handlers, "_which", lambda _name: "/opt/homebrew/bin/uv")

    site.queue_up = False
    down = await site.client.post(f"{URL}/runtimes/mlx-vlm/install", json={"version": "0.7.13"})
    assert down.status_code == 503 and message(down) == api.QUEUE_DOWN
    assert await site.job_rows() == []
    site.queue_up = True

    site.machine = "x86_64"
    intel = await site.client.post(f"{URL}/runtimes/mlx-vlm/install", json={"version": "0.7.13"})
    assert intel.status_code == 409
    assert message(intel) == "MLX models need a Mac with Apple silicon."
    assert await site.events() == []


async def test_unload_now_gives_the_memory_back(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_id = await site.installed()
    async with site.factory() as db:
        model = await registry.get_model(db, uuid.UUID(model_id))
        assert model is not None
        folder = library.library_dir(registry.snapshot(model))
    site.server.chat(
        {"model": str(folder), "messages": [{"role": "user", "content": "ok"}]},
        idle_s=300,
        timeout_s=300,
    )
    first = await site.client.post(f"{URL}/server/unload")
    assert first.status_code == 200 and first.json() == {"unloaded": True}
    assert (await site.overview())["server"]["state"] == "idle"
    nothing = await site.client.post(f"{URL}/server/unload")
    assert nothing.json() == {"unloaded": False}

    site.server.down = True
    down = await site.client.post(f"{URL}/server/unload")
    assert down.status_code == 503 and message(down) == api.SERVER_NOT_ANSWERING
    site.server.down = False

    # A model in the middle of an answer is let go of once it has finished.
    finished = threading.Event()

    def answering() -> bool:
        finished.wait(5)
        return True

    monkeypatch.setattr(site.server, "unload", answering)
    monkeypatch.setattr(api, "REMOVE_UNLOAD_WAIT_S", 0.05)
    busy = await site.client.post(f"{URL}/server/unload")
    finished.set()
    assert busy.status_code == 409 and message(busy) == api.SERVER_BUSY
    assert await site.events() == [
        "local_models.model_added",
        "local_models.unloaded",
        "local_models.unloaded",
    ]


# --- what every request shares ---------------------------------------------------------


def test_the_api_knows_every_kind_the_lane_registers() -> None:
    """A later phase registers its kinds at the foot of processing/local_ai_tasks
    (13.2). The API imports that module at import, so it knows them from its
    first request — before anything has woken the lane."""
    assert api.local_ai_tasks is local_ai_tasks
    assert {"download", "place", "runtime_install", "test"} <= set(jobs.registered())


async def test_the_model_server_is_asked_once_whichever_request_asks(site: Site) -> None:
    """Each request is handed a `ModelServer` of its own, as on a real install;
    there is still one model server, remembered for two seconds and never asked
    a second question while one is unanswered."""
    asked: list[str] = []
    unloads: list[str] = []
    answered = threading.Event()

    class Fresh(fake.FakeModelServer):
        def status(self) -> dict[str, Any] | None:
            asked.append("status")
            return super().status()

        def unload(self) -> bool:
            unloads.append("unload")
            answered.wait(5)
            return True

    def fresh_deps() -> Deps:
        return replace(site.deps(), server=Fresh(sleep=lambda _seconds: None))

    site.app.dependency_overrides[api.get_deps] = fresh_deps
    for _ in range(3):
        answer = await site.client.get(URL)
        assert answer.status_code == 200, answer.text
    assert asked == ["status"]

    # Two presses of Unload while the first is still being answered: one unload.
    asyncio.get_running_loop().call_later(0.3, answered.set)
    first, second = await asyncio.gather(
        site.client.post(f"{URL}/server/unload"), site.client.post(f"{URL}/server/unload")
    )
    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json() == second.json() == {"unloaded": True}
    assert unloads == ["unload"]


async def test_two_pages_looking_at_a_slow_library_both_see_it_connected(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library on a share takes a moment to look at. The second page to ask
    meanwhile waits for the same answer; it is not told the share went away."""
    await site.installed()
    looked: list[int] = []
    real = api._look

    def slow(settings: Any, models: Any) -> Any:
        looked.append(len(models))
        time.sleep(0.5)
        return real(settings, models)

    monkeypatch.setattr(api, "_look", slow)
    reader = await site.reader()
    try:
        api._STATUS.clear()
        pages = await asyncio.gather(site.client.get(URL), reader.get(URL))
    finally:
        await reader.aclose()
    for page in pages:
        assert page.status_code == 200, page.text
        body = page.json()
        assert body["library"]["connected"] is True and body["library"]["problem"] is None
        assert body["models"][0]["in_library"] is True
    assert looked == [1]


async def test_a_reader_never_reads_a_server_path_in_a_jobs_message(site: Site) -> None:
    """The runtime's damaged record names its file. A Test that met it says so
    in words to everyone, and the file only under an administrator's details."""
    model_id = await site.installed()
    asked = await site.client.post(f"{URL}/models/{model_id}/test")
    assert asked.status_code == 202, asked.text
    state = site.runtimes / "mlx-vlm" / "state.json"
    state.write_text("{not json", encoding="utf-8")
    await site.drain()
    reader = await site.reader()
    try:
        page = await site.overview(reader)
    finally:
        await reader.aclose()
    [tested] = [job for job in page["jobs"] if job["kind"] == "test"]
    assert (tested["state"], tested["message"], tested["detail"]) == (
        "failed",
        RECORD_DAMAGED,
        None,
    )
    assert page["models"][0]["test"]["message"] == RECORD_DAMAGED
    assert json.dumps(str(site.tmp))[1:-1] not in json.dumps(page)
    [mine] = [job for job in (await site.overview())["jobs"] if job["kind"] == "test"]
    assert mine["message"] == RECORD_DAMAGED and str(state) in mine["detail"]


async def test_a_copy_a_test_is_reading_from_is_kept_when_the_role_moves(site: Site) -> None:
    site.library_on_the_nas()
    first = await site.installed()
    second = await site.installed(TEXT_ONLY, TEXT_ONLY_BUILD)
    await site.client.put(f"{URL}/roles/looker", json={"model_id": first})
    await site.client.put(f"{URL}/roles/editor", json={"model_id": second})
    await site.drain()
    assert len(local_copies(site)) == 2
    async with site.factory() as db:
        job = await jobs.create(db, "test", subject="reading", model_id=uuid.UUID(second))
        job.state = "running"
        job.started_at = job.beat_at = NOON.astimezone(UTC)
        await db.commit()

    # The editor goes to the first model; the second's copy is being read.
    back = await site.client.put(f"{URL}/roles/editor", json={"model_id": first})
    assert back.status_code == 200, back.text
    assert len(local_copies(site)) == 2


async def stale_download(site: Site, job_id: str) -> None:
    """The download's worker died: its row still says running, its beat is old."""
    beat = NOON.astimezone(UTC) - timedelta(seconds=jobs.STALE_AFTER_S + 5)
    async with site.factory() as db:
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == uuid.UUID(job_id))
            .values(state="running", started_at=beat, beat_at=beat)
        )
        await db.commit()


async def test_a_download_whose_worker_died_can_be_resumed_or_removed(site: Site) -> None:
    """The page already says "start it again"; Resume and Remove agree with it,
    without waiting for the lane's next tick."""
    added = await site.add()
    await stale_download(site, added["job"]["id"])
    [model] = (await site.overview())["models"]
    assert model["job"]["state"] == "failed" and model["job"]["can_cancel"] is False

    again = await site.client.post(f"{URL}/models/{added['id']}/download")
    assert again.status_code == 202, again.text
    async with site.factory() as db:
        dead = await db.get(LocalAiJob, uuid.UUID(added["job"]["id"]))
        assert dead is not None and (dead.state, dead.message) == ("failed", jobs.STOPPED)
    await site.drain()
    assert (await site.overview())["models"][0]["state"] == registry.INSTALLED

    other = await site.add(TEXT_ONLY, TEXT_ONLY_BUILD, accept_warnings=True)
    await stale_download(site, other["job"]["id"])
    removed = await site.client.delete(f"{URL}/models/{other['id']}")
    assert removed.status_code == 200, removed.text
    assert [model["id"] for model in (await site.overview())["models"]] == [added["id"]]


# --- GGUF builds and llama.cpp (L6 6.1–6.3, 6.5) -------------------------------------

OMNI = "example-gguf/demo-omni-30b-GGUF"
OMNI_BUILD = "49a5b1ae335e30e775d4eb03f822b52a63ea4271"
VISION = "google/demo-vision-gguf"
LLAMA = "0.9.0"


def with_llama_cpp(site: Site, *, installed: bool = True) -> None:
    """The Mac as it is with both programs: llama.cpp 0.9.0 installed beside MLX-VLM."""
    site.kinds = {"mlx-vlm": MlxVlm(), "llama-cpp": LlamaCpp()}
    if installed:
        runtimes.install(LlamaCpp(), LLAMA, site.runtimes, run=fake.FakeRunner(make_llama=True))
        runtimes.activate(site.runtimes, "llama-cpp", LLAMA)


async def approve_gguf_converter(site: Site) -> None:
    answer = await site.client.put(
        f"{URL}/publishers", json={"converters": ["mlx-community", "example-gguf"]}
    )
    assert answer.status_code == 200, answer.text


async def test_the_check_lists_every_build_with_its_size_and_one_suggestion(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_llama_cpp(site)
    await approve_gguf_converter(site)
    monkeypatch.setattr(library, "free_bytes", lambda _settings=None: 812 * 1024**3)

    answer = await site.client.post(f"{URL}/preflight", json={"name": OMNI, "variant": None})

    assert answer.status_code == 200, answer.text
    card = answer.json()
    assert card.pop("build_date").startswith("2026-11-03")
    readme = {"path": "README.md", "size": 2324, "part": "text"}
    projector = {"path": "mmproj-demo-omni-30b-BF16.gguf", "size": 1589506176, "part": "pictures"}
    # L6 6.1 field for field, but for two of L1's choices: the publisher row
    # carries the converter sentence, and sizes are written "812.0 GB".
    assert card == {
        "repo_id": OMNI,
        "revision": OMNI_BUILD,
        "revision_short": "49a5b1a",
        "ok": True,
        "verdict": "This model can be added, but it will not run until the overnight budget "
        "is raised.",
        "checks": [
            {
                "key": "publisher",
                "label": "Published by",
                "tone": "ok",
                "value": "example-gguf — an approved converter",
                "detail": "Packaged by example-gguf, not by the maker. FrameFound can check "
                "what a build says it was made from, not that it is true — which is why "
                "converters are a list you control.",
            },
            {
                "key": "built_from",
                "label": "Built from",
                "tone": "ok",
                "value": "nvidia/demo-omni-30b, by nvidia — an approved maker",
                "detail": None,
            },
            {
                "key": "licence",
                "label": "Licence",
                "tone": "ok",
                "value": "other — nvidia-open-model-agreement",
                "detail": "As stated on this build's own page. The maker's page may state a "
                "different one.",
            },
            {
                "key": "access",
                "label": "Access",
                "tone": "ok",
                "value": "Open — no account needed",
                "detail": None,
            },
            {
                "key": "build",
                "label": "Build",
                "tone": "warn",
                "value": "Q4_K_M — one of 3 in this repository",
                "detail": "None of the three fits the overnight budget; this is the smallest. "
                "Choose another under Which build.",
            },
            {
                "key": "files",
                "label": "Files",
                "tone": "ok",
                "value": "3 files to download: the model, the part that reads pictures, and "
                "its README",
                "detail": "Weights are GGUF. The part that reads pictures is "
                "mmproj-demo-omni-30b-BF16.gguf (1.5 GB). Left out: the other builds (Q6_K, "
                "Q8_0) and .gitattributes.",
            },
            {
                "key": "size",
                "label": "Size",
                "tone": "ok",
                "value": "24.3 GB",
                "detail": "The model library has 812.0 GB free.",
            },
            {
                "key": "memory",
                "label": "Memory",
                "tone": "warn",
                "value": "About 29.2 GB — more than the overnight budget",
                "detail": "The overnight budget is 20 GB.",
            },
            {
                "key": "runtime",
                "label": "Runs on",
                "tone": "ok",
                "value": "llama.cpp 0.9.0",
                "detail": "It is a GGUF build; llama.cpp runs those. It will be given room for "
                "16,384 tokens; its page says it can take 262,144.",
            },
        ],
        "problems": [],
        "warnings": [
            "It needs about 29.2 GB, more than the overnight budget (20 GB). It can be "
            "downloaded, but it will not run until the budget is raised."
        ],
        "notes": [],
        "access": "open",
        "size_bytes": 26104638036,
        "memory_gb": 29.2,
        "fits": "never",
        "takes": ["text", "image"],
        "runtime": {"name": "llama-cpp", "label": "llama.cpp", "installed": True},
        "variant": "Q4_K_M",
        "variants": [
            {
                "id": "Q4_K_M",
                "label": "Q4_K_M — 24.3 GB",
                "quant": "Q4_K_M",
                "bits": 4,
                "size_bytes": 26104638036,
                "memory_gb": 29.2,
                "fits": "never",
                "recommended": True,
                "takes": ["text", "image"],
                "note": "",
                "files": [
                    {"path": "demo-omni-30b-Q4_K_M.gguf", "size": 24515129536, "part": "model"},
                    projector,
                    readme,
                ],
            },
            {
                "id": "Q6_K",
                "label": "Q6_K — 32.7 GB",
                "quant": "Q6_K",
                "bits": 6,
                "size_bytes": 35097675348,
                "memory_gb": 39.3,
                "fits": "never",
                "recommended": False,
                "takes": ["text", "image"],
                "note": "",
                "files": [
                    {"path": "demo-omni-30b-Q6_K.gguf", "size": 33508166848, "part": "model"},
                    projector,
                    readme,
                ],
            },
            {
                "id": "Q8_0",
                "label": "Q8_0 — 32.8 GB",
                "quant": "Q8_0",
                "bits": 8,
                "size_bytes": 35175003732,
                "memory_gb": 39.4,
                "fits": "never",
                "recommended": False,
                "takes": ["text", "image"],
                "note": "",
                "files": [
                    {"path": "demo-omni-30b-Q8_0.gguf", "size": 33585495232, "part": "model"},
                    projector,
                    readme,
                ],
            },
        ],
        "existing": None,
    }
    assert site.hub.fetches == 0 and await site.models() == []

    # Choosing another build is the same check again with its figures.
    other = await site.client.post(f"{URL}/preflight", json={"name": OMNI, "variant": "Q8_0"})
    assert other.status_code == 200, other.text
    chosen = other.json()
    assert (chosen["variant"], chosen["size_bytes"], chosen["memory_gb"]) == (
        "Q8_0",
        35175003732,
        39.4,
    )
    assert [variant["recommended"] for variant in chosen["variants"]] == [True, False, False]
    missing = await site.client.post(f"{URL}/preflight", json={"name": OMNI, "variant": "Q5_K_M"})
    assert missing.status_code == 400
    assert message(missing) == "That build is not in this repository."


async def test_download_without_a_choice_takes_the_suggested_build(site: Site) -> None:
    with_llama_cpp(site)
    await approve_gguf_converter(site)
    site.hub.shrink(OMNI)

    added = await site.add(OMNI, OMNI_BUILD, variant=None)
    assert (added["variant"], added["variant_label"], added["runtime"]) == (
        "Q4_K_M",
        "Q4_K_M",
        "llama-cpp",
    )
    assert (added["runtime_label"], added["needs_compare"]) == ("llama.cpp", True)
    assert added["takes"] == ["text", "image"]
    await site.drain()

    [model] = (await site.overview())["models"]
    assert model["state"] == "installed"
    [row] = await site.models()
    folder = library.library_dir(registry.snapshot(row))
    assert folder.parts[-2:] == (OMNI_BUILD, "Q4_K_M")
    manifest = files.read_manifest(folder)
    assert manifest is not None
    assert sorted(file.path for file in manifest.files) == [
        "README.md",
        "demo-omni-30b-Q4_K_M.gguf",
        "mmproj-demo-omni-30b-BF16.gguf",
    ]
    async with site.factory() as db:
        audit = (
            await db.scalars(select(AuditLog).where(AuditLog.event == "local_models.model_added"))
        ).one()
    assert (audit.detail["variant"], audit.detail["runtime"]) == ("Q4_K_M", "llama-cpp")

    # Another build of the same repository is a second model beside it.
    card = (await site.client.post(f"{URL}/preflight", json={"name": OMNI})).json()
    assert card["existing"] == {
        "model_id": model["id"],
        "revision_short": "49a5b1a",
        "variant": "Q4_K_M",
        "same_build": True,
    }
    second = await site.add(OMNI, OMNI_BUILD, variant="Q8_0")
    assert second["variant"] == "Q8_0" and second["id"] != model["id"]
    again = await site.client.post(
        f"{URL}/models", json={"name": OMNI, "revision": OMNI_BUILD, "variant": "Q8_0"}
    )
    assert again.status_code == 409 and message(again) == preflight.ALREADY_INSTALLED
    runtime = {item["name"]: item for item in (await site.overview())["runtimes"]}["llama-cpp"]
    assert runtime["models"] == 2


async def test_installing_llama_cpp_by_a_daily_build_number(site: Site) -> None:
    with_llama_cpp(site, installed=False)
    site.run = fake.FakeRunner(make_llama=True)
    site.index = lambda _url, **_more: {"tag_name": "v0.9.0", "prerelease": False}

    page = await site.overview()
    shown = {runtime["name"]: runtime for runtime in page["runtimes"]}
    assert list(shown) == ["mlx-vlm", "llama-cpp"]
    assert (shown["llama-cpp"]["installed"], shown["llama-cpp"]["models"]) == (None, 0)
    assert shown["llama-cpp"]["version_hint"] == "0.9.0 or a daily build such as b12318"
    assert shown["llama-cpp"]["update_note"] == LlamaCpp.update_note
    assert shown["llama-cpp"]["installed_detail"] is None
    assert (shown["mlx-vlm"]["version_hint"], shown["mlx-vlm"]["update_note"]) == ("0.7.12", "")
    assert shown["mlx-vlm"]["installed_detail"] is None  # MLX-VLM has nothing more to say

    daily = await site.client.post(f"{URL}/runtimes/llama-cpp/install", json={"version": "B12318"})
    assert daily.status_code == 202, daily.text
    job = daily.json()["job"]
    assert (job["kind"], job["subject"]) == ("runtime_install", "llama.cpp b12318")
    await site.drain()

    checked = await site.client.post(f"{URL}/runtimes/llama-cpp/check")
    assert checked.status_code == 200, checked.text
    row = checked.json()
    # A daily build is never "older" than a numbered version (D14).
    assert (row["installed"], row["latest"], row["update_available"]) == (
        "b12318",
        "0.9.0",
        False,
    )
    assert row["installed_detail"] is None  # a daily build is its own build

    numbered = await site.client.post(f"{URL}/runtimes/llama-cpp/install", json={"version": None})
    assert numbered.status_code == 202, numbered.text
    await site.drain()
    row = {item["name"]: item for item in (await site.overview())["runtimes"]}["llama-cpp"]
    assert (row["installed"], row["previous"]) == ("0.9.0", "b12318")
    assert (row["about"], row["installed_detail"]) == ("build b12110", "build b12110")
    same = await site.client.post(f"{URL}/runtimes/llama-cpp/install", json={"version": "v0.9.0"})
    assert same.status_code == 409 and message(same) == "0.9.0 is already the version in use."
    # It is FrameFound's own Python that fetched it: no uv was asked for.
    assert not any("uv" in Path(argv[0]).name for argv in site.run.calls)


async def test_a_bad_llama_cpp_version_is_refused_in_its_own_words(site: Site) -> None:
    with_llama_cpp(site, installed=False)

    answer = await site.client.post(
        f"{URL}/runtimes/llama-cpp/install", json={"version": "the newest please"}
    )

    assert answer.status_code == 400
    assert message(answer) == llama_release.NOT_A_VERSION
    assert await site.job_rows() == []


async def test_llama_cpps_newest_version_that_cannot_be_learned_is_said_in_its_words(
    site: Site,
) -> None:
    with_llama_cpp(site, installed=False)

    def github_down(_url: str, **_more: Any) -> Any:
        raise HubError("unreachable", "The address could not be reached.")

    site.index = github_down
    down = await site.client.post(f"{URL}/runtimes/llama-cpp/check")
    assert down.status_code == 503 and message(down) == llama_release.NEWEST_UNREACHABLE
    row = {item["name"]: item for item in (await site.overview())["runtimes"]}["llama-cpp"]
    assert row["latest_problem"] == llama_release.NEWEST_UNREACHABLE

    def github_limits(_url: str, **_more: Any) -> Any:
        raise HubError("rate_limited", "Too many requests.", 403)

    site.index = github_limits
    limited = await site.client.post(f"{URL}/runtimes/llama-cpp/install", json={})
    assert limited.status_code == 503 and message(limited) == llama_release.LIMITED
    # MLX-VLM's index, down the same way, is still the package index's sentence.
    site.index = github_down
    mlx = await site.client.post(f"{URL}/runtimes/mlx-vlm/check")
    assert mlx.status_code == 503 and message(mlx) == api.INDEX_UNREACHABLE


async def test_an_mlx_model_shows_no_build_and_does_not_ask_for_compare(site: Site) -> None:
    with_llama_cpp(site, installed=False)
    await site.installed()

    page = await site.overview()
    [model] = page["models"]
    assert (model["variant"], model["variant_label"], model["needs_compare"]) == ("", "", False)
    shown = {runtime["name"]: runtime for runtime in page["runtimes"]}
    assert (shown["mlx-vlm"]["models"], shown["llama-cpp"]["models"]) == (1, 0)
    # An install with MLX-VLM only: the second program's row asks for nothing.
    assert shown["llama-cpp"]["installed"] is None and shown["llama-cpp"]["job"] is None


async def test_on_a_server_without_runtimes_nothing_new_is_offered(site: Site) -> None:
    site.unconfigured()

    page = await site.overview()
    assert page["runtimes"] == [] and page["models"] == []
    for path, body in (
        ("/runtimes/llama-cpp/check", None),
        ("/runtimes/llama-cpp/install", {"version": "b12318"}),
        ("/preflight", {"name": OMNI, "variant": "Q4_K_M"}),
    ):
        answer = await site.client.post(URL + path, json=body)
        assert answer.status_code == 409 and message(answer) == UNAVAILABLE, path
    assert site.hub.calls == [] and site.woken == []


# --- the Compare question (L6 6.3, 6.4) -------------------------------------------------

VISION_BUILD = "7b" * 20
LOOKER_QUESTION = (
    "demo-vision-gguf runs on llama.cpp and has not been through Compare on your footage, so "
    "nobody has seen how well it looks at your pictures. Make it the Looker anyway?"
)


async def gguf_model(site: Site) -> str:
    """A GGUF build from an approved maker, fitting by day, on llama.cpp: its id."""
    with_llama_cpp(site)
    site.hub.shrink(VISION)
    return await site.installed(VISION, VISION_BUILD)


async def role_audits(site: Site) -> list[dict[str, Any]]:
    async with site.factory() as db:
        rows = await db.scalars(
            select(AuditLog)
            .where(AuditLog.event == "local_models.role_assigned")
            .order_by(AuditLog.id)
        )
        return [dict(row.detail or {}) for row in rows]


def compared_at(site: Site, moment: datetime | None) -> None:
    """L2's record, as the gate reads it: Compare last finished at `moment`."""

    async def last_finished(_db: AsyncSession, _model_id: uuid.UUID) -> datetime | None:
        return moment

    site.monkeypatch.setattr(gate, "_compared", last_finished)


async def test_giving_an_uncompared_llama_cpp_model_a_role_asks_first(site: Site) -> None:
    # The library is on the NAS, so a yes would copy the model here first.
    site.library_on_the_nas()
    model_id = await gguf_model(site)
    [model] = (await site.overview())["models"]
    assert (model["needs_compare"], model["compared"]) == (True, False)
    assert (model["compared_at"], model["compared_by"]) == (None, None)

    asked = await site.client.put(f"{URL}/roles/looker", json={"model_id": model_id})
    assert asked.status_code == 428
    assert asked.json()["error"]["code"] == "confirmation_required"
    assert message(asked) == LOOKER_QUESTION
    editor = await site.client.put(f"{URL}/roles/editor", json={"model_id": model_id})
    # It reads pictures, so its Test is a picture Test: only Compare is named.
    assert editor.status_code == 428 and message(editor) == (
        "demo-vision-gguf runs on llama.cpp and has not been through Compare on your footage, "
        "so nobody has seen an answer from it. Make it the Editor anyway?"
    )
    # Asking changed nothing, and queued no copy.
    page = await site.overview()
    assert [(role["model_id"], role["pending_model_id"]) for role in page["roles"]] == [
        (None, None),
        (None, None),
    ]
    assert [row.kind for row in await site.job_rows()] == ["download"]
    assert await role_audits(site) == []

    # A refusal comes before the question: a yes would not have helped.
    await site.needs(model_id, 600.0)
    refused = await site.client.put(f"{URL}/roles/looker", json={"model_id": model_id})
    assert refused.status_code == 409 and "budget" in message(refused)


async def test_saying_yes_gives_the_role_and_the_audit_row_says_it_was_skipped(
    site: Site,
) -> None:
    model_id = await gguf_model(site)
    given = await site.client.put(
        f"{URL}/roles/looker", json={"model_id": model_id, "skip_compare": True}
    )
    assert given.status_code == 200, given.text
    assert (given.json()["model_id"], given.json()["compared"]) == (model_id, False)
    [audit] = await role_audits(site)
    assert audit["compare"] == "skipped" and audit["copy"] is False
    # The model that has the role is not asked again ("Copy again" lands here).
    again = await site.client.put(f"{URL}/roles/looker", json={"model_id": model_id})
    assert again.status_code == 200, again.text

    # Once a comparison has finished, nobody is asked and the card stops saying so.
    finished = datetime(2026, 11, 13, 22, 5, tzinfo=UTC)
    compared_at(site, finished)
    page = await site.overview()
    assert page["roles"][0]["compared"] is True
    [model] = page["models"]
    assert (model["compared"], model["compared_by"]) == (True, "compare")
    assert utc(model["compared_at"]) == finished
    editor = await site.client.put(f"{URL}/roles/editor", json={"model_id": model_id})
    assert editor.status_code == 200, editor.text
    assert [audit["compare"] for audit in await role_audits(site)] == ["skipped", "seen"]


async def test_a_yes_to_a_model_that_must_be_copied_first_queues_the_copy(site: Site) -> None:
    site.library_on_the_nas()
    model_id = await gguf_model(site)
    given = await site.client.put(
        f"{URL}/roles/looker", json={"model_id": model_id, "skip_compare": True}
    )
    assert given.status_code == 202, given.text
    assert given.json()["pending_model_id"] == model_id and given.json()["compared"] is None
    [audit] = await role_audits(site)
    assert (audit["copy"], audit["compare"]) == (True, "skipped")
    await site.drain()
    looker = (await site.overview())["roles"][0]
    assert (looker["model_id"], looker["ready"], looker["compared"]) == (model_id, True, False)


async def test_taking_a_role_away_is_never_asked_about(site: Site) -> None:
    model_id = await gguf_model(site)
    await site.client.put(f"{URL}/roles/looker", json={"model_id": model_id, "skip_compare": True})
    away = await site.client.put(f"{URL}/roles/looker", json={"model_id": None})
    assert away.status_code == 200, away.text
    assert (away.json()["model_id"], away.json()["compared"]) == (None, None)


async def test_a_renamed_compare_lookup_still_shows_the_page_and_still_asks(site: Site) -> None:
    model_id = await gguf_model(site)
    # L2's function renamed underneath the gate: the page and the role must
    # still answer, as "not compared yet", never as a 500 (P17).
    site.monkeypatch.delattr(comparisons, "last_finished_for")
    page = await site.client.get(URL)
    assert page.status_code == 200, page.text
    [model] = page.json()["models"]
    assert (model["needs_compare"], model["compared"]) == (True, False)
    asked = await site.client.put(f"{URL}/roles/looker", json={"model_id": model_id})
    assert asked.status_code == 428 and message(asked) == LOOKER_QUESTION
    given = await site.client.put(
        f"{URL}/roles/looker", json={"model_id": model_id, "skip_compare": True}
    )
    assert given.status_code == 200, given.text
    [audit] = await role_audits(site)
    assert audit["compare"] == "skipped"


async def test_an_mlx_model_is_given_a_role_without_the_question(site: Site) -> None:
    with_llama_cpp(site)
    compared_at(site, None)
    model_id = await site.installed()
    [model] = (await site.overview())["models"]
    assert (model["needs_compare"], model["compared"], model["compared_by"]) == (False, True, None)
    given = await site.client.put(f"{URL}/roles/looker", json={"model_id": model_id})
    assert given.status_code == 200, given.text
    assert given.json()["compared"] is None
    [audit] = await role_audits(site)
    assert audit["compare"] == "not_needed"
