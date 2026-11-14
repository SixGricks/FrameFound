"""What an edit needs to know about a shot: what it is, was it used, where
its still is, and how it is written down.

Three kinds of rule, each kept in one place because more than one part of
the app asks:

- **"Used in a cut."** A shot was used when a piece of a cut overlaps it by
  a second, or by half the piece if the piece is shorter than two. The
  connector's `find_shots` flags such shots; anything that compares a
  model's taste with what an editor actually chose reads the same rule, so
  the two can never disagree about what "used" means.
- **"This shot's still."** The picture already on disk for a moment of a
  clip. Finding one never makes one: nothing here starts ffmpeg or waits on
  the proxy store, so it is safe behind a page that is only looking.
- **"Which shots, and how each reads."** The shot search behind
  `find_shots` (`ShotFilter`, `select_shots`), the one-line description
  of a shot Claude reads (`shot_line`, `window_line`) and the short words
  in it (`short_id`, `clip_name`, `score`), and writing a cut into history
  (`record_cut`). A local model drafting a cut is shown the same lines, and
  its cut is recorded the same way, so what it reads is what Claude reads.

This module imports nothing from `framefound.mcp`. That package imports the
optional `mcp` SDK at the top of its files, so API and job code cannot
import it; these rules used to live there and were out of their reach.
`mcp/shots.py` and `mcp/compact.py` keep every name they had, bound to the
ones here, and turn a `ShotError` into the connector's own error with the
same words.
"""

import asyncio
import re
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.config import Settings, get_settings
from framefound.db.models import (
    Asset,
    Cut,
    CutPiece,
    Frame,
    Library,
    Look,
    ShotWindow,
    catalogued,
)
from framefound.media import folders, motion

# A piece counts as having used a window when they overlap by a second, or
# by half the piece if it is shorter than two.
USED_OVERLAP_S = 1.0
# A sampled frame this close to the moment asked for shows the same picture.
STILL_NEAR_MS = 600
# Clip ids asked about in one query: a library's worth in a single IN list
# is more bound values than SQLite takes.
PIECES_CHUNK = 500
# Cut sources that count as footage that went somewhere the moment they are
# written: Claude's cuts and imported timelines. A local draft ("local")
# counts once it has been sent.
FINISHED_SOURCES = ("mcp", "import")
# A piece's reason as kept in history (`cut_pieces.note`); a CutIn note is
# at most this long already.
NOTE_CHARS = 500
# The provenance names a cut records a build by, and each column's length:
# a value cut to fit is better than a cut that is not recorded at all.
PROVENANCE = {
    "model": 200,
    "model_revision": 64,
    "variant": 100,
    "runtime": 30,
    "runtime_version": 40,
    "prompt_version": 40,
}

Piece = tuple[int, int, str]  # in_ms, out_ms, the cut's title


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def used_by(window: ShotWindow, pieces: Sequence[Piece]) -> list[str]:
    """The titles of the cuts that used this shot, each once, in the order
    their pieces were given."""
    titles: list[str] = []
    w0, w1 = window.start_ms / 1000, window.end_ms / 1000
    for in_ms, out_ms, title in pieces:
        p0, p1 = in_ms / 1000, out_ms / 1000
        need = min(USED_OVERLAP_S, 0.5 * (p1 - p0))
        if _overlap(w0, w1, p0, p1) >= max(need, 0.05) and title not in titles:
            titles.append(title)
    return titles


def counts_as_used() -> Any:
    """The SQL condition for "this cut is footage that went somewhere": one
    the connector built, one imported from a finished timeline, or a local
    model's draft whose Premiere file has been downloaded.

    Cut history also holds drafts a local model wrote. Three drafts of one
    folder must not mark half of it used (and the Editor, told to prefer
    unused shots, would then avoid its own last attempt), and a draft nobody
    sent to Premiere must not become the truth a model is measured against.
    So every reader that means "used" or "finished" applies this, and this
    is the only place that says which cuts those are. Cuts written before
    drafts existed are all `mcp` or `import`, so they count as they did.

    Written as a list of the sources that count rather than "not local":
    the same rows for every source FrameFound writes, and a source nobody
    expected is not quietly taken for finished work."""
    return or_(Cut.source.in_(FINISHED_SOURCES), Cut.sent_at.is_not(None))


async def pieces_by_asset(
    db: AsyncSession,
    asset_ids: Sequence[uuid.UUID],
    *,
    cut_ids: Collection[uuid.UUID] | None = None,
) -> dict[uuid.UUID, list[Piece]]:
    """The pieces cut from each of these clips, by clip.

    Without `cut_ids`, the pieces of every cut that `counts_as_used` (what
    `find_shots` flags as used). With `cut_ids`, only the pieces of those
    cuts, and the rule is not applied again: the caller chose them (Compare
    only offers cuts that already pass it). An empty collection means no
    cut counts, which is not the same as not having said."""
    out: dict[uuid.UUID, list[Piece]] = {}
    only = None if cut_ids is None else list(cut_ids)
    if not asset_ids or only == []:
        return out
    for chunk_start in range(0, len(asset_ids), PIECES_CHUNK):
        chunk = asset_ids[chunk_start : chunk_start + PIECES_CHUNK]
        query = (
            select(CutPiece.asset_id, CutPiece.in_ms, CutPiece.out_ms, Cut.title)
            .join(Cut, Cut.id == CutPiece.cut_id)
            .where(CutPiece.asset_id.in_(chunk))
        )
        if only is not None:
            query = query.where(CutPiece.cut_id.in_(only))
        else:
            query = query.where(counts_as_used())
        rows = (await db.execute(query)).all()
        for asset_id, in_ms, out_ms, title in rows:
            out.setdefault(uuid.UUID(str(asset_id)), []).append(
                (int(in_ms), int(out_ms), str(title))
            )
    return out


async def still_path(
    db: AsyncSession,
    asset_id: uuid.UUID,
    ts_ms: int,
    settings: Settings | None = None,
    *,
    nearest: bool = True,
) -> Path | None:
    """The picture already on disk for `ts_ms` of a clip, or None.

    In order: the still the shot analysis saved at exactly that moment; the
    clip's closest sampled frame when it is within 0.6 s; and, only when
    `nearest`, that closest frame however far off it is (better than a blank
    tile on a contact sheet, wrong for showing a model "this shot").

    Reads only. It never grabs a frame from the proxy, so a shot without a
    picture stays without one here; the file checks run in a thread because
    the data folder may be a slow disk."""
    found = await still_found(
        db, asset_id, ts_ms, (settings or get_settings()).data_dir, nearest=nearest
    )
    return found[0] if found is not None else None


async def still_found(
    db: AsyncSession,
    asset_id: uuid.UUID,
    ts_ms: int,
    data_dir: Path,
    *,
    nearest: bool = True,
    skip: Collection[Path] = (),
) -> tuple[Path, int] | None:
    """`still_path`, with the moment the picture actually shows: `ts_ms` for
    the saved still, the frame's own moment for a sampled frame.

    The moment comes from the frame's row, never from the file's name. A
    photograph's frame row points at a picture made for something else, and
    a clip nothing could be decoded from has one row at 0 pointing at its
    thumbnail, so the name does not always say when. Whoever labels a tile
    or tells a model "this is 0:07" needs the row's answer.

    `skip` is files to step past: ones the caller found here, opened, and
    could not read as a picture. This lookup only asks whether a file is
    there, so half a JPEG left by an ffmpeg that was stopped looks like a
    still to it; and because the saved still is tried first, that one file
    would otherwise stand in front of every sampled frame the clip has.
    Handed back in `skip`, it is treated as not there and the answer is the
    next file in the same order.

    This is the lookup itself; `still_path` is it without the moment. It
    takes the data folder rather than the settings because the one caller
    that goes on to grab a missing still (`processing.shots.still_at`) is
    handed a folder."""
    # Imported here, as everywhere else: processing.shots brings numpy and
    # the decode path with it, and most callers of this module never ask
    # for a still.
    from framefound.processing import shots

    saved = data_dir / shots.still_relpath(asset_id, ts_ms)
    if saved not in skip and await asyncio.to_thread(saved.is_file):
        return saved, ts_ms
    by_closeness = (
        select(Frame.ts_ms, Frame.relative_path)
        .where(Frame.asset_id == asset_id)
        .order_by(func.abs(Frame.ts_ms - ts_ms), Frame.ts_ms)
    )
    # One row is all that is ever needed unless files are being stepped
    # past. Then the clip's rows are read in order and matched by the path
    # they resolve to, since that is what the caller was given; it happens
    # only after a file has failed to read, which is rare.
    rows: Any = (await db.execute(by_closeness if skip else by_closeness.limit(1))).all()
    for frame_ms, relative in rows:
        path = data_dir / str(relative)
        if path in skip:
            continue
        shown_ms = int(frame_ms)
        if not nearest and abs(shown_ms - ts_ms) > STILL_NEAR_MS:
            return None
        return (path, shown_ms) if await asyncio.to_thread(path.is_file) else None
    return None


# --- the short words of a line (moved from mcp/compact.py) ---------------------

# Ids are the first eight hex characters of the asset's UUID; every
# connector tool that takes an asset id accepts one.
SHORT = 8
_DJI = re.compile(r"^DJI_\d{14}_(\d{4})_\w+$", re.IGNORECASE)


def short_id(value: uuid.UUID | str) -> str:
    return str(value).replace("-", "")[:SHORT]


def clip_name(filename: str, width: int = 24) -> str:
    """A name short enough for a table: DJI_20261103051509_0639_D.MP4 is
    DJI_0639 (the counter is what the operator says out loud); anything
    else loses its extension and is cut to `width`."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    found = _DJI.match(stem)
    if found:
        return f"DJI_{found.group(1)}"
    stem = stem.replace(" ", "_")
    return stem if len(stem) <= width else stem[: width - 1] + "…"


def score(value: float | None) -> str:
    """.91 rather than 0.91: two characters saved on every line."""
    if value is None:
        return ""
    text = f"{value:.2f}"
    return text[1:] if text.startswith("0") else text


LIGHT_SHORT = {
    "golden hour": "golden",
    "blue hour": "blue",
    "low sun": "low-sun",
    "mid sun": "mid-sun",
    "high sun": "high-sun",
}


# --- reading a shot filter (moved from mcp/shots.py) ---------------------------

# How many windows one search reads before its Python filters. The
# connector's own SCAN_LIMIT is passed in by `find_shots`, so a test that
# lowers that one still reaches the query.
SCAN_LIMIT = 20000

MOVE_ALIASES = {
    "glide": "lateral glide",
    "lateral": "lateral glide",
    "truck": "lateral glide",
    "drift": "top-down drift",
    "top down": "top-down drift",
    "topdown": "top-down drift",
    "push": "push-in",
    "push in": "push-in",
    "pushin": "push-in",
    "dolly in": "push-in",
    "forward": "push-in",
    "pull": "pull-out",
    "pull out": "pull-out",
    "pullout": "pull-out",
    "dolly out": "pull-out",
    "reveal": "pull-out",
    "still": "static",
    "locked off": "static",
    "ascend": "rise",
    "climb": "rise",
    "boom up": "rise",
    "sink": "descend",
    "boom down": "descend",
}
LIGHT_ALIASES = {
    "golden": "golden hour",
    "sunset": "golden hour",
    "sunrise": "golden hour",
    "blue": "blue hour",
    "dusk": "blue hour",
    "low": "low sun",
    "mid": "mid sun",
    "midday": "high sun",
    "high": "high sun",
    "noon": "high sun",
}
SKY_WORDS = {"overcast": "overcast", "cloudy": "overcast", "direct": "direct", "sunny": "direct"}
SPEED_CHOICES = tuple(int(s * 100) for s in motion.SPEEDS)


class ShotError(ValueError):
    """A filter that cannot be read; the message names the choices.

    The connector turns it into its own error with the same words, so what
    Claude is told did not change when this moved out of it."""


def normalise_move(value: str) -> str:
    key = value.strip().lower().replace("_", " ")
    key = MOVE_ALIASES.get(key, key)
    if key not in motion.MOVES and key.endswith("s") and key[:-1] in motion.MOVES:
        key = key[:-1]
    if key not in motion.MOVES:
        raise ShotError(f"No move called {value!r}. Moves: {', '.join(motion.MOVES)}")
    return key


def normalise_light(value: str) -> tuple[str, str]:
    """('light', class) or ('sky', overcast|direct)."""
    key = value.strip().lower().replace("_", " ")
    if key in SKY_WORDS:
        return "sky", SKY_WORDS[key]
    key = LIGHT_ALIASES.get(key, key)
    from framefound.media.sun import LIGHT_CLASSES

    if key not in LIGHT_CLASSES:
        raise ShotError(
            f"No light called {value!r}. Light: {', '.join(LIGHT_CLASSES)}, overcast, direct"
        )
    return "light", key


def speed_of(at_speed: int) -> float:
    if at_speed not in SPEED_CHOICES:
        raise ShotError(f"at_speed is a percentage, one of {', '.join(map(str, SPEED_CHOICES))}")
    return at_speed / 100


def smooth_at(window: ShotWindow, at_speed: int) -> float:
    by = window.smooth_by_speed or {}
    value = by.get(str(at_speed))
    return float(value) if value is not None else float(window.smooth or 0.0)


def parse_when(text: str) -> datetime:
    """A capture-date bound from ISO text; a date or time with no zone is
    taken as UTC, as `find_shots` always has."""
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise ShotError(f"{text!r} is not an ISO date") from None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


# --- a local model's opinion of a shot (moved from mcp/shots.py; L3) -----------


async def shot_looks(
    db: AsyncSession, asset_ids: list[uuid.UUID]
) -> dict[tuple[uuid.UUID, int, int], Look]:
    """The current looks of these clips' shots, by (asset, start, end).

    Joined on the shot's times, never on a window id: windows are rebuilt on
    every re-analysis, and looking/store.py keeps a look current only while
    its times are still a window's.
    """
    out: dict[tuple[uuid.UUID, int, int], Look] = {}
    if not asset_ids:
        return out
    rows = (
        await db.execute(
            select(Look).where(
                Look.asset_id.in_(asset_ids),
                Look.current.is_(True),
                Look.ok.is_(True),
                Look.start_ms.is_not(None),
                Look.end_ms.is_not(None),
            )
        )
    ).scalars()
    for look in rows:
        if look.start_ms is not None and look.end_ms is not None:
            out[(uuid.UUID(str(look.asset_id)), look.start_ms, look.end_ms)] = look
    return out


def window_score() -> Any:
    """The score of the current, successful look at a window's times, as a
    column of a query over `ShotWindow` (NULL when no model looked)."""
    return (
        select(Look.score)
        .where(
            Look.asset_id == ShotWindow.asset_id,
            Look.start_ms == ShotWindow.start_ms,
            Look.end_ms == ShotWindow.end_ms,
            Look.current.is_(True),
            Look.ok.is_(True),
        )
        .limit(1)
        .scalar_subquery()
    )


def score_word(look: Look | None) -> str:
    """The words a shot's line ends with: "" when no model looked at it, so
    the line is exactly what it was before scores existed."""
    if look is None or look.score is None:
        return ""
    return f" score {look.score}" + (" made-up" if look.demo else "")


# --- one shot, written down (moved from mcp/shots.py) --------------------------


def window_line(
    window: ShotWindow,
    at_speed: int = 100,
    *,
    sequence_fps: float = 29.97,
    used: bool = False,
    with_subjects: bool = True,
) -> str:
    """The compact description of one window, without the clip: times,
    move, smoothness, energy, light, subjects, and flags."""
    start, end = window.start_ms / 1000, window.end_ms / 1000
    # ASCII dashes and two subjects: twenty lines must stay near 2 KB.
    parts = [f"{start:.1f}-{end:.1f}s", window.move.replace(" ", "-")]
    if window.direction and window.move != "static":
        parts.append(window.direction)
    if at_speed == 100:
        parts.append(f"smooth {score(window.smooth)}")
    else:
        parts.append(f"smooth@{at_speed}% {score(smooth_at(window, at_speed))}")
    parts.append(f"energy {window.energy}")
    if window.light:
        parts.append(LIGHT_SHORT.get(window.light, window.light))
    sky = (window.look or {}).get("sky")
    if sky == "overcast":
        parts.append("overcast")
    if with_subjects and window.subjects:
        parts.append(",".join(list(window.subjects)[:2]))
    if at_speed != 100:
        length = (end - start) / (at_speed / 100)
        parts.append(f"->{length:.1f}s")
        if (
            at_speed < 100
            and window.source_fps
            and window.source_fps * at_speed / 100 < sequence_fps * 0.97
        ):
            parts.append("dup")
    if used:
        parts.append("used")
    return " ".join(parts)


def shot_line(
    asset: Asset,
    window: ShotWindow,
    at_speed: int = 100,
    *,
    sequence_fps: float = 29.97,
    used: bool = False,
) -> str:
    """A `find_shots` line without its score or local path: the clip's
    short id and name, then `window_line`. `parse_shot` reads it back, so
    anything added to a line goes after it, never into it."""
    return f"{short_id(asset.id)} {clip_name(asset.filename)} " + window_line(
        window, at_speed, sequence_fps=sequence_fps, used=used
    )


def shot_snapshot(window: ShotWindow | None, speed: float = 1.0) -> dict[str, Any]:
    """What a piece's window was, for cut history."""
    if window is None:
        return {}
    key = motion.speed_key(speed)
    return {
        "idx": window.idx,
        "start_ms": window.start_ms,
        "end_ms": window.end_ms,
        "move": window.move,
        "direction": window.direction,
        "smooth": (window.smooth_by_speed or {}).get(key, window.smooth),
        "energy": window.energy,
        "light": window.light,
        "sky": (window.look or {}).get("sky"),
        "subjects": list((window.subjects or {}).keys())[:4],
    }


def best_window(windows: list[ShotWindow], in_s: float, out_s: float) -> ShotWindow | None:
    best, overlap = None, 0.0
    for window in windows:
        found = _overlap(window.start_ms / 1000, window.end_ms / 1000, in_s, out_s)
        if found > overlap:
            best, overlap = window, found
    return best


async def windows_for(
    db: AsyncSession, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ShotWindow]]:
    out: dict[uuid.UUID, list[ShotWindow]] = {}
    if not asset_ids:
        return out
    rows = (
        (
            await db.execute(
                select(ShotWindow)
                .where(ShotWindow.asset_id.in_(asset_ids))
                .order_by(ShotWindow.asset_id, ShotWindow.idx)
            )
        )
        .scalars()
        .all()
    )
    for window in rows:
        out.setdefault(uuid.UUID(str(window.asset_id)), []).append(window)
    return out


async def record_cut(
    db: AsyncSession,
    title: str,
    pieces: list[tuple[Asset, float, float, float]],
    *,
    client: str | None,
    profile: str | None,
    created_by: uuid.UUID | None,
    source: str = "mcp",
    created_at: datetime | None = None,
    notes: Sequence[str] | None = None,
    parent: Cut | None = None,
    provenance: Mapping[str, str] | None = None,
) -> Cut:
    """Write a cut and its pieces, each with a snapshot of its window, and
    commit.

    `notes` is one reason per piece (kept, cut to the column, on the piece).
    `parent`, loaded in this session, makes the cut the next version of the
    parent's family: same root, `version` one above the highest in it. A
    cut with no parent is version 1 of a family of its own. `provenance`
    names the build that drafted it under its column names
    (`Answer.provenance()`, plus `variant` when there is one); a name that
    is not a provenance column is refused rather than dropped.
    """
    if notes is not None and len(notes) != len(pieces):
        raise ValueError(f"{len(notes)} notes for {len(pieces)} pieces")
    unknown = sorted(set(provenance or {}) - set(PROVENANCE))
    if unknown:
        raise ValueError(f"not provenance: {', '.join(unknown)}")
    libraries = {lib.id: lib for lib in (await db.execute(select(Library))).scalars()}
    counts: dict[uuid.UUID, int] = {}
    for asset, *_ in pieces:
        counts[asset.library_id] = counts.get(asset.library_id, 0) + 1
    library_id = max(counts, key=lambda k: counts[k]) if counts else None
    library = libraries.get(library_id) if library_id else None
    windows = await windows_for(db, list({asset.id for asset, *_ in pieces}))
    cut = Cut(
        title=title[:300],
        client=(client or (library.name if library else ""))[:200],
        library_id=library_id,
        source=source,
        profile=profile,
        duration_s=round(sum((out - in_) / speed for _, in_, out, speed in pieces), 3),
        piece_count=len(pieces),
        created_by=created_by,
        version=1,
    )
    if parent is not None:
        root = parent.root_id or parent.id
        highest = (
            await db.execute(
                select(func.max(Cut.version)).where(or_(Cut.id == root, Cut.root_id == root))
            )
        ).scalar_one_or_none()
        cut.root_id, cut.parent_id = root, parent.id
        cut.version = max(int(highest or 0), parent.version or 1) + 1
    for name, value in (provenance or {}).items():
        setattr(cut, name, value[: PROVENANCE[name]])
    if created_at is not None:
        cut.created_at = created_at
    db.add(cut)
    await db.flush()
    for position, (asset, in_s, out_s, speed) in enumerate(pieces):
        window = best_window(windows.get(asset.id, []), in_s, out_s)
        db.add(
            CutPiece(
                cut_id=cut.id,
                position=position,
                asset_id=asset.id,
                filename=asset.filename,
                in_ms=int(round(in_s * 1000)),
                out_ms=int(round(out_s * 1000)),
                speed=speed,
                shot=shot_snapshot(window, speed),
                note=(notes[position] if notes is not None else "")[:NOTE_CHARS],
            )
        )
    await db.commit()
    return cut


async def family(db: AsyncSession, cut: Cut) -> list[Cut]:
    """Every version of `cut`'s family, `cut` included, oldest first.

    The family is everything sharing `coalesce(root_id, id)`. It survives
    its first version being deleted (no foreign key), and two versions
    written at the same instant may share a number, so the order is the
    version and then when each was written.
    """
    root = cut.root_id or cut.id
    rows = await db.execute(
        select(Cut)
        .where(or_(Cut.id == root, Cut.root_id == root))
        .order_by(Cut.version, Cut.created_at, Cut.id)
    )
    return list(rows.scalars().all())


# --- which shots (the body of find_shots, moved from mcp/shots.py) -------------

Order = Literal["smooth", "length", "time", "score"]
Shot = tuple[ShotWindow, Asset, list[str]]  # window, its clip, titles of cuts that used it


@dataclass
class ShotFilter:
    """What `find_shots` can ask for, already read: moves, light classes and
    skies are the normalised words (`normalise_move`, `normalise_light`),
    capture bounds are datetimes. A field left at its default asks nothing.
    """

    library_id: uuid.UUID | None = None
    # find_shots' meaning: these letters anywhere in the path, any case.
    folder: str = ""
    # This folder and everything beneath it, and nothing beside it with a
    # longer name: media.folders.under, the one definition of that.
    folder_prefix: str = ""
    asset_ids: Sequence[uuid.UUID] = ()
    captured_after: datetime | None = None
    captured_before: datetime | None = None
    moves: Sequence[str] = ()
    energy: Sequence[str] = ()
    light: Sequence[str] = ()
    skies: Sequence[str] = ()
    subjects: Sequence[str] = ()
    min_smooth: float = 0.0
    at_speed: int = 100
    min_length_s: float = 0.0
    exclude_used: bool = False
    real_frames_only: bool = False
    sequence_fps: float = 29.97
    # A local model's score (L3): keep only shots scored at least this.
    min_score: int = 0
    order: Order = "smooth"


@dataclass
class ShotScan:
    """What one search found, and what `find_shots` needs to say why when
    it found nothing."""

    shots: list[Shot]  # every match, in the filter's order; not limited
    # Rows the database returned before the Python filters (moves and light
    # are in SQL; smoothness, length, subjects, sky and "used" are not).
    scanned: int
    looks: dict[tuple[uuid.UUID, int, int], Look]
    # Asked only when a min_score search scanned nothing: whether the scope
    # has any window at all, and whether any of them is scored.
    any_window: bool = False
    any_scored: bool = False

    def look_of(self, window: ShotWindow, asset: Asset) -> Look | None:
        return self.looks.get((asset.id, window.start_ms, window.end_ms))


def _aware(when: datetime | None) -> datetime | None:
    if when is None or when.tzinfo is not None:
        return when
    return when.replace(tzinfo=UTC)


async def scan_shots(db: AsyncSession, f: ShotFilter, *, scan_limit: int | None = None) -> ShotScan:
    """The shot windows that match `f`: online, catalogued video only, the
    SQL and Python filters exactly as `find_shots` has always applied them,
    sorted by `f.order`. ShotError for a speed or score out of range, or a
    folder prefix that is not a folder."""
    speed = speed_of(f.at_speed)
    if not 0 <= f.min_score <= 100:
        raise ShotError("min_score is a local model's 0-100 opinion; give 0 to 100")
    query = (
        select(ShotWindow, Asset)
        .join(Asset, Asset.id == ShotWindow.asset_id)
        .where(Asset.availability == "online", Asset.media_type == "video", catalogued())
    )
    if f.library_id is not None:
        query = query.where(Asset.library_id == f.library_id)
    if f.folder and f.folder.strip():
        query = query.where(
            func.lower(Asset.relative_path).contains(f.folder.strip().lower(), autoescape=True)
        )
    if f.folder_prefix:
        try:
            inside = folders.under(f.folder_prefix)
        except folders.FolderError as err:
            raise ShotError(str(err)) from None
        query = query.where(*inside)
    if f.asset_ids:
        query = query.where(Asset.id.in_(list(f.asset_ids)))
    after, before = _aware(f.captured_after), _aware(f.captured_before)
    if after is not None:
        query = query.where(Asset.captured_at >= after)
    if before is not None:
        query = query.where(Asset.captured_at <= before)
    if f.moves:
        query = query.where(ShotWindow.move.in_(list(f.moves)))
    if f.energy:
        query = query.where(ShotWindow.energy.in_(list(f.energy)))
    if f.light:
        query = query.where(ShotWindow.light.in_(list(f.light)))
    # The score filter and order go into SQL, before the cut to
    # SCAN_LIMIT: scored shots are few, and an unordered first 20 000
    # rows of a large archive could hold none of them.
    scored_by = window_score()
    scanned = query
    if f.min_score:
        scanned = scanned.where(scored_by >= f.min_score)
    if f.order == "score":
        scanned = scanned.order_by(
            scored_by.desc().nullslast(), ShotWindow.asset_id, ShotWindow.start_ms
        )
    limit = SCAN_LIMIT if scan_limit is None else scan_limit
    rows = (await db.execute(scanned.limit(limit))).all()
    # Why min_score found nothing: no shot in scope scored at all, or
    # none scored that high. Asked of the whole scope, not of a sample.
    any_window = any_scored = False
    if f.min_score and not rows:
        any_window = (await db.execute(query.limit(1))).first() is not None
        any_scored = (
            any_window
            and (await db.execute(query.where(scored_by.is_not(None)).limit(1))).first() is not None
        )
    pieces = await pieces_by_asset(db, list({a.id for _, a in rows}))
    looks = await shot_looks(db, list({a.id for _, a in rows}))

    def look_of(window: ShotWindow, asset: Asset) -> Look | None:
        return looks.get((asset.id, window.start_ms, window.end_ms))

    wanted_subjects = [s.strip().lower() for s in f.subjects if s.strip()]
    matched: list[Shot] = []
    for window, asset in rows:
        if smooth_at(window, f.at_speed) < f.min_smooth:
            continue
        if f.min_score:
            seen = look_of(window, asset)
            if seen is None or seen.score is None or seen.score < f.min_score:
                continue
        if (window.end_ms - window.start_ms) / 1000 / speed < f.min_length_s:
            continue
        if f.skies and (window.look or {}).get("sky") not in f.skies:
            continue
        if wanted_subjects:
            have = [s.lower() for s in (window.subjects or {})]
            if not any(any(w == h or w in h for h in have) for w in wanted_subjects):
                continue
        if (
            f.real_frames_only
            and speed < 1.0
            and (not window.source_fps or window.source_fps * speed < f.sequence_fps * 0.97)
        ):
            continue
        used = used_by(window, pieces.get(asset.id, []))
        if f.exclude_used and used:
            continue
        matched.append((window, asset, used))

    def key(item: Shot) -> tuple[Any, ...]:
        window, asset, _ = item
        length = window.end_ms - window.start_ms
        when = asset.captured_at.timestamp() if asset.captured_at else 0.0
        if f.order == "length":
            return (-length, asset.filename, window.idx)
        if f.order == "time":
            return (when, asset.filename, window.start_ms)
        smooth = -round(smooth_at(window, f.at_speed), 2)
        if f.order == "score":
            seen = look_of(window, asset)
            scored = seen.score if seen is not None else None
            return (scored is None, -(scored or 0), smooth, -length, asset.filename, window.idx)
        return (smooth, -length, asset.filename, window.idx)

    matched.sort(key=key)
    return ShotScan(
        shots=matched,
        scanned=len(rows),
        looks=looks,
        any_window=any_window,
        any_scored=any_scored,
    )


async def select_shots(
    db: AsyncSession, f: ShotFilter, *, scan_limit: int | None = None
) -> list[Shot]:
    """The shots `find_shots` would list for `f`, all of them, in its order.
    The caller slices."""
    return (await scan_shots(db, f, scan_limit=scan_limit)).shots


async def analysed_clips(db: AsyncSession, library_id: uuid.UUID | None) -> int:
    """How many catalogued clips have shot windows, in one library or all."""
    return int(
        (
            await db.execute(
                select(func.count(func.distinct(ShotWindow.asset_id)))
                .join(Asset, Asset.id == ShotWindow.asset_id)
                .where(
                    catalogued(),
                    *([Asset.library_id == library_id] if library_id else []),
                )
            )
        ).scalar_one()
    )
