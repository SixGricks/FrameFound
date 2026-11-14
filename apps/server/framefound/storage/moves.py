"""Moving one kind of bulk storage to another folder: copy, check, switch.

    moves.move("proxies", Path("/Volumes/Photos/Framefound/proxies"))

What `framefound proxies move` promised for proxies alone, for every kind in
storage/stores.py, and without a restart:

    1. check the folder (`validate_target`): on a drive that is really
       there, not footage, not somebody else's files, not another kind's,
       and with room for what is coming
    2. copy while everything keeps running. The long part; safe to interrupt,
       and asking again carries on from where it stopped
    3. pause the kind's writers, copy whatever changed meanwhile, and wait
       until nothing is still being written
    4. check that every file arrived, and take back out of the new folder
       the copies of files that were deleted while the move ran
    5. mark the folder as a store and switch to it; every process follows at
       its next lookup

Nothing of the kind's is deleted where it lives. The old copies stay where
they were until `remove_old_copies` is asked, by name, for that one kind.

One move runs at a time, whoever starts it: the turn is a file in the data
folder (`active_move`), taken here rather than by the callers, so the page's
job, the command line and anything else cannot each lift a pause another is
relying on.

Plain Python on purpose — no rsync, no launchctl — so the same function
serves a page and a command line on any machine, and reports its progress
file by file through a callback.
"""

import contextlib
import json
import os
import shutil
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.config import Settings, get_settings
from framefound.db.models import Asset, Library
from framefound.storage import stores
from framefound.storage.worker_roots import DATA_STORE_MARKER, UnmappedRoot, local_root

log = structlog.get_logger()

# Every kind but frame grabs, which stay on this machine. The model library
# moves like the rest (local_models/library.py reads it through the stores):
# a model that has a role runs from its copy on this machine, so the
# library's folder only matters to downloads, copies and removals.
MOVABLE = ("proxies", "renders", "exports", "basemaps", "models", "backups")

# Each kind's finished files, relative to its folder. Whatever else is in
# the folder is not this kind's: never copied, never counted, never removed.
# (A data store's `derivatives/` also holds every thumbnail.)
_PATTERNS = {
    "proxies": "derivatives/*/*/proxy.*",
    "renders": "*.mp4",
    "exports": "listings/*.zip",
    "basemaps": "*.pmtiles",
    "backups": "framefound-*.tar.gz",
    # Only the library: the kind's default folder is `models_dir`, which
    # also holds the ONNX and Whisper caches, and those never move.
    "models": "library/**/*",
}
# Kinds kept a folder per clip, tens of thousands of them: listing one on a
# share takes minutes, which a request cannot wait for.
_DEEP = frozenset({"proxies"})
# What Finder, Explorer and a NAS's indexer leave in any folder they have
# looked at. A folder holding only these is still an empty one.
_LITTER = frozenset({"Thumbs.db", "desktop.ini", "@eaDir"})

# The turn (see the module docstring): `<data_dir>/.framefound-move.lock`,
# renewed while its move runs. One that has not been renewed for this long
# belongs to a move whose process was killed.
MOVE_LOCK = ".framefound-move.lock"
LOCK_BEAT_S = 15.0
LOCK_STALE_S = 120.0
# How often, while a file is being copied, the room left on the disk it is
# going to is looked at again: other things fill disks during a long copy.
SPACE_CHECK_S = 2.0

COPY_CHUNK = 4 * 1024 * 1024
# A filesystem may keep times more coarsely than the one a file came from
# (two seconds, on the FAT family), so a copy can look this much older than
# its original and still be the same file.
MTIME_SLACK_NS = 2_000_000_000
# How long nothing in the old folder may change before the last pass is
# trusted. A transcode writes many times a second; this is for the pause
# before its first write.
SETTLE_S = 10.0
# How long the move waits for a file still being written (a long proxy
# transcode) before giving up with nothing changed.
WRITER_WAIT_S = 20 * 60.0
PAUSE_RENEW_S = 60.0


class MoveError(RuntimeError):
    """Something the operator has to fix; the message says what."""


class FolderChanged(MoveError):
    """The old copies are no longer in the folder the operator was shown.
    Nothing was removed; the page has to be looked at again."""


@dataclass
class Progress:
    """Where a move has got to. The same object is handed to `report` each
    time, updated; copy what you keep."""

    kind: str
    source: str
    target: str
    # checking | copying | finishing | switching | done
    phase: str = "checking"
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0


Report = Callable[[Progress], None]


@dataclass(frozen=True)
class MoveResult:
    kind: str
    source: Path | None
    target: Path
    files: int  # the kind's files now in the target
    bytes: int
    copied: int  # how many of them this run had to copy


@dataclass(frozen=True)
class Removed:
    files: int
    bytes: int
    # Old files with no copy in the folder in use: left alone, and counted.
    kept: int


@dataclass(frozen=True)
class _File:
    relative: str  # posix, from the kind's folder
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class _Listing:
    finished: tuple[_File, ...]
    # Under a `.partial` name: something is, or was, being put in place.
    unfinished: tuple[_File, ...]


def _listing(kind: str, root: Path) -> _Listing:
    pattern = _PATTERNS[kind]
    found: dict[str, _File] = {}
    for path in (*root.glob(pattern), *root.glob(pattern + stores.PARTIAL_SUFFIX)):
        try:
            seen = path.stat()  # once per file: a proxy store has tens of thousands
        except FileNotFoundError:  # deleted between the listing and the look
            continue
        if stat.S_ISREG(seen.st_mode):
            relative = path.relative_to(root).as_posix()
            found[relative] = _File(relative, seen.st_size, seen.st_mtime_ns)
    files = sorted(found.values(), key=lambda entry: entry.relative)
    return _Listing(
        finished=tuple(f for f in files if not f.relative.endswith(stores.PARTIAL_SUFFIX)),
        unfinished=tuple(f for f in files if f.relative.endswith(stores.PARTIAL_SUFFIX)),
    )


def usage(kind: str, root: Path) -> tuple[int, int]:
    """(files, bytes) of `kind` in `root`. A walk of the folder: quick for
    the flat kinds, one directory read per clip for proxies."""
    finished = _listing(kind, root).finished
    return len(finished), sum(entry.size for entry in finished)


def _holds_any(kind: str, root: Path) -> bool:
    # A file, not a folder: the library's pattern also matches the empty
    # folders a removed model leaves, and an empty library holds nothing.
    return any(path.is_file() for path in root.glob(_PATTERNS[kind]))


def _arrived(copy: _File | None, original: _File) -> bool:
    """True when `copy` is `original`, whole and no older."""
    return (
        copy is not None
        and copy.size == original.size
        and copy.mtime_ns + MTIME_SLACK_NS >= original.mtime_ns
    )


def size_text(size: int) -> str:
    return f"{size / 1024**3:.1f} GB" if size >= 1024**3 else f"{size / 1024**2:.0f} MB"


def tidy(folder: Path) -> Path:
    """`folder` with any `..`, doubled or trailing slash worked out.

    Every check here compares a folder with the data folder, the libraries
    and the other stores by its parts, and `/data/../data/renders` must not
    read as somewhere else. (Two spellings that only the filesystem knows
    are one folder — another capitalisation, a symlink — are
    `stores.same_folder`'s to tell.)"""
    return Path(os.path.normpath(folder))


# --- is there room ------------------------------------------------------------
#
# Every other writer stops short of filling a disk (derivatives.ensure_space):
# the data folder shares its disk with the catalogue and the queue, and a
# full one takes both down. A move writes more than all of them together —
# every proxy at once, onto whichever disk it was pointed at — and leaves
# what it copied in place when it stops, so one that ran out of room would
# leave the disk full.


def _free_bytes(folder: Path) -> int | None:
    """Free space on the disk `folder` is, or will be, on. None when that
    cannot be asked; the copy then finds out for itself."""
    try:
        nearest = next(p for p in (folder, *folder.parents) if p.exists())
        return shutil.disk_usage(nearest).free
    except (OSError, StopIteration):
        return None


def _floor_bytes(settings: Settings) -> int:
    return int(settings.min_free_gb * 1024**3)


def _short_of_room(target: Path, needed: int, settings: Settings, things: str) -> str | None:
    """Why `needed` more bytes may not be written to `target`, or None."""
    if needed <= 0:
        return None
    free = _free_bytes(target)
    if free is None or free - needed >= _floor_bytes(settings):
        return None
    return (
        f"There is not enough room in {target}: the {things} still to copy take "
        f"{size_text(needed)} and the disk has {size_text(free)} free. FrameFound leaves "
        f"the last {settings.min_free_gb:g} GB of any disk it writes to (a full disk stops "
        "the catalogue). Free some space, or choose another folder."
    )


def room_problem(
    kind: str, target: Path, settings: Settings | None = None, *, known_bytes: int | None = None
) -> str | None:
    """Why `kind`'s files will not fit in `target`, as far as can be told
    without a long walk; None when they fit or it cannot be told.

    For the answer to come with the click. The move itself measures exactly,
    before each pass (`_Mover.check_room`). Proxies are a folder per clip and
    are not listed here: `known_bytes` is what the catalogue says they take,
    used when the target holds none of them yet."""
    settings = settings or get_settings()
    source = stores.kind_root(kind, settings)
    if source is None:
        return None
    try:
        if kind in _DEEP:
            if known_bytes is None or _holds_any(kind, target):
                return None
            needed = known_bytes
        else:
            there = {entry.relative: entry for entry in _listing(kind, target).finished}
            needed = sum(
                entry.size
                for entry in _listing(kind, source).finished
                if not _arrived(there.get(entry.relative), entry)
            )
    except OSError:
        return None
    return _short_of_room(target, needed, settings, stores.words(kind).things)


# --- one move at a time ---------------------------------------------------------


@dataclass(frozen=True)
class Moving:
    """The move that has the turn."""

    kind: str
    target: str
    since: float  # seconds since the epoch
    token: str


def active_move(settings: Settings | None = None, now: float | None = None) -> Moving | None:
    """The move running now, whoever started it — the Storage page's job or
    a command line — or None. One local file read."""
    settings = settings or get_settings()
    path = settings.data_dir / MOVE_LOCK
    try:
        renewed = path.stat().st_mtime
        if (time.time() if now is None else now) - renewed > LOCK_STALE_S:
            return None  # its process was killed; the next move takes its place
        raw = json.loads(path.read_text(encoding="utf-8"))
        return Moving(
            kind=str(raw["kind"]),
            target=str(raw["target"]),
            since=float(raw["since"]),
            token=str(raw["token"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def busy_sentence(held: Moving | None) -> str:
    """What to tell somebody who asked for a move while `held` is running."""
    if held is None or held.kind not in stores.KINDS:
        return "Another move is running right now. Wait for it to finish; one runs at a time."
    return (
        f"{stores.words(held.kind).things.capitalize()} are being moved to {held.target} "
        "right now. Wait for that to finish; one move runs at a time."
    )


class _Turn:
    """This move's hold on the turn."""

    def __init__(self, path: Path, token: str) -> None:
        self.path = path
        self.token = token
        self.lost = False

    def mine(self) -> bool:
        try:
            return bool(json.loads(self.path.read_text(encoding="utf-8"))["token"] == self.token)
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def renew(self) -> bool:
        if not self.mine():
            self.lost = True
            return False
        with contextlib.suppress(OSError):
            os.utime(self.path)
        return True

    def check(self) -> None:
        """Asked before anything that another move would also be doing: the
        pause and the switch. A move that was not heard from for
        LOCK_STALE_S (its machine slept, a share hung) may wake to find
        another in its place, and must stop rather than switch too."""
        if self.lost or not self.mine():
            self.lost = True
            raise MoveError(
                "This move was not heard from for so long that another was allowed to "
                "start in its place. It stopped; nothing was changed by it."
            )

    def release(self) -> None:
        if self.mine():
            with contextlib.suppress(OSError):
                self.path.unlink(missing_ok=True)


def _take_turn(kind: str, target: Path, settings: Settings) -> _Turn:
    path = settings.data_dir / MOVE_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    body = json.dumps(
        {
            "kind": kind,
            "target": str(target),
            "since": time.time(),
            "token": token,
            "pid": os.getpid(),
        }
    )
    for _attempt in range(3):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                stale = time.time() - path.stat().st_mtime > LOCK_STALE_S
            except FileNotFoundError:
                continue  # given up between the two looks
            if not stale:
                raise MoveError(busy_sentence(active_move(settings))) from None
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            continue
        with os.fdopen(handle, "w", encoding="utf-8") as writer:
            writer.write(body)
        return _Turn(path, token)
    raise MoveError(busy_sentence(None))


@contextlib.contextmanager
def _turn(kind: str, target: Path, settings: Settings) -> Iterator[_Turn]:
    """Hold the turn for as long as the block runs, or raise MoveError when
    another move has it.

    Renewed from a thread of its own, not from the copy: listing a proxy
    store on a share can take minutes without copying a byte, and a move
    that looked dead then would be joined by a second one."""
    turn = _take_turn(kind, target, settings)
    over = threading.Event()

    def keep() -> None:
        while not over.wait(LOCK_BEAT_S):
            if not turn.renew():
                return

    renewing = threading.Thread(target=keep, name="framefound-move-turn", daemon=True)
    renewing.start()
    try:
        yield turn
    finally:
        over.set()
        renewing.join(timeout=LOCK_BEAT_S)
        turn.release()


# --- which folders will do ----------------------------------------------------


def _visible(folder: Path) -> list[str]:
    return sorted(
        entry.name
        for entry in os.scandir(folder)
        if not entry.name.startswith(".") and entry.name not in _LITTER
    )


def _check_place(folder: Path, settings: Settings, catalogued: int, what: str) -> None:
    """The rules every store folder shares, whatever is going into it."""
    data_dir = settings.data_dir
    if not folder.is_absolute():
        raise MoveError("Give the full path of the folder, starting with /")
    if folder.parent == folder:
        raise MoveError(f"{folder} is a whole disk, not a folder. Name a folder on it.")
    if folder == data_dir or folder in data_dir.parents:
        raise MoveError(
            f"{folder} holds FrameFound's own data folder ({data_dir}). Choose a folder "
            "on the drive or share the files should move to."
        )
    if data_dir in folder.parents:
        raise MoveError(
            f"{folder} is inside FrameFound's own data folder. Choose a folder on "
            f"another drive or share for {what}."
        )
    if catalogued:
        # A store's marker keeps the scanner out of the folder and everything
        # below it, so the next scan would flag all of this missing.
        raise MoveError(
            f"{folder} holds {catalogued:,} files the catalogue lists as footage. "
            "FrameFound never scans inside a store, so they would all be reported "
            "missing. Choose an empty folder."
        )
    # Never create the drive's or share's own folder: if `/Volumes/Photos` is
    # not mounted, making it would put the "store" on this machine's disk.
    if not folder.parent.is_dir():
        raise MoveError(
            f"{folder.parent} does not exist. Connect the drive or share first; "
            "FrameFound only creates the last folder of the path."
        )
    if folder.exists() and not folder.is_dir():
        raise MoveError(f"{folder} is a file, not a folder.")


def _check_writable(folder: Path) -> None:
    try:
        folder.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".framefound-write-test-"):
            pass
    except OSError as err:
        raise MoveError(f"{folder} is not writable from this machine ({err})") from err


def on_data_disk(folder: Path, settings: Settings | None = None) -> bool:
    """True when `folder` is on the same disk as FrameFound's own data.

    Nothing refuses that — a second folder on one disk is allowed, if
    pointless — but for a folder meant to be on a share it means the share
    is not mounted and the folder is the empty one it mounts on. Where a
    mountpoint outlives its mount (`/mnt/nas` from fstab, unlike
    `/Volumes/<share>` on a Mac) no other check can tell; this is for a
    page or a command to ask "are you sure?" with."""
    settings = settings or get_settings()
    try:
        nearest = next(p for p in (folder, *folder.parents) if p.exists())
        return nearest.stat().st_dev == settings.data_dir.stat().st_dev
    except (OSError, StopIteration):
        return False


def one_folder(one: Path, other: Path) -> bool:
    """`stores.same_folder` without its last resort of writing a file: for a
    look at folders that are not this move's to write in."""
    if one == other:
        return True
    try:
        return one.is_dir() and other.is_dir() and os.path.samefile(one, other)
    except OSError:
        return False


def _other_kinds_folder(kind: str, target: Path, settings: Settings) -> str | None:
    """Whose folder `target` is, when it is another kind's.

    Five of the six kinds share one marker name, so the marker alone cannot
    say "this is the export store" to a move of slideshow videos. Where each
    kind lives, and would live in the bulk folder, can."""
    bulk = stores.bulk_root(settings)
    for other in stores.KINDS:
        if other == kind:
            continue
        theirs = [stores.kind_root(other, settings)]
        if bulk is not None:
            theirs.append(bulk / other)
        if any(folder is not None and one_folder(folder, target) for folder in theirs):
            return other
    return None


def validate_target(
    kind: str,
    target: Path,
    settings: Settings | None = None,
    *,
    catalogued: int = 0,
    create: bool = True,
) -> None:
    """Refuse a folder `kind` must not be moved into; the message says why.

    `catalogued` is how many files the catalogue lists inside the folder
    (`catalogued_under`): the one check that needs the database. Creates the
    last folder of the path, and only that, to prove it can be written —
    unless `create` is False, for a look before the operator has been asked
    whether a folder on this machine's own disk is really meant
    (`preflight`): declining then leaves nothing behind."""
    settings = settings or get_settings()
    words = stores.words(kind)
    if kind not in MOVABLE:
        raise MoveError(f"{words.things.capitalize()} cannot be moved yet.")
    if not target.is_absolute():
        raise MoveError("Give the full path of the folder, starting with /")
    target = tidy(target)
    if target == stores.default_root(kind, settings):
        return  # back onto this machine, where it always lived: always there
    _check_place(target, settings, catalogued, words.things)
    bulk = stores.bulk_root(settings)
    if bulk is not None and one_folder(target, bulk):
        raise MoveError(
            f"{target} is the folder for all bulk storage; {words.things} go in "
            f"their own folder inside it: {target / kind}"
        )
    try:
        other = _other_kinds_folder(kind, target, settings)
        if other is not None:
            raise MoveError(
                f"{target} is the folder for {stores.words(other).things}. Each kind has "
                f"a folder of its own; choose another for {words.things}."
            )
        present = set(os.listdir(target)) if target.is_dir() else set()
        if DATA_STORE_MARKER in present:
            raise MoveError(f"{target} is a FrameFound data store. Choose another folder.")
        # Its own marker: a store this kind has used before. The incoming
        # one: an earlier attempt at this move, to be carried on. Either may
        # name the kind it was placed for.
        for marker in (stores.marker_for(kind), stores.INCOMING_MARKER):
            whose = stores.marker_facts(target, marker).get("kind") if marker in present else None
            if whose is not None and whose != kind and whose in stores.KINDS:
                unfinished = marker == stores.INCOMING_MARKER
                raise MoveError(
                    f"{target} is where "
                    + (
                        f"an unfinished move of {stores.words(whose).things} was copying to"
                        if unfinished
                        else f"{stores.words(whose).things} are kept"
                    )
                    + f". Choose another folder for {words.things}."
                )
        expected = bool({stores.marker_for(kind), stores.INCOMING_MARKER} & present)
        if stores.STORE_MARKERS & present and not expected:
            raise MoveError(
                f"{target} is already a FrameFound store for something else. Choose another folder."
            )
        if not expected and target.is_dir() and _visible(target):
            # A folder of somebody's files marked as a store would be skipped
            # by every scan from then on.
            raise MoveError(
                f"{target} already has files in it that FrameFound did not put "
                "there. Choose an empty folder, or name a new one."
            )
    except OSError as err:
        raise MoveError(f"{target} cannot be read ({err.strerror or err})") from err
    if create:
        _check_writable(target)


def validate_bulk_root(
    root: Path,
    settings: Settings | None = None,
    *,
    catalogued: int = 0,
    create: bool = True,
) -> None:
    """Refuse a folder that must not become the one folder for bulk storage.

    One already in use is accepted as it is. So is a folder holding nothing
    but kinds' own folders: an install made with
    `--proxies <folder>/proxies` before there was a bulk folder becomes one
    without anything moving. `create` as for `validate_target`."""
    settings = settings or get_settings()
    root = tidy(root)
    _check_place(root, settings, catalogued, "bulk storage")
    try:
        if root.is_dir() and not (root / stores.BULK_STORE_MARKER).is_file():
            for name in _visible(root):
                inside = root / name
                known = name in stores.KINDS and inside.is_dir()
                marked = known and (inside / stores.marker_for(name)).is_file()
                if not known or (_visible(inside) and not marked):
                    raise MoveError(
                        f"{root} already has files in it that FrameFound did not put "
                        f"there ({name}). Choose an empty folder, or name a new one."
                    )
    except OSError as err:
        raise MoveError(f"{root} cannot be read ({err.strerror or err})") from err
    if create:
        _check_writable(root)


async def catalogued_under(db: AsyncSession, folder: Path, settings: Settings | None = None) -> int:
    """How many files the catalogue lists in `folder` or beneath it.

    For `validate_target`: a store may sit beside footage on the same share
    (the bulk folder on the Photos drive is exactly that), but never on top
    of it. Files already flagged missing do not count — a folder emptied of
    old footage is free to be reused."""
    total = 0
    for library in (await db.execute(select(Library))).scalars().all():
        try:
            root = local_root(library.root_path, settings)
        except UnmappedRoot:
            continue
        query = (
            select(func.count())
            .select_from(Asset)
            .where(Asset.library_id == library.id, Asset.availability != "missing")
        )
        if folder == root or folder in root.parents:
            pass  # the whole library is inside the folder
        elif root in folder.parents:
            inside = folder.relative_to(root).as_posix()
            for special in ("\\", "%", "_"):
                inside = inside.replace(special, "\\" + special)
            query = query.where(Asset.relative_path.like(f"{inside}/%", escape="\\"))
        else:
            continue
        total += int((await db.execute(query)).scalar_one())
    return total


# --- one folder for everything bulky -----------------------------------------


@dataclass(frozen=True)
class Question:
    """Something to put to the operator before a folder is used. Asked, not
    refused: only they know whether the folder is what it is meant to be."""

    why: str  # what was found, in full sentences
    ask: str  # the question itself

    def __str__(self) -> str:
        return f"{self.why} {self.ask}"


def same_disk_question(folder: Path) -> Question:
    return Question(
        f"{folder} is on the same disk as FrameFound's own data. If it is meant to be "
        "on another drive or a network share, that drive is not connected right now "
        "and the files would go onto this disk instead.",
        "Use this folder anyway?",
    )


def _lost_its_marker(root: Path, settings: Settings) -> bool:
    """True when `root` is the bulk folder already and does not look like it."""
    return root == stores.bulk_root(settings) and not (root / stores.BULK_STORE_MARKER).is_file()


def bulk_question(root: Path, settings: Settings | None = None) -> Question | None:
    """What to ask the operator before `root` is made the bulk folder, or
    None when there is nothing to ask. A yes is `adopt_bulk_root`'s `sure`.

    Both questions are the same doubt from two sides: is this folder the
    drive or share it is meant to be, or the empty folder left where one
    mounts? Marked on that say-so, every kind in it would read as connected,
    everything already on the real share as missing, and new files would
    fill this machine's own disk."""
    settings = settings or get_settings()
    root = tidy(root)
    if _lost_its_marker(root, settings):
        return Question(
            f"{root} is already the folder for bulk storage, but its marker file is "
            "missing, which is what a drive or share that is not connected looks like. "
            "If it is only disconnected, connect it and nothing else needs doing. If "
            "the drive was replaced and this is a new, empty folder, FrameFound can "
            "start it empty and make the files again.",
            "Start it empty?",
        )
    if on_data_disk(root, settings):
        return same_disk_question(root)
    return None


def adopt_bulk_root(
    root: Path,
    *,
    settings: Settings | None = None,
    catalogued: int = 0,
    sure: bool = False,
) -> list[str]:
    """Make `root` the one folder for bulk storage, moving nothing.

    Returns the kinds that stay where they are. A kind that already has
    files is given a record of its present folder first, so naming the bulk
    folder never makes a proxy or a render unfindable; each is brought over
    later with `move`. A kind with nothing yet simply starts in the new
    folder.

    A folder that is the bulk folder already but has lost its marker is
    only marked again when the operator has said so (`sure`, the answer to
    `bulk_question`): that is what a share that is not mounted looks like,
    and marking the folder it mounts on is the mistake the markers exist to
    prevent."""
    settings = settings or get_settings()
    root = tidy(root)
    held = active_move(settings)
    if held is not None:
        # Which folder a kind with no files yet lives in changes here, under
        # a move that has already decided where it is copying from.
        raise MoveError(busy_sentence(held))
    validate_bulk_root(root, settings, catalogued=catalogued, create=False)
    if _lost_its_marker(root, settings) and not sure:
        raise MoveError(
            f"{root} is already the folder for bulk storage and is not connected (its "
            "marker file is missing). Connect the drive or share. Nothing was changed."
        )
    _check_writable(root)
    staying: dict[str, Path] = {}
    # Backups are not here: with no folder of their own they have no copy
    # to lose. The model library is: naming a bulk folder after a model was
    # downloaded must not point the library at an empty folder.
    for kind in ("proxies", "renders", "exports", "basemaps", "models"):
        current = stores.kind_folder(kind, settings)
        if stores.placed_by(kind, settings) in ("own", "setting") or current == root / kind:
            continue  # the bulk folder does not decide where this kind lives
        try:
            # Away counts as holding files: nobody can look, and guessing
            # "empty" is the guess that loses them.
            holds = stores.kind_problem(kind, settings) is not None or _holds_any(kind, current)
        except OSError:
            holds = True
        if holds:
            staying[kind] = current
    try:
        stores.mark_bulk_root(root)
        stores.set_bulk_root(settings.data_dir, root, keep=staying)
    except OSError as err:
        raise MoveError(f"{root} could not be set up ({err.strerror or err})") from err
    except stores.StoresFileError as err:
        raise MoveError(str(err)) from err
    log.info("storage.bulk_root_adopted", root=str(root), staying=sorted(staying))
    return sorted(staying)


# --- the move -----------------------------------------------------------------


def _copy_file(
    source: Path, destination: Path, mtime_ns: int, on_bytes: Callable[[int], None]
) -> None:
    """Copy one file so that its real name only ever belongs to a whole one.

    The copy is given the time its original had when it was listed, not the
    time it has now: an original that changes while it is being read then
    looks newer than its copy, and the next pass takes it again."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + stores.PARTIAL_SUFFIX)
    try:
        with source.open("rb") as reader, partial.open("wb") as writer:
            while chunk := reader.read(COPY_CHUNK):
                writer.write(chunk)
                on_bytes(len(chunk))
        with contextlib.suppress(OSError):  # some shares refuse to have times set
            os.utime(partial, ns=(mtime_ns, mtime_ns))
        os.replace(partial, destination)
    except BaseException:
        # Half a file is no use to the next attempt, which starts each file
        # again, and on a disk that has just run out of room it is the
        # largest thing that can be given back.
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
        raise


class _Mover:
    """One move's state: what the passes share."""

    def __init__(
        self,
        kind: str,
        source: Path,
        target: Path,
        settings: Settings,
        report: Report | None,
        turn: _Turn,
    ) -> None:
        self.kind = kind
        self.source = source
        self.target = target
        self.settings = settings
        self.report = report
        self.turn = turn
        self.progress = Progress(kind=kind, source=str(source), target=str(target))
        self.source_separate = source != stores.default_root(kind, settings)
        self.target_separate = target != stores.default_root(kind, settings)
        self.copied = 0
        # The files this run copied, by relative path: the ones it may take
        # back out of the target if they are deleted while it runs.
        self.brought: set[str] = set()
        self._room_seen = float("-inf")
        # What the target holds, listed once and then kept by hand: walking a
        # proxy store on a share again for every pass would stretch the pause.
        self.there: dict[str, _File] | None = None
        self.paused_at: float | None = None
        self._told = 0.0

    def tell(self, phase: str | None = None) -> None:
        if phase is not None:
            self.progress.phase = phase
        self._told = time.monotonic()
        if self.report is not None:
            self.report(self.progress)

    def beat(self) -> None:
        """Called between chunks: keep the page moving, keep the pause alive."""
        now = time.monotonic()
        if now - self._told >= 1.0:
            self.tell()
        if self.paused_at is not None and now - self.paused_at >= PAUSE_RENEW_S:
            self.pause()

    def pause(self) -> None:
        self.turn.check()
        stores.pause(self.settings.data_dir, self.kind)
        self.paused_at = time.monotonic()

    def check_room(self, needed: int) -> None:
        """Before a pass copies anything: will what it has to bring fit?"""
        short = _short_of_room(self.target, needed, self.settings, stores.words(self.kind).things)
        if short is not None:
            raise MoveError(f"{short} Nothing was changed.")

    def watch_room(self) -> None:
        """Between chunks: the pass fitted when it began, and other things
        write to the same disk while it runs."""
        now = time.monotonic()
        if now - self._room_seen < SPACE_CHECK_S:
            return
        self._room_seen = now
        free = _free_bytes(self.target)
        if free is not None and free < _floor_bytes(self.settings):
            raise MoveError(
                f"The move stopped: {self.target} is down to {size_text(free)} free, and "
                f"FrameFound leaves the last {self.settings.min_free_gb:g} GB of any disk "
                "it writes to. Nothing was changed; what was copied is kept. Free some "
                "space, then start the move again to carry on."
            )

    def check_target(self) -> None:
        # Asked before every file, because `mkdir` on the empty folder a
        # dropped share leaves behind would carry the copy on to this
        # machine's own disk until it was full.
        if self.target_separate and not (self.target / stores.INCOMING_MARKER).is_file():
            raise MoveError(
                f"{self.target} went away during the copy. Nothing was changed; "
                "reconnect it and start the move again to carry on."
            )

    def check_source(self) -> None:
        # An unmounted share lists as an empty folder, which would otherwise
        # read as "nothing left to copy".
        if self.source_separate and stores.kind_problem(self.kind, self.settings) is not None:
            raise MoveError(
                f"{self.source} went away during the copy. Nothing was changed; "
                "reconnect it and start the move again to carry on."
            )

    def listing(self) -> _Listing:
        found = _listing(self.kind, self.source)
        self.check_source()
        return found

    def sync(self, wanted: tuple[_File, ...]) -> None:
        """Copy every file in `wanted` that is not already in the target,
        whole and no older than its original."""
        if self.there is None:
            self.there = {f.relative: f for f in _listing(self.kind, self.target).finished}
        there = self.there
        progress = self.progress
        progress.files_total, progress.bytes_total = len(wanted), sum(f.size for f in wanted)
        progress.files_done = progress.bytes_done = 0
        self.tell()
        self.check_room(sum(f.size for f in wanted if not _arrived(there.get(f.relative), f)))

        def count(length: int) -> None:
            progress.bytes_done += length
            self.beat()
            self.watch_room()

        for entry in wanted:
            if _arrived(there.get(entry.relative), entry):
                progress.bytes_done += entry.size
            else:
                self.check_target()
                before = progress.bytes_done
                try:
                    _copy_file(
                        self.source / entry.relative,
                        self.target / entry.relative,
                        entry.mtime_ns,
                        count,
                    )
                except FileNotFoundError:
                    # Deleted while the move ran (or its share dropped, which
                    # the next listing says): nothing to bring.
                    self.check_source()
                    progress.bytes_done = before
                else:
                    self.copied += 1
                    self.brought.add(entry.relative)
                    # A file that grew while it was read is counted, and
                    # remembered, at the size it was listed with: the next
                    # look finds it changed and takes it again.
                    there[entry.relative] = entry
                    progress.bytes_done = before + entry.size
            progress.files_done += 1
            self.beat()
        self.tell()


def move(
    kind: str,
    target: Path,
    *,
    settings: Settings | None = None,
    mode: str = "copy",
    catalogued: int = 0,
    report: Report | None = None,
    wait_s: float = WRITER_WAIT_S,
    settle_s: float = SETTLE_S,
    sleep: Callable[[float], None] = time.sleep,
    before_switch: Callable[[], None] | None = None,
) -> MoveResult:
    """Move `kind` to `target`: copy, check, switch. Blocking, and long.

    `mode="empty"` switches without copying, for a store that is gone for
    good; its files are made again. That includes a replacement at the very
    path of the one that died (a new drive under the old name). Raises
    MoveError with nothing changed: the kind still lives where it did, its
    writers are no longer paused, and whatever was copied is kept for the
    next attempt. One move runs at a time; a second is refused while the
    first holds the turn (`active_move`).

    Whether something is still being written is read off the folder itself
    (two looks, `settle_s` apart, that agree), never off a job's status in
    the database: a writer that reaches its last step during the pause waits
    for the move (derivatives.store_ready), so a move that waited for that
    job to finish would be waiting for itself.

    `before_switch` runs once everything is copied and checked, with the
    kind's writers still paused, just before the switch; a MoveError from it
    stops the move with nothing changed. (The model library uses it: a model
    with a role must have its copy on this machine before the library
    leaves — `local_models.library.keep_on_this_machine`.)"""
    settings = settings or get_settings()
    if mode not in ("copy", "empty"):
        raise ValueError(f"mode is 'copy' or 'empty', not {mode!r}")
    target = tidy(target)
    try:
        with _turn(kind, target, settings) as turn:
            return _move(
                kind,
                target,
                settings,
                mode,
                catalogued,
                report,
                wait_s,
                settle_s,
                sleep,
                turn,
                before_switch,
            )
    except stores.StoresFileError as err:
        raise MoveError(str(err)) from err
    except OSError as err:
        raise MoveError(
            f"The move stopped: {err.strerror or err}"
            f"{f' ({err.filename})' if err.filename else ''}. Nothing was changed; "
            "start it again to carry on."
        ) from err


def _move(
    kind: str,
    target: Path,
    settings: Settings,
    mode: str,
    catalogued: int,
    report: Report | None,
    wait_s: float,
    settle_s: float,
    sleep: Callable[[float], None],
    turn: _Turn,
    before_switch: Callable[[], None] | None = None,
) -> MoveResult:
    validate_target(kind, target, settings, catalogued=catalogued)
    data_dir = settings.data_dir
    words = stores.words(kind)
    source = stores.kind_root(kind, settings)
    separate = target != stores.default_root(kind, settings)

    def done_without_copying() -> MoveResult:
        files, size = usage(kind, target)
        if report is not None:
            report(Progress(kind=kind, source=str(source or ""), target=str(target), phase="done"))
        return MoveResult(kind, source, target, files, size, copied=0)

    if _standing(kind, source, target, mode, settings) == "there":
        # Asked for twice, or interrupted just after its switch: see that
        # nothing was left paused.
        stores.resume(data_dir, kind)
        return done_without_copying()

    alone = False
    if separate:
        # Not a store until the copy has been checked, but FrameFound's own
        # from the first byte: the scanner must not catalogue a half-made
        # copy that sits on the same share as the footage.
        alone = _mark_incoming(kind, target)

    if mode == "empty" or source is None:
        if before_switch is not None:
            before_switch()
        turn.check()
        _switch(kind, target, settings, previous=None)
        return done_without_copying()

    # Asked now: once the switch is made the kind lives somewhere else, and
    # this is about the folder it is leaving.
    source_marked = stores.needs_marker(kind, settings)
    mover = _Mover(kind, source, target, settings, report, turn)
    mover.tell("copying")
    mover.sync(mover.listing().finished)

    # The copy above may have taken hours, and files were made meanwhile.
    # From here nothing new is started, so the folder can be caught up with.
    mover.pause()
    try:
        mover.tell("finishing")
        deadline: float | None = None
        before: _Listing | None = None
        while True:
            found = mover.listing()
            # Unchanged since the last look, a settle interval ago, and
            # everything in that look has been copied since: nothing is
            # being written, and the target has all of it.
            if found == before:
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise MoveError(
                    f"Some {words.things} were still being written after "
                    f"{wait_s / 60:.0f} minutes. Nothing was changed; start the move "
                    "again when they have finished, and it carries on from here."
                )
            mover.sync(found.finished)
            if deadline is None:
                # Counted from the end of the first catch-up, which may have
                # had hours of new files to bring.
                deadline = time.monotonic() + wait_s
            before = found
            mover.pause()  # renewed: a long wait must not let the pause lapse
            sleep(settle_s)

        mover.check_target()
        arrived = {entry.relative: entry.size for entry in _listing(kind, target).finished}
        whole = [entry for entry in found.finished if arrived.get(entry.relative) == entry.size]
        if len(whole) < len(found.finished):
            raise MoveError(
                f"Only {len(whole):,} of {len(found.finished):,} {words.things} are in "
                f"{target}. Nothing was changed; start the move again to carry on."
            )
        mover.tell("switching")
        if before_switch is not None:
            before_switch()
            mover.pause()  # renewed: what it did may have taken a while
        turn.check()
        _take_back_deleted(
            target,
            gone=set(arrived) - {entry.relative for entry in found.finished},
            brought=None if alone else mover.brought,
        )
        _switch(kind, target, settings, previous=source, previous_marked=source_marked)
    finally:
        # Lifted whether or not the move worked. (The switch lifts it in the
        # same write as the change of folder; this is for every other way out.)
        # Except when another move of this kind has taken this one's place:
        # the pause is then that move's, and it is relying on it.
        theirs = active_move(settings) if turn.lost else None
        if theirs is None or theirs.kind != kind:
            with contextlib.suppress(stores.StoresFileError, OSError):
                stores.resume(data_dir, kind)
    mover.tell("done")
    log.info(
        "storage.moved",
        kind=kind,
        source=str(source),
        target=str(target),
        files=len(whole),
        copied=mover.copied,
    )
    return MoveResult(
        kind, source, target, len(whole), sum(entry.size for entry in whole), mover.copied
    )


def _standing(kind: str, source: Path | None, target: Path, mode: str, settings: Settings) -> str:
    """How `target` stands to where `kind` lives now: "there" (it lives
    there already and is connected, so there is nothing to do), "replace"
    (start again, empty, at the very path of a store that is gone) or "move".
    Raises MoveError for a move that must not be made.

    One rule for the page's check at the click and for the move itself."""
    words = stores.words(kind)
    if source is None:
        return "move"
    away = stores.kind_problem(kind, settings)
    if source == target:
        if away is None:
            return "there"
        if mode == "empty":
            # "The old store is gone; start empty" in the operator's own
            # words. A replacement drive given the dead one's name mounts at
            # the same path, and a rebuilt share keeps its own.
            return "replace"
        # Never marked again on a plain "move it to where it is": a store
        # that has lost its marker is a share that is not mounted, and that
        # would bless the empty folder it mounts on.
        raise MoveError(
            f"{words.things.capitalize()} already live in {target}. {away} If the drive "
            "or share was replaced by a new one, start the folder empty instead and "
            f"have the {words.things} made again."
        )
    if away is None and stores.same_folder(source, target):
        # Another capitalisation on a volume that ignores it, a symlink, the
        # same share mounted a second time. "Copied" onto itself, every file
        # would be found already there, and the old spelling would then be
        # offered for removal as the old copies: the only copies.
        raise MoveError(
            f"{target} is the folder {words.things} already live in ({source}), under "
            "another name. Nothing was changed."
        )
    if _inside(target, source):
        # The kind's files are listed from `source` recursively (the model
        # library's `library/**/*`, a proxy's whole path), so every catch-up
        # pass would find the copies it had just written and copy them again,
        # one folder deeper each time, until the disk or the clock stopped it.
        raise MoveError(
            f"{target} is inside the folder {words.things} live in now ({source}). "
            "Choose a folder outside it. Nothing was changed."
        )
    if mode == "copy" and away is not None:
        raise MoveError(
            f"The current {words.store} cannot be copied from: {away} Connect it, "
            f"or start the new folder empty and have the {words.things} made again."
        )
    return "move"


def _inside(target: Path, source: Path) -> bool:
    """True when `target` is a folder somewhere below `source`, as written
    or once links and `..` are followed (the folder need not exist yet: its
    nearest existing parent is what is resolved)."""
    if source in target.parents:
        return True
    try:
        real_source = source.resolve()
        real_target = target.resolve()
    except OSError:
        return False
    return real_target != real_source and real_source in real_target.parents


def _mark_incoming(kind: str, target: Path) -> bool:
    """Mark `target` as the folder this move is copying into. True when
    every one of the kind's files in it is this move's own doing.

    Written once, by the first attempt, with whether the folder then held
    any of the kind's files; an attempt that carries on reads that back. It
    decides what may be taken out again before the switch
    (`_take_back_deleted`)."""
    facts = stores.marker_facts(target, stores.INCOMING_MARKER)
    if facts.get("kind") == kind:
        return facts.get("began-empty") == "yes"
    # A marker with no kind on it was placed before kinds were written
    # there: an attempt is being carried on, and what the folder held when
    # it began is not known.
    carried_on = (target / stores.INCOMING_MARKER).is_file()
    began_empty = not carried_on and not _holds_any(kind, target)
    stores.mark_store(
        target, stores.INCOMING_MARKER, kind=kind, began_empty="yes" if began_empty else "no"
    )
    return began_empty


def _take_back_deleted(target: Path, *, gone: set[str], brought: set[str] | None) -> None:
    """Remove from `target` the copies of files that are no longer in the
    folder they were copied from.

    A basemap or a slideshow deleted during the hours of the copy is deleted
    where the kind still lives; the copy already made would otherwise bring
    it back at the switch. `gone` is what the target holds of the kind that
    the last look at the source did not. `brought` is what this run copied,
    or None when everything of the kind in the target is this move's
    (`_mark_incoming`): then an earlier, interrupted attempt's copies are
    covered too. Files that were in a folder the kind had used before are
    never touched — they may be the only copies of something."""
    for relative in sorted(gone if brought is None else gone & brought):
        path = target / relative
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
            _drop_empty_parents(path, target)
        log.info("storage.move_copy_taken_back", folder=str(target), file=relative)


def _drop_empty_parents(path: Path, root: Path) -> None:
    """The folders `path` sat in, when it was the last thing in them.
    `rmdir` refuses a folder with anything left in it, so a thumbnail beside
    a proxy keeps its folder."""
    for parent in path.parents:
        if parent == root:
            break
        try:
            parent.rmdir()
        except OSError:
            break


def _switch(
    kind: str,
    target: Path,
    settings: Settings,
    *,
    previous: Path | None,
    previous_marked: bool = True,
) -> None:
    """Mark the folder as a store, then record it: the one step that
    changes where the kind lives."""
    if target != stores.default_root(kind, settings):
        stores.mark_store(target, stores.marker_for(kind), kind=kind)
        with contextlib.suppress(OSError):
            (target / stores.INCOMING_MARKER).unlink(missing_ok=True)
    stores.switch(
        settings.data_dir, kind, target, previous=previous, previous_marked=previous_marked
    )


# --- before anything is asked for ---------------------------------------------


@dataclass(frozen=True)
class Preflight:
    """What a look at a move found, before it is started."""

    target: Path  # as it will be recorded
    source: Path | None
    # The kind lives there already: nothing to move.
    already: bool
    # To put to the operator before going on, or None (see `on_data_disk`).
    question: Question | None


def preflight(
    kind: str,
    target: Path,
    *,
    settings: Settings | None = None,
    mode: str = "copy",
    catalogued: int = 0,
    known_bytes: int | None = None,
    confirmed: bool = False,
) -> Preflight:
    """Everything that would refuse this move, found now — for a page to
    answer the click with, and a command to stop on before it starts.

    Raises MoveError exactly where `move` would, and for files that will not
    fit as far as that can be told quickly (`room_problem`). Creates nothing
    while a `question` is unanswered (`confirmed` is the operator's yes), so
    a no leaves no folder behind on a mountpoint."""
    settings = settings or get_settings()
    target = tidy(target)
    validate_target(kind, target, settings, catalogued=catalogued, create=False)
    source = stores.kind_root(kind, settings)
    standing = _standing(kind, source, target, mode, settings)
    if standing == "there":
        return Preflight(target, source, already=True, question=None)
    if standing == "move" and mode == "copy" and source is not None:
        short = room_problem(kind, target, settings, known_bytes=known_bytes)
        if short is not None:
            raise MoveError(f"{short} Nothing was changed.")
    question = None
    if target != stores.default_root(kind, settings):
        if on_data_disk(target, settings):
            question = same_disk_question(target)
        if question is None or confirmed:
            _check_writable(target)
    return Preflight(target, source, already=False, question=question)


# --- afterwards ---------------------------------------------------------------


def remove_old_copies(
    kind: str,
    *,
    settings: Settings | None = None,
    old_root: Path | None = None,
    everything: bool = False,
    expected: Path | None = None,
) -> Removed:
    """Free the space `kind`'s files still take where they used to live.

    Only this kind's files, only in the folder its last completed move left
    (or `old_root`), and only those that have a copy in the folder in use
    now — the rest are left and counted in `kept`, unless `everything` says
    the operator has looked and wants them gone too. Never asked for
    automatically, and never while that kind is being moved.

    `expected` is the folder the operator was shown. When the record names
    another one now (a second move finished while the page was open), or
    none, `FolderChanged` is raised and nothing is removed: a button that
    said one folder must never empty another.

    The old folder and the one in use have to be two folders on the disk,
    not only two names (`stores.same_folder`): every file of a folder "has a
    copy" in itself."""
    settings = settings or get_settings()
    words = stores.words(kind)
    if kind not in MOVABLE:
        raise MoveError(f"{words.things.capitalize()} cannot be moved yet.")
    current = stores.kind_root(kind, settings)
    state = stores.read_state(settings.data_dir)
    recorded = state.previous.get(kind)
    old = old_root or recorded
    if expected is not None and (old is None or tidy(expected) != tidy(old)):
        now = (
            f"the old copies on record are in {old} now"
            if old is not None
            else "there are no old copies on record now"
        )
        raise FolderChanged(
            f"The page showed old copies of {words.things} in {tidy(expected)}, but {now}. "
            "Nothing was removed. Look at the Storage page again before removing anything."
        )
    if old is None:
        raise MoveError(f"There are no old copies of {words.things} on record.")
    if current is None or old == current:
        raise MoveError(f"{old} is where {words.things} live now. Nothing was removed.")
    held = active_move(settings)
    if held is not None and held.kind == kind:
        # Its target may be this very folder, half filled.
        raise MoveError(
            f"{words.things.capitalize()} are being moved right now. Nothing was removed."
        )
    busy = stores.write_problem(kind, settings)
    if busy is not None:
        # With the folder in use out of reach, the old copies may be the
        # only ones anybody can get at.
        raise MoveError(f"{busy} Nothing was removed.")
    try:
        if old == stores.default_root(kind, settings):
            connected = True  # this machine's own disk
        elif old == recorded and kind in state.unmarked:
            # A folder that never had to carry a marker (backup copies in
            # the folder their setting named): there is all it can be.
            connected = old.is_dir()
        else:
            connected = any((old / marker).is_file() for marker in stores.STORE_MARKERS)
        if not connected:
            raise MoveError(f"{old} is not connected. Nothing was removed.")
        if stores.same_folder(old, current):
            if old_root is None:
                # Nothing to offer for removal, now or later.
                with contextlib.suppress(stores.StoresFileError):
                    stores.set_previous(settings.data_dir, kind, None)
            raise MoveError(
                f"{old} and {current} are the same folder under two names, so these are "
                f"not old copies: they are the {words.things} in use. Nothing was removed."
            )
        there = {entry.relative for entry in _listing(kind, current).finished}
        files = size = kept = 0
        for entry in _listing(kind, old).finished:
            if not everything and entry.relative not in there:
                kept += 1
                continue
            path = old / entry.relative
            with contextlib.suppress(OSError):
                if os.path.samefile(path, current / entry.relative):
                    # The folders were told apart, and this one file still
                    # is its own "copy" (a link inside one of them). Left.
                    kept += 1
                    continue
            path.unlink(missing_ok=True)
            files, size = files + 1, size + entry.size
            _drop_empty_parents(path, old)
    except OSError as err:
        raise MoveError(f"The old copies could not be removed ({err.strerror or err})") from err
    if not kept:
        with contextlib.suppress(stores.StoresFileError):
            stores.set_previous(settings.data_dir, kind, None)
    log.info("storage.old_copies_removed", kind=kind, folder=str(old), files=files, kept=kept)
    return Removed(files=files, bytes=size, kept=kept)
