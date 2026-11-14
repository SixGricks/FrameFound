"""Where derived files live: the data store, and a store for each bulky kind.

Everything FrameFound generates hangs off `data_dir` by a relative path kept
in the database. What every page load reads (thumbnails, frames, transcripts)
stays there, on this machine's own disk. The bulky kinds can each live
somewhere else — a NAS share, an external drive:

    proxies   <root>/derivatives/ab/<asset>/proxy.mp4   the same relative path
    renders   <root>/<slideshow>.mp4
    exports   <root>/listings/<listing>.zip
    basemaps  <root>/<name>.pmtiles
    models    <root>/library/...          the model library (local_models/
                                          library.py); a model with a role is
                                          also copied to this machine
    backups   <root>/framefound-*.tar.gz  the copy kept off this machine; the
                                          first copy stays in <data_dir>/backups

Nothing in the database changes when a kind moves. A slideshow's row still
says `renders/<id>.mp4`; only the folder that path hangs off is different.

Where a kind lives, first match wins:

    1. its own entry in `<data_dir>/.framefound-stores.json`, which is what a
       move writes (storage/moves.py)
    2. its own setting: `FRAMEFOUND_PROXY_DIR` for proxies,
       `FRAMEFOUND_BACKUP_COPY_DIR` for backup copies
    3. `<bulk>/<kind>`: one folder for everything bulky, from that file's
       `bulk` or else `FRAMEFOUND_BULK_DIR`
    4. where it always was: under `data_dir` (`models_dir` for models; for
       backups, no copy at all)

The file exists because a move has to take effect without a restart:
`get_settings()` is cached for the life of a process and every worker child
holds its own copy. It is read through a cache keyed on the file's own
`stat`, so a lookup costs one local `stat` and a switch reaches every process
on its next lookup. A setting (2, 3) is for install time, when a restart is
part of the job anyway.

A separate store is a mount that can be missing. The folder it would have
been mounted on may still exist, empty — the failure storage/reachability.py
was written about — so a separate store must carry a marker file, and nothing
is written to it without one. The marker is only ever placed by the
operator's own command, never automatically: writing it on start-up would
bless whatever empty folder happened to be there.
"""

import contextlib
import json
import os
import shutil
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath

import structlog

from framefound.config import Settings, get_settings
from framefound.storage.worker_roots import DATA_STORE_MARKER, is_remote_worker

log = structlog.get_logger()

PROXY_STORE_MARKER = ".framefound-proxy-store"
BULK_STORE_MARKER = ".framefound-bulk-store"
# Placed in a folder while a move is copying into it (storage/moves.py). Not
# a store yet — nothing is read from or written to it on the strength of this
# file — but already FrameFound's own, and the scanner must stay out: a copy
# onto the share the footage is on would otherwise be catalogued halfway.
INCOMING_MARKER = ".framefound-incoming"
# A folder holding any of these is FrameFound's own output. The scanner never
# catalogues inside one, wherever an operator put it.
STORE_MARKERS = frozenset(
    {DATA_STORE_MARKER, PROXY_STORE_MARKER, BULK_STORE_MARKER, INCOMING_MARKER}
)

KINDS = ("proxies", "renders", "exports", "basemaps", "models", "backups")
# Kinds whose relative paths start with their own name (`renders/<id>.mp4`).
# Inside the kind's folder that first component is dropped; a proxy keeps its
# whole path, so a proxy store made before there were kinds needs no change.
_NAMED_KINDS = frozenset({"renders", "exports", "basemaps"})

STORES_FILE = ".framefound-stores.json"
# A file still being written into a store. It only takes its real name once
# it is whole, so a share that drops mid-copy never leaves something that
# looks like a finished video.
PARTIAL_SUFFIX = ".partial"
# A move pauses the writers of the kind it is moving and renews the pause as
# it works. One nobody has renewed for this long belongs to a move that died,
# and must not hold proxies or renders back for ever.
PAUSE_EXPIRES_S = 15 * 60.0
# The stores file is changed under a lock that is held for one small write.
LOCK_WAIT_S = 10.0
LOCK_STALE_S = 30.0


@dataclass(frozen=True)
class Words:
    store: str  # "the proxy store is not connected"
    things: str  # "proxies are being moved"
    then: str  # what picks the work up again once the store is back


_WORDS = {
    # Nothing queues a proxy a second time, so the ones skipped while the
    # store was away are asked for: ops/proxy_store.py finds and queues them.
    "proxies": Words(
        "proxy store",
        "proxies",
        "then make the proxies that were skipped (Make the missing proxies again, on the "
        "Storage page).",
    ),
    "renders": Words("slideshow store", "slideshow videos", "then render the slideshow again."),
    "exports": Words("export store", "listing exports", "then export again."),
    "basemaps": Words("basemap store", "basemaps", "then download the basemap again."),
    "models": Words("model library", "models", "then try again."),
    "backups": Words("backup copy folder", "backup copies", "the next backup is copied again."),
}

_MARKER_TEXT = {
    PROXY_STORE_MARKER: (
        "This folder is a FrameFound proxy store. FrameFound writes proxies here only "
        "while this file is present, and never catalogues anything inside.\n"
    ),
    BULK_STORE_MARKER: (
        "This folder is FrameFound bulk storage. FrameFound writes here only while "
        "this file is present, and never catalogues anything inside.\n"
    ),
    INCOMING_MARKER: (
        "FrameFound is copying files into this folder. It becomes a store when the "
        "copy has been checked; until then nothing is read from it.\n"
    ),
}


# --- the stores file ---------------------------------------------------------


@dataclass(frozen=True)
class StoreState:
    """What `.framefound-stores.json` says. Never mutated: a change writes a
    new file, and every process picks that up on its next lookup."""

    bulk: Path | None = None
    kinds: Mapping[str, Path] = field(default_factory=dict)
    # kind -> when its pause was last renewed (None: written by hand, kept
    # until removed by hand).
    paused: Mapping[str, float | None] = field(default_factory=dict)
    # kind -> where it lived before its last completed move. The old copies
    # are still there until `moves.remove_old_copies` is asked to remove them.
    previous: Mapping[str, Path] = field(default_factory=dict)
    # The kinds whose `previous` folder never had to carry a marker (a backup
    # copy folder named by its setting). Whether such a folder is connected
    # cannot be read off a marker it never had.
    unmarked: frozenset[str] = frozenset()
    # Set when the file is there but cannot be understood. Guessing "nothing
    # configured" then would send proxies back to this machine's disk and
    # report every one on the NAS missing, so the bulky kinds pause instead.
    damaged: str | None = None

    def is_paused(self, kind: str, now: float | None = None) -> bool:
        if kind not in self.paused:
            return False
        since = self.paused[kind]
        return since is None or (time.time() if now is None else now) - since < PAUSE_EXPIRES_S


_EMPTY = StoreState()
# path of the file -> (what `stat` said, what the file said then)
_CACHE: dict[Path, tuple[tuple[int, int, int] | None, StoreState]] = {}


def read_state(data_dir: Path) -> StoreState:
    """The stores file as it is on disk now."""
    path = data_dir / STORES_FILE
    try:
        found = path.stat()
        # Inode as well as time and size: two switches inside one tick of a
        # coarse filesystem clock are still two different files, because each
        # write is a new file renamed into place.
        stamp: tuple[int, int, int] | None = (found.st_mtime_ns, found.st_size, found.st_ino)
    except (FileNotFoundError, NotADirectoryError):
        stamp = None
    except OSError as err:
        return StoreState(damaged=_damaged(path, err.strerror or str(err)))
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    state = _EMPTY if stamp is None else _parse(path, data_dir)
    _CACHE[path] = (stamp, state)
    return state


def _damaged(path: Path, why: str) -> str:
    return (
        f"FrameFound's record of where its stores are ({path}) cannot be read: {why}. "
        "Nothing is written to them until it is repaired or removed."
    )


def _parse(path: Path, data_dir: Path) -> StoreState:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("it is not a JSON object")
        bulk = raw.get("bulk")
        times = raw.get("paused_at") or {}
        previous = _folders(raw.get("previous"), data_dir, "previous")
        return StoreState(
            bulk=_folder(bulk, data_dir, "bulk") if bulk else None,
            kinds=_folders(raw.get("kinds"), data_dir, "kinds"),
            paused={
                str(kind): float(times[kind]) if kind in times else None
                for kind in raw.get("paused") or []
            },
            previous=previous,
            unmarked=frozenset(
                str(kind) for kind in raw.get("previous_unmarked") or [] if kind in previous
            ),
        )
    except (OSError, ValueError, TypeError, AttributeError) as err:
        log.warning("stores.file_damaged", path=str(path), error=str(err))
        return StoreState(damaged=_damaged(path, str(err)))


def _folders(raw: object, data_dir: Path, name: str) -> dict[str, Path]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f'"{name}" is not a list of folders by kind')
    # A kind this version does not know (a newer version's file) is passed
    # over rather than treated as damage.
    return {
        kind: _folder(value, data_dir, f"{name}.{kind}")
        for kind, value in raw.items()
        if kind in KINDS
    }


def _folder(value: object, data_dir: Path, name: str) -> Path:
    """A folder as the file holds it. One inside the data store is written
    relative to it ("." is the data store itself), so the entry still means
    the same folder to a worker that mounts the store somewhere else, and
    after the data store itself is moved."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'"{name}" is not a folder')
    folder = Path(value)
    if folder.is_absolute():
        return folder
    if ".." in folder.parts:
        raise ValueError(f'"{name}" points outside the data store')
    return data_dir / folder


def _written(folder: Path, data_dir: Path) -> str:
    try:
        return folder.relative_to(data_dir).as_posix()
    except ValueError:
        return str(folder)


def _write(data_dir: Path, state: StoreState) -> None:
    """Replace the file whole: written beside it and renamed, so a reader
    sees the old settings or the new ones and never half of either."""
    path = data_dir / STORES_FILE
    body: dict[str, object] = {"version": 1}
    if state.bulk is not None:
        body["bulk"] = _written(state.bulk, data_dir)
    body["kinds"] = {kind: _written(folder, data_dir) for kind, folder in state.kinds.items()}
    body["paused"] = sorted(state.paused)
    stamped = {kind: since for kind, since in state.paused.items() if since is not None}
    if stamped:
        body["paused_at"] = stamped
    if state.previous:
        body["previous"] = {
            kind: _written(folder, data_dir) for kind, folder in state.previous.items()
        }
    if state.unmarked & set(state.previous):
        body["previous_unmarked"] = sorted(state.unmarked & set(state.previous))
    if not (state.bulk or state.kinds or state.paused or state.previous):
        # Nothing left to say: no file is the same as an empty one, and it is
        # what an install that never moved anything has.
        path.unlink(missing_ok=True)
    else:
        data_dir.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f"{path.name}.{os.getpid()}{PARTIAL_SUFFIX}")
        with partial.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(body, indent=2) + "\n")
        os.replace(partial, path)
    _CACHE.pop(path, None)


class StoresFileError(RuntimeError):
    """The stores file cannot be changed: it is there but cannot be
    understood, or another process has held it too long. Changing it blind
    could throw away the one record of where a kind lives; the message says
    what to do."""


@contextlib.contextmanager
def _locked(data_dir: Path) -> Iterator[None]:
    """One writer at a time, across processes.

    A change is a read and then a write. A move's switch landing between
    another writer's two halves would be written straight back over — the
    kind returned to its old folder with nothing to say it had happened."""
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = data_dir / f"{STORES_FILE}.lock"
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                    # Nothing holds it this long: its owner died mid-write.
                    lock.unlink(missing_ok=True)
                    continue
            if time.monotonic() >= deadline:
                raise StoresFileError(
                    f"FrameFound's record of where its stores are is in use ({lock}). "
                    "Try again in a moment."
                ) from None
            time.sleep(0.02)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink(missing_ok=True)


def _update(data_dir: Path, change: Callable[[StoreState], StoreState]) -> None:
    with _locked(data_dir):
        state = read_state(data_dir)
        if state.damaged is not None:
            raise StoresFileError(state.damaged)
        changed = change(state)
        if changed != state:
            _write(data_dir, changed)


def load_overrides(data_dir: Path) -> dict[str, Path]:
    """Each kind's own recorded folder (rule 1 of the module docstring)."""
    return dict(read_state(data_dir).kinds)


def save_overrides(data_dir: Path, kinds: Mapping[str, Path]) -> None:
    wanted = {_known(kind): folder for kind, folder in kinds.items()}
    _update(data_dir, lambda state: replace(state, kinds=wanted))


def set_override(data_dir: Path, kind: str, folder: Path) -> None:
    """Record that `kind` lives in `folder`, effective in every process at
    its next lookup. The caller has copied and checked; this only switches."""
    _known(kind)
    _update(data_dir, lambda state: replace(state, kinds={**state.kinds, kind: folder}))


def clear_override(data_dir: Path, kind: str) -> None:
    """Forget `kind`'s own folder, so its setting or the bulk folder decides
    again. Writes nothing when there was nothing to forget."""
    if _known(kind) in read_state(data_dir).kinds:
        _update(data_dir, lambda state: replace(state, kinds=_without(state.kinds, kind)))


def set_bulk_root(
    data_dir: Path, folder: Path | None, keep: Mapping[str, Path] | None = None
) -> None:
    """Record the one folder for everything bulky (rule 3). Every kind
    without a folder or setting of its own lives in it from the next lookup,
    so a kind with files elsewhere is given its own entry in the same write
    (`keep`) — `moves.adopt_bulk_root` works out which."""
    kept = {_known(kind): where for kind, where in (keep or {}).items()}
    _update(data_dir, lambda state: replace(state, bulk=folder, kinds={**state.kinds, **kept}))


def switch(
    data_dir: Path,
    kind: str,
    folder: Path,
    *,
    previous: Path | None = None,
    previous_marked: bool = True,
) -> None:
    """The last step of a move, in one write: `kind` lives in `folder`, its
    writers may carry on, and `previous` (when given) is remembered as where
    the old copies still are. One write, so no process ever sees the new
    folder with the pause already gone or the old one with it still on.

    `previous_marked` is whether that old folder had to carry a store marker
    (`needs_marker`), which is how the removal of the old copies later tells
    it is still connected."""
    _known(kind)

    def change(state: StoreState) -> StoreState:
        olds = dict(state.previous)
        plain = set(state.unmarked)
        if previous is not None:
            olds[kind] = previous
            if previous_marked:
                plain.discard(kind)
            else:
                plain.add(kind)
        if olds.get(kind) == folder:
            del olds[kind]  # moved back onto its old copies: they are the live ones again
            plain.discard(kind)
        return replace(
            state,
            kinds={**state.kinds, kind: folder},
            paused=_without(state.paused, kind),
            previous=olds,
            unmarked=frozenset(plain),
        )

    _update(data_dir, change)


def pause(data_dir: Path, kind: str) -> None:
    """Stop new writes of `kind` (a move is taking its last look). Call
    again to renew: an unrenewed pause lapses after PAUSE_EXPIRES_S."""
    _known(kind)
    _update(data_dir, lambda state: replace(state, paused={**state.paused, kind: time.time()}))


def resume(data_dir: Path, kind: str) -> None:
    if kind in read_state(data_dir).paused:
        _update(data_dir, lambda state: replace(state, paused=_without(state.paused, kind)))


def set_previous(data_dir: Path, kind: str, folder: Path | None) -> None:
    """Remember (or, with None, forget) where `kind`'s old copies are."""
    _known(kind)
    if folder is not None:
        _update(
            data_dir,
            lambda state: replace(
                state,
                previous={**state.previous, kind: folder},
                unmarked=state.unmarked - {kind},
            ),
        )
    elif kind in read_state(data_dir).previous:
        _update(
            data_dir,
            lambda state: replace(
                state,
                previous=_without(state.previous, kind),
                unmarked=state.unmarked - {kind},
            ),
        )


def _without[T](entries: Mapping[str, T], kind: str) -> dict[str, T]:
    return {name: value for name, value in entries.items() if name != kind}


# --- where a kind lives ------------------------------------------------------


def _known(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"{kind!r} is not a kind of storage ({', '.join(KINDS)})")
    return kind


def _state(settings: Settings) -> StoreState:
    # The file's folders are the server's own paths. A worker on another
    # machine sees the same shares somewhere else, so it goes by its own
    # settings alone and never guesses (see `not_here`).
    return _EMPTY if is_remote_worker(settings) else read_state(settings.data_dir)


def default_root(kind: str, settings: Settings | None = None) -> Path | None:
    """Where `kind` lives when nothing says otherwise: where it always did."""
    settings = settings or get_settings()
    if _known(kind) == "proxies":
        return settings.data_dir  # proxies keep `derivatives/...` beneath it
    if kind == "models":
        return settings.models_dir
    if kind == "backups":
        return None  # no off-machine copy unless one is asked for
    return settings.data_dir / kind


def bulk_root(settings: Settings | None = None) -> Path | None:
    """The one folder for everything bulky, when there is one."""
    settings = settings or get_settings()
    return _state(settings).bulk or settings.bulk_dir


def _resolve(kind: str, settings: Settings) -> tuple[Path | None, str]:
    """(folder, which rule of the module docstring chose it)."""
    state = _state(settings)
    if _known(kind) in state.kinds:
        return state.kinds[kind], "own"
    if kind == "proxies" and settings.proxy_dir is not None:
        return settings.proxy_dir, "setting"
    if kind == "backups" and settings.backup_copy_dir is not None:
        return settings.backup_copy_dir, "setting"
    bulk = state.bulk or settings.bulk_dir
    if bulk is not None:
        return bulk / kind, "bulk"
    return default_root(kind, settings), "default"


def placed_by(kind: str, settings: Settings | None = None) -> str:
    """Which rule of the module docstring decides where `kind` lives: "own",
    "setting", "bulk" or "default"."""
    return _resolve(kind, settings or get_settings())[1]


def words(kind: str) -> Words:
    """How messages speak of `kind`."""
    return _WORDS[_known(kind)]


def kind_root(kind: str, settings: Settings | None = None) -> Path | None:
    """The folder `kind`'s files hang off. None only for backups with no
    off-machine copy configured."""
    return _resolve(kind, settings or get_settings())[0]


def kind_folder(kind: str, settings: Settings | None = None) -> Path:
    """`kind_root` for a kind that always has a folder."""
    root = kind_root(kind, settings)
    if root is None:
        raise ValueError(f"No folder is set for {kind}")
    return root


def is_separate(kind: str, settings: Settings | None = None) -> bool:
    """True when `kind` lives anywhere but its default place — that is, on
    something that can be unplugged or unmounted."""
    settings = settings or get_settings()
    root = kind_root(kind, settings)
    return root is not None and root != default_root(kind, settings)


def marker_for(kind: str) -> str:
    """The file a separate store for `kind` must hold to count as connected."""
    return PROXY_STORE_MARKER if _known(kind) == "proxies" else BULK_STORE_MARKER


def is_proxy(relative_path: str) -> bool:
    """True for a video proxy's path (`derivatives/ab/<asset>/proxy.mp4`)."""
    path = PurePosixPath(relative_path.replace("\\", "/"))
    return path.parts[:1] == ("derivatives",) and path.stem == "proxy"


def kind_of(relative_path: str) -> str | None:
    """Which kind a stored relative path belongs to, or None for everything
    that simply lives in the data store."""
    # Asked for every thumbnail served, so the path is taken apart once.
    path = PurePosixPath(relative_path.replace("\\", "/"))
    parts = path.parts
    if parts[:1] == ("derivatives",):
        return "proxies" if path.stem == "proxy" else None
    if len(parts) < 2 or parts[0] not in _NAMED_KINDS:
        return None
    if parts[:2] == ("renders", "work"):
        return None  # a render's scratch: always on this machine's disk
    return parts[0]


def path_for(relative_path: str, settings: Settings | None = None) -> Path:
    """A derived file's absolute path, in whichever store holds its kind."""
    settings = settings or get_settings()
    return derivative_path(settings.data_dir, relative_path, settings)


# --- is it there -------------------------------------------------------------


def needs_marker(kind: str, settings: Settings | None = None) -> bool:
    """Whether the folder `kind` lives in must carry its marker to count as
    connected. Not in its default place, which is this machine's own disk;
    and not a backup copy folder named by FRAMEFOUND_BACKUP_COPY_DIR, a
    setting older than the markers that has always meant any folder that is
    there."""
    settings = settings or get_settings()
    root, rule = _resolve(kind, settings)
    if root is None or root == default_root(kind, settings):
        return False
    return not (kind == "backups" and rule == "setting")


def kind_problem(kind: str, settings: Settings | None = None) -> str | None:
    """Why `kind`'s files cannot be read or written right now, or None.

    Always None while the kind lives in its default place. For a separate
    store the marker is the test: present, the folder is the store; absent,
    it is not mounted (or was never set up) whatever else is there."""
    settings = settings or get_settings()
    damaged = _state(settings).damaged
    if damaged is not None:
        return damaged
    root = _resolve(kind, settings)[0]
    if root is None or root == default_root(kind, settings):
        return None
    words = _WORDS[kind]
    try:
        if not needs_marker(kind, settings):
            if root.is_dir():
                return None
            return f"The {words.store} is not connected: {root} is not reachable."
        if (root / marker_for(kind)).is_file():
            return None
    except OSError as err:  # a hung or dropped network mount
        return f"The {words.store} at {root} cannot be read ({err.strerror or err})."
    return (
        f"The {words.store} is not connected: {root} has no {marker_for(kind)}. "
        f"Reconnect the drive or share; {words.then}"
    )


def write_problem(kind: str, settings: Settings | None = None) -> str | None:
    """Why nothing new of `kind` may be written right now, or None: its
    store is away, or it is in the last steps of being moved. Reading is
    unaffected by a move — ask `kind_problem` for that."""
    settings = settings or get_settings()
    problem = kind_problem(kind, settings)
    if problem is not None:
        return problem
    # Kind names, not paths, so a worker on another machine honours it too.
    if read_state(settings.data_dir).is_paused(kind):
        words = _WORDS[kind]
        return (
            f"{words.things.capitalize()} are being moved to another folder. Nothing "
            f"new is written until that finishes; {words.then}"
        )
    return None


def away(kind: str, settings: Settings | None = None) -> str | None:
    """`kind_problem` in a sentence fit for anyone signed in: no paths."""
    settings = settings or get_settings()
    if kind_problem(kind, settings) is None:
        return None
    if _state(settings).damaged is not None:
        return "FrameFound's storage settings cannot be read. An administrator can repair them."
    return f"The {_WORDS[kind].store} is not connected. Reconnect the drive or share and try again."


def unwritable(kind: str, settings: Settings | None = None) -> str | None:
    """`write_problem` in a sentence fit for anyone signed in."""
    settings = settings or get_settings()
    gone = away(kind, settings)
    if gone is not None:
        return gone
    if write_problem(kind, settings) is not None:
        return (
            f"{_WORDS[kind].things.capitalize()} are being moved to another folder. "
            "Try again when the move has finished."
        )
    return None


@dataclass(frozen=True)
class Located:
    path: Path
    there: bool
    # Set when the file is not there because its store is away, which is a
    # different thing to tell somebody than "it is gone".
    away: str | None


def locate(relative_path: str, settings: Settings | None = None) -> Located:
    """Where a stored file is, whether it is there, and if not, whether that
    is only because its store is away.

    Looks at the store, which may be a share that has hung: a request
    handler calls this in a thread (`asyncio.to_thread`), never on the event
    loop."""
    settings = settings or get_settings()
    path = path_for(relative_path, settings)
    try:
        there = path.is_file()
    except OSError:
        there = False
    kind = kind_of(relative_path)
    return Located(path, there, None if there or kind is None else away(kind, settings))


def not_here(kind: str, settings: Settings | None = None) -> str | None:
    """On a worker running on another machine: why it must leave `kind` to
    the server, or None.

    The server's record names folders as the server sees them. A worker that
    has no setting of its own for the kind would otherwise write into the
    shared data store, where the server no longer looks."""
    settings = settings or get_settings()
    if not is_remote_worker(settings):
        return None
    state = read_state(settings.data_dir)
    placed = state.kinds.get(kind) or (state.bulk / kind if state.bulk else None)
    if placed is None or is_separate(kind, settings):
        return None
    try:
        placed.relative_to(settings.data_dir)
        return None  # inside the shared data store, which this worker has
    except ValueError:
        pass
    return (
        f"The server keeps {_WORDS[kind].things} in {placed}, and this worker has no "
        "setting for where that is here (FRAMEFOUND_PROXY_DIR or FRAMEFOUND_BULK_DIR)."
    )


# --- making and filling a store ---------------------------------------------


def mark_store(folder: Path, marker: str, **facts: str) -> None:
    """Place a marker. Called only by the operator's own commands.

    `facts` are written under the sentence as `name: value` lines
    (`marker_facts` reads them back): which kind the folder is for, so that
    the kinds sharing one marker name can still be told apart."""
    folder.mkdir(parents=True, exist_ok=True)
    lines = "".join(f"{name.replace('_', '-')}: {value}\n" for name, value in facts.items())
    (folder / marker).write_text(_MARKER_TEXT[marker] + lines, encoding="utf-8")


def marker_facts(folder: Path, marker: str) -> dict[str, str]:
    """The `name: value` lines of a marker in `folder`. Empty for a marker
    that has none (every marker placed before they were written), is not
    there, or cannot be read."""
    try:
        text = (folder / marker).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    facts: dict[str, str] = {}
    for line in text.splitlines():
        name, found, value = line.partition(": ")
        if found and name.replace("-", "").isalpha() and name.islower():
            facts[name] = value.strip()
    return facts


def mark_bulk_root(root: Path) -> None:
    """Make `root` the folder for everything bulky: its own marker (which
    also keeps the scanner out of everything beneath it) and a marked folder
    for each kind. Called only by the operator's own commands."""
    mark_store(root, BULK_STORE_MARKER)
    for kind in KINDS:
        mark_store(root / kind, marker_for(kind), kind=kind)


def same_folder(one: Path, other: Path) -> bool:
    """True when the two paths are one directory, whatever they are called.

    Paths are compared as text everywhere else, and text is not enough
    before something is deleted: `/Volumes/Photos/Framefound` and
    `.../FrameFound` are one folder on a Mac's case-insensitive volumes and
    two strings to Python; a symlink is another name for its target; and
    macOS mounts a share a second time as `/Volumes/Photos-1`. Treated as two
    stores, the "old copies" of one are the only files of the other.

    Looks at both folders (shares, possibly): never called on the event loop."""
    if one == other:
        return True
    try:
        if not (one.is_dir() and other.is_dir()):
            return False
        if os.path.samefile(one, other):
            return True
    except OSError:
        return False
    # One share mounted twice is two devices to `stat`, so the same file has
    # two identities. A file made through one name and found through the
    # other settles it. The name is new each time, so no cache of "that file
    # is not there" can answer for it.
    probe = f".framefound-same-folder-{os.getpid()}-{time.time_ns()}"
    for made_in, looked_in in ((other, one), (one, other)):
        try:
            (made_in / probe).write_bytes(b"")
        except OSError:
            continue  # not writable through this name: try the other
        try:
            return (looked_in / probe).exists()
        except OSError:
            return False
        finally:
            with contextlib.suppress(OSError):
                (made_in / probe).unlink(missing_ok=True)
    return False


def _in_root(root: Path, kind: str, relative_path: str) -> Path:
    """Where `relative_path` sits in a folder of `kind`'s, in use or old."""
    if kind == "proxies":
        return root / relative_path
    return root.joinpath(*PurePosixPath(relative_path.replace("\\", "/")).parts[1:])


def discard(relative_path: str, settings: Settings | None = None) -> None:
    """Delete a stored file for good: from the folder its kind lives in, and
    from the folder that kind's last move left its old copies in.

    The old copies are kept until the operator removes them, and a later
    move back onto that folder makes them the live files again. A basemap or
    a slideshow deleted meanwhile would come back with them. The caller has
    checked the kind can be written (`unwritable`); the old folder is tried
    and, when it is away, left."""
    settings = settings or get_settings()
    path_for(relative_path, settings).unlink(missing_ok=True)
    kind = kind_of(relative_path)
    old = None if kind is None else _state(settings).previous.get(kind)
    if kind is not None and old is not None:
        with contextlib.suppress(OSError):
            _in_root(old, kind, relative_path).unlink(missing_ok=True)


def put_in_place(finished: Path, destination: Path) -> None:
    """Move a finished file from this machine's scratch into its store.

    On the same disk that is a rename. Onto a share it is a copy under a
    `.partial` name and then a rename, so the real name only ever belongs to
    a whole file. The caller has checked the store is there (`write_problem`):
    creating folders in a store that is away would start filling the empty
    folder it mounts on."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):  # a different disk, which is the usual case for a share
        os.replace(finished, destination)
        return
    partial = destination.with_name(destination.name + PARTIAL_SUFFIX)
    try:
        shutil.copyfile(finished, partial)
        os.replace(partial, destination)
    except BaseException:
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
        raise
    finished.unlink(missing_ok=True)


# --- the proxy store, by its older names -------------------------------------
#
# Proxies were the first kind to have a store of their own, and these are the
# names the rest of the code knows it by.


def separate_proxy_store(settings: Settings | None = None) -> Path | None:
    """The proxy store when it is not simply the data store, else None."""
    settings = settings or get_settings()
    return kind_root("proxies", settings) if is_separate("proxies", settings) else None


def proxy_root(data_dir: Path, settings: Settings | None = None) -> Path:
    """The folder proxies' relative paths hang off."""
    return separate_proxy_store(settings) or data_dir


def derivative_path(data_dir: Path, relative_path: str, settings: Settings | None = None) -> Path:
    """A derived file's absolute path: in its kind's store when that kind has
    one of its own, under `data_dir` for everything else."""
    kind = kind_of(relative_path)
    if kind is None or not is_separate(kind, settings):
        return data_dir / relative_path
    return _in_root(kind_folder(kind, settings), kind, relative_path)


def proxy_store_problem(settings: Settings | None = None) -> str | None:
    """Why proxies cannot be read or written right now, or None."""
    return kind_problem("proxies", settings)


def mark_proxy_store(store: Path) -> None:
    """Place the marker. Called only by the operator's own commands."""
    mark_store(store, PROXY_STORE_MARKER)
