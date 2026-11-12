"""`framefound migrate --from user@host` (framefound/native/migrate.py).

The old server is a fake `proc.run` that answers the SSH commands a real
one would, and records every command in order. What is tested is the
orchestration — what is refused before anything changes, what order things
happen in, what is carried and what is left — which is where a migration
goes wrong. The commands themselves (ssh, rsync, pg_dump) need the machines.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from framefound.native import database, envfile, installer, migrate, proc
from framefound.native.layout import Layout
from framefound.native.migrate import MigrationError, Options, RootPlan, Source

REPO = Path(__file__).resolve().parents[3]

OLD_ENV = """# the VM's .env
FRAMEFOUND_SECRET_KEY=old-server-secret
FRAMEFOUND_SERVER_NAME=Six Gricks
FRAMEFOUND_WHISPER_MODEL=base
FRAMEFOUND_DATA_STORE=/mnt/ffdata/framefound-data
FRAMEFOUND_MEDIA_ROOT=/mnt/media
POSTGRES_USER=framefound
POSTGRES_PASSWORD=old-db-password
POSTGRES_DB=framefound
COMPOSE_FILE=docker-compose.yml:docker-compose.braw.yml
"""


class OldServer:
    """Answers the commands `migrate` sends over SSH; records everything."""

    def __init__(self, roots: list[str], env_text: str = OLD_ENV, schema: str = "0029") -> None:
        self.roots = roots
        self.env_text = env_text
        self.schema = schema
        self.log: list[str] = []
        self.dump = b"PGDMP-old-catalogue"

    def __call__(self, argv: list[str], **kwargs: Any) -> SimpleNamespace:
        words = [str(word) for word in argv]
        out = ""
        if words[0] == "ssh":
            remote = words[-1]
            if "-fN" in words:
                # The shared connection is opened with the terminal attached:
                # captured, its master would hold the pipe and hang the run.
                assert not kwargs.get("capture")
                self.log.append("connect")
            elif "-O" in words:
                self.log.append("disconnect")
            elif "cat .env" in remote:
                self.log.append("read settings")
                out = self.env_text
            elif "root_path FROM libraries" in remote:
                self.log.append("read libraries")
                out = "\n".join(self.roots) + "\n"
            elif "alembic_version" in remote:
                out = self.schema + "\n"
            elif "docker compose stop" in remote:
                self.log.append("stop old app")
            elif "pg_dump" in remote:
                self.log.append("dump")
                kwargs["stdout"].write(self.dump)
            else:  # pragma: no cover - a command the fake does not know
                raise AssertionError(remote)
        elif Path(words[0]).name == "rsync":
            self.log.append("copy proxies" if "--include=proxy.*" in words else "copy data")
        else:  # pragma: no cover
            raise AssertionError(words)
        return SimpleNamespace(returncode=0, stdout=out, stderr="")


class FakeLaunchd:
    def __init__(self, log: list[str]) -> None:
        self.log = log


@pytest.fixture()
def here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A fresh native install with every local effect recorded."""
    layout = Layout(home=tmp_path / "FrameFound", source=REPO)
    envfile.write(layout.env_file, envfile.initial(layout, "/pg/bin"))
    env = envfile.read(layout.env_file)
    volumes = tmp_path / "Volumes"
    (volumes / "intel" / "2026").mkdir(parents=True)
    (volumes / "intel" / "2026" / "clip.mov").write_bytes(b"x")
    state = SimpleNamespace(
        layout=layout, env=env, volumes=volumes, users=0, sql=[], restored=None, log=[]
    )

    def restore(_env: dict[str, str], dump: Path) -> None:
        state.restored = dump.read_bytes()
        state.log.append("restore")

    monkeypatch.setattr(database, "count", lambda _env, _table: state.users)
    monkeypatch.setattr(database, "restore", restore)
    monkeypatch.setattr(database, "psql", lambda _env, sql, *_a: state.sql.append(sql) or "")
    monkeypatch.setattr(
        installer, "stop_all", lambda _launchd, _names=None: state.log.append("stop here")
    )
    monkeypatch.setattr(installer, "start_all", lambda *_a, **_k: state.log.append("start here"))
    monkeypatch.setattr(installer, "wait_for_api", lambda _env: True)
    return state


def run(
    here: SimpleNamespace,
    server: OldServer,
    monkeypatch: pytest.MonkeyPatch,
    *,
    answer: bool = True,
    proxy_problem: str | None = None,
    **options: Any,
) -> list[str]:
    server.log = here.log  # one timeline for both machines
    monkeypatch.setattr(proc, "run", server)
    said: list[str] = []
    migrate.migrate(
        here.layout,
        here.env,
        FakeLaunchd(here.log),  # type: ignore[arg-type]
        Options(source="framefound@vm", volumes=here.volumes, **options),
        proxy_root=here.layout.home / "proxies",
        proxy_problem=proxy_problem,
        say=said.append,
        confirm=lambda _question: answer,
    )
    return said


# --- the whole move ----------------------------------------------------------


def test_the_move_happens_in_the_order_that_keeps_the_old_server_up(
    here: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(here, OldServer(["/media/intel/2026"]), monkeypatch)

    assert [step for step in here.log if step != "disconnect"] == [
        "connect",
        "read settings",
        "read libraries",
        # the long copy, with the old server still serving
        "copy data",
        "copy proxies",
        # the cutover
        "stop old app",
        "dump",
        "stop here",
        "restore",
        # only what changed during the first copy
        "copy data",
        "copy proxies",
        "start here",
    ]
    assert here.restored == b"PGDMP-old-catalogue"
    assert not (here.layout.run / "migration.dump").exists()  # the dump does not linger
    # A stale connection is cleared first, and ours is closed at the end.
    assert here.log[0] == "disconnect" and here.log[-1] == "disconnect"


def test_library_roots_are_rewritten_once_for_this_mac(
    here: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(here, OldServer(["/media/intel/2026"]), monkeypatch)
    (sql,) = here.sql
    new_root = (here.volumes / "intel" / "2026").as_posix()
    assert f"SET root_path = '{new_root}' WHERE root_path = '/media/intel/2026';" in sql
    assert sql.startswith("BEGIN;") and sql.endswith("COMMIT;")


def test_the_catalogues_settings_come_along_and_the_machines_do_not(
    here: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = dict(here.env)
    run(here, OldServer(["/media/intel/2026"]), monkeypatch)
    after = envfile.read(here.layout.env_file)
    # Without the old key, every sealed secret in the restored database is noise.
    assert after["FRAMEFOUND_SECRET_KEY"] == "old-server-secret"
    assert after["FRAMEFOUND_SERVER_NAME"] == "Six Gricks"
    assert after["FRAMEFOUND_WHISPER_MODEL"] == "base"
    assert after["FRAMEFOUND_SETUP_TOKEN"] == ""  # the admin arrives in the catalogue
    # This install's own plumbing is untouched.
    for key in ("POSTGRES_PASSWORD", "POSTGRES_PORT", "FRAMEFOUND_DATA_DIR", "REDIS_PASSWORD"):
        assert after[key] == before[key], key
    assert "FRAMEFOUND_DATA_STORE" not in after and "COMPOSE_FILE" not in after


# --- refusals, all before anything changes -------------------------------------


def _nothing_changed(here: SimpleNamespace) -> None:
    assert "stop old app" not in here.log and "restore" not in here.log
    assert "copy data" not in here.log
    assert envfile.read(here.layout.env_file)["FRAMEFOUND_SECRET_KEY"] != "old-server-secret"


def test_a_library_with_nowhere_to_go_stops_it_before_anything_changes(
    here: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = OldServer(["/media/intel/2026", "/srv/footage"])
    with pytest.raises(MigrationError, match="/srv/footage"):
        run(here, server, monkeypatch)
    _nothing_changed(here)
    # …and the same run with the missing place supplied goes through.
    here.log.clear()
    (here.volumes / "footage").mkdir()
    run(here, server, monkeypatch, maps=f"/srv/footage={here.volumes / 'footage'}")
    assert "start here" in here.log


def test_a_catalogue_already_here_is_not_overwritten_by_accident(
    here: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    here.users = 1
    with pytest.raises(MigrationError, match="--replace"):
        run(here, OldServer(["/media/intel/2026"]), monkeypatch)
    _nothing_changed(here)
    run(here, OldServer(["/media/intel/2026"]), monkeypatch, replace=True)
    assert "restore" in here.log


def test_proxies_are_not_copied_into_a_store_that_is_away(
    here: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(MigrationError, match="nowhere to go"):
        run(here, OldServer(["/media/intel/2026"]), monkeypatch, proxy_problem="not connected")
    _nothing_changed(here)
    # Leaving them behind is still possible.
    run(
        here,
        OldServer(["/media/intel/2026"]),
        monkeypatch,
        proxy_problem="not connected",
        proxies="skip",
    )
    assert "copy proxies" not in here.log and "copy data" in here.log


def test_an_older_checkout_than_the_old_server_is_refused(
    here: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(MigrationError, match="framefound update"):
        run(here, OldServer(["/media/intel/2026"], schema="9999"), monkeypatch)
    _nothing_changed(here)


def test_a_data_store_in_a_docker_volume_cannot_be_copied(
    here: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_text = OLD_ENV.replace("FRAMEFOUND_DATA_STORE=/mnt/ffdata/framefound-data\n", "")
    with pytest.raises(MigrationError, match="Docker volume"):
        run(here, OldServer(["/media/intel/2026"], env_text=env_text), monkeypatch)
    _nothing_changed(here)


def test_saying_no_changes_nothing(here: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(MigrationError, match="Nothing was changed"):
        run(here, OldServer(["/media/intel/2026"]), monkeypatch, answer=False)
    _nothing_changed(here)


def test_the_plan_names_a_share_that_is_not_mounted_here(
    here: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    said = run(here, OldServer(["/media/intel/2026", "/media/breeze"]), monkeypatch)
    plan = "\n".join(said)
    assert "/media/breeze" in plan and "absent on this Mac" in plan
    assert "will show as offline" in plan
    assert "start here" in here.log  # a warning, not a refusal


# --- when it goes wrong partway ---------------------------------------------------


def test_a_failure_after_the_old_app_stopped_says_how_to_bring_it_back(
    here: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_restore(_env: dict[str, str], _dump: Path) -> None:
        raise database.DatabaseError("The dump did not restore cleanly")

    said: list[str] = []
    server = OldServer(["/media/intel/2026"])
    server.log = here.log
    monkeypatch.setattr(proc, "run", server)
    monkeypatch.setattr(database, "restore", broken_restore)
    with pytest.raises(database.DatabaseError):
        migrate.migrate(
            here.layout,
            here.env,
            FakeLaunchd(here.log),  # type: ignore[arg-type]
            Options(source="framefound@vm", volumes=here.volumes),
            proxy_root=here.layout.home / "proxies",
            proxy_problem=None,
            say=said.append,
            confirm=lambda _q: True,
        )
    assert any("docker compose up -d" in line for line in said)
    assert "start here" not in here.log
    assert not (here.layout.run / "migration.dump").exists()
    # The settings are only carried once the catalogue is in.
    assert envfile.read(here.layout.env_file)["FRAMEFOUND_SECRET_KEY"] != "old-server-secret"


def test_leaving_the_old_server_running_skips_the_stop_and_the_second_copy(
    here: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run(here, OldServer(["/media/intel/2026"]), monkeypatch, leave_source_running=True)
    assert "stop old app" not in here.log
    assert here.log.count("copy data") == 1


# --- the pieces ------------------------------------------------------------------


def test_commands_for_the_old_server_are_quoted(tmp_path: Path) -> None:
    source = Source("framefound@vm", "frame found", tmp_path / "ctl")
    argv = source.compose("exec", "-T", "postgres", "psql", "-c", "SELECT 'a b'; rm -rf /")
    assert argv[0] == "ssh" and argv[-2] == "framefound@vm"
    assert argv[-1] == (
        "cd 'frame found' && docker compose exec -T postgres psql -c "
        "'SELECT '\"'\"'a b'\"'\"'; rm -rf /'"
    )
    # Every command is a client of the one connection opened at the start.
    assert "ControlMaster=no" in argv and f"ControlPath={tmp_path / 'ctl'}" in argv
    assert "ControlMaster=yes" in source.open() and "-fN" in source.open()
    assert source.close()[-3:] == ["-O", "exit", "framefound@vm"]


def test_the_old_database_is_left_running_to_be_dumped() -> None:
    assert "postgres" not in migrate.SOURCE_APP_SERVICES
    assert "redis" not in migrate.SOURCE_APP_SERVICES and "caddy" not in migrate.SOURCE_APP_SERVICES
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    for name in migrate.SOURCE_APP_SERVICES:
        assert f"\n  {name}:\n" in compose, name  # every name is a real compose service


def test_shares_map_from_media_to_volumes_unless_told_otherwise(tmp_path: Path) -> None:
    (tmp_path / "intel").mkdir()
    (tmp_path / "intel" / "a.mov").write_bytes(b"x")
    (tmp_path / "Promo").mkdir()
    (tmp_path / "Promo" / "b.mov").write_bytes(b"x")
    plans = migrate.plan_roots(
        ["/media/intel", "/media/promo/video", "/data/else"],
        f"/media/promo={tmp_path / 'Promo'}",
        tmp_path,
    )
    assert plans == [
        RootPlan("/media/intel", tmp_path / "intel", "ok"),
        RootPlan("/media/promo/video", tmp_path / "Promo" / "video", "absent"),
        RootPlan("/data/else", None, "unmapped"),
    ]


def test_a_library_path_with_an_apostrophe_is_quoted(tmp_path: Path) -> None:
    sql = migrate.rewrite_roots_sql(
        [
            RootPlan("/media/intel/O'Brien", tmp_path / "O'Brien", "ok"),
            RootPlan("/same", Path("/same"), "ok"),
        ]
    )
    assert "WHERE root_path = '/media/intel/O''Brien';" in sql
    assert sql.count("UPDATE") == 1  # a root that does not move is not touched
    assert migrate.rewrite_roots_sql([]) == ""


def test_the_copies_split_proxies_from_everything_else(tmp_path: Path) -> None:
    options = Options(source="framefound@vm")
    source = Source("framefound@vm", "framefound", tmp_path / "ctl")
    data = migrate.data_copy(options, source, "/mnt/ffdata/framefound-data/", tmp_path / "data")
    assert data[-2] == "framefound@vm:/mnt/ffdata/framefound-data/"
    assert "--exclude=/derivatives/*/*/proxy.*" in data
    # Scratch, half-made renders, the old backups (they hold its settings)
    # and the old store's marker stay behind.
    for left in ("--exclude=/tmp/", "--exclude=/backups/", "--exclude=.framefound-*"):
        assert left in data
    proxies = migrate.proxy_copy(options, source, "/mnt/ffdata/framefound-data", tmp_path / "nas")
    assert proxies[-2] == "framefound@vm:/mnt/ffdata/framefound-data/derivatives/"
    assert proxies[-1] == f"{tmp_path / 'nas'}/derivatives/"
    assert proxies.index("--include=proxy.*") < proxies.index("--exclude=*")
    for argv in (data, proxies):
        assert "--partial" in argv  # an interrupted copy carries on
        assert argv[argv.index("-e") + 1].startswith("ssh -o ControlMaster=no")


def test_the_secret_key_is_required() -> None:
    with pytest.raises(MigrationError, match="FRAMEFOUND_SECRET_KEY"):
        migrate.carried_settings({"FRAMEFOUND_SERVER_NAME": "x"})


def test_this_checkout_knows_its_own_migrations(tmp_path: Path) -> None:
    layout = Layout(home=tmp_path, source=REPO)
    assert migrate.knows_revision(layout, "0029")
    assert migrate.knows_revision(layout, "0030")
    assert not migrate.knows_revision(layout, "9999")
