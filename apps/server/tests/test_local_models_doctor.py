"""`framefound doctor` and local AI (native/doctor.py `check_local_models`; L1 S13, 7.4).

Doctor is what the owner runs when the AI models page says something is
wrong and he wants to know more. These tests build an install's folders in
a scratch directory and stand in for the three things doctor asks the Mac
(`sysctl`, `lsof` and the ports): nothing here starts a program or opens a
socket. What they cannot show is those commands answering on a real Mac.
"""

import asyncio
import json
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from framefound.config import Settings, get_settings
from framefound.db.base import Base
from framefound.db.models import LocalAiRole, LocalModel
from framefound.local_models import fake, files, runtimes, server, settings_store
from framefound.local_models.hub import RepoFile
from framefound.local_models.kinds import LlamaCpp
from framefound.native import doctor, envfile
from framefound.native.layout import Layout
from framefound.ops.worker_check import Check
from framefound.storage import stores

REPO = Path(__file__).resolve().parents[3]
GB = 1024**3
THIRTY_TWO_GB = f"{32 * GB}\n"
LOOKER = "mlx-community/gemma-4-e4b-it-4bit"
EDITOR = "mlx-community/gemma-4-12B-it-OptiQ-4bit"
REVISION = "475b9088d29754a3379866cf5aeb6b41acd313c2"
APPLE = ("Darwin", "arm64")


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A program started or a socket opened by doctor's own code fails the test."""

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a test tried to start a process or open a socket")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr("socket.create_connection", refuse)
    for name in ("FRAMEFOUND_RUNTIMES_DIR", "FRAMEFOUND_BULK_DIR", "FRAMEFOUND_DOMAIN"):
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


@pytest.fixture()
def settings(layout: Layout) -> Settings:
    for folder in (layout.data, layout.models, layout.runtimes, layout.run):
        folder.mkdir(parents=True, exist_ok=True)
    url = f"sqlite+aiosqlite:///{(layout.home / 'catalogue.db').as_posix()}"
    asyncio.run(_schema(url))
    return Settings(
        data_dir=layout.data,
        models_dir=layout.models,
        runtimes_dir=layout.runtimes,
        database_url=url,
    )


async def _schema(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


class Mac:
    """What doctor asks the machine, answered from attributes a test sets."""

    def __init__(self) -> None:
        self.memsize: str | None = THIRTY_TWO_GB
        self.listening: set[int] = {54805}
        self.lsof = ""
        self.uv: str | None = "/opt/homebrew/bin/uv"
        self.asked: list[list[str]] = []

    def run(self, argv: list[str], **_kwargs: Any) -> "subprocess.CompletedProcess[str]":
        self.asked.append([str(word) for word in argv])
        if argv[0] == "sysctl":
            if self.memsize is None:
                return subprocess.CompletedProcess(argv, 1, "", "unknown oid")
            return subprocess.CompletedProcess(argv, 0, self.memsize, "")
        if argv[0] == "lsof":
            return subprocess.CompletedProcess(argv, 0 if self.lsof else 1, self.lsof, "")
        raise AssertionError(f"doctor ran something unexpected: {argv}")

    def which(self, name: str, path: str | None = None) -> str | None:
        assert name == "uv"
        # The services' PATH, with Homebrew on it: not the Terminal's.
        assert path is not None and "/opt/homebrew/bin" in path.split(":")
        return self.uv

    def port_open(self, port: int) -> bool:
        return port in self.listening


@pytest.fixture()
def mac() -> Mac:
    return Mac()


def _doctor(
    layout: Layout,
    env: dict[str, str],
    settings: Settings,
    mac: Mac,
    machine: tuple[str, str] = APPLE,
) -> dict[str, Check]:
    found = doctor.check_local_models(
        layout,
        env,
        settings,
        run=mac.run,
        which=mac.which,
        port_open=mac.port_open,
        machine=machine,
    )
    assert all(not check.fatal for check in found), "local AI is optional: nothing is FAIL"
    return {check.name: check for check in found}


def _install(root: Path, version: str, *, program: bool = True) -> Path:
    """A version of MLX-VLM as an install leaves it: its environment, then the mark."""
    folder = runtimes.version_dir(root, "mlx-vlm", version)
    (folder / "venv" / "bin").mkdir(parents=True)
    if program:
        (folder / "venv" / "bin" / "python").write_bytes(b"")
    (folder / runtimes.WHOLE).write_text(
        json.dumps({"installed_at": "2026-11-12T18:39:10Z"}), encoding="utf-8"
    )
    runtimes.activate(root, "mlx-vlm", version)
    return folder


# --- nothing set up: nothing said ------------------------------------------------------


def test_an_install_without_local_ai_says_exactly_what_it_said_before(
    layout: Layout, env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("doctor looked at local AI on an install that has none")

    plain = Settings(data_dir=tmp_path)  # the containers, and a Mac install not yet updated
    assert (
        doctor.check_local_models(
            layout, env, plain, run=refuse, which=refuse, port_open=refuse, machine=APPLE
        )
        == []
    )

    # The whole report: every other check stood in for, so what `run` adds is all there is.
    before = [Check("api", True, "API"), Check("backup", True, "newest is 2 h old", False)]
    for name in ("check_front_door", "check_stores", "check_onnx", "check_power"):
        monkeypatch.setattr(doctor, name, lambda *_a, **_k: [])
    monkeypatch.setattr(doctor, "check_agents", lambda *_a: before[:1])
    monkeypatch.setattr(doctor, "check_backups", lambda *_a: before[1:])
    monkeypatch.setattr(doctor, "check_tools", lambda: [])

    async def no_services(*_a: Any, **_k: Any) -> list[Check]:
        return []

    monkeypatch.setattr(doctor, "check_services", no_services)
    assert doctor.run(layout, env, plain, launchd=None) == before  # type: ignore[arg-type]


def test_this_macs_memory_is_read_as_sysctl_writes_it() -> None:
    assert doctor.parse_memsize(THIRTY_TWO_GB) == 32 * GB
    assert doctor.parse_memsize(f"hw.memsize: {16 * GB}") == 16 * GB
    for nonsense in ("", "0", "unknown oid 'hw.memsize'", "32 GB", "-1"):
        assert doctor.parse_memsize(nonsense) is None, nonsense


def test_the_program_holding_a_port_is_read_from_lsof() -> None:
    assert doctor.parse_listeners("p812\ncllama-server\nf7\np90\ncPython\n") == [
        (812, "llama-server"),
        (90, "Python"),
    ]
    assert doctor.parse_listeners("") == []
    assert doctor.parse_listeners("garbage\nc orphan\n") == []


def test_doctor_reads_the_record_the_model_server_writes() -> None:
    assert doctor.RUNTIME_RECORD == server.RECORD_NAME


# --- set up -------------------------------------------------------------------------------


def test_a_healthy_mac_gets_one_line_per_piece(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    _install(layout.runtimes, "0.7.11")
    _install(layout.runtimes, "0.7.12")
    checks = _doctor(layout, env, settings, mac)

    assert list(checks) == ["local AI", "uv", "runtime mlx-vlm", "model server", "memory budgets"]
    assert all(check.ok for check in checks.values()), checks
    assert checks["local AI"].detail == f"{layout.runtimes}, Apple silicon, 32 GB"
    assert checks["uv"].detail == "/opt/homebrew/bin/uv"
    assert checks["runtime mlx-vlm"].detail == "0.7.12 (0.7.11 kept to go back to)"
    assert checks["model server"].detail == "answering on 54805"
    assert checks["memory budgets"].detail == "8 GB by day, 20 GB 21:30–05:30, of 32 GB"
    # Nothing is loaded, so the runtime's own port was not looked into.
    assert [argv[0] for argv in mac.asked] == ["sysctl"]


def test_each_thing_that_is_missing_says_what_to_do_about_it(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    mac.uv = None
    mac.listening = set()
    checks = _doctor(layout, env, settings, mac)

    assert "brew install uv" in checks["uv"].detail and not checks["uv"].ok
    runtime = checks["runtime mlx-vlm"]
    assert not runtime.ok and "AI models page › Software that runs models" in runtime.detail
    server_line = checks["model server"]
    assert not server_line.ok
    assert "nothing answers on 54805 — framefound restart model-server" in server_line.detail
    assert str(layout.logs / "model-server.log") in server_line.detail
    # The folder itself is fine, and FrameFound is not broken: warnings only.
    assert checks["local AI"].ok


def test_a_mac_that_cannot_run_mlx_is_told_so(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    checks = _doctor(layout, env, settings, mac, machine=("Darwin", "x86_64"))
    assert checks["local AI"].detail == f"{layout.runtimes}, Darwin x86_64, 32 GB"
    runtime = checks["runtime mlx-vlm"]
    assert not runtime.ok and runtime.detail == "MLX models need a Mac with Apple silicon."


def test_a_runtimes_folder_that_cannot_be_written_is_named(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    layout.runtimes.rmdir()
    line = _doctor(layout, env, settings, mac)["local AI"]
    assert not line.ok and str(layout.runtimes) in line.detail
    assert "framefound install makes it" in line.detail


def test_a_version_whose_program_has_gone_is_not_reported_as_installed(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    _install(layout.runtimes, "0.7.12", program=False)
    line = _doctor(layout, env, settings, mac)["runtime mlx-vlm"]
    assert not line.ok
    assert line.detail.startswith("0.7.12 is recorded as in use but its program is missing")
    assert line.detail.endswith(
        "AI models page › Software that runs models › Install a particular version › 0.7.12 "
        "installs it afresh"
    )


def test_a_damaged_record_of_the_version_in_use_is_said_in_the_pages_words(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    (layout.runtimes / "mlx-vlm").mkdir()
    (layout.runtimes / "mlx-vlm" / runtimes.STATE).write_text("{not json", encoding="utf-8")
    line = _doctor(layout, env, settings, mac)["runtime mlx-vlm"]
    assert not line.ok and "is damaged" in line.detail and "Install a version" in line.detail


def test_the_program_on_the_runtimes_port_is_named_and_ours_is_told_apart(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    mac.listening = {54805, 54806}
    mac.lsof = "p4242\ncnode\nf12\n"
    line = _doctor(layout, env, settings, mac)["model runtime"]
    assert not line.ok
    assert "port 54806 is held by node (pid 4242), not by FrameFound" in line.detail
    assert ["lsof", "-nP", "-iTCP:54806", "-sTCP:LISTEN", "-Fpc"] in mac.asked

    # The model server's own record names that process: a model is loaded.
    (layout.run / doctor.RUNTIME_RECORD).write_text(json.dumps({"pid": 4242}), encoding="utf-8")
    mac.lsof = "p4242\ncpython3.12\n"
    line = _doctor(layout, env, settings, mac)["model runtime"]
    assert line.ok and line.detail == "a model is loaded (python3.12 4242)"

    # lsof could not say: still a warning, with nothing invented.
    (layout.run / doctor.RUNTIME_RECORD).unlink()
    mac.lsof = ""
    line = _doctor(layout, env, settings, mac)["model runtime"]
    assert not line.ok and "a program lsof could not name" in line.detail


# --- what the catalogue holds -----------------------------------------------------------


def _model(repo_id: str, size: int = 5) -> LocalModel:
    return LocalModel(
        repo_id=repo_id,
        repo_key=repo_id.casefold(),
        revision=REVISION,
        publisher=repo_id.partition("/")[0],
        runtime="mlx-vlm",
        files=[{"path": "model.safetensors", "size": size, "sha256": None, "blob_id": None}],
        size_bytes=size,
        state="installed",
    )


def _put_in_library(settings: Settings, model: LocalModel) -> None:
    """The model whole in the library on this Mac's own disk (no bulk folder)."""
    folder = settings.models_dir / "library" / model.repo_id / model.revision
    folder.mkdir(parents=True)
    (folder / "model.safetensors").write_bytes(b"x" * model.size_bytes)
    files.write_manifest(
        folder,
        files.Manifest(
            repo_id=model.repo_id,
            revision=model.revision,
            variant="",
            runtime="mlx-vlm",
            files=(RepoFile("model.safetensors", model.size_bytes, None, None),),
            completed_at="2026-11-12T18:39:10Z",
        ),
    )


def _catalogue(settings: Settings, fill: Callable[[AsyncSession], Any]) -> None:
    async def go() -> None:
        engine = create_async_engine(settings.db_url)
        async with AsyncSession(engine) as db:
            await fill(db)
            await db.commit()
        await engine.dispose()

    asyncio.run(go())


def test_a_role_whose_model_is_not_on_this_mac_points_at_copy_again(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    looker, editor, coming = _model(LOOKER), _model(EDITOR), _model("google/gemma-4-E4B-it")
    _put_in_library(settings, looker)

    async def fill(db: AsyncSession) -> None:
        db.add_all([looker, editor, coming])
        await db.flush()
        db.add_all(
            [
                LocalAiRole(role="looker", model_id=looker.id, pending_model_id=coming.id),
                LocalAiRole(role="editor", model_id=editor.id),
            ]
        )

    _catalogue(settings, fill)
    checks = _doctor(layout, env, settings, mac)

    looker_line = checks["looker model"]
    assert looker_line.ok
    assert looker_line.detail == (
        f"{LOOKER} on this Mac; google/gemma-4-E4B-it takes it over once its copy is on this Mac"
    )
    editor_line = checks["editor model"]
    assert not editor_line.ok
    assert editor_line.detail == (
        f"{EDITOR} has the role but is not on this Mac — AI models page › Who does what › "
        "Copy again"
    )


def test_a_role_nobody_holds_gets_no_line(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    async def fill(db: AsyncSession) -> None:
        db.add(LocalAiRole(role="looker", model_id=None))

    _catalogue(settings, fill)
    checks = _doctor(layout, env, settings, mac)
    assert "looker model" not in checks and "editor model" not in checks


def test_a_budget_that_leaves_the_mac_too_little_is_pointed_out(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    config = settings_store.LocalModelsConfig(night_budget_gb=30.0)

    async def fill(db: AsyncSession) -> None:
        await settings_store.save_config(db, config)

    _catalogue(settings, fill)
    line = _doctor(layout, env, settings, mac)["memory budgets"]
    assert not line.ok
    assert line.detail.startswith(
        "the overnight budget (30 GB) leaves under 4 GB of this Mac's 32 GB for everything else"
    )
    assert "Sharing this Mac" in line.detail

    # Without an overnight window only the daytime budget is ever in force.
    config.night_start = config.night_end = "21:30"
    _catalogue(settings, lambda db: settings_store.save_config(db, config))
    line = _doctor(layout, env, settings, mac)["memory budgets"]
    assert line.ok and line.detail == "8 GB by day, no overnight window, of 32 GB"

    # A Mac that will not say how much memory it has: the budgets alone, unjudged.
    mac.memsize = None
    checks = _doctor(layout, env, settings, mac)
    assert checks["memory budgets"].detail == "8 GB by day, no overnight window"
    assert checks["local AI"].detail == f"{layout.runtimes}, Apple silicon"


def test_a_pause_somebody_forgot_is_said_with_who_paused(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    config = settings_store.LocalModelsConfig(
        paused=True, paused_at="2026-11-10T19:02:00Z", paused_by="dj@example.com"
    )
    _catalogue(settings, lambda db: settings_store.save_config(db, config))
    line = _doctor(layout, env, settings, mac)["local AI paused"]
    assert not line.ok
    assert line.detail.startswith("paused since ") and "by dj@example.com" in line.detail
    assert line.detail.endswith("no model runs until Resume on the AI models page")


# --- the second runtime, llama.cpp (L6 7.3) ------------------------------------------------


def _llama(root: Path, *versions: str) -> Path:
    """llama.cpp installed and switched to, version by version, as the page does it."""
    run = fake.FakeRunner(make_llama=True)
    for version in versions:
        runtimes.install(LlamaCpp(), version, root, run=run)
        runtimes.activate(root, "llama-cpp", version)
    return runtimes.version_dir(root, "llama-cpp", versions[-1])


def _gguf_row(repo_id: str = "google/demo-vision-gguf") -> LocalModel:
    model = _model(repo_id)
    model.runtime = "llama-cpp"
    model.variant = "Q4_K_M"
    model.files = [{"path": "demo-Q4_K_M.gguf", "size": 5, "sha256": None, "blob_id": None}]
    return model


def test_llama_cpp_gets_no_line_until_a_model_needs_it(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    _install(layout.runtimes, "0.7.12")
    checks = _doctor(layout, env, settings, mac)
    # An MLX-only Mac: an empty second line would read as a fault.
    assert "runtime llama-cpp" not in checks
    assert checks["runtime mlx-vlm"].ok
    # Not on an Intel Mac either, while nothing needs it.
    assert "runtime llama-cpp" not in _doctor(layout, env, settings, mac, ("Darwin", "x86_64"))

    _catalogue(settings, lambda db: _add(db, _gguf_row()))
    line = _doctor(layout, env, settings, mac)["runtime llama-cpp"]
    assert not line.ok and not line.fatal
    assert line.detail == (
        "not installed, and 1 model on the AI models page needs it — AI models page › "
        "Software that runs models › Install"
    )
    _catalogue(settings, lambda db: _add(db, _gguf_row("google/another-gguf")))
    line = _doctor(layout, env, settings, mac)["runtime llama-cpp"]
    assert "and 2 models on the AI models page need it" in line.detail

    intel = _doctor(layout, env, settings, mac, ("Darwin", "x86_64"))["runtime llama-cpp"]
    assert intel.detail == "llama.cpp is installed here from its build for Apple-silicon Macs."


async def _add(db: AsyncSession, model: LocalModel) -> None:
    db.add(model)


def test_an_installed_llama_cpp_says_its_build_and_the_one_kept(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    _install(layout.runtimes, "0.7.12")
    _llama(layout.runtimes, "b12318", "0.9.0")
    checks = _doctor(layout, env, settings, mac)

    line = checks["runtime llama-cpp"]
    assert line.ok and line.detail == "0.9.0, build b12110 (b12318 kept to go back to)"
    # MLX-VLM's line has nothing to add, so it reads as it did before.
    assert checks["runtime mlx-vlm"].detail == "0.7.12"
    assert list(checks)[:4] == ["local AI", "uv", "runtime mlx-vlm", "runtime llama-cpp"]
    # Nothing was run to say so: no check runs the program.
    assert [argv[0] for argv in mac.asked] == ["sysctl"]


def test_a_llama_cpp_whose_program_has_gone_is_not_reported_as_installed(
    layout: Layout, env: dict[str, str], settings: Settings, mac: Mac
) -> None:
    folder = _llama(layout.runtimes, "0.9.0")
    for program in folder.rglob("llama-server"):
        program.unlink()
    line = _doctor(layout, env, settings, mac)["runtime llama-cpp"]
    assert not line.ok
    assert line.detail == (
        f"0.9.0 is recorded as in use but its program is missing ({folder}) — AI models page "
        "› Software that runs models › Install a particular version › 0.9.0 installs it afresh"
    )


def test_a_catalogue_that_cannot_be_read_is_said_once(
    layout: Layout, env: dict[str, str], tmp_path: Path, mac: Mac
) -> None:
    layout.runtimes.mkdir(parents=True)
    empty = f"sqlite+aiosqlite:///{(tmp_path / 'no-tables.db').as_posix()}"
    broken = Settings(data_dir=tmp_path, runtimes_dir=layout.runtimes, database_url=empty)
    checks = _doctor(layout, env, broken, mac)
    line = checks["local AI roles"]
    assert not line.ok and "see the database line" in line.detail
    assert "memory budgets" not in checks
