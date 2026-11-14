"""The model server (local_models/server.py): one runtime at a time, on loopback.

The runtime is `fake.FakeSpawner` / `fake.FakeChild` — nothing is started —
and its HTTP API is an `httpx.MockTransport` that answers as MLX-VLM is
believed to (design L1, F5). The clock and the sleep are a counter, `ps` and
`kill` are `fake.FakeRunner`, and the port probe is a switch. The app is
driven through `httpx.ASGITransport`. A fixture makes starting a real
process, or reaching a real address, fail the test.
"""

import asyncio
import errno
import importlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from framefound.config import Settings, get_settings
from framefound.local_models import fake, files, gguf, llama_release, runtimes, server
from framefound.local_models.hub import RepoFile
from framefound.local_models.kinds import LlamaCpp, MlxVlm, Serve
from framefound.local_models.server import Refused, Supervisor, create_app, token_for
from framefound.native import proc
from framefound.native.layout import Layout
from framefound.storage import stores

SECRET = "not-a-real-secret-key"
FIRST = "mlx-community/gemma-4-e4b-it-4bit"
FIRST_REV = "475b9088d29754a3379866cf5aeb6b41acd313c2"
SECOND = "mlx-community/gemma-4-12B-it-OptiQ-4bit"
SECOND_REV = "de9e183868050b34bf6b4272518500ffcebf15b3"
PORT = 54806
JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgG"
GB = 1024**3

_OURS = (
    "FRAMEFOUND_BULK_DIR",
    "FRAMEFOUND_PROXY_DIR",
    "FRAMEFOUND_MODELS_DIR",
    "FRAMEFOUND_DATA_DIR",
    "FRAMEFOUND_RUNTIMES_DIR",
    "FRAMEFOUND_LOCAL_AI_DEMO",
    "FRAMEFOUND_DOMAIN",
    "FRAMEFOUND_SECRET_KEY",
    "FRAMEFOUND_MODEL_RUNTIME_PORT",
)


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test may start a program or reach a real address; trying to is a failure.

    The event loop of an async test makes a loopback socket pair of its own
    on Windows, so sockets as such are not refused: what is refused is every
    way this code could reach out — a real process, a port probe, and a real
    HTTP transport.
    """

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


class Clock:
    """Time that moves only when the server sleeps or a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Port:
    """Whether something listens on the runtime's port. A `kill` of `owner` closes it."""

    def __init__(self) -> None:
        self.open = False
        self.owner: int | None = None
        self.asked: list[int] = []

    def __call__(self, port: int) -> bool:
        self.asked.append(port)
        return self.open

    def killed(self, argv: list[str]) -> tuple[int, str]:
        if self.owner is not None and argv[-1] == str(self.owner):
            self.open = False
        return 0, ""


class Runtime:
    """MLX-VLM's HTTP API, in memory: `/health` and `/v1/chat/completions`, with its key."""

    def __init__(self, spawner: fake.FakeSpawner, clock: Clock) -> None:
        self.spawner = spawner
        self.clock = clock
        # How many more times /health says it is still loading.
        self.loading = 0
        self.never_ready = False
        self.warmups = 0
        self.chats: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []
        self.urls: list[str] = []
        # Replaces the answer to a real chat: a response, or raise.
        self.reply: Callable[[httpx.Request, dict[str, Any]], httpx.Response] | None = None
        self.timings: dict[str, Any] | None = {"peak_memory": 5.1}
        self.takes = 0.0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        key = self.spawner.last.env.get("MLX_VLM_SERVER_API_KEY")
        if request.headers.get("authorization") != f"Bearer {key}":
            return httpx.Response(401, json={"detail": "Invalid API key"})
        if request.url.path == "/health":
            if self.never_ready or self.loading > 0:
                self.loading -= 1
                return httpx.Response(503, json={"status": "loading"})
            return httpx.Response(200, json={"status": "healthy", "loaded_model": "x"})
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        if body.get("max_tokens") == 1 and body["messages"] == [{"role": "user", "content": "ok"}]:
            self.warmups += 1
            return httpx.Response(200, json=self.answer("O"))
        self.chats.append(body)
        self.headers.append(request.headers)
        self.clock.now += self.takes
        if self.reply is not None:
            return self.reply(request, body)
        return httpx.Response(200, json=self.answer("A tree beside a lake."))

    def answer(self, text: str) -> dict[str, Any]:
        reply: dict[str, Any] = {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        if self.timings is not None:
            reply["timings"] = self.timings
        return reply


class Machine:
    """A Mac with FrameFound's folders under `tmp_path`, MLX-VLM installable, nothing running."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.data = tmp_path / "data"
        self.models = tmp_path / "models"
        self.runtimes = tmp_path / "runtimes"
        for folder in (self.data, self.models, self.runtimes):
            folder.mkdir(parents=True)
        self.layout = Layout(home=tmp_path / "home", source=tmp_path)
        self.clock = Clock()
        self.port = Port()
        self.spawner = fake.FakeSpawner()
        self.runtime = Runtime(self.spawner, self.clock)
        self.runner = fake.FakeRunner(
            {
                "ps -o rss=": (0, "2097152\n"),
                "ps -o command=": (1, ""),
                "kill": self.port.killed,
            }
        )

    def settings(self, **more: Any) -> Settings:
        values: dict[str, Any] = {
            "data_dir": self.data,
            "models_dir": self.models,
            "runtimes_dir": self.runtimes,
            "secret_key": SECRET,
            "min_free_gb": 0,
        }
        values.update(more)
        return Settings(**values)

    def install(self, *versions: str) -> None:
        """Install and switch to each version, as the page's Install and Update do."""
        run = fake.FakeRunner(
            {
                "uv venv": (0, ""),
                "uv pip install": (0, ""),
                "uv pip freeze": (0, ""),
                "python": lambda argv: (0, f"{Path(argv[0]).parents[2].name}\n"),
            },
            make_venvs=True,
        )
        for version in versions:
            runtimes.install(MlxVlm(), version, self.runtimes, run=run)
            runtimes.activate(self.runtimes, "mlx-vlm", version)

    def supervisor(self, settings: Settings | None = None, **more: Any) -> Supervisor:
        options: dict[str, Any] = {
            "spawn": self.spawner,
            "http": httpx.MockTransport(self.runtime.handler),
            "run": self.runner,
            "port_open": self.port,
            "clock": self.clock,
            "sleep": self.clock.sleep,
            "load_timeout_s": 60.0,
            "layout": self.layout,
        }
        options.update(more)
        return Supervisor(settings or self.settings(), **options)

    @property
    def here(self) -> Path:
        """Where a role's copy lives on this machine."""
        return self.models / "local-ai"

    @property
    def library(self) -> Path:
        """The library, on this machine (no bulk folder)."""
        return self.models / "library"


@pytest.fixture()
def mac(tmp_path: Path) -> Machine:
    machine = Machine(tmp_path)
    machine.install("0.7.12")
    return machine


def model(
    root: Path,
    repo: str = FIRST,
    revision: str = FIRST_REV,
    *,
    runtime: str = "mlx-vlm",
    contents: dict[str, bytes] | None = None,
    listed: dict[str, int] | None = None,
) -> Path:
    """A whole model folder as the library writes one: its files, then its manifest."""
    publisher, name = repo.split("/")
    folder = root / publisher / name / revision
    files_here = contents or {
        "config.json": b'{"model_type": "gemma4"}',
        "model.safetensors": b"weights" * 64,
        "tokenizer.json": b'{"version": "1.0"}',
    }
    for path, body in files_here.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    sizes = {path: len(body) for path, body in files_here.items()}
    sizes.update(listed or {})
    files.write_manifest(
        folder,
        files.Manifest(
            repo_id=repo,
            revision=revision,
            variant="",
            runtime=runtime,
            files=tuple(RepoFile(path, size, None, None) for path, size in sizes.items()),
            completed_at="2026-11-12T18:39:10Z",
        ),
    )
    return folder


def ask(folder: Path | str, *parts: dict[str, Any], **more: Any) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": "What is in this picture?"}]
    content.extend(parts)
    body: dict[str, Any] = {
        "model": str(folder),
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": 40,
    }
    body.update(more)
    return body


def refused(call: Callable[[], Any]) -> Refused:
    with pytest.raises(Refused) as caught:
        call()
    return caught.value


def picture(url: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": url}}


def app_client(
    app: Any,
    *,
    host: str = "127.0.0.1",
    client: tuple[str, int] = ("127.0.0.1", 50123),
) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=client)
    return httpx.AsyncClient(transport=transport, base_url=f"http://{host}:54805")


def token() -> dict[str, str]:
    return {server.TOKEN_HEADER: token_for(SECRET)}


# --- who may ask --------------------------------------------------------------------


def test_the_token_is_made_from_the_secret_key() -> None:
    made = token_for(SECRET)
    assert made == token_for(SECRET)
    assert made != token_for("another-key-not-a-real-one")
    assert len(made) == 64 and int(made, 16) >= 0
    assert SECRET not in made


async def test_a_request_without_the_token_is_refused(mac: Machine) -> None:
    folder = model(mac.here)
    app = create_app(mac.settings(), mac.supervisor())
    async with app_client(app) as client:
        assert (await client.get("/healthz")).json() == {"ok": True}
        for headers in ({}, {server.TOKEN_HEADER: "not-the-token"}):
            answer = await client.post("/v1/chat/completions", json=ask(folder), headers=headers)
            assert answer.status_code == 401
            assert answer.json()["error"]["code"] == "bad_token"
            assert (await client.get("/status", headers=headers)).status_code == 401
            assert (await client.post("/unload", headers=headers)).status_code == 401
        assert (await client.get("/status", headers=token())).json()["state"] == "idle"
    assert mac.spawner.spawned == []


async def test_with_no_secret_key_every_request_is_refused(mac: Machine) -> None:
    settings = mac.settings(secret_key="")
    app = create_app(settings, mac.supervisor(settings))
    async with app_client(app) as client:
        for answer in (
            await client.get("/status", headers={server.TOKEN_HEADER: token_for("")}),
            await client.post("/unload"),
            await client.post("/v1/chat/completions", json=ask(model(mac.here))),
        ):
            assert answer.status_code == 503
            assert answer.json()["error"] == {
                "code": "no_secret",
                "message": server.NO_KEY_SET,
                "detail": "",
            }
        assert (await client.get("/healthz")).status_code == 200
    assert mac.spawner.spawned == []


@pytest.mark.parametrize("host", ["192.168.1.20", "10.0.0.5", "0.0.0.0", "203.0.113.9"])  # noqa: S104 - a server address the test claims, to be refused
async def test_a_server_bound_beyond_loopback_answers_nothing(mac: Machine, host: str) -> None:
    # A request that arrived on another of the machine's addresses: the
    # service was bound wider than 127.0.0.1 by mistake. Nothing is served.
    app = create_app(mac.settings(), mac.supervisor())
    async with app_client(app, host=host) as client:
        for answer in (
            await client.get("/healthz"),
            await client.get("/status", headers=token()),
            await client.post("/v1/chat/completions", json=ask(model(mac.here)), headers=token()),
        ):
            assert answer.status_code == 403
            assert answer.json()["error"]["message"] == server.NOT_LOOPBACK
    assert mac.spawner.spawned == []


async def test_a_request_from_another_machine_is_refused(mac: Machine) -> None:
    app = create_app(mac.settings(), mac.supervisor())
    async with app_client(app, client=("192.168.1.30", 51515)) as client:
        answer = await client.get("/status", headers=token())
        assert answer.status_code == 403
        assert answer.json()["error"]["code"] == "not_loopback"


@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]"])
async def test_loopback_in_either_form_is_answered(mac: Machine, host: str) -> None:
    client_host = "::1" if host == "[::1]" else host
    app = create_app(mac.settings(), mac.supervisor())
    async with app_client(app, host=host, client=(client_host, 50000)) as client:
        assert (await client.get("/status", headers=token())).status_code == 200


# --- what may be loaded -------------------------------------------------------------


def test_a_folder_outside_the_models_folders_is_refused(mac: Machine) -> None:
    elsewhere = model(mac.root / "elsewhere")
    model(mac.here)
    supervisor = mac.supervisor()
    for named in (
        FIRST,
        str(elsewhere),
        str(mac.here),
        str(mac.library),
        str(
            mac.here
            / ".."
            / ".."
            / "elsewhere"
            / "mlx-community"
            / "gemma-4-e4b-it-4bit"
            / FIRST_REV
        ),
        "",
        "relative/folder",
    ):
        refusal = refused(lambda named=named: supervisor.chat(ask(named)))
        assert (refusal.status, refusal.code, refusal.message) == (
            400,
            "not_allowed",
            server.NOT_OUR_FOLDER,
        ), named
    for odd in (None, 42, ["a"]):
        refusal = refused(lambda odd=odd: supervisor.chat({**ask("x"), "model": odd}))
        assert refusal.code == "not_allowed"
    assert mac.spawner.spawned == []
    assert mac.runtime.urls == []


def test_a_folder_named_relative_to_where_the_server_runs_is_refused(
    mac: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It would land inside the models folder, but only by an accident of
    # where the service happens to be running from.
    model(mac.here)
    monkeypatch.chdir(mac.models)
    relative = f"local-ai/{FIRST}/{FIRST_REV}"
    refusal = refused(lambda: mac.supervisor().chat(ask(relative)))
    assert refusal.code == "not_allowed"
    assert mac.spawner.spawned == []


def test_a_link_out_of_the_models_folder_is_followed_and_refused(mac: Machine) -> None:
    outside = model(mac.root / "elsewhere")
    link = mac.here / "mlx-community" / "sneaky"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this machine cannot make a link")
    refusal = refused(lambda: mac.supervisor().chat(ask(link)))
    assert refusal.code == "not_allowed"
    assert mac.spawner.spawned == []


def test_a_model_runs_from_this_machine_or_from_the_library(mac: Machine) -> None:
    supervisor = mac.supervisor()
    for folder in (model(mac.here), model(mac.library, SECOND, SECOND_REV)):
        answer = supervisor.chat(ask(folder))
        assert answer["choices"][0]["message"]["content"] == "A tree beside a lake."
    assert len(mac.spawner.spawned) == 2


def test_a_library_on_the_nas_is_where_its_models_may_run_from(mac: Machine) -> None:
    nas = mac.root / "nas"
    nas.mkdir()
    stores.mark_bulk_root(nas)
    settings = mac.settings(bulk_dir=nas)
    on_the_nas = model(nas / "models" / "library")
    answer = mac.supervisor(settings).chat(ask(on_the_nas))
    assert answer["framefound"]["repo_id"] == FIRST
    # The library that used to be on this machine is no longer one of them.
    refusal = refused(lambda: mac.supervisor(settings).chat(ask(model(mac.library))))
    assert refusal.code == "not_allowed"


@pytest.mark.parametrize(
    "part",
    [
        picture("https://example.com/frame.jpg"),
        picture("http://127.0.0.1:8080/api/v1/assets/1/thumb"),
        picture("file:///Users/someone/Pictures/frame.jpg"),
        picture("/Users/someone/Pictures/frame.jpg"),
        picture("data:image/png;base64,iVBORw0KGgo="),
        picture("data:image/jpeg;base64,../../../etc/passwd"),
        {"type": "image_url", "image_url": "https://example.com/frame.jpg"},
        {"type": "image_url", "image_url": {"url": JPEG, "fetch": "https://example.com"}},
        {"type": "image_url", "image_url": {"url": JPEG}, "file": "/Users/someone/frame.jpg"},
    ],
)
def test_a_picture_given_as_an_address_is_refused(mac: Machine, part: dict[str, Any]) -> None:
    refusal = refused(lambda: mac.supervisor().chat(ask(model(mac.here), part)))
    assert (refusal.status, refusal.code, refusal.message) == (
        400,
        "not_allowed",
        server.NOT_A_PICTURE,
    )
    assert mac.spawner.spawned == []


def test_a_picture_given_as_data_reaches_the_runtime_as_it_was_sent(mac: Machine) -> None:
    mac.supervisor().chat(ask(model(mac.here), picture(JPEG)))
    sent = mac.runtime.chats[0]["messages"][0]["content"]
    assert sent[1] == picture(JPEG)


@pytest.mark.parametrize(
    "more",
    [
        {"adapter_path": "adapters/one"},
        {"draft_model": "someone/else"},
        {"tools": []},
        {"messages": [{"role": "user", "content": [{"type": "input_audio", "data": "x"}]}]},
        {"messages": [{"role": "user", "content": [{"type": "image", "url": JPEG}]}]},
        {"messages": [{"role": "tool", "content": "x"}]},
        {"messages": [{"role": "user", "content": "x", "name": "someone"}]},
        {"messages": []},
        {"messages": "What is in this picture?"},
    ],
)
def test_anything_framefound_would_not_send_is_refused(mac: Machine, more: dict[str, Any]) -> None:
    refusal = refused(lambda: mac.supervisor().chat({**ask(model(mac.here)), **more}))
    assert refusal.status == 400
    assert refusal.code in {"not_allowed", "bad_request"}
    assert mac.spawner.spawned == []


@pytest.mark.parametrize(
    ("spoil", "said"),
    [
        (lambda f: (f / "modeling_gemma.py").write_text("import os"), "modeling_gemma.py"),
        (
            # Its own settings, as its manifest lists them, now ask for code.
            lambda f: model(
                f.parents[2],
                contents={
                    "config.json": b'{"model_type": "gemma4", "model_file": "modeling.py"}',
                    "model.safetensors": b"weights",
                    "tokenizer.json": b"{}",
                },
            ),
            files.NEEDS_CODE,
        ),
        (lambda f: (f / "model.safetensors").unlink(), "missing model.safetensors"),
        (lambda f: (f / "model.safetensors").write_bytes(b"short"), "not the size"),
        (lambda f: (f / files.MANIFEST).unlink(), "no list of its files"),
    ],
)
def test_a_folder_that_fails_the_check_is_never_loaded(
    mac: Machine, spoil: Callable[[Path], Any], said: str
) -> None:
    folder = model(mac.here)
    spoil(folder)
    refusal = refused(lambda: mac.supervisor().chat(ask(folder)))
    assert (refusal.status, refusal.code) == (409, "bad_files")
    assert said in refusal.message
    assert mac.spawner.spawned == []


def test_the_folder_is_checked_again_before_every_request(mac: Machine) -> None:
    folder = model(mac.here)
    supervisor = mac.supervisor()
    supervisor.chat(ask(folder))
    (folder / "processing_gemma.py").write_text("import subprocess")
    refusal = refused(lambda: supervisor.chat(ask(folder)))
    assert refusal.code == "bad_files"
    assert "processing_gemma.py" in refusal.message
    assert len(mac.runtime.chats) == 1


def test_a_library_that_cannot_be_read_is_not_called_a_bad_model(
    mac: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stalled(_folder: Path) -> str | None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(server.files, "folder_problem", stalled)
    refusal = refused(lambda: mac.supervisor().chat(ask(model(mac.here))))
    assert (refusal.status, refusal.code) == (503, "library_away")
    assert (
        refusal.message == "The model's folder could not be read just now. Try again in a moment."
    )
    assert mac.spawner.spawned == []


def test_a_model_on_a_library_that_is_away_says_so(
    mac: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    nas = mac.root / "nas"
    nas.mkdir()
    stores.mark_bulk_root(nas)
    settings = mac.settings(bulk_dir=nas)
    folder = model(nas / "models" / "library")
    (nas / "models" / stores.BULK_STORE_MARKER).unlink()
    stores._CACHE.clear()

    def stalled(_folder: Path) -> str | None:
        raise OSError(errno.ETIMEDOUT, "Operation timed out")

    monkeypatch.setattr(server.files, "folder_problem", stalled)
    refusal = refused(lambda: mac.supervisor(settings).chat(ask(folder)))
    assert refusal.code == "library_away"
    assert "not connected" in refusal.message
    assert str(nas) not in refusal.message


def test_a_library_share_that_has_dropped_is_not_called_a_broken_model(mac: Machine) -> None:
    """On a Mac a share that drops leaves its mount point behind, empty: the
    model's folder has no manifest, as an unfinished model would. The
    library's marker says which it is."""
    nas = mac.root / "nas"
    nas.mkdir()
    stores.mark_bulk_root(nas)
    settings = mac.settings(bulk_dir=nas)
    folder = model(nas / "models" / "library")
    for file in list(folder.iterdir()):
        file.unlink()  # what an empty mount point shows
    (nas / "models" / stores.BULK_STORE_MARKER).unlink()
    stores._CACHE.clear()

    refusal = refused(lambda: mac.supervisor(settings).chat(ask(folder)))

    assert (refusal.status, refusal.code) == (503, "library_away")
    assert "not connected" in refusal.message and str(nas) not in refusal.message
    assert mac.spawner.spawned == []

    # Connected, the same empty folder is what it looks like: not a whole model.
    stores.mark_store(nas / "models", stores.BULK_STORE_MARKER, kind="models")
    stores._CACHE.clear()
    refusal = refused(lambda: mac.supervisor(settings).chat(ask(folder)))
    assert (refusal.status, refusal.code) == (409, "bad_files")


def test_a_hung_library_never_holds_up_a_model_run_from_this_machine(
    mac: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library is on a share that has hung: any look at it waits. A model
    whose copy is on this machine is answered without one, and a request
    for a model in the library waits on its own, not holding the one turn."""
    nas = mac.root / "nas"
    nas.mkdir()
    stores.mark_bulk_root(nas)
    settings = mac.settings(bulk_dir=nas)
    on_the_nas = model(nas / "models" / "library", SECOND, SECOND_REV)
    here = model(mac.here)
    stalled, release = threading.Event(), threading.Event()
    real_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        if path.is_relative_to(nas):
            if threading.current_thread() is threading.main_thread():
                raise AssertionError("the share was looked at for a model on this machine")
            stalled.set()
            release.wait(10)
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    supervisor = mac.supervisor(settings)
    asked: list[object] = []

    def ask_the_library() -> None:
        try:
            asked.append(supervisor.chat(ask(on_the_nas)))
        except Refused as refusal:
            asked.append(refusal)

    waiting = threading.Thread(target=ask_the_library)
    waiting.start()
    try:
        assert stalled.wait(10)
        answer = supervisor.chat(ask(here))
        assert answer["framefound"]["repo_id"] == FIRST
        began = time.monotonic()
        mac.clock.now += 10_000
        supervisor.tick()  # the idle unload is not held up either
        assert time.monotonic() - began < 1.0
        assert not mac.spawner.children[0].running
    finally:
        release.set()
        waiting.join(10)
    assert asked and isinstance(asked[0], dict)  # and it is answered once the share is back


# --- which software, and how it is started -----------------------------------------------


def test_without_its_software_installed_a_model_is_not_started(tmp_path: Path) -> None:
    bare = Machine(tmp_path)
    refusal = refused(lambda: bare.supervisor().chat(ask(model(bare.here))))
    assert (refusal.status, refusal.code, refusal.message) == (
        409,
        "no_runtime",
        "MLX-VLM is not installed.",
    )
    assert bare.spawner.spawned == []


def test_a_damaged_record_of_the_software_in_use_is_not_read_as_not_installed(
    mac: Machine,
) -> None:
    (mac.runtimes / "mlx-vlm" / "state.json").write_text("{not json")
    refusal = refused(lambda: mac.supervisor().chat(ask(model(mac.here))))
    assert refusal.code == "no_runtime"
    assert "is damaged" in refusal.message
    assert mac.spawner.spawned == []


def test_a_model_for_software_this_install_lacks_is_not_started(mac: Machine) -> None:
    folder = model(mac.here, runtime="not-a-runtime")
    refusal = refused(lambda: mac.supervisor().chat(ask(folder)))
    assert refusal.code == "no_runtime"
    assert refusal.message == (
        "This model needs software this install does not have (not-a-runtime)."
    )


def test_the_demo_runtime_is_never_started(mac: Machine) -> None:
    folder = model(mac.here, runtime="demo")
    supervisor = mac.supervisor(kinds={"demo": fake.DemoKind()})
    refusal = refused(lambda: supervisor.chat(ask(folder)))
    assert (refusal.code, refusal.message) == ("no_runtime", server.DEMO_NEVER_STARTS)
    assert mac.spawner.spawned == []


def test_the_runtime_is_started_offline_on_loopback_with_the_model_folder(
    mac: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The service runs with framefound.env loaded: none of it may reach the child.
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", SECRET)
    monkeypatch.setenv("POSTGRES_PASSWORD", "not-a-real-password")
    monkeypatch.setenv("HF_HOME", str(mac.models / "huggingface"))
    monkeypatch.setenv("MLX_TRUST_REMOTE_CODE", "true")
    folder = model(mac.here)
    mac.supervisor().chat(ask(folder))

    started = mac.spawner.last
    version = mac.runtimes / "mlx-vlm" / "0.7.12"
    scratch = mac.runtimes / "scratch"
    assert started.argv == [
        str(version / "venv" / "bin" / "python"),
        "-m",
        "mlx_vlm.server",
        "--host",
        "127.0.0.1",
        "--port",
        str(PORT),
        "--model",
        str(folder.resolve()),
        "--log-level",
        "WARNING",
    ]
    assert started.cwd == scratch
    assert (scratch / "hf-home").is_dir()
    env = started.env
    key = env["MLX_VLM_SERVER_API_KEY"]
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["HF_HOME"] == str(scratch / "hf-home")
    assert "MLX_TRUST_REMOTE_CODE" not in env
    assert not [name for name in env if name.startswith(("FRAMEFOUND_", "POSTGRES"))]
    assert SECRET not in env.values()
    assert not any(key in word for word in started.argv)
    assert not any("trust" in word for word in started.argv)
    # Spoken to on loopback only, with that key, about that same folder.
    assert {url.split("/")[2] for url in mac.runtime.urls} == {f"127.0.0.1:{PORT}"}
    assert mac.runtime.headers[0]["authorization"] == f"Bearer {key}"
    assert mac.runtime.chats[0]["model"] == str(folder.resolve())
    assert mac.runtime.chats[0]["stream"] is False


class Careless(MlxVlm):
    """A runtime kind someone got wrong: the server must not start it."""

    def __init__(self, change: Callable[[Serve], Serve]) -> None:
        self.change = change

    def serve(
        self, folder: Path, model_dir: Path, variant: str, port: int, key: str, scratch: Path
    ) -> Serve:
        return self.change(super().serve(folder, model_dir, variant, port, key, scratch))


def _argv(serve: Serve, argv: list[str]) -> Serve:
    return Serve(argv=argv, env=serve.env, cwd=serve.cwd)


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (
            lambda s: _argv(s, [w if w != "127.0.0.1" else "0.0.0.0" for w in s.argv]),  # noqa: S104 - a mistake the server refuses
            "it would listen beyond this machine",
        ),
        (
            lambda s: _argv(s, [w for w in s.argv if w not in {"--host", "127.0.0.1"}]),
            "it would listen beyond this machine",
        ),
        (
            # Named twice: the program takes the last.
            lambda s: _argv(s, [*s.argv, "--host", "0.0.0.0"]),  # noqa: S104 - a mistake the server refuses
            "it would listen beyond this machine",
        ),
        (lambda s: _argv(s, [*s.argv, "--trust-remote-code"]), "code that came with the model"),
        (
            lambda s: _argv(s, [*s.argv, "--api-key", s.env["MLX_VLM_SERVER_API_KEY"]]),
            "its key would be readable",
        ),
    ],
)
def test_a_start_that_is_not_safe_is_refused(
    mac: Machine, change: Callable[[Serve], Serve], why: str
) -> None:
    supervisor = mac.supervisor(kinds={"mlx-vlm": Careless(change)})
    refusal = refused(lambda: supervisor.chat(ask(model(mac.here))))
    assert refusal.code == "load_failed"
    assert refusal.message.startswith("FrameFound will not start MLX-VLM that way:")
    assert why in refusal.message
    assert mac.spawner.spawned == []


def test_the_child_is_offline_whatever_its_kind_wrote(mac: Machine) -> None:
    def online(serve: Serve) -> Serve:
        env = {k: v for k, v in serve.env.items() if k != "HF_HUB_OFFLINE"}
        env.update({"MLX_TRUST_REMOTE_CODE": "true", "FRAMEFOUND_SECRET_KEY": SECRET})
        return Serve(argv=serve.argv, env=env, cwd=serve.cwd)

    mac.supervisor(kinds={"mlx-vlm": Careless(online)}).chat(ask(model(mac.here)))
    env = mac.spawner.last.env
    assert env["HF_HUB_OFFLINE"] == "1"
    assert "MLX_TRUST_REMOTE_CODE" not in env
    assert "FRAMEFOUND_SECRET_KEY" not in env


# --- one model at a time ------------------------------------------------------------------


def test_the_same_model_is_not_loaded_twice(mac: Machine) -> None:
    folder = model(mac.here)
    supervisor = mac.supervisor()
    first = supervisor.chat(ask(folder))
    second = supervisor.chat(
        ask(
            str(
                mac.here
                / "mlx-community"
                / ".."
                / "mlx-community"
                / "gemma-4-e4b-it-4bit"
                / FIRST_REV
            )
        )
    )
    assert len(mac.spawner.spawned) == 1
    assert mac.runtime.warmups == 1
    assert first["framefound"]["load_seconds"] >= 0
    assert second["framefound"]["load_seconds"] == 0.0
    assert mac.spawner.children[0].running


def test_asking_for_another_model_stops_the_first(mac: Machine) -> None:
    supervisor = mac.supervisor()
    supervisor.chat(ask(model(mac.here)))
    second = model(mac.here, SECOND, SECOND_REV)
    answer = supervisor.chat(ask(second))
    first_child, second_child = mac.spawner.children
    assert first_child.signals == ["terminate"] and not first_child.running
    assert second_child.running
    assert mac.spawner.last.argv[mac.spawner.last.argv.index("--model") + 1] == str(
        second.resolve()
    )
    assert answer["framefound"]["repo_id"] == SECOND
    assert supervisor.status()["repo_id"] == SECOND


def test_a_change_of_software_version_loads_the_model_again_under_it(mac: Machine) -> None:
    mac.install("0.7.13")
    folder = model(mac.here)
    supervisor = mac.supervisor()
    assert supervisor.chat(ask(folder))["framefound"]["runtime_version"] == "0.7.13"
    runtimes.rollback(mac.runtimes, "mlx-vlm")
    answer = supervisor.chat(ask(folder))
    assert answer["framefound"]["runtime_version"] == "0.7.12"
    assert len(mac.spawner.spawned) == 2
    assert "0.7.12" in mac.spawner.last.argv[0]
    assert not mac.spawner.children[0].running


def test_an_idle_model_is_unloaded(mac: Machine) -> None:
    supervisor = mac.supervisor()
    supervisor.chat(ask(model(mac.here)), idle_s=300)
    child = mac.spawner.children[0]
    assert supervisor.record_path.is_file()
    mac.clock.now += 299
    supervisor.tick()
    assert child.running
    assert supervisor.status()["unloads_in_s"] == 1.0
    mac.clock.now += 1
    supervisor.tick()
    assert child.signals == ["terminate"] and not child.running
    assert supervisor.status()["state"] == "idle"
    assert supervisor.status()["problem"] is None
    assert not supervisor.record_path.exists()


def test_each_request_says_how_long_its_model_may_stay_idle(mac: Machine) -> None:
    folder = model(mac.here)
    supervisor = mac.supervisor()
    supervisor.chat(ask(folder), idle_s=600)
    supervisor.chat(ask(folder), idle_s=60)
    mac.clock.now += 61
    supervisor.tick()
    assert supervisor.status()["state"] == "idle"


def test_a_runtime_that_dies_while_loading_is_reported_with_its_last_words(
    mac: Machine,
) -> None:
    mac.spawner.queued.append(fake.FakeChild(exits_with=1, after_polls=3))
    mac.spawner.says = lambda start: (
        "Loading the model…\nTraceback (most recent call last):\n"
        f"  started with key {start.env['MLX_VLM_SERVER_API_KEY']}\n"
        "ValueError: Received parameters not in model: vision_tower.weight\n"
    )
    mac.runtime.loading = 10
    supervisor = mac.supervisor()
    refusal = refused(lambda: supervisor.chat(ask(model(mac.here))))
    key = mac.spawner.last.env["MLX_VLM_SERVER_API_KEY"]
    assert (refusal.status, refusal.code, refusal.message) == (
        409,
        "load_failed",
        "It would not load.",
    )
    assert "ValueError: Received parameters not in model" in refusal.detail
    assert "started with key ***" in refusal.detail
    assert key not in refusal.detail
    assert supervisor.status()["state"] == "idle"
    assert supervisor.status()["problem"] == "It would not load."
    assert not supervisor.record_path.exists()


def test_only_this_starts_words_are_reported(mac: Machine) -> None:
    supervisor = mac.supervisor()
    mac.spawner.says = "first start: fine\n"
    supervisor.chat(ask(model(mac.here)))
    mac.spawner.queued.append(fake.FakeChild(exits_with=1))
    mac.spawner.says = "second start: broken\n"
    refusal = refused(lambda: supervisor.chat(ask(model(mac.here, SECOND, SECOND_REV))))
    assert refusal.detail == "second start: broken"


def test_a_runtime_that_never_gets_ready_is_stopped(mac: Machine) -> None:
    mac.runtime.never_ready = True
    supervisor = mac.supervisor(load_timeout_s=10.0)
    began = mac.clock.now
    refusal = refused(lambda: supervisor.chat(ask(model(mac.here))))
    assert (refusal.status, refusal.code) == (504, "load_timeout")
    assert refusal.message == "It did not finish loading within 10 seconds, so it was stopped."
    assert 10 <= mac.clock.now - began < 11
    assert not mac.spawner.children[0].running


def test_a_large_model_is_given_longer_to_load(
    mac: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Four gigabytes of weights, as the manifest lists them. The bytes are
    # not written (the check of sizes is stood in for); the wait is worked
    # out from the manifest alone.
    monkeypatch.setattr(server.files, "folder_problem", lambda _folder: None)
    large = model(mac.here, SECOND, SECOND_REV, listed={"model.safetensors": 4 * GB})
    mac.runtime.never_ready = True
    supervisor = mac.supervisor(load_timeout_s=10.0)

    began = mac.clock.now
    refusal = refused(lambda: supervisor.chat(ask(model(mac.here))))
    small_wait = mac.clock.now - began
    began = mac.clock.now
    refusal_large = refused(lambda: supervisor.chat(ask(large)))
    large_wait = mac.clock.now - began

    assert "within 10 seconds" in refusal.message
    assert 10 <= small_wait < 11
    # Ten seconds, and fifteen for each of its four gigabytes.
    assert "within 70 seconds" in refusal_large.message
    assert 70 <= large_wait < 71


def test_a_large_model_that_loads_slowly_is_not_cut_off(
    mac: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server.files, "folder_problem", lambda _folder: None)
    large = model(mac.here, listed={"model.safetensors": 4 * GB})
    mac.runtime.loading = 100  # fifty seconds of "still loading"
    answer = mac.supervisor(load_timeout_s=10.0).chat(ask(large))
    assert answer["framefound"]["load_seconds"] == 50.0


def test_a_runtime_that_hangs_is_killed_and_reported(mac: Machine) -> None:
    mac.spawner.queued.append(fake.FakeChild(stubborn=True))

    def hang(request: httpx.Request, _body: dict[str, Any]) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    mac.runtime.reply = hang
    supervisor = mac.supervisor()
    refusal = refused(lambda: supervisor.chat(ask(model(mac.here)), timeout_s=30))
    assert (refusal.status, refusal.code) == (504, "timeout")
    assert refusal.message == "It took longer than 30 seconds to answer, so it was stopped."
    child = mac.spawner.children[0]
    assert child.signals == ["terminate", "kill"] and not child.running
    assert supervisor.status()["state"] == "idle"


def test_a_runtime_that_dies_while_answering_is_reported(mac: Machine) -> None:
    mac.spawner.says = "mlx: [metal::malloc] Resource limit exceeded\n"

    def gone(request: httpx.Request, _body: dict[str, Any]) -> httpx.Response:
        mac.spawner.children[-1].die(-9)
        raise httpx.RemoteProtocolError("Server disconnected", request=request)

    mac.runtime.reply = gone
    supervisor = mac.supervisor()
    refusal = refused(lambda: supervisor.chat(ask(model(mac.here))))
    assert (refusal.status, refusal.code, refusal.message) == (502, "died", server.DIED)
    assert "Resource limit exceeded" in refusal.detail
    assert supervisor.status()["problem"] == server.DIED


def test_a_runtime_that_dies_after_loading_is_reported_as_a_sentence(mac: Machine) -> None:
    folder = model(mac.here)
    supervisor = mac.supervisor()
    supervisor.chat(ask(folder))
    mac.spawner.children[0].die(137)
    supervisor.tick()
    status = supervisor.status()
    assert status["state"] == "idle"
    assert status["problem"] == (
        "The model stopped by itself (exit 137) after it had loaded. It is loaded again the "
        "next time it is asked."
    )
    supervisor.chat(ask(folder))
    assert len(mac.spawner.spawned) == 2
    assert supervisor.status()["problem"] is None


def test_an_error_from_the_runtime_is_passed_on_and_the_model_stays_loaded(
    mac: Machine,
) -> None:
    mac.runtime.reply = lambda _request, _body: httpx.Response(500, text="boom: no such token")
    supervisor = mac.supervisor()
    refusal = refused(lambda: supervisor.chat(ask(model(mac.here))))
    assert (refusal.status, refusal.code) == (502, "runtime_error")
    assert refusal.message == "MLX-VLM could not answer (it said 500)."
    assert refusal.detail == "boom: no such token"
    assert mac.spawner.children[0].running
    assert supervisor.status()["state"] == "ready"


def test_a_runtime_that_outlives_its_kill_is_still_known_as_ours(mac: Machine) -> None:
    """Stuck reading a share, the runtime ignores even SIGKILL for a while and
    keeps the port. Its record is kept, so the next load ends it as
    FrameFound's own instead of refusing it as another program's."""
    stuck = fake.FakeChild(pid=5151, stubborn=True, survives_kill=True)
    mac.spawner.queued.append(stuck)

    # The port is held for as long as a child that was started is running.
    def held(_port: int) -> bool:
        return any(child.running for child in mac.spawner.children)

    version = mac.runtimes / "mlx-vlm" / "0.7.12"
    mac.runner.answer(
        "ps -o command=",
        (0, f"{version}/venv/bin/python -m mlx_vlm.server --host 127.0.0.1 --port 54806\n"),
    )
    mac.runner.answer("kill", lambda argv: (stuck.die(-9), (0, ""))[1])
    supervisor = mac.supervisor(port_open=held)
    folder = model(mac.here)
    supervisor.chat(ask(folder))

    assert supervisor.unload() is True
    assert stuck.running and stuck.signals == ["terminate", "kill"]
    assert supervisor.record_path.is_file()
    assert supervisor.status()["problem"] == server.STILL_RUNNING

    answer = supervisor.chat(ask(folder))

    assert ["kill", "-TERM", "5151"] in mac.runner.calls and not stuck.running
    assert answer["framefound"]["repo_id"] == FIRST
    assert len(mac.spawner.spawned) == 2
    assert supervisor.status()["problem"] is None


# --- the leftover on the port ---------------------------------------------------------------


def _leftover(mac: Machine, pid: int, command: str) -> None:
    version = mac.runtimes / "mlx-vlm" / "0.7.12"
    mac.layout.run.mkdir(parents=True, exist_ok=True)
    (mac.layout.run / "runtime.json").write_text(
        json.dumps({"pid": pid, "runtime": "mlx-vlm", "runtime_dir": str(version)})
    )
    mac.port.open = True
    mac.port.owner = pid
    mac.runner.answer("ps -o command=", (0, command.replace("<version>", str(version)) + "\n"))


def test_a_leftover_runtime_on_the_port_is_ended_and_a_strangers_is_not(mac: Machine) -> None:
    ours = "<version>/venv/bin/python -m mlx_vlm.server --host 127.0.0.1 --port 54806"
    _leftover(mac, 777, ours)
    answer = mac.supervisor().chat(ask(model(mac.here)))
    assert ["kill", "-TERM", "777"] in mac.runner.calls
    assert answer["framefound"]["repo_id"] == FIRST

    # Something else on the port: a program that is not ours, never touched.
    for command in ("/usr/local/bin/some-other-server --port 54806", ""):
        stranger = Machine(mac.root / f"stranger-{len(command)}")
        stranger.install("0.7.12")
        _leftover(stranger, 888, command)
        refusal = refused(lambda s=stranger: s.supervisor().chat(ask(model(s.here))))
        assert (refusal.status, refusal.code, refusal.message) == (
            409,
            "port_in_use",
            "Port 54806 is in use by another program.",
        )
        assert not [call for call in stranger.runner.calls if call[0] == "kill"]
        assert stranger.spawner.spawned == []

    # Nothing on record at all: not ours either.
    unknown = Machine(mac.root / "unknown")
    unknown.install("0.7.12")
    unknown.port.open = True
    refusal = refused(lambda: unknown.supervisor().chat(ask(model(unknown.here))))
    assert refusal.code == "port_in_use"
    assert unknown.runner.calls == []


def test_a_leftover_that_will_not_stop_is_killed(mac: Machine) -> None:
    _leftover(mac, 777, "<version>/venv/bin/python -m mlx_vlm.server --port 54806")

    def stubborn(argv: list[str]) -> tuple[int, str]:
        if argv[1] == "-KILL":
            mac.port.open = False
        return 0, ""

    mac.runner.answer("kill", stubborn)
    mac.supervisor().chat(ask(model(mac.here)))
    assert [call for call in mac.runner.calls if call[0] == "kill"] == [
        ["kill", "-TERM", "777"],
        ["kill", "-KILL", "777"],
    ]


def test_a_leftover_is_ended_when_the_server_starts(mac: Machine) -> None:
    _leftover(mac, 777, "<version>/venv/bin/python -m mlx_vlm.server --port 54806")
    supervisor = mac.supervisor()
    supervisor.start()
    assert ["kill", "-TERM", "777"] in mac.runner.calls
    assert supervisor.status()["problem"] is None
    assert not (mac.layout.run / "runtime.json").exists()

    stranger = Machine(mac.root / "stranger")
    stranger.port.open = True
    held = stranger.supervisor()
    held.start()
    assert held.status()["problem"] == "Port 54806 is in use by another program."


# --- what an answer carries -----------------------------------------------------------------


def test_the_answer_says_which_build_and_software_answered_and_what_it_cost(
    mac: Machine,
) -> None:
    mac.runtime.loading = 4
    mac.runtime.takes = 3.0
    answer = mac.supervisor().chat(ask(model(mac.here)))
    assert answer["choices"][0]["message"]["content"] == "A tree beside a lake."
    assert answer["framefound"] == {
        "repo_id": FIRST,
        "revision": FIRST_REV,
        "runtime": "mlx-vlm",
        "runtime_version": "0.7.12",
        "load_seconds": 2.0,
        "answer_seconds": 3.0,
        "memory_gb": 5.1,
        "memory_source": "runtime",
        "demo": False,
    }


def test_memory_the_runtime_does_not_report_is_read_from_its_process(mac: Machine) -> None:
    mac.runtime.timings = None
    supervisor = mac.supervisor()
    folder = model(mac.here)
    first = supervisor.chat(ask(folder))["framefound"]
    assert (first["memory_gb"], first["memory_source"]) == (2.0, "process")
    pid = mac.spawner.children[0].pid
    assert ["ps", "-o", "rss=", "-p", str(pid)] in mac.runner.calls

    mac.runner.answer("ps -o rss=", proc.CommandFailed("ps is not installed or not on PATH"))
    second = supervisor.chat(ask(folder))["framefound"]
    assert (second["memory_gb"], second["memory_source"]) == (None, None)


def test_status_says_what_is_loaded_and_when_it_unloads(mac: Machine) -> None:
    supervisor = mac.supervisor()
    assert supervisor.status()["state"] == "idle"
    folder = model(mac.here)
    supervisor.chat(ask(folder), idle_s=300)
    mac.clock.now += 100
    status = supervisor.status()
    assert status == {
        "state": "ready",
        "folder": str(folder.resolve()),
        "repo_id": FIRST,
        "revision": FIRST_REV,
        "runtime": "mlx-vlm",
        "runtime_version": "0.7.12",
        "memory_gb": 5.1,
        "loaded_for_s": 100.0,
        "idle_for_s": 100.0,
        "unloads_in_s": 200.0,
        "pid": mac.spawner.children[0].pid,
        "problem": None,
    }


def test_a_second_request_waits_its_turn_and_then_gives_up(mac: Machine) -> None:
    folder = model(mac.here)
    supervisor = mac.supervisor()
    entered, release = threading.Event(), threading.Event()

    def slow(_request: httpx.Request, _body: dict[str, Any]) -> httpx.Response:
        entered.set()
        release.wait(10)
        return httpx.Response(200, json=mac.runtime.answer("Done."))

    mac.runtime.reply = slow
    answers: list[dict[str, Any]] = []
    first = threading.Thread(target=lambda: answers.append(supervisor.chat(ask(folder))))
    first.start()
    try:
        assert entered.wait(10)
        assert supervisor.status()["state"] == "busy"
        supervisor.queue_wait_s = 0.05
        refusal = refused(lambda: supervisor.chat(ask(folder)))
        assert (refusal.status, refusal.code) == (503, "busy")
        assert "It will be asked again later." in refusal.message
        # The idle clock cannot stop a model that is answering, and the tick
        # does not wait for the answer to find that out.
        mac.clock.now += 10_000
        began = time.monotonic()
        supervisor.tick()
        assert time.monotonic() - began < 1.0
    finally:
        release.set()
        first.join(10)
    assert answers and answers[0]["choices"][0]["message"]["content"] == "Done."
    assert mac.spawner.children[0].running


def test_unloading_stops_the_model_and_says_whether_one_was_loaded(mac: Machine) -> None:
    supervisor = mac.supervisor()
    assert supervisor.unload() is False
    supervisor.chat(ask(model(mac.here)))
    assert supervisor.unload() is True
    assert not mac.spawner.children[0].running
    assert supervisor.status()["state"] == "idle"
    assert not supervisor.record_path.exists()


def test_closing_the_service_stops_the_runtime(mac: Machine) -> None:
    supervisor = mac.supervisor()
    supervisor.chat(ask(model(mac.here)))
    supervisor.close()
    assert mac.spawner.children[0].signals == ["terminate"]
    assert supervisor.status()["state"] == "idle"


async def test_the_service_ends_a_leftover_unloads_when_idle_and_stops_its_runtime(
    mac: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What only the app's lifespan does, run as uvicorn runs it: at start the
    leftover on the port is ended; while it runs a model idle for too long is
    unloaded by the ticking thread; at stop the runtime is stopped."""
    monkeypatch.setattr(server, "TICK_S", 0.01)
    _leftover(mac, 777, "<version>/venv/bin/python -m mlx_vlm.server --port 54806")
    folder = model(mac.here)
    supervisor = mac.supervisor()
    app = create_app(mac.settings(), supervisor)

    async with app.router.lifespan_context(app):
        assert ["kill", "-TERM", "777"] in mac.runner.calls
        supervisor.chat(ask(folder), idle_s=30)
        first = mac.spawner.children[0]
        mac.clock.now += 31
        for _ in range(500):
            if supervisor.status()["state"] == "idle":
                break
            await asyncio.sleep(0.01)
        assert not first.running and supervisor.status()["state"] == "idle"
        supervisor.chat(ask(folder))
        second = mac.spawner.children[1]
        assert second.running

    assert second.signals == ["terminate"] and not second.running
    assert supervisor.status()["state"] == "idle"


def test_a_tick_that_fails_is_logged_and_the_ticking_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "TICK_S", 0.001)
    stop = threading.Event()
    ticks: list[int] = []

    class Flaky:
        def tick(self) -> None:
            ticks.append(1)
            if len(ticks) == 1:
                raise RuntimeError("the stores file could not be read")
            stop.set()

    with capture_logs() as logged:
        server._tick_until(Flaky(), stop)  # type: ignore[arg-type]

    assert len(ticks) == 2
    assert [entry["event"] for entry in logged if entry["log_level"] == "warning"] == [
        "model_server.tick_failed"
    ]


def test_a_runtime_that_cannot_be_started_is_reported(mac: Machine) -> None:
    mac.spawner.fails = proc.CommandFailed(
        "/runtimes/mlx-vlm/0.7.12/venv/bin/python is not installed or not on PATH"
    )
    refusal = refused(lambda: mac.supervisor().chat(ask(model(mac.here))))
    assert (refusal.code, refusal.message) == ("load_failed", "It would not load.")
    assert "not installed" in refusal.detail


class Unservable(MlxVlm):
    """A kind that cannot say how to start this model, as a kind's own error."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def serve(
        self, folder: Path, model_dir: Path, variant: str, port: int, key: str, scratch: Path
    ) -> Serve:
        raise self.error


@pytest.mark.parametrize(
    "error",
    [
        # llama.cpp's `_inside`: the manifest names a file outside the model folder.
        ValueError("The model's list of files names ../outside.gguf, outside its folder."),
        # A file of the model gone between the check and the start.
        FileNotFoundError(2, "No such file or directory", "demo-vision-Q4_K_M.gguf"),
    ],
)
async def test_a_kind_that_cannot_start_a_model_is_refused_not_a_crash(
    mac: Machine, error: Exception
) -> None:
    supervisor = mac.supervisor(kinds={"mlx-vlm": Unservable(error)})
    app = create_app(mac.settings(), supervisor)
    async with app_client(app) as client:
        answer = await client.post(
            "/v1/chat/completions", json=ask(model(mac.here)), headers=token()
        )
    assert answer.status_code == 409, answer.text
    assert answer.json()["error"] == {
        "code": "load_failed",
        "message": server.WOULD_NOT_LOAD,
        "detail": str(error),
    }
    # Nothing was started or recorded, and the page is told why.
    assert mac.spawner.spawned == []
    assert not (mac.layout.run / server.RECORD_NAME).exists()
    status = supervisor.status()
    assert (status["state"], status["problem"]) == ("idle", server.WOULD_NOT_LOAD)


async def test_a_llama_cpp_version_whose_program_was_deleted_is_refused_until_it_is_reinstalled(
    mac: Machine,
) -> None:
    llama = LlamaCpp()
    runtimes.install(llama, "0.9.0", mac.runtimes, run=fake.FakeRunner(make_llama=True))
    runtimes.activate(mac.runtimes, "llama-cpp", "0.9.0")
    version = runtimes.version_dir(mac.runtimes, "llama-cpp", "0.9.0")
    folder = model(
        mac.here,
        "google/demo-vision-gguf",
        contents={"demo-vision-Q4_K_M.gguf": gguf.MAGIC + b"weights" * 64},
        runtime="llama-cpp",
    )

    def llama_cpp(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        key = mac.spawner.last.env["LLAMA_API_KEY"]
        if request.headers.get("authorization") != f"Bearer {key}":
            return httpx.Response(401)
        return httpx.Response(200, json=mac.runtime.answer("A tree beside a lake."))

    supervisor = mac.supervisor(
        kinds={"mlx-vlm": MlxVlm(), "llama-cpp": llama}, http=httpx.MockTransport(llama_cpp)
    )
    # Deleted by hand: the version is still recorded whole and in use.
    for program in (version / llama_release.DIST).rglob(llama_release.PROGRAM):
        program.unlink()
    gone = (
        "llama.cpp 0.9.0 is recorded but its program is missing. Install 0.9.0 again under "
        "Software that runs models (Install a particular version): that installs it afresh."
    )
    app = create_app(mac.settings(), supervisor)
    async with app_client(app) as client:
        answer = await client.post("/v1/chat/completions", json=ask(folder), headers=token())
        # The kind's own sentence, as every other refusal is: not a 500.
        assert answer.status_code == 409, answer.text
        assert answer.json()["error"] == {"code": "load_failed", "message": gone, "detail": ""}
        # Nothing was half started: no child, no record of one, nothing loaded.
        assert mac.spawner.spawned == []
        assert not (mac.layout.run / server.RECORD_NAME).exists()
        status = supervisor.status()
        assert (status["state"], status["folder"], status["problem"]) == ("idle", None, gone)

        # Installing it again, as the page does for a broken version: unmarked,
        # it is half an install, which the install removes and makes again.
        (version / runtimes.WHOLE).unlink()
        runtimes.install(llama, "0.9.0", mac.runtimes, run=fake.FakeRunner(make_llama=True))
        answer = await client.post("/v1/chat/completions", json=ask(folder), headers=token())
        assert answer.status_code == 200, answer.text
        assert answer.json()["framefound"]["runtime"] == "llama-cpp"
        assert mac.spawner.spawned[0].argv[0] == str(llama_release.program(version))
        assert supervisor.status()["problem"] is None


# --- the HTTP side ---------------------------------------------------------------------------


async def test_a_chat_over_http_is_answered_with_its_facts(mac: Machine) -> None:
    folder = model(mac.here)
    supervisor = mac.supervisor()
    app = create_app(mac.settings(), supervisor)
    async with app_client(app) as client:
        answer = await client.post(
            "/v1/chat/completions",
            json=ask(folder, picture(JPEG)),
            headers={**token(), server.IDLE_HEADER: "120", server.TIMEOUT_HEADER: "60"},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json()["framefound"]["runtime_version"] == "0.7.12"
        assert supervisor.status()["unloads_in_s"] == 120.0
        status = await client.get("/status", headers=token())
        assert status.json()["repo_id"] == FIRST
        assert (await client.post("/unload", headers=token())).json() == {"unloaded": True}
        assert (await client.post("/unload", headers=token())).json() == {"unloaded": False}


async def test_a_refusal_over_http_is_its_status_and_a_sentence(
    mac: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(mac.settings(), mac.supervisor())
    async with app_client(app) as client:
        answer = await client.post("/v1/chat/completions", json=ask(FIRST), headers=token())
        assert answer.status_code == 400
        assert answer.json() == {
            "error": {"code": "not_allowed", "message": server.NOT_OUR_FOLDER, "detail": ""}
        }
        for body in (b"{not json", b"[1, 2]"):
            answer = await client.post("/v1/chat/completions", content=body, headers=token())
            assert answer.status_code == 400
            assert answer.json()["error"]["message"] == server.NOT_JSON
        for header, value in (
            (server.IDLE_HEADER, "soon"),
            (server.IDLE_HEADER, "0"),
            (server.TIMEOUT_HEADER, "nan"),
            (server.TIMEOUT_HEADER, "99999"),
        ):
            answer = await client.post(
                "/v1/chat/completions",
                json=ask(model(mac.here)),
                headers={**token(), header: value},
            )
            assert answer.status_code == 400, (header, value)
            assert header in answer.json()["error"]["message"]
        monkeypatch.setattr(server, "MAX_BODY_BYTES", 200)
        answer = await client.post(
            "/v1/chat/completions", json=ask(model(mac.here), picture(JPEG)), headers=token()
        )
        assert answer.status_code == 413
        assert answer.json()["error"]["message"] == server.TOO_LARGE

        # Sent in pieces, with no length said up front: counted as it arrives.
        async def pieces() -> Any:
            body = json.dumps(ask(model(mac.here), picture(JPEG))).encode()
            for start in range(0, len(body), 50):
                yield body[start : start + 50]

        answer = await client.post("/v1/chat/completions", content=pieces(), headers=token())
        assert "content-length" not in answer.request.headers
        assert answer.status_code == 413
    assert mac.spawner.spawned == []


# --- what the module is -------------------------------------------------------------------


def test_the_module_imports_no_database_and_no_queue() -> None:
    forbidden = (
        "sqlalchemy",
        "asyncpg",
        "psycopg",
        "alembic",
        "celery",
        "kombu",
        "redis",
        "framefound.db",
        "framefound.processing",
        "framefound.celery_app",
        "framefound.api",
        "framefound.local_models.registry",
        "framefound.local_models.settings_store",
    )
    saved = dict(sys.modules)
    try:
        for name in list(sys.modules):
            if name.startswith(("framefound", *forbidden)):
                del sys.modules[name]
        importlib.import_module("framefound.local_models.server")
        imported = sorted(name for name in sys.modules if name.startswith(forbidden))
    finally:
        for name in set(sys.modules) - set(saved):
            del sys.modules[name]
        sys.modules.update(saved)
    assert imported == []
