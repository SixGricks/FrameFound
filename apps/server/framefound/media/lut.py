"""A look as a `.cube` file: the colour engine, written down as a table.

A LUT (look-up table) is how a look leaves FrameFound for an editing
program: a grid of colours, and for each one the colour it should become.
The table made here is not a second version of the look kept in step with
the first. It is `develop.grade` - the engine itself - run on every point
of the grid. At those points the file says exactly what the engine renders;
between them whoever reads the file fills in.

What that does and does not promise, in plain words:

- **One colour in, one colour out.** A LUT carries exposure, contrast,
  temperature, tint, saturation, vibrance, and darkening the shadows. It
  cannot carry anything that looks at the pixels round a pixel, or moves
  pixels: lifting the shadows, pulling back the highlights, window pull,
  local contrast, straightening, correcting verticals, a new sky.
  `lut_recipe` is the one place a recipe is narrowed, and it names what it
  left out. Nothing is dropped quietly, and `build` refuses a recipe that
  still holds any of it.
- **Measured once, on one still.** Auto white balance and auto-levels read a
  whole picture, so a LUT carries them only as the numbers read off one
  still (`develop.measure`). That is the engine's own correction for that
  still, and it holds for the clip while its light stays the same. Where
  the light changes the LUT does not follow it; the engine measuring each
  frame afresh would.
- **Exact at the grid's points, close between them.** 33 points a side is
  the size every program reads; 65 is offered for a finer table. Read back
  onto pictures and compared with the engine, in 8-bit levels (of 255),
  average and very worst pixel: an ordinary look 0.1 and 3 at 33 points,
  0.02 and 2 at 65; a strong look 0.2 and 8 at 33, 0.05 and 4 at 65; every
  slider at its stop 0.6 and 34 at 33. The difference sits where a look
  pushes a colour past white or black, because the engine's cut-off then
  falls between two points, so it grows with how hard a look clips.
- **Six decimals.** Each value is written to within half a millionth, a
  four-thousandth of an 8-bit level. So even on a point of the grid, a
  value the engine left on the very edge between two 8-bit levels can read
  back one level away: fewer than one value in ten thousand.
- **Never outside 0 to 1.** The engine clips its output and so does the
  table, so every reader agrees about it.
- **Not colour management.** The table maps the code values of ordinary
  (Rec. 709) video, as the clip looks in FrameFound's preview, to code
  values of the same kind. The engine never converts to linear light and
  the format names no colour space. For log, HLG or HDR footage that the
  editing program converts or tone-maps before the LUT, the look lands
  differently, and nothing here can detect that - so the file says what it
  was made for (`ASSUMES`).
- **Not yet seen in Premiere.** What is tested is that the file keeps the
  rules of Adobe's Cube LUT Specification 1.0, and that a reader which keeps
  them too (`parse_cube` and `apply`, with the tetrahedral interpolation
  the specification asks for) gives the engine's picture. Which
  interpolation Premiere uses, and that a frame there looks like the still
  here, is unproven until it is tried.

A LUT is never stored. The sliders, the measurements and the size settle
every byte of it, so it is built when somebody asks.
"""

import json
import re
import textwrap
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from PIL import Image

from framefound.media import develop

# 33 is what every program reads (about 1 MB of text); 65 is about 7 MB. 17
# is not offered: measured, it is three to four times further from the engine.
SIZES: tuple[int, ...] = (33, 65)
DEFAULT_SIZE = 33
DECIMALS = 6
# The specification's limit on a line, and on how many points a side a
# three-dimensional table may have.
MAX_LINE_BYTES = 250
MIN_POINTS, MAX_POINTS = 2, 256

# What a LUT cannot carry, in the words the page and the file use. The order
# is the order they are listed in.
LEFT_OUT_WORDS: dict[str, str] = {
    "shadows": "lifting the shadows",
    "highlights": "pulling back the highlights",
    "window_pull": "window pull",
    "local_contrast": "local contrast",
    "rotate": "straightening",
    "keystone": "correcting verticals",
    "sky": "sky replacement",
}
_ASSUMES_LINES = (
    "Made for ordinary (Rec. 709) video as it looks in FrameFound's preview.",
    "Not for log or HDR footage unless Premiere shows it the same way.",
)
ASSUMES = " ".join(_ASSUMES_LINES)

# Carried whatever their value. `shadows` is carried only when negative, and
# a new slider is left out until somebody adds it here: the engine refuses a
# lattice anything it knows to be spatial, but it cannot know about a step
# that has not been written yet.
_CARRIED = frozenset(develop.POINTWISE_KEYS + develop.MEASURED_KEYS)

# Pixels read through a table at a time, so a full-size frame costs a few
# megabytes of working room rather than a few hundred.
_CHUNK = 1 << 18

# Punctuation people type that has a plain equivalent; anything else outside
# the file's character set becomes "?".
_PLAIN = str.maketrans(
    {
        "\N{EM DASH}": "-",
        "\N{EN DASH}": "-",
        "\N{LEFT SINGLE QUOTATION MARK}": "'",
        "\N{RIGHT SINGLE QUOTATION MARK}": "'",
        "\N{LEFT DOUBLE QUOTATION MARK}": '"',
        "\N{RIGHT DOUBLE QUOTATION MARK}": '"',
        "\N{HORIZONTAL ELLIPSIS}": "...",
        "\N{MULTIPLICATION SIGN}": "x",
    }
)
_NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
_ROW = re.compile(rf"[ \t]*({_NUMBER})[ \t]+({_NUMBER})[ \t]+({_NUMBER})[ \t]*")
_UNSAFE_IN_A_NAME = re.compile(r"[^A-Za-z0-9._ ()+-]+")
_KEYWORDS = frozenset({"TITLE", "DOMAIN_MIN", "DOMAIN_MAX", "LUT_3D_SIZE", "LUT_1D_SIZE"})
# "No number shall exceed the range +-1e+37".
_LARGEST_NUMBER = 1e37
# Anything a line of the file may not hold (the newline is what parts lines).
_NOT_TEXT = re.compile(r"[^\x20-\x7e\t\n]")


def lut_recipe(recipe: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """(what a LUT carries, the keys left out) - the ONE place a recipe is
    narrowed.

    The recipe is cleaned first. Carried: the per-pixel sliders, `shadows`
    when negative (darkening is per-pixel; lifting reads the room), and
    `auto` / `auto_wb`, which `build` then wants a measurement for. The keys
    left out come back in the order of `LEFT_OUT_WORDS`, for the caller to
    show: a look that quietly lost its shadow lift is a darker picture than
    the one the operator was promised.
    """
    carried: dict[str, Any] = {}
    left_out: list[str] = []
    for key, value in develop.clean_recipe(dict(recipe)).items():
        if key in _CARRIED or (key == "shadows" and value < 0):
            carried[key] = value
        else:
            left_out.append(key)
    listed = list(LEFT_OUT_WORDS)
    left_out.sort(key=lambda key: listed.index(key) if key in listed else len(listed))
    return carried, left_out


def left_out_words(keys: Sequence[str]) -> list[str]:
    """The keys `lut_recipe` left out, as words ready to print."""
    return [LEFT_OUT_WORDS.get(key, key.replace("_", " ")) for key in keys]


def identity(size: int = DEFAULT_SIZE) -> Any:
    """Every point of the grid as its own colour: an (n, n, n, 3) float32
    lattice indexed [blue][green][red], so `reshape(-1, 3)` walks red
    fastest - the file's order.

    Each coordinate is k / (n - 1) rounded once to float32, which is the very
    number the engine makes of an 8-bit code that sits on the same point.
    """
    if isinstance(size, bool) or not isinstance(size, int) or not MIN_POINTS <= size <= MAX_POINTS:
        raise ValueError(f"a table has {MIN_POINTS} to {MAX_POINTS} points a side, not {size!r}")

    import numpy as np

    axis = np.arange(size, dtype=np.float32) / np.float32(size - 1)
    blue, green, red = np.meshgrid(axis, axis, axis, indexing="ij")
    return np.stack([red, green, blue], axis=-1)


def build(
    recipe: Mapping[str, Any],
    *,
    size: int = DEFAULT_SIZE,
    measured: Mapping[str, Any] | None = None,
) -> Any:
    """The table for a look: the engine's own answer at every point of the
    grid. (n, n, n, 3) float32 in 0..1, indexed [blue][green][red].

    ValueError for a size that is not offered, for a recipe `lut_recipe`
    would narrow (narrow it first, and tell the operator what went), and -
    from the engine - for `auto` or `auto_wb` with no reading in `measured`,
    or a reading of the wrong shape. An empty recipe builds the identity.
    """
    if isinstance(size, bool) or not isinstance(size, int) or size not in SIZES:
        offered = " or ".join(str(n) for n in SIZES)
        raise ValueError(f"a LUT has {offered} points a side, not {size!r}")
    carried, left_out = lut_recipe(recipe)
    if left_out:
        raise ValueError(
            f"a LUT cannot carry {', '.join(left_out)}: narrow the recipe with lut_recipe first"
        )
    return develop.grade(
        identity(size), carried, measured=dict(measured) if measured else None, pointwise=True
    )


def apply(image: Image.Image, table: Any) -> Image.Image:
    """Read a picture back through a table: tetrahedral interpolation, clip,
    8 bits. This is what the operator is shown, so that what they judge is
    what the file will do.

    Each little cube of the grid is cut into six tetrahedra along its grey
    diagonal and a colour is mixed from the four corners of the one it falls
    in - what the specification says a reader should do. A pixel that sits
    on a point of the grid gets that point's value untouched, which is what
    makes "exactly the engine" checkable on a picture.
    """
    import numpy as np

    points, n = _points(table, np)
    codes = np.asarray(image.convert("RGB")).reshape(-1, 3)

    # Where each 8-bit code falls on an axis: the cell below it and how far
    # into that cell, in whole numbers so a code on a point is on it exactly.
    # White belongs to the last cell, all the way in.
    scaled = np.arange(256, dtype=np.int64) * (n - 1)
    cell_of = np.minimum(scaled // 255, n - 2)
    part_of = ((scaled - cell_of * 255) / 255.0).astype(np.float32)
    # A step along red, green, blue in the flat table (red fastest).
    step = np.asarray((1, n, n * n), dtype=np.int64)

    out = np.empty(codes.shape, dtype=np.uint8)
    for start in range(0, len(codes), _CHUNK):
        chunk = codes[start : start + _CHUNK]
        part = part_of[chunk]
        corner = cell_of[chunk] @ step
        # Walk from the cell's dark corner to its bright one along the axis
        # the colour is furthest along first: those four corners are the
        # tetrahedron it is in.
        order = np.argsort(-part, axis=-1, kind="stable")
        ranked = np.take_along_axis(part, order, axis=-1)
        most, middle, least = ranked[:, 0:1], ranked[:, 1:2], ranked[:, 2:3]
        first = corner + step[order[:, 0]]
        second = first + step[order[:, 1]]
        mixed = (
            (1.0 - most) * points[corner]
            + (most - middle) * points[first]
            + (middle - least) * points[second]
            + least * points[corner + step.sum()]
        )
        out[start : start + len(chunk)] = (np.clip(mixed, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    return Image.fromarray(out.reshape(image.height, image.width, 3))


def to_cube(table: Any, *, title: str, comments: Sequence[str] = ()) -> str:
    """The file's text: TITLE, the comments, DOMAIN_MIN, DOMAIN_MAX,
    LUT_3D_SIZE, then the table with red changing fastest (the order of the
    specification's own sample writer).

    The rules that fail quietly are kept here rather than left to callers:
    lines end with "\\n" only (a carriage return makes it "not a valid cube
    file"), every character is plain ASCII 0x20-0x7E (an accent in a clip's
    name is folded, anything else becomes "?"), and no line is longer than
    250 bytes (a long comment is wrapped). Send it as `.encode("ascii")`;
    never write it through a text-mode file, which on Windows puts the
    carriage returns back.
    """
    import numpy as np

    points, n = _points(table, np)
    lines: list[str] = []
    name = _ascii(title).replace('"', "'").strip()[: MAX_LINE_BYTES - len('TITLE ""')].rstrip()
    if name:
        lines.append(f'TITLE "{name}"')
    for comment in comments:
        lines.extend(_comment_lines(comment))
    lines += ["DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0", f"LUT_3D_SIZE {n}"]
    row = " ".join([f"%.{DECIMALS}f"] * 3)
    # Adding zero turns the minus-zero a clip can leave into zero:
    # "-0.000000" is a legal number and an alarming thing to find in a file.
    lines.extend(row % tuple(point) for point in (points + 0.0).tolist())
    return "\n".join(lines) + "\n"


def parse_cube(text: str | bytes) -> Any:
    """The table back from a file's text, (n, n, n, 3) float32 indexed
    [blue][green][red]. For tests and the round-trip check: it stands in for
    "a reader that follows the specification", so it is strict where the
    specification is, and ValueError says which rule a file broke.

    Only what FrameFound writes is read: one three-dimensional table over
    the domain 0 to 1.
    """
    import numpy as np

    if isinstance(text, bytes):
        try:
            text = text.decode("ascii")
        except UnicodeDecodeError as err:
            raise ValueError("a .cube file is plain ASCII text") from err
    if "\r" in text:
        raise ValueError("carriage returns: lines of a .cube file end with a newline only")
    if stray := _NOT_TEXT.search(text):
        number = text.count("\n", 0, stray.start()) + 1
        raise ValueError(f"line {number}: a character that is not plain ASCII text")

    seen: set[str] = set()
    size: int | None = None
    rows: list[tuple[str, ...]] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if len(line) > MAX_LINE_BYTES:
            raise ValueError(f"line {number}: longer than {MAX_LINE_BYTES} bytes")
        if line.startswith("#") or not line.strip():
            continue
        if found := _ROW.fullmatch(line):
            rows.append(found.groups())
            continue
        keyword, *after = line.split(None, 1)
        rest = after[0] if after else ""
        if keyword[0] in "+-.0123456789" or (rows and keyword not in _KEYWORDS):
            raise ValueError(f"line {number}: a line of the table is three numbers")
        if rows:
            raise ValueError(f"line {number}: {keyword} comes after the table has begun")
        if keyword in seen:
            raise ValueError(f"line {number}: {keyword} appears twice")
        seen.add(keyword)
        if keyword == "TITLE":
            quoted = rest.strip()
            if len(quoted) < 2 or quoted[0] != '"' or quoted[-1] != '"' or '"' in quoted[1:-1]:
                raise ValueError(f"line {number}: a title is one piece of text in double quotes")
        elif keyword in ("DOMAIN_MIN", "DOMAIN_MAX"):
            bound = 0.0 if keyword == "DOMAIN_MIN" else 1.0
            edges = _ROW.fullmatch(rest)
            if edges is None or [float(edge) for edge in edges.groups()] != [bound] * 3:
                raise ValueError(f"line {number}: only the domain 0 to 1 is read here")
        elif keyword == "LUT_3D_SIZE":
            if not rest.strip().isdecimal() or not MIN_POINTS <= int(rest) <= MAX_POINTS:
                raise ValueError(
                    f"line {number}: a table has {MIN_POINTS} to {MAX_POINTS} points a side"
                )
            size = int(rest)
        elif keyword == "LUT_1D_SIZE":
            raise ValueError(f"line {number}: one-dimensional tables are not read here")
        else:
            raise ValueError(f"line {number}: {keyword} is not a keyword of the format")

    if size is None:
        raise ValueError("no LUT_3D_SIZE: the file does not say how big its table is")
    if len(rows) != size**3:
        raise ValueError(f"the table has {len(rows)} lines; {size} points a side needs {size**3}")
    values = np.asarray(rows, dtype=np.float64)
    if not (np.abs(values) <= _LARGEST_NUMBER).all():
        raise ValueError("a number in the table is too large: the format stops at 1e37")
    return values.astype(np.float32).reshape(size, size, size, 3)


def comment_block(
    *,
    clip: str,
    look: str,
    size: int,
    recipe: Mapping[str, Any],
    measured: Mapping[str, Any] | None = None,
    measured_at_ms: int | None = None,
    left_out: Sequence[str] = (),
    provenance: Mapping[str, Any] | None = None,
    demo: bool = False,
) -> list[str]:
    """The comments every file carries, one string a line, for `to_cube`.

    Somebody opening the file a year on should be able to read what it is,
    what it assumes and how to make it again: the clip, which look (`look`
    is the caller's words, "the local model's suggestion"), the assumptions,
    the sliders as JSON, where the white balance was measured and what it
    came to, what a LUT could not carry (`left_out` is keys), and the model
    when one suggested it. A made-up look says so on the first line.

    No line here grades the look: sliders and gains are what it is, not how
    good it is.
    """
    lines = []
    if demo:
        lines.append("DEMONSTRATION - these sliders were made up, not suggested by a model")
    lines.append(f"FrameFound look for {clip} - {look}")
    lines.append(
        f"The table is FrameFound's colour engine at each of {size} x {size} x {size} points."
    )
    lines.extend(_ASSUMES_LINES)
    lines.append("Values 0 to 1 in, 0 to 1 out.")
    lines.append(f"Sliders: {json.dumps(dict(recipe), sort_keys=True)}")

    readings = dict(measured or {})
    where = "a still" if measured_at_ms is None else f"the still at {_moment(measured_at_ms)}"
    if recipe.get("auto") and "levels" in readings:
        spans = ", ".join(
            f"{channel} {low:.{DECIMALS}f} to {high:.{DECIMALS}f}"
            for channel, (low, high) in zip(
                ("red", "green", "blue"), readings["levels"], strict=True
            )
        )
        lines.append(f"Levels measured on {where} - {spans}")
    if recipe.get("auto_wb") and "wb_gains" in readings:
        gains = " ".join(f"{gain:.{DECIMALS}f}" for gain in readings["wb_gains"])
        lines.append(f"White balance measured on {where} - gains {gains}")

    lines.append(
        f"A LUT cannot carry, so left out: {', '.join(left_out_words(left_out)) or '(nothing)'}"
    )
    if made_by := _made_by(provenance or {}):
        lines.append(f"Model: {made_by}")
    return lines


def clip_stem(clip_filename: str) -> str:
    """A clip's name without its folder or extension, safe as part of a file
    name anywhere: ASCII, no path characters. "clip" when no letter or digit
    is left of it (a name in another script), so two clips can come out
    alike: whoever puts several in one folder or zip must tell them apart."""
    stem = PurePosixPath(clip_filename.replace("\\", "/")).stem
    safe = _name_part(stem)[:100].strip(" .")
    return safe if any(char.isalnum() for char in safe) else "clip"


def cube_filename(clip_filename: str, source: str) -> str:
    """ "DJI_0639 - FrameFound model.cube" - ASCII, no path characters, so it
    is safe in a download header and in a zip whatever the clip was called."""
    return f"{clip_stem(clip_filename)} - FrameFound {_name_part(source)[:40] or 'look'}.cube"


def _points(table: Any, np: Any) -> tuple[Any, int]:
    """A table as (n^3, 3) float32 with red changing fastest, and n.
    ValueError for anything that is not a cube of colours."""
    arr = np.asarray(table, dtype=np.float32)
    if (
        arr.ndim != 4
        or arr.shape[3] != 3
        or len(set(arr.shape[:3])) != 1
        or not MIN_POINTS <= arr.shape[0] <= MAX_POINTS
    ):
        raise ValueError("a table is n x n x n colours, indexed [blue][green][red]")
    if not np.isfinite(arr).all():
        raise ValueError("a table holds colours; this one holds something that is not a number")
    return np.ascontiguousarray(arr).reshape(-1, 3), int(arr.shape[0])


def _ascii(text: str) -> str:
    """Text as the file may hold it: characters 0x20 to 0x7E and nothing
    else. Accents are folded to their letters, line breaks and tabs become
    spaces (a comment must not be able to start a new line of the file), and
    what has no plain form becomes "?"."""
    out = []
    for char in unicodedata.normalize("NFKD", text.translate(_PLAIN)):
        if unicodedata.combining(char):
            continue
        if " " <= char <= "~":
            out.append(char)
        else:
            out.append(" " if char.isspace() else "?")
    return "".join(out)


def _comment_lines(text: str) -> list[str]:
    """One comment as lines of the file, wrapped inside the line limit."""
    wrapped = textwrap.wrap(
        _ascii(text), width=MAX_LINE_BYTES - len("# "), break_on_hyphens=False
    ) or [""]
    return [f"# {line}".rstrip() for line in wrapped]


def _name_part(text: str) -> str:
    cleaned = _UNSAFE_IN_A_NAME.sub("_", _ascii(text))
    return " ".join(cleaned.split()).strip(" .")


def _moment(ms: int) -> str:
    """7000 -> "0:07.000"; an hour in, "1:02:03.456"."""
    seconds, millis = divmod(max(0, int(ms)), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    return f"{minutes}:{seconds:02d}.{millis:03d}"


def _made_by(provenance: Mapping[str, Any]) -> str:
    """ "<model> @ <revision> - <runtime> <version> - prompt <version>", with
    whatever of it is known."""
    parts = []
    if model := provenance.get("model"):
        revision = str(provenance.get("model_revision") or "")[:8]
        parts.append(f"{model} @ {revision}" if revision else str(model))
    runtime = " ".join(
        str(provenance[key]) for key in ("runtime", "runtime_version") if provenance.get(key)
    )
    if runtime:
        parts.append(runtime)
    if prompt := provenance.get("prompt_version"):
        parts.append(f"prompt {prompt}")
    return " - ".join(parts)
