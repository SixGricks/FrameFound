"""Which file of a GGUF repository is what: the model, the part that reads pictures, the builds.

A GGUF repository is not one model but a shelf of them: the same weights
rounded to different precisions ("quantisations"), one file each, often
twenty of them and 150 GB together, plus a separate file that lets the model
read pictures (the "projector", `mmproj` in its name). Every rule that only
looks at the suffix gets such a repository wrong — the projector is a
`.gguf` too, and "the repository's size" is the size of every build at once.
So the files are sorted here, once, into builds, and everything after the
check reads a build's own files (`kinds.Variant.files`), never the plan.

The rules, each one the check shows rather than a guess it hides (L6 D8):

- A `.gguf` whose name contains `mmproj` (any capitals) is a projector;
  every other `.gguf` is a model file.
- The parts of a split model (`<stem>-00001-of-00003.gguf`) are one build,
  and the first part is what the program is given. A set with a part
  missing is not a build.
- A build's id is the quantisation at the end of its name (`QUANT`), else
  the whole name. The id becomes a folder name, so one that could not be is
  left out with the reason.
- The projector for a build is the one named for it, else by precision
  (`PROJECTOR_ORDER`), else the first by name.
- The build suggested is a `Q4_K_M` that fits a budget (llama.cpp's own
  default), else the largest that fits by day, else the largest that fits
  overnight, else the smallest.

Pure, apart from `looks_like_gguf`, which reads four bytes of a file.
Nothing here talks to the network.
"""

# Annotations stay text: `kinds` is imported as a module, not by name,
# because the llama.cpp kind in kinds.py is built on this module, and each
# then finds the other half-made while the first is still being imported.
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from framefound.local_models import kinds
from framefound.local_models.files import TEXT_NAMES, FilePlan
from framefound.local_models.hub import RepoFile

PROJECTOR = re.compile(r"mmproj", re.IGNORECASE)
SPLIT = re.compile(r"^(?P<stem>.+)-(?P<part>\d{5})-of-(?P<count>\d{5})$")
# The quantisation at the end of a model file's name (without `.gguf` and
# without a split suffix): "…-Reasoning-Q4_K_M" → "Q4_K_M",
# "…-UD-IQ4_NL_XL" → "UD-IQ4_NL_XL", "…-MXFP4_MOE" → "MXFP4_MOE",
# "name.Q2_K" → "Q2_K". (L6 design 2.5.)
QUANT = re.compile(
    r"(?:^|[-._])((?:UD-)?(?:I?Q\d+(?:_[A-Z0-9]+)*|TQ\d_\d|BF16|F16|F32|F64|MXFP4(?:_[A-Z0-9]+)*))$",
    re.IGNORECASE,
)
# When no projector is named for a build: the precision a projector loses
# nothing at and costs least, first.
PROJECTOR_ORDER = ("F16", "BF16", "Q8_0", "F32")
# The first four bytes of every GGUF file.
MAGIC = b"GGUF"
# A build id is a folder name under the model's revision and is stored in
# `local_models.variant` (String 100).
BUILD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
# llama.cpp's own default build ("-hf … default to Q4_K_M").
DEFAULT_BUILD = "Q4_K_M"

SMALL_BUILD = "A very small build: it answers noticeably worse than a 4-bit one."
NOT_A_FOLDER_NAME = "its build name could not be a folder name"
SAME_NAME = "another build has the same name"
UNEVEN_PARTS = "its parts do not add up to one model"

_SUFFIX = ".gguf"
_GB = 1024**3
_MB = 1024**2
# Below this many bits per weight a build is "very small".
_FEW_BITS = 4
_BITS = (
    (re.compile(r"I?Q(\d+)(?:_|$)"), None),
    (re.compile(r"TQ(\d)_"), None),
    (re.compile(r"MXFP4(?:_|$)"), 4),
    (re.compile(r"BF16$"), 16),
    (re.compile(r"F(16|32|64)$"), None),
)


@dataclass(frozen=True)
class Build:
    """One model of a GGUF repository: a file, or the parts of a split one."""

    # "Q4_K_M"; the whole name when it carries no quantisation.
    id: str
    # The model file, or its split parts in order (the first is what is loaded).
    parts: tuple[RepoFile, ...]
    bytes: int


def is_projector(path: str) -> bool:
    """Is this the part that reads pictures, not a model? By its name only."""
    return _is_gguf(path) and PROJECTOR.search(_name(path)) is not None


def build_id(stem: str) -> str | None:
    """The quantisation at the end of a name without `.gguf`, in capitals; None when it has none."""
    found = QUANT.search(stem)
    return found.group(1).upper() if found else None


def builds(files: Sequence[RepoFile]) -> tuple[list[Build], list[tuple[str, str]]]:
    """The model builds among `files`, smallest first, and (path, why) for model files left out.

    Only `.gguf` model files are looked at; projectors and every other file
    are neither builds nor left out here.
    """
    left_out: list[tuple[str, str]] = []
    # (folder, stem, split or not) → {part number: its files}, and how many
    # parts each of its files says there are. A part is a list because two
    # files can claim it (`demo.gguf` and `demo.GGUF`, whose stems agree).
    sets: dict[tuple[str, str, bool], dict[int, list[RepoFile]]] = {}
    counts: dict[tuple[str, str, bool], set[int]] = {}
    for file in files:
        if not _is_gguf(file.path) or is_projector(file.path):
            continue
        stem = _stem(file.path)
        split = SPLIT.match(stem)
        folder = str(PurePosixPath(file.path).parent)
        if split is None:
            key, part, count = (folder, stem, False), 1, 1
        else:
            key = (folder, split.group("stem"), True)
            part, count = int(split.group("part")), int(split.group("count"))
        sets.setdefault(key, {}).setdefault(part, []).append(file)
        counts.setdefault(key, set()).add(count)

    whole: list[tuple[str, tuple[RepoFile, ...]]] = []
    for key, claimed in sets.items():
        if any(len(claims) > 1 for claims in claimed.values()):
            # Two files for one part: keeping either would be choosing by the
            # order of a listing, so neither is a build, and both are shown.
            why = UNEVEN_PARTS if key[2] else SAME_NAME
            left_out.extend((file.path, why) for claims in claimed.values() for file in claims)
            continue
        numbered = {part: claims[0] for part, claims in claimed.items()}
        count, *others = sorted(counts[key])
        missing = sorted(set(range(1, count + 1)) - set(numbered))
        if others or (not missing and len(numbered) != count):
            left_out.extend((file.path, UNEVEN_PARTS) for file in numbered.values())
        elif missing:
            why = f"part {missing[0]} of {count} is missing"
            left_out.extend((file.path, why) for file in numbered.values())
        else:
            whole.append((key[1], tuple(numbered[number] for number in sorted(numbered))))

    # The quantisation names a build, unless two builds would share it: then
    # each goes by its whole name, which tells them apart. Names that still
    # agree (the same name in two folders) name no build at all: the Mac's
    # disk could not keep two folders of that name apart, and keeping the
    # first would be choosing by the order of a listing.
    quants = Counter((build_id(stem) or stem).casefold() for stem, _parts in whole)
    named: list[tuple[str, tuple[RepoFile, ...]]] = []
    for stem, parts in whole:
        quant = build_id(stem) or stem
        named.append((quant if quants[quant.casefold()] == 1 else stem, parts))
    ids = Counter(id_.casefold() for id_, _parts in named)
    found: list[Build] = []
    for id_, parts in named:
        if ids[id_.casefold()] > 1:
            left_out.extend((file.path, SAME_NAME) for file in parts)
        elif not BUILD_ID.fullmatch(id_):
            left_out.extend((file.path, NOT_A_FOLDER_NAME) for file in parts)
        else:
            found.append(Build(id_, parts, sum(file.size for file in parts)))
    found.sort(key=lambda build: (build.bytes, build.id))
    return found, left_out


def projectors(files: Sequence[RepoFile]) -> list[RepoFile]:
    """The parts that read pictures among `files`, by name."""
    return sorted((file for file in files if is_projector(file.path)), key=lambda file: file.path)


def projector_for(build: Build, candidates: Sequence[RepoFile]) -> RepoFile | None:
    """The projector a build is loaded with; None when there is none.

    The one whose name carries the build's id as a whole word (`F16` is not
    found in `BF16`, nor `Q4_K` in `Q4_K_M`); else the first precision of
    `PROJECTOR_ORDER` there is; else the first by name.
    """
    ordered = sorted(candidates, key=lambda file: file.path)
    if not ordered:
        return None
    word = re.compile(rf"(?<![A-Za-z0-9]){re.escape(build.id)}(?![A-Za-z0-9_])", re.IGNORECASE)
    for file in ordered:
        if word.search(_stem(file.path)):
            return file
    for precision in PROJECTOR_ORDER:
        for file in ordered:
            if build_id(_stem(file.path)) == precision:
                return file
    return ordered[0]


def quantisation(build: Build) -> str:
    """The quantisation a build's file names ("Q4_K_M"), whatever the build's id; "" when none."""
    stem = _stem(build.parts[0].path)
    split = SPLIT.match(stem)
    return build_id(split.group("stem") if split else stem) or ""


def nominal_bits(build: str) -> int | None:
    """Bits per weight a build's id names: 2 for "UD-IQ2_M", 4 for "MXFP4_MOE", 16 for "F16".

    A build named by its whole file name because another shares its
    quantisation ("with-vision-Q2_K") is read by the quantisation at its end;
    None when the name carries none ("demo-model"). Nominal: the Hub's own
    table gives Q4_K as 4.5 bits and Q2_K as 2.625; this is the number in
    the name, which is what "under four bits" is judged on.
    """
    text = build.upper().removeprefix("UD-")
    for pattern, fixed in _BITS:
        found = pattern.match(text)
        if found:
            return fixed if fixed is not None else int(found.group(1))
    quant = build_id(build)
    # A whole name's own quantisation; never itself again, so this ends.
    if quant is not None and quant != build.upper():
        return nominal_bits(quant)
    return None


def variants(plan: FilePlan) -> tuple[kinds.Variant, ...]:
    """One variant per build of the plan's GGUF files, smallest first.

    A build's files are its model part(s), its projector and the plan's
    licence and readme texts — nothing else: llama.cpp reads no settings or
    tokenizer file (L6 D9). Its weights are model and projector together,
    which is what it needs in memory (D13). Its label, quantisation, note
    and what it takes are its own.
    """
    found, _left_out = builds(plan.weights)
    pictures = projectors(plan.weights)
    texts = tuple(file for file in plan.keep if file.path in TEXT_NAMES)
    made: list[kinds.Variant] = []
    for build in found:
        projector = projector_for(build, pictures)
        extra = (projector,) if projector is not None else ()
        own = build.parts + extra + texts
        # Read from the file, not the id: two builds of one quantisation go by
        # their whole names, and a 2-bit one must still say it is small.
        quant = quantisation(build)
        bits = nominal_bits(quant) if quant else None
        made.append(
            kinds.Variant(
                id=build.id,
                label=f"{build.id} — {_size_text(sum(file.size for file in own))}",
                files=own,
                weights_bytes=build.bytes + sum(file.size for file in extra),
                quant=quant,
                note=SMALL_BUILD if bits is not None and bits < _FEW_BITS else "",
                takes=("text", "image") if projector is not None else ("text",),
            )
        )
    made.sort(key=lambda variant: (variant.weights_bytes, variant.id))
    return tuple(made)


def prefer(sized: Sequence[tuple[kinds.Variant, kinds.Fit]]) -> str:
    """The id of the build to suggest (`RuntimeKind.prefer`). `sized` is never empty.

    A `Q4_K_M` that fits a budget, by day before overnight; else the largest
    that fits by day; else the largest that fits overnight; else the
    smallest, which the check then says fits no budget.
    """
    if not sized:
        raise ValueError("There is no build to choose from.")
    for budget in ("day", "night"):
        default = [
            variant
            for variant, fits in sized
            if fits == budget and variant.id.upper().endswith(DEFAULT_BUILD)
        ]
        if default:
            return min(default, key=_size).id
    for budget in ("day", "night"):
        fitting = [variant for variant, fits in sized if fits == budget]
        if fitting:
            return max(fitting, key=_size).id
    return min((variant for variant, _fits in sized), key=_size).id


def roles(files: Sequence[RepoFile]) -> tuple[str, str | None]:
    """(the file the program is given, the projector or None) for one build's files.

    The model file, or the first part of a split one. `ValueError` when the
    files hold no model, or more than one.
    """
    found, _left_out = builds(files)
    if len(found) != 1:
        raise ValueError(
            "This copy of the model has no GGUF model file."
            if not found
            else "This copy of the model holds more than one GGUF build."
        )
    build = found[0]
    projector = projector_for(build, projectors(files))
    return build.parts[0].path, projector.path if projector is not None else None


def looks_like_gguf(path: Path) -> bool:
    """Does the file start as every GGUF file does? Reads four bytes.

    A disk that cannot be read raises `OSError` (the store is away, which is
    not the same as a wrong file).
    """
    with path.open("rb") as source:
        return source.read(len(MAGIC)) == MAGIC


def _is_gguf(path: str) -> bool:
    return path.lower().endswith(_SUFFIX)


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _stem(path: str) -> str:
    """The file's name without its folder and without `.gguf`."""
    return _name(path)[: -len(_SUFFIX)]


def _size(variant: kinds.Variant) -> tuple[int, str]:
    return variant.weights_bytes, variant.id


def _size_text(size: int) -> str:
    """As the pages write sizes (files.py, preflight.py): GB with one decimal, MB below that."""
    if size >= _GB:
        return f"{size / _GB:.1f} GB"
    return f"{max(1, round(size / _MB))} MB"
