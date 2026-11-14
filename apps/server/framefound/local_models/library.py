"""Where models live: the library, this machine's copies, and the downloads.

    <models kind folder>/library/<publisher>/<name>/<revision>[/<variant>]/   the library
    <models_dir>/local-ai/<publisher>/<name>/<revision>[/<variant>]/          a role's copy
    <data_dir>/tmp/local-ai/downloads/<model id>/<path>.partial               a download

The library is the `models` kind of the bulk store (storage/stores.py): on
this machine's own disk until a bulk folder or a move puts it on the NAS.
It is always the `library/` folder inside the kind's folder, because that
folder's default is `models_dir`, which also holds the ONNX and Whisper
caches. Nothing here ever deletes outside the folder a model was given.

When the library is somewhere that can be unplugged, a model that has a role
is copied to this machine (`place`), so the Looker keeps working with the
NAS away. When the library is on this machine it is the copy, and nothing
is copied.

A download lands on this machine first, file by file, is checked against
what Hugging Face lists, and only then is put in the library under its real
name. `manifest.json`, written last, is the only mark of a finished model:
a share that drops half way leaves files without one, which reads as "not
installed", never as a model that is whole.

Everything here blocks and may touch a share: the lane calls it in a thread.
It imports no database and no queue (the model server uses it too).
"""

import contextlib
import os
import shutil
import stat
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from framefound.config import Settings, get_settings
from framefound.local_models.files import (
    MANIFEST,
    Manifest,
    check_file,
    inspect_config,
    read_manifest,
    safe_path,
    to_inspect,
    write_manifest,
)
from framefound.local_models.hub import Hub, RepoFile, Stopped
from framefound.storage import stores

LIBRARY = "library"
LOCAL = "local-ai"
KIND = "models"
PARTIAL = stores.PARTIAL_SUFFIX

# How often progress is handed to `report` while bytes are moving.
REPORT_EVERY_S = 2.0
# A move of the library pauses its writers for its last pass; a download that
# reaches a file's last step then waits rather than giving up. The pause
# lapses on its own (stores.PAUSE_EXPIRES_S), so this is only a backstop.
MOVE_WAIT_S = 30 * 60.0
MOVE_POLL_S = 5.0
COPY_CHUNK = 4 * 1024 * 1024

# The only failures that mean "the disk says this is not here". Any other
# OSError means nobody could look (files.py has the same rule).
_NOT_THERE = (FileNotFoundError, NotADirectoryError)
_GB = 1024**3
_MB = 1024**2

BAD_NAME = (
    "This model's name or build cannot be used as a folder name, so FrameFound will "
    "not store it. Remove the model."
)
NOT_IN_LIBRARY = "Its files are not in the library. Download it again, or remove it."
NOT_ON_THIS_MACHINE = "Its copy on this machine is missing or not complete. Copy it again."
# Said after the library's own "not connected" sentence, so the operator
# knows what stopped and what did not.
KEPT = "What has arrived so far is kept on this machine; Resume carries on from there."
ROLE_STAYS = "The role stays with the model that has it now."
NOTHING_REMOVED = "Nothing was removed."
ONLY_IN_LIBRARY = (
    "Models that have a role are on this machine and keep working; this one is only in the library."
)


class LibraryProblem(RuntimeError):
    """Something the operator has to fix; the message says what.

    `resumable` is False when doing the same again cannot help (the model's
    own files ask for code, its files are gone from the library): the way
    out is to remove it or download it again, not Resume."""

    def __init__(self, message: str, *, resumable: bool = True) -> None:
        super().__init__(message)
        self.resumable = resumable


@dataclass(frozen=True)
class ModelFiles:
    """A plain snapshot of a model's row, safe to hand to a thread
    (`registry.snapshot`)."""

    id: uuid.UUID
    repo_id: str
    revision: str
    variant: str
    runtime: str
    files: tuple[RepoFile, ...]
    # Made by the demo's fake hub: a few kilobytes per file, which no
    # checksum would pass. Only ever true in demo mode (runtime "demo").
    demo: bool = False


# (phase, done_bytes, total_bytes)
Report = Callable[[str, int, int], None]


def _never() -> bool:
    return False


# --- where --------------------------------------------------------------------


def _parts(model: ModelFiles) -> tuple[str, ...]:
    """The folder names a model is kept under. Every one a single plain name:
    a repository name with a climb in it must not become a path that leaves
    the library."""
    publisher, _slash, name = model.repo_id.partition("/")
    parts = (publisher, name, model.revision, *((model.variant,) if model.variant else ()))
    for part in parts:
        if not part or "/" in part or not safe_path(part):
            raise LibraryProblem(BAD_NAME, resumable=False)
    return parts


def library_root(settings: Settings | None = None) -> Path:
    """`<the models kind's folder>/library`."""
    return stores.kind_folder(KIND, settings) / LIBRARY


def library_dir(model: ModelFiles, settings: Settings | None = None) -> Path:
    """`<library>/<publisher>/<name>/<revision>[/<variant>]`."""
    return library_root(settings).joinpath(*_parts(model))


def copies_to_this_machine(settings: Settings | None = None) -> bool:
    """True when the library is somewhere that can be unplugged, so a model
    that has a role needs a copy on this machine."""
    return stores.is_separate(KIND, settings)


def _local_root(settings: Settings) -> Path:
    return settings.models_dir / LOCAL


def local_dir(model: ModelFiles, settings: Settings | None = None) -> Path:
    """Where a model runs from on this machine: its copy under
    `<models_dir>/local-ai` when the library is elsewhere, else the library
    folder itself (D14)."""
    settings = settings or get_settings()
    if copies_to_this_machine(settings):
        return _local_root(settings).joinpath(*_parts(model))
    return library_dir(model, settings)


def scratch_root(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.data_dir / "tmp" / "local-ai" / "downloads"


def scratch_dir(model_id: uuid.UUID, settings: Settings | None = None) -> Path:
    """`<data_dir>/tmp/local-ai/downloads/<id>`: always this machine's own
    disk, so what a download has fetched outlives a share that drops."""
    return scratch_root(settings) / str(model_id)


def away(settings: Settings | None = None) -> str | None:
    """Why nothing may be written to the library now, without paths: for
    anyone signed in."""
    return stores.unwritable(KIND, settings)


def unreachable(settings: Settings | None = None) -> str | None:
    """Why the library cannot be READ now (its share is away), without paths;
    None while it is there. A move's pause stops writing, not reading."""
    return stores.away(KIND, settings)


def write_problem(settings: Settings | None = None) -> str | None:
    """The same, with paths: for logs and administrators."""
    return stores.write_problem(KIND, settings)


def free_bytes(settings: Settings | None = None) -> int | None:
    """Room on the disk that holds the library; None while it is away or
    cannot be asked."""
    settings = settings or get_settings()
    if stores.kind_problem(KIND, settings) is not None:
        return None
    found = _disk(library_root(settings))
    return None if found is None else found[0]


# --- is it there --------------------------------------------------------------


@dataclass(frozen=True)
class Presence:
    # None: the library is away, nobody can look.
    in_library: bool | None
    on_this_machine: bool


def presence(model: ModelFiles, settings: Settings | None = None) -> Presence:
    """Is the model whole in the library, and on this machine? By the
    manifest and the files' sizes; nothing is hashed (that was done when
    the files arrived)."""
    settings = settings or get_settings()
    try:
        library = library_dir(model, settings)
        here = local_dir(model, settings)
    except LibraryProblem:
        return Presence(in_library=False, on_this_machine=False)
    in_library: bool | None
    if stores.kind_problem(KIND, settings) is not None:
        in_library = None
    else:
        try:
            in_library = _whole(library, model)
        except OSError:
            in_library = None
    if not copies_to_this_machine(settings):
        return Presence(in_library=in_library, on_this_machine=in_library is True)
    try:
        on_this_machine = _whole(here, model)
    except OSError:
        on_this_machine = False
    return Presence(in_library=in_library, on_this_machine=on_this_machine)


def serving_dir(model: ModelFiles, *, need_local: bool, settings: Settings | None = None) -> Path:
    """The folder to load the model from: its copy on this machine when that
    is whole; else, when `need_local` is False (a Test of a model with no
    role), the library when that is whole; else LibraryProblem."""
    settings = settings or get_settings()
    if copies_to_this_machine(settings):
        here = local_dir(model, settings)
        with contextlib.suppress(OSError):
            if _whole(here, model):
                return here
        if need_local:
            raise LibraryProblem(NOT_ON_THIS_MACHINE)
        if stores.kind_problem(KIND, settings) is not None:
            raise LibraryProblem(f"{away(settings)} {ONLY_IN_LIBRARY}")
    library = library_dir(model, settings)
    try:
        whole = _whole(library, model)
    except OSError as err:
        raise LibraryProblem(_unreadable(settings, err, ONLY_IN_LIBRARY)) from None
    if whole:
        return library
    raise LibraryProblem(NOT_IN_LIBRARY, resumable=False)


def _describes(manifest: Manifest, model: ModelFiles) -> bool:
    """The manifest is this build's, and lists the files this build is."""
    return (
        manifest.repo_id.casefold() == model.repo_id.casefold()
        and manifest.revision == model.revision
        and manifest.variant == model.variant
        and {file.path.casefold() for file in manifest.files}
        == {file.path.casefold() for file in model.files}
    )


def _whole(folder: Path, model: ModelFiles) -> bool:
    """The folder holds this build: its manifest, and every file it lists at
    its size. OSError when the disk could not be looked at."""
    manifest = read_manifest(folder)
    if manifest is None or not _describes(manifest, model):
        return False
    return all(_size_there(folder, file.path) == file.size for file in manifest.files)


def _at(folder: Path, relative: str) -> Path:
    return folder.joinpath(*relative.split("/"))


def _size_there(folder: Path, relative: str) -> int | None:
    """The size of a plain file at `relative`, or None when there is none.
    lstat, not is_file(): a link is not one of the model's files, and on
    newer Pythons is_file() answers False for a disk nobody could read."""
    try:
        found = _at(folder, relative).lstat()
    except _NOT_THERE:
        return None
    return found.st_size if stat.S_ISREG(found.st_mode) else None


# --- download -----------------------------------------------------------------


class _Teller:
    """`report`, at most every REPORT_EVERY_S, and always when told to."""

    def __init__(self, report: Report | None, phase: str, total: int) -> None:
        self.report = report
        self.phase = phase
        self.total = total
        self._last = float("-inf")

    def __call__(self, done: int, *, now: bool = False) -> None:
        if self.report is None:
            return
        moment = time.monotonic()
        if now or moment - self._last >= REPORT_EVERY_S:
            self._last = moment
            self.report(self.phase, max(0, min(done, self.total)), self.total)


def download(
    hub: Hub,
    model: ModelFiles,
    *,
    settings: Settings | None = None,
    report: Report | None = None,
    should_stop: Callable[[], bool] = _never,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Fetch every file of `model` into the library, checked, then write its
    manifest. Resumable: what arrived before stays and is not fetched again.

    Raises `LibraryProblem` with nothing in the library under a wrong name
    (a file that fails its check is thrown away, and named); `hub.HubError`
    when Hugging Face could not be read; `hub.Stopped` when `should_stop`
    said so. In each case what had arrived is kept, and the last `report`
    before the error says how much that is."""
    settings = settings or get_settings()
    scratch = scratch_dir(model.id, settings)
    # Where the library is, asked again after every wait: a move of the
    # library may finish while this download waits for it, and from then on
    # the old folder is the move's "old copies", which may be emptied.
    library = _ready(model, settings, should_stop, sleep, then=KEPT)

    try:
        there = {file.path: _in_library(library, file, model.demo) for file in model.files}
    except OSError as err:
        raise LibraryProblem(_unreadable(settings, err, KEPT)) from None
    started = {
        file.path: min(_size_there(scratch, file.path + PARTIAL) or 0, file.size)
        for file in model.files
        if there[file.path] is None
    }
    left = sum(file.size for file in model.files if there[file.path] is None)
    if not model.demo:  # the demo writes a few kilobytes against gigabytes listed
        _check_room(
            settings, scratch=scratch, library=library, left=left, fetched=sum(started.values())
        )

    total = sum(file.size for file in model.files)
    done = sum(file.size for file in model.files if there[file.path] is not None)
    done += sum(started.values())
    tell = _Teller(report, "downloading", total)
    tell(done, now=True)

    try:
        kept: list[RepoFile] = []
        for file in model.files:
            if should_stop():
                raise Stopped()
            partial = _at(scratch, file.path + PARTIAL)
            size = there[file.path]
            if size is not None:
                _unlink(partial)  # a leftover from an attempt that put it in place
                kept.append(_as_written(file, size, model.demo))
                continue
            got = started[file.path]

            def arrived(count: int) -> None:
                nonlocal got, done
                got += count
                done += count
                tell(done)

            try:
                if (_size_there(scratch, file.path + PARTIAL) or 0) > file.size:
                    # Longer than the file can be: asked for "the rest", the server
                    # would say there is none, and the check would refuse it for ever.
                    partial.unlink(missing_ok=True)
                    done -= got
                    got = 0
                hub.fetch(
                    model.repo_id,
                    model.revision,
                    file.path,
                    partial,
                    on_bytes=arrived,
                    should_stop=should_stop,
                )
                if not model.demo:
                    wrong = check_file(partial, file)
                    if wrong is not None:
                        partial.unlink(missing_ok=True)
                        done -= got  # reported below, as every ending is
                        raise LibraryProblem(f"{wrong} Resume fetches it again.")
                written = _size_there(scratch, file.path + PARTIAL) or 0
            except OSError as err:
                # This machine's own disk (full, or a folder it may not write).
                raise LibraryProblem(_not_written_here("The download", err, KEPT)) from None
            library = _ready(model, settings, should_stop, sleep, then=KEPT)
            try:
                stores.put_in_place(partial, _at(library, file.path))
            except OSError as err:
                raise LibraryProblem(_unreadable(settings, err, KEPT)) from None
            kept.append(_as_written(file, written, model.demo))
            done += file.size - got
            tell(done)

        library = _ready(model, settings, should_stop, sleep, then=KEPT)
        try:
            # Files put in place before a move of the library were carried over
            # by it; one that was not is fetched again, never reported whole.
            for file in kept:
                if _size_there(library, file.path) != file.size:
                    raise LibraryProblem(
                        f"{file.path} went from the library while the model was downloading. "
                        "Resume fetches it again."
                    )
            # Read again as they are on disk: what is loaded is what is here,
            # not what the page said when the model was checked.
            for file in to_inspect(kept):
                with _at(library, file.path).open("rb") as source:
                    text = source.read(4_000_001).decode("utf-8-sig", errors="replace")
                problems = inspect_config(file.path, text)
                if problems:
                    raise LibraryProblem(problems[0], resumable=False)
            write_manifest(
                library,
                Manifest(
                    repo_id=model.repo_id,
                    revision=model.revision,
                    variant=model.variant,
                    runtime=model.runtime,
                    files=tuple(kept),
                    completed_at=_now(),
                    demo=model.demo,
                ),
            )
        except OSError as err:
            raise LibraryProblem(_unreadable(settings, err, KEPT)) from None
    except Exception:
        # However it ended (the line dropped, Cancel, a disk that filled, a
        # library that went away), say where it stands before the error goes
        # up. Reports are at most one every REPORT_EVERY_S, so the last one
        # may be that old, and the job writes "The connection dropped at X of
        # Y GB" and "Resume carries on from X GB" from it: left alone, those
        # said less than had been kept.
        tell(done, now=True)
        raise
    with contextlib.suppress(OSError):
        _drop_empty_tree(scratch, scratch_root(settings))
    tell(total, now=True)


def _in_library(library: Path, file: RepoFile, demo: bool) -> int | None:
    """The size of `file` in the library when it is there whole, else None.
    Only checked files are ever put there, so its size says it is whole."""
    size = _size_there(library, file.path)
    if size is None or (size != file.size and not demo):
        return None
    return size


def _as_written(file: RepoFile, size: int, demo: bool) -> RepoFile:
    # The demo's few kilobytes are what its manifest lists, so that the
    # demo's library reads as whole the way a real one does.
    return replace(file, size=size, sha256=None, blob_id=None) if demo else file


def _ready(
    model: ModelFiles,
    settings: Settings,
    should_stop: Callable[[], bool],
    sleep: Callable[[float], None],
    *,
    then: str,
) -> Path:
    """Wait out a move of the library that is in its last steps; then the
    model's folder in the library as it is NOW — the move may have switched
    the library to another folder while this waited. LibraryProblem if the
    library still cannot be written to (away)."""
    waited = 0.0
    while waited < MOVE_WAIT_S and stores.read_state(settings.data_dir).is_paused(KIND):
        if should_stop():
            raise Stopped()
        sleep(MOVE_POLL_S)
        waited += MOVE_POLL_S
    problem = away(settings)
    if problem is not None:
        raise LibraryProblem(f"{problem} {then}")
    return library_dir(model, settings)


def _check_room(
    settings: Settings, *, scratch: Path, library: Path, left: int, fetched: int
) -> None:
    """Refuse before a byte is fetched when the files will not fit: this
    machine's disk must take what is still to come, the library's what it
    does not hold yet, each leaving `min_free_gb`. One disk is asked once."""
    floor = int(settings.min_free_gb * _GB)
    to_fetch = max(0, left - fetched)
    here = _disk(scratch)
    there = _disk(library)
    asks: list[tuple[str, int, int]] = []
    if here is not None:
        asks.append(("on this machine's own disk", to_fetch, here[0]))
    if there is not None and (here is None or there[1] != here[1]):
        asks.append(("on the disk that holds the model library", left, there[0]))
    for where, needed, free in asks:
        if needed > 0 and free - needed < floor:
            raise LibraryProblem(
                f"There is not enough room {where}: the model needs {_size_text(needed)} "
                f"more and the disk has {_size_text(free)} free. FrameFound leaves the last "
                f"{settings.min_free_gb:g} GB of any disk it writes to. Free some space, "
                "then try again."
            )


def _disk(folder: Path) -> tuple[int, int] | None:
    """(free bytes, device) of the disk `folder` is, or will be, on."""
    try:
        nearest = next(p for p in (folder, *folder.parents) if p.exists())
        return shutil.disk_usage(nearest).free, nearest.stat().st_dev
    except (OSError, StopIteration):
        return None


# --- this machine's copy --------------------------------------------------------


def place(
    model: ModelFiles,
    *,
    settings: Settings | None = None,
    report: Report | None = None,
    should_stop: Callable[[], bool] = _never,
) -> None:
    """Copy the model from the library to this machine, each file checked.

    Nothing to copy when the library is on this machine: it is the copy, and
    it is only checked to be whole. A failure leaves no copy that reads as
    whole (the manifest is removed first and written last), so the role it
    was meant for stays with whoever has it."""
    settings = settings or get_settings()
    library = library_dir(model, settings)
    if not copies_to_this_machine(settings):
        try:
            whole = _whole(library, model)
        except OSError as err:
            raise LibraryProblem(_unreadable(settings, err, ROLE_STAYS)) from None
        if not whole:
            raise LibraryProblem(NOT_IN_LIBRARY, resumable=False)
        return
    here = local_dir(model, settings)
    with contextlib.suppress(OSError):
        if _whole(here, model):
            return  # copied already: nothing to do, whether or not the library is there
    if stores.kind_problem(KIND, settings) is not None:
        raise LibraryProblem(f"{away(settings)} {ROLE_STAYS}")
    try:
        manifest = read_manifest(library)
    except OSError as err:
        raise LibraryProblem(_unreadable(settings, err, ROLE_STAYS)) from None
    if manifest is None or not _describes(manifest, model):
        raise LibraryProblem(NOT_IN_LIBRARY, resumable=False)
    # Checked against the row, which is what was verified at download; the
    # demo's manifest lists its few kilobytes instead.
    wanted = manifest.files if model.demo else model.files

    try:
        # A copy being made again must never read as whole half way through.
        _unlink(here / MANIFEST)
        for file in wanted:
            _unlink(_at(here, file.path + PARTIAL))
        have = {file.path: _size_there(here, file.path) == file.size for file in wanted}
    except OSError as err:
        raise LibraryProblem(
            _not_written_here("The copy on this machine", err, ROLE_STAYS)
        ) from None
    _check_local_room(settings, here, sum(f.size for f in wanted if not have[f.path]))

    total = sum(file.size for file in wanted)
    done = sum(file.size for file in wanted if have[file.path])
    tell = _Teller(report, "copying", total)
    tell(done, now=True)
    for file in wanted:
        if should_stop():
            raise Stopped()
        if have[file.path]:
            continue
        partial = _at(here, file.path + PARTIAL)

        def copied(count: int) -> None:
            nonlocal done
            done += count
            tell(done)

        before = done
        try:
            _copy(_at(library, file.path), partial, copied, should_stop)
            wrong = check_file(partial, file)
        except _WriteHereError as failed:
            with contextlib.suppress(OSError):
                _unlink(partial)
            raise LibraryProblem(
                _not_written_here("The copy on this machine", failed.err, ROLE_STAYS)
            ) from None
        except FileNotFoundError:
            _unlink(partial)
            raise LibraryProblem(
                f"{file.path} is missing from the library. Download the model again.",
                resumable=False,
            ) from None
        except OSError as err:
            _unlink(partial)
            raise LibraryProblem(_unreadable(settings, err, ROLE_STAYS)) from None
        if wrong is not None:
            _unlink(partial)
            raise LibraryProblem(
                f"{wrong} The library's copy may be damaged: if copying again does the same, "
                "download the model again."
            )
        try:
            os.replace(partial, _at(here, file.path))
        except OSError as err:
            raise LibraryProblem(
                _not_written_here("The copy on this machine", err, ROLE_STAYS)
            ) from None
        done = before + file.size
        tell(done)
    try:
        write_manifest(here, manifest)
    except OSError as err:
        raise LibraryProblem(
            _not_written_here("The copy on this machine", err, ROLE_STAYS)
        ) from None
    tell(total, now=True)


class _WriteHereError(Exception):
    """Writing on this machine failed (not reading the library): `err` says why."""

    def __init__(self, err: OSError) -> None:
        super().__init__(str(err))
        self.err = err


def _copy(
    source: Path,
    destination: Path,
    on_bytes: Callable[[int], None],
    should_stop: Callable[[], bool],
) -> None:
    """Copy `source` to `destination`. OSError reading the source; `_WriteHereError`
    writing the destination, so a full disk here is not called a library problem."""
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        writer = destination.open("wb")
    except OSError as err:
        raise _WriteHereError(err) from err
    with writer, source.open("rb") as reader:
        while chunk := reader.read(COPY_CHUNK):
            try:
                writer.write(chunk)
            except OSError as err:
                raise _WriteHereError(err) from err
            on_bytes(len(chunk))
            if should_stop():
                raise Stopped()


def keep_on_this_machine(models: Iterable[ModelFiles], settings: Settings | None = None) -> int:
    """Before the library leaves this machine: give each of `models` (those
    that have a role, or are waiting for one) its own copy under
    `<models_dir>/local-ai`, so it goes on running from this machine once
    the library is elsewhere. `local_dir` follows where the library is, and
    while it was here nothing was ever copied (D14).

    The library is on this machine's disk while this is asked, so each file
    is given a second name (a hard link: no room used, and removing the
    library's old copies later leaves it), and copied only where the disk
    cannot do that. A model not whole in the library is passed by: it could
    not run before either. Nothing to do once the library is elsewhere —
    those copies are `place`'s. Returns how many models were given a copy.
    LibraryProblem when one cannot be made; no copy half made reads as whole."""
    settings = settings or get_settings()
    if copies_to_this_machine(settings):
        return 0
    made = 0
    for model in models:
        try:
            source = library_dir(model, settings)
            here = _local_root(settings).joinpath(*_parts(model))
        except LibraryProblem:
            continue  # a name no folder may have: nothing of it is here to keep
        try:
            if not _whole(source, model) or _whole(here, model):
                continue
            manifest = read_manifest(source)
            if manifest is None:
                continue
            _unlink(here / MANIFEST)
            for file in manifest.files:
                partial = _at(here, file.path + PARTIAL)
                _unlink(partial)
                partial.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(_at(source, file.path), partial)
                except OSError:
                    shutil.copyfile(_at(source, file.path), partial)
                os.replace(partial, _at(here, file.path))
            write_manifest(here, manifest)
        except OSError as err:
            raise LibraryProblem(
                f"{model.repo_id} could not be given its own copy on this machine "
                f"({err.strerror or err}), so the model library was not moved: the "
                "model would have stopped working."
            ) from None
        made += 1
    return made


def _check_local_room(settings: Settings, here: Path, needed: int) -> None:
    found = _disk(here)
    floor = int(settings.min_free_gb * _GB)
    if found is not None and needed > 0 and found[0] - needed < floor:
        raise LibraryProblem(
            f"There is not enough room on this machine's own disk for the model: it needs "
            f"{_size_text(needed)} and the disk has {_size_text(found[0])} free. FrameFound "
            f"leaves the last {settings.min_free_gb:g} GB of any disk it writes to. "
            f"{ROLE_STAYS}"
        )


# --- removing -------------------------------------------------------------------


def drop_local(model: ModelFiles, settings: Settings | None = None) -> int:
    """Remove the model's copy on this machine; the bytes freed. Never the
    library: the copy lives under `<models_dir>/local-ai/<publisher>/...`
    and the library under `<its folder>/library/<publisher>/...`, so the two
    are never one folder, wherever the library is. Nothing to do when there
    is no copy."""
    settings = settings or get_settings()
    root = _local_root(settings)
    folder = root.joinpath(*_parts(model))
    return _clear(folder, root, _names(model, folder))


def remove(model: ModelFiles, settings: Settings | None = None) -> int:
    """Remove the model's files from the library, its partial download and
    its copy on this machine; the bytes freed. Refused, with nothing
    touched, while the library cannot be written to."""
    settings = settings or get_settings()
    problem = away(settings)
    if problem is not None:
        raise LibraryProblem(f"{problem} {NOTHING_REMOVED}")
    root = library_root(settings)
    folder = library_dir(model, settings)
    try:
        freed = _clear(folder, root, _names(model, folder))
    except OSError as err:
        raise LibraryProblem(
            f"The model's files could not all be removed from the library "
            f"({err.strerror or err}). Try again."
        ) from None
    # The folder a move of the library left its old copies in: a later move
    # back there would otherwise bring this model's files back with them.
    old = stores.read_state(settings.data_dir).previous.get(KIND)
    if old is not None and old != stores.kind_folder(KIND, settings):
        with contextlib.suppress(OSError):
            old_root = old / LIBRARY
            _clear(old_root.joinpath(*_parts(model)), old_root, _names(model, None))
    scratch = scratch_dir(model.id, settings)
    freed += _clear(scratch, scratch_root(settings), _names(model, None))
    return freed + drop_local(model, settings)


def _names(model: ModelFiles, folder: Path | None) -> list[str]:
    """The files a model's folder may hold: its own, by the row and by its
    manifest when that can be read."""
    paths = {file.path for file in model.files}
    if folder is not None:
        with contextlib.suppress(OSError):
            manifest = read_manifest(folder)
            if manifest is not None:
                paths |= {file.path for file in manifest.files if safe_path(file.path)}
    return sorted(paths)


def _clear(folder: Path, root: Path, paths: Iterable[str]) -> int:
    """Delete the manifest, then each named file and its `.partial`, then
    every folder left empty up to (not including) `root`. Nothing else: a
    folder one level too high would hold the CLIP and Whisper caches."""
    if root not in folder.parents:
        raise LibraryProblem(BAD_NAME, resumable=False)
    names = [MANIFEST, MANIFEST + PARTIAL]
    for path in paths:
        names += [path, path + PARTIAL]
    freed = 0
    for name in names:
        target = _at(folder, name)
        try:
            found = target.lstat()
        except _NOT_THERE:
            continue
        if not (stat.S_ISREG(found.st_mode) or stat.S_ISLNK(found.st_mode)):
            continue
        target.unlink()
        freed += found.st_size
        _drop_empty_parents(target, folder)
    _drop_empty_parents(folder / "x", root)
    return freed


def _drop_empty_parents(path: Path, stop: Path) -> None:
    """The folders `path` sat in, up to `stop`, while each is empty. `rmdir`
    refuses a folder with anything left in it."""
    for parent in path.parents:
        if parent == stop:
            break
        try:
            parent.rmdir()
        except OSError:
            break


def _drop_empty_tree(folder: Path, stop: Path) -> None:
    """Every empty folder under `folder`, then `folder` and its parents up to
    `stop`. A download's scratch, once its files have been put in place."""
    for here, _folders, _files in sorted(os.walk(folder), key=lambda entry: -len(entry[0])):
        with contextlib.suppress(OSError):
            Path(here).rmdir()
    _drop_empty_parents(folder / "x", stop)


def _unlink(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError, NotADirectoryError):
        path.unlink()


# --- words ----------------------------------------------------------------------


def _unreadable(settings: Settings, err: OSError, then: str) -> str:
    """A disk error while reading or writing the library, said plainly: the
    library being away when that is what it is, else what the system said."""
    gone = away(settings)
    if gone is not None:
        return f"{gone} {then}"
    return f"The model library could not be read or written ({err.strerror or err}). {then}"


def _not_written_here(what: str, err: OSError, then: str) -> str:
    """A disk error on this machine's own side (full, or a folder it may not
    write), said plainly."""
    return f"{what} could not be written on this machine's disk ({err.strerror or err}). {then}"


def _size_text(size: int) -> str:
    if size >= _GB:
        return f"{size / _GB:.1f} GB"
    return f"{max(1, round(size / _MB))} MB"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
