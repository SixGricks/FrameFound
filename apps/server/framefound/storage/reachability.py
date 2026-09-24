"""Is a library's folder actually there?

A network share that fails to mount does not make its mountpoint vanish: the
directory it would have been mounted on is still there, just empty. Every
check that asks `is_dir()` passes, a scan walks nothing, and the
honest-looking conclusion is that every file was deleted.

That is how this deployment lost its NAS for 39 days (Aug-Sep 2026): a kernel
update arrived without the module the CIFS mounts needed, the mounts failed
at boot, and nothing said so. A daily scan walked the empty mountpoint,
flagged 7,162 assets missing and reported success, while the health page
measured the VM's own root disk under the library's name and called it fine.

So "reachable" means the folder exists and, when the catalogue already knows
files in it, is not empty. An empty folder is only suspicious in that second
case — a brand-new library legitimately starts with nothing.
"""

import os
from pathlib import Path


def root_state(root: Path) -> str:
    """'ok', 'absent' (missing or unreadable), or 'empty'. Stops at the first
    entry, so it costs one directory read even on a share of millions."""
    try:
        with os.scandir(root) as entries:
            for _ in entries:
                return "ok"
    except OSError:
        return "absent"
    return "empty"


def unreachable_reason(root: Path, known_assets: int) -> str | None:
    """Why this library's files cannot be seen right now, or None if they can.

    `known_assets` is how many files the catalogue already holds for it: an
    empty folder is a failed mount when there should be something there, and
    simply a new library when there should not.
    """
    state = root_state(root)
    if state == "absent":
        return "Library folder is not reachable."
    if state == "empty" and known_assets > 0:
        return (
            f"Library folder is empty, but the catalogue holds {known_assets:,} files "
            "from it — the network share is almost certainly not mounted."
        )
    return None
