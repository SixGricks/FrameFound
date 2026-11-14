"""Choosing the sample a comparison asks every model about, and freezing it.

A comparison is only worth anything if every model faces the same thing: a
model added next month must see exactly what the first one saw, after stills
have been regenerated, tags added and clips re-analysed. So the sample is
drawn once (`draw`), and then everything a model is shown is copied
(`freeze`): each item's pictures go to `<data_dir>/compare/<comparison id>/`,
and its facts sentence, the vocabulary and the ground truth are written onto
the comparison's rows by the caller.

`draw` only reads. It is also the preview the owner sees while still
choosing, so it never writes a row or a file, never grabs a still from a
proxy (a shot with no picture on this machine is left out and counted) and
never asks whether an original on the NAS is there: stills and previews are
on this machine, and requiring the originals would empty the sample whenever
the NAS is away.

Ground truth is what a person decided, as of the draw: tags a person added or
confirmed (yes) or rejected (no) on the footage, and which shots the counted
cuts used. Suggestions waiting in review are neither, and a draft cut is not
finished work (`shotlist.counts_as_used`).

Item `i` of `Drawn.items` is position `i` of the comparison: `freeze` names
its pictures by that index, and the rows must use the same one.
"""

import asyncio
import contextlib
import math
import os
import random
import re
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.ai import tag_review
from framefound.config import Settings, get_settings
from framefound.db.models import (
    Asset,
    AssetTag,
    Comparison,
    ComparisonItem,
    Cut,
    CutPiece,
    Derivative,
    Library,
    Listing,
    ListingItem,
    ShotWindow,
    Tag,
    catalogued,
)
from framefound.looking import prompt
from framefound.media import folders
from framefound.nle import shotlist
from framefound.storage import stores

DEFAULT_SIZE = 60
MIN_SIZE = 10
MAX_SIZE = 200
# A shorter window's three stills are one still.
MIN_SHOT_MS = 1000
# <data_dir>/compare/<comparison id>/
FOLDER = "compare"
# Ids asked about in one IN list (SQLite's bound-value limit).
CHUNK = shotlist.PIECES_CHUNK
# How many candidates `draw` looks for pictures of before it stops: the
# size times TRIES_PER_ITEM, and never fewer than MIN_TRIES. The draw is the
# preview a person waits on, and each shot costs up to three file checks and
# a frame query; in a library whose saved stills are gone (an analysis from
# before they were kept, a data folder not carried over) almost nothing has
# a picture, and an unbounded walk checked every shot in it per request.
TRIES_PER_ITEM = 5
MIN_TRIES = 200

KINDS = ("folder", "listing", "cuts", "comparison")
What = Literal["shots", "photos", "both"]
WHATS = ("shots", "photos", "both")
# The truth a person gave: these sources on asset_tags. "suggested" is a
# claim waiting for a person, never truth.
YES_SOURCES = ("manual", "confirmed")
NO_SOURCES = ("rejected",)

# Picture files, and nothing else, are ever removed from a comparison's
# folder: <position:04d>-<n>.jpg and its .partial.
_PICTURE = re.compile(r"\d{4,}-\d+\.jpg(\.partial)?")


class SampleError(ValueError):
    """The sample cannot be made as asked; the message is the sentence (HTTP 400)."""


@dataclass(frozen=True)
class SampleSpec:
    """What to draw. `what`, `size` and `stills` left as None mean "the
    usual": both, 60 and 3 — except for a sample of an earlier comparison,
    where they mean "as that comparison had them". A listing is always
    photographs and a cut sample always shots. `Drawn.spec` is this with
    every value filled in, and is what the comparison should record."""

    kind: Literal["folder", "listing", "cuts", "comparison"]
    library_id: uuid.UUID | None = None  # folder
    folder: str = ""  # folder: relative to the library; "" is all of it
    listing_id: uuid.UUID | None = None
    cut_ids: tuple[uuid.UUID, ...] = ()  # cuts
    neighbours: bool = True  # cuts: also the clips beside the used ones
    comparison_id: uuid.UUID | None = None  # comparison: the same items again
    what: What | None = None
    size: int | None = None
    stills: Literal[1, 3] | None = None
    # folder: which cuts count; None = every finished one touching it.
    truth_cut_ids: tuple[uuid.UUID, ...] | None = None
    seed: int = 0


@dataclass(frozen=True)
class Candidate:
    kind: Literal["photo", "shot"]
    asset_id: uuid.UUID
    library_name: str
    relative_path: str
    filename: str
    media_type: str
    start_ms: int | None
    end_ms: int | None
    facts: dict[str, Any]  # window_facts(window): for the page
    facts_line: str  # prompt.facts_line(window): what every model is told
    truth_yes: tuple[tuple[str, str], ...]  # (slug, name)
    truth_no: tuple[tuple[str, str], ...]
    used: bool | None  # None: a photograph, or no cut was counted
    used_in: tuple[str, ...]  # titles of the counted cuts that used it
    # (the moment the picture shows, in ms; the file to copy); filled by draw.
    pictures: tuple[tuple[int, Path], ...] = ()


@dataclass(frozen=True)
class Drawn:
    label: str  # "Shots used in Brookside Quick 30 - Finished Work - Rev 2"
    items: tuple[Candidate, ...]  # at most the size, in shooting order
    pool: dict[str, int]  # {"photos", "shots", "clips"}: what there was to choose from
    counted_cuts: tuple[tuple[uuid.UUID, str, str, datetime], ...]  # id, title, source, when
    left_out: dict[str, int]  # reason -> how many (left_out_sentence)
    vocabulary: tuple[tuple[str, int], ...]
    spec: SampleSpec = field(default_factory=lambda: SampleSpec(kind="folder"))


# --- the facts of a shot -------------------------------------------------------


def _finite(value: Any, places: int) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), places) if math.isfinite(value) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def window_facts(window: ShotWindow) -> dict[str, Any]:
    """What was measured about a shot, as it was that day, for the page:
    {"length_s", "move", "direction", "smooth", "energy", "light", "sky"}.
    A missing fact is None. Never `subjects` (another model's guess), never
    the file or the folder."""
    look = window.look if isinstance(window.look, dict) else {}
    length = None
    if isinstance(window.start_ms, int) and isinstance(window.end_ms, int):
        length = round(max(0, window.end_ms - window.start_ms) / 1000, 1)
    return {
        "length_s": length,
        "move": _text(window.move),
        "direction": _text(window.direction),
        "smooth": _finite(window.smooth, 2),
        "energy": _text(window.energy),
        "light": _text(window.light),
        "sky": _text(look.get("sky")),
    }


# --- the order the sample is taken in --------------------------------------------


def _key(candidate: Candidate) -> tuple[str, str, int, str]:
    start = candidate.start_ms if candidate.start_ms is not None else -1
    return (candidate.library_name, candidate.relative_path, start, str(candidate.asset_id))


def _decided(candidate: Candidate) -> bool:
    return bool(candidate.truth_yes or candidate.truth_no)


def _shuffled(candidates: Iterable[Candidate], rng: random.Random) -> list[Candidate]:
    # Sorted first, so the same seed draws the same sample whatever order
    # the database returned the rows in.
    out = sorted(candidates, key=_key)
    rng.shuffle(out)
    return out


def _round_robin(shots: Iterable[Candidate], rng: random.Random) -> list[Candidate]:
    """One shot from each clip in turn, clips and each clip's shots
    shuffled, so one long clip cannot fill the sample."""
    by_clip: dict[uuid.UUID, list[Candidate]] = {}
    for shot in sorted(shots, key=_key):
        by_clip.setdefault(shot.asset_id, []).append(shot)
    clips = sorted(by_clip, key=lambda clip: _key(by_clip[clip][0]))
    rng.shuffle(clips)
    for clip in clips:
        rng.shuffle(by_clip[clip])
    out: list[Candidate] = []
    for turn in range(max((len(clip) for clip in by_clip.values()), default=0)):
        out.extend(by_clip[clip][turn] for clip in clips if turn < len(by_clip[clip]))
    return out


def _thin(candidates: Sequence[Candidate], size: int) -> list[Candidate]:
    """`size` of them spread evenly along their order, first and last
    included (the front of the house and the back both stay), then the rest
    in order as stand-ins for any without a picture."""
    if len(candidates) <= size:
        return list(candidates)
    if size == 1:
        picked = [0]
    else:
        step = (len(candidates) - 1) / (size - 1)
        picked = [round(turn * step) for turn in range(size)]
    chosen = set(picked)
    return [candidates[i] for i in picked] + [
        candidate for i, candidate in enumerate(candidates) if i not in chosen
    ]


def _interleave(first: list[Candidate], second: list[Candidate]) -> list[Candidate]:
    out: list[Candidate] = []
    for turn in range(max(len(first), len(second))):
        out.extend(side[turn] for side in (first, second) if turn < len(side))
    return out


def _wanted(what: str, size: int) -> dict[str, int]:
    """How many of each a sample takes before either side may make up the
    other's shortfall. Used shots are at most half the shots while there
    are unused ones to show: a model is measured on telling them apart."""
    if what == "photos":
        return {"photo": size}
    shots = size if what == "shots" else size - size // 2
    wanted = {"shot": shots, "used": shots // 2}
    if what == "both":
        wanted["photo"] = size // 2
    return wanted


def _keeps_order(kind: str) -> bool:
    return kind in ("listing", "comparison")


def order(candidates: Sequence[Candidate], spec: SampleSpec) -> list[Candidate]:
    """The order candidates are tried in, best first; pure.

    A listing, or an earlier comparison, keeps its own order, thinned evenly
    when larger than the size. Otherwise, with `random.Random(spec.seed)`:
    shots in three passes — used (at most half the shots wanted), then
    unused ones of clips somebody decided tags on, then the rest, then the
    used ones over the half — each pass taking one shot per clip in turn.
    Photographs with decided tags first, then the rest, each shuffled. For
    `both`, shots and photographs alternate.
    """
    size = spec.size or DEFAULT_SIZE
    if _keeps_order(spec.kind):
        return _thin(candidates, size)
    rng = random.Random(spec.seed)  # noqa: S311 - drawing a sample, not a secret
    shots = [c for c in candidates if c.kind == "shot"]
    photos = [c for c in candidates if c.kind == "photo"]
    cap = _wanted(spec.what or "both", size).get("used", 0)
    used = _round_robin((s for s in shots if s.used), rng)
    decided = _round_robin((s for s in shots if not s.used and _decided(s)), rng)
    others = _round_robin((s for s in shots if not s.used and not _decided(s)), rng)
    shot_order = used[:cap] + decided + others + used[cap:]
    photo_order = _shuffled((p for p in photos if _decided(p)), rng) + _shuffled(
        (p for p in photos if not _decided(p)), rng
    )
    return _interleave(shot_order, photo_order)


# --- the pool -------------------------------------------------------------------


@dataclass
class _Pool:
    label: str
    spec: SampleSpec  # every value filled in
    candidates: list[Candidate]
    previews: dict[uuid.UUID, str]  # a photograph's ready preview, relative
    left_out: Counter[str]
    pool: dict[str, int]
    counted: list[Cut]


@dataclass
class _Shot:
    """A shot before its truth is known."""

    asset: Asset
    start_ms: int
    end_ms: int
    facts: dict[str, Any]
    facts_line: str


def _chunks(ids: Sequence[uuid.UUID]) -> Iterable[Sequence[uuid.UUID]]:
    for start in range(0, len(ids), CHUNK):
        yield ids[start : start + CHUNK]


def _like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _check(spec: SampleSpec) -> None:
    if spec.kind not in KINDS:
        raise SampleError("A sample comes from a folder, a listing, cuts or an earlier comparison.")
    if spec.what is not None and spec.what not in WHATS:
        raise SampleError("A sample is of shots, photographs or both.")
    if spec.size is not None and (
        isinstance(spec.size, bool)
        or not isinstance(spec.size, int)
        or not MIN_SIZE <= spec.size <= MAX_SIZE
    ):
        raise SampleError(f"A sample is between {MIN_SIZE} and {MAX_SIZE} items.")
    if spec.stills is not None and spec.stills not in (1, 3):
        raise SampleError("A shot is shown as one still or three.")
    if isinstance(spec.seed, bool) or not isinstance(spec.seed, int):
        raise SampleError("The sample's seed is a whole number.")


async def _library_names(db: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
    wanted = sorted(set(ids), key=str)
    names: dict[uuid.UUID, str] = {}
    for chunk in _chunks(wanted):
        rows = await db.execute(select(Library.id, Library.name).where(Library.id.in_(chunk)))
        names.update({uuid.UUID(str(i)): str(name) for i, name in rows.all()})
    return names


async def _previews(db: AsyncSession, asset_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Each photograph's ready preview: the picture a model is shown."""
    found: dict[uuid.UUID, str] = {}
    for chunk in _chunks(asset_ids):
        rows = await db.execute(
            select(Derivative.asset_id, Derivative.relative_path).where(
                Derivative.asset_id.in_(chunk),
                Derivative.kind == "preview",
                Derivative.status == "ready",
            )
        )
        found.update({uuid.UUID(str(a)): str(rel) for a, rel in rows.all()})
    return found


async def _windows(
    db: AsyncSession, videos: Sequence[Asset], left_out: Counter[str]
) -> list[_Shot]:
    """Every shot of these clips at least MIN_SHOT_MS long; clips with no
    shot analysis and shots too short to show are counted."""
    by_asset: dict[uuid.UUID, list[ShotWindow]] = {}
    for chunk in _chunks([video.id for video in videos]):
        rows = await db.execute(
            select(ShotWindow)
            .where(ShotWindow.asset_id.in_(chunk))
            .order_by(ShotWindow.asset_id, ShotWindow.start_ms)
        )
        for window in rows.scalars():
            by_asset.setdefault(window.asset_id, []).append(window)
    shots: list[_Shot] = []
    for video in videos:
        windows = by_asset.get(video.id, [])
        if not windows:
            left_out["no_shots"] += 1
        for window in windows:
            if window.end_ms - window.start_ms < MIN_SHOT_MS:
                left_out["short"] += 1
                continue
            shots.append(
                _Shot(
                    video,
                    window.start_ms,
                    window.end_ms,
                    window_facts(window),
                    prompt.facts_line(window),
                )
            )
    return shots


async def _truth(
    db: AsyncSession, asset_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]]:
    """The tags a person said yes or no to, per asset, by name."""
    yes: dict[uuid.UUID, list[tuple[str, str]]] = {}
    no: dict[uuid.UUID, list[tuple[str, str]]] = {}
    for chunk in _chunks(asset_ids):
        rows = await db.execute(
            select(AssetTag.asset_id, AssetTag.source, Tag.slug, Tag.name)
            .join(Tag, Tag.id == AssetTag.tag_id)
            .where(AssetTag.asset_id.in_(chunk), AssetTag.source.in_(YES_SOURCES + NO_SOURCES))
        )
        for asset_id, source, slug, name in rows.all():
            side = yes if source in YES_SOURCES else no
            side.setdefault(uuid.UUID(str(asset_id)), []).append((str(slug), str(name)))

    def tidy(rows: list[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(set(rows), key=lambda row: (row[1].lower(), row[0])))

    return {
        asset_id: (tidy(yes.get(asset_id, [])), tidy(no.get(asset_id, [])))
        for asset_id in set(yes) | set(no)
    }


async def _finished(db: AsyncSession, ids: Iterable[uuid.UUID]) -> list[Cut]:
    """These cuts, every one of which must exist and be finished work."""
    wanted = list(dict.fromkeys(ids))
    cuts = {
        cut.id: cut
        for cut in (await db.execute(select(Cut).where(Cut.id.in_(wanted)))).scalars().all()
    }
    if len(cuts) < len(wanted):
        raise SampleError("One of the chosen cuts is no longer in cut history.")
    finished = set(
        (
            await db.execute(select(Cut.id).where(Cut.id.in_(wanted), shotlist.counts_as_used()))
        ).scalars()
    )
    drafts = [cuts[i] for i in wanted if i not in finished]
    if drafts:
        raise SampleError(
            f"{_title(drafts[0])} is a draft, not finished work, so it cannot say "
            "which shots were used."
        )
    return sorted(cuts.values(), key=_when)


async def _cuts_touching(db: AsyncSession, video_ids: Sequence[uuid.UUID]) -> list[Cut]:
    """Every finished cut with a piece on one of these clips."""
    ids: set[uuid.UUID] = set()
    for chunk in _chunks(video_ids):
        rows = await db.execute(
            select(CutPiece.cut_id)
            .join(Cut, Cut.id == CutPiece.cut_id)
            .where(CutPiece.asset_id.in_(chunk), shotlist.counts_as_used())
            .distinct()
        )
        ids.update(uuid.UUID(str(i)) for i in rows.scalars())
    if not ids:
        return []
    cuts = (await db.execute(select(Cut).where(Cut.id.in_(list(ids))))).scalars().all()
    return sorted(cuts, key=_when)


def _when(cut: Cut) -> tuple[float, str]:
    """Oldest cut first; one with no time yet (not flushed) before them."""
    return (cut.created_at.timestamp() if cut.created_at else 0.0, str(cut.id))


def _title(cut: Cut) -> str:
    return cut.title.strip() or "An untitled cut"


async def _candidates(
    db: AsyncSession,
    photos: Sequence[Asset],
    shots: Sequence[_Shot],
    counted: Sequence[Cut],
) -> list[Candidate]:
    """Photographs and shots with what a person decided about them."""
    assets = {a.id: a for a in photos} | {s.asset.id: s.asset for s in shots}
    names = await _library_names(db, (a.library_id for a in assets.values()))
    truth = await _truth(db, sorted(assets, key=str))
    nothing: tuple[tuple[str, str], ...] = ()
    pieces: dict[uuid.UUID, list[shotlist.Piece]] = {}
    if counted:
        clips = sorted({s.asset.id for s in shots}, key=str)
        pieces = await shotlist.pieces_by_asset(db, clips, cut_ids=[cut.id for cut in counted])

    def common(asset: Asset) -> dict[str, Any]:
        yes, no = truth.get(asset.id, (nothing, nothing))
        return {
            "asset_id": asset.id,
            "library_name": names.get(asset.library_id, ""),
            "relative_path": asset.relative_path,
            "filename": asset.filename,
            "media_type": asset.media_type,
            "truth_yes": yes,
            "truth_no": no,
        }

    out = [
        Candidate(
            kind="photo",
            start_ms=None,
            end_ms=None,
            facts={},
            facts_line="",
            used=None,
            used_in=(),
            **common(photo),
        )
        for photo in photos
    ]
    for shot in shots:
        used_in: tuple[str, ...] = ()
        if counted:
            span = ShotWindow(start_ms=shot.start_ms, end_ms=shot.end_ms)
            used_in = tuple(shotlist.used_by(span, pieces.get(shot.asset.id, [])))
        out.append(
            Candidate(
                kind="shot",
                start_ms=shot.start_ms,
                end_ms=shot.end_ms,
                facts=shot.facts,
                facts_line=shot.facts_line,
                used=bool(used_in) if counted else None,
                used_in=used_in,
                **common(shot.asset),
            )
        )
    return out


def _filled(
    spec: SampleSpec, *, what: str | None = None, size: int | None = None, stills: int | None = None
) -> SampleSpec:
    """The spec with every value decided: those given here (what this kind
    of sample forces, or an earlier comparison had), else the spec's own,
    else the usual."""
    chosen = what or spec.what or "both"
    return replace(
        spec,
        what=cast(What, chosen if chosen in WHATS else "both"),
        size=size or spec.size or DEFAULT_SIZE,
        stills=1 if (stills or spec.stills) == 1 else 3,
    )


async def _from_folder(db: AsyncSession, spec: SampleSpec) -> _Pool:
    if spec.library_id is None:
        raise SampleError("Choose the library the folder is in.")
    library = await db.get(Library, spec.library_id)
    if library is None:
        raise SampleError("That library is no longer in the catalogue.")
    try:
        folder = folders.clean_folder(spec.folder)
        inside = folders.under(folder)
    except folders.FolderError as err:
        raise SampleError(str(err)) from err
    what = spec.what or "both"
    left_out: Counter[str] = Counter()
    media = {"shots": ("video",), "photos": ("image",), "both": ("video", "image")}[what]
    assets = (
        (
            await db.execute(
                select(Asset)
                .where(
                    Asset.library_id == library.id,
                    catalogued(),
                    Asset.media_type.in_(media),
                    *inside,
                )
                .order_by(Asset.relative_path)
            )
        )
        .scalars()
        .all()
    )
    images = [a for a in assets if a.media_type == "image"]
    previews = await _previews(db, [a.id for a in images])
    photos = [a for a in images if a.id in previews]
    left_out["no_picture"] += len(images) - len(photos)
    shots = await _windows(db, [a for a in assets if a.media_type == "video"], left_out)
    clips = sorted({s.asset.id for s in shots}, key=str)
    if spec.truth_cut_ids is not None:
        counted = await _finished(db, spec.truth_cut_ids) if spec.truth_cut_ids else []
    else:
        counted = await _cuts_touching(db, clips)
    where = f"{library.name} › {folder}" if folder else f"All of {library.name}"
    return _Pool(
        label=where,
        spec=_filled(replace(spec, folder=folder)),
        candidates=await _candidates(db, photos, shots, counted),
        previews=previews,
        left_out=left_out,
        pool={"photos": len(photos), "shots": len(shots), "clips": len(clips)},
        counted=counted,
    )


async def _from_listing(db: AsyncSession, spec: SampleSpec) -> _Pool:
    if spec.listing_id is None:
        raise SampleError("Choose a listing.")
    listing = await db.get(Listing, spec.listing_id)
    if listing is None:
        raise SampleError("That listing is no longer there.")
    rows = (
        await db.execute(
            select(ListingItem.frame_ms, Asset)
            .join(Asset, Asset.id == ListingItem.asset_id)
            .where(ListingItem.listing_id == listing.id, catalogued())
            .order_by(ListingItem.position, ListingItem.created_at, ListingItem.id)
        )
    ).all()
    left_out: Counter[str] = Counter()
    images: list[Asset] = []
    for frame_ms, asset in rows:
        if frame_ms is not None:
            left_out["video_frames"] += 1
        elif asset.media_type != "image":
            left_out["not_photos"] += 1
        else:
            images.append(asset)
    previews = await _previews(db, [a.id for a in images])
    photos = [a for a in images if a.id in previews]
    left_out["no_picture"] += len(images) - len(photos)
    return _Pool(
        label=f"The listing {listing.name}",
        spec=_filled(spec, what="photos"),
        candidates=await _candidates(db, photos, [], []),
        previews=previews,
        left_out=left_out,
        pool={"photos": len(photos), "shots": 0, "clips": 0},
        counted=[],
    )


async def _beside(db: AsyncSession, clips: Sequence[Asset]) -> list[Asset]:
    """The other clips directly in the folders these clips are in (not in
    the folders below)."""
    found: dict[uuid.UUID, Asset] = {}
    places = sorted({(c.library_id, c.relative_path.rpartition("/")[0]) for c in clips}, key=str)
    for library_id, folder in places:
        conditions: list[Any] = [
            Asset.library_id == library_id,
            Asset.media_type == "video",
            catalogued(),
        ]
        if folder:
            try:
                conditions += folders.under(folder)
            except folders.FolderError:
                continue  # a name the folder rules refuse; its clips still count
            deeper = _like_literal(folder + "/") + "%/%"
            conditions.append(~Asset.relative_path.like(deeper, escape="\\"))
        else:
            conditions.append(~Asset.relative_path.like("%/%"))
        for asset in (await db.execute(select(Asset).where(*conditions))).scalars():
            found[asset.id] = asset
    return list(found.values())


async def _from_cuts(db: AsyncSession, spec: SampleSpec) -> _Pool:
    if not spec.cut_ids:
        raise SampleError("Choose at least one cut.")
    counted = await _finished(db, spec.cut_ids)
    pieces = (
        await db.execute(
            select(CutPiece.asset_id).where(CutPiece.cut_id.in_([cut.id for cut in counted]))
        )
    ).scalars()
    piece_assets = list(pieces)
    named = sorted({uuid.UUID(str(a)) for a in piece_assets if a is not None}, key=str)
    there: dict[uuid.UUID, Asset] = {}
    for chunk in _chunks(named):
        for asset in (await db.execute(select(Asset).where(Asset.id.in_(chunk)))).scalars():
            there[asset.id] = asset
    left_out: Counter[str] = Counter()
    left_out["pieces_gone"] += sum(
        1 for a in piece_assets if a is None or uuid.UUID(str(a)) not in there
    )
    used = [a for a in there.values() if a.media_type == "video" and a.proxy_of_id is None]
    videos = {a.id: a for a in used}
    if spec.neighbours:
        videos.update({a.id: a for a in await _beside(db, used)})
    ordered = sorted(videos.values(), key=lambda a: (a.relative_path, str(a.id)))
    shots = await _windows(db, ordered, left_out)
    titles = [_title(cut) for cut in counted]
    if len(titles) == 1:
        label = f"Shots used in {titles[0]}"
    elif len(titles) == 2:
        label = f"Shots used in {titles[0]} and {titles[1]}"
    else:
        label = f"Shots used in {titles[0]}, {titles[1]} and {len(titles) - 2} more"
    if spec.neighbours:
        label += ", and the clips beside them"
    return _Pool(
        label=label,
        spec=_filled(spec, what="shots"),
        candidates=await _candidates(db, [], shots, counted),
        previews={},
        left_out=left_out,
        pool={"photos": 0, "shots": len(shots), "clips": len({s.asset.id for s in shots})},
        counted=counted,
    )


async def _from_comparison(db: AsyncSession, spec: SampleSpec) -> _Pool:
    if spec.comparison_id is None:
        raise SampleError("Choose the comparison to take the items from.")
    earlier = await db.get(Comparison, spec.comparison_id)
    if earlier is None:
        raise SampleError("That comparison is no longer there.")
    what = spec.what or earlier.what
    items = (
        (
            await db.execute(
                select(ComparisonItem)
                .where(ComparisonItem.comparison_id == earlier.id)
                .order_by(ComparisonItem.position)
            )
        )
        .scalars()
        .all()
    )
    kinds = {"shots": ("shot",), "photos": ("photo",)}.get(what, ("shot", "photo"))
    items = [item for item in items if item.kind in kinds]
    ids = sorted({item.asset_id for item in items if item.asset_id is not None}, key=str)
    there: dict[uuid.UUID, Asset] = {}
    for chunk in _chunks(ids):
        rows = await db.execute(select(Asset).where(Asset.id.in_(chunk), catalogued()))
        for asset in rows.scalars():
            there[asset.id] = asset
    windows: dict[tuple[uuid.UUID, int, int], ShotWindow] = {}
    for chunk in _chunks(sorted(there, key=str)):
        for window in (
            await db.execute(select(ShotWindow).where(ShotWindow.asset_id.in_(chunk)))
        ).scalars():
            windows[(window.asset_id, window.start_ms, window.end_ms)] = window
    previews = await _previews(db, sorted(there, key=str))
    left_out: Counter[str] = Counter()
    photos: list[Asset] = []
    shots: list[_Shot] = []
    # Each item keeps its place, photographs and shots alike.
    places: list[tuple[str, uuid.UUID, int | None]] = []
    for item in items:
        found = there.get(item.asset_id) if item.asset_id is not None else None
        if found is None:
            left_out["gone"] += 1
            continue
        asset = found
        if item.kind == "photo":
            if asset.id not in previews:
                left_out["no_picture"] += 1
                continue
            photos.append(asset)
            places.append(("photo", asset.id, None))
            continue
        if item.start_ms is None or item.end_ms is None:
            left_out["gone"] += 1
            continue
        # Re-read as of now when the same span is still a shot; a re-analysis
        # that moved the boundaries leaves the facts of this span as they
        # were measured, rather than another span's.
        same = windows.get((asset.id, item.start_ms, item.end_ms))
        facts = window_facts(same) if same is not None else dict(item.facts or {})
        line = prompt.facts_line(same) if same is not None else item.facts_line
        shots.append(_Shot(asset, item.start_ms, item.end_ms, facts, line))
        places.append(("shot", asset.id, item.start_ms))
    counted_ids = []
    for entry in earlier.counted_cuts or []:
        try:
            counted_ids.append(uuid.UUID(str(entry.get("id"))))
        except (AttributeError, ValueError):
            continue
    counted = (
        list((await db.execute(select(Cut).where(Cut.id.in_(counted_ids)))).scalars().all())
        if counted_ids
        else []
    )
    counted.sort(key=_when)
    made = await _candidates(db, photos, shots, counted)
    # _candidates puts photographs first; put everything back in its place.
    by_place = {(c.kind, c.asset_id, c.start_ms): c for c in made}
    candidates = [by_place[place] for place in places if place in by_place]
    size = spec.size or min(MAX_SIZE, max(MIN_SIZE, earlier.size or len(items) or DEFAULT_SIZE))
    return _Pool(
        label=f"The same items as {earlier.name}",
        spec=_filled(spec, what=what, size=size, stills=spec.stills or earlier.stills),
        candidates=candidates,
        previews=previews,
        left_out=left_out,
        pool={
            "photos": len(photos),
            "shots": len(shots),
            "clips": len({s.asset.id for s in shots}),
        },
        counted=counted,
    )


# --- drawing --------------------------------------------------------------------


async def _pictures(
    db: AsyncSession,
    candidate: Candidate,
    previews: dict[uuid.UUID, str],
    stills: int,
    settings: Settings,
) -> tuple[tuple[int, Path], ...]:
    """The pictures already on this machine for a candidate; () when none.
    Never grabs one."""
    if candidate.kind == "photo":
        relative = previews.get(candidate.asset_id)
        if relative is None:
            return ()
        path = stores.path_for(relative, settings)
        return ((0, path),) if await asyncio.to_thread(path.is_file) else ()
    if candidate.start_ms is None or candidate.end_ms is None:
        return ()
    # Imported here, as shotlist does: processing.shots brings the decode
    # path with it, and a module that only chooses should not load it.
    from framefound.processing.shots import window_moments

    start, middle, end = window_moments(candidate.start_ms, candidate.end_ms)
    moments = (middle,) if stills == 1 else (start, middle, end)
    found: list[tuple[int, Path]] = []
    for moment in dict.fromkeys(moments):
        picture = await shotlist.still_found(
            db, candidate.asset_id, moment, settings.data_dir, nearest=False
        )
        if picture is None:
            continue
        path, shown = picture
        # Two moments that come to the same file are one picture.
        if all(path != seen for _ms, seen in found):
            found.append((shown, path))
    return tuple(found)


def _groups(candidate: Candidate) -> tuple[str, ...]:
    if candidate.kind == "photo":
        return ("photo",)
    return ("shot", "used") if candidate.used else ("shot",)


async def draw(db: AsyncSession, spec: SampleSpec, settings: Settings | None = None) -> Drawn:
    """The sample, as it would be frozen now. Reads only.

    The pool (by kind of sample) is put in `order`, and candidates are taken
    in turn until `size` have a picture on this machine; a kind or the used
    shots that already have their share step back, and are taken only if
    the others run out. The chosen are then put in shooting order (by path,
    then time), except a listing and an earlier comparison, which keep
    their own. SampleError when nothing is left, saying why.

    At most `size * TRIES_PER_ITEM` (at least MIN_TRIES) candidates are
    looked at; when that runs out before the sample is full, the ones never
    looked at are counted as `unchecked`, not as `over`.
    """
    settings = settings or get_settings()
    _check(spec)
    build = {
        "folder": _from_folder,
        "listing": _from_listing,
        "cuts": _from_cuts,
        "comparison": _from_comparison,
    }[spec.kind]
    pool = await build(db, spec)
    filled = pool.spec
    size = filled.size or DEFAULT_SIZE
    stills = filled.stills or 3
    keep = _keeps_order(filled.kind)
    place = {id(candidate): i for i, candidate in enumerate(pool.candidates)}
    tried = order(pool.candidates, filled)
    caps = {} if keep else _wanted(filled.what or "both", size)

    chosen: list[tuple[int, Candidate]] = []
    taken: Counter[str] = Counter()
    later: list[Candidate] = []
    no_picture = 0
    tries = max(MIN_TRIES, size * TRIES_PER_ITEM)

    def done() -> bool:
        return len(chosen) >= size or len(chosen) + no_picture >= tries

    async def take(candidate: Candidate) -> None:
        nonlocal no_picture
        pictures = await _pictures(db, candidate, pool.previews, stills, settings)
        if not pictures:
            no_picture += 1
            return
        chosen.append((place[id(candidate)], replace(candidate, pictures=pictures)))
        taken.update(_groups(candidate))

    for candidate in tried:
        if done():
            break
        if any(taken[group] >= caps.get(group, size) for group in _groups(candidate)):
            later.append(candidate)
            continue
        await take(candidate)
    for candidate in later:
        if done():
            break
        await take(candidate)

    left_out = pool.left_out
    left_out["no_picture"] += no_picture
    # What was never looked at: not needed when the sample is full, unchecked
    # when the looking stopped first.
    rest = len(tried) - len(chosen) - no_picture
    left_out["over" if len(chosen) >= size else "unchecked"] += rest
    out = {reason: count for reason, count in left_out.items() if count > 0}
    if not chosen:
        reasons = [left_out_sentence(reason, count) for reason, count in out.items()]
        if not reasons:
            noun = {"shots": "shots", "photos": "photographs"}.get(
                filled.what or "both", "photographs or shots"
            )
            reasons = [f"it holds no {noun}."]
        raise SampleError("There is nothing to compare on here: " + " ".join(reasons))

    if keep:
        chosen.sort(key=lambda pair: pair[0])
    else:
        chosen.sort(key=lambda pair: _key(pair[1])[1:3] + (pair[1].library_name,))
    vocabulary = prompt.most_used(await tag_review.vocabulary(db))
    return Drawn(
        label=pool.label[:300],
        items=tuple(candidate for _place, candidate in chosen),
        pool=pool.pool,
        counted_cuts=tuple((cut.id, cut.title, cut.source, cut.created_at) for cut in pool.counted),
        left_out=out,
        vocabulary=tuple(vocabulary),
        spec=filled,
    )


def _count(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def left_out_sentence(reason: str, count: int, *, size: int | None = None) -> str:
    """What `Drawn.left_out[reason]` means, as a sentence. `size` is the
    sample's, for "over"."""
    one = count == 1
    match reason:
        case "no_shots":
            they = "it has" if one else "they have"
            clips = _count(count, "clip has", "clips have")
            return f"{clips} no shot analysis yet, so {they} no shots to show."
        case "no_picture":
            return f"{_count(count, 'item has', 'items have')} no picture on this machine yet."
        case "short":
            return f"{_count(count, 'shot is', 'shots are')} under a second long."
        case "video_frames":
            items = _count(count, "listing item is a frame", "listing items are frames")
            return f"{items} of a video."
        case "not_photos":
            items = _count(count, "listing item is not", "listing items are not")
            return f"{items} {'a photograph' if one else 'photographs'}."
        case "pieces_gone":
            pieces = _count(count, "piece of these cuts is", "pieces of these cuts are")
            clips = "a clip that is" if one else "clips that are"
            return f"{pieces} from {clips} no longer in the catalogue."
        case "gone":
            items = _count(count, "item", "items")
            are = "is" if one else "are"
            return f"{items} of the earlier comparison {are} no longer in the catalogue."
        case "over":
            needed = f"{count:,} more {'was' if one else 'were'} not needed"
            return f"{needed}: the sample is {size:,}." if size is not None else f"{needed}."
        case "unchecked":
            more = _count(count, "more was", "more were")
            return (
                f"{more} not looked at: too many of those tried first had no picture "
                "on this machine yet."
            )
    return f"{count:,} left out ({reason})."


# --- the frozen pictures ----------------------------------------------------------


def pictures_dir(comparison_id: uuid.UUID, settings: Settings | None = None) -> Path:
    """`<data_dir>/compare/<comparison id>/`. The name is the id read back
    as a UUID, so nothing a request says can make it another folder."""
    settings = settings or get_settings()
    return settings.data_dir / FOLDER / str(uuid.UUID(str(comparison_id)))


def picture_path(
    comparison_id: uuid.UUID, position: int, n: int, settings: Settings | None = None
) -> Path:
    """`<data_dir>/compare/<id>/<position:04d>-<n>.jpg`: worked out, never
    stored. `position` and `n` count from 0."""
    for value in (position, n):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("a picture is numbered by whole numbers from 0")
    return pictures_dir(comparison_id, settings) / f"{position:04d}-{n}.jpg"


def freeze(
    comparison_id: uuid.UUID, items: Sequence[Candidate], settings: Settings | None = None
) -> list[tuple[int, ...]]:
    """Copy each item's pictures into the comparison's folder, as the JPEG a
    model is sent (`tag_review.preview_from_file`: 768 px at most, quality
    80). Item `i` is position `i`. Blocking: call it in a thread.

    Returns, per item, the moments of the pictures it now has, in order —
    the item's `picture_ms`. A picture that cannot be read as one (half a
    JPEG left by an ffmpeg that was stopped) is skipped and the rest move
    up, so the files stay numbered 0, 1, 2; an item none of whose pictures
    could be read has none, and every run then records that.

    SampleError when this machine is short of space. Anything that stops
    it part-way removes what it wrote before passing the error on.
    """
    settings = settings or get_settings()
    from framefound.processing import derivatives as deriv

    try:
        deriv.ensure_space(settings.data_dir)
    except deriv.OutOfSpace as err:
        # Its own sentence: the derivatives' one talks about previews and
        # generation pausing, which is not what was being done here.
        raise SampleError(
            "There is not enough free space on this machine to keep the comparison's "
            f"pictures: nothing is written once less than {settings.min_free_gb:,.0f} GB "
            "is free. Make some room and try again."
        ) from err
    folder = pictures_dir(comparison_id, settings)
    frozen: list[tuple[int, ...]] = []
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for position, item in enumerate(items):
            kept: list[int] = []
            for moment, source in item.pictures:
                try:
                    data = tag_review.preview_from_file(source)
                except Exception:  # noqa: S112 - one unreadable still must not lose the sample
                    continue
                target = picture_path(comparison_id, position, len(kept), settings)
                partial = target.with_name(target.name + ".partial")
                partial.write_bytes(data)
                os.replace(partial, target)
                kept.append(moment)
            frozen.append(tuple(kept))
    except BaseException:
        drop_pictures(comparison_id, settings)
        raise
    return frozen


def read_pictures(
    comparison_id: uuid.UUID, position: int, count: int, settings: Settings | None = None
) -> list[bytes]:
    """An item's frozen pictures, in order; [] when any is gone. Every model
    is shown all of an item or none of it — never a different set from its
    neighbour's. Blocking."""
    pictures: list[bytes] = []
    for n in range(max(0, count)):
        try:
            pictures.append(picture_path(comparison_id, position, n, settings).read_bytes())
        except OSError:
            return []
    return pictures


def drop_pictures(comparison_id: uuid.UUID, settings: Settings | None = None) -> None:
    """Remove a comparison's pictures: only files named as `freeze` names
    them, only in its own folder, then the folder if that left it empty.
    Never a tree removal. Blocking."""
    folder = pictures_dir(comparison_id, settings)
    try:
        entries = list(os.scandir(folder))
    except FileNotFoundError:
        return
    for entry in entries:
        if _PICTURE.fullmatch(entry.name) and not entry.is_dir(follow_symlinks=False):
            try:
                os.unlink(entry.path)
            except FileNotFoundError:
                continue
    # Something that is not a picture may be in it: not ours to remove.
    with contextlib.suppress(OSError):
        folder.rmdir()
