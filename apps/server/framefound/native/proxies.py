"""`framefound proxies`: where video proxies live, and moving them.

Proxies are most of the derived data by size and the only part worth
putting somewhere other than the Mac's own disk. A NAS share has the room;
an external SSD is quicker to scrub through. Either is one command, and
changing your mind later is the same command with a different folder:

    framefound proxies move "/Volumes/intel/FrameFound Proxies"
    framefound proxies move "/Volumes/Fast SSD/FrameFound Proxies"

A move copies (never deletes), checks the copy, marks the new folder as the
proxy store, points FrameFound at it and restarts the workers. The old
copies stay until the operator removes them.
"""

import tempfile
from collections.abc import Callable
from pathlib import Path

from framefound.config import Settings
from framefound.native import envfile, installer, proc, services
from framefound.native.launchd import Launchd
from framefound.native.layout import Layout
from framefound.storage import stores

Say = Callable[[str], None]


class ProxyMoveError(RuntimeError):
    """Something the operator has to fix; the message says what."""


def count(root: Path) -> int:
    """Proxies under a store root (`derivatives/ab/<asset>/proxy.mp4`)."""
    return sum(1 for _ in root.glob("derivatives/*/*/proxy.*"))


def copy_command(rsync: str, source: Path, target: Path) -> list[str]:
    """Copy only the proxies, keeping their folders. Resumable: run again
    after an interruption and it carries on."""
    return [
        rsync,
        "-rt",
        "--partial",
        "--info=progress2",
        "--prune-empty-dirs",
        "--include=*/",
        "--include=proxy.*",
        "--exclude=*",
        f"{source}/derivatives/",
        f"{target}/derivatives/",
    ]


def validate_target(target: Path, data_dir: Path) -> None:
    if not target.is_absolute():
        raise ProxyMoveError("Give the full path of the folder, starting with /")
    if target == data_dir:
        return  # back into the data store: always there
    # Never create the drive's or share's own folder: if `/Volumes/Fast SSD`
    # is not mounted, making it would put the "store" on the Mac's disk.
    if not target.parent.is_dir():
        raise ProxyMoveError(
            f"{target.parent} does not exist. Connect the drive or share first; "
            "FrameFound only creates the last folder of the path."
        )
    try:
        target.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target, prefix=".framefound-write-test-"):
            pass
    except OSError as err:
        raise ProxyMoveError(f"{target} is not writable from this Mac ({err})") from err


def move(
    layout: Layout,
    settings: Settings,
    target: Path,
    launchd: Launchd,
    say: Say,
    rsync: str = "rsync",
) -> None:
    data_dir = settings.data_dir
    validate_target(target, data_dir)
    source = stores.proxy_root(data_dir, settings)
    if source != target:
        away = stores.proxy_store_problem(settings)
        if away is not None:
            raise ProxyMoveError(
                f"The current proxy store cannot be copied from: {away} "
                "Connect it, or start the new store empty with --empty and "
                "rebuild with: framefound proxies rebuild"
            )
        existing = count(source)
        if existing:
            say(f"Copying {existing:,} proxies from {source}")
            # Nothing may write a proxy while they are being copied.
            launchd.stop("worker-media")
            try:
                proc.run(copy_command(rsync, source, target))
            finally:
                launchd.start("worker-media")
            copied = count(target)
            if copied < existing:
                raise ProxyMoveError(
                    f"Only {copied:,} of {existing:,} proxies are in {target}. Nothing "
                    "was changed; run the same command again to carry on."
                )
    switch(layout, target, data_dir, launchd, say)
    if source != target and count(source):
        say(
            f"The old copies are still in {source}. Once playback looks right, "
            f"free the space with:\n  find \"{source / 'derivatives'}\" -name 'proxy.*' -delete"
        )


def start_empty(
    layout: Layout, settings: Settings, target: Path, launchd: Launchd, say: Say
) -> None:
    """Point at a new store without copying. For a store that is gone."""
    validate_target(target, settings.data_dir)
    switch(layout, target, settings.data_dir, launchd, say)


def switch(layout: Layout, target: Path, data_dir: Path, launchd: Launchd, say: Say) -> None:
    """Mark the store, record it, and restart what reads the setting."""
    separate = target != data_dir
    if separate:
        stores.mark_proxy_store(target)
    envfile.update(layout.env_file, {"FRAMEFOUND_PROXY_DIR": str(target) if separate else ""})
    # A folder recorded for proxies by an earlier move from the Storage page
    # (storage/stores.py) would outrank the setting just written. The newest
    # instruction wins, so that record goes — and comes back as "the data
    # store" when a bulk folder is set, where no setting at all would mean
    # the bulk folder's own `proxies`.
    stores.clear_override(data_dir, "proxies")
    bulk_setting = envfile.read(layout.env_file).get("FRAMEFOUND_BULK_DIR", "").strip()
    if not separate and (stores.read_state(data_dir).bulk or bulk_setting):
        stores.set_override(data_dir, "proxies", data_dir)
    installer.stop_all(launchd, services.APP_SERVICES)
    for name in services.APP_SERVICES:
        launchd.start(name)
    say(f"Proxies now live in {target}")


def describe(settings: Settings) -> list[str]:
    root = stores.proxy_root(settings.data_dir, settings)
    lines = [f"Proxies live in {root}"]
    problem = stores.proxy_store_problem(settings)
    if problem is not None:
        lines.append(f"NOT CONNECTED: {problem}")
    elif stores.separate_proxy_store(settings) is None:
        lines.append(
            "(with the rest of the derived files; move them with: framefound proxies move <folder>)"
        )
    return lines
