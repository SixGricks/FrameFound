"""The Editor's question: which shots a local model is offered, what it is
told, the form its answer takes, and what FrameFound does to that answer
before any of it reaches a timeline.

Pure: no database, no clock, no model. The job that asks (nle/drafts.py)
reads the shots, calls the model and writes the cut; everything it decides
on the way is decided here, so it can be tested without any of those.

**What the Editor reads is what Claude reads.** Each shot is offered as a
numbered line whose tail is `find_shots`' own description of the shot
(`shotlist.window_line`, then L3's `score NN` word). The number and the
view go in front of that description, never into it, so `parse_shot` and
the twenty-lines-in-2-KB budget of the connector are untouched.

**The Editor answers with numbers.** An eight-character hex id is easy for
a small model to miscopy and impossible to check; a line number is looked
up in a map. Nothing a model writes is used as an id, a path or a name:
shot numbers are looked up, seconds are clamped inside the shot named,
reasons are cut short and lose any character a timeline file or a database
cannot hold.

**FrameFound owns the arithmetic.** The model is told how many cuts to make
and how long each may be; it is not trusted to add up. `check` repairs or
drops whatever does not fit and says so in one sentence per change, and a
draft that ends short is left short: FrameFound never adds a shot the model
did not choose.

**Change anything a model is sent or held to — `INSTRUCTIONS`, `prompt`,
`Candidate.editor_line`, `EditorAnswer` — and `PROMPT_VERSION` moves with
it.** That includes the connector's shot line this module borrows
(`shotlist.window_line`, `shotlist.score_word`): a change made there for
`find_shots` changes what the Editor is asked. tests/test_cut_editor.py
pins the text (its shot lines built from real windows) and the schema.
Every number here (eighty lines, three per clip, the ranking's weights) is
a starting point to be measured on real drafts, not a known-good value.

This module imports nothing but the standard library, pydantic,
`nle.shotlist` and the models' types: the API, the job and the chat tool
all reach it, on installs with and without the connector's `mcp` extra.
"""

import json
import math
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from pydantic import BaseModel

from framefound.db.models import Asset, Look, ShotWindow
from framefound.nle.shotlist import (
    Shot,
    ShotError,
    clip_name,
    normalise_light,
    normalise_move,
    score_word,
    short_id,
    smooth_at,
    window_line,
)

PROMPT_VERSION = "editor-1"
# Shots offered: about 8 KB of text. A golf-course folder has hundreds of
# windows; a small model given all of them reads the first screenful.
MAX_LINES = 80
# At most this many windows of one clip, so one long flight cannot fill
# the list by itself.
PER_CLIP = 3
MAX_DRAFT_CUTS = 60
# A draft within 10% of the length asked for is the length asked for.
TOLERANCE = 0.10
# Two cuts of one clip that share more than this are the same shot twice.
REPEAT_OVERLAP_S = 0.25
# The XML writer silently skips a piece of no frames; a piece shorter than
# this never leaves `check`, so the record and the timeline agree.
MIN_PIECE_S = 0.5
REASON_CHARS = 200
# What no XML 1.0 file can hold (control characters, lone surrogates), and
# DEL. A reason becomes a marker in the Premiere file and a row of cut
# history: a NUL a model wrote as an escape, which is valid JSON, would make
# a file Premiere and Resolve refuse and a row PostgreSQL refuses.
_UNWRITABLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]")

# The bounds a request may ask for (the API holds the form to the same).
TARGET_RANGE = (5.0, 600.0)
VIEWS = ("aerial", "ground")


# --- what is asked for ---------------------------------------------------------


@dataclass(frozen=True)
class Constraints:
    """The length asked for and how long one shot may be, in seconds."""

    target_s: float
    min_shot_s: float = 1.5
    max_shot_s: float = 8.0

    def __post_init__(self) -> None:
        values = (self.target_s, self.min_shot_s, self.max_shot_s)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("lengths must be numbers of seconds")
        low, high = TARGET_RANGE
        if not low <= self.target_s <= high:
            raise ValueError(f"target_s is {low:g} to {high:g} seconds")
        if not 0 < self.min_shot_s <= self.max_shot_s:
            raise ValueError("the shortest shot must be above 0 and no longer than the longest")

    @property
    def about(self) -> int:
        """How many cuts to ask for: the length over the middle shot length,
        rounded half up, 1 to `MAX_DRAFT_CUTS`."""
        mean = (self.min_shot_s + self.max_shot_s) / 2
        return max(1, min(MAX_DRAFT_CUTS, math.floor(self.target_s / mean + 0.5)))


# --- what the brief's own words prefer -----------------------------------------


@dataclass(frozen=True)
class Hints:
    """What the brief's words prefer. Never a filter: the brief also goes to
    the model word for word, and a shot nobody hinted at is still offered
    when there is room."""

    moves: frozenset[str] = frozenset()
    light: frozenset[str] = frozenset()
    skies: frozenset[str] = frozenset()
    views: frozenset[str] = frozenset()
    # The brief's other words, matched against a shot's subjects.
    words: frozenset[str] = frozenset()
    steady: bool = False
    # "30 seconds", "0:45", "a minute", "90s", held to 5-600 s.
    target_s: float | None = None


_TOKEN = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")
# Single words too ambiguous to read as a move or a light by themselves
# ("low energy", "a blue sky", "still images"); as part of a phrase
# ("low sun", "blue hour") they still count.
_AMBIGUOUS = frozenset({"low", "mid", "high", "blue", "still", "direct", "forward"})
_VIEW_WORDS = {
    "aerial": "aerial",
    "aerials": "aerial",
    "drone": "aerial",
    "drones": "aerial",
    "overhead": "aerial",
    "from above": "aerial",
    "ground": "ground",
    "handheld": "ground",
    "hand-held": "ground",
    "eye-level": "ground",
    "eye level": "ground",
}
_STEADY_WORDS = frozenset(
    {"smooth", "smoothest", "steady", "stable", "calm", "gentle", "slow", "floating", "silky"}
)
_STOP_WORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "into", "onto", "over", "under", "about", "around",
        "that", "this", "these", "those", "some", "any", "all", "most", "more", "less", "than",
        "then", "them", "they", "their", "its", "our", "your", "you", "use", "using", "make",
        "want", "need", "like", "just", "only", "also", "very", "good", "best", "nice", "lots",
        "shot", "shots", "cut", "cuts", "clip", "clips", "video", "edit", "film", "footage",
        "open", "opening", "close", "closing", "end", "ending", "start", "starting", "on",
        "second", "seconds", "sec", "secs", "minute", "minutes", "min", "mins", "long",
        "short", "length", "each", "per", "but", "not", "few", "half", "please",
    }
)  # fmt: skip

_NUMBER_WORDS = {
    "half a": 0.5, "half an": 0.5, "a": 1, "an": 1, "one": 1, "two": 2, "three": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50,
    "sixty": 60, "ninety": 90,
}  # fmt: skip
_NUMBER = r"(\d+(?:\.\d+)?|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + ")"
_DURATION = re.compile(
    r"\b" + _NUMBER + r"\s*-?\s*(seconds?|secs?|s|minutes?|mins?)\b(\s+and\s+a\s+half)?"
)
_CLOCK = re.compile(r"\b(\d{1,2}):([0-5]\d)\b(?!\s*(?:am|pm|a\.m|p\.m))")
# A length said of one shot, not of the whole cut: "shots of 2 to 4 s",
# "each about 3 s", "no shot over 5 seconds". Not "cut" or "clip" alone:
# "cut it to 30 seconds" is the length of the cut.
_PER_SHOT = re.compile(r"\b(shots?|cuts|clips|each|per|every)\b")
_CLAUSE = re.compile(r"[,;.:!?\n]")


def _durations(text: str) -> list[float]:
    """Every length the brief names for the whole cut, in seconds."""
    text = " ".join(text.split())
    found: list[tuple[int, float]] = []
    for m in _DURATION.finditer(text):
        number, unit, and_half = m.groups()
        spelt = not number[0].isdigit()
        if spelt and (unit == "s" or (_NUMBER_WORDS[number] < 2 and unit[0] == "s")):
            continue  # "a s" is not a length, and "a second pass" is not one second
        value = float(_NUMBER_WORDS[number]) if spelt else float(number)
        if and_half:
            value += 0.5
        found.append((m.start(), value * 60 if unit.startswith("m") else value))
    for m in _CLOCK.finditer(text):
        found.append((m.start(), float(int(m.group(1)) * 60 + int(m.group(2)))))
    lengths = []
    for at, value in found:
        clause = _CLAUSE.split(text[:at])[-1]
        if _PER_SHOT.search(" ".join(clause.split()[-4:])):
            continue
        lengths.append(value)
    return lengths


def _plurals(token: str) -> set[str]:
    """The word, and what it is the plural or possessive of: "greens" and
    "clubhouse's" name the subjects "green" and "clubhouse"."""
    token = token.removesuffix("'s")
    out = {token}
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        out.add(token[:-1])
    return out


def read_brief(brief: str) -> Hints:
    """What the brief's words prefer: moves, light, sky, view, steadiness,
    a length, and the remaining words for matching subjects.

    A brief that names several lengths ("thirty seconds, nothing over four
    seconds") means the largest; a length said of one shot ("shots of 2 to
    4 s") is not the length of the cut.
    """
    text = brief.lower()
    tokens = _TOKEN.findall(text)
    taken = [False] * len(tokens)
    moves: set[str] = set()
    light: set[str] = set()
    skies: set[str] = set()
    views: set[str] = set()
    steady = False
    for n in (3, 2, 1):
        for i in range(len(tokens) - n + 1):
            if any(taken[i : i + n]):
                continue
            phrase = " ".join(tokens[i : i + n])
            if n == 1 and phrase in _AMBIGUOUS:
                continue
            if phrase in _VIEW_WORDS:
                views.add(_VIEW_WORDS[phrase])
            elif n == 1 and phrase in _STEADY_WORDS:
                steady = True
            elif (move := _read(normalise_move, phrase)) is not None:
                moves.add(move)
            elif (said := _read(normalise_light, phrase)) is not None:
                (skies if said[0] == "sky" else light).add(said[1])
            else:
                continue
            taken[i : i + n] = [True] * n
    words: set[str] = set()
    for token, used in zip(tokens, taken, strict=True):
        if used or len(token) < 3 or token[0].isdigit():
            continue
        if token not in _STOP_WORDS and token not in _NUMBER_WORDS:
            words |= _plurals(token)
    lengths = _durations(text)
    target = None
    if lengths:
        low, high = TARGET_RANGE
        target = min(high, max(low, max(lengths)))
    return Hints(
        moves=frozenset(moves),
        light=frozenset(light),
        skies=frozenset(skies),
        views=frozenset(views),
        words=frozenset(words),
        steady=steady,
        target_s=target,
    )


def _read[T](reader: Callable[[str], T], phrase: str) -> T | None:
    try:
        return reader(phrase)
    except ShotError:
        return None


# --- which shots are offered ---------------------------------------------------


def _view(window: ShotWindow) -> str:
    view = (window.framing or {}).get("view")
    return view if view in VIEWS else ""


def _named(subject: str, words: frozenset[str]) -> bool:
    parts = _TOKEN.findall(subject.lower())
    return bool(parts) and all(part in words for part in parts)


def rank(shot: Shot, hints: Hints, score: float | None = None) -> float:
    """How much the brief wants this shot: its smoothness at real time
    (0-1), plus 0.5 for each of a hinted move, a hinted light or sky, a
    hinted view and a subject the brief names; plus a quarter of the
    smoothness again when the brief asks for steady footage; less 0.3 when
    an earlier cut used it; plus up to 0.5 for a local model's 0-100 score
    (L3). Preferences only: every shot gets a rank."""
    window, _asset, used = shot
    smooth = min(1.0, max(0.0, smooth_at(window, 100)))
    value = smooth
    if window.move in hints.moves:
        value += 0.5
    if window.light in hints.light or (window.look or {}).get("sky") in hints.skies:
        value += 0.5
    if _view(window) in hints.views:
        value += 0.5
    if hints.words and any(_named(s, hints.words) for s in (window.subjects or {})):
        value += 0.5
    if hints.steady:
        value += 0.25 * smooth
    if used:
        value -= 0.3
    if score is not None:
        value += 0.5 * min(100.0, max(0.0, float(score))) / 100
    return value


@dataclass(frozen=True)
class Candidate:
    """One shot the Editor is offered, by clip and span (a `ShotWindow` id
    does not survive a re-analysis, so it is never kept)."""

    n: int  # the number the Editor sees, from 1
    asset_id: uuid.UUID
    filename: str
    start_s: float
    end_s: float
    view: str  # "aerial" | "ground" | ""
    # `window_line` at real time with the used flag, then L3's score word:
    # what `find_shots` prints after the clip's id and name.
    line: str

    def editor_line(self) -> str:
        """The number and the view go before `find_shots`' description of the
        shot, never into it."""
        head = [str(self.n), clip_name(self.filename)]
        if self.view:
            head.append(self.view)
        return " ".join([*head, self.line])

    def claude_line(self) -> str:
        """The shot as `find_shots` prints it (without the local path): the id
        that starts it is accepted by every connector tool."""
        return f"{short_id(self.asset_id)} {clip_name(self.filename)} {self.line}"

    def as_json(self) -> dict[str, Any]:
        """The form kept on a draft (`cut_drafts.candidates`)."""
        return {
            "n": self.n,
            "asset_id": str(self.asset_id),
            "filename": self.filename,
            "start_ms": round(self.start_s * 1000),
            "end_ms": round(self.end_s * 1000),
            "view": self.view,
            "line": self.line,
        }

    @classmethod
    def from_json(cls, row: Mapping[str, Any]) -> "Candidate":
        return cls(
            n=int(row["n"]),
            asset_id=uuid.UUID(str(row["asset_id"])),
            filename=str(row["filename"]),
            start_s=int(row["start_ms"]) / 1000,
            end_s=int(row["end_ms"]) / 1000,
            view=str(row.get("view") or ""),
            line=str(row["line"]),
        )


def _shot_order(shot: Shot) -> tuple[Any, ...]:
    window, asset, _ = shot
    when = asset.captured_at
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    stamp = when.timestamp() if when is not None else math.inf
    return (stamp, asset.filename, window.start_ms, window.end_ms)


def _no_look(_window: ShotWindow, _asset: Asset) -> Look | None:
    return None


def choose(
    shots: Sequence[Shot],
    hints: Hints,
    c: Constraints,
    *,
    limit: int = MAX_LINES,
    per_clip: int = PER_CLIP,
    look_of: Callable[[ShotWindow, Asset], Look | None] = _no_look,
) -> list[Candidate]:
    """The shots the Editor is offered: windows at least `c.min_shot_s` long,
    ranked (`rank`), the best `limit` kept with at most `per_clip` of one
    clip, then numbered from 1 in shooting order (when the clip was shot,
    its name, the start of the shot).

    `look_of` is `ShotScan.look_of`: a local model's opinion of a shot adds
    to its rank and ends its line, as in `find_shots`. Always at real time:
    the Editor is never shown a speed (P9).
    """
    kept: list[tuple[Shot, Look | None]] = []
    ranked: list[tuple[float, tuple[Any, ...], Shot, Look | None]] = []
    for shot in shots:
        window, asset, _ = shot
        if (window.end_ms - window.start_ms) / 1000 < c.min_shot_s:
            continue
        look = look_of(window, asset)
        score = look.score if look is not None else None
        ranked.append((-rank(shot, hints, score), _shot_order(shot), shot, look))
    ranked.sort(key=lambda item: (item[0], item[1]))
    per: dict[uuid.UUID, int] = {}
    for _, _, shot, look in ranked:
        if len(kept) >= limit:
            break
        asset_id = shot[1].id
        if per.get(asset_id, 0) >= per_clip:
            continue
        per[asset_id] = per.get(asset_id, 0) + 1
        kept.append((shot, look))
    kept.sort(key=lambda item: _shot_order(item[0]))
    return [
        Candidate(
            n=n,
            asset_id=asset.id,
            filename=asset.filename,
            start_s=window.start_ms / 1000,
            end_s=window.end_ms / 1000,
            view=_view(window),
            line=window_line(window, 100, used=bool(used)) + score_word(look),
        )
        for n, ((window, asset, used), look) in enumerate(kept, start=1)
    ]


# --- what the Editor is asked, and the form of its answer ----------------------


# The two classes below carry comments, not docstrings, on purpose: pydantic
# copies a docstring into the JSON schema, and the schema goes to the
# runtime with every question.
#
# Loose on purpose: no bounds, no lengths. A reason of 210 characters or a
# shot number that is not on the list is repaired by `check` and reported,
# not thrown back at the model as "not the JSON asked for".


# One cut on the timeline: a numbered shot and the seconds of it to use.
class EditorCut(BaseModel):
    shot: int
    in_s: float
    out_s: float
    reason: str = ""


# The Editor's draft: its cuts in timeline order.
class EditorAnswer(BaseModel):
    title: str = ""
    cuts: list[EditorCut]
    notes: str = ""


# They travel first in the one user message (L1 D22); `ask()` adds "Answer
# with one JSON object and nothing else." and the response format itself.
INSTRUCTIONS = (
    "You are drafting a rough cut for a video editor from a numbered list of shots.\n"
    "Each line is: number, clip, view, in-out seconds of the source, camera move, "
    "smoothness 0-1, subject energy, light, subjects. "
    '"used" means an earlier video already used that shot. '
    '"score N" is another model\'s 0-100 opinion of the shot, where one was given.\n'
    "Rules:\n"
    "- Use only shot numbers from the list. Never invent a shot.\n"
    "- in_s and out_s are seconds of that shot's source and must lie inside its "
    "in-out range.\n"
    '- Do not use a shot twice. Prefer shots not marked "used".\n'
    "- The order of the cuts is their order on the timeline. Open and close on "
    "purpose; vary the moves.\n"
    "- reason: one line of at most 15 words saying why this shot is here.\n"
    'The answer has "title" (a short title for the cut), "cuts" (each with "shot", '
    '"in_s", "out_s" and "reason") and "notes" (anything the editor should know).'
)


def _secs(value: float) -> str:
    """30 rather than 30.0, 1.5 as it is."""
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def prompt(brief: str, c: Constraints, candidates: Sequence[Candidate]) -> str:
    """The question after `INSTRUCTIONS`: the brief as written, the length in
    numbers the model need not add up, and the numbered shots."""
    lines = [
        f"Brief: {brief.strip()}",
        f"Length: about {_secs(c.target_s)} seconds. Use about {c.about} cuts, "
        f"each between {_secs(c.min_shot_s)} and {_secs(c.max_shot_s)} seconds.",
        "",
        "Shots:",
        *(candidate.editor_line() for candidate in candidates),
    ]
    return "\n".join(lines)


def max_tokens(c: Constraints) -> int:
    """Room for twice the cuts asked for, so a model that overshoots is cut
    back by `check` rather than cut off mid-answer."""
    return 400 + 60 * min(MAX_DRAFT_CUTS, 2 * c.about)


# --- what FrameFound does with the answer --------------------------------------


@dataclass(frozen=True)
class Piece:
    """One cut that passed `check`: inside its shot, at least half a second,
    in timeline order."""

    n: int
    asset_id: uuid.UUID
    in_s: float
    out_s: float
    reason: str

    @property
    def length(self) -> float:
        return self.out_s - self.in_s


@dataclass
class _Placed:
    cut: int  # its number in the Editor's list, from 1, for the sentences
    candidate: Candidate
    in_s: float
    out_s: float
    reason: str

    @property
    def length(self) -> float:
        return self.out_s - self.in_s


def _up(value: float) -> float:
    """Rounded up to 0.01 s: an in point never moves outside its shot."""
    return math.ceil(round(value * 100, 6)) / 100


def _down(value: float) -> float:
    return math.floor(round(value * 100, 6)) / 100


@dataclass
class _Report:
    said: list[str] = field(default_factory=list)

    def __call__(self, sentence: str) -> None:
        self.said.append(sentence)


def _place(
    cut: EditorCut, number: int, shot: Candidate, c: Constraints, say: _Report
) -> _Placed | None:
    """One cut held inside its shot and to the shot lengths asked for, or
    None when nothing usable is left of it."""
    in_s, out_s = float(cut.in_s), float(cut.out_s)
    if not (math.isfinite(in_s) and math.isfinite(out_s)):
        say(f"Cut {number} gave no usable in and out points and was dropped.")
        return None
    if in_s > out_s:
        in_s, out_s = out_s, in_s
        say(f"Cut {number} had its in and out points the wrong way round; they were swapped.")
    start, end = shot.start_s, shot.end_s
    if out_s > end or in_s > end:
        say(f"Cut {number} ran past the end of its shot and was pulled back to {end:.1f} s.")
    if in_s < start or out_s < start:
        say(f"Cut {number} started before its shot and was moved up to {start:.1f} s.")
    in_s, out_s = min(max(in_s, start), end), min(max(out_s, start), end)
    if out_s - in_s > c.max_shot_s:
        say(f"Cut {number} was {out_s - in_s:.1f} s long and was cut to {_secs(c.max_shot_s)} s.")
        out_s = in_s + c.max_shot_s
    want = min(c.min_shot_s, end - start)
    if out_s - in_s < c.min_shot_s and want >= MIN_PIECE_S:
        # Widened evenly about its middle, then slid back inside the shot.
        had = out_s - in_s
        middle = (in_s + out_s) / 2
        in_s, out_s = middle - want / 2, middle + want / 2
        if in_s < start:
            in_s, out_s = start, start + want
        if out_s > end:
            in_s, out_s = end - want, end
        say(
            f"Cut {number} was {had:.1f} s long and was widened to {_secs(want)} s inside its shot."
        )
    in_s, out_s = _up(in_s), _down(out_s)
    if out_s - in_s < MIN_PIECE_S:
        whole = f"{end - start:.2f}".rstrip("0").rstrip(".")
        say(
            f"Cut {number} was too short to use, even across its whole shot "
            f"({whole} s), and was dropped."
        )
        return None
    reason = " ".join(_UNWRITABLE.sub(" ", str(cut.reason)).split())[:REASON_CHARS]
    return _Placed(number, shot, in_s, out_s, reason)


def check(
    answer: EditorAnswer, candidates: Sequence[Candidate], c: Constraints
) -> tuple[list[Piece], list[str]]:
    """The Editor's answer made safe to build: (the cut, what was changed).

    In order, each change one sentence: more than `MAX_DRAFT_CUTS` cuts are
    cut back; a cut naming a shot that is not on the list, or with no
    usable times, is dropped; reversed times are swapped; times are held
    inside the shot; a cut longer than the longest shot asked for is
    shortened, one shorter than the shortest is widened inside its shot,
    and one still under half a second is dropped; a cut that repeats an
    earlier one of the same clip is dropped; a draft well over the length
    asked for loses trailing cuts (or the last is shortened); a draft well
    under it is left short and says by how much. Times are rounded inward
    to 0.01 s.
    """
    say = _Report()
    by_number = {candidate.n: candidate for candidate in candidates}
    cuts = list(answer.cuts)
    if len(cuts) > MAX_DRAFT_CUTS:
        say(f"It listed {len(cuts)} cuts; only the first {MAX_DRAFT_CUTS} were kept.")
        cuts = cuts[:MAX_DRAFT_CUTS]

    placed: list[_Placed] = []
    for number, cut in enumerate(cuts, start=1):
        shot = by_number.get(cut.shot)
        if shot is None:
            say(f"Cut {number} named shot {cut.shot}, which is not on the list.")
            continue
        piece = _place(cut, number, shot, c, say)
        if piece is None:
            continue
        earlier = next(
            (
                p
                for p in placed
                if p.candidate.asset_id == shot.asset_id
                and min(p.out_s, piece.out_s) - max(p.in_s, piece.in_s) > REPEAT_OVERLAP_S
            ),
            None,
        )
        if earlier is not None:
            say(f"Cut {number} repeated the shot of cut {earlier.cut}.")
            continue
        placed.append(piece)

    if placed:
        _fit_length(placed, c, say)
    pieces = [Piece(p.candidate.n, p.candidate.asset_id, p.in_s, p.out_s, p.reason) for p in placed]
    return pieces, say.said


def _total(placed: Sequence[_Placed]) -> float:
    return round(sum(p.length for p in placed), 2)


def _fit_length(placed: list[_Placed], c: Constraints, say: _Report) -> None:
    """Too long: trailing cuts go while what is left is still the length
    asked for, then the last is shortened (never under the shortest shot).
    Too short: left so, and said."""
    over, under = c.target_s * (1 + TOLERANCE), c.target_s * (1 - TOLERANCE)
    total = _total(placed)
    if total > over:
        dropped = 0
        while len(placed) > 1 and total - placed[-1].length >= under:
            total = round(total - placed.pop().length, 2)
            dropped += 1
        if dropped:
            which = "cut was" if dropped == 1 else f"{dropped} cuts were"
            say(f"It ran over the {_secs(c.target_s)} s asked for, so the last {which} left off.")
        if total > over:
            last = placed[-1]
            floor = min(c.min_shot_s, last.length)
            new_out = _down(max(last.in_s + floor, last.out_s - (total - c.target_s)))
            if new_out < last.out_s:
                say(
                    f"The last cut was shortened by {last.out_s - new_out:.1f} s "
                    f"to bring the cut nearer the {_secs(c.target_s)} s asked for."
                )
                last.out_s = new_out
        return
    if total < under:
        say(f"It is {_secs(c.target_s - total)} s short of the {_secs(c.target_s)} s asked for.")


# --- an answer that stopped, and the demonstration answer ----------------------


def cut_off(text: str) -> bool:
    """An answer that opens a JSON object and never closes it: the model ran
    out of room, which is not the same as answering in the wrong form."""
    depth = 0
    quoted = escaped = False
    opened = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"' and opened:
            quoted = True
        elif char == "{":
            depth += 1
            opened = True
        elif char == "}" and opened:
            depth -= 1
            if depth == 0:
                return False
    return opened and depth > 0


DEMO_REASON = "Demonstration: taken evenly from the list, not judged."
_ABOUT = re.compile(
    r"Use about (\d+) cuts, each between (\d+(?:\.\d+)?) and (\d+(?:\.\d+)?) seconds"
)
_LINE = re.compile(r"^(\d+) .*?\b(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)s\b")


def _question_text(body: Mapping[str, Any]) -> str:
    messages = body.get("messages") or []
    first = messages[0] if messages and isinstance(messages[0], Mapping) else {}
    content = first.get("content")
    if isinstance(content, str):
        return content
    parts = content if isinstance(content, list) else []
    return "\n".join(
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, Mapping) and part.get("type") == "text"
    )


def demo_answer(body: Mapping[str, Any]) -> str:
    """What the demonstration model answers for `EditorAnswer`
    (`fake.FakeModelServer.answer_for`): the number of cuts asked for, taken
    evenly through the list, each starting half a second into its shot for
    the middle of the allowed length. A real, checkable cut — and plainly
    not a judgement, which its reasons say."""
    text = _question_text(body)
    about = _ABOUT.search(text)
    count, shortest, longest = (
        (int(about.group(1)), float(about.group(2)), float(about.group(3)))
        if about
        else (1, 1.5, 8.0)
    )
    shown: list[tuple[int, float, float]] = []
    listing = text.split("\nShots:\n", 1)[1] if "\nShots:\n" in text else ""
    for line in listing.splitlines():
        found = _LINE.match(line)
        if found:
            shown.append((int(found.group(1)), float(found.group(2)), float(found.group(3))))
    count = min(count, len(shown))
    middle = (shortest + longest) / 2
    cuts = []
    for i in range(count):
        number, start, end = shown[int((i + 0.5) * len(shown) / count)]
        in_s = start + 0.5
        out_s = max(in_s, min(in_s + middle, end - 0.1))
        cuts.append(
            {
                "shot": number,
                "in_s": round(in_s, 2),
                "out_s": round(out_s, 2),
                "reason": DEMO_REASON,
            }
        )
    return json.dumps({"title": "", "cuts": cuts, "notes": DEMO_REASON})
