"""A selects reel: search hits turned into moments an editor reviews and cuts.

Between `build_bin` (whole clips, nothing chosen) and `build_rough_cut` (an
edit someone already made) sits the step an editor does by hand after a
search: scrub each hit, find where the soundbite starts and ends or where the
shot of the mainline trench is, and string those moments out to look at. This
does that step, and hands Premiere one FCP7 XML with:

1. **a bin of the source clips**, each with a marker at every selected moment
   whose comment says why it was picked — the sentence that was said, the
   tag, or `visual: <query>`;
2. **a "Selects" sequence** of the moments end to end, each with handles
   (a second either side, clamped to the clip) and the same marker on it.

Never a rendered file: every piece points at the original on the share, so
the editor trims, re-cuts and grades from full quality.

Everything here is arithmetic on rows already fetched; `api/v1/selects.py`
does the fetching for the web UI, the panel and the MCP connector alike.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from framefound.nle.fcp7 import Clip, Cut, Marker, build_project

Order = Literal["rank", "captured"]

DEFAULT_HANDLES_S = 1.0
DEFAULT_MAX_PICTURE_S = 8.0  # a shot found by what it shows
DEFAULT_MAX_SPEECH_S = 20.0  # a soundbite, completed to its sentence
DEFAULT_STILL_S = 4.0  # a GELCO masterpiece photograph on screen
NO_SCENES_HALF_S = 3.0  # ±3 s around the frame when a clip has no detected cuts
# Speech pauses longer than this are a new thought, not the same sentence.
SENTENCE_GAP_S = 1.5
_ENDS_SENTENCE = re.compile(r"[.!?…][\"'”’)\]]*\s*$")


# --- moments ----------------------------------------------------------------


@dataclass
class Segment:
    start_s: float
    end_s: float
    text: str


@dataclass
class Moment:
    """One stretch of one clip worth looking at, and why.

    `in_s`/`out_s` are the moment itself, in seconds of the source; handles
    are added when the sequence is built, so merging and ordering never see
    them. `at_s` is where the marker goes: where the words start.
    """

    asset_id: str
    in_s: float
    out_s: float
    reason: str
    rank: int
    at_s: float = 0.0
    is_still: bool = False
    reasons: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.reasons:
            self.reasons = [self.reason]


def speech_span(
    segments: list[Segment], hit: int, max_s: float = DEFAULT_MAX_SPEECH_S
) -> tuple[float, float, str]:
    """The soundbite around segment `hit`: whole sentences, at most `max_s`.

    Whisper cuts its segments where the audio pauses, not where sentences
    end, so the hit is often half a sentence ("…the mainline was the first
    thing we"). Neighbours are taken in while the hit's sentence is still
    open — backwards while the previous segment did not end one, forwards
    while the last one taken does not — and while the speaker has not
    paused long enough to have moved on. The cap wins over the sentence: a
    superintendent who talks in one forty-second breath gets twenty.
    """
    first = last = hit
    start, end = segments[hit].start_s, segments[hit].end_s
    while first > 0:
        before = segments[first - 1]
        if (
            _ENDS_SENTENCE.search(before.text)
            or segments[first].start_s - before.end_s > SENTENCE_GAP_S
            or end - before.start_s > max_s
        ):
            break
        first -= 1
        start = before.start_s
    while last + 1 < len(segments) and not _ENDS_SENTENCE.search(segments[last].text):
        after = segments[last + 1]
        if after.start_s - segments[last].end_s > SENTENCE_GAP_S or after.end_s - start > max_s:
            break
        last += 1
        end = after.end_s
    end = min(end, start + max_s)
    text = " ".join(s.text.strip() for s in segments[first : last + 1] if s.text.strip())
    return start, end, text


def scene_groups(rows: Iterable[tuple[int, int | None]]) -> tuple[list[list[int]], bool]:
    """Sampled frames `(ts_ms, scene_number)` grouped into shots, and whether
    any scene detection ran at all.

    With detection, a range starts only at a detected cut (and at the first
    frame, the opening shot before any cut); the interval ticks stored
    between cuts with scene_number None fall inside the shot they were
    sampled from. Keying ticks by row index instead, in the same set as the
    scene numbers, let the tick at row 3 take key 3 and swallow scene 3 — one
    "scene" ran straight across a hard cut. Without detection, each sampled
    frame stands for the stretch up to the next one.
    """
    rows = list(rows)
    numbered = any(scene is not None for _, scene in rows)
    groups: list[list[int]] = []
    for ts_ms, scene in rows:
        if not groups or not numbered or scene is not None:
            groups.append([int(ts_ms)])
        else:
            groups[-1].append(int(ts_ms))
    return groups, numbered


def scene_ranges(groups: list[list[int]], duration_s: float | None) -> list[tuple[float, float]]:
    """Each group as (start_s, end_s): to the next group, or to the end."""
    ranges = []
    for position, sampled in enumerate(groups):
        start_ms = sampled[0]
        end_ms = (
            groups[position + 1][0]
            if position + 1 < len(groups)
            else int((duration_s or start_ms / 1000) * 1000)
        )
        ranges.append((start_ms / 1000, max(end_ms, start_ms) / 1000))
    return ranges


def picture_span(
    at_s: float,
    scenes: list[tuple[float, float]],
    detected: bool,
    duration_s: float | None,
    max_s: float = DEFAULT_MAX_PICTURE_S,
) -> tuple[float, float]:
    """The shot a visual or tag hit belongs to: the detected scene holding
    the frame, at most `max_s` of it centred on the frame. A clip with no
    scene detection gets ±3 s around the frame."""
    end_of_clip = duration_s if duration_s and duration_s > 0 else None
    if detected:
        for start, end in scenes:
            if start <= at_s < end or (end == start == at_s):
                break
        else:
            start, end = at_s - NO_SCENES_HALF_S, at_s + NO_SCENES_HALF_S
    else:
        start, end = at_s - NO_SCENES_HALF_S, at_s + NO_SCENES_HALF_S
    start = max(0.0, start)
    if end_of_clip is not None:
        end = min(end, end_of_clip)
    if end - start > max_s:
        # Centred on the frame, slid back inside the scene where the frame
        # sits near one of its ends.
        half = max_s / 2
        lo, hi = start, end
        start = min(max(at_s - half, lo), hi - max_s)
        end = start + max_s
    return start, max(end, start)


def merge(moments: list[Moment], handles_s: float = DEFAULT_HANDLES_S) -> list[Moment]:
    """One moment where two of the same clip would overlap once handled.

    Two hits a few seconds apart in one interview would otherwise put the
    same words on the timeline twice, the second time starting a second
    before the first ended. Merged, the moment spans both, keeps the better
    rank, says both reasons, and marks where the first one starts.
    """
    by_asset: dict[str, list[Moment]] = {}
    for moment in moments:
        by_asset.setdefault(moment.asset_id, []).append(moment)
    merged: list[Moment] = []
    for group in by_asset.values():
        group.sort(key=lambda m: (m.in_s, m.out_s))
        current = replace(group[0], reasons=list(group[0].reasons))
        for moment in group[1:]:
            if moment.is_still or moment.in_s - handles_s <= current.out_s + handles_s:
                if moment.rank < current.rank:
                    current.rank = moment.rank
                    current.at_s = moment.at_s
                current.out_s = max(current.out_s, moment.out_s)
                current.reasons += [r for r in moment.reasons if r not in current.reasons]
                current.reason = " / ".join(current.reasons)
            else:
                merged.append(current)
                current = replace(moment, reasons=list(moment.reasons))
        merged.append(current)
    return merged


def ordered(
    moments: list[Moment],
    order: Order = "rank",
    captured: Callable[[str], datetime | None] | None = None,
) -> list[Moment]:
    """Search rank (the order the hits were found in), or capture time — a
    year-end video runs January to December. Undated clips go last, in rank
    order, rather than pretending to a date."""
    if order == "captured" and captured is not None:

        def key(m: Moment) -> tuple[int, float, float, int]:
            when = captured(m.asset_id)
            stamp = when.timestamp() if when is not None else 0.0
            return (when is None, stamp, m.in_s, m.rank)

        return sorted(moments, key=key)
    return sorted(moments, key=lambda m: (m.rank, m.in_s))


# --- naming -----------------------------------------------------------------

_DAY_FOLDER = re.compile(r"^(\d{4})[ ._-](\d{1,2})[ ._-](\d{1,2})$")
_YEAR_FOLDER = re.compile(r"^(19|20)\d{2}$")


def shoot_date(relative_path: str, captured_at: datetime | None = None) -> str | None:
    """The shoot's date: `Course / 2025 10 28 / …` names it; newer shoots
    filed as `Course / 2026 / Drone / …` leave it to the camera's clock."""
    folders = PurePosixPath(relative_path).parts[:-1]
    for part in folders:
        found = _DAY_FOLDER.match(part)
        if found:
            year, month, day = found.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
    if captured_at is not None:
        return captured_at.date().isoformat()
    for part in folders:
        if _YEAR_FOLDER.match(part):
            return part
    return None


def short_reason(reason: str, words: int = 6, chars: int = 40) -> str:
    text = " ".join(reason.split())
    cut = " ".join(text.split(" ")[:words])
    if len(cut) > chars:
        cut = cut[:chars].rsplit(" ", 1)[0] or cut[:chars]
    return cut + ("…" if cut != text else "")


def select_name(relative_path: str, captured_at: datetime | None, reason: str) -> str:
    """`Pete Dye 2025-10-28 – the mainline was the first…`: where and when it
    was shot (the library's first folder is the course), then why it is here."""
    parts = PurePosixPath(relative_path).parts
    course = parts[0] if len(parts) > 1 else PurePosixPath(relative_path).stem
    where_when = " ".join(x for x in (course, shoot_date(relative_path, captured_at)) if x)
    return f"{where_when} – {short_reason(reason)}"


def safe_filename(title: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in " -_" else "-" for ch in title).strip()
    return f"{safe or 'selects'} selects.xml"


# --- the document -------------------------------------------------------------


@dataclass
class Source:
    """What the document needs to know about one clip."""

    asset_id: str
    clip: Clip  # path already translated for the workstation
    relative_path: str
    captured_at: datetime | None
    is_still: bool


def build_selects_xml(
    title: str,
    moments: list[Moment],
    sources: dict[str, Source],
    *,
    handles_s: float = DEFAULT_HANDLES_S,
) -> str:
    """The bin (each source once, a marker per moment) and the Selects
    sequence (each moment with its handles), in one FCP7 document."""
    clips: dict[str, Clip] = {}
    cuts: list[Cut] = []
    for moment in moments:
        source = sources[moment.asset_id]
        name = select_name(source.relative_path, source.captured_at, moment.reason)
        if moment.asset_id not in clips:
            clips[moment.asset_id] = replace(source.clip, markers=[])
        clips[moment.asset_id].markers.append(
            Marker(
                name=name, seconds=0.0 if source.is_still else moment.at_s, comment=moment.reason
            )
        )
        if source.is_still:
            cuts.append(
                Cut(
                    source.clip,
                    0.0,
                    moment.out_s - moment.in_s,
                    is_still=True,
                    note=moment.reason,
                    marker_name=name,
                )
            )
            continue
        end_of_clip = source.clip.duration_s
        in_s = max(0.0, moment.in_s - handles_s)
        out_s = moment.out_s + handles_s
        if end_of_clip:
            out_s = min(out_s, end_of_clip)
        cuts.append(
            Cut(
                source.clip,
                in_s,
                out_s,
                note=moment.reason,
                marker_s=moment.at_s,
                marker_name=name,
            )
        )
    label = f"{title.strip() or 'FrameFound'} selects"
    return build_project(label, label, list(clips.values()), label, cuts)
