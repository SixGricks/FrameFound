"""FrameFound as a program on a Mac (framefound/native, docs/mac.md).

Everything here runs on any platform: the package keeps each outside effect
(launchctl, Homebrew's binaries) behind an argv list, and these tests read
the lists. What they cannot show is the commands themselves working on a
Mac — that is `framefound doctor`'s job on the machine."""

import importlib
import os
import plistlib
import re
import shlex
import tarfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from framefound import local_models
from framefound.config import Settings, get_settings
from framefound.native import (
    backup,
    cli,
    database,
    doctor,
    envfile,
    installer,
    services,
    storage,
)
from framefound.native.launchd import AgentState, Launchd, parse_print
from framefound.native.layout import DEFAULT_PORTS, Layout
from framefound.storage import moves, stores

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture()
def layout(tmp_path: Path) -> Layout:
    return Layout(home=tmp_path / "FrameFound", source=REPO)


@pytest.fixture()
def env(layout: Layout) -> dict[str, str]:
    return envfile.parse(envfile.initial(layout, "/opt/homebrew/opt/postgresql@17/bin"))


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    stores._CACHE.clear()
    yield
    get_settings.cache_clear()
    stores._CACHE.clear()


@pytest.fixture(autouse=True)
def _no_terminal_recorded() -> Iterator[None]:
    """`cli.main` records the PATH of the Terminal it was typed in, in the
    environment, for the install an update becomes. A test starts with none
    recorded and leaves none behind for the next one."""
    before = os.environ.pop(installer.SHELL_PATH_ENV, None)
    yield
    os.environ.pop(installer.SHELL_PATH_ENV, None)
    if before is not None:
        os.environ[installer.SHELL_PATH_ENV] = before


# --- the settings file -----------------------------------------------------


def test_values_are_read_literally() -> None:
    text = "# comment\nA=1\nPASSWORD=pa$$w=rd `id` $(id)\nLANES=a b c\n\nnot a setting\nB =  2 \n"
    assert envfile.parse(text) == {
        "A": "1",
        "PASSWORD": "pa$$w=rd `id` $(id)",
        "LANES": "a b c",
        "B": "2",
    }


def test_an_update_keeps_the_operators_file() -> None:
    text = "# mine\nA=1\n# about B\nB=2\n"
    assert envfile.merged(text, {"B": "3", "C": "4"}) == "# mine\nA=1\n# about B\nB=3\n\nC=4\n"


def test_filling_in_missing_settings_never_replaces_one() -> None:
    text = "SECRET=keep\nEMPTY=\n"
    merged = envfile.merged(text, {"SECRET": "new", "EMPTY": "x", "ADDED": "1"}, only_missing=True)
    assert envfile.parse(merged) == {"SECRET": "keep", "EMPTY": "", "ADDED": "1"}


def test_a_fresh_settings_file_is_complete(layout: Layout, env: dict[str, str]) -> None:
    for key in (
        "FRAMEFOUND_SECRET_KEY",
        "POSTGRES_PASSWORD",
        "REDIS_PASSWORD",
        "FRAMEFOUND_PG_BIN",
        "FRAMEFOUND_DATA_DIR",
        "FRAMEFOUND_MODELS_DIR",
    ):
        assert env[key], key
    assert env["FRAMEFOUND_PROXY_DIR"] == ""
    # Offered, and empty: nothing bulky leaves this Mac until a folder is given.
    assert env["FRAMEFOUND_BULK_DIR"] == "" and env["FRAMEFOUND_BACKUP_COPY_DIR"] == ""
    # Loopback, and not the ports another Postgres, Redis or dev server uses.
    assert env["POSTGRES_HOST"] == env["REDIS_HOST"] == "127.0.0.1"
    assert {env["POSTGRES_PORT"], env["REDIS_PORT"]}.isdisjoint({"5432", "6379"})
    assert {env["FRAMEFOUND_API_PORT"], env["FRAMEFOUND_WEB_PORT"]}.isdisjoint({"8000", "3000"})
    # Caddy on this machine is the only proxy whose forwarded header counts.
    assert env["FRAMEFOUND_TRUSTED_PROXIES"] == "127.0.0.1/32"


def test_the_application_reads_the_file_it_is_given(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], layout: Layout
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    loaded = Settings()
    assert loaded.data_dir == layout.data
    assert loaded.models_dir == layout.models
    assert loaded.proxy_dir is None
    assert loaded.bulk_dir is None  # a blank line is "not set", not the working directory
    assert loaded.postgres_port == int(env["POSTGRES_PORT"])
    assert loaded.redis_url == (f"redis://:{env['REDIS_PASSWORD']}@127.0.0.1:{env['REDIS_PORT']}/0")


def test_a_queue_password_is_escaped_in_the_url() -> None:
    assert Settings(REDIS_PASSWORD="p@ss/w:rd").redis_url == (  # type: ignore[call-arg]
        "redis://:p%40ss%2Fw%3Ard@redis:6379/0"
    )
    assert Settings().redis_url == "redis://redis:6379/0"  # the containers: unchanged


def test_a_fresh_settings_file_names_the_runtimes_folder(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, env: dict[str, str]
) -> None:
    assert env["FRAMEFOUND_RUNTIMES_DIR"] == str(layout.runtimes)
    assert layout.runtimes in layout.folders()  # made by install, not by the first download
    assert env["FRAMEFOUND_MODEL_SERVER_PORT"] == "54805"
    assert env["FRAMEFOUND_MODEL_RUNTIME_PORT"] == "54806"
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    loaded = Settings()
    # The line that makes this an install that can run models.
    assert loaded.runtimes_dir == layout.runtimes
    assert local_models.availability(loaded).mode == "ready"
    assert (loaded.model_server_port, loaded.model_runtime_port) == (54805, 54806)


def test_a_setting_a_release_adds_arrives_with_its_comment() -> None:
    template = (
        "# ---- Files ----\n"
        "# Where the data is.\n"
        "DATA=/d\n"
        "# Turns the page on.\n"
        "# Second line of it.\n"
        "NEW_DIR=/r\n"
        "\n"
        "# ---- Plumbing ----\n"
        "PORT_A=1\n"
        "# The two new ports.\n"
        "NEW_ONE=5\n"
        "NEW_TWO=6\n"
        "BLANKED=x\n"
    )
    mine = "# my own note\nDATA=/elsewhere\nPORT_A=9\nBLANKED=\nMINE=1"
    grown = envfile.added(mine, template)
    # Everything the operator wrote is still there, word for word, first.
    assert grown.startswith(mine + "\n")
    assert grown[len(mine) :] == (
        "\n\n# Turns the page on.\n# Second line of it.\nNEW_DIR=/r"
        "\n\n# The two new ports.\nNEW_ONE=5\nNEW_TWO=6\n"
    )
    # A setting left blank on purpose is a choice, not a gap.
    assert envfile.parse(grown)["BLANKED"] == "" and envfile.parse(grown)["DATA"] == "/elsewhere"
    # Nothing missing: nothing written.
    assert envfile.added(grown, template) == grown


def test_install_keeps_secrets_on_a_second_run(layout: Layout) -> None:
    first = installer.ensure_env(layout, Path("/opt/homebrew/opt/postgresql@17/bin"))
    envfile.update(layout.env_file, {"FRAMEFOUND_FAST_WORKERS": "1"})
    second = installer.ensure_env(layout, Path("/somewhere/else"))
    assert second["FRAMEFOUND_SECRET_KEY"] == first["FRAMEFOUND_SECRET_KEY"]
    assert second["POSTGRES_PASSWORD"] == first["POSTGRES_PASSWORD"]
    assert second["FRAMEFOUND_PG_BIN"] == first["FRAMEFOUND_PG_BIN"]
    assert second["FRAMEFOUND_FAST_WORKERS"] == "1"


# --- services ----------------------------------------------------------------


def _queues(argv: list[str]) -> set[str]:
    return set(argv[argv.index("-Q") + 1].split(","))


def test_every_queue_a_task_uses_has_a_worker_here(layout: Layout, env: dict[str, str]) -> None:
    """A queue nothing consumes is work that silently never happens — the
    hazard of adding a queue (`exports`) and forgetting one deployment."""
    from framefound.celery_app import celery_app
    from framefound.processing import tasks  # noqa: F401 - registers the tasks

    used = {
        getattr(task, "queue", None) or "default"
        for name, task in celery_app.tasks.items()
        if name.startswith("framefound.")
    }
    consumed: set[str] = set()
    for name in services.LANES:
        consumed |= _queues(services.launch(name, layout, env).argv)
    assert used <= consumed, used - consumed


def test_the_containers_consume_the_same_queues(layout: Layout, env: dict[str, str]) -> None:
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    in_compose = set()
    for queues in re.findall(r'"-Q", "([a-z_,]+)"', compose):
        in_compose |= set(queues.split(","))
    native: set[str] = set()
    for name in services.LANES:
        native |= _queues(services.launch(name, layout, env).argv)
    assert in_compose == native


def test_transcription_runs_one_at_a_time_whatever_the_settings(
    layout: Layout, env: dict[str, str]
) -> None:
    argv = services.launch("worker-ai", layout, {**env, "FRAMEFOUND_AI_WORKERS": "4"}).argv
    assert "--concurrency=1" in argv


def test_lane_concurrency_comes_from_the_settings(layout: Layout, env: dict[str, str]) -> None:
    assert "--concurrency=2" in services.launch("worker-media", layout, env).argv
    tuned = {**env, "FRAMEFOUND_MEDIA_WORKERS": "1"}
    assert "--concurrency=1" in services.launch("worker-media", layout, tuned).argv
    nonsense = {**env, "FRAMEFOUND_MEDIA_WORKERS": "lots"}
    assert "--concurrency=2" in services.launch("worker-media", layout, nonsense).argv


def test_the_api_migrates_before_it_serves(layout: Layout, env: dict[str, str]) -> None:
    start = services.launch("api", layout, env)
    assert [Path(start.before[0][0]).name, *start.before[0][1:]] == ["alembic", "upgrade", "head"]
    assert start.cwd == layout.server_source  # where alembic.ini is
    assert "127.0.0.1" in start.argv and env["FRAMEFOUND_API_PORT"] in start.argv
    # The app decides whose X-Forwarded-For to believe, not uvicorn.
    assert "--no-proxy-headers" in start.argv


def test_the_database_listens_on_loopback_only(layout: Layout, env: dict[str, str]) -> None:
    argv = services.launch("postgres", layout, env).argv
    assert Path(argv[0]) == Path(env["FRAMEFOUND_PG_BIN"]) / "postgres"
    assert "listen_addresses=127.0.0.1" in argv
    assert argv[argv.index("-p") + 1] == env["POSTGRES_PORT"]
    assert argv[argv.index("-D") + 1] == str(layout.pgdata)


def test_the_model_server_listens_on_loopback_only(layout: Layout, env: dict[str, str]) -> None:
    """A model answers whoever reaches it, and its port is not behind
    Caddy's rules: it is bound to this machine and to nothing else."""
    start = services.launch("model-server", layout, {**env, "FRAMEFOUND_MODEL_SERVER_PORT": "6100"})
    argv = start.argv
    assert Path(argv[0]) == layout.venv_bin / "uvicorn"
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "6100"
    assert not any(word in ("0.0.0.0", "::") for word in argv)  # noqa: S104 - what must be absent
    # The loopback guard reads the connection's own address, never a header.
    assert "--no-proxy-headers" in argv and "--proxy-headers" not in argv
    # The program named is the supervisor, built by its factory.
    assert "--factory" in argv
    module, _, attribute = argv[1].partition(":")
    assert callable(getattr(importlib.import_module(module), attribute))
    assert start.cwd == layout.server_source and start.before == [] and start.env == {}
    # It needs no database, so nothing holds its start up; and it stops in
    # time for its child to be stopped first.
    service = services.BY_NAME["model-server"]
    assert service.waits_for == ()
    assert service.exit_timeout_s > int(argv[argv.index("--timeout-graceful-shutdown") + 1]) + 25
    # Two ports of its own, unlike every other; and what the application
    # falls back on is what the file would say.
    assert len(set(DEFAULT_PORTS.values())) == len(DEFAULT_PORTS)
    defaults = Settings()
    assert str(defaults.model_server_port) == DEFAULT_PORTS["FRAMEFOUND_MODEL_SERVER_PORT"]
    assert str(defaults.model_runtime_port) == DEFAULT_PORTS["FRAMEFOUND_MODEL_RUNTIME_PORT"]


def test_the_model_server_starts_after_the_workers_and_stays_up(layout: Layout) -> None:
    names = [service.name for service in services.SERVICES]
    assert names.index("worker-ai") < names.index("model-server") < names.index("backup")
    plist = services.agent(services.BY_NAME["model-server"], layout)
    assert plist["KeepAlive"] is True and "StartInterval" not in plist
    # Below Adobe, as the other heavy lanes; a restart reloads its model.
    assert plist["Nice"] > 0 and plist["LowPriorityBackgroundIO"] is True
    assert "model-server" in services.APP_SERVICES


def test_the_web_app_is_told_where_the_api_is(layout: Layout, env: dict[str, str]) -> None:
    start = services.launch("web", layout, env)
    assert start.env["HOSTNAME"] == "127.0.0.1"
    assert start.env["API_INTERNAL_URL"] == f"http://127.0.0.1:{env['FRAMEFOUND_API_PORT']}"


def test_every_service_can_be_launched(layout: Layout, env: dict[str, str]) -> None:
    for service in services.SERVICES:
        assert services.launch(service.name, layout, env).argv
    with pytest.raises(KeyError):
        services.launch("nope", layout, env)


def test_an_agent_holds_no_secrets_and_no_program(layout: Layout, env: dict[str, str]) -> None:
    """launchd runs the launcher, which reads framefound.env itself: the
    plists in ~/Library/LaunchAgents never contain a password."""
    for service in services.SERVICES:
        raw = plistlib.dumps(services.agent(service, layout))
        plist = plistlib.loads(raw)
        assert plist["Label"] == f"com.framefound.{service.name}"
        assert plist["ProgramArguments"][-3:] == ["framefound.native", "run", service.name]
        for secret in ("FRAMEFOUND_SECRET_KEY", "POSTGRES_PASSWORD", "REDIS_PASSWORD"):
            assert env[secret].encode() not in raw


def test_services_stay_up_and_the_backup_runs_on_a_timer(layout: Layout) -> None:
    api = services.agent(services.BY_NAME["api"], layout)
    assert api["KeepAlive"] is True and "StartInterval" not in api
    nightly = services.agent(services.BY_NAME["backup"], layout)
    assert nightly["StartInterval"] == 3600 and "KeepAlive" not in nightly


def test_heavy_lanes_run_below_everything_else(layout: Layout) -> None:
    assert services.agent(services.BY_NAME["api"], layout)["Nice"] == 0
    media = services.agent(services.BY_NAME["worker-media"], layout)
    assert media["Nice"] > 0 and media["LowPriorityBackgroundIO"] is True
    # A transcode in hand is allowed to finish before launchd kills it.
    assert media["ExitTimeOut"] >= 300


def test_celery_gets_the_fork_safety_switch(layout: Layout, env: dict[str, str]) -> None:
    process = services.process_environment(layout, env)
    assert process["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] == "YES"
    assert process["HF_HOME"].startswith(env["FRAMEFOUND_MODELS_DIR"])
    assert process["PATH"].startswith(str(layout.venv_bin))


def test_every_service_is_started_with_a_locale_launchd_does_not_give_it(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, env: dict[str, str]
) -> None:
    """launchd hands an agent no LANG and no LC_ALL. Without one Postgres
    logged "postmaster became multithreaded during startup" every 15 seconds
    on the first real install and never came up, while the same command
    typed in a Terminal — which sets LANG — worked."""
    process = services.process_environment(layout, env)
    assert process["LC_ALL"] == process["LANG"] == services.LOCALE == "en_US.UTF-8"

    # Through the launcher, as launchd runs it: nothing in its own
    # environment, and a settings file that tries to blank the locale.
    envfile.write(layout.env_file, envfile.initial(layout, "/pg/bin") + "LC_ALL=\nLANG=\n")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    started: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(cli.os, "chdir", lambda _path: None)
    monkeypatch.setattr(cli.os, "execvpe", lambda _p, argv, child: started.append((argv, child)))
    for name in ("postgres", "valkey", "web"):
        assert cli.run_service(layout, name) == 0
    assert Path(started[0][0][0]).name == "postgres"
    for _argv, child in started:
        assert child["LC_ALL"] == child["LANG"] == "en_US.UTF-8"


def test_the_cluster_is_created_in_the_locale_the_services_start_it_under(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, env: dict[str, str]
) -> None:
    """Stated, not taken from the Terminal the installer happens to run in:
    a cluster's locale is fixed when it is made."""
    ran: list[list[str]] = []

    def initdb(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        ran.append([str(word) for word in argv])
        layout.pgdata.mkdir(parents=True, exist_ok=True)
        (layout.pgdata / "PG_VERSION").write_text("17\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(database.proc, "run", initdb)
    monkeypatch.setenv("LANG", "de_DE.UTF-8")  # whoever's Terminal it is
    monkeypatch.delenv("LC_ALL", raising=False)

    assert database.initialise(layout, env) is True

    assert Path(ran[0][0]).name == "initdb"
    assert f"--locale={services.LOCALE}" in ran[0] and "--encoding=UTF8" in ran[0]
    # What every cluster already on a Mac was created with. Changing this
    # would start those under a locale they were not made in.
    assert "--locale=en_US.UTF-8" in ran[0]
    assert env["POSTGRES_PASSWORD"] not in " ".join(ran[0])
    assert not (layout.run / "initdb-password").exists()
    # A cluster that is there is never made again, whatever it was made with.
    assert database.initialise(layout, env) is False and len(ran) == 1


# --- launchd -------------------------------------------------------------------

PRINT_RUNNING = (
    "gui/501/com.framefound.api = {\n"
    "\tactive count = 1\n"
    "\tstate = running\n"
    "\tprogram = /Users/dj/FrameFound/venv/bin/python\n"
    "\tpid = 4242\n"
    "\tlast exit code = (never exited)\n"
    "\tsubsystem = {\n\t\tstate = something nested\n\t}\n"
    "}\n"
)
PRINT_WAITING = "x = {\n\tstate = waiting\n\tlast exit code = 78\n}\n"


def test_launchctl_print_is_read() -> None:
    assert parse_print(PRINT_RUNNING) == AgentState(True, "running", 4242, None)
    assert parse_print(PRINT_WAITING) == AgentState(True, "waiting", None, 78)
    assert not parse_print(PRINT_WAITING).running


class FakeLaunchctl:
    """Records argv; pretends `loaded` services exist."""

    def __init__(self, loaded: set[str] | None = None) -> None:
        self.loaded = set(loaded or ())
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        self.calls.append(argv)
        verb, label = argv[1], self._label(argv)
        if verb == "print":
            ok = label in self.loaded
            return SimpleNamespace(returncode=0 if ok else 113, stdout=PRINT_RUNNING if ok else "")
        if verb == "bootstrap":
            self.loaded.add(label)
        if verb == "bootout":
            self.loaded.discard(label)
        return SimpleNamespace(returncode=0, stdout="")

    @staticmethod
    def _label(argv: list[str]) -> str:
        # "gui/501/<label>" for print and bootout, a plist path for bootstrap.
        return re.split(r"[/\\]", argv[-1])[-1].removesuffix(".plist")

    def verbs(self) -> list[tuple[str, str]]:
        return [
            (argv[1], self._label(argv).removeprefix(services.LABEL_PREFIX))
            for argv in self.calls
            if argv[1] != "print"
        ]


@pytest.fixture()
def launchctl(monkeypatch: pytest.MonkeyPatch) -> FakeLaunchctl:
    monkeypatch.setattr("os.getuid", lambda: 501, raising=False)
    return FakeLaunchctl()


def _launchd(layout: Layout, launchctl: FakeLaunchctl, tmp_path: Path) -> Launchd:
    return Launchd(layout, runner=launchctl, agents_dir=tmp_path / "agents", sleep=lambda _s: None)


def test_starting_twice_loads_once(
    layout: Layout, launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    launchd = _launchd(layout, launchctl, tmp_path)
    launchd.write(services.BY_NAME["api"])
    launchd.start("api")
    launchd.start("api")
    assert launchctl.verbs() == [("bootstrap", "api")]
    assert plistlib.loads(launchd.plist_path("api").read_bytes())["Label"] == "com.framefound.api"


def test_everything_stops_in_reverse_so_the_database_is_last(
    layout: Layout, launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    launchctl.loaded = {services.label(s.name) for s in services.SERVICES}
    installer.stop_all(_launchd(layout, launchctl, tmp_path))
    stopped = [name for verb, name in launchctl.verbs() if verb == "bootout"]
    assert stopped[-1] == "postgres" and stopped[-2] == "valkey"
    assert stopped.index("api") < stopped.index("postgres")
    assert stopped.index("model-server") < stopped.index("valkey")
    assert len(stopped) == len(services.SERVICES)


def test_starting_everything_brings_the_database_up_first(
    monkeypatch: pytest.MonkeyPatch,
    layout: Layout,
    env: dict[str, str],
    launchctl: FakeLaunchctl,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(installer.proc, "wait_for_port", lambda port, seconds: True)
    created: list[str] = []
    monkeypatch.setattr(database, "exists", lambda _env: False)
    monkeypatch.setattr(database, "create", lambda _env: created.append("db"))

    installer.start_all(layout, env, _launchd(layout, launchctl, tmp_path), lambda _m: None)

    started = [name for verb, name in launchctl.verbs() if verb == "bootstrap"]
    assert started[0] == "postgres"
    assert set(started) == {s.name for s in services.SERVICES}
    assert created == ["db"]
    assert layout.data.is_dir() and layout.logs.is_dir()


def test_a_database_that_never_answers_stops_the_start(
    monkeypatch: pytest.MonkeyPatch,
    layout: Layout,
    env: dict[str, str],
    launchctl: FakeLaunchctl,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(installer.proc, "wait_for_port", lambda port, seconds: False)
    with pytest.raises(installer.InstallError, match="postgres.log"):
        installer.start_all(layout, env, _launchd(layout, launchctl, tmp_path), lambda _m: None)
    assert [name for verb, name in launchctl.verbs() if verb == "bootstrap"] == ["postgres"]


# The settings file an install made before local AI models has (the template
# of the 2026-11-11 release, cut down), as its operator left it.
BEFORE_LOCAL_AI = """# FrameFound on this Mac — every setting in one place (docs/mac.md).
FRAMEFOUND_SERVER_NAME=FrameFound
FRAMEFOUND_HTTP_PORT=8080
FRAMEFOUND_DATA_DIR={home}/data
FRAMEFOUND_BULK_DIR=/Volumes/Photos/Framefound
FRAMEFOUND_PROXY_DIR=
FRAMEFOUND_MODELS_DIR={home}/models
FRAMEFOUND_MEDIA_ROOT=/Volumes
# fewer while Premiere is open — DJ
FRAMEFOUND_FAST_WORKERS=1
FRAMEFOUND_SECRET_KEY=an-old-secret-not-a-real-one
POSTGRES_PASSWORD=an-old-password-not-a-real-one
REDIS_PASSWORD=an-old-queue-password-not-a-real-one
FRAMEFOUND_API_PORT=54801
FRAMEFOUND_WEB_PORT=54802
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=54803
REDIS_HOST=127.0.0.1
REDIS_PORT=54804
FRAMEFOUND_PG_BIN=/opt/homebrew/opt/postgresql@17/bin
"""


def test_an_install_from_before_local_ai_upgrades_cleanly(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    for folder in (layout.data, layout.models, layout.run, layout.logs):
        folder.mkdir(parents=True)
    old = BEFORE_LOCAL_AI.format(home=layout.home.as_posix())
    envfile.write(layout.env_file, old)
    old_env = envfile.read(layout.env_file)

    # The new code, before `install` has run again (a `git pull` alone, or
    # `framefound restart` in between): local AI stays off, and the model
    # server can still be started — on its default port, waiting for nothing.
    for key, value in old_env.items():
        monkeypatch.setenv(key, value)
    assert local_models.availability(Settings()).ok is False
    launched: list[list[str]] = []
    waited: list[int] = []
    monkeypatch.setattr(cli.proc, "wait_for_port", lambda port, _s: waited.append(port) or True)
    monkeypatch.setattr(cli.os, "chdir", lambda _path: None)
    monkeypatch.setattr(cli.os, "execvpe", lambda _p, argv, _env: launched.append(argv))
    assert cli.run_service(layout, "model-server") == 0
    assert launched[0][launched[0].index("--port") + 1] == "54805" and waited == []

    # `framefound update` runs install again: the three settings arrive with
    # their comments, and not one line the operator had changes.
    pg_bin = Path("/opt/homebrew/opt/postgresql@17/bin")
    new_env = installer.ensure_env(layout, pg_bin)
    text = layout.env_file.read_text(encoding="utf-8")
    assert text.startswith(old)
    assert {key: new_env[key] for key in old_env} == old_env
    assert set(new_env) - set(old_env) >= {
        "FRAMEFOUND_RUNTIMES_DIR",
        "FRAMEFOUND_MODEL_SERVER_PORT",
        "FRAMEFOUND_MODEL_RUNTIME_PORT",
    }
    assert new_env["FRAMEFOUND_RUNTIMES_DIR"] == str(layout.runtimes)
    assert new_env["FRAMEFOUND_MODEL_SERVER_PORT"] == "54805"
    assert "turns\n# that page on.\nFRAMEFOUND_RUNTIMES_DIR=" in text
    installer.ensure_env(layout, pg_bin)
    assert layout.env_file.read_text(encoding="utf-8") == text  # a third run adds nothing

    for key, value in new_env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    assert local_models.availability(Settings()).mode == "ready"

    # …and starts everything again: the folder is made, the new service is
    # written and started with the others, and its agent holds no secret.
    monkeypatch.setattr(installer.proc, "wait_for_port", lambda port, seconds: True)
    monkeypatch.setattr(database, "exists", lambda _env: True)
    launchd = _launchd(layout, launchctl, tmp_path)
    installer.start_all(layout, new_env, launchd, lambda _m: None)
    assert layout.runtimes.is_dir()
    assert ("bootstrap", "model-server") in launchctl.verbs()
    agent = launchd.plist_path("model-server").read_bytes()
    for secret in ("FRAMEFOUND_SECRET_KEY", "POSTGRES_PASSWORD", "REDIS_PASSWORD"):
        assert new_env[secret].encode() not in agent

    # Removing the services removes it too, and leaves the folder.
    cli.cmd_uninstall(layout, launchd)
    assert not launchd.plist_path("model-server").exists() and layout.runtimes.is_dir()


def test_status_lists_the_model_server_in_line_with_the_rest(
    layout: Layout,
    launchctl: FakeLaunchctl,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    envfile.write(layout.env_file, envfile.initial(layout, "/opt/homebrew/opt/postgresql@17/bin"))
    launchctl.loaded = {services.label(s.name) for s in services.SERVICES} - {
        services.label("model-server")
    }
    assert cli.cmd_status(layout, _launchd(layout, launchctl, tmp_path)) == 0
    lines = capsys.readouterr().out.splitlines()[: len(services.SERVICES)]
    assert [line.split()[0] for line in lines] == [s.name for s in services.SERVICES]
    model_server = next(line for line in lines if line.startswith("model-server "))
    assert "stopped" in model_server and model_server.endswith("local AI model server")
    # One column for the state, wide enough for the longest name to come
    # (worker-local-ai): every state starts at the same place.
    columns = {line.index("running" if "running" in line else "stopped") for line in lines}
    assert columns == {17}


# --- the installer's own decisions ------------------------------------------


def _brew(prefix: Path, *, vector_for: tuple[int, ...], installed: tuple[int, ...]) -> None:
    for major in vector_for:
        folder = prefix / "share" / f"postgresql@{major}" / "extension"
        folder.mkdir(parents=True)
        (folder / "vector.control").write_text("")
    for major in installed:
        bin_ = prefix / "opt" / f"postgresql@{major}" / "bin"
        bin_.mkdir(parents=True)
        (bin_ / "postgres").write_text("")


def test_the_oldest_postgres_with_pgvector_is_used(tmp_path: Path) -> None:
    _brew(tmp_path, vector_for=(17, 18), installed=(17, 18))
    assert installer.postgres_majors_with_vector(tmp_path) == [17, 18]
    assert installer.find_pg_bin(tmp_path) == tmp_path / "opt" / "postgresql@17" / "bin"


def test_a_postgres_without_pgvector_is_not_used(tmp_path: Path) -> None:
    _brew(tmp_path, vector_for=(18,), installed=(16, 17))
    assert installer.find_pg_bin(tmp_path) is None  # 18 has the extension but is not installed
    _brew(tmp_path / "other", vector_for=(), installed=(17,))
    assert installer.find_pg_bin(tmp_path / "other") is None


def test_only_missing_tools_are_installed() -> None:
    have = {"node", "npm", "ffmpeg", "ffprobe"}
    # Homebrew has no copy of anything; then, everything is everywhere.
    assert installer.missing_formulae(lambda p: p if p in have else None, lambda _p: None) == [
        "valkey",
        "caddy",
        "exiftool",
        "rsync",
    ]
    assert installer.missing_formulae(lambda p: p, lambda p: p) == []


def _homebrew(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *programs: str) -> Path:
    """A pretend Homebrew, the only one the installer knows of, holding
    `brew` and `programs`. Returns its prefix."""
    prefix = tmp_path / "homebrew"
    (prefix / "bin").mkdir(parents=True)
    for program in ("brew", *programs):
        (prefix / "bin" / program).write_text("")
    monkeypatch.setattr(installer, "BREW_PREFIXES", (prefix,))
    return prefix


def _on_every_mac(program: str) -> str:
    # What `which` says on a Mac where every tool is somewhere on PATH,
    # rsync included: macOS has its own in /usr/bin.
    return f"/usr/bin/{program}"


def test_the_rsync_that_came_with_macos_does_not_count_as_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`which rsync` always answers on a Mac, so the first real install never
    installed Homebrew's and the operator had to. Only a copy under
    Homebrew's prefix counts, and that is the copy the commands then run."""
    prefix = _homebrew(monkeypatch, tmp_path)
    assert installer.missing_formulae(_on_every_mac) == ["rsync"]
    assert installer.rsync() == "rsync"  # nothing better yet: whatever PATH finds

    (prefix / "bin" / "rsync").write_text("")
    assert installer.missing_formulae(_on_every_mac) == []
    assert installer.rsync() == str(prefix / "bin" / "rsync")
    # The one tool macOS has a namesake of. The others are taken from
    # wherever the services' PATH finds them.
    assert installer.SHADOWED_BY_MACOS == {"rsync"} <= set(installer.TOOLS)


def test_install_gets_homebrews_rsync_on_a_mac_that_has_the_systems(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prefix = _homebrew(monkeypatch, tmp_path)
    _brew(prefix, vector_for=(17,), installed=(17,))
    real = installer.missing_formulae
    monkeypatch.setattr(installer, "missing_formulae", lambda: real(_on_every_mac))
    delivers = True
    ran: list[list[str]] = []

    def brew(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        ran.append([str(word) for word in argv])
        if delivers:
            for formula in argv[2:]:
                (prefix / "bin" / formula).write_text("")  # what `brew install rsync` links
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(installer.proc, "run", brew)
    said: list[str] = []

    assert installer.ensure_tools(said.append) == prefix / "opt" / "postgresql@17" / "bin"
    assert ran == [[str(prefix / "bin" / "brew"), "install", "rsync"]]
    assert said == ["Installing with Homebrew: rsync"]
    # Run again, it is there: nothing is installed twice.
    installer.ensure_tools(said.append)
    assert len(ran) == 1

    # A Homebrew that reports success and leaves no rsync is said, not
    # passed over because the system's is on PATH.
    (prefix / "bin" / "rsync").unlink()
    delivers = False
    with pytest.raises(installer.InstallError, match="rsync"):
        installer.ensure_tools(said.append)


def test_a_tool_only_this_terminal_can_find_is_installed_where_the_services_look(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, env: dict[str, str], tmp_path: Path
) -> None:
    """A Terminal with a node of its own: nvm puts one on the shell's PATH.
    launchd's agents are not given that PATH, so counted as installed the web
    service could never start, and the install would still end on "FrameFound
    is running", because only the API is asked. The rsync trap, for the rest."""
    prefix = _homebrew(monkeypatch, tmp_path, "rsync")
    _brew(prefix, vector_for=(17,), installed=(17,))
    where_services_look = set(installer.TOOLS) - {"node", "npm"}
    looked_on: set[str | None] = set()

    def which(program: str, mode: int = 0, path: str | None = None) -> str | None:
        looked_on.add(path)
        if path is None:  # this Terminal's own PATH: everything is on it somewhere
            return f"/Users/me/.nvm/versions/node/v22.11.0/bin/{program}"
        return f"/opt/homebrew/bin/{program}" if program in where_services_look else None

    delivers = True
    ran: list[list[str]] = []

    def brew(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        ran.append([str(word) for word in argv])
        if delivers:
            where_services_look.update({"node", "npm"})  # what `brew install node` links
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(installer.shutil, "which", which)
    monkeypatch.setattr(installer.proc, "run", brew)

    assert installer.missing_formulae() == ["node"]
    installer.ensure_tools(lambda _m: None)
    assert ran == [[str(prefix / "bin" / "brew"), "install", "node"]]
    assert installer.missing_formulae() == []
    # Every lookup was on the PATH a service is started with: Homebrew's
    # folders and the system's, nothing of the operator's own.
    assert looked_on == {services.BASE_PATH}
    assert services.process_environment(layout, env)["PATH"].endswith(f":{services.BASE_PATH}")

    # And "is it there now" is asked the same way: a Homebrew that says it
    # installed node and left none is not passed over for the Terminal's.
    where_services_look -= {"node", "npm"}
    delivers = False
    with pytest.raises(installer.InstallError, match="node"):
        installer.ensure_tools(lambda _m: None)


def _pretend_mac(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, tmp_path: Path
) -> tuple[Path, list[str]]:
    """Everything `installer.install` reaches outside itself, stood in for:
    a Mac with Homebrew on this Terminal's PATH, where every step works.
    Returns the command's path and the slow steps in the order they ran,
    each marked with whether the command was already there."""
    prefix = _homebrew(monkeypatch, tmp_path, "rsync")
    command = prefix / "bin" / "framefound"
    steps: list[str] = []

    def step(name: str, result: Any = None) -> Any:
        def ran(*_args: Any, **_kwargs: Any) -> Any:
            steps.append(f"{name}, {'with' if command.exists() else 'before'} the command")
            return result

        return ran

    monkeypatch.setenv(installer.SHELL_PATH_ENV, str(prefix / "bin"))
    monkeypatch.setattr(installer, "check_platform", lambda _layout: None)
    monkeypatch.setattr(installer, "ensure_tools", step("tools", Path("/pg/bin")))
    monkeypatch.setattr(database, "initialise", step("cluster", False))
    monkeypatch.setattr(installer, "build_web", step("web"))
    monkeypatch.setattr(database, "exists", lambda _env: True)
    monkeypatch.setattr(installer.proc, "wait_for_port", lambda port, seconds: True)
    monkeypatch.setattr(installer, "wait_for_api", lambda _env: True)
    return command, steps


def test_the_command_is_in_place_before_anything_is_started(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    """On the first real install Postgres did not start, the message said to
    run `framefound doctor`, and there was no `framefound`: the command was
    the last step of an install that had stopped."""
    command, steps = _pretend_mac(monkeypatch, layout, tmp_path)
    launchd = _launchd(layout, launchctl, tmp_path)
    monkeypatch.setattr(installer.proc, "wait_for_port", lambda port, seconds: False)

    with pytest.raises(installer.InstallError) as stopped:
        installer.install(layout, launchd, lambda _m: None)

    assert steps == [
        "tools, before the command",  # what it needs first: the venv and the tools
        "cluster, with the command",
        "web, with the command",
    ]
    assert f'exec "{layout.python}" -m framefound.native "$@"' in command.read_text()
    assert "postgres.log" in str(stopped.value)
    assert str(stopped.value).endswith("then: framefound doctor")

    # The same when it is the API that never answers.
    monkeypatch.setattr(installer.proc, "wait_for_port", lambda port, seconds: True)
    monkeypatch.setattr(installer, "wait_for_api", lambda _env: False)
    with pytest.raises(installer.InstallError, match="api.log") as stopped:
        installer.install(layout, launchd, lambda _m: None)
    assert str(stopped.value).endswith("then: framefound doctor")


def test_a_second_install_leaves_the_same_command_and_says_nothing_about_it(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    command, _steps = _pretend_mac(monkeypatch, layout, tmp_path)
    launchd = _launchd(layout, launchctl, tmp_path)
    said: list[str] = []

    installer.install(layout, launchd, said.append)
    first = command.read_bytes()
    installer.install(layout, launchd, said.append)

    assert command.read_bytes() == first
    assert sorted(path.name for path in command.parent.iterdir()) == ["brew", "framefound", "rsync"]
    assert said == ["Starting the services"] * 2


def test_an_install_that_cannot_place_the_command_carries_on_and_names_what_works(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    """Homebrew's bin belongs to another account on a shared Mac. That is no
    reason to stop, and no reason to tell anyone to type `framefound`."""
    command, steps = _pretend_mac(monkeypatch, layout, tmp_path)

    def refused(_layout: Layout, _prefix: Path) -> Path:
        raise PermissionError(13, "Permission denied", str(command))

    monkeypatch.setattr(installer, "install_command", refused)
    monkeypatch.setattr(installer, "wait_for_api", lambda _env: False)
    said: list[str] = []

    with pytest.raises(installer.InstallError) as stopped:
        installer.install(layout, _launchd(layout, launchctl, tmp_path), said.append)

    instead = installer.typed_command(layout)
    assert instead.endswith("-m framefound.native") and "python" in instead
    assert any("could not be put" in line and instead in line for line in said)
    assert steps[-1].startswith("web")  # it went on to build and start everything
    assert str(stopped.value).endswith(f"then: {instead} doctor")
    assert "then: framefound doctor" not in str(stopped.value)


def test_a_terminal_that_does_not_look_in_homebrew_is_given_the_full_path(
    monkeypatch: pytest.MonkeyPatch,
    layout: Layout,
    launchctl: FakeLaunchctl,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The Terminal Homebrew was installed from, before a new window: the
    bootstrap script finds Homebrew anyway, and the command it installs is
    one this Terminal cannot find by name."""
    command, _steps = _pretend_mac(monkeypatch, layout, tmp_path)
    monkeypatch.setenv(installer.SHELL_PATH_ENV, installer.os.pathsep.join(["/usr/bin", "/bin"]))
    monkeypatch.setattr(database, "count", lambda _env, _table: 0)
    args = cli.parser().parse_args(["install"])

    assert cli.cmd_install(layout, _launchd(layout, launchctl, tmp_path), args) == 0

    typed = installer.typed_command(layout)
    assert typed != "framefound" and command.name in typed and str(command.parent) in typed
    out = capsys.readouterr().out
    assert "where this Terminal does not look" in out
    assert f"Check it any time with:  {typed} doctor" in out
    assert f"{typed} migrate --from" in out
    # With Homebrew on the Terminal's PATH (a trailing slash and all), the
    # name is enough and nothing is said about where the command is.
    monkeypatch.setenv(installer.SHELL_PATH_ENV, f"{command.parent}/")
    assert installer.typed_command(layout) == "framefound"
    assert cli.cmd_install(layout, _launchd(layout, launchctl, tmp_path), args) == 0
    out = capsys.readouterr().out
    assert "Check it any time with:  framefound doctor" in out and "does not look" not in out


def test_only_macos_and_not_from_a_protected_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, layout: Layout
) -> None:
    with pytest.raises(installer.InstallError, match="macOS"):
        installer.check_platform(layout, platform="linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    installer.check_platform(
        Layout(home=tmp_path / "FrameFound", source=tmp_path / "src"), "darwin"
    )
    desktop = Layout(home=tmp_path / "FrameFound", source=tmp_path / "Desktop" / "Media Hub")
    with pytest.raises(installer.InstallError, match="Desktop"):
        installer.check_platform(desktop, platform="darwin")


def test_the_queue_config_is_private_and_loopback(layout: Layout, env: dict[str, str]) -> None:
    conf = installer.valkey_conf(layout, env)
    assert "bind 127.0.0.1\n" in conf and f"port {env['REDIS_PORT']}\n" in conf
    assert f"requirepass {env['REDIS_PASSWORD']}\n" in conf
    assert f'dir "{layout.valkey}"\n' in conf  # quoted: a home folder may have spaces
    assert "maxmemory-policy noeviction" in conf


def test_the_front_door_sends_only_api_paths_to_the_api(env: dict[str, str]) -> None:
    caddy = installer.caddyfile(env)
    assert f":{env['FRAMEFOUND_HTTP_PORT']} {{" in caddy
    api_block = caddy[caddy.index("handle /api/*") : caddy.index("handle {")]
    assert f"reverse_proxy 127.0.0.1:{env['FRAMEFOUND_API_PORT']}" in api_block
    assert (
        f"reverse_proxy 127.0.0.1:{env['FRAMEFOUND_WEB_PORT']}" in caddy[caddy.index("handle {") :]
    )
    assert "admin off" in caddy
    # As in the containers' Caddyfile: nobody outside may claim the tunnel.
    assert "request_header -X-FrameFound-Ingress" in caddy
    assert "Content-Security-Policy" in api_block


def test_the_front_door_matches_the_containers_rules() -> None:
    """The native Caddyfile is a second copy of the security posture in
    infrastructure/caddy/Caddyfile; this keeps the two from drifting."""
    original = (REPO / "infrastructure" / "caddy" / "Caddyfile").read_text(encoding="utf-8")
    native = installer.caddyfile({})
    for rule in (
        'X-Content-Type-Options "nosniff"',
        'X-Frame-Options "DENY"',
        "max_size 100MB",
        "@not_docs not path /api/docs* /api/redoc* /api/openapi.json",
        f'Content-Security-Policy "{installer.API_CSP}"',
        "request_header -X-FrameFound-Ingress",
        "request_header -X-FrameFound-Client",
    ):
        assert rule in original, rule
        assert rule in native, rule


def test_homebrew_goes_on_the_front_of_a_bare_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "bin").mkdir()
    monkeypatch.setattr(installer, "BREW_PREFIXES", (tmp_path, tmp_path / "absent"))
    sep = installer.os.pathsep
    assert installer.with_homebrew_on_path(f"/usr/bin{sep}/bin").split(sep) == [
        str(tmp_path / "bin"),
        "/usr/bin",
        "/bin",
    ]
    already = f"{tmp_path / 'bin'}{sep}/usr/bin"
    assert installer.with_homebrew_on_path(already) == already


def test_database_names_are_plain_identifiers() -> None:
    assert database.user({}) == "framefound" and database.name({"POSTGRES_DB": "ff_2"}) == "ff_2"
    for bad in ("ff; DROP TABLE users", "Frame Found", "ff'--", "9lives"):
        with pytest.raises(database.DatabaseError):
            database.name({"POSTGRES_DB": bad})


def test_the_database_password_never_reaches_a_command_line(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    seen: list[tuple[list[str], dict[str, str]]] = []

    def record(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        seen.append(([str(a) for a in argv], dict(kwargs.get("env") or {})))
        return SimpleNamespace(returncode=0, stdout="1\n", stderr="")

    monkeypatch.setattr(database.proc, "run", record)
    assert database.exists(env) is True
    database.dump(env, Path("/tmp/catalog.dump"))  # noqa: S108
    database.restore(env, Path("/tmp/catalog.dump"))  # noqa: S108
    for argv, child in seen:
        assert env["POSTGRES_PASSWORD"] not in " ".join(argv)
        assert child["PGPASSWORD"] == env["POSTGRES_PASSWORD"]
    programs = [Path(argv[0]).name for argv, _ in seen]
    # A restore replaces the database: drop, create, then load.
    assert programs == ["psql", "pg_dump", "dropdb", "createdb", "pg_restore"]


# --- backups -------------------------------------------------------------------


def _backup_inputs(work: Path) -> None:
    work.mkdir()
    (work / "catalog.dump").write_bytes(b"PGDMP-pretend")
    (work / "manifest.json").write_text("{}\n")
    (work / "env").write_text("FRAMEFOUND_SECRET_KEY=abc\n")


def test_an_archive_round_trips_and_is_checked(tmp_path: Path) -> None:
    _backup_inputs(tmp_path / "work")
    archive = tmp_path / "out" / "framefound-20261111T000000Z.tar.gz"
    backup.pack(tmp_path / "work", archive)
    assert not list(archive.parent.glob("*.partial"))

    dump = backup.unpack(archive, tmp_path / "restored")
    assert dump.read_bytes() == b"PGDMP-pretend"
    # The same layout `manage.sh backup` writes, so either side reads both.
    with tarfile.open(archive) as tar:
        assert {"./catalog.dump", "./manifest.json", "./checksums.sha256", "./env"} <= set(
            tar.getnames()
        )
    sums = (tmp_path / "restored" / "checksums.sha256").read_text()
    assert re.fullmatch(r"([0-9a-f]{64}  \S+\n){2}", sums)  # `sha256sum -c` format


def test_a_damaged_archive_is_refused(tmp_path: Path) -> None:
    _backup_inputs(tmp_path / "work")
    (tmp_path / "work" / "checksums.sha256").write_text("0" * 64 + "  catalog.dump\n")
    archive = tmp_path / "framefound-bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for item in (tmp_path / "work").iterdir():
            tar.add(item, arcname=f"./{item.name}")
    with pytest.raises(backup.BackupError, match="damaged"):
        backup.unpack(archive, tmp_path / "restored")
    with tarfile.open(tmp_path / "framefound-other.tar.gz", "w:gz"):
        pass
    with pytest.raises(backup.BackupError, match="not a FrameFound backup"):
        backup.unpack(tmp_path / "framefound-other.tar.gz", tmp_path / "r2")


def test_a_backup_is_due_after_a_day_and_old_ones_are_pruned(tmp_path: Path) -> None:
    assert backup.is_due(tmp_path)  # none yet
    names = [f"framefound-2026110{n}T000000Z.tar.gz" for n in range(1, 6)]
    for age, name in enumerate(reversed(names)):
        path = tmp_path / name
        path.write_bytes(b"x")
        stamp = 1_800_000_000 - age * 86400
        import os

        os.utime(path, (stamp, stamp))
    now = 1_800_000_000 + 3600
    assert not backup.is_due(tmp_path, now)
    assert backup.is_due(tmp_path, now + 86400)
    removed = backup.prune(tmp_path, keep=2)
    assert sorted(p.name for p in removed) == names[:3]
    assert [p.name for p in backup.archives(tmp_path)] == [names[4], names[3]]


def test_a_backup_holds_the_catalogue_and_the_settings(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, env: dict[str, str], tmp_path: Path
) -> None:
    envfile.write(layout.env_file, envfile.initial(layout, "/pg/bin"))
    monkeypatch.setattr(database, "dump", lambda _env, target: target.write_bytes(b"PGDMP"))
    monkeypatch.setattr(database, "schema_version", lambda _env: "0030")
    nas = tmp_path / "nas-backups"
    nas.mkdir()
    env = {**env, "FRAMEFOUND_BACKUP_COPY_DIR": str(nas)}

    archive = backup.run(layout, env)

    assert archive is not None and archive.parent == layout.data / "backups"
    assert (nas / archive.name).is_file()  # the copy off this Mac
    work = tmp_path / "look"
    backup.unpack(archive, work)
    assert (work / "schema_version").read_text() == "0030\n"
    assert "FRAMEFOUND_SECRET_KEY" in (work / "env").read_text()
    # The hourly check does nothing while the newest is fresh.
    assert backup.run(layout, env, if_due=True) is None


def test_a_backup_carries_the_record_of_where_the_bulky_files_live(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, env: dict[str, str], tmp_path: Path
) -> None:
    """A bulk folder named on the Storage page, and every move since, exist
    only in a file in the data folder — which is otherwise rebuildable and
    was in no backup. A catalogue restored onto a new Mac then said every
    proxy and slideshow was ready, and looked for them on its own disk."""
    envfile.write(layout.env_file, envfile.initial(layout, "/pg/bin"))
    monkeypatch.setattr(database, "dump", lambda _env, target: target.write_bytes(b"PGDMP"))
    monkeypatch.setattr(database, "schema_version", lambda _env: "0030")
    monkeypatch.setattr(database, "restore", lambda _env, _dump: None)
    nas, ssd = tmp_path / "nas", tmp_path / "ssd"
    stores.set_bulk_root(layout.data, nas)
    stores.set_override(layout.data, "proxies", ssd)
    stores.pause(layout.data, "renders")  # a move was in its last pass when the backup ran
    here = Settings(data_dir=layout.data)

    archive = backup.run(layout, env)
    assert archive is not None

    # The Mac is replaced: a new data folder, and the catalogue restored.
    record = layout.data / stores.STORES_FILE
    record.unlink()
    stores._CACHE.clear()
    assert stores.kind_root("proxies", here) == layout.data  # the silent wrong answer
    said: list[str] = []
    backup.restore(layout, env, archive, said.append)

    assert stores.kind_root("proxies", here) == ssd
    assert stores.kind_root("renders", here) == nas / "renders"
    assert "not connected" in (stores.kind_problem("renders", here) or "")  # said, not guessed
    assert not stores.read_state(layout.data).is_paused("renders")  # that move is long over
    assert any("framefound storage" in line for line in said)

    # Restored over an install that has a record of its own, that one stands:
    # it says where the files are now, and the backup where they were.
    stores.set_override(layout.data, "proxies", tmp_path / "moved-since")
    said.clear()
    backup.restore(layout, env, archive, said.append)
    assert stores.kind_root("proxies", here) == tmp_path / "moved-since"
    assert any("differs from the backup's" in line for line in said)


def test_an_unreachable_copy_folder_does_not_fail_the_backup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = tmp_path / "framefound-x.tar.gz"
    archive.write_bytes(b"x")
    assert backup.copy_off_machine(archive, tmp_path / "share-not-mounted", keep=3) is False
    assert "skipped" in capsys.readouterr().out


def test_a_share_that_fills_up_mid_copy_does_not_fail_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = tmp_path / "framefound-x.tar.gz"
    archive.write_bytes(b"x")
    share = tmp_path / "share"
    share.mkdir()

    def full(src: Path, dst: Path) -> None:
        Path(dst).write_bytes(b"half")  # what a full share leaves behind
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backup.shutil, "copyfile", full)
    assert backup.copy_off_machine(archive, share, keep=3) is False
    said = capsys.readouterr().out
    assert "No space left" in said and str(archive) in said
    assert list(share.iterdir()) == []  # the half copy is cleared


# --- doctor --------------------------------------------------------------------


def test_the_sleep_timer_is_read_from_pmset() -> None:
    awake = " standby              1\n sleep                0 (sleep prevented by caffeinate)\n"
    assert doctor.parse_sleep_minutes(awake) == 0
    assert doctor.parse_sleep_minutes(" displaysleep         10\n sleep                15\n") == 15
    assert doctor.parse_sleep_minutes("nothing here") is None


def _locale_is_fine(_argv: list[str], **_kwargs: Any) -> SimpleNamespace:
    # What the locale line's probe answers on a Mac that has the locale.
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_a_stopped_service_is_named_with_its_log(
    layout: Layout, env: dict[str, str], launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    launchctl.loaded = {services.label(s.name) for s in services.SERVICES} - {
        services.label("worker-ai")
    }
    launchd = _launchd(layout, launchctl, tmp_path)
    checks = {c.name: c for c in doctor.check_agents(launchd, layout, env, run=_locale_is_fine)}
    assert checks["api"].ok
    assert not checks["worker-ai"].ok and "framefound start" in checks["worker-ai"].detail


def test_framefound_doctor_prints_the_locale_and_rsync_lines(
    monkeypatch: pytest.MonkeyPatch,
    layout: Layout,
    launchctl: FakeLaunchctl,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The two lines the first real install would have needed, in the report
    the operator actually reads: the command, start to finish, with only the
    checks that reach outside this Mac's files stood in for. Each line is
    added inside a function `doctor.run` already called, so nothing but this
    shows that `run` still asks for it."""
    envfile.write(layout.env_file, envfile.initial(layout, "/pg/bin"))
    settings = envfile.read(layout.env_file)
    _homebrew(monkeypatch, tmp_path, "rsync")
    launchctl.loaded = {services.label(s.name) for s in services.SERVICES}
    monkeypatch.setattr(cli, "_adopt", lambda _layout, _env: None)
    for name in (
        "check_front_door",
        "check_stores",
        "check_onnx",
        "check_local_models",
        "check_backups",
        "check_power",
    ):
        monkeypatch.setattr(doctor, name, lambda *_a, **_k: [])
    monkeypatch.setattr(doctor.worker_check, "check_tools", lambda: [])

    async def no_services(*_a: Any, **_k: Any) -> list[doctor.Check]:
        return []

    monkeypatch.setattr(doctor, "check_services", no_services)
    asked: list[dict[str, str]] = []

    def locale_line(_layout: Layout, given: dict[str, str], **_kwargs: Any) -> doctor.Check:
        asked.append(given)
        return doctor.Check("locale", True, "en_US.UTF-8 for every background service")

    monkeypatch.setattr(doctor, "check_locale", locale_line)

    assert cli.cmd_doctor(layout, _launchd(layout, launchctl, tmp_path), parity=False) == 0

    lines = capsys.readouterr().out.splitlines()
    names = [line.split()[1] for line in lines if line.startswith(("ok", "warn", "FAIL"))]
    assert names == [*(s.name for s in services.SERVICES), "locale", "rsync"]
    # The locale is read from this install's own settings, handed all the way
    # down: not from the Terminal doctor is typed in.
    assert asked == [settings]


def test_doctor_tries_the_locale_the_services_are_started_with(
    layout: Layout, env: dict[str, str], launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    tried: list[tuple[list[str], dict[str, str]]] = []
    this_mac_has_it = True

    def probe(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        tried.append(([str(word) for word in argv], dict(kwargs["env"])))
        return SimpleNamespace(returncode=0 if this_mac_has_it else 1, stdout="", stderr="")

    launchd = _launchd(layout, launchctl, tmp_path)
    checks = doctor.check_agents(launchd, layout, env, run=probe)

    # One line more than the services, after them.
    assert [check.name for check in checks] == [s.name for s in services.SERVICES] + ["locale"]
    assert checks[-1].ok and "en_US.UTF-8" in checks[-1].detail
    # Tried by the interpreter the services run through, in what the launcher
    # gives a service and nothing else: no password is handed to a probe.
    argv, given = tried[0]
    assert argv[:2] == [str(layout.python), "-c"] and "setlocale" in argv[2]
    assert given == services.process_environment(layout, env)
    assert not set(given) & {"POSTGRES_PASSWORD", "REDIS_PASSWORD", "FRAMEFOUND_SECRET_KEY"}

    # A Mac that does not have that locale: said, and FrameFound may well
    # still be running, so it is a warning.
    this_mac_has_it = False
    absent = doctor.check_locale(layout, env, run=probe)
    assert not absent.ok and not absent.fatal and "locale -a" in absent.detail


def test_doctor_would_have_caught_services_started_without_a_locale(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, env: dict[str, str]
) -> None:
    """The first real install: every service started with no LANG, Postgres
    would not come up, and nothing typed in a Terminal showed why — a
    Terminal has a LANG of its own."""
    as_it_was = services.process_environment(layout, env)
    del as_it_was["LC_ALL"], as_it_was["LANG"]
    monkeypatch.setattr(services, "process_environment", lambda _layout, _env: as_it_was)
    monkeypatch.setenv("LANG", "en_US.UTF-8")  # the Terminal doctor is typed in

    def nothing_to_try(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("there is no locale to try")

    line = doctor.check_locale(layout, env, run=nothing_to_try)

    assert not line.ok and line.fatal
    assert "no LANG or LC_ALL" in line.detail and "Postgres" in line.detail
    assert str(layout.logs / "postgres.log") in line.detail


def test_doctor_says_when_rsync_is_the_one_that_came_with_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def homebrew_has_it(_program: str) -> str:
        return "/opt/homebrew/bin/rsync"

    def the_systems(_program: str) -> str:
        return "/usr/bin/rsync"

    system = doctor.check_rsync(lambda _program: None, the_systems)
    assert not system.ok and not system.fatal  # only two commands use it
    assert "/usr/bin/rsync is the one that came with macOS" in system.detail
    assert "brew install rsync" in system.detail

    own = doctor.check_rsync(homebrew_has_it, the_systems)
    assert own.ok and own.detail == "/opt/homebrew/bin/rsync"

    none = doctor.check_rsync(lambda _program: None, lambda _program: None)
    assert not none.ok and "not installed" in none.detail

    # In the report it follows the tools every worker needs.
    monkeypatch.setattr(
        doctor.worker_check, "check_tools", lambda: [doctor.Check("ffmpeg", True, "/x/ffmpeg")]
    )
    assert [check.name for check in doctor.check_tools()] == ["ffmpeg", "rsync"]


def test_a_proxy_store_that_is_away_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    data, nas = tmp_path / "data", tmp_path / "nas"
    data.mkdir()
    nas.mkdir()
    checks = {c.name: c for c in doctor.check_stores(Settings(data_dir=data, proxy_dir=nas))}
    assert checks["data store"].ok
    assert not checks["proxy store"].ok and not checks["proxy store"].fatal
    stores.mark_proxy_store(nas)
    checks = {c.name: c for c in doctor.check_stores(Settings(data_dir=data, proxy_dir=nas))}
    assert checks["proxy store"].ok


def test_backups_only_on_this_mac_are_pointed_out(layout: Layout, env: dict[str, str]) -> None:
    checks = {c.name: c for c in doctor.check_backups(layout, env)}
    assert not checks["backup"].ok and not checks["backup"].fatal
    assert "FRAMEFOUND_BACKUP_COPY_DIR" in checks["backup copy"].detail
    assert "--store" in checks["backup copy"].detail  # the simpler way to get one


def test_backup_copies_go_to_the_bulk_folder_without_a_setting_of_their_own(
    layout: Layout, env: dict[str, str], tmp_path: Path
) -> None:
    nas = tmp_path / "nas"
    (nas / "backups").mkdir(parents=True)  # the folder the share mounts on; not the share
    with_store = {**env, "FRAMEFOUND_BULK_DIR": str(nas)}

    away = {c.name: c for c in doctor.check_backups(layout, with_store)}["backup copy"]
    assert not away.ok and not away.fatal and "not connected" in away.detail

    stores.mark_bulk_root(nas)
    there = {c.name: c for c in doctor.check_backups(layout, with_store)}["backup copy"]
    assert there.ok and str(nas / "backups") in there.detail
    # Told otherwise, that folder is used and the bulk one is not.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    told = {**with_store, "FRAMEFOUND_BACKUP_COPY_DIR": str(elsewhere)}
    assert (
        str(elsewhere)
        in {c.name: c for c in doctor.check_backups(layout, told)}["backup copy"].detail
    )


def test_each_kind_on_a_drive_of_its_own_gets_a_line(tmp_path: Path) -> None:
    data, nas = tmp_path / "data", tmp_path / "nas"
    data.mkdir()
    nas.mkdir()
    # With nothing moved, doctor says what it always said.
    plain = [c.name for c in doctor.check_stores(Settings(data_dir=data))]
    assert plain == ["data store", "proxy store"]

    with_store = Settings(data_dir=data, bulk_dir=nas)
    lines = ("proxy store", "slideshow store", "export store", "basemap store")
    checks = {c.name: c for c in doctor.check_stores(with_store)}
    for name in lines:
        # The share is away: what lives there waits, FrameFound is not broken.
        assert not checks[name].ok and not checks[name].fatal, name
        assert "not connected" in checks[name].detail
    assert checks["data store"].ok

    stores.mark_bulk_root(nas)
    checks = {c.name: c for c in doctor.check_stores(with_store)}
    for name in lines:
        assert checks[name].ok and "GB free" in checks[name].detail, name
    assert str(nas / "renders") in checks["slideshow store"].detail


# --- bulk storage: `install --store`, `framefound storage` -------------------------


def _nothing_catalogued(_settings: Settings, _folder: Path) -> storage.Asked:
    return storage.Asked(catalogued=0, moving=None)


def _put(root: Path, relative: str, content: bytes = b"content") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# A move waits ten seconds for the old folder to go quiet. Not in a test.
QUICK: dict[str, Any] = {"settle_s": 0.0, "sleep": lambda _seconds: None}
PROXY = "derivatives/ab/abcd/proxy.mp4"


def test_install_with_a_store_makes_the_bulk_folder_and_records_it(
    layout: Layout, tmp_path: Path
) -> None:
    envfile.write(layout.env_file, envfile.initial(layout, "/pg/bin"))
    settings = Settings(data_dir=layout.data)
    root = tmp_path / "Photos" / "Framefound"
    root.parent.mkdir()
    said: list[str] = []

    storage.use_bulk_folder(layout, settings, root, said.append, ask=_nothing_catalogued)

    assert (root / stores.BULK_STORE_MARKER).is_file()
    for kind in stores.KINDS:
        assert (root / kind / stores.marker_for(kind)).is_file(), kind
    # Recorded where the running services look (no restart), and in the
    # file a person reads.
    assert stores.bulk_root(settings) == root
    assert envfile.read(layout.env_file)["FRAMEFOUND_BULK_DIR"] == str(root)
    # Nothing has been made yet on a new install, so every kind starts there.
    for kind in ("proxies", "renders", "exports", "basemaps", "backups"):
        assert stores.kind_root(kind, settings) == root / kind, kind
        assert stores.kind_problem(kind, settings) is None
    # Proxies keep their layout, so `--proxies <store>/proxies` on an older
    # install and `--store <store>` on a new one are the same place.
    assert stores.derivative_path(layout.data, PROXY, settings) == root / "proxies" / PROXY
    assert not any("storage move" in line for line in said)


def test_a_folder_that_cannot_be_the_store_is_refused_before_anything_is_marked(
    layout: Layout, tmp_path: Path
) -> None:
    envfile.write(layout.env_file, envfile.initial(layout, "/pg/bin"))
    settings = Settings(data_dir=layout.data)
    photos = tmp_path / "Photos"

    def use(folder: Path, asked: storage.Asked) -> None:
        storage.use_bulk_folder(layout, settings, folder, lambda _m: None, ask=lambda _s, _f: asked)

    # The share is not connected: its folder is not made on this Mac's disk.
    with pytest.raises(moves.MoveError, match="Connect the drive"):
        use(photos / "Framefound", storage.Asked(0, None))
    assert not photos.exists()
    photos.mkdir()
    # A folder of catalogued footage: marked, every file in it would be
    # reported missing at the next scan.
    with pytest.raises(moves.MoveError, match="catalogue lists as footage"):
        use(photos / "2026", storage.Asked(12, None))
    # The Storage page is in the middle of a move.
    with pytest.raises(moves.MoveError, match="Storage page"):
        use(photos / "Framefound", storage.Asked(0, "proxies"))

    assert stores.bulk_root(settings) is None
    assert envfile.read(layout.env_file)["FRAMEFOUND_BULK_DIR"] == ""
    assert list(photos.rglob(".framefound-*")) == []


def test_a_store_given_to_an_install_that_has_files_leaves_them_findable(
    layout: Layout, tmp_path: Path
) -> None:
    envfile.write(layout.env_file, envfile.initial(layout, "/pg/bin"))
    settings = Settings(data_dir=layout.data)
    proxy = _put(layout.data, PROXY)
    root = tmp_path / "Photos" / "Framefound"
    root.parent.mkdir()
    said: list[str] = []

    storage.use_bulk_folder(
        layout, settings, root, said.append, ask=_nothing_catalogued, sure=lambda _question: True
    )

    assert stores.derivative_path(layout.data, PROXY, settings) == proxy  # not lost
    assert stores.kind_root("renders", settings) == root / "renders"  # nothing to lose
    # …and the operator is told the one command that brings them over.
    assert any(f'framefound storage move proxies "{root / "proxies"}"' in line for line in said)


def test_a_store_that_looks_like_a_share_that_is_not_connected_is_asked_about_first(
    layout: Layout, tmp_path: Path
) -> None:
    """`install --store` marked the folder and then printed a note. With the
    share away and the folder it mounts on still there, that built the whole
    marked tree on this Mac's disk: every kind "connected", everything on
    the real share missing. The Storage page and `storage move` both asked."""
    envfile.write(layout.env_file, envfile.initial(layout, "/pg/bin"))
    layout.data.mkdir(parents=True)
    settings = Settings(data_dir=layout.data)
    root = tmp_path / "Photos" / "Framefound"
    root.parent.mkdir()  # `/Volumes/Photos`, left behind by a share that is not mounted
    asked: list[str] = []

    def no(question: str) -> bool:
        asked.append(question)
        return False

    def use(sure: Any) -> None:
        storage.use_bulk_folder(
            layout, settings, root, lambda _m: None, ask=_nothing_catalogued, sure=sure
        )

    with pytest.raises(moves.MoveError, match="--yes"):
        use(no)
    assert "same disk" in asked[0] and asked[0].endswith("Use this folder anyway?")
    # Declined: not marked, not recorded, not even made.
    assert not root.exists() and stores.bulk_root(settings) is None
    assert envfile.read(layout.env_file)["FRAMEFOUND_BULK_DIR"] == ""

    use(lambda _question: True)
    assert stores.kind_problem("renders", settings) is None

    # Run again with the share away: the folder is the bulk folder already,
    # and does not look like it. Marking it again is the same mistake.
    import shutil

    shutil.rmtree(root)
    with pytest.raises(moves.MoveError, match="marker file is missing"):
        use(no)
    assert not root.exists()
    assert "not connected" in (stores.kind_problem("renders", settings) or "")


def test_given_a_store_and_a_proxy_folder_the_proxy_folder_wins_for_proxies(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    envfile.write(layout.env_file, envfile.initial(layout, "/pg/bin"))
    monkeypatch.setattr(
        installer, "install", lambda _layout, _launchd, _say: envfile.read(layout.env_file)
    )
    monkeypatch.setattr(database, "count", lambda _env, _table: 1)
    monkeypatch.setattr(storage, "ask_catalogue", _nothing_catalogued)
    monkeypatch.setattr(cli, "say", lambda _message: None)

    def adopt(_layout: Layout, values: dict[str, str]) -> None:
        # What cli._adopt does, undone when the test ends.
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    monkeypatch.setattr(cli, "_adopt", adopt)
    store, ssd = tmp_path / "Photos" / "Framefound", tmp_path / "Fast SSD" / "Proxies"
    store.parent.mkdir()
    ssd.parent.mkdir()
    args = cli.parser().parse_args(["install", "--proxies", str(ssd), "--store", str(store)])

    assert cli.cmd_install(layout, _launchd(layout, launchctl, tmp_path), args) == 0

    written = envfile.read(layout.env_file)
    assert written["FRAMEFOUND_BULK_DIR"] == str(store)
    assert written["FRAMEFOUND_PROXY_DIR"] == str(ssd)
    settings = Settings(data_dir=layout.data, bulk_dir=store, proxy_dir=ssd)
    assert stores.kind_root("proxies", settings) == ssd
    assert stores.kind_problem("proxies", settings) is None
    assert stores.kind_root("renders", settings) == store / "renders"
    assert stores.kind_root("backups", settings) == store / "backups"


def test_a_store_that_cannot_be_set_does_not_read_as_a_failed_install(
    monkeypatch: pytest.MonkeyPatch,
    layout: Layout,
    launchctl: FakeLaunchctl,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The NAS was not connected in Finder. The install worked; only the
    folder did not, and the last thing printed has to say both."""
    envfile.write(layout.env_file, envfile.initial(layout, "/pg/bin"))
    monkeypatch.setattr(
        installer, "install", lambda _layout, _launchd, _say: envfile.read(layout.env_file)
    )
    monkeypatch.setattr(storage, "ask_catalogue", _nothing_catalogued)
    monkeypatch.setattr(cli, "Layout", SimpleNamespace(default=lambda: layout))
    monkeypatch.setattr(cli, "Launchd", lambda _layout: _launchd(layout, launchctl, tmp_path))

    def adopt(_layout: Layout, values: dict[str, str]) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    monkeypatch.setattr(cli, "_adopt", adopt)
    unmounted = tmp_path / "Photos" / "Framefound"

    assert cli.main(["install", "--store", str(unmounted), "--proxies", str(tmp_path / "p")]) == 1

    err = capsys.readouterr().err
    assert "installed and running" in err and "Connect the drive" in err
    assert "Traceback" not in err
    assert envfile.read(layout.env_file)["FRAMEFOUND_BULK_DIR"] == ""
    # …and the proxy folder was not acted on with half the instruction missing.
    assert envfile.read(layout.env_file)["FRAMEFOUND_PROXY_DIR"] == ""
    assert not unmounted.parent.exists()


def test_storage_shows_where_each_kind_lives(layout: Layout, tmp_path: Path) -> None:
    settings = Settings(data_dir=layout.data, models_dir=layout.models)
    _put(layout.data, "renders/show.mp4", b"video")

    _put(layout.models, "hub/clip/model.onnx", b"w" * 2048)

    plain = "\n".join(storage.describe(settings))
    assert "Bulk folder: none" in plain and "--store" in plain
    assert f"renders   {layout.data / 'renders'}   1 files" in plain
    assert "backups   no copy is kept off this Mac" in plain
    # The model library is counted, not the caches in the same folder.
    assert f"models    {layout.models}   0 files, 0 MB in the model library" in plain
    assert "nothing is stored here yet" not in plain

    nas = tmp_path / "nas"
    (nas / "renders").mkdir(parents=True)
    stores.set_override(layout.data, "renders", nas / "renders")  # recorded, but not there
    away = "\n".join(storage.describe(settings))
    assert "NOT CONNECTED" in away and str(nas / "renders") in away

    # With a bulk folder and no model added yet, the library starts there.
    stores.mark_bulk_root(nas / "Framefound")
    stores.set_bulk_root(layout.data, nas / "Framefound")
    _put(nas / "Framefound", "models/library/google/m/1/model.safetensors", b"m" * 10)
    bulky = "\n".join(storage.describe(settings))
    assert f"models    {nas / 'Framefound' / 'models'}   1 files, 0 MB in the model" in bulky
    # A settings file edited by hand afterwards is not what is followed, and
    # the command says so instead of showing the old folder without a word.
    edited = Settings(data_dir=layout.data, models_dir=layout.models, bulk_dir=nas / "Elsewhere")
    told = "\n".join(storage.describe(edited))
    assert f"Bulk folder: {nas / 'Framefound'}" in told
    assert f"FRAMEFOUND_BULK_DIR={nas / 'Elsewhere'}" in told and "not what is followed" in told
    assert "not what is followed" not in bulky


def test_storage_move_copies_checks_and_switches_without_a_restart(
    layout: Layout, tmp_path: Path
) -> None:
    settings = Settings(data_dir=layout.data)
    _put(layout.data, "renders/show.mp4", b"video")
    target = tmp_path / "Fast SSD" / "renders"
    target.parent.mkdir()
    said: list[str] = []

    def move(**more: Any) -> None:
        storage.move(
            settings, "renders", target, said.append, ask=_nothing_catalogued, **QUICK, **more
        )

    # A folder on the disk FrameFound's data is on is asked about, because
    # that is what a drive that is not connected looks like.
    with pytest.raises(moves.MoveError, match="Nothing was changed"):
        move()
    assert stores.kind_root("renders", settings) == layout.data / "renders"
    assert not target.exists()  # and a no leaves no folder where the drive mounts

    move(sure=lambda _question: True)

    assert (target / "show.mp4").read_bytes() == b"video"
    assert stores.kind_root("renders", settings) == target
    # Nothing is deleted, and the operator is told where the old copies are.
    assert (layout.data / "renders" / "show.mp4").is_file()
    assert any("old copies are still in" in line for line in said)
    assert any("renders" in line and "old copies" in line for line in storage.describe(settings))


def test_storage_move_works_out_a_roundabout_path_before_comparing_it(
    layout: Layout, tmp_path: Path
) -> None:
    """Only the page did. From the command line `/data/renders/../elsewhere`
    read as somewhere outside the data folder, and a kind's own folder spelt
    the long way round read as a different folder."""
    settings = Settings(data_dir=layout.data)
    _put(layout.data, "renders/show.mp4", b"video")
    (tmp_path / "Fast SSD").mkdir()

    def move(target: Path) -> None:
        storage.move(
            settings,
            "renders",
            target,
            lambda _m: None,
            sure=lambda _q: True,
            ask=_nothing_catalogued,
            **QUICK,
        )

    with pytest.raises(moves.MoveError, match="inside FrameFound's own data folder"):
        move(layout.data / "renders" / ".." / "elsewhere")

    move(tmp_path / "Fast SSD" / ".." / "Fast SSD" / "renders")
    assert stores.kind_root("renders", settings) == tmp_path / "Fast SSD" / "renders"
    move(tmp_path / "Fast SSD" / "renders" / ".")  # the same folder again: nothing to do
    assert stores.read_state(layout.data).previous == {"renders": layout.data / "renders"}
    assert (layout.data / "renders" / "show.mp4").is_file()


def test_storage_move_is_refused_while_the_storage_page_is_moving_something(
    layout: Layout, tmp_path: Path
) -> None:
    """Two moves at once would each lift the pause the other relies on."""
    settings = Settings(data_dir=layout.data)
    (tmp_path / "ssd").mkdir()
    with pytest.raises(moves.MoveError, match="being moved from the Storage page"):
        storage.move(
            settings,
            "exports",
            tmp_path / "ssd" / "exports",
            lambda _m: None,
            sure=lambda _q: True,
            ask=lambda _s, _f: storage.Asked(0, "proxies"),
            **QUICK,
        )
    assert stores.kind_root("exports", settings) == layout.data / "exports"


def test_the_newest_instruction_for_proxies_wins_whichever_command_gave_it(
    layout: Layout, tmp_path: Path
) -> None:
    """`framefound proxies move` writes a setting; `framefound storage move
    proxies` writes the record that outranks it. (The other order is in
    test_proxy_store.py.)"""
    first, second = tmp_path / "first", tmp_path / "second"
    stores.mark_proxy_store(first)  # where `proxies move` left them
    _put(first, PROXY, b"proxy")
    settings = Settings(data_dir=layout.data, proxy_dir=first)

    storage.move(
        settings,
        "proxies",
        second,
        lambda _m: None,
        sure=lambda _q: True,
        ask=_nothing_catalogued,
        **QUICK,
    )

    assert stores.proxy_root(layout.data, settings) == second
    assert (second / PROXY).read_bytes() == b"proxy"


def test_the_catalogue_is_asked_before_a_folder_is_marked(tmp_path: Path) -> None:
    """Without a network: the same question, put to a catalogue in a file."""
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from framefound.db.base import Base
    from framefound.storage import move_job

    url = f"sqlite+aiosqlite:///{(tmp_path / 'catalogue.db').as_posix()}"
    settings = Settings(data_dir=tmp_path / "data", database_url=url)

    # No catalogue to ask: refused, in words, rather than marked unasked.
    with pytest.raises(moves.MoveError, match="could not be asked"):
        storage.ask_catalogue(settings, tmp_path / "nas")

    async def prepare() -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            running = move_job.new(
                "proxies", tmp_path / "nas", source=None, mode="copy", requested_by="x"
            )
            running.phase = "copying"
            await move_job.save(db, running)
        await engine.dispose()

    asyncio.run(prepare())
    assert storage.ask_catalogue(settings, tmp_path / "nas") == storage.Asked(0, "proxies")


# --- the command -----------------------------------------------------------------


def test_every_documented_command_parses() -> None:
    parse = cli.parser().parse_args
    assert parse(["install", "--proxies", "/Volumes/x/p"]).proxies == "/Volumes/x/p"
    assert parse(["restart", "worker-media"]).service == "worker-media"
    assert parse(["restart", "model-server"]).service == "model-server"
    assert parse(["doctor", "--parity"]).parity is True
    assert parse(["proxies"]).proxies_command is None
    assert parse(["proxies", "move", "/Volumes/x", "--empty"]).empty is True
    stored = parse(["install", "--store", "/Volumes/Photos/Framefound"])
    assert stored.store == "/Volumes/Photos/Framefound" and stored.proxies is None
    assert parse(["storage"]).storage_command is None
    moved = parse(["storage", "move", "renders", "/Volumes/Fast SSD/renders", "--empty"])
    assert (moved.kind, moved.empty, moved.yes) == ("renders", True, False)
    with pytest.raises(SystemExit):
        parse(["storage", "move", "thumbnails", "/Volumes/x"])  # not a kind that moves
    migrate_args = parse(
        ["migrate", "--from", "framefound@192.168.1.193", "--map", "/media/a=/Volumes/b"]
    )
    assert migrate_args.source == "framefound@192.168.1.193"
    assert migrate_args.map == ["/media/a=/Volumes/b"] and migrate_args.source_dir == "framefound"
    tool = parse(["ops", "pair_proxies", "--apply", "--library", "GELCO"])
    assert tool.tool == "pair_proxies" and tool.rest == ["--apply", "--library", "GELCO"]
    with pytest.raises(SystemExit):
        parse(["restart", "not-a-service"])


def test_commands_in_the_help_text_exist() -> None:
    documented = set(re.findall(r"^framefound (\w+)", cli.__doc__ or "", re.MULTILINE))
    assert len(documented) > 10  # the pattern still matches the help text
    known = set(cli.parser()._subparsers._group_actions[0].choices)  # type: ignore[union-attr]
    assert documented <= known and "run" in known and "run" not in documented


def test_a_service_with_nothing_to_run_from_says_so(
    layout: Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.run_service(layout, "api") == cli.EX_CONFIG  # no framefound.env
    assert "not installed" in capsys.readouterr().err


def test_using_it_before_installing_is_one_plain_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FRAMEFOUND_HOME", str(tmp_path / "nothing-here"))
    monkeypatch.setattr("os.getuid", lambda: 501, raising=False)
    assert cli.main(["status"]) == 1
    err = capsys.readouterr().err
    assert "not installed" in err and "Traceback" not in err


def test_the_storage_command_reaches_its_functions_and_fails_in_one_line(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[Any] = []

    def show(_layout: Layout) -> int:
        seen.append("show")
        return 0

    def move(_layout: Layout, kind: str, target: Path, *, empty: bool, yes: bool) -> int:
        seen.append((kind, target, empty, yes))
        return 0

    monkeypatch.setattr(cli, "cmd_storage_show", show)
    monkeypatch.setattr(cli, "cmd_storage_move", move)
    parse = cli.parser().parse_args
    launchd: Any = None
    assert cli.dispatch(parse(["storage"]), layout, launchd) == 0
    assert (
        cli.dispatch(parse(["storage", "move", "exports", "/x/y", "--yes"]), layout, launchd) == 0
    )
    assert seen == ["show", ("exports", Path("/x/y"), False, True)]

    # A folder that will not do is a sentence, not a traceback.
    def refused(*_args: Any, **_kwargs: Any) -> int:
        raise moves.MoveError("/x/y already has files in it that FrameFound did not put there.")

    monkeypatch.setattr(cli, "cmd_storage_move", refused)
    monkeypatch.setattr("os.getuid", lambda: 501, raising=False)
    assert cli.main(["storage", "move", "exports", "/x/y"]) == 1
    err = capsys.readouterr().err
    assert "did not put there" in err and "Traceback" not in err


# --- `framefound update` when the checkout is not as the release left it --------


def _git_answers(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, tuple[int, str]]
) -> list[list[str]]:
    """Stand in for every program `update` runs. git answers by its words
    after `git -C <checkout>` (exit code, output), 0 and nothing otherwise.
    Returns what was run."""
    ran: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        words = [str(word) for word in argv]
        ran.append(words)
        code, out = answers.get(" ".join(words[3:]), (0, "")) if words[0] == "git" else (0, "")
        return SimpleNamespace(returncode=code, stdout=out, stderr="")

    monkeypatch.setattr(cli.proc, "run", run)
    # The backup an update takes first is the subject of its own tests
    # (below, `_backups`); these are about git, so it is taken as given.
    monkeypatch.setattr(cli, "_backup_first", lambda _layout: None)
    return ran


def _only_read(ran: list[list[str]]) -> bool:
    # Every program run was git, and no git command that changes a checkout.
    return all(words[0] == "git" and words[3] in ("pull", "diff", "rev-list") for words in ran)


def test_an_update_stopped_by_an_edit_made_on_this_mac_says_which_and_discards_nothing(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Mac that found the locale bug has the fix as an edit to
    services.py, and the release changes services.py. git refuses to
    overwrite it; `update` used to end on "git failed (exit 1)".

    This is what an install says from this release on. It is NOT what that
    Mac says on its next update: the command that runs there is the release
    it already has, and this code arrives with the very pull git refuses.
    That one Mac has to be cleared by hand first (the hand-over says how)."""
    edited = "apps/server/framefound/native/services.py"
    ran = _git_answers(
        monkeypatch,
        {
            "pull --ff-only": (1, ""),
            # Two files edited here; the release changes one of them.
            "diff --name-only HEAD": (0, f"{edited}\nnotes-to-self.txt\n"),
            "diff --name-only HEAD...@{upstream}": (0, f"apps/web/package.json\n{edited}\n"),
        },
    )
    monkeypatch.setattr(cli.os, "execv", lambda *_args: pytest.fail("install was handed over to"))
    monkeypatch.setattr(cli, "Layout", SimpleNamespace(default=lambda: layout))
    monkeypatch.setattr("os.getuid", lambda: 501, raising=False)

    assert cli.main(["update"]) == 1

    err = capsys.readouterr().err
    assert "Traceback" not in err and "git failed" not in err
    assert "before anything was changed" in err
    assert edited in err and "notes-to-self.txt" not in err and "package.json" not in err
    # How to clear it, without losing the edit: set it aside, update, read it
    # again, then drop it. The commands are for the operator to run, not run
    # for them.
    for step in ("stash\n", "update\n", "stash show -p\n", "stash drop\n"):
        assert step in err, step
    # Putting the edit back with `stash pop` is not one of the steps: the
    # release changed the same file, and a clash leaves git's markers in code
    # every service imports. It is named only to warn, with the way back,
    # which is `checkout HEAD --` (git refuses a plain `checkout --` there).
    commands = [line.strip() for line in err.splitlines() if line.startswith("  git ")]
    assert not any(command.endswith("stash pop") for command in commands)
    assert "Do not put the edits back with `git stash pop`" in err
    assert commands[-1].endswith(f"checkout HEAD -- {edited}")
    assert _only_read(ran) and ran[0][3:] == ["pull", "--ff-only"]


AHEAD, BEHIND = "rev-list --count @{upstream}..HEAD", "rev-list --count HEAD..@{upstream}"


def test_an_update_stopped_for_another_reason_says_what_it_can(
    monkeypatch: pytest.MonkeyPatch, layout: Layout
) -> None:
    monkeypatch.setattr(cli.os, "execv", lambda *_args: pytest.fail("install was handed over to"))
    # Changes committed on this Mac, and a release that has moved on without
    # them: the checkout cannot simply move forward.
    ran = _git_answers(
        monkeypatch, {"pull --ff-only": (128, ""), AHEAD: (0, "2\n"), BEHIND: (0, "5\n")}
    )
    with pytest.raises(installer.InstallError) as stopped:
        cli.cmd_update(layout)
    assert "2 changes of its own" in str(stopped.value) and "pull --rebase" in str(stopped.value)
    assert _only_read(ran)

    # Anything else (no network, say): git's own words are on the screen
    # already, and an edit the release does not touch is not blamed for it.
    ran = _git_answers(
        monkeypatch,
        {
            "pull --ff-only": (1, ""),
            "diff --name-only HEAD": (0, "notes-to-self.txt\n"),
            "diff --name-only HEAD...@{upstream}": (128, ""),
            AHEAD: (0, "0\n"),
        },
    )
    with pytest.raises(installer.InstallError) as stopped:
        cli.cmd_update(layout)
    assert "its own message is above" in str(stopped.value)
    assert "notes-to-self.txt" not in str(stopped.value) and "stash" not in str(stopped.value)
    assert _only_read(ran)


def test_a_failed_update_does_not_blame_commits_that_were_not_in_the_way(
    monkeypatch: pytest.MonkeyPatch, layout: Layout
) -> None:
    """The fix committed on this Mac (git's own advice: "commit your changes
    or stash them"), and later an update with no network. Ahead of the
    release and not behind it, a pull has nothing to do and succeeds, so the
    commit cannot be why this one failed. Blaming it sent the operator to
    `git pull --rebase`, which fails the same way."""
    monkeypatch.setattr(cli.os, "execv", lambda *_args: pytest.fail("install was handed over to"))
    for behind in ((0, "0\n"), (128, "")):  # nothing new known of; or git cannot say
        ran = _git_answers(
            monkeypatch, {"pull --ff-only": (128, ""), AHEAD: (0, "1\n"), BEHIND: behind}
        )
        with pytest.raises(installer.InstallError) as stopped:
            cli.cmd_update(layout)
        message = str(stopped.value)
        assert "its own message is above" in message and "running as it was" in message
        assert "of its own" not in message and "rebase" not in message
        assert _only_read(ran)
        assert [words[3:] for words in ran[-2:]] == [AHEAD.split(), BEHIND.split()]


def test_an_update_git_allows_installs_the_release_and_hands_over_to_it(
    monkeypatch: pytest.MonkeyPatch, layout: Layout
) -> None:
    ran = _git_answers(monkeypatch, {})
    handed: list[list[str]] = []
    monkeypatch.setattr(cli.os, "execv", lambda _program, argv: handed.append(argv))

    assert cli.cmd_update(layout) == 0

    assert [words[0] for words in ran] == ["git", "uv"]  # nothing asked of git but the pull
    assert f"{layout.server_source}[{cli.EXTRAS}]" in ran[1]
    assert handed == [[str(layout.python), "-m", "framefound.native", "install"]]


# The real step, kept before `_git_answers` stands in for it.
BACKUP_FIRST = cli._backup_first


def _backups(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, ran: list[list[str]], fails: str = ""
) -> None:
    """An installed Mac whose catalogue dump is written into `ran` where it
    happens (after `_git_answers`), and fails with `fails` when given. The
    rest of `framefound backup` runs for real."""
    envfile.write(layout.env_file, envfile.initial(layout, "/pg/bin"))

    def dump(_env: dict[str, str], target: Path) -> None:
        ran.append(["pg_dump"])
        if fails:
            raise cli.proc.CommandFailed(fails)
        target.write_bytes(b"PGDMP")

    monkeypatch.setattr(database, "dump", dump)
    monkeypatch.setattr(database, "schema_version", lambda _env: "0036")
    monkeypatch.setattr(cli, "_backup_first", BACKUP_FIRST)


def test_an_update_backs_up_the_catalogue_before_anything_is_pulled(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    """The release an update hands over to may migrate the catalogue; an
    archive taken just before is the way back from a migration that goes
    wrong on real data."""
    ran = _git_answers(monkeypatch, {})
    _backups(monkeypatch, layout, ran)
    handed: list[list[str]] = []
    monkeypatch.setattr(cli.os, "execv", lambda _program, argv: handed.append(argv))
    monkeypatch.setattr(cli, "Layout", SimpleNamespace(default=lambda: layout))

    assert cli.main(["update"]) == 0

    assert [words[0] for words in ran] == ["pg_dump", "git", "uv"]
    [archive] = backup.archives(layout.data / "backups")
    assert f"Backup written: {archive}" in capsys.readouterr().out
    assert handed == [[str(layout.python), "-m", "framefound.native", "install"]]


def test_a_backup_that_fails_stops_the_update_and_says_how_to_go_on_without_one(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    ran = _git_answers(monkeypatch, {})
    _backups(monkeypatch, layout, ran, fails="pg_dump failed (exit 1): connection refused")
    monkeypatch.setattr(cli.os, "execv", lambda *_args: pytest.fail("install was handed over to"))
    monkeypatch.setattr(cli, "Layout", SimpleNamespace(default=lambda: layout))

    assert cli.main(["update"]) == 1

    assert ran == [["pg_dump"]]  # nothing pulled, nothing installed
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert err.startswith(
        "framefound: The update stopped before anything was changed: the backup it takes first "
        "failed (The database could not be dumped: pg_dump failed (exit 1): connection refused)."
    )
    assert err.rstrip().endswith(f"{installer.typed_command(layout)} update --skip-backup")
    assert backup.archives(layout.data / "backups") == []


def test_a_copy_off_this_mac_that_fails_still_updates_and_names_the_archive(
    monkeypatch: pytest.MonkeyPatch,
    layout: Layout,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The NAS that holds the off-machine copies fills up mid-copy. The
    archive on this Mac was already written, so the update must not be
    stopped as "the backup failed" and pointed at --skip-backup."""
    ran = _git_answers(monkeypatch, {})
    _backups(monkeypatch, layout, ran)
    nas = tmp_path / "nas-backups"
    nas.mkdir()
    envfile.update(layout.env_file, {"FRAMEFOUND_BACKUP_COPY_DIR": str(nas)})
    real_copy = backup.shutil.copyfile

    def copyfile(source: Path, target: Path) -> Path:
        if Path(target).parent == nas:
            Path(target).write_bytes(b"half")  # what a copy cut off leaves
            raise OSError(28, "No space left on device")
        return Path(real_copy(source, target))

    monkeypatch.setattr(backup.shutil, "copyfile", copyfile)
    handed: list[list[str]] = []
    monkeypatch.setattr(cli.os, "execv", lambda _program, argv: handed.append(argv))
    monkeypatch.setattr(cli, "Layout", SimpleNamespace(default=lambda: layout))

    assert cli.main(["update"]) == 0

    assert [words[0] for words in ran] == ["pg_dump", "git", "uv"]
    [archive] = backup.archives(layout.data / "backups")
    out = capsys.readouterr().out
    assert f"Backup written: {archive}" in out
    # backup.py says it now; the update itself carries on.
    assert f"the copy to {nas} failed" in out and "No space left on device" in out
    assert "--skip-backup" not in out
    assert list(nas.iterdir()) == []  # the half copy was cleared
    assert handed == [[str(layout.python), "-m", "framefound.native", "install"]]


def test_skip_backup_updates_without_one(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    ran = _git_answers(monkeypatch, {})
    _backups(monkeypatch, layout, ran, fails="pg_dump failed (exit 1): connection refused")
    handed: list[list[str]] = []
    monkeypatch.setattr(cli.os, "execv", lambda _program, argv: handed.append(argv))
    monkeypatch.setattr(cli, "Layout", SimpleNamespace(default=lambda: layout))

    assert cli.main(["update", "--skip-backup"]) == 0

    assert [words[0] for words in ran] == ["git", "uv"]
    assert "Backup" not in capsys.readouterr().out
    assert handed == [[str(layout.python), "-m", "framefound.native", "install"]]


def test_a_command_typed_by_its_full_path_still_knows_what_this_terminal_can_find(
    monkeypatch: pytest.MonkeyPatch,
    layout: Layout,
    launchctl: FakeLaunchctl,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The Terminal Homebrew was installed from, after the bootstrap script
    has finished: the operator types the full path it gave them. Nothing has
    recorded this Terminal's PATH for that run, and the command puts Homebrew
    on its own PATH before it does anything, so asked there it would always
    answer "just type framefound": the one thing this Terminal cannot do."""
    command, _steps = _pretend_mac(monkeypatch, layout, tmp_path)
    full_path = shlex.quote(str(command))
    sep = os.pathsep
    monkeypatch.setattr(database, "count", lambda _env, _table: 0)
    monkeypatch.setattr(cli, "Layout", SimpleNamespace(default=lambda: layout))
    monkeypatch.setattr(cli, "Launchd", lambda _layout: _launchd(layout, launchctl, tmp_path))
    monkeypatch.setattr(cli.os, "execv", lambda *_args: pytest.fail("install was handed over to"))
    _git_answers(monkeypatch, {"pull --ff-only": (1, "")})

    def typed_in(path: str, *argv: str) -> tuple[int, str]:
        """Run the command as typed in a Terminal with this PATH and no
        bootstrap script before it. Returns the exit code and all it said."""
        monkeypatch.delenv(installer.SHELL_PATH_ENV, raising=False)
        monkeypatch.setenv("PATH", path)
        code = cli.main(list(argv))
        # Homebrew is on the command's own PATH by now, whatever the Terminal's.
        assert str(command.parent) in os.environ["PATH"].split(sep)
        said = capsys.readouterr()
        return code, said.out + said.err

    without_homebrew = sep.join(["/usr/bin", "/bin"])
    code, said = typed_in(without_homebrew, "install")
    assert code == 0
    assert "where this Terminal does not look" in said
    assert f"Check it any time with:  {full_path} doctor" in said
    assert "framefound doctor" not in said.replace(full_path, "")
    # The same for the sentence a refused update ends on…
    code, said = typed_in(without_homebrew, "update")
    assert code == 1 and said.rstrip().endswith(f"then:  {full_path} update")
    # …and for the install an update becomes, which is a new process started
    # by this one: what was recorded goes with it, where the PATH it inherits
    # already has Homebrew on it.
    assert os.environ[installer.SHELL_PATH_ENV] == without_homebrew

    # A Terminal that does look in Homebrew's bin is told the bare name.
    code, said = typed_in(sep.join(["/usr/bin", str(command.parent)]), "install")
    assert code == 0 and "does not look" not in said
    assert "Check it any time with:  framefound doctor" in said

    # What the bootstrap script recorded is not replaced by the PATH it then
    # changed; and a service, which launchd starts, has no Terminal to record.
    monkeypatch.setenv(installer.SHELL_PATH_ENV, without_homebrew)
    assert cli.main(["install"]) == 0
    assert f"Check it any time with:  {full_path} doctor" in capsys.readouterr().out
    monkeypatch.delenv(installer.SHELL_PATH_ENV)
    assert cli.main(["run", "no-such-service"]) == cli.EX_CONFIG
    assert installer.SHELL_PATH_ENV not in os.environ


def test_the_bootstrap_script_tells_the_installer_this_terminals_own_path() -> None:
    script = (REPO / "infrastructure" / "mac" / "install.sh").read_text(encoding="utf-8")
    told = script.index(f'export {installer.SHELL_PATH_ENV}="$PATH"')
    # Before Homebrew is put in front of it: afterwards it would always say
    # the command can be found by name.
    assert told < script.index('PATH="$prefix/bin:$PATH"')


def test_the_bootstrap_script_hands_over_to_the_installer() -> None:
    script = (REPO / "infrastructure" / "mac" / "install.sh").read_text(encoding="utf-8")
    assert f'EXTRAS="{cli.EXTRAS}"' in script  # the same extras `framefound update` installs
    assert 'exec "$HOME_DIR/venv/bin/python" -m framefound.native install "$@"' in script
    assert "sudo" not in script.replace("nothing needs sudo", "")
    assert "--store" in script and "--proxies" in script  # both ways in are documented
