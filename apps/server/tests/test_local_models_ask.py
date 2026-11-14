"""Asking a model (local_models/ask.py), the stand-in model server
(`fake.FakeModelServer`, `fake.fake_answer`) and the one place the outside
world is handed in (local_models/deps.py).

The model server is `fake.FakeModelServer`, which runs the real server's own
check on every body it is sent; `HttpModelServer` is driven through an
`httpx.MockTransport`. Models are fixture pages from `local_models/demo_data`
shrunk to a few kilobytes, put through the real check and really
"downloaded" from `fake.FakeHub` into a library under `tmp_path`; MLX-VLM is
"installed" by `fake.FakeRunner`. The database is file-backed SQLite. A
fixture makes starting a process or reaching a real address fail the test.
"""

import inspect
import json
import subprocess
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.models import LocalModel
from framefound.local_models import (
    UNAVAILABLE,
    fake,
    library,
    preflight,
    registry,
    runtimes,
    schedule,
    server,
)
from framefound.local_models import ask as asking_module
from framefound.local_models.ask import (
    JSON_ONLY,
    LIBRARY_SILENT,
    MAX_PICTURE_BYTES,
    RECORD_DAMAGED,
    REPAIR,
    Answer,
    HttpModelServer,
    ModelFailed,
    NotNow,
    ServerRefused,
    ServerUnreachable,
    ask,
    extract_json,
    server_for,
    sweep,
)
from framefound.local_models.deps import Deps, deps_for
from framefound.local_models.hub import HttpHub
from framefound.local_models.kinds import MlxVlm
from framefound.local_models.settings_store import LocalModelsConfig, load_config, save_config
from framefound.storage import stores

Factory = async_sessionmaker[AsyncSession]

SECRET = "not-a-real-secret-key"
FIRST = "mlx-community/gemma-4-e4b-it-4bit"
FIRST_BUILD = "475b9088d29754a3379866cf5aeb6b41acd313c2"
TEXT_ONLY = "mlx-community/gemma-4-12B-it-OptiQ-4bit"
VERSION = "0.7.12"
NOON = datetime(2026, 11, 12, 12, 0)
LATE = datetime(2026, 11, 12, 23, 0)
# The start of a JPEG and a few made-up bytes: ask() looks at no more.
JPEG = b"\xff\xd8\xff\xe0made-up picture"

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
    "FRAMEFOUND_MODEL_SERVER_PORT",
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
    get_settings.cache_clear()
    stores._CACHE.clear()
    yield
    get_settings.cache_clear()
    stores._CACHE.clear()


@pytest.fixture(autouse=True)
def _no_answers_left_behind() -> Iterator[None]:
    """`answer_for` is class-wide: a test that registers an answer must not leave it."""
    before = dict(fake.FakeModelServer._answers)
    yield
    fake.FakeModelServer._answers.clear()
    fake.FakeModelServer._answers.update(before)


@pytest.fixture()
async def factory(tmp_path: Path) -> AsyncIterator[Factory]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


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

    def install_mlx_vlm(self, version: str = VERSION) -> None:
        def prints_its_version(argv: list[str]) -> tuple[int, str]:
            return 0, f"{Path(argv[0]).parents[2].name}\n"

        run = fake.FakeRunner(
            {
                "uv venv": (0, ""),
                "uv pip install": (0, ""),
                "uv pip freeze": (0, ""),
                "python": prints_its_version,
            },
            make_venvs=True,
        )
        runtimes.install(MlxVlm(), version, self.runtimes, run=run)
        runtimes.activate(self.runtimes, "mlx-vlm", version)

    async def add(
        self,
        factory: Factory,
        repo: str = FIRST,
        *,
        role: str | None = None,
        state: str = registry.INSTALLED,
        download: bool = True,
    ) -> uuid.UUID:
        """A model checked and added as the page would, its files in the library."""
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


@pytest.fixture()
def site(tmp_path: Path) -> Site:
    made = Site(tmp_path)
    made.install_mlx_vlm()
    return made


class Caption(BaseModel):
    caption: str = Field(max_length=200)
    tags: list[str] = Field(min_length=1, max_length=8)
    score: int = Field(ge=1, le=5)


GOOD = '{"caption": "A bright kitchen", "tags": ["kitchen"], "score": 4}'
ASKED = {"role": "user", "content": "Describe."}


async def asking(
    factory: Factory,
    site: Site,
    who: str | uuid.UUID,
    model_server: fake.FakeModelServer | None = None,
    **more: Any,
) -> Answer:
    """Ask as a handler does, in its own session, and commit what was recorded."""
    values: dict[str, Any] = {
        "prompt": "Describe this picture.",
        "prompt_version": "test-1",
        "now": NOON,
        "settings": site.settings,
        "server": model_server or fake.FakeModelServer(),
    }
    values.update(more)
    async with factory() as db:
        answer = await ask(db, who, **values)
        await db.commit()
    return answer


async def refused(factory: Factory, site: Site, who: str | uuid.UUID, **more: Any) -> NotNow:
    with pytest.raises(NotNow) as raised:
        await asking(factory, site, who, **more)
    return raised.value


async def set_config(factory: Factory, **changes: Any) -> None:
    async with factory() as db:
        config = await load_config(db)
        for name, value in changes.items():
            setattr(config, name, value)
        await save_config(db, config)


async def set_model(factory: Factory, model_id: uuid.UUID, **changes: Any) -> None:
    async with factory() as db:
        model = await db.get(LocalModel, model_id)
        assert model is not None
        for name, value in changes.items():
            setattr(model, name, value)
        await db.commit()


async def stored(factory: Factory, model_id: uuid.UUID) -> LocalModel:
    async with factory() as db:
        model = await registry.get_model(db, model_id)
        assert model is not None
        return model


# --- the contract --------------------------------------------------------------------


def test_the_signature_of_ask_is_the_contracts() -> None:
    """Section 13.1: later phases call it by exactly these names."""
    parameters = inspect.signature(ask).parameters
    assert [(p.name, p.kind.name) for p in parameters.values()] == [
        ("db", "POSITIONAL_OR_KEYWORD"),
        ("who", "POSITIONAL_OR_KEYWORD"),
        ("prompt", "KEYWORD_ONLY"),
        ("prompt_version", "KEYWORD_ONLY"),
        ("instructions", "KEYWORD_ONLY"),
        ("pictures", "KEYWORD_ONLY"),
        ("schema", "KEYWORD_ONLY"),
        ("max_tokens", "KEYWORD_ONLY"),
        ("timeout_s", "KEYWORD_ONLY"),
        ("server", "KEYWORD_ONLY"),
        ("now", "KEYWORD_ONLY"),
        ("settings", "KEYWORD_ONLY"),
    ]
    defaults = {name: p.default for name, p in parameters.items() if p.default is not p.empty}
    assert defaults == {
        "instructions": "",
        "pictures": (),
        "schema": None,
        "max_tokens": 600,
        "timeout_s": 300.0,
        "server": None,
        "now": None,
        "settings": None,
    }


def test_an_answer_and_both_failures_can_be_built_by_hand() -> None:
    """A later phase's tests replace ask() with a function that returns a hand-built
    Answer or raises one of these, constructed exactly so."""
    answer = Answer(
        text=GOOD,
        parsed=None,
        model_id=uuid.uuid4(),
        repo_id=FIRST,
        revision=FIRST_BUILD,
        runtime="mlx-vlm",
        runtime_version=VERSION,
        prompt_version="look-1",
        seconds=4.2,
        load_seconds=14.0,
        memory_gb=6.1,
        memory_source="runtime",
        finish_reason="stop",
        ran_from="this_machine",
        attempts=1,
        demo=False,
    )
    assert answer.provenance() == {
        "model": FIRST,
        "model_revision": FIRST_BUILD,
        "runtime": "mlx-vlm",
        "runtime_version": VERSION,
        "prompt_version": "look-1",
    }

    later = NotNow("window", "Waiting.", NOON)
    assert (later.reason, str(later), later.not_before, later.retry) == (
        "window",
        "Waiting.",
        NOON,
        True,
    )
    assert NotNow("no_model", "None yet.").retry is False
    failed = ModelFailed("bad_answer", "Not the form.", "words", "", "length")
    assert (failed.reason, failed.text, failed.detail, failed.finish_reason) == (
        "bad_answer",
        "words",
        "",
        "length",
    )
    # A reason nobody can handle is a mistake, not a third kind of failure.
    with pytest.raises(ValueError):
        NotNow("later", "?")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ModelFailed("broken", "?")


# --- each reason it may not run -------------------------------------------------------


async def test_a_container_server_refuses_before_reading_anything(
    factory: Factory, site: Site
) -> None:
    site.settings = site.make(runtimes_dir=None)
    model_server = fake.FakeModelServer()

    found = await refused(factory, site, "looker", model_server=model_server)

    assert (found.reason, str(found), found.retry) == ("unavailable", UNAVAILABLE, False)
    assert model_server.requests == []


async def test_a_pause_holds_every_question(factory: Factory, site: Site) -> None:
    model_id = await site.add(factory, role="looker")
    await set_config(factory, paused=True)

    for who in ("looker", model_id):
        found = await refused(factory, site, who)
        assert (found.reason, str(found), found.retry) == ("paused", schedule.PAUSED, True)


async def test_a_role_nobody_has_is_said_by_its_name(factory: Factory, site: Site) -> None:
    await site.add(factory)  # installed, but nobody gave it a role

    found = await refused(factory, site, "editor")

    assert found.reason == "no_model" and not found.retry
    assert str(found).startswith("No model is the Editor yet.")


async def test_a_model_still_arriving_or_gone_is_not_installed(
    factory: Factory, site: Site
) -> None:
    arriving = await site.add(factory, state=registry.DOWNLOADING, download=False)
    stopped = await site.add(factory, TEXT_ONLY, state=registry.INCOMPLETE, download=False)

    for who, said in (
        (arriving, registry.STILL_DOWNLOADING),
        (stopped, registry.NOT_COMPLETE),
        (uuid.uuid4(), "That model is no longer installed."),
    ):
        found = await refused(factory, site, who)
        assert (found.reason, str(found), found.retry) == ("not_installed", said, False)


async def test_a_publisher_blocked_after_install_stops_the_model(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory, role="looker")
    await set_config(factory, blocked_added=["mlx-community"])

    found = await refused(factory, site, "looker")

    assert found.reason == "not_approved" and not found.retry
    assert str(found).startswith("It is no longer approved: ")
    # Nothing was deleted or taken away: put the list back and it runs again.
    await set_config(factory, blocked_added=[])
    answer = await asking(factory, site, "looker")
    assert answer.model_id == model_id


async def test_a_model_whose_program_is_missing_says_what_to_install(
    factory: Factory, tmp_path: Path
) -> None:
    site = Site(tmp_path)  # MLX-VLM never installed
    model_id = await site.add(factory)

    found = await refused(factory, site, model_id)
    assert (found.reason, found.retry) == ("no_runtime", False)
    assert str(found) == (
        "MLX-VLM is not installed. Install it under Software that runs models on the AI "
        "models page."
    )

    await set_model(factory, model_id, runtime="some-other-program")
    found = await refused(factory, site, model_id)
    assert (found.reason, str(found)) == ("no_runtime", registry.NO_PROGRAM_HERE)


async def test_a_damaged_record_of_the_program_is_said_without_its_path(
    factory: Factory, site: Site
) -> None:
    """Everyone who can see the job reads its message; the file it names is for
    an administrator's details."""
    model_id = await site.add(factory)
    state = site.runtimes / "mlx-vlm" / "state.json"
    state.write_text("{not json", encoding="utf-8")

    found = await refused(factory, site, model_id)
    assert (found.reason, found.retry) == ("no_runtime", False)
    assert str(found) == RECORD_DAMAGED
    assert str(site.runtimes) not in str(found)
    assert str(state) in found.detail


async def test_a_library_that_does_not_answer_is_waited_for_not_hung_on(
    factory: Factory, site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A share that has hung would hold the lane, which runs one job at a time."""
    model_id = await site.add(factory)
    hung = threading.Event()

    def never_answers(*_args: Any) -> Any:
        hung.wait(30)
        raise AssertionError("the share answered after all")

    monkeypatch.setattr(asking_module, "LIBRARY_WAIT_S", 0.05)
    monkeypatch.setattr(asking_module, "_folder", never_answers)
    try:
        found = await refused(factory, site, model_id)
    finally:
        hung.set()
    assert (found.reason, found.retry, str(found)) == ("library_away", True, LIBRARY_SILENT)


async def test_a_model_over_the_daytime_budget_waits_for_the_window(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory, role="looker")
    await set_model(factory, model_id, memory_gb=10.1)

    found = await refused(factory, site, "looker", now=NOON)
    assert (found.reason, found.retry) == ("window", True)
    assert found.not_before == datetime(2026, 11, 12, 21, 30)
    assert str(found).startswith("Waiting for the overnight window (starts 21:30)")

    # Overnight the larger budget is in force.
    answer = await asking(factory, site, "looker", now=LATE)
    assert answer.model_id == model_id

    await set_model(factory, model_id, memory_gb=25.0)
    found = await refused(factory, site, "looker", now=LATE)
    assert (found.reason, found.not_before, found.retry) == ("never_fits", None, False)
    assert "more than the overnight budget (20 GB)" in str(found)


async def test_a_roles_model_must_be_on_this_machine(factory: Factory, site: Site) -> None:
    site.library_on_the_nas()
    model_id = await site.add(factory, role="looker")
    model_server = fake.FakeModelServer()

    found = await refused(factory, site, "looker", model_server=model_server)
    assert (found.reason, str(found), found.retry) == (
        "not_here",
        library.NOT_ON_THIS_MACHINE,
        True,
    )
    assert model_server.requests == []

    library.place(registry.snapshot(await stored(factory, model_id)), settings=site.settings)
    answer = await asking(factory, site, "looker", model_server=model_server)
    assert answer.ran_from == "this_machine"
    here = library.local_dir(registry.snapshot(await stored(factory, model_id)), site.settings)
    assert model_server.requests[-1]["model"] == str(here)


async def test_a_test_may_run_from_the_library(factory: Factory, site: Site) -> None:
    site.library_on_the_nas()
    model_id = await site.add(factory)  # no role, so never copied to this machine
    model_server = fake.FakeModelServer()

    answer = await asking(factory, site, model_id, model_server=model_server)

    assert answer.ran_from == "library"
    folder = library.library_dir(registry.snapshot(await stored(factory, model_id)), site.settings)
    assert model_server.requests[-1]["model"] == str(folder)

    # With the share gone it waits for it, and says so without a path.
    site.nas_drops()
    found = await refused(factory, site, str(model_id), model_server=model_server)
    assert (found.reason, found.retry) == ("library_away", True)
    assert str(found).endswith(library.ONLY_IN_LIBRARY)
    assert str(site.nas) not in str(found)


async def test_a_model_whose_files_left_the_library_is_not_installed(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory)
    folder = library.library_dir(registry.snapshot(await stored(factory, model_id)), site.settings)
    (folder / "model.safetensors").unlink()

    found = await refused(factory, site, model_id)

    assert (found.reason, str(found), found.retry) == (
        "not_installed",
        library.NOT_IN_LIBRARY,
        False,
    )


async def test_a_server_that_does_not_answer_is_asked_again_in_a_minute(
    factory: Factory, site: Site
) -> None:
    await site.add(factory, role="looker")
    model_server = fake.FakeModelServer()
    model_server.down = True

    found = await refused(factory, site, "looker", model_server=model_server)

    assert (found.reason, found.retry) == ("server_down", True)
    assert found.not_before == NOON + timedelta(seconds=60)


@pytest.mark.parametrize(
    ("code", "raised", "reason"),
    [
        ("busy", NotNow, "busy"),
        ("library_away", NotNow, "library_away"),
        ("no_runtime", NotNow, "no_runtime"),
        ("load_failed", ModelFailed, "load_failed"),
        ("load_timeout", ModelFailed, "load_failed"),
        ("port_in_use", ModelFailed, "load_failed"),
        ("bad_files", ModelFailed, "bad_files"),
        ("not_allowed", ModelFailed, "bad_files"),
        ("died", ModelFailed, "died"),
        ("timeout", ModelFailed, "timeout"),
        ("runtime_error", ModelFailed, "runtime_error"),
    ],
)
async def test_each_refusal_of_the_server_becomes_its_reason_in_the_servers_words(
    factory: Factory, site: Site, code: str, raised: type[Exception], reason: str
) -> None:
    await site.add(factory, role="looker")
    model_server = fake.FakeModelServer()
    model_server.refuse(code, "The server's own sentence.")

    with pytest.raises(raised) as found:
        await asking(factory, site, "looker", model_server=model_server)

    error: Any = found.value
    assert (error.reason, str(error)) == (reason, "The server's own sentence.")


async def test_the_runtimes_last_words_are_kept_with_a_failure(
    factory: Factory, site: Site
) -> None:
    await site.add(factory, role="looker")
    model_server = fake.FakeModelServer()

    def dies(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ServerRefused(502, "died", server.DIED, "Killed: 9")

    model_server.chat = dies  # type: ignore[method-assign]
    with pytest.raises(ModelFailed) as found:
        await asking(factory, site, "looker", model_server=model_server)

    assert (found.value.reason, str(found.value), found.value.detail) == (
        "died",
        server.DIED,
        "Killed: 9",
    )


# --- the question, and the answer -----------------------------------------------------


async def test_the_question_is_one_message_the_real_server_accepts(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory, role="looker")
    model_server = fake.FakeModelServer()

    await asking(
        factory,
        site,
        "looker",
        model_server=model_server,
        instructions="You catalogue footage for a property marketer.",
        prompt="Describe this picture.",
        pictures=[JPEG, JPEG],
        schema=Caption,
        max_tokens=300,
    )

    (body,) = model_server.requests
    # The real server's own check ran on it in the fake; and nothing else is sent.
    assert set(body) <= server.ALLOWED_KEYS
    folder = library.local_dir(registry.snapshot(await stored(factory, model_id)), site.settings)
    assert body["model"] == str(folder)
    assert (body["temperature"], body["stream"], body["max_tokens"]) == (0, False, 300)
    (message,) = body["messages"]
    assert message["role"] == "user"
    text, *pictures = message["content"]
    # Instructions in the same user turn: small models' templates differ on system roles.
    assert text == {
        "type": "text",
        "text": "You catalogue footage for a property marketer.\n\nDescribe this picture."
        f"\n\n{JSON_ONLY}",
    }
    assert pictures == [MlxVlm().picture_part(JPEG)] * 2
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Caption", "strict": True, "schema": Caption.model_json_schema()},
    }


async def test_an_answer_carries_the_model_its_build_and_the_prompt_version(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory, role="looker")
    model_server = fake.FakeModelServer(load_seconds=14.0, answer_seconds=4.2)

    answer = await asking(factory, site, "looker", model_server=model_server, schema=Caption)

    assert answer.provenance() == {
        "model": FIRST,
        "model_revision": FIRST_BUILD,
        "runtime": "mlx-vlm",
        "runtime_version": VERSION,
        "prompt_version": "test-1",
    }
    assert (answer.model_id, answer.repo_id, answer.revision) == (model_id, FIRST, FIRST_BUILD)
    assert (answer.seconds, answer.load_seconds, answer.attempts) == (4.2, 14.0, 1)
    assert (answer.finish_reason, answer.ran_from, answer.demo) == ("stop", "this_machine", True)
    assert isinstance(answer.parsed, Caption)
    assert answer.parsed == Caption.model_validate_json(answer.text)
    # Which program, at which version, last loaded it: what a failed load
    # after an update is explained by.
    assert (await stored(factory, model_id)).last_loaded_with == f"mlx-vlm/{VERSION}"

    # The second question finds it loaded; and the version is the one the
    # server says it ran, when it says.
    model_server.runtime_version = "0.7.13"
    again = await asking(factory, site, "looker", model_server=model_server, schema=Caption)
    assert (again.load_seconds, again.runtime_version) == (0.0, "0.7.13")
    assert again.text == answer.text  # the same question, the same answer


async def test_without_a_schema_the_words_come_back_as_they_were_written(
    factory: Factory, site: Site
) -> None:
    await site.add(factory, role="editor")
    model_server = fake.FakeModelServer()

    answer = await asking(factory, site, "editor", model_server=model_server)

    assert answer.parsed is None and answer.text.startswith("A made-up answer")
    assert "response_format" not in model_server.requests[0]
    assert JSON_ONLY not in json.dumps(model_server.requests[0])


@pytest.mark.parametrize(
    "written",
    [
        f"```json\n{GOOD}\n```",
        f"Sure! Here is the JSON you asked for:\n\n```\n{GOOD}\n```\nLet me know.",
        f"Here it is: {GOOD} Hope that helps.",
    ],
)
async def test_an_answer_wrapped_in_a_code_fence_is_still_read(
    factory: Factory, site: Site, written: str
) -> None:
    await site.add(factory, role="looker")
    fake.FakeModelServer.answer_for("Caption", lambda _body: written)

    answer = await asking(factory, site, "looker", schema=Caption)

    assert answer.parsed == Caption.model_validate_json(GOOD)
    assert (answer.text, answer.attempts) == (written, 1)


def test_the_json_is_found_between_the_first_and_last_brace() -> None:
    assert extract_json('noise {"a": {"b": 1}} more') == '{"a": {"b": 1}}'
    assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json("no object at all") == "no object at all"


class Settling(fake.FakeModelServer):
    """Reports a little less memory, and the other kind of figure, each time it answers."""

    def chat(self, body: dict[str, Any], *, idle_s: float, timeout_s: float) -> dict[str, Any]:
        reply = super().chat(body, idle_s=idle_s, timeout_s=timeout_s)
        self.memory_gb = (self.memory_gb or 0.0) - 0.5
        self.memory_source = "process"
        return reply


async def test_a_bad_answer_gets_one_chance_to_be_repaired(factory: Factory, site: Site) -> None:
    await site.add(factory, role="looker")
    model_server = Settling(load_seconds=10.0, answer_seconds=2.0, memory_gb=6.5)
    model_server.prose_first(1)

    answer = await asking(factory, site, "looker", model_server=model_server, schema=Caption)

    assert answer.attempts == 2 and isinstance(answer.parsed, Caption)
    # Both turns count; the memory is the larger figure, with how THAT one was measured.
    assert (answer.seconds, answer.load_seconds) == (4.0, 10.0)
    assert (answer.memory_gb, answer.memory_source) == (6.5, "runtime")
    first, second = model_server.requests
    question = first["messages"][0]
    # The repair turn: the question, what the model wrote, and one plain request.
    assert second["messages"] == [
        question,
        {"role": "assistant", "content": fake.PROSE},
        {"role": "user", "content": REPAIR},
    ]


async def test_a_second_bad_answer_fails_the_item_with_what_was_written(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory, role="looker")
    model_server = fake.FakeModelServer(memory_gb=6.1)
    model_server.prose_first(2)
    model_server.finish_reason = "length"

    with pytest.raises(ModelFailed) as found:
        await asking(factory, site, "looker", model_server=model_server, schema=Caption)

    assert (found.value.reason, found.value.text, found.value.finish_reason) == (
        "bad_answer",
        fake.PROSE,
        "length",
    )
    # An answer that ran out of room was cut off, not malformed; it says so.
    assert str(found.value) == (
        "Its answer was not the form asked for, twice (it ran out of room before it finished)."
    )
    assert len(model_server.requests) == 2
    # Nothing was committed: a handler whose item failed keeps none of its session.
    assert (await stored(factory, model_id)).last_loaded_with is None


async def test_the_measured_memory_only_goes_up(factory: Factory, site: Site) -> None:
    model_id = await site.add(factory, role="looker")
    model_server = fake.FakeModelServer(memory_gb=7.0)

    answer = await asking(factory, site, "looker", model_server=model_server)
    assert (answer.memory_gb, answer.memory_source) == (7.0, "runtime")
    assert (await stored(factory, model_id)).measured_memory_gb == 7.0

    model_server.memory_gb = 5.0
    answer = await asking(factory, site, "looker", model_server=model_server)
    assert answer.memory_gb == 5.0
    model = await stored(factory, model_id)
    assert model.measured_memory_gb == 7.0
    assert registry.memory_gb(model) == 7.0


async def test_what_ask_records_is_the_callers_to_commit(factory: Factory, site: Site) -> None:
    model_id = await site.add(factory, role="looker")
    model_server = fake.FakeModelServer(memory_gb=7.0)

    async with factory() as db:
        await ask(
            db,
            "looker",
            prompt="Describe this picture.",
            prompt_version="test-1",
            now=NOON,
            settings=site.settings,
            server=model_server,
        )
        await db.rollback()

    model = await stored(factory, model_id)
    assert (model.measured_memory_gb, model.last_loaded_with) == (None, None)


@pytest.mark.parametrize(
    ("more", "said"),
    [
        ({"prompt_version": " "}, "A question needs its prompt version"),
        ({"pictures": [JPEG] * 9}, "at most 8 pictures"),
        ({"pictures": [JPEG + b"x" * MAX_PICTURE_BYTES]}, "at most 1,500,000 bytes"),
        ({"pictures": [b"\x89PNG\r\n"]}, "as JPEG"),
        ({"max_tokens": 0}, "at least one token"),
    ],
)
async def test_a_callers_mistake_is_refused_before_anything_is_asked(
    factory: Factory, site: Site, more: dict[str, Any], said: str
) -> None:
    await site.add(factory, role="looker")
    model_server = fake.FakeModelServer()

    with pytest.raises(ValueError, match=said):
        await asking(factory, site, "looker", model_server=model_server, **more)
    with pytest.raises(ValueError, match="no role called"):
        await asking(factory, site, "director", model_server=model_server)
    assert model_server.requests == []


# --- the sweep --------------------------------------------------------------------------


async def test_a_model_loaded_past_its_window_is_unloaded_by_the_sweep(
    factory: Factory, site: Site
) -> None:
    model_id = await site.add(factory, role="looker")
    await set_model(factory, model_id, memory_gb=10.1)
    model_server = fake.FakeModelServer()
    await asking(factory, site, "looker", model_server=model_server, now=LATE)
    assert model_server.loaded is not None

    async with factory() as db:
        # Still the overnight window: it stays.
        assert await sweep(db, model_server, now=LATE, settings=site.settings) is None
        assert model_server.unloads == 0
        # Morning: over the daytime budget, so it is unloaded without waiting
        # for the next question.
        morning = datetime(2026, 11, 13, 6, 0)
        why = await sweep(db, model_server, now=morning, settings=site.settings)

    assert why is not None and why.startswith("Waiting for the overnight window")
    assert (model_server.loaded, model_server.unloads) == (None, 1)


async def test_a_pause_unloads_the_model_at_the_next_sweep(factory: Factory, site: Site) -> None:
    await site.add(factory, role="looker")
    model_server = fake.FakeModelServer()
    await asking(factory, site, "looker", model_server=model_server)
    await set_config(factory, paused=True)

    async with factory() as db:
        why = await sweep(db, model_server, now=NOON, settings=site.settings)

    assert (why, model_server.loaded) == (schedule.PAUSED, None)


async def test_the_sweep_leaves_alone_what_it_cannot_read(factory: Factory, site: Site) -> None:
    model_id = await site.add(factory, role="looker")
    await set_model(factory, model_id, memory_gb=10.1)
    model_server = fake.FakeModelServer()

    async with factory() as db:
        # Nothing loaded.
        assert await sweep(db, model_server, now=NOON, settings=site.settings) is None
        # A folder that is no model's row.
        stranger = {"model": str(site.models / "stranger"), "messages": [ASKED]}
        model_server.chat(stranger, idle_s=1, timeout_s=1)
        assert await sweep(db, model_server, now=NOON, settings=site.settings) is None
        # A server that does not answer.
        model_server.down = True
        assert await sweep(db, model_server, now=NOON, settings=site.settings) is None
    assert model_server.unloads == 0


async def test_a_model_in_the_middle_of_an_answer_is_left_to_finish(
    factory: Factory, site: Site
) -> None:
    """It was allowed when it was asked; unloading would wait out the answer and
    hold up the lane. The next sweep looks again."""
    await site.add(factory, role="looker")
    model_server = fake.FakeModelServer()
    await asking(factory, site, "looker", model_server=model_server)
    await set_config(factory, paused=True)
    resting = model_server.status()
    assert resting is not None

    for state in ("loading", "busy"):
        model_server.status = lambda state=state: {**resting, "state": state}  # type: ignore[method-assign]
        async with factory() as db:
            assert await sweep(db, model_server, now=NOON, settings=site.settings) is None
    assert model_server.unloads == 0


# --- the stand-in model server and its made-up answers -------------------------------


class Sliders(BaseModel):
    exposure: float = Field(ge=-2.0, le=2.0)
    warmth: float = Field(gt=0.0, lt=1.0)
    contrast: int = Field(ge=-100, le=100)
    stars: int = Field(ge=1, le=5)
    step: int = Field(ge=0, le=100, multiple_of=25)
    title: str = Field(min_length=3, max_length=12)
    mood: Literal["calm", "busy", "dark"]
    keep: bool
    tags: list[str] = Field(min_length=2, max_length=4)
    note: Caption | None = None


def test_a_made_up_answer_stays_inside_the_schemas_bounds() -> None:
    schema = Sliders.model_json_schema()
    answers = [fake.fake_answer(schema, f"seed {n}") for n in range(200)]
    read = [Sliders.model_validate_json(text) for text in answers]

    for one in read:
        # Inside a range, never at its stops.
        assert -2.0 < one.exposure < 2.0 and 0.0 < one.warmth < 1.0
        assert -100 < one.contrast < 100 and 1 < one.stars < 5
        assert one.step in (25, 50, 75)
        assert one.note is not None and 1 < one.note.score < 5
    # The seed varies what is said, and the same seed says the same.
    assert {one.keep for one in read} == {True, False}
    assert {one.mood for one in read} == {"calm", "busy", "dark"}
    assert len({one.exposure for one in read}) > 50
    assert fake.fake_answer(schema, "seed 7") == answers[7]
    assert fake.fake_answer(None, "anything") == "{}"


async def test_a_later_phase_says_what_the_fake_answers_for_its_schema() -> None:
    seen: list[dict[str, Any]] = []

    def answers(body: dict[str, Any]) -> str:
        seen.append(body)
        return GOOD

    fake.FakeModelServer.answer_for("Caption", answers)
    model_server = fake.FakeModelServer()
    body = {
        "model": "/models/x",
        "messages": [ASKED],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "Caption", "schema": Caption.model_json_schema()},
        },
    }

    reply = model_server.chat(body, idle_s=300, timeout_s=300)

    assert reply["choices"][0]["message"]["content"] == GOOD
    assert seen == [body]
    fake.FakeModelServer.answer_for("Caption", None)
    reply = model_server.chat(body, idle_s=300, timeout_s=300)
    Caption.model_validate_json(reply["choices"][0]["message"]["content"])


def test_the_fake_refuses_what_the_real_server_refuses() -> None:
    model_server = fake.FakeModelServer()
    address = {"type": "image_url", "image_url": {"url": "https://example.com/a.jpg"}}

    for body in (
        {"model": "/m", "messages": [ASKED], "adapter_path": "/elsewhere"},
        {"model": "/m", "messages": [{"role": "user", "content": [address]}]},
    ):
        with pytest.raises(ServerRefused) as found:
            model_server.chat(body, idle_s=300, timeout_s=300)
        assert (found.value.status, found.value.code) == (400, "not_allowed")
    with pytest.raises(ValueError, match="no refusal called"):
        model_server.refuse("tired")


# --- the real client of the model server -----------------------------------------------


def test_the_client_sends_the_token_and_reads_the_servers_refusals() -> None:
    sent: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path == "/v1/chat/completions":
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "load_failed",
                        "message": "It would not load.",
                        "detail": "Traceback: out of memory",
                    }
                },
            )
        if request.url.path == "/unload":
            return httpx.Response(200, json={"unloaded": True})
        return httpx.Response(200, json={"state": "idle"})

    client = HttpModelServer(
        "http://127.0.0.1:54805", server.token_for(SECRET), transport=httpx.MockTransport(answer)
    )

    with pytest.raises(ServerRefused) as found:
        client.chat({"model": "/m", "messages": [ASKED]}, idle_s=999_999, timeout_s=99_999)
    assert (found.value.status, found.value.code, str(found.value), found.value.detail) == (
        409,
        "load_failed",
        "It would not load.",
        "Traceback: out of memory",
    )
    assert client.status() == {"state": "idle"}
    assert client.unload() is True

    chat = sent[0]
    assert (chat.method, str(chat.url)) == ("POST", "http://127.0.0.1:54805/v1/chat/completions")
    assert json.loads(chat.content) == {"model": "/m", "messages": [ASKED]}
    # The token the real server checks against, made from the same secret.
    assert all(each.headers[server.TOKEN_HEADER] == server.token_for(SECRET) for each in sent)
    # Kept inside what the server accepts.
    assert chat.headers[server.IDLE_HEADER] == "86400"
    assert chat.headers[server.TIMEOUT_HEADER] == "3600"


@pytest.mark.parametrize(
    ("trouble", "raised"),
    [
        (httpx.ConnectError("refused"), ServerUnreachable),
        # A port nobody answers on is "not there", not "took too long to answer".
        (httpx.ConnectTimeout("no answer"), ServerUnreachable),
        (httpx.ReadTimeout("slow"), ServerRefused),
        (httpx.RemoteProtocolError("dropped"), ServerUnreachable),
    ],
)
def test_a_server_that_is_not_there_or_hangs_is_said_plainly(
    trouble: Exception, raised: type[Exception]
) -> None:
    def answer(_request: httpx.Request) -> httpx.Response:
        raise trouble

    client = HttpModelServer("http://127.0.0.1:54805", "t", transport=httpx.MockTransport(answer))

    with pytest.raises(raised) as found:
        client.chat({"model": "/m", "messages": [ASKED]}, idle_s=60, timeout_s=60)
    assert "127.0.0.1" not in str(found.value)
    if isinstance(found.value, ServerRefused):
        assert found.value.code == "timeout"
    # A status that cannot be read is "not answering", never an error.
    assert client.status() is None


def test_an_answer_that_is_not_json_is_a_runtime_error() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        status = 200 if request.url.path == "/v1/chat/completions" else 500
        return httpx.Response(status, text="<html>oops</html>")

    client = HttpModelServer("http://127.0.0.1:54805", "t", transport=httpx.MockTransport(answer))

    with pytest.raises(ServerRefused) as found:
        client.chat({"model": "/m", "messages": [ASKED]}, idle_s=60, timeout_s=60)
    assert found.value.code == "runtime_error"
    with pytest.raises(ServerRefused) as found:
        client.unload()
    assert (found.value.status, found.value.code) == (500, "runtime_error")


# --- which server, and which outside world --------------------------------------------


def test_the_real_install_asks_the_model_server_on_loopback(tmp_path: Path) -> None:
    site = Site(tmp_path)

    found = server_for(site.make(model_server_port=60123))

    assert isinstance(found, HttpModelServer)
    assert found._base == "http://127.0.0.1:60123"
    assert found._token == server.token_for(SECRET)


def test_the_demo_has_one_stand_in_and_a_real_install_never_gets_it(tmp_path: Path) -> None:
    site = Site(tmp_path)
    demo = site.make(runtimes_dir=None, local_ai_demo=True)

    first, second = server_for(demo), server_for(demo)
    assert isinstance(first, fake.FakeModelServer) and first is second

    # The flag is ignored wherever runtimes are configured or a public address is set.
    for real in (
        site.make(local_ai_demo=True),
        site.make(runtimes_dir=None, local_ai_demo=True, domain="media.example.com"),
    ):
        assert not isinstance(server_for(real), fake.FakeModelServer)


def test_the_outside_world_is_handed_in_one_place(tmp_path: Path) -> None:
    site = Site(tmp_path)

    deps = deps_for(site.settings)

    assert isinstance(deps, Deps) and deps.settings is site.settings
    assert list(deps.kinds) == ["mlx-vlm", "llama-cpp"]
    assert isinstance(deps.server, HttpModelServer)
    assert isinstance(deps.hub_for("not-a-real-token"), HttpHub)
    assert deps.uv == "uv" and deps.system and deps.machine
    # Where models cannot run there is nothing to run them with, but the page can still draw.
    assert dict(deps_for(site.make(runtimes_dir=None)).kinds) == {}
    # The demo's stand-ins arrive with demo mode itself: one for each program.
    demo_kinds = deps_for(site.make(runtimes_dir=None, local_ai_demo=True)).kinds
    assert list(demo_kinds) == ["demo", "demo-gguf"]
