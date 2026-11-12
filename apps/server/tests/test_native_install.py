"""FrameFound as a program on a Mac (framefound/native, docs/mac.md).

Everything here runs on any platform: the package keeps each outside effect
(launchctl, Homebrew's binaries) behind an argv list, and these tests read
the lists. What they cannot show is the commands themselves working on a
Mac — that is `framefound doctor`'s job on the machine."""

import plistlib
import re
import tarfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from framefound.config import Settings, get_settings
from framefound.native import backup, cli, database, doctor, envfile, installer, services
from framefound.native.launchd import AgentState, Launchd, parse_print
from framefound.native.layout import Layout
from framefound.storage import stores

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
    yield
    get_settings.cache_clear()


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
    assert loaded.postgres_port == int(env["POSTGRES_PORT"])
    assert loaded.redis_url == (f"redis://:{env['REDIS_PASSWORD']}@127.0.0.1:{env['REDIS_PORT']}/0")


def test_a_queue_password_is_escaped_in_the_url() -> None:
    assert Settings(REDIS_PASSWORD="p@ss/w:rd").redis_url == (  # type: ignore[call-arg]
        "redis://:p%40ss%2Fw%3Ard@redis:6379/0"
    )
    assert Settings().redis_url == "redis://redis:6379/0"  # the containers: unchanged


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
    for queues in re.findall(r'"-Q", "([a-z,]+)"', compose):
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
    assert installer.missing_formulae(lambda p: p if p in have else None) == [
        "valkey",
        "caddy",
        "exiftool",
        "rsync",
    ]
    assert installer.missing_formulae(lambda p: p) == []


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


def test_an_unreachable_copy_folder_does_not_fail_the_backup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = tmp_path / "framefound-x.tar.gz"
    archive.write_bytes(b"x")
    assert backup.copy_off_machine(archive, tmp_path / "share-not-mounted", keep=3) is False
    assert "skipped" in capsys.readouterr().out


# --- doctor --------------------------------------------------------------------


def test_the_sleep_timer_is_read_from_pmset() -> None:
    awake = " standby              1\n sleep                0 (sleep prevented by caffeinate)\n"
    assert doctor.parse_sleep_minutes(awake) == 0
    assert doctor.parse_sleep_minutes(" displaysleep         10\n sleep                15\n") == 15
    assert doctor.parse_sleep_minutes("nothing here") is None


def test_a_stopped_service_is_named_with_its_log(
    layout: Layout, launchctl: FakeLaunchctl, tmp_path: Path
) -> None:
    launchctl.loaded = {services.label(s.name) for s in services.SERVICES} - {
        services.label("worker-ai")
    }
    checks = {c.name: c for c in doctor.check_agents(_launchd(layout, launchctl, tmp_path), layout)}
    assert checks["api"].ok
    assert not checks["worker-ai"].ok and "framefound start" in checks["worker-ai"].detail


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


# --- the command -----------------------------------------------------------------


def test_every_documented_command_parses() -> None:
    parse = cli.parser().parse_args
    assert parse(["install", "--proxies", "/Volumes/x/p"]).proxies == "/Volumes/x/p"
    assert parse(["restart", "worker-media"]).service == "worker-media"
    assert parse(["doctor", "--parity"]).parity is True
    assert parse(["proxies"]).proxies_command is None
    assert parse(["proxies", "move", "/Volumes/x", "--empty"]).empty is True
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


def test_the_bootstrap_script_hands_over_to_the_installer() -> None:
    script = (REPO / "infrastructure" / "mac" / "install.sh").read_text(encoding="utf-8")
    assert f'EXTRAS="{cli.EXTRAS}"' in script  # the same extras `framefound update` installs
    assert 'exec "$HOME_DIR/venv/bin/python" -m framefound.native install "$@"' in script
    assert "sudo" not in script.replace("nothing needs sudo", "")
