"""`framefound migrate --from user@host`: move a container install here.

What moves: the catalogue (one database dump), the settings that must not
change (the secret key above all — it unseals every stored secret), the
derived files, and the proxies. What changes on the way: each library's
root, once — the old server saw `/media/intel`, this Mac sees
`/Volumes/intel`. The old server kept everything in one data folder; here
the bulky kinds may each have a folder of their own (storage/stores.py), and
each is copied straight into it rather than through this Mac's disk.

The order keeps the old server answering for as long as possible and makes
every step repeatable:

    1. plan     read the old settings and library roots; refuse here, before
                anything is touched, if a library has nowhere to go
    2. copy     derived files and proxies, while the old server still runs
    3. stop     the old server's app (its database stays up, to be dumped)
    4. dump     the catalogue, and restore it here
    5. copy     again — only what changed during the first copy
    6. rewrite  library roots; carry the settings; start

Nothing on the old server is deleted or changed beyond `docker compose
stop`; `docker compose up -d` there undoes the migration.
"""

import os
import shlex
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from framefound.native import database, envfile, installer, proc, services
from framefound.native.launchd import Launchd
from framefound.native.layout import Layout
from framefound.storage import stores
from framefound.storage.reachability import root_state
from framefound.storage.worker_roots import RootMapping, parse_root_map, translate

Say = Callable[[str], None]

# Settings that describe the catalogue rather than the machine, so they come
# with it. Everything else (ports, paths, passwords) is this install's own.
CARRIED = (
    # Unseals TOTP seeds and stored tokens, and signs media URLs: with a
    # different key every sealed secret in the restored database is noise.
    "FRAMEFOUND_SECRET_KEY",
    "FRAMEFOUND_SERVER_NAME",
    # A different model would label and embed new media differently from
    # everything already in the catalogue.
    "FRAMEFOUND_WHISPER_MODEL",
    "FRAMEFOUND_VISION_MODEL",
    "FRAMEFOUND_TAG_AUTOSUGGEST",
    "FRAMEFOUND_SESSION_IDLE_MINUTES",
    "FRAMEFOUND_SESSION_ABSOLUTE_HOURS",
)
# The old server's app. Postgres stays up to be dumped; Caddy and the queue
# are left alone.
SOURCE_APP_SERVICES = (
    "scanner",
    "scheduler",
    "worker",
    "worker-media",
    "worker-ai",
    "worker-vision",
    "api",
    "web",
    "backup",
)
# In the old data store but not wanted here: scratch, half-made renders, the
# old server's own backups (they hold its settings file), and its marker.
DATA_EXCLUDES = ("/tmp/", "/backups/", "/renders/work/", ".framefound-*")  # noqa: S108
PROXY_PATTERN = "proxy.*"
# The other kinds that may have a folder of their own here, and what in the
# old data store's folder of that name is not a finished file of that kind.
KIND_EXCLUDES: dict[str, tuple[str, ...]] = {
    "renders": ("/work/",),  # a render's pieces
    "exports": (),
    "basemaps": ("*.part",),  # an extract that was still being written
}


class MigrationError(RuntimeError):
    """Something the operator has to fix; the message says what."""


@dataclass(frozen=True)
class Source:
    """The old server, reached over SSH."""

    host: str  # user@host
    directory: str  # the compose folder, relative to that user's home
    control: Path  # the socket of one authenticated connection, shared by every command

    # One connection, opened first and on its own, so a password or
    # passphrase is asked for once rather than for each of a dozen commands
    # and both copies. Opened explicitly rather than with ControlMaster=auto:
    # a master that the first *captured* command spawned would keep that
    # command's error pipe open, and the command would appear to hang until
    # the connection expired.

    def open(self) -> list[str]:
        """Authenticate and go to the background (run with the terminal
        attached, never captured)."""
        return [
            "ssh",
            "-o",
            "ControlMaster=yes",
            "-o",
            f"ControlPath={self.control}",
            "-o",
            "ControlPersist=1800",
            "-fN",
            self.host,
        ]

    def close(self) -> list[str]:
        return ["ssh", "-o", f"ControlPath={self.control}", "-O", "exit", self.host]

    def ssh(self) -> list[str]:
        """A client of the open connection; never starts one of its own."""
        return ["ssh", "-o", "ControlMaster=no", "-o", f"ControlPath={self.control}"]

    def command(self, remote: str) -> list[str]:
        """Run `remote` in the compose folder on the old server."""
        return [*self.ssh(), self.host, f"cd {shlex.quote(self.directory)} && {remote}"]

    def compose(self, *words: str) -> list[str]:
        return self.command("docker compose " + " ".join(shlex.quote(w) for w in words))

    def rsync_shell(self) -> str:
        return " ".join(shlex.quote(word) for word in self.ssh())


@dataclass(frozen=True)
class RootPlan:
    server: str
    local: Path | None
    state: str  # ok | absent | empty | unmapped


@dataclass
class Options:
    source: str
    directory: str = "framefound"
    maps: str = ""  # extra "server=local" pairs, ";"-separated
    proxies: str = "copy"  # copy | skip
    replace: bool = False  # overwrite a catalogue already here
    leave_source_running: bool = False
    rsync: str = "rsync"
    volumes: Path = field(default_factory=lambda: Path("/Volumes"))


def knows_revision(layout: Layout, revision: str) -> bool:
    """True when this checkout's migrations include `revision` — that is,
    when it can bring the old catalogue forward rather than choke on it."""
    from alembic.script import ScriptDirectory
    from alembic.util.exc import CommandError

    try:
        scripts = ScriptDirectory(str(layout.server_source / "migrations"))
        return scripts.get_revision(revision) is not None
    except CommandError:
        return False


def carried_settings(remote: dict[str, str]) -> dict[str, str]:
    values = {key: remote[key] for key in CARRIED if remote.get(key)}
    if "FRAMEFOUND_SECRET_KEY" not in values:
        raise MigrationError(
            "The old server's .env has no FRAMEFOUND_SECRET_KEY, so the secrets "
            "sealed in its database could not be opened here."
        )
    # The first admin already exists in the catalogue being brought over.
    values["FRAMEFOUND_SETUP_TOKEN"] = ""
    return values


def root_mappings(maps: str, volumes: Path) -> list[RootMapping]:
    """The operator's `--map` pairs, then the default: the containers mount
    every share under /media, macOS mounts them under /Volumes."""
    explicit = parse_root_map(maps)
    return [*explicit, *parse_root_map(f"/media={volumes}")]


def plan_roots(roots: list[str], maps: str, volumes: Path) -> list[RootPlan]:
    mappings = root_mappings(maps, volumes)
    plans = []
    for root in roots:
        local = translate(root, mappings)
        state = "unmapped" if local is None else root_state(local)
        plans.append(RootPlan(root, local, state))
    return plans


def data_copy(
    options: Options, source: Source, store: str, target: Path, elsewhere: Iterable[str] = ()
) -> list[str]:
    """Everything derived except proxies, into the data folder. The kinds
    in `elsewhere` are left out too: they have folders of their own here
    (`kind_copy`)."""
    return [
        options.rsync,
        "-rlts",
        "--partial",
        "--info=progress2",
        "-e",
        source.rsync_shell(),
        *(f"--exclude={pattern}" for pattern in DATA_EXCLUDES),
        f"--exclude=/derivatives/*/*/{PROXY_PATTERN}",
        *(f"--exclude=/{kind}/" for kind in elsewhere),
        f"{source.host}:{store.rstrip('/')}/",
        f"{target}/",
    ]


def proxy_copy(options: Options, source: Source, store: str, target: Path) -> list[str]:
    """Only the proxies, into the proxy store, keeping their folders."""
    return [
        options.rsync,
        "-rlts",
        "--partial",
        "--info=progress2",
        "-e",
        source.rsync_shell(),
        "--prune-empty-dirs",
        "--include=*/",
        f"--include={PROXY_PATTERN}",
        "--exclude=*",
        f"{source.host}:{store.rstrip('/')}/derivatives/",
        f"{target}/derivatives/",
    ]


def kind_copy(options: Options, source: Source, store: str, kind: str, target: Path) -> list[str]:
    """One bulky kind's finished files, into that kind's own folder here:
    the old `<data>/renders/x.mp4` becomes `<renders folder>/x.mp4`."""
    return [
        options.rsync,
        "-rlts",
        "--partial",
        "--info=progress2",
        # An old server that never rendered a slideshow has no `renders`
        # folder. That is nothing to copy, not a failed migration.
        "--ignore-missing-args",
        "-e",
        source.rsync_shell(),
        *(f"--exclude={pattern}" for pattern in (*KIND_EXCLUDES[kind], ".framefound-*")),
        f"{source.host}:{store.rstrip('/')}/{kind}/",
        f"{target}/",
    ]


def rewrite_roots_sql(plans: list[RootPlan]) -> str:
    """One statement per library root that moves. Values go in as SQL string
    literals with quotes doubled; a library folder may contain an apostrophe."""

    def literal(value: str) -> str:
        if "\x00" in value:
            raise MigrationError("A library path contains a NUL character")
        return "'" + value.replace("'", "''") + "'"

    statements = [
        f"UPDATE libraries SET root_path = {literal(plan.local.as_posix())} "  # noqa: S608
        f"WHERE root_path = {literal(plan.server)};"
        for plan in plans
        if plan.local is not None and plan.local.as_posix() != plan.server
    ]
    return "BEGIN; " + " ".join(statements) + " COMMIT;" if statements else ""


def describe(
    plans: list[RootPlan],
    options: Options,
    store: str,
    proxy_root: Path,
    kind_roots: Mapping[str, Path] | None = None,
) -> list[str]:
    lines = [f"From {options.source} ({options.directory}/), data store {store}", "", "Libraries:"]
    for plan in plans:
        note = "" if plan.state == "ok" else f"   <- {plan.state} on this Mac"
        lines.append(f"  {plan.server}  ->  {plan.local or '?'}{note}")
    lines += [
        "",
        "Proxies: "
        + (
            f"copied into {proxy_root}"
            if options.proxies == "copy"
            else "left behind (rebuild later with: framefound proxies rebuild)"
        ),
        *(
            f"{stores.words(kind).things.capitalize()}: copied into {root}"
            for kind, root in (kind_roots or {}).items()
        ),
        "The old server's app is "
        + (
            "left running (its catalogue may change under the dump)"
            if options.leave_source_running
            else "stopped for the dump; its database and files are not touched"
        ),
    ]
    return lines


def migrate(
    layout: Layout,
    env: dict[str, str],
    launchd: Launchd,
    options: Options,
    proxy_root: Path,
    proxy_problem: str | None,
    say: Say,
    confirm: Callable[[str], bool],
    kind_roots: Mapping[str, Path] | None = None,
    kind_problems: Mapping[str, str | None] | None = None,
) -> None:
    """`kind_roots` names the folder of each bulky kind (other than proxies)
    that has one of its own on this Mac, and `kind_problems` why any of them
    cannot be written to right now. Left out, everything but proxies goes
    into the data folder, as it did before there were kinds."""
    layout.run.mkdir(parents=True, exist_ok=True)
    source = Source(options.source, options.directory, layout.run / "migrate-ssh.sock")
    # A connection left by an interrupted run would be reused blindly.
    proc.run(source.close(), capture=True, check=False)
    source.control.unlink(missing_ok=True)
    say(f"Connecting to {options.source}")
    proc.run(source.open())
    try:
        _migrate(
            layout,
            env,
            launchd,
            options,
            source,
            proxy_root,
            proxy_problem,
            say,
            confirm,
            dict(kind_roots or {}),
            dict(kind_problems or {}),
        )
    finally:
        proc.run(source.close(), capture=True, check=False)


def _migrate(
    layout: Layout,
    env: dict[str, str],
    launchd: Launchd,
    options: Options,
    source: Source,
    proxy_root: Path,
    proxy_problem: str | None,
    say: Say,
    confirm: Callable[[str], bool],
    kind_roots: dict[str, Path],
    kind_problems: dict[str, str | None],
) -> None:

    # 1. Plan — everything that can refuse does so before anything changes.
    say("Reading the old server's settings")
    remote_env = envfile.parse(proc.run(source.command("cat .env"), capture=True).stdout or "")
    if not remote_env:
        raise MigrationError(f"No .env found in {options.directory}/ on {options.source}")
    carried = carried_settings(remote_env)
    store = (remote_env.get("FRAMEFOUND_DATA_STORE") or "").strip()
    if not store:
        raise MigrationError(
            "The old server keeps its data in a Docker volume (FRAMEFOUND_DATA_STORE "
            "is not set), which cannot be copied from outside. Move it to a folder "
            "first (docs/storage.md), then run this again."
        )
    remote_user = database.user({"POSTGRES_USER": remote_env.get("POSTGRES_USER", "")})
    remote_db = database.name({"POSTGRES_DB": remote_env.get("POSTGRES_DB", "")})
    psql = ("exec", "-T", "postgres", "psql", "-U", remote_user, "-d", remote_db, "-tA", "-c")
    roots = [
        line
        for line in (
            proc.run(
                source.compose(*psql, "SELECT root_path FROM libraries ORDER BY root_path"),
                capture=True,
            ).stdout
            or ""
        ).splitlines()
        if line.strip()
    ]
    remote_schema = (
        proc.run(
            source.compose(*psql, "SELECT version_num FROM alembic_version"), capture=True
        ).stdout
        or ""
    ).strip()
    if remote_schema and not knows_revision(layout, remote_schema):
        raise MigrationError(
            f"The old server's catalogue is at schema {remote_schema}, which this "
            "checkout does not know: it is older than the old server. Run "
            "`framefound update` here first."
        )
    plans = plan_roots(roots, options.maps, options.volumes)
    unmapped = [plan.server for plan in plans if plan.local is None]
    if unmapped:
        raise MigrationError(
            "No place on this Mac for: " + ", ".join(unmapped) + ". Add --map "
            "'<old path>=<path here>' for each."
        )
    if options.proxies == "copy" and proxy_problem is not None:
        raise MigrationError(f"The proxies have nowhere to go: {proxy_problem}")
    for kind in kind_roots:
        # Copied into the empty folder an unmounted share leaves behind,
        # they would land on this Mac's disk under the share's name.
        if kind_problems.get(kind) is not None:
            raise MigrationError(
                f"The {stores.words(kind).things} have nowhere to go: {kind_problems[kind]}"
            )
    if database.count(env, "users") and not options.replace:
        raise MigrationError(
            "This install already has a catalogue with users in it. Pass --replace "
            "to overwrite it with the old server's."
        )
    for line in describe(plans, options, store, proxy_root, kind_roots):
        say(line)
    away = [plan.server for plan in plans if plan.state != "ok"]
    if away:
        say("")
        say(
            "Not reachable here right now: " + ", ".join(away) + ". Their libraries "
            "will show as offline until the share is mounted; nothing is lost."
        )
    if not confirm("Go ahead?"):
        raise MigrationError("Nothing was changed.")

    def copy_files(note: str) -> None:
        say(note)
        data_dir = Path(env["FRAMEFOUND_DATA_DIR"])
        proc.run(data_copy(options, source, store, data_dir, elsewhere=kind_roots))
        if options.proxies == "copy":
            proc.run(proxy_copy(options, source, store, proxy_root))
        for kind, root in kind_roots.items():
            proc.run(kind_copy(options, source, store, kind, root))

    # 2. The long copy, with the old server still serving.
    copy_files("Copying derived files (the old server stays up for this)")

    stopped = False
    dump = layout.run / "migration.dump"
    try:
        # 3. Stop the old app so the dump is the final word.
        if not options.leave_source_running:
            say("Stopping the old server's app")
            proc.run(source.compose("stop", *SOURCE_APP_SERVICES))
            stopped = True

        # 4. Dump there, restore here.
        say("Copying the catalogue")
        # 600 from the first byte: the dump is the whole catalogue.
        with os.fdopen(os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as handle:
            proc.run(
                source.compose(
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "-U",
                    remote_user,
                    "-d",
                    remote_db,
                    "--format=custom",
                ),
                stdout=handle,
            )
        if dump.stat().st_size == 0:
            raise MigrationError("The old server returned an empty database dump")
        installer.stop_all(
            launchd, tuple(n for n in services.BY_NAME if n not in ("postgres", "valkey"))
        )
        database.restore(env, dump)

        # 5. What changed while the first copy ran.
        if not options.leave_source_running:
            copy_files("Copying what changed since the first pass")

        # 6. Roots, settings, start.
        sql = rewrite_roots_sql(plans)
        if sql:
            database.psql(env, sql)
        envfile.update(layout.env_file, carried)
        say("Starting FrameFound here")
        fresh = envfile.read(layout.env_file)
        installer.start_all(layout, fresh, launchd, say)
        if not installer.wait_for_api(fresh):
            raise MigrationError(
                f"The catalogue is restored but the API did not start — see "
                f"{layout.logs / 'api.log'}, then: framefound doctor"
            )
    except Exception:
        if stopped:
            say(
                "The old server's app is stopped. To bring it back exactly as it was:\n"
                f"  ssh {options.source} 'cd {options.directory} && docker compose up -d'"
            )
        raise
    finally:
        dump.unlink(missing_ok=True)
