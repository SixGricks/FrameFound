"""`framefound doctor`: is this install healthy, one line per thing.

Every line is something that has actually gone wrong on a FrameFound
install, said in terms of what to do about it. FAIL means FrameFound is
broken or about to be; warn means it is running and something deserves a
look. The same checks a worker on another machine runs (ops/worker_check.py)
make up the middle of it.
"""

import asyncio
import re
import shutil
import tempfile
from pathlib import Path

from framefound.config import Settings
from framefound.native import backup, proc, services
from framefound.native.launchd import Launchd
from framefound.native.layout import Layout
from framefound.ops.worker_check import Check, check_onnx, check_services, check_tools
from framefound.storage import stores
from framefound.storage.worker_roots import DATA_STORE_MARKER

STALE_BACKUP_S = 48 * 3600


def check_agents(launchd: Launchd, layout: Layout) -> list[Check]:
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

    store = stores.separate_proxy_store(settings)
    if store is None:
        checks.append(
            Check(
                "proxy store",
                True,
                "with the data store — move with: framefound proxies move <folder>",
                fatal=False,
            )
        )
        return checks
    away = stores.proxy_store_problem(settings)
    if away is not None:
        # Not fatal: everything but proxy playback and new proxies carries on.
        checks.append(Check("proxy store", False, away, fatal=False))
        return checks
    problem = _writable(store)
    if problem is not None:
        checks.append(Check("proxy store", False, f"{store} is not writable ({problem})"))
    else:
        free = shutil.disk_usage(store).free / 1024**3
        checks.append(Check("proxy store", True, f"{store}, {free:.0f} GB free"))
    return checks


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
    copy_dir = (env.get("FRAMEFOUND_BACKUP_COPY_DIR") or "").strip()
    if not copy_dir:
        checks.append(
            Check(
                "backup copy",
                False,
                "backups stay on this Mac only — set FRAMEFOUND_BACKUP_COPY_DIR to a NAS folder",
                fatal=False,
            )
        )
    elif not Path(copy_dir).is_dir():
        checks.append(Check("backup copy", False, f"{copy_dir} is not reachable", fatal=False))
    else:
        age = backup.newest_age_s(Path(copy_dir))
        detail = copy_dir if age is None else f"{copy_dir}, newest {age / 3600:.0f} h old"
        checks.append(Check("backup copy", True, detail, fatal=False))
    return checks


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


def run(
    layout: Layout, env: dict[str, str], settings: Settings, launchd: Launchd, parity: bool = False
) -> list[Check]:
    checks = check_agents(launchd, layout)
    checks += check_front_door(env)
    checks += asyncio.run(check_services(settings, libraries_fatal=False))
    checks += check_stores(settings)
    checks += check_tools()
    checks += check_onnx(parity)
    checks += check_backups(layout, env)
    checks += check_power()
    return checks
