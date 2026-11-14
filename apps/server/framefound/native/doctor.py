"""`framefound doctor`: is this install healthy, one line per thing.

Every line is something that has actually gone wrong on a FrameFound
install, said in terms of what to do about it. FAIL means FrameFound is
broken or about to be; warn means it is running and something deserves a
look. The same checks a worker on another machine runs (ops/worker_check.py)
make up the middle of it.
"""

import asyncio
import json
import platform
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from framefound.config import Settings
from framefound.native import backup, installer, proc, services
from framefound.native.launchd import Launchd
from framefound.native.layout import Layout
from framefound.ops import worker_check
from framefound.ops.worker_check import Check, check_onnx, check_services
from framefound.storage import stores
from framefound.storage.worker_roots import DATA_STORE_MARKER

if TYPE_CHECKING:
    from framefound.db.models import LocalModel
    from framefound.local_models.kinds import RuntimeKind
    from framefound.local_models.schedule import Budgets

STALE_BACKUP_S = 48 * 3600


# What the locale line runs, in the services' own environment: the call a
# program makes to take its locale from that environment. It fails for a
# locale this Mac does not have.
LOCALE_PROBE = "import locale; locale.setlocale(locale.LC_ALL, '')"


def check_locale(
    layout: Layout,
    env: dict[str, str],
    *,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = proc.run,
) -> Check:
    """The locale the background services are started with.

    launchd hands an agent no LANG and no LC_ALL, and Postgres will not
    start in the background without one: the first real install logged
    "postmaster became multithreaded during startup" every 15 seconds while
    every command typed in a Terminal (which sets LANG itself) worked. So
    this reads what the launcher gives a service (services.process_environment)
    and never this process's own environment, where the Terminal's LANG
    would stand in for the one that is missing."""
    given = services.process_environment(layout, env)
    name = given.get("LC_ALL") or given.get("LANG") or ""
    if not name:
        return Check(
            "locale",
            False,
            "the background services are started with no LANG or LC_ALL, and Postgres does "
            "not start without one (its log says 'postmaster became multithreaded during "
            f"startup': {layout.logs / 'postgres.log'}) — framefound update",
        )
    try:
        tried = run(
            [str(layout.python), "-c", LOCALE_PROBE],
            env=given,
            capture=True,
            check=False,
            timeout=20,
        )
    except proc.CommandFailed as err:
        return Check("locale", False, f"{name}, but it could not be tried: {err}", fatal=False)
    if tried.returncode != 0:
        return Check(
            "locale",
            False,
            f"the background services are started with {name}, which this Mac does not "
            "have (`locale -a` lists the ones it has) — Postgres may not start",
            fatal=False,
        )
    return Check("locale", True, f"{name} for every background service")


def check_agents(
    launchd: Launchd,
    layout: Layout,
    env: dict[str, str],
    *,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = proc.run,
) -> list[Check]:
    """One line per service, then one for what every one of them is started
    with: its locale. The install's settings (`env`) are not optional: left
    out, the locale line would quietly go missing from the report, and it
    is the only line that shows why Postgres will not start under launchd."""
    checks = []
    for service in services.SERVICES:
        state = launchd.state(service.name)
        # An interval service (the backup) is healthy loaded and idle.
        healthy = state.loaded if service.every_s is not None else state.running
        if healthy:
            detail = service.summary
        elif not state.loaded:
            detail = "not loaded — framefound start"
        else:
            exit_note = f", last exit {state.last_exit}" if state.last_exit is not None else ""
            detail = f"{state.state}{exit_note} — see {layout.logs / (service.name + '.log')}"
        checks.append(Check(service.name, healthy, detail))
    checks.append(check_locale(layout, env, run=run))
    return checks


def check_front_door(env: dict[str, str]) -> list[Check]:
    http = services.port(env, "FRAMEFOUND_HTTP_PORT")
    return [
        Check(
            "address",
            proc.port_open(http),
            f"http://localhost:{http}"
            if proc.port_open(http)
            else f"nothing answers on port {http}",
        )
    ]


def _writable(folder: Path) -> str | None:
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".framefound-doctor-"):
            return None
    except OSError as err:
        return str(err)


def check_stores(settings: Settings) -> list[Check]:
    checks = []
    data = settings.data_dir
    problem = _writable(data)
    free = shutil.disk_usage(data).free / 1024**3 if problem is None else 0.0
    if problem is not None:
        checks.append(Check("data store", False, f"{data} is not writable ({problem})"))
    elif free < settings.min_free_gb:
        checks.append(
            Check(
                "data store",
                False,
                f"{data} has {free:.0f} GB free, below the {settings.min_free_gb:.0f} GB floor: "
                "generation is paused",
            )
        )
    else:
        marked = (data / DATA_STORE_MARKER).is_file()
        note = "" if marked else " (no marker yet: the API writes it on start)"
        checks.append(Check("data store", True, f"{data}, {free:.0f} GB free{note}"))

    if stores.separate_proxy_store(settings) is None:
        checks.append(
            Check(
                "proxy store",
                True,
                "with the data store — move with: framefound proxies move <folder>",
                fatal=False,
            )
        )
    else:
        checks.append(_check_store("proxies", settings))
    # The other bulky kinds get a line only when they have a folder of their
    # own (storage/stores.py): on an install that has moved nothing, doctor
    # says what it always said. Backup copies have their own line below. The
    # model library is one of these: away, it holds up downloads, copies and
    # removals, while a model that has a role runs from this machine.
    for kind in ("renders", "exports", "basemaps", "models"):
        if stores.is_separate(kind, settings):
            checks.append(_check_store(kind, settings))
    return checks


def _check_store(kind: str, settings: Settings) -> Check:
    """One kind's own folder: connected, writable, and how much room."""
    name = stores.words(kind).store
    root = stores.kind_folder(kind, settings)
    away = stores.kind_problem(kind, settings)
    if away is not None:
        # Not fatal: only what lives there waits, and everything else carries on.
        return Check(name, False, away, fatal=False)
    problem = _writable(root)
    if problem is not None:
        return Check(name, False, f"{root} is not writable ({problem})")
    free = shutil.disk_usage(root).free / 1024**3
    return Check(name, True, f"{root}, {free:.0f} GB free")


def check_backups(layout: Layout, env: dict[str, str]) -> list[Check]:
    age = backup.newest_age_s(backup.folder(env, layout))
    if age is None:
        newest = Check("backup", False, "none yet — framefound backup", fatal=False)
    elif age > STALE_BACKUP_S:
        newest = Check(
            "backup", False, f"the newest is {age / 86400:.0f} days old — framefound backup", False
        )
    else:
        newest = Check("backup", True, f"newest is {age / 3600:.0f} h old", fatal=False)
    checks = [newest]
    # FRAMEFOUND_BACKUP_COPY_DIR, or the bulk folder's `backups` without it.
    copy_dir = backup.copy_dir(env, layout)
    away = backup.copy_problem(env, layout)
    if copy_dir is None:
        checks.append(
            Check(
                "backup copy",
                False,
                "backups stay on this Mac only — give FrameFound a bulk folder "
                "(framefound install --store <folder>), or set "
                "FRAMEFOUND_BACKUP_COPY_DIR to a NAS folder",
                fatal=False,
            )
        )
    elif away is not None:
        checks.append(Check("backup copy", False, away, fatal=False))
    elif not copy_dir.is_dir():
        checks.append(Check("backup copy", False, f"{copy_dir} is not reachable", fatal=False))
    else:
        age = backup.newest_age_s(copy_dir)
        detail = str(copy_dir) if age is None else f"{copy_dir}, newest {age / 3600:.0f} h old"
        checks.append(Check("backup copy", True, detail, fatal=False))
    return checks


def check_rsync(
    homebrew: Callable[[str], str | None] = installer.homebrew_program,
    which: Callable[[str], str | None] = shutil.which,
) -> Check:
    """Which rsync `framefound proxies move` and `framefound migrate` would
    run (installer.rsync). A warning at most: nothing else uses it."""
    own = homebrew("rsync")
    if own is not None:
        return Check("rsync", True, own, fatal=False)
    system = which("rsync")
    if system is None:
        detail = "not installed"
    else:
        # openrsync, or rsync 2.6.9 on an older macOS: neither has the
        # options the copies are written with (--info=progress2,
        # --ignore-missing-args, -s).
        detail = f"{system} is the one that came with macOS, not Homebrew's"
    return Check(
        "rsync",
        False,
        f"{detail} — framefound proxies move and framefound migrate need Homebrew's; "
        "run: brew install rsync",
        fatal=False,
    )


def check_tools() -> list[Check]:
    """The programs every worker needs, and the one only this Mac's own
    commands use."""
    return [*worker_check.check_tools(), check_rsync()]


def parse_sleep_minutes(pmset_output: str) -> int | None:
    """The system sleep timer from `pmset -g` (0 = never), or None if absent."""
    found = re.search(r"^\s*sleep\s+(\d+)", pmset_output, re.MULTILINE)
    return int(found.group(1)) if found else None


def check_power() -> list[Check]:
    try:
        output = proc.run(["pmset", "-g"], capture=True, check=False, timeout=10).stdout or ""
    except proc.CommandFailed:
        return []
    minutes = parse_sleep_minutes(output)
    if minutes is None:
        return []
    return [
        Check(
            "stays awake",
            minutes == 0,
            "the Mac never sleeps"
            if minutes == 0
            else f"the Mac sleeps after {minutes} min idle, and FrameFound with it — "
            "System Settings > Energy: prevent automatic sleeping",
            fatal=False,
        )
    ]


# --- local AI models (docs/local-models.md) ------------------------------------

# What the rest of the Mac needs beside a model: Adobe, macOS and FrameFound
# itself. A budget that leaves less than this is a setting that will make
# Premiere stutter or the Mac swap the night it is used.
LOCAL_AI_HEADROOM_GB = 4.0
# Where the model server records the program it started (local_models/server.py
# RECORD_NAME; a test holds the two together). Read here, never written.
RUNTIME_RECORD = "runtime.json"
_GB = 1024**3


def parse_memsize(text: str) -> int | None:
    """This Mac's memory in bytes, from `sysctl -n hw.memsize` (or `sysctl
    hw.memsize`, which puts the name first); None for anything else."""
    found = re.fullmatch(r"\s*(?:hw\.memsize:\s*)?(\d{1,20})\s*", text)
    if found is None:
        return None
    size = int(found.group(1))
    return size if size > 0 else None


def parse_listeners(text: str) -> list[tuple[int, str]]:
    """(pid, program) for each process in `lsof -F pc` output.

    The output is one field per line, each starting with its letter: `p` a
    process, `c` its program, `f` a file of it. Anything else is skipped."""
    found: list[tuple[int, str]] = []
    pid: int | None = None
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
            found.append((pid, ""))
        elif line.startswith("c") and pid is not None and found and found[-1][0] == pid:
            found[-1] = (pid, line[1:].strip())
    return found


def check_local_models(
    layout: Layout,
    env: dict[str, str],
    settings: Settings,
    *,
    run: Callable[..., "subprocess.CompletedProcess[str]"] = proc.run,
    which: Callable[..., str | None] = shutil.which,
    port_open: Callable[[int], bool] = proc.port_open,
    machine: tuple[str, str] | None = None,
) -> list[Check]:
    """One line per piece of local AI, and nothing at all where it is not set up.

    Nothing without `FRAMEFOUND_RUNTIMES_DIR`, the line that turns the AI
    models page on: an install that has not been brought up to date, and
    every container, says exactly what it said before. Every line is a
    warning at most. Local AI is optional, and nothing else in FrameFound
    waits for it.

    `run`, `which`, `port_open` and `machine` (`platform.system()`,
    `platform.machine()`) are the outside world, handed in so a test can
    stand in for each.
    """
    root = settings.runtimes_dir
    if root is None:
        return []
    # Imported here, not at the top: `framefound status` and the rest of the
    # command should not load the model manager to do their own work.
    from framefound.local_models.kinds import kinds

    system, arch = machine if machine is not None else (platform.system(), platform.machine())
    memory = _physical_memory_gb(run)
    here = kinds(settings)
    # Read first, said last: which runtimes the models added need decides
    # whether an optional runtime that is not installed gets a line at all.
    needed, catalogue = asyncio.run(_catalogue_lines(settings, memory))

    checks = [_local_ai_line(root, system, arch, memory)]
    if any(kind.needs_uv for kind in here.values()):
        # The worker that installs a runtime looks for uv on the services'
        # PATH, which is not the PATH of the Terminal doctor is run from.
        path = services.process_environment(layout, env)["PATH"]
        uv = which("uv", path=path)
        checks.append(
            Check("uv", True, uv, fatal=False)
            if uv
            else Check(
                "uv",
                False,
                "not found — the software that runs models cannot be installed or updated "
                "from the AI models page; run: brew install uv",
                fatal=False,
            )
        )
    for position, kind in enumerate(here.values()):
        # The first kind is the one Getting started installs, so it is always
        # reported. Any other is needed only once a model added runs on it:
        # an empty llama.cpp line on an MLX-only Mac would read as a fault.
        line = _runtime_line(
            root, kind, system, arch, needed=needed.get(kind.name, 0), optional=position > 0
        )
        if line is not None:
            checks.append(line)
    checks += _model_server_lines(layout, env, run, port_open)
    checks += catalogue
    return checks


def _physical_memory_gb(run: Callable[..., "subprocess.CompletedProcess[str]"]) -> float | None:
    try:
        done = run(["sysctl", "-n", "hw.memsize"], capture=True, check=False, timeout=10)
    except proc.CommandFailed:
        return None
    size = parse_memsize(done.stdout or "") if done.returncode == 0 else None
    return None if size is None else size / _GB


def _gb(value: float) -> str:
    from framefound.local_models.schedule import gb_text

    return gb_text(value)


def _local_ai_line(root: Path, system: str, arch: str, memory: float | None) -> Check:
    problem = _writable(root)
    if problem is not None:
        return Check(
            "local AI",
            False,
            f"{root} is not writable ({problem}) — the software that runs models is kept "
            "there; framefound install makes it",
            fatal=False,
        )
    what = "Apple silicon" if (system, arch) == ("Darwin", "arm64") else f"{system} {arch}"
    size = "" if memory is None else f", {_gb(round(memory))} GB"
    return Check("local AI", True, f"{root}, {what}{size}", fatal=False)


def _runtime_line(
    root: Path,
    kind: "RuntimeKind",
    system: str,
    arch: str,
    *,
    needed: int = 0,
    optional: bool = False,
) -> Check | None:
    """The version of one runtime in use, the one kept to go back to, and what is wrong.

    None for an `optional` runtime that nothing needs (`needed` is how many
    models added run on it) and that is not installed or cannot be here.
    """
    from framefound.local_models import runtimes

    name = f"runtime {kind.name}"
    cannot = kind.supported_here(system, arch)
    if cannot is not None:
        return Check(name, False, cannot, fatal=False) if needed or not optional else None
    try:
        installed = runtimes.describe(root, kind.name)
    except runtimes.RuntimeProblem as err:
        return Check(name, False, str(err), fatal=False)
    if installed.current is None:
        if optional and not needed:
            return None
        wanted = (
            f", and {needed} model{'s' if needed != 1 else ''} on the AI models page "
            f"need{'s' if needed == 1 else ''} it"
            if needed
            else ""
        )
        return Check(
            name,
            False,
            f"not installed{wanted} — AI models page › Software that runs models › Install",
            fatal=False,
        )
    # A version whose program has gone (a venv or an unpacked archive
    # deleted by hand) is "installed" on paper and cannot run anything.
    folder = runtimes.version_dir(root, kind.name, installed.current)
    try:
        program = kind.program(folder)
    except OSError:
        program = None
    if program is None:
        return Check(
            name,
            False,
            f"{installed.current} is recorded as in use but its program is missing "
            f"({folder}) — AI models page › Software that runs models › Install a particular "
            f"version › {installed.current} installs it afresh",
            fatal=False,
        )
    try:
        about = kind.about(folder)
    except (OSError, ValueError):
        about = ""
    build = f", {about}" if about else ""
    kept = f" ({installed.previous} kept to go back to)" if installed.previous else ""
    return Check(name, True, f"{installed.current}{build}{kept}", fatal=False)


def _model_server_lines(
    layout: Layout,
    env: dict[str, str],
    run: Callable[..., "subprocess.CompletedProcess[str]"],
    port_open: Callable[[int], bool],
) -> list[Check]:
    server = services.port(env, "FRAMEFOUND_MODEL_SERVER_PORT")
    checks = [
        Check("model server", True, f"answering on {server}", fatal=False)
        if port_open(server)
        else Check(
            "model server",
            False,
            f"nothing answers on {server} — framefound restart model-server "
            f"(its log: {layout.logs / 'model-server.log'})",
            fatal=False,
        )
    ]
    # The runtime's own port is normally closed (no model loaded). Open, it
    # is either the model the server loaded or another program squatting on
    # it, which the page can only call "another program": this names it.
    runtime = services.port(env, "FRAMEFOUND_MODEL_RUNTIME_PORT")
    if not port_open(runtime):
        return checks
    holders = _listeners(run, runtime)
    ours = _recorded_pid(layout.run / RUNTIME_RECORD)
    for pid, program in holders:
        if pid == ours:
            checks.append(
                Check(
                    "model runtime",
                    True,
                    f"a model is loaded ({program or 'pid'} {pid})",
                    fatal=False,
                )
            )
            return checks
    if holders:
        pid, program = holders[0]
        who = f"{program or 'a program'} (pid {pid})"
    else:
        who = "a program lsof could not name"
    checks.append(
        Check(
            "model runtime",
            False,
            f"port {runtime} is held by {who}, not by FrameFound — no model can be loaded "
            f"until it is closed, or FRAMEFOUND_MODEL_RUNTIME_PORT is changed",
            fatal=False,
        )
    )
    return checks


def _listeners(
    run: Callable[..., "subprocess.CompletedProcess[str]"], port: int
) -> list[tuple[int, str]]:
    try:
        done = run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
            capture=True,
            check=False,
            timeout=10,
        )
    except proc.CommandFailed:
        return []
    return parse_listeners(done.stdout or "")


def _recorded_pid(path: Path) -> int | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = body.get("pid") if isinstance(body, dict) else None
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None


async def _catalogue_lines(
    settings: Settings, memory: float | None
) -> tuple[dict[str, int], list[Check]]:
    """How many models each runtime is needed by; the roles, the pause and the memory budgets.

    Its own engine, disposed whatever happens, as check_services does. A
    catalogue that cannot be read is said once here (and needs nothing); the
    database line above already says why."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from framefound.local_models import ROLE_TITLES, registry, settings_store

    engine = create_async_engine(settings.db_url)
    needed: dict[str, int] = {}
    try:
        async with AsyncSession(engine) as db:
            config = await settings_store.load_config(db)
            needed = await registry.models_per_runtime(db)
            held = await registry.roles(db)
            checks: list[Check] = []
            for role, row in held.items():
                if row.model_id is None:
                    continue
                model = await registry.get_model(db, row.model_id)
                if model is None:
                    continue
                pending = (
                    await registry.get_model(db, row.pending_model_id)
                    if row.pending_model_id is not None
                    else None
                )
                checks.append(
                    _role_line(f"{ROLE_TITLES[role].lower()} model", model, pending, settings)
                )
    except Exception:  # noqa: BLE001 - any failure to read is "not checked", said once
        return {}, [
            Check(
                "local AI roles",
                False,
                "not checked: the catalogue could not be read (see the database line)",
                fatal=False,
            )
        ]
    finally:
        await engine.dispose()
    if config.paused:
        checks.append(_paused_line(config.paused_at, config.paused_by))
    checks.append(_budgets_line(config.budgets(), memory))
    return needed, checks


def _role_line(
    name: str, model: "LocalModel", pending: "LocalModel | None", settings: Settings
) -> Check:
    from framefound.local_models import library, registry

    try:
        there = library.presence(registry.snapshot(model), settings).on_this_machine
    except ValueError as err:
        return Check(name, False, f"{model.repo_id}: {err}", fatal=False)
    after = (
        f"; {pending.repo_id} takes it over once its copy is on this Mac"
        if pending is not None
        else ""
    )
    if there:
        return Check(name, True, f"{model.repo_id} on this Mac{after}", fatal=False)
    return Check(
        name,
        False,
        f"{model.repo_id} has the role but is not on this Mac — AI models page › "
        f"Who does what › Copy again{after}",
        fatal=False,
    )


def _paused_line(paused_at: str, paused_by: str) -> Check:
    when = ""
    if paused_at:
        try:
            when = f" since {datetime.fromisoformat(paused_at).astimezone():%a %H:%M}"
        except ValueError:
            when = f" since {paused_at}"
    who = f" by {paused_by}" if paused_by else ""
    return Check(
        "local AI paused",
        False,
        f"paused{when}{who}: no model runs until Resume on the AI models page",
        fatal=False,
    )


def _budgets_line(budgets: "Budgets", memory: float | None) -> Check:
    from framefound.local_models.schedule import clock_text, has_window

    day = f"{_gb(budgets.day_gb)} GB by day"
    overnight = has_window(budgets)
    night = (
        f"{_gb(budgets.night_gb)} GB {clock_text(budgets.night_start)}–"
        f"{clock_text(budgets.night_end)}"
        if overnight
        else "no overnight window"
    )
    if memory is None:
        return Check("memory budgets", True, f"{day}, {night}", fatal=False)
    total = round(memory)
    in_force = [("daytime", budgets.day_gb)]
    if overnight:
        in_force.append(("overnight", budgets.night_gb))
    for which, gb in sorted(in_force, key=lambda pair: -pair[1]):
        if memory - gb < LOCAL_AI_HEADROOM_GB:
            return Check(
                "memory budgets",
                False,
                f"the {which} budget ({_gb(gb)} GB) leaves under {_gb(LOCAL_AI_HEADROOM_GB)} GB "
                f"of this Mac's {_gb(total)} GB for everything else — lower it on the AI "
                "models page › Sharing this Mac",
                fatal=False,
            )
    return Check("memory budgets", True, f"{day}, {night}, of {_gb(total)} GB", fatal=False)


def run(
    layout: Layout, env: dict[str, str], settings: Settings, launchd: Launchd, parity: bool = False
) -> list[Check]:
    checks = check_agents(launchd, layout, env)
    checks += check_front_door(env)
    checks += asyncio.run(check_services(settings, libraries_fatal=False))
    checks += check_stores(settings)
    checks += check_tools()
    checks += check_onnx(parity)
    checks += check_local_models(layout, env, settings)
    checks += check_backups(layout, env)
    checks += check_power()
    return checks
