"""Where derived files live: the data store, and optionally a proxy store.

Everything FrameFound generates hangs off `data_dir` by a relative path kept
in the database. Video proxies are the exception an operator may want: they
are most of the bytes, and the rest (thumbnails, frames, transcripts) is what
every page load reads. So proxies alone can live somewhere else —
`FRAMEFOUND_PROXY_DIR`, a NAS share or an external drive — under the *same*
relative paths:

    <data_dir>/derivatives/ab/<asset>/thumbnail.webp
    <proxy_dir>/derivatives/ab/<asset>/proxy.mp4

Nothing in the database changes when proxies move; it is a copy and a new
value for that one setting (`framefound proxies move`).

A separate store is a mount that can be missing. The folder it would have
been mounted on may still exist, empty — the failure storage/reachability.py
was written about — so a separate proxy store must carry a marker file, and
nothing is written to it without one. The marker is only ever placed by the
operator's own command, never automatically: writing it on start-up would
bless whatever empty folder happened to be there.
"""

from pathlib import Path, PurePosixPath

from framefound.config import Settings, get_settings
from framefound.storage.worker_roots import DATA_STORE_MARKER

PROXY_STORE_MARKER = ".framefound-proxy-store"
# A folder holding either is FrameFound's own output. The scanner never
# catalogues inside one, wherever an operator put it.
STORE_MARKERS = frozenset({DATA_STORE_MARKER, PROXY_STORE_MARKER})


def is_proxy(relative_path: str) -> bool:
    """True for a video proxy's path (`derivatives/ab/<asset>/proxy.mp4`)."""
    path = PurePosixPath(relative_path.replace("\\", "/"))
    return path.parts[:1] == ("derivatives",) and path.stem == "proxy"


def separate_proxy_store(settings: Settings | None = None) -> Path | None:
    """The proxy store when it is not simply the data store, else None."""
    settings = settings or get_settings()
    if settings.proxy_dir is None or settings.proxy_dir == settings.data_dir:
        return None
    return settings.proxy_dir


def proxy_root(data_dir: Path, settings: Settings | None = None) -> Path:
    """The folder proxies' relative paths hang off."""
    return separate_proxy_store(settings) or data_dir


def derivative_path(data_dir: Path, relative_path: str, settings: Settings | None = None) -> Path:
    """A derived file's absolute path: under the proxy store for a proxy when
    one is configured, under `data_dir` for everything else."""
    if is_proxy(relative_path):
        return proxy_root(data_dir, settings) / relative_path
    return data_dir / relative_path


def proxy_store_problem(settings: Settings | None = None) -> str | None:
    """Why proxies cannot be read or written right now, or None.

    Always None when proxies live in the data store. For a separate store the
    marker is the test: present, the folder is the store; absent, it is not
    mounted (or was never set up) whatever else is there."""
    store = separate_proxy_store(settings)
    if store is None:
        return None
    try:
        if (store / PROXY_STORE_MARKER).is_file():
            return None
    except OSError as err:  # a hung or dropped network mount
        return f"The proxy store at {store} cannot be read ({err.strerror or err})."
    return (
        f"The proxy store is not connected: {store} has no {PROXY_STORE_MARKER}. "
        "Reconnect the drive or share; proxies resume on their own."
    )


def mark_proxy_store(store: Path) -> None:
    """Place the marker. Called only by the operator's own commands."""
    store.mkdir(parents=True, exist_ok=True)
    (store / PROXY_STORE_MARKER).write_text(
        "This folder is a FrameFound proxy store. FrameFound writes proxies here only "
        "while this file is present, and never catalogues anything inside.\n"
    )
