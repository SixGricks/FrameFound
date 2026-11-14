"""The programs that run models, as folders on this Mac: install, switch, roll back.

Each version of a program lives in a folder of its own with its own Python,
so installing one can never break FrameFound or the version in use:

    <runtimes>/mlx-vlm/state.json        {"current": "0.7.12", "previous": "0.7.11"}
    <runtimes>/mlx-vlm/0.7.12/venv/…     one self-contained environment per version
    <runtimes>/mlx-vlm/0.7.12/installed.json   the "whole" mark, written last
    <runtimes>/scratch/hf-home/          the running program's empty HF_HOME

Installing is: a new folder for the version, the kind's commands run into
it, the kind's check run on it, and only then the mark. Nothing about which
version is in use changes while that happens. Switching is `activate`: one
replacement of `state.json`, which names the version in use and the one
before it, so a rollback is the same single write the other way round. A
failed install removes its own folder and leaves `state.json` and every
other version exactly as they were.

Every program is run through `proc.run` (or a stand-in a test gives), with
the environment `kinds.child_env` builds: an installer is a program from the
internet and is given none of FrameFound's settings. Nothing about installed
runtimes is in the database. These functions block: call them from a thread.
"""

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from framefound.config import Settings, get_settings
from framefound.local_models import UNAVAILABLE, availability
from framefound.local_models.kinds import VERSION as VERSION
from framefound.local_models.kinds import RuntimeKind, child_env, release_order
from framefound.native import proc

# `proc.run`'s shape: what `install` is given to run each command with.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]

STATE = "state.json"
WHOLE = "installed.json"
SCRATCH = "scratch"
# An install that takes longer than this is stuck, not slow.
STEP_TIMEOUT_S = 1800.0
# Listing what was installed is a record, not the install: it is not waited on long.
FREEZE_TIMEOUT_S = 120.0
# How many of the installer's last lines a failure keeps for the page.
TAIL_LINES = 20
TAIL_CHARS = 2000
# `state.json` is a read and then a write; this keeps two writers apart.
LOCK_WAIT_S = 10.0
LOCK_STALE_S = 30.0

NOTHING_TO_GO_BACK_TO = "There is no earlier version kept to go back to."
UNCHANGED = "Nothing in use was changed."

# A folder name this module made: a version or a kind's name. Anything else
# under the runtimes folder is not ours to read as one, or to delete.
_PART = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}")


class RuntimeProblem(RuntimeError):
    """Something the operator has to fix; the message says what.

    `detail` holds the last lines the installer printed, for the page to
    show as they are ("" when there are none).
    """

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True)
class Installed:
    """What is on disk for one kind."""

    # The version in use; None when nothing is.
    current: str | None
    # The version before it, kept for one click back; None when there is none.
    previous: str | None
    # Every whole version on disk, newest first.
    versions: tuple[str, ...]
    # When the version in use was installed ("2026-11-12T18:39:10Z").
    installed_at: str | None


def root(settings: Settings | None = None) -> Path:
    """The runtimes folder: `FRAMEFOUND_RUNTIMES_DIR`, or the demo's own under the data folder.

    `RuntimeProblem` with the page's sentence on an install that cannot run
    models at all.
    """
    settings = settings or get_settings()
    mode = availability(settings).mode
    if mode == "ready" and settings.runtimes_dir is not None:
        return settings.runtimes_dir
    if mode == "demo":
        return settings.data_dir / "tmp" / "local-ai" / "demo-runtimes"
    raise RuntimeProblem(UNAVAILABLE)


def scratch_dir(root: Path) -> Path:
    """Where a running program works and keeps its (empty) HF_HOME. Not a kind's name."""
    return root / SCRATCH


def clean_version(kind: RuntimeKind, text: str) -> str:
    """`text` as `kind` writes a version; `ValueError(sentence)` for anything else."""
    return kind.clean_version(text)


def version_dir(root: Path, name: str, version: str) -> Path:
    """`<root>/<name>/<version>`. `ValueError` for a name or version that is not a folder name."""
    return _kind_dir(root, name) / _part(version)


def describe(root: Path, name: str) -> Installed:
    """What is installed for the kind `name`.

    `RuntimeProblem` when `state.json` cannot be understood, or names as in
    use a version that is not whole on disk: which version runs is then
    unknown, and it is not guessed. Installing a version and switching to
    it writes a fresh `state.json`, which is the way out.
    """
    folder = _kind_dir(root, name)
    current, previous = _read_state(folder)
    versions = _whole_versions(folder)
    if current is not None and current not in versions:
        raise RuntimeProblem(
            f"The version recorded as in use ({current}) is missing or incomplete on disk. "
            "Install it again, or install another version."
        )
    if previous not in versions:
        previous = None
    installed_at = _installed_at(folder / current) if current is not None else None
    return Installed(current, previous, versions, installed_at)


def current_dir(root: Path, name: str) -> tuple[Path, str] | None:
    """The folder and version in use; None when nothing is. `RuntimeProblem` as `describe`."""
    current = describe(root, name).current
    if current is None:
        return None
    return version_dir(root, name, current), current


def install(
    kind: RuntimeKind,
    version: str,
    root: Path,
    *,
    uv: str = "uv",
    run: Runner = proc.run,
    say: Callable[[str], None] = lambda _line: None,
) -> None:
    """Install `version` of `kind` into its own folder. It does not switch to it: `activate` does.

    Each of the kind's commands, then its check, is run through `run` with
    an environment of `child_env({})`; `say` is told what is happening
    before each. The check must print exactly the version asked for. Only
    then is `installed.json` written. A version already whole on disk is
    left as it is.

    Any failure removes this version's folder and raises `RuntimeProblem`
    with the installer's last lines in `.detail`. `state.json` is never
    touched, so the version in use — and every other — is exactly as it was.
    """
    try:
        version = kind.clean_version(version)
    except ValueError as err:
        raise RuntimeProblem(str(err)) from None
    folder = version_dir(root, kind.name, version)
    if _is_whole(folder):
        say(f"{kind.label} {version} is already installed.")
        return
    done = False
    try:
        if _exists(folder):
            # Left by an install that was stopped part-way (the machine went
            # down). It was never marked whole, so nothing can be using it.
            shutil.rmtree(folder)
        folder.mkdir(parents=True)
        steps = kind.install_steps(folder, version, uv)
        if steps:
            _install_into(kind, version, folder, steps, uv, run, say)
            freeze = _freeze(kind, folder, uv, run)
        else:
            # A kind with nothing to install (the demo) is its folder.
            freeze = ""
        _write_json(
            folder / WHOLE,
            {"version": version, "installed_at": _now(), "freeze": freeze},
        )
        done = True
    except OSError as err:
        raise RuntimeProblem(
            f"{kind.label} {version} could not be installed: {err.strerror or err}. {UNCHANGED}"
        ) from None
    finally:
        if not done:
            shutil.rmtree(folder, ignore_errors=True)


def activate(root: Path, name: str, version: str) -> None:
    """Use `version` from now on, keeping the one in use as the one to go back to.

    One replacement of `state.json`. Then versions that are neither the
    one in use nor the one before it are removed. `RuntimeProblem` when
    `version` is not whole on disk.
    """
    folder = _kind_dir(root, name)
    with _locked(folder):
        if not _is_whole(version_dir(root, name, version)):
            raise RuntimeProblem(f"Version {version} is not installed, so it cannot be used.")
        try:
            current, previous = _read_state(folder)
            sound = True
        except RuntimeProblem:
            # Whatever was in use is not known. Recording this version
            # repairs the record; removing the others would be a guess.
            current, previous, sound = None, None, False
        if current != version:
            previous = current
        _write_state(folder, version, previous)
        if sound:
            _prune(folder, version, previous)


def rollback(root: Path, name: str) -> str:
    """Go back to the version before: the two swap places in one write. Returns the one in use."""
    folder = _kind_dir(root, name)
    with _locked(folder):
        current, previous = _read_state(folder)
        if previous is None or not _is_whole(folder / previous):
            raise RuntimeProblem(NOTHING_TO_GO_BACK_TO)
        _write_state(folder, previous, current)
        return previous


def prune(root: Path, name: str) -> list[str]:
    """Remove the whole versions that are neither in use nor the one before it; returns them.

    Removes nothing while `state.json` cannot be understood.
    """
    folder = _kind_dir(root, name)
    with _locked(folder):
        try:
            current, previous = _read_state(folder)
        except RuntimeProblem:
            return []
        return _prune(folder, current, previous)


# --- installing ----------------------------------------------------------------


def _install_into(
    kind: RuntimeKind,
    version: str,
    folder: Path,
    steps: list[list[str]],
    uv: str,
    run: Runner,
    say: Callable[[str], None],
) -> None:
    for number, argv in enumerate(steps, start=1):
        if number == 1 and len(steps) > 1:
            say("Making its own environment…")
        else:
            say(f"Installing {version} — a few minutes…")
        done = _run(kind, version, argv, run)
        if done.returncode != 0:
            raise RuntimeProblem(
                f"{kind.label} {version} could not be installed: "
                f"{Path(argv[0]).name} stopped with exit {done.returncode}. {UNCHANGED}",
                _tail(done),
            )
    say("Checking it starts…")
    done = _run(kind, version, kind.check_step(folder), run)
    if done.returncode != 0:
        raise RuntimeProblem(
            f"{kind.label} {version} was installed but would not start, so it was removed. "
            f"{UNCHANGED}",
            _tail(done),
        )
    printed = (done.stdout or "").strip().splitlines()
    reported = printed[-1].strip() if printed else ""
    if reported != version:
        raise RuntimeProblem(
            f"The installer reported a different version ({reported or 'none'}) from the one "
            f"asked for ({version}), so it was removed. {UNCHANGED}",
            _tail(done),
        )


def _run(
    kind: RuntimeKind, version: str, argv: list[str], run: Runner
) -> "subprocess.CompletedProcess[str]":
    try:
        return run(argv, env=child_env({}), capture=True, check=False, timeout=STEP_TIMEOUT_S)
    except proc.CommandFailed as err:
        # The program is not there, or ran out of time.
        raise RuntimeProblem(
            f"{kind.label} {version} could not be installed: {err}. {UNCHANGED}"
        ) from None


def _freeze(kind: RuntimeKind, folder: Path, uv: str, run: Runner) -> str:
    """What was installed, as the installer lists it. Best effort: never fails an install."""
    argv = kind.freeze_step(folder, uv)
    if not argv:
        return ""
    try:
        done = run(argv, env=child_env({}), capture=True, check=False, timeout=FREEZE_TIMEOUT_S)
    except proc.CommandFailed:
        return ""
    return (done.stdout or "") if done.returncode == 0 else ""


def _tail(done: "subprocess.CompletedProcess[str]") -> str:
    """The installer's last lines: what it said went wrong, else what it said last."""
    text = (done.stderr or "").strip() or (done.stdout or "").strip()
    return "\n".join(text.splitlines()[-TAIL_LINES:])[-TAIL_CHARS:]


# --- the record ------------------------------------------------------------------


def _read_state(folder: Path) -> tuple[str | None, str | None]:
    """(current, previous) from `state.json`; (None, None) when there is none yet."""
    path = folder / STATE
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    except OSError as err:
        raise RuntimeProblem(_damaged(path, err.strerror or str(err))) from None
    try:
        body = json.loads(text)
    except ValueError:
        raise RuntimeProblem(_damaged(path, "it is not readable")) from None
    if not isinstance(body, dict):
        raise RuntimeProblem(_damaged(path, "it is not readable"))
    names: list[str | None] = []
    for key in ("current", "previous"):
        value = body.get(key)
        if value is not None and (not isinstance(value, str) or not _PART.fullmatch(value)):
            raise RuntimeProblem(_damaged(path, f"its {key} version cannot be read"))
        names.append(value)
    current, previous = names
    if current is None and previous is not None:
        raise RuntimeProblem(_damaged(path, "it keeps a version to go back to but none in use"))
    return current, (previous if previous != current else None)


def _damaged(path: Path, why: str) -> str:
    return (
        f"FrameFound's record of which version is in use ({path}) is damaged: {why}. "
        "Install a version to replace it."
    )


def _write_state(folder: Path, current: str, previous: str | None) -> None:
    # One replacement, so a reader sees the old pair or the new, never half.
    _write_json(folder / STATE, {"version": 1, "current": current, "previous": previous})


def _write_json(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(body, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


@contextlib.contextmanager
def _locked(folder: Path) -> Iterator[None]:
    """One writer of `state.json` at a time, across processes.

    An activate (the lane, after an install) and a rollback (the page)
    landing on each other's read and write would lose one of them.
    """
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / f"{STATE}.lock"
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
                raise RuntimeProblem(
                    "Another change to this software is being saved. Try again in a moment."
                ) from None
            time.sleep(0.02)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.unlink(missing_ok=True)


# --- the folders -------------------------------------------------------------------


def _part(text: str) -> str:
    if not _PART.fullmatch(text) or ".." in text:
        raise ValueError(f"{text!r} is not a plain folder name.")
    return text


def _kind_dir(root: Path, name: str) -> Path:
    if _part(name) == SCRATCH:
        raise ValueError(f"{name!r} is the runtimes folder's own scratch folder.")
    return root / name


def _exists(path: Path) -> bool:
    try:
        path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _is_whole(folder: Path) -> bool:
    # The mark is written last, so a folder with it holds a version that
    # installed and passed its check. A folder without it is half an install.
    return _exists(folder / WHOLE)


def _whole_versions(folder: Path) -> tuple[str, ...]:
    try:
        entries = list(os.scandir(folder))
    except (FileNotFoundError, NotADirectoryError):
        return ()
    found = [
        entry.name
        for entry in entries
        if _PART.fullmatch(entry.name)
        and ".." not in entry.name
        and entry.is_dir(follow_symlinks=False)
        and _is_whole(Path(entry.path))
    ]
    return _newest_first(found)


def _newest_first(names: list[str]) -> tuple[str, ...]:
    """Newest first by the one rule for which release follows which
    (`kinds.release_order`, what "update available" uses too): 0.8.1 after
    0.8, a release after its candidates, a `.post` fix-up after the release.
    A folder name that is not a version is listed after every one that is,
    by its numbers."""
    versions = [name for name in names if release_order(name) is not None]
    others = [name for name in names if release_order(name) is None]
    versions.sort(key=lambda name: (release_order(name), name), reverse=True)
    others.sort(key=_by_numbers)
    return (*versions, *others)


def _by_numbers(name: str) -> tuple[tuple[int, ...], str]:
    # Numbers as numbers (0.7.12 after 0.7.9), newest first.
    numbers = tuple(int(piece) for piece in re.findall(r"\d+", name))
    return tuple(-number for number in numbers), name


def _installed_at(folder: Path) -> str | None:
    try:
        body = json.loads((folder / WHOLE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    stamp = body.get("installed_at") if isinstance(body, dict) else None
    return stamp if isinstance(stamp, str) else None


def _prune(folder: Path, current: str | None, previous: str | None) -> list[str]:
    keep = {current, previous}
    gone: list[str] = []
    for version in _whole_versions(folder):
        if version in keep:
            continue
        # The mark goes first: a removal cut short leaves half a folder that
        # nothing reads as a version, and the next install of it clears it.
        # Tidying is not the switch: a version that will not go is left for
        # the next time, rather than reporting a switch that happened as failed.
        try:
            (folder / version / WHOLE).unlink()
        except OSError:
            continue
        shutil.rmtree(folder / version, ignore_errors=True)
        gone.append(version)
    return gone


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
