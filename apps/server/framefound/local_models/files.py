"""What may come onto the disk, and how a model's folder is known to be whole.

The owner's rule is that no program code from a model's repository ever runs.
A runtime flag does not keep that rule: the program that runs MLX models
imports a Python file from the model's folder whenever the model's settings
name one, with no question asked, and its own downloader fetches `*.py`. So
the rule is kept by what is allowed onto the disk, in four places that all
live here and none of which is optional:

1. `plan_files` — only weights in a format that cannot carry code, and plain
   data files, are ever downloaded. Code is not on the list, so it is never
   fetched.
2. `inspect_config` — a settings file that asks for code from the repository
   refuses the model, at the check and again once the file is on disk.
3. `check_file` — every file that arrives is the file Hugging Face listed.
4. `folder_problem` — before every load, the folder holds exactly the files
   its manifest lists and nothing else.

`manifest.json` is written last and is the only mark of "installed": a folder
without one is an unfinished download, whatever else is in it.

Nothing here talks to the network. `check_file`, the two hashes and
`folder_problem` read files, which on a share can be slow: call them from a
thread.

**"Not there" and "could not look" are kept apart.** The model library may be
on a network share. A file the disk says does not exist is missing, and gets
a sentence. Any other failure to read (the share stalled, went stale, or
refused) raises `OSError` from `check_file`, `read_manifest` and
`folder_problem` alike: the caller treats that as the store being away and
tries later. Read as "missing", it would tell the operator that a good copy
of a nine-gigabyte model is broken and should be downloaded again.
"""

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from framefound.local_models.hub import RepoFile

# Formats that hold numbers and nothing else.
WEIGHTS = (".safetensors", ".gguf")
# Formats that can carry program code (most are Python pickles underneath),
# or that no runtime here reads. Never downloaded.
PICKLE_ERA = (".bin", ".pt", ".pth", ".pkl", ".pickle", ".ckpt", ".npz", ".h5", ".msgpack", ".onnx")
DATA_SUFFIXES = (".json", ".jinja", ".txt", ".model")
TEXT_NAMES = (
    "README.md",
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "NOTICE",
    "NOTICE.md",
    "USE_POLICY.md",
)
# The first model's tokenizer.json is 32 MB. Anything far past that under a
# settings file's name is not a settings file.
MAX_DATA_BYTES = 128 * 1024 * 1024
# In a settings file, each of these means "to load me, run code from my repository".
CODE_KEYS = ("auto_map", "model_file", "custom_pipelines")
# The settings files a runtime reads to decide how to load a model.
INSPECTED = (
    "config.json",
    "tokenizer_config.json",
    "processor_config.json",
    "preprocessor_config.json",
)
# The most settings files one model may have. The first model has three; a
# model made of several parts has one or two per part. A repository that
# keeps every training checkpoint (or one padded on purpose) lists hundreds,
# and the check reads each over the network, one request apiece.
MAX_INSPECTED = 16
MANIFEST = "manifest.json"

NEEDS_CODE = (
    "This model only runs with program code from its own repository. FrameFound never runs "
    "code that comes with a model, so it has to wait for a build that does not need any."
)

_CODE = (".py", ".pyc", ".pyi", ".pyx", ".ipynb", ".sh", ".js", ".mjs", ".ts", ".so", ".dylib")
_PICTURES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")
_HASH_CHUNK = 4 * 1024 * 1024
_MAX_PATH = 400
_MAX_PART_BYTES = 255
# A settings file is read whole to be inspected; nothing honest is larger.
_MAX_INSPECTED_BYTES = 4_000_000
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_SHA256_LENGTH = 64
_BLOB_ID_LENGTH = 40
_GB = 1024**3
_MB = 1024**2
# The only failures that mean "the disk says this is not here". Every other
# OSError (input/output error, stale handle, timed out, not connected,
# permission refused) means nobody could look, and is left to rise.
_NOT_THERE = (FileNotFoundError, NotADirectoryError)


@dataclass(frozen=True)
class FilePlan:
    """Which of a repository's files would be downloaded, and why not the rest."""

    # Weights and data files, in download order (smallest first, so a wrong
    # settings file is found before nine gigabytes of weights arrive).
    keep: tuple[RepoFile, ...]
    weights: tuple[RepoFile, ...]
    # (path, why) for every file that is never fetched.
    left_out: tuple[tuple[str, str], ...]
    # Sentences. Any one of them means the model cannot be added.
    problems: tuple[str, ...]
    weights_bytes: int
    total_bytes: int
    # Which weights formats are among the kept files.
    suffixes: tuple[str, ...]


@dataclass(frozen=True)
class Manifest:
    """What a finished model folder holds. Written last; read before every load."""

    repo_id: str
    revision: str
    variant: str
    runtime: str
    files: tuple[RepoFile, ...]
    completed_at: str
    demo: bool = False


def safe_path(path: str) -> bool:
    """May this name from a file listing become a path under a model's folder?

    Relative, forward slashes only, no part that climbs (`..`) or hides
    (a leading dot), no drive or stream (`:`), no control character. A part
    that ends in a dot or a space is refused too: a network share stores
    such a name as something else, and the file would never be found again.
    """
    if not path or len(path) > _MAX_PATH or "\\" in path or ":" in path:
        return False
    if not path.isprintable():
        return False
    for part in path.split("/"):
        if not part or part.startswith(".") or part != part.strip() or part.endswith("."):
            return False
        if len(part.encode()) > _MAX_PART_BYTES:
            return False
    return True


def plan_files(files: Sequence[RepoFile]) -> FilePlan:
    """Decide, from the listing alone, what would be downloaded.

    Kept: every safetensors / GGUF file, every plain data file (by suffix,
    up to `MAX_DATA_BYTES`), and the licence and readme texts. Everything
    else is left out with its reason and is never fetched. Every file ends
    up in exactly one of `keep` and `left_out`.
    """
    keep: list[RepoFile] = []
    left_out: list[tuple[str, str]] = []
    problems: list[str] = []
    saw_weights = False
    older: list[RepoFile] = []

    for file in files:
        suffix = _suffix(file.path)
        is_weights = suffix in WEIGHTS
        saw_weights = saw_weights or is_weights
        wanted = is_weights or suffix in DATA_SUFFIXES or file.path in TEXT_NAMES

        if not wanted:
            if suffix in PICKLE_ERA:
                older.append(file)
            left_out.append((file.path, _why_not(suffix)))
        elif not is_weights and _only_hidden(file.path):
            # A dot-file is nobody's settings file. Not fetched, not a reason
            # to refuse an otherwise good model.
            left_out.append((file.path, "a hidden file"))
        elif not safe_path(file.path):
            left_out.append((file.path, "a name FrameFound will not write to disk"))
            problems.append(
                "One of its files has a name FrameFound will not write to disk "
                f"({_shown(file.path)})."
            )
        elif file.path.casefold() == MANIFEST:
            left_out.append((file.path, "FrameFound keeps its own file under this name"))
        elif is_weights and file.sha256 is None:
            left_out.append((file.path, "no checksum to verify it by"))
            problems.append(
                f"Hugging Face lists no checksum for {file.path}, so what is downloaded "
                "cannot be verified."
            )
        elif not is_weights and file.size > MAX_DATA_BYTES:
            left_out.append((file.path, "too large to be a settings file"))
            problems.append(
                f"{file.path} is {_size_text(file.size)}, far larger than a settings file "
                "should be. FrameFound will not download it."
            )
        else:
            keep.append(file)

    keep.sort(key=lambda file: (file.size, file.path))
    weights = [file for file in keep if _suffix(file.path) in WEIGHTS]

    if not saw_weights:
        if older:
            formats = ", ".join(
                dict.fromkeys(
                    _suffix(file.path) for file in sorted(older, key=lambda file: -file.size)
                )
            )
            problems.append(
                f"Its weights are in an older file format ({formats}) that can carry program "
                "code. FrameFound only accepts safetensors and GGUF files."
            )
        else:
            problems.append(
                "It has no model weights FrameFound can use. FrameFound only accepts "
                "safetensors and GGUF files."
            )

    # The Mac's disk and a network share do not tell capitals apart; Hugging
    # Face does. Two such files would overwrite each other.
    spelled: dict[str, str] = {}
    for file in keep:
        folded = file.path.casefold()
        if folded not in spelled:
            spelled[folded] = file.path
        elif spelled[folded] == file.path:
            problems.append(f"Hugging Face lists {file.path} twice.")
        else:
            problems.append(
                f"Two of its files have names that differ only in capitals ({spelled[folded]} "
                f"and {file.path}). This machine's disk cannot hold both."
            )

    settings = len(to_inspect(keep))
    if settings > MAX_INSPECTED:
        problems.append(
            f"It has {settings} settings files (config.json and the like), far more than one "
            "model has. FrameFound reads every one of them before it accepts a model, and will "
            "not read that many."
        )

    return FilePlan(
        keep=tuple(keep),
        weights=tuple(weights),
        left_out=tuple(left_out),
        problems=tuple(dict.fromkeys(problems)),
        weights_bytes=sum(file.size for file in weights),
        total_bytes=sum(file.size for file in keep),
        suffixes=tuple(
            suffix for suffix in WEIGHTS if any(_suffix(file.path) == suffix for file in weights)
        ),
    )


def to_inspect(files: Iterable[RepoFile]) -> tuple[RepoFile, ...]:
    """The settings files among `files` that must pass `inspect_config`, wherever they sit.

    Whatever their capitals. Hugging Face tells `Config.json` from
    `config.json`; the Mac's disk and a network share do not, so the program
    that opens `config.json` there is handed `Config.json`. A file left
    uninspected for a capital letter would be read by the runtime all the same.
    """
    return tuple(file for file in files if PurePosixPath(file.path).name.casefold() in INSPECTED)


def inspect_config(name: str, text: str) -> list[str]:
    """Why a settings file rules the model out: sentences, `[]` when it is clean.

    A file that asks for code from the repository — any of `CODE_KEYS`, at
    any depth, with something in it — gives `NEEDS_CODE`. A key that is
    there but empty names no code and is let through. A file that cannot be
    read as JSON is a problem too: what cannot be read cannot be cleared.
    """
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        return [
            f"{_shown(name)} could not be read, so FrameFound cannot tell whether this model "
            "needs program code from its repository."
        ]
    # A list of things still to look at rather than a function that calls
    # itself: the file's depth is the publisher's choice.
    waiting: list[object] = [data]
    while waiting:
        item = waiting.pop()
        if isinstance(item, dict):
            if any(item.get(key) for key in CODE_KEYS):
                return [NEEDS_CODE]
            waiting.extend(item.values())
        elif isinstance(item, list):
            waiting.extend(item)
    return []


def git_blob_id(path: Path) -> str:
    """The id git gives this file's bytes: how Hugging Face identifies a small file."""
    # SHA-1 because that is what git's id is; it identifies, it does not protect.
    digest = hashlib.sha1(b"blob %d\0" % path.stat().st_size, usedforsecurity=False)
    return _digest(path, digest)


def sha256_of(path: Path) -> str:
    return _digest(path, hashlib.sha256())


def check_file(path: Path, file: RepoFile) -> str | None:
    """Is the file at `path` the one Hugging Face listed? None when it is, else a sentence.

    By size; a `.gguf` file then by its first four bytes; then by SHA-256
    when the listing gives one; else by git blob id when it gives that. A
    file with neither is checked by size alone. The
    sentence says the file "was thrown away" because that is what every
    caller does with one that fails; this function itself deletes nothing.
    A disk that cannot be read raises `OSError`: that is the store being
    away, not a wrong file.
    """
    # stat, not is_file(): on newer Pythons is_file() answers False for a
    # disk that could not be read at all, which would read as "not there".
    try:
        found = path.stat()
    except _NOT_THERE:
        return f"{file.path} is not there."
    if not stat.S_ISREG(found.st_mode):
        return f"{file.path} is not there."
    if found.st_size != file.size:
        return f"{file.path} is not the size Hugging Face lists for it, so it was thrown away."
    if _suffix(file.path) == ".gguf":
        # Imported here: gguf.py builds on this module's plans and names.
        from framefound.local_models import gguf

        # Before the hash, which takes minutes on a 20 GB file: a GGUF file
        # that does not start as one is wrong whatever its checksum says,
        # and llama.cpp would only fail on it at the first Test.
        if not gguf.looks_like_gguf(path):
            return f"{file.path} is not a GGUF file, so it was thrown away."
    if file.sha256 is not None:
        matches = sha256_of(path) == file.sha256
    elif file.blob_id is not None:
        matches = git_blob_id(path) == file.blob_id
    else:
        matches = True
    if not matches:
        return (
            f"{file.path} is not the file Hugging Face lists (its checksum is different), "
            "so it was thrown away."
        )
    return None


def read_manifest(folder: Path) -> Manifest | None:
    """The folder's manifest; None when there is none or it cannot be trusted.

    None is "not installed", and that is the right reading of every way the
    file itself can be wrong: no file, half a file, a list that names a path
    outside the folder. Keys a newer FrameFound may have added are ignored.

    A disk that will not say whether the file is there raises `OSError`:
    "could not look" is not "not installed", and a caller that took it for
    that would offer to download a model that is sitting there whole.
    """
    target = folder / MANIFEST
    try:
        found = target.stat()
        if not stat.S_ISREG(found.st_mode) or found.st_size > _MAX_MANIFEST_BYTES:
            return None
        raw = target.read_bytes()
    except _NOT_THERE:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None
    files: list[RepoFile] = []
    for entry in data["files"]:
        file = _listed(entry)
        if file is None:
            return None
        files.append(file)
    if len({file.path.casefold() for file in files}) != len(files):
        return None
    words = [data.get(key) for key in ("repo_id", "revision", "variant", "runtime", "completed_at")]
    if not all(isinstance(word, str) for word in words) or not data["repo_id"]:
        return None
    return Manifest(
        repo_id=data["repo_id"],
        revision=data["revision"],
        variant=data["variant"],
        runtime=data["runtime"],
        files=tuple(files),
        completed_at=data["completed_at"],
        demo=data.get("demo") is True,
    )


def write_manifest(folder: Path, manifest: Manifest) -> None:
    """Mark the folder as a finished model.

    Written beside its final name and moved into place in one step, so a
    reader sees the old state or the whole new file, never half of one.
    """
    folder.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"version": 1, **asdict(manifest)}, indent=2) + "\n"
    partial = folder / f"{MANIFEST}.partial"
    with partial.open("w", encoding="utf-8", newline="\n") as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())
    os.replace(partial, folder / MANIFEST)


def folder_problem(folder: Path) -> str | None:
    """Why a model must not be loaded from this folder; None when it may be.

    Run before every load. The manifest is there; every file it lists is
    there, a plain file, at its size; nothing else is in the folder; and the
    settings files, as they are on disk now, still ask for no code. Nothing
    is hashed: that was done when the files arrived.

    A sentence means the folder is wrong. A disk that could not be read
    raises `OSError` instead (see the top of this file): the folder may be
    perfectly good, and nobody could look.
    """
    manifest = read_manifest(folder)
    if manifest is None:
        return "This copy of the model has no list of its files, so it is not complete."
    listed = {file.path for file in manifest.files}
    for file in manifest.files:
        try:
            found = folder.joinpath(*file.path.split("/")).lstat()
        except _NOT_THERE:
            return f"This copy of the model is missing {file.path}."
        # lstat, and a plain file only: a link would let the folder hold
        # something its manifest never described.
        if not stat.S_ISREG(found.st_mode):
            return f"{file.path} in this copy of the model is not an ordinary file."
        if found.st_size != file.size:
            return f"{file.path} in this copy of the model is not the size it should be."

    unread: list[OSError] = []
    for here, folders, names in os.walk(folder, onerror=unread.append):
        inside = Path(here).relative_to(folder)
        for name in folders:
            if _is_link(Path(here) / name):
                return _stray((inside / name).as_posix())
        for name in names:
            relative = (inside / name).as_posix()
            if relative in listed or relative == MANIFEST or _finder_left_it(name):
                continue
            return _stray(relative)
    for error in unread:
        if not isinstance(error, _NOT_THERE):
            raise error
    if unread:
        # A folder that went while it was being looked through: nobody saw
        # what was in it.
        return "This copy of the model could not be read in full."

    for file in to_inspect(manifest.files):
        on_disk = folder.joinpath(*file.path.split("/"))
        try:
            with on_disk.open("rb") as source:
                text = source.read(_MAX_INSPECTED_BYTES + 1).decode("utf-8-sig", errors="replace")
        except _NOT_THERE:
            return f"This copy of the model is missing {file.path}."
        problems = inspect_config(file.path, text)
        if problems:
            return problems[0]
    return None


def _suffix(path: str) -> str:
    """The file's ending, lower-cased: ".safetensors". A dot-file has none."""
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _why_not(suffix: str) -> str:
    if suffix in _CODE:
        return "program code"
    if suffix in _PICTURES:
        return "a picture"
    if suffix in PICKLE_ERA:
        return "an older weights format"
    return "not needed to run the model"


def _only_hidden(path: str) -> bool:
    """Unsafe only because a part starts with a dot (and it is not `..`)."""
    parts = path.split("/")
    if not any(part.startswith(".") for part in parts) or ".." in parts:
        return False
    return safe_path("/".join(part.lstrip(".") or "x" for part in parts))


def _is_link(path: Path) -> bool:
    # lstat, not is_symlink(): on newer Pythons is_symlink() answers False
    # for a disk that could not be read, and a link would pass as a folder.
    try:
        return stat.S_ISLNK(path.lstat().st_mode)
    except _NOT_THERE:
        return False


def _finder_left_it(name: str) -> bool:
    # Opening a folder in the Finder leaves .DS_Store in it, and a share
    # without extended attributes gets a "._name" beside each file. No
    # runtime reads either, and a model that stopped loading because its
    # folder had been looked at would be a fault with no visible cause.
    return name == ".DS_Store" or name.startswith("._")


def _stray(relative: str) -> str:
    return (
        "This copy of the model holds something that is not part of it "
        f"({_shown(relative)}). FrameFound will not load a model from a folder like that."
    )


def _listed(entry: object) -> RepoFile | None:
    """One manifest entry, or None when it is not one FrameFound could have written."""
    if not isinstance(entry, dict):
        return None
    path, size = entry.get("path"), entry.get("size")
    sha256, blob_id = entry.get("sha256"), entry.get("blob_id")
    if not isinstance(path, str) or not safe_path(path) or path.casefold() == MANIFEST:
        return None
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        return None
    if sha256 is not None and not _is_hex(sha256, _SHA256_LENGTH):
        return None
    if blob_id is not None and not _is_hex(blob_id, _BLOB_ID_LENGTH):
        return None
    return RepoFile(path=path, size=size, sha256=sha256, blob_id=blob_id)


def _is_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and all(letter in "0123456789abcdef" for letter in value)
    )


def _digest(path: Path, digest: "hashlib._Hash") -> str:
    with path.open("rb") as source:
        while chunk := source.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _size_text(size: int) -> str:
    """As the pages write sizes: GB with one decimal, whole MB below that. 1 GB = 1024³."""
    if size >= _GB:
        return f"{size / _GB:.1f} GB"
    return f"{max(1, round(size / _MB))} MB"


def _shown(text: str) -> str:
    """Somebody else's file name made fit to put in a sentence."""
    return "".join(letter for letter in text if letter.isprintable())[:120]
