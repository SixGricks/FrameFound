"""The llama.cpp kind (local_models/kinds.py `LlamaCpp`; L6 S4, design 3.3).

The kind is data — argv lists, an environment, a request body — so most of
these read what it returns. The rest drive it through the real machinery
with stand-ins: an install through the `runtime_install` job with
`fake.FakeRunner(make_llama=True)` in place of `python -m
…llama_release`, and the model server swapping between an MLX and a GGUF
model with `fake.FakeSpawner` and an `httpx.MockTransport` in place of the
programs. A fixture makes starting a process or reaching a real address
fail the test. What none of this can show is `llama-server` itself
accepting these flags on the Mac (design 12).
"""

import json
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.models import LocalAiJob
from framefound.local_models import (
    fake,
    files,
    gguf,
    handlers,
    jobs,
    kinds,
    llama_release,
    runtimes,
    server,
)
from framefound.local_models.deps import Deps
from framefound.local_models.hub import HubError, RepoFile, parse_info
from framefound.local_models.kinds import LlamaCpp, MlxVlm
from framefound.local_models.server import Refused, Supervisor
from framefound.native.layout import Layout
from framefound.storage import stores

Factory = async_sessionmaker[AsyncSession]

SECRET = "not-a-real-secret-key"
PORT = 54806
KEY = "a-key-for-this-start-only"
VISION = "gguf/demo-vision-gguf"
REVISION = "7b7b7b7b7b7b7b7b7b7b7b7b7b7b7b7b7b7b7b7b"

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


def installed(root: Path, version: str = "0.9.0") -> Path:
    """llama.cpp `version` installed and in use, as the page's Install leaves it."""
    runtimes.install(LlamaCpp(), version, root, run=fake.FakeRunner(make_llama=True))
    runtimes.activate(root, "llama-cpp", version)
    return runtimes.version_dir(root, "llama-cpp", version)


def gguf_model(
    root: Path, *, projector: bool = True, repo: str = "google/demo-vision-gguf"
) -> Path:
    """A downloaded GGUF build as the library writes one: its files, then its manifest."""
    folder = root.joinpath(*repo.split("/"), REVISION, "Q4_K_M")
    contents = {"demo-vision-Q4_K_M.gguf": gguf.MAGIC + b"weights" * 64, "README.md": b"# Demo"}
    if projector:
        contents["mmproj-demo-vision-F16.gguf"] = gguf.MAGIC + b"pictures" * 16
    for path, body in contents.items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(body)
    files.write_manifest(
        folder,
        files.Manifest(
            repo_id=repo,
            revision=REVISION,
            variant="Q4_K_M",
            runtime="llama-cpp",
            files=tuple(RepoFile(path, len(body), None, None) for path, body in contents.items()),
            completed_at="2026-11-12T18:39:10Z",
        ),
    )
    return folder


def started(tmp_path: Path, *, projector: bool = True) -> tuple[kinds.Serve, Path]:
    runtime = installed(tmp_path / "runtimes")
    model = gguf_model(tmp_path / "models", projector=projector)
    serve = LlamaCpp().serve(runtime, model, "Q4_K_M", PORT, KEY, tmp_path / "scratch")
    return serve, model


# --- how it is started --------------------------------------------------------------------


def test_llama_cpp_is_started_on_loopback_with_one_model_file(tmp_path: Path) -> None:
    serve, model = started(tmp_path)
    argv = serve.argv

    program = llama_release.program(
        runtimes.version_dir(tmp_path / "runtimes", "llama-cpp", "0.9.0")
    )
    assert program is not None and argv[0] == str(program)
    assert Path(argv[0]).name == "llama-server"
    # One file, by its absolute path: without `-m` it would be a router that
    # loads whatever model a request names.
    assert argv[argv.index("-m") + 1] == str(model / "demo-vision-Q4_K_M.gguf")
    assert Path(argv[argv.index("-m") + 1]).is_absolute()
    assert argv.count("-m") == 1
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == str(PORT)
    assert argv[argv.index("--alias") + 1] == kinds.ALIAS
    assert argv[argv.index("-np") + 1] == "1"
    assert argv[argv.index("--reasoning-budget") + 1] == "0"
    for flag in ("--offline", "--no-webui", "--no-slots"):
        assert flag in argv
    assert serve.cwd == tmp_path / "scratch"


def test_the_context_is_always_given_never_left_to_the_model(tmp_path: Path) -> None:
    # Left alone it reserves memory for 262,144 tokens or more (L6 P1).
    for projector in (True, False):
        argv = started(tmp_path / str(projector), projector=projector)[0].argv
        assert argv[argv.index("-c") + 1] == "16384" == str(kinds.CONTEXT_TOKENS)


def test_a_build_with_a_picture_reader_is_started_with_it_and_one_without_says_so(
    tmp_path: Path,
) -> None:
    with_pictures, model = started(tmp_path / "with")
    argv = with_pictures.argv
    assert argv[argv.index("--mmproj") + 1] == str(model / "mmproj-demo-vision-F16.gguf")
    assert "--no-mmproj" not in argv

    text_only = started(tmp_path / "without", projector=False)[0].argv
    assert "--no-mmproj" in text_only and "--mmproj" not in text_only


def test_the_key_travels_in_the_environment_never_on_the_command_line(tmp_path: Path) -> None:
    serve, _model = started(tmp_path)
    assert serve.env["LLAMA_API_KEY"] == KEY
    assert not any(KEY in word for word in serve.argv)
    assert LlamaCpp().auth_headers(KEY) == {"Authorization": f"Bearer {KEY}"}


def test_no_flag_that_downloads_or_reads_other_folders_is_ever_passed(tmp_path: Path) -> None:
    for projector in (True, False):
        argv = started(tmp_path / str(projector), projector=projector)[0].argv
        for flag in (*kinds.NEVER_PASSED, "--reasoning-format", "--jinja-template"):
            assert flag not in argv, flag
        assert not any(word.startswith(("http://", "https://")) for word in argv)


def test_the_child_gets_no_llama_arg_variable_and_none_of_framefounds_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model server runs with framefound.env loaded, and llama.cpp reads a
    # LLAMA_ARG_* variable for almost every flag (L6 P5).
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", SECRET)
    monkeypatch.setenv("POSTGRES_PASSWORD", "not-a-real-password")
    monkeypatch.setenv("LLAMA_ARG_MODEL_URL", "https://example.com/other.gguf")
    monkeypatch.setenv("LLAMA_ARG_HOST", "0.0.0.0")  # noqa: S104 - an inherited value that must not pass
    monkeypatch.setenv("HF_TOKEN", "hf_not_a_real_token")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    serve, _model = started(tmp_path)

    assert not any(name.startswith("LLAMA_ARG_") for name in serve.env)
    assert not any(name.startswith("FRAMEFOUND_") for name in serve.env)
    assert set(serve.env) <= {
        *kinds.PASSED_ON,
        "LLAMA_API_KEY",
        "HF_HUB_OFFLINE",
        "HF_HOME",
        "LLAMA_CACHE",
    }
    assert serve.env["PATH"] == "/usr/bin:/bin"
    assert serve.env["HF_HUB_OFFLINE"] == "1"
    assert serve.env["HF_HOME"] == str(tmp_path / "scratch" / "hf-home")


def test_a_version_whose_program_is_gone_is_not_started_and_says_so(tmp_path: Path) -> None:
    runtime = installed(tmp_path / "runtimes")
    for program in (runtime / llama_release.DIST).rglob(llama_release.PROGRAM):
        program.unlink()
    model = gguf_model(tmp_path / "models")
    with pytest.raises(runtimes.RuntimeProblem) as caught:
        LlamaCpp().serve(runtime, model, "Q4_K_M", PORT, KEY, tmp_path / "scratch")
    assert str(caught.value) == (
        "llama.cpp 0.9.0 is recorded but its program is missing. Install 0.9.0 again under "
        "Software that runs models (Install a particular version): that installs it afresh."
    )
    assert LlamaCpp().program(runtime) is None

    # A folder whose manifest is gone does not say which file is the model.
    (model / files.MANIFEST).unlink()
    with pytest.raises(runtimes.RuntimeProblem):
        LlamaCpp().serve(installed(tmp_path / "again"), model, "Q4_K_M", PORT, KEY, tmp_path)


def test_a_chat_names_the_alias_and_a_picture_is_always_data(tmp_path: Path) -> None:
    kind = LlamaCpp()
    body = {"model": "/somewhere/else", "messages": [], "stream": True, "max_tokens": 40}
    sent = kind.request(body, tmp_path / "model")
    assert sent["model"] == kinds.ALIAS and sent["stream"] is False
    assert sent["max_tokens"] == 40
    assert body["model"] == "/somewhere/else"  # the caller's body is not changed
    assert kind.picture_part(b"\xff\xd8") == MlxVlm().picture_part(b"\xff\xd8")
    assert kind.picture_part(b"\xff\xd8")["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # It reports no memory figure: the model server measures the process.
    assert kind.memory_gb({"timings": {"peak_memory": 5.1}}) is None


def test_ready_means_two_hundred_and_ok_not_loading() -> None:
    kind = LlamaCpp()
    assert kind.is_ready(200, {"status": "ok"})
    loading = {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}}
    assert not kind.is_ready(503, loading)
    assert not kind.is_ready(200, {"status": "loading"})
    assert not kind.is_ready(200, {"status": "healthy"})  # MLX-VLM's word, not this program's


def test_only_its_own_started_program_is_its_own(tmp_path: Path) -> None:
    kind, folder = LlamaCpp(), tmp_path / "llama-cpp" / "0.9.0"
    assert kind.owns(f"{folder}/dist/llama-b12110/llama-server -m /x.gguf", folder)
    assert not kind.owns("/opt/homebrew/bin/llama-server -m /x.gguf", folder)
    assert not kind.owns(f"{folder}/venv/bin/python -m mlx_vlm.server", folder)


# --- how it is installed and which versions it knows ------------------------------------


def test_it_is_installed_by_framefounds_own_python_not_by_uv(tmp_path: Path) -> None:
    kind, folder = LlamaCpp(), tmp_path / "0.9.0"
    assert kind.needs_uv is False and MlxVlm.needs_uv is True
    module = "framefound.local_models.llama_release"
    assert kind.install_steps(folder, "0.9.0", "/opt/homebrew/bin/uv") == [
        [sys.executable, "-m", module, "fetch", "--version", "0.9.0", "--into", str(folder)]
    ]
    assert kind.check_step(folder) == [sys.executable, "-m", module, "check", "--in", str(folder)]
    assert kind.freeze_step(folder, "uv") == [
        sys.executable,
        "-m",
        module,
        "about",
        "--in",
        str(folder),
    ]
    # The program the steps run is a module that exists, by the name given.
    assert kind.module == llama_release.__name__


def test_a_version_is_a_number_or_a_daily_build_and_nothing_else() -> None:
    kind = LlamaCpp()
    assert kind.clean_version(" v0.9.0 ") == "0.9.0"
    assert kind.clean_version("0.10") == "0.10"
    assert kind.clean_version("B12318") == "b12318"
    for wrong in ("latest", "0.9.0; rm -rf /", "--into=/", "b12", "12318", "../0.9.0", ""):
        with pytest.raises(ValueError, match="not a llama.cpp version"):
            kind.clean_version(wrong)
    # The page's "a particular version" goes through the same rule.
    with pytest.raises(ValueError):
        runtimes.clean_version(kind, "mlx-vlm==0.7.12")


def test_a_daily_build_is_never_offered_an_update_to_a_numbered_one() -> None:
    kind = LlamaCpp()
    assert kind.is_newer("0.10.0", "0.9.0")
    assert not kind.is_newer("0.9.0", "0.9.0")
    assert not kind.is_newer("0.9.0", "0.10.0")
    # Counted differently: neither is older or newer (L6 D14).
    assert not kind.is_newer("0.10.0", "b12318")
    assert not kind.is_newer("b12400", "0.9.0")
    assert not kind.is_newer("b12400", "b12318")


def test_the_newest_version_is_githubs_latest_and_a_bad_answer_is_said_in_its_words() -> None:
    kind = LlamaCpp()
    asked: list[str] = []

    def github(url: str) -> dict[str, Any]:
        asked.append(url)
        return {"tag_name": "v0.9.0", "prerelease": False}

    assert kind.latest(github) == "0.9.0"
    assert asked == [f"{llama_release.API}/releases/latest"]

    with pytest.raises(HubError) as caught:
        kind.latest(lambda _url: {"tag_name": "b12318", "prerelease": True})
    assert caught.value.problem == "bad_answer"
    assert str(caught.value) == llama_release.NEWEST_UNREADABLE

    def unreachable(_url: str) -> dict[str, Any]:
        raise HubError("unreachable", "api.github.com could not be reached.")

    with pytest.raises(HubError) as caught:
        kind.latest(unreachable)
    assert (caught.value.problem, str(caught.value)) == (
        "unreachable",
        "llama.cpp's download page could not be reached, so the newest version is not known.",
    )

    def limited(_url: str) -> dict[str, Any]:
        raise HubError("unreachable", "api.github.com did not answer (403).", 403)

    with pytest.raises(HubError) as caught:
        kind.latest(limited)
    assert (caught.value.problem, str(caught.value)) == ("rate_limited", llama_release.LIMITED)


def test_it_runs_on_apple_silicon_only_and_says_so_in_its_own_words() -> None:
    kind = LlamaCpp()
    assert kind.supported_here("Darwin", "arm64") is None
    for system, machine in (("Darwin", "x86_64"), ("Linux", "aarch64"), ("Windows", "AMD64")):
        assert kind.supported_here(system, machine) == (
            "llama.cpp is installed here from its build for Apple-silicon Macs."
        )


# --- which repositories it runs --------------------------------------------------------


def _repo(name: str, **change: Any) -> tuple[Any, files.FilePlan]:
    payload = fake.fixture(name)
    payload.update(change)
    info = parse_info(payload)
    return info, files.plan_files(info.files)


def test_a_gguf_repository_is_given_to_llama_cpp_and_an_mlx_one_to_mlx_vlm(
    tmp_path: Path,
) -> None:
    here = kinds.kinds(Settings(runtimes_dir=tmp_path))

    kind, support = kinds.choose(*_repo(VISION), here)
    assert kind is not None and kind.name == "llama-cpp"
    # The room it will be given is said beside what its page says it can take
    # (P1: it is always started with CONTEXT_TOKENS, never the page's figure).
    assert support.ok and support.note == (
        "It is a GGUF build; llama.cpp runs those. It will be given room for 16,384 tokens; "
        "its page says it can take 131,072."
    )
    assert support.takes == ("text", "image")
    assert [variant.id for variant in support.variants] == ["Q4_K_M", "Q8_0", "F16"]
    suggested = here["llama-cpp"].prefer([(variant, "day") for variant in support.variants])
    assert suggested == "Q4_K_M"

    kind, support = kinds.choose(*_repo("gemma-4-e4b-it-4bit"), here)
    assert kind is not None and kind.name == "mlx-vlm" and support.ok
    assert LlamaCpp().supports(*_repo("gemma-4-e4b-it-4bit")).note == "This is not a GGUF build."


def test_the_sentence_shown_when_nothing_runs_it_is_the_matching_kinds(tmp_path: Path) -> None:
    here = kinds.kinds(Settings(runtimes_dir=tmp_path))

    kind, support = kinds.choose(*_repo("gguf/demo-projector-only"), here)
    assert kind is None and not support.ok
    # llama.cpp's sentence, not MLX-VLM's "cannot run yet", although MLX-VLM is asked first.
    assert support.note == "This repository holds only the part that reads pictures, not a model."

    info, plan = _repo(VISION)
    texts = files.plan_files([file for file in info.files if not file.path.endswith(".gguf")])
    assert LlamaCpp().supports(info, texts).note == "This is not a GGUF build."
    # GGUF files, but none that is a build (the same name in two folders).
    clash = files.plan_files(
        [RepoFile("a/demo.gguf", 10, "a" * 64, None), RepoFile("b/demo.gguf", 10, "b" * 64, None)]
    )
    kind, support = kinds.choose(info, clash, here)
    assert kind is None and support.note == "No GGUF model file was found in it."

    needs_code, plan = _repo(VISION, tags=["gguf", "custom_code"])
    refused = LlamaCpp().supports(needs_code, plan)
    assert not refused.ok and refused.note == files.NEEDS_CODE
    # Its sizes are still shown beside what is wrong with it.
    assert len(refused.variants) == 3


def test_a_gguf_build_without_a_picture_reader_takes_text_only() -> None:
    info, plan = _repo(VISION)
    plan = files.plan_files([file for file in info.files if "mmproj" not in file.path])
    support = LlamaCpp().supports(info, plan)
    assert support.ok and support.takes == ("text",)
    assert all(variant.takes == ("text",) for variant in support.variants)


# --- installed through the job, end to end ----------------------------------------------


@pytest.fixture()
async def factory(tmp_path: Path) -> AsyncIterator[Factory]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _deps(tmp_path: Path, run: fake.FakeRunner, **more: Any) -> Deps:
    values: dict[str, Any] = {
        "settings": Settings(
            data_dir=tmp_path / "data",
            models_dir=tmp_path / "models",
            runtimes_dir=tmp_path / "runtimes",
            secret_key=SECRET,
            min_free_gb=0,
        ),
        "hub_for": fake.FakeHub().with_token,
        "fetch_json": lambda _url: {"tag_name": "v0.9.0", "prerelease": False},
        "server": fake.FakeModelServer(sleep=lambda _seconds: None),
        "kinds": {"mlx-vlm": MlxVlm(), "llama-cpp": LlamaCpp()},
        "run": run,
        "uv": "uv",
        "sleep": lambda _seconds: None,
        "now": lambda: datetime(2026, 11, 12, 12, 0),
        "system": "Darwin",
        "machine": "arm64",
    }
    values.update(more)
    return Deps(**values)


async def _install(factory: Factory, deps: Deps, version: str | None = None) -> LocalAiJob:
    payload: dict[str, Any] = {"name": "llama-cpp"}
    if version is not None:
        payload["version"] = version
    async with factory() as db:
        job = await jobs.create(db, "runtime_install", subject="llama.cpp", payload=payload)
        await db.commit()
    while await jobs.run_next(factory, deps):
        pass
    async with factory() as db:
        done = await db.get(LocalAiJob, job.id)
        assert done is not None
        return done


async def test_installing_llama_cpp_through_the_job_needs_no_uv_and_is_recorded(
    factory: Factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No uv on this Mac: MLX-VLM could not be installed, llama.cpp can.
    monkeypatch.setattr(handlers, "_which", lambda _name: None)
    for folder in ("data", "models", "runtimes"):
        (tmp_path / folder).mkdir()
    run = fake.FakeRunner(make_llama=True)
    deps = _deps(tmp_path, run)
    root = tmp_path / "runtimes"

    # No version given: the newest numbered one GitHub names.
    done = await _install(factory, deps)
    assert (done.state, done.result) == ("done", {"version": "0.9.0"})
    folder = runtimes.version_dir(root, "llama-cpp", "0.9.0")
    assert runtimes.describe(root, "llama-cpp").current == "0.9.0"
    assert LlamaCpp().about(folder) == "build b12110"
    assert LlamaCpp().program(folder) is not None
    whole = json.loads((folder / runtimes.WHOLE).read_text(encoding="utf-8"))
    assert whole["freeze"].startswith("build b12110\n")
    # Three steps, each FrameFound's own Python, each with the bare environment.
    steps = [ran.argv[3] for ran in run.ran]
    assert steps == ["fetch", "check", "about"]
    assert all(ran.argv[0] == sys.executable for ran in run.ran)
    assert all(
        ran.env is not None and not any(name.startswith("FRAMEFOUND_") for name in ran.env)
        for ran in run.ran
    )
    assert deps.server.unloads == 1  # type: ignore[attr-defined]

    # A daily build by its number; the numbered one is kept to go back to.
    done = await _install(factory, deps, "B12318")
    assert (done.state, done.result) == ("done", {"version": "b12318"})
    state = runtimes.describe(root, "llama-cpp")
    assert (state.current, state.previous) == ("b12318", "0.9.0")
    assert LlamaCpp().about(runtimes.version_dir(root, "llama-cpp", "b12318")) == ""
    assert runtimes.rollback(root, "llama-cpp") == "0.9.0"


async def test_a_fetch_that_fails_leaves_no_folder_and_the_version_in_use_alone(
    factory: Factory, tmp_path: Path
) -> None:
    for folder in ("data", "models", "runtimes"):
        (tmp_path / folder).mkdir()
    root = tmp_path / "runtimes"
    installed(root)
    broken = fake.FakeRunner(
        {"llama_release fetch": (1, "Asking GitHub about 0.10.0…\n", llama_release.NO_CHECKSUM)},
        make_llama=True,
    )
    done = await _install(factory, _deps(tmp_path, broken), "0.10.0")

    assert done.state == "failed"
    assert done.message is not None and runtimes.UNCHANGED in done.message
    assert done.message.startswith("llama.cpp 0.10.0 could not be installed")
    assert done.detail is not None and llama_release.NO_CHECKSUM in done.detail
    assert not runtimes.version_dir(root, "llama-cpp", "0.10.0").exists()
    assert runtimes.describe(root, "llama-cpp").current == "0.9.0"

    wrong = await _install(factory, _deps(tmp_path, broken), "latest")
    assert wrong.state == "failed" and wrong.message == llama_release.NOT_A_VERSION


async def test_an_intel_mac_is_told_why_llama_cpp_cannot_be_installed(
    factory: Factory, tmp_path: Path
) -> None:
    run = fake.FakeRunner(make_llama=True)
    done = await _install(factory, _deps(tmp_path, run, machine="x86_64"), "0.9.0")
    assert done.state == "failed"
    assert done.message == "llama.cpp is installed here from its build for Apple-silicon Macs."
    assert run.ran == []


# --- the model server, swapping between the two programs --------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Programs:
    """MLX-VLM's and llama.cpp's HTTP APIs, each with its own key and its own word for ready."""

    def __init__(self, spawner: fake.FakeSpawner) -> None:
        self.spawner = spawner
        self.models: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        env = self.spawner.last.env
        llama = "LLAMA_API_KEY" in env
        key = env["LLAMA_API_KEY"] if llama else env.get("MLX_VLM_SERVER_API_KEY")
        if request.url.path == "/health":
            # llama.cpp's health asks for no key; MLX-VLM's does.
            if not llama and request.headers.get("authorization") != f"Bearer {key}":
                return httpx.Response(401)
            return httpx.Response(200, json={"status": "ok" if llama else "healthy"})
        if request.headers.get("authorization") != f"Bearer {key}":
            return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
        body = json.loads(request.content)
        self.models.append(body["model"])
        message = {"role": "assistant", "content": "A tree beside a lake."}
        return httpx.Response(
            200,
            json={"choices": [{"index": 0, "message": message, "finish_reason": "stop"}]},
        )


def _mlx_model(root: Path) -> Path:
    folder = root / "mlx-community" / "gemma-4-e4b-it-4bit" / REVISION
    contents = {"config.json": b'{"model_type": "gemma4"}', "model.safetensors": b"w" * 64}
    for path, body in contents.items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(body)
    files.write_manifest(
        folder,
        files.Manifest(
            repo_id="mlx-community/gemma-4-e4b-it-4bit",
            revision=REVISION,
            variant="",
            runtime="mlx-vlm",
            files=tuple(RepoFile(path, len(body), None, None) for path, body in contents.items()),
            completed_at="2026-11-12T18:39:10Z",
        ),
    )
    return folder


def _supervisor(tmp_path: Path) -> tuple[Supervisor, fake.FakeSpawner, Programs]:
    settings = Settings(
        data_dir=tmp_path / "data",
        models_dir=tmp_path / "models",
        runtimes_dir=tmp_path / "runtimes",
        secret_key=SECRET,
        min_free_gb=0,
    )
    for folder in (settings.data_dir, settings.models_dir):
        folder.mkdir(parents=True, exist_ok=True)
    spawner = fake.FakeSpawner()
    programs = Programs(spawner)
    clock = Clock()
    supervisor = Supervisor(
        settings,
        kinds={"mlx-vlm": MlxVlm(), "llama-cpp": LlamaCpp()},
        spawn=spawner,
        http=httpx.MockTransport(programs.handler),
        run=fake.FakeRunner(
            {"ps -o rss=": (0, "2097152\n"), "ps -o command=": (1, ""), "kill": (0, "")}
        ),
        port_open=lambda _port: False,
        clock=clock,
        sleep=clock.sleep,
        load_timeout_s=60.0,
        layout=Layout(home=tmp_path / "home", source=tmp_path),
    )
    return supervisor, spawner, programs


def _ask(folder: Path) -> dict[str, Any]:
    return {
        "model": str(folder),
        "messages": [{"role": "user", "content": "What is in this picture?"}],
        "max_tokens": 40,
    }


def test_swapping_from_an_mlx_model_to_a_gguf_one_stops_the_first_program(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtimes"
    runtimes.install(
        MlxVlm(),
        "0.7.12",
        root,
        run=fake.FakeRunner(
            {
                "uv": (0, ""),
                "python": lambda argv: (0, f"{Path(argv[0]).parents[2].name}\n"),
            },
            make_venvs=True,
        ),
    )
    runtimes.activate(root, "mlx-vlm", "0.7.12")
    llama = installed(root)
    supervisor, spawner, programs = _supervisor(tmp_path)
    here = tmp_path / "models" / "local-ai"
    mlx, gguf_folder = _mlx_model(here), gguf_model(here)

    first = supervisor.chat(_ask(mlx))["framefound"]
    assert first["runtime"] == "mlx-vlm"
    assert "mlx_vlm.server" in spawner.spawned[0].argv

    second = supervisor.chat(_ask(gguf_folder))["framefound"]
    assert (second["runtime"], second["runtime_version"]) == ("llama-cpp", "0.9.0")
    # One model at a time: the MLX program was stopped before llama.cpp started.
    assert spawner.children[0].signals[:1] == ["terminate"] and not spawner.children[0].running
    start = spawner.spawned[1]
    assert start.argv[0] == str(llama_release.program(llama))
    assert start.argv[start.argv.index("-m") + 1] == str(gguf_folder / "demo-vision-Q4_K_M.gguf")
    assert "MLX_VLM_SERVER_API_KEY" not in start.env
    # Its chats name the alias, never the folder; memory is measured from the process.
    assert programs.models[-1] == kinds.ALIAS
    assert second["memory_source"] == "process"

    third = supervisor.chat(_ask(mlx))["framefound"]
    assert third["runtime"] == "mlx-vlm"
    assert not spawner.children[1].running and len(spawner.spawned) == 3


def test_the_refusal_names_llama_cpp_when_that_is_what_is_missing(tmp_path: Path) -> None:
    (tmp_path / "runtimes").mkdir()
    supervisor, spawner, _programs = _supervisor(tmp_path)
    folder = gguf_model(tmp_path / "models" / "local-ai")
    with pytest.raises(Refused) as caught:
        supervisor.chat(_ask(folder))
    assert (caught.value.code, caught.value.message) == (
        "no_runtime",
        "llama.cpp is not installed.",
    )
    assert spawner.spawned == []
    # Nothing was started, so no start was recorded either.
    assert not (tmp_path / "home" / "run" / server.RECORD_NAME).exists()


# --- the demo's stand-ins (L6 D15, P21) -------------------------------------------


def test_the_demo_has_a_stand_in_for_each_program_and_a_real_install_has_none(
    tmp_path: Path,
) -> None:
    in_the_demo = kinds.kinds(Settings(local_ai_demo=True))
    # In the real kinds' order, so a repository goes to the same program as on the Mac.
    assert list(in_the_demo) == ["demo", "demo-gguf"]
    assert isinstance(in_the_demo["demo-gguf"], fake.DemoGgufKind)
    assert all(kind.demo for kind in in_the_demo.values())
    # Exactly these are the demo's: a model on one was made by the demo.
    assert set(in_the_demo) == kinds.DEMO_RUNTIMES
    assert all(kinds.is_demo_runtime(name) for name in in_the_demo)

    # A GGUF repository goes to the GGUF stand-in, an MLX one to the MLX stand-in.
    kind, support = kinds.choose(*_repo(VISION), in_the_demo)
    assert kind is not None and kind.name == "demo-gguf" and support.ok
    kind, support = kinds.choose(*_repo("gemma-4-e4b-it-4bit"), in_the_demo)
    assert kind is not None and kind.name == "demo" and support.ok

    # One stray variable never makes a real install show a stand-in.
    for real in (
        Settings(runtimes_dir=tmp_path),
        Settings(local_ai_demo=True, runtimes_dir=tmp_path),
        Settings(local_ai_demo=True, domain="media.example.com"),
        Settings(),
    ):
        made = kinds.kinds(real)
        assert not any(kinds.is_demo_runtime(name) for name in made)
        assert not any(kind.demo for kind in made.values())
