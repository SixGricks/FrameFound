"""Where a worker finds the files, when it is not the server.

The catalogue stores each library's root as the server's own path
(`/media/intel/2026`). A worker on another machine — the Mac mini, with the
same NAS share mounted at `/Volumes/intel` — has to translate that before it
can open anything, and it has to be careful in two ways the server is not:

1. **It must never decide a file is gone.** A wrong or missing mapping, or a
   share that did not mount on that machine, makes every file look deleted.
   The server's scanner is the only judge of "missing"
   (storage/reachability.py has the history of why that judgement is hard
   enough even there).
2. **It must never write derivatives to its own disk by accident.** The data
   store is shared over the network. If that share is not mounted, its
   mountpoint can still exist as an empty local folder, and every thumbnail,
   proxy and transcript would land on the worker's disk — invisible to the
   server, which still records them as made. So the worker requires a marker
   file the server writes into the real store (`DATA_STORE_MARKER`).

On the server `root_map` is empty, `local_root` is the identity and every
check here passes: behaviour there is unchanged.
"""

import contextlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from framefound.config import Settings, get_settings

# Written by the API at start-up into the data store it serves. Its presence
# on a remote worker proves the folder it sees is that store, not an empty
# mountpoint left behind by a failed mount.
DATA_STORE_MARKER = ".framefound-data-store"


# How long a remote worker waits before handing back a job it cannot do.
# Without the pause it would take the same message straight back and spin;
# with it, one hand-back per worker process every half minute while a share
# is down, and the server's own workers take the jobs meanwhile.
HAND_BACK_PAUSE_S = 30.0


class NotHere(RuntimeError):
    """This machine cannot do this job, though another very likely can.

    Raised only on a remote worker. The task layer (celery_app.FrameFoundTask)
    turns it into a requeue rather than a failure or a retry count."""


class UnmappedRoot(NotHere):
    """A library root no `root_map` entry covers. Message is safe to display."""


@dataclass(frozen=True)
class RootMapping:
    server: PurePosixPath
    local: Path


def parse_root_map(raw: str) -> list[RootMapping]:
    """`/media/intel=/Volumes/intel;/media/b=/Volumes/B` → mappings, longest
    server prefix first so a nested library wins over its parent share.

    Raises ValueError on a malformed entry: a typo here should stop the worker
    at start-up, not surface as thousands of unreachable files."""
    mappings: list[RootMapping] = []
    for entry in raw.replace("\n", ";").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        server, sep, local = entry.partition("=")
        server, local = server.strip(), local.strip()
        if not sep or not server or not local:
            raise ValueError(f"root_map entry {entry!r} is not 'server path=local path'")
        server_path = PurePosixPath(server)
        if not server_path.is_absolute():
            raise ValueError(f"root_map server path {server!r} must be absolute")
        local_path = Path(local)
        if not local_path.is_absolute():
            raise ValueError(f"root_map local path {local!r} must be absolute")
        mappings.append(RootMapping(server_path, local_path))
    mappings.sort(key=lambda m: len(m.server.parts), reverse=True)
    return mappings


def translate(root_path: str, mappings: list[RootMapping]) -> Path | None:
    """`root_path` through the first mapping that covers it (they come
    longest prefix first from `parse_root_map`), or None when none does.

    Whole path components only: `/media/intel` covers `/media/intel/2026`
    but not `/media/intel-archive`. Also what a migration uses to rewrite
    each library's root once, for good (native/migrate.py)."""
    server_root = PurePosixPath(root_path)
    for mapping in mappings:
        if server_root == mapping.server or mapping.server in server_root.parents:
            return mapping.local.joinpath(*server_root.relative_to(mapping.server).parts)
    return None


def is_remote_worker(settings: Settings | None = None) -> bool:
    """True when this process runs on a machine other than the server."""
    return bool((settings or get_settings()).root_map.strip())


def local_root(root_path: str, settings: Settings | None = None) -> Path:
    """The library root as this machine sees it.

    Matches whole path components (`/media/intel` maps `/media/intel/2026`
    but not `/media/intel-archive`). On a remote worker an unmapped root
    raises rather than falling back to the server path, which would only
    "work" by finding nothing."""
    settings = settings or get_settings()
    if not settings.root_map.strip():
        return Path(root_path)
    found = translate(root_path, parse_root_map(settings.root_map))
    if found is not None:
        return found
    raise UnmappedRoot(
        f"No root_map entry covers {root_path} on this worker — add one, "
        "e.g. /media/<share>=/Volumes/<share>."
    )


def data_store_problem(data_dir: Path, settings: Settings | None = None) -> str | None:
    """Why this process must not write derivatives right now, or None.

    Only remote workers check: on the server the data store is a local disk
    or a bind mount Docker would refuse to start without."""
    if not is_remote_worker(settings):
        return None
    try:
        if (data_dir / DATA_STORE_MARKER).is_file():
            return None
    except OSError as err:  # a hung or dropped network mount
        return f"The shared data store at {data_dir} cannot be read ({err.strerror or err})."
    return (
        f"The shared data store is not mounted at {data_dir} on this worker "
        f"(no {DATA_STORE_MARKER} there), so nothing will be written."
    )


def write_marker(data_dir: Path) -> None:
    """Called by the API at start-up. Best effort: a read-only or missing
    store is reported by the health checks, not here."""
    marker = data_dir / DATA_STORE_MARKER
    with contextlib.suppress(OSError):
        if not marker.exists():
            marker.write_text(
                "This folder is a FrameFound data store. Remote workers check for "
                "this file before writing, so they never fill an empty mountpoint.\n"
            )
