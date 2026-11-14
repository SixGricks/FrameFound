"""What is in a scope, and what in it still needs looking at.

A scope is a folder of one library and everything beneath it ("" is the
whole library), optionally only what arrived since a moment (looking at
ingest). Everything here is a question for the catalogue: the paths, the
derived files and the shots are rows the scanner and the pipeline already
wrote, so nothing here lists a folder, stats a file or opens a picture. A
review of ten thousand files on a share that is asleep is counted from the
database in a few queries, and a scope whose share is away counts the same.

The unit of looking is an *item*: a photograph, one shot of a clip, or a
clip judged from one still when it has no shots to show (camera raw, which
can never have a playable copy here; a clip whose analysis found no shots;
a clip that could not be made ready, when the review asks for that).
Whether an item is done is read from the `looks` ledger, keyed by the
shot's times and never by a `shot_windows` id: those rows are rebuilt on
every re-analysis, and a shot found again at the same times keeps its look.

The asset row's roll-up (`looked_at`, `looked_by`) is the cheap first
filter — "nothing in it still needs looking at" — and the ledger decides
per item. Who may see which library is the API's question; a scope is
always one library, named by id.

What a pass will ask is written once in Python (`items_for`) and once in
SQL (`has_items`, and the counts under a `Run`); tests hold the two equal,
so a progress bar's total is what the pass hands out, and an item a run
has given up on never keeps its asset — or a job for it — coming back.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, case, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from framefound.db.models import Asset, Derivative, Look, ShotWindow, catalogued
from framefound.looking import INGEST_TRIES
from framefound.media import folders

# What the Looker can be shown. Sound files are in a folder, never looked at.
LOOKABLE = ("image", "video")
# A clip whose playable copy can never be made on this machine, so it is
# judged from its poster (processing/derivatives.py refuses BRAW proxies).
STILLS_ONLY_EXTENSIONS = ("braw",)
# The derived files that show a whole item, best first: a photograph's
# preview, else its thumbnail; a clip's poster, else the thumbnail made from
# it. One list for "can it be shown" (here) and "what is shown"
# (pictures.WHOLE), so the dialog never counts an item the pass cannot show.
WHOLE_PICTURES: dict[str, tuple[str, ...]] = {
    "image": ("preview", "thumbnail"),
    "video": ("poster", "thumbnail"),
}

ClipState = Literal["shots", "stills_only", "need_shots", "need_copy", "not_ready"]


@dataclass(frozen=True)
class Scope:
    """A folder of a library and everything beneath it.

    `folder` is cleaned on the way in (`folders.clean_folder`, so a bad path
    raises FolderError here, once), and "" is the whole library. `since` is
    for looking at ingest: only what was first indexed at or after it, so
    switching a library on never sweeps up its past."""

    library_id: uuid.UUID
    folder: str = ""
    since: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "folder", folders.clean_folder(self.folder))


@dataclass(frozen=True)
class Run:
    """One run of looking, for what it will and will not ask again.

    A review (`request_id`) never asks again what failed in it — a new
    review is how a failed item is tried again. Looking at ingest
    (`request_id` None) gives an item `INGEST_TRIES` tries under a key, and
    then leaves it be. `stills_fallback` is the review's last pass: clips
    that never became ready are judged from one still. These are
    `items_for`'s arguments, for the SQL that must agree with it."""

    request_id: uuid.UUID | None
    stills_fallback: bool = False


@dataclass(frozen=True)
class Item:
    """One question for the model: a photograph, a shot, or a whole clip
    judged from one still (`start_ms` and `end_ms` both None)."""

    asset_id: uuid.UUID
    filename: str
    media_type: str
    start_ms: int | None
    end_ms: int | None
    window: ShotWindow | None

    @property
    def span(self) -> tuple[int | None, int | None]:
        """The key of this item in the `looks` ledger, beside its asset."""
        return self.start_ms, self.end_ms

    @property
    def middle_ms(self) -> int | None:
        """Where a suggestion about this item opens: the middle of the shot,
        None for a whole item."""
        if self.start_ms is None or self.end_ms is None:
            return None
        return (self.start_ms + self.end_ms) // 2


@dataclass(frozen=True)
class Counts:
    """What a scope holds, as the review dialog says it.

    `photos`, `shots`, `stills_only`, `already`, `by_other` and
    `failed_before` count items (a clip of twenty shots is twenty); the
    rest count files."""

    photos: int  # photographs to look at
    clips: int  # clips with at least one shot to look at
    shots: int  # shots to look at, in those clips
    stills_only: int  # clips to be judged from one still (camera raw; no shots found)
    already: int  # items that need nothing
    by_other: int  # items whose current look is not under this key (what "redo" adds)
    need_copy: int  # clips with no playable copy yet, that one can be made for now
    need_copy_footage_s: float  # their length, for the estimate of making copies
    copy_away: int  # clips with no playable copy whose original's share is away
    need_shots: int  # clips with a copy whose shots have not been found
    not_ready: int  # still being processed: nothing to show the model yet
    missing: int  # the file is gone (would need looking at otherwise)
    audio: int  # sound files: never looked at
    failed_before: int  # of the items to look at: tried before and failed
    # `already` and `by_other` by kind, as "Not looked at yet" is said:
    # photographs, clips, and the shots in those clips. A sum in items alone
    # read "12 items" in a folder of 8 files (6 photographs, and 2 clips
    # whose 6 shots were counted one by one). `*_clips` counts each clip once,
    # whether it was judged whole or has at least one shot counted here; a
    # clip partly looked at is in the clips to do as well. Zero unless
    # `count` filled them in.
    already_photos: int = 0
    already_clips: int = 0
    already_shots: int = 0
    other_photos: int = 0
    other_clips: int = 0
    other_shots: int = 0

    @property
    def items(self) -> int:
        """Items that could be looked at now: photographs, shots, and clips
        judged from one still."""
        return self.photos + self.shots + self.stills_only


def clip_state(
    *,
    has_motion: bool,
    has_proxy: bool,
    has_poster: bool,
    extension: str,
    has_windows: bool,
) -> ClipState:
    """How far a clip is from being looked at, from its derived files.

    - its shots have been found (a ready `motion` file, and windows) →
      `shots`; when the analysis found none, it is judged from its poster
      like camera raw;
    - camera raw → `stills_only` (it can never have a playable copy here);
    - a playable copy but no shots yet → `need_shots`;
    - a poster but no playable copy → `need_copy`;
    - otherwise it is still being processed → `not_ready` (also camera raw
      and a shot-less clip with no poster: there is nothing to show).

    A clip with windows and no ready `motion` file (a seeded catalogue, an
    analysis file lost or failed) is not ready by this rule: a review makes
    it ready first, which analyses it again. What a run hands out for it is
    `judged_now`'s question.

    `has_poster` is "a still of the whole clip is ready": any of
    `WHOLE_PICTURES["video"]`. `scope._state_sql` is this rule in SQL; a
    test holds the two equal."""
    if has_motion and has_windows:
        return "shots"
    if has_motion or extension.lower() in STILLS_ONLY_EXTENSIONS:
        return "stills_only" if has_poster else "not_ready"
    if has_proxy:
        return "need_shots"
    if has_poster:
        return "need_copy"
    return "not_ready"


def judged_now(stills_fallback: bool) -> tuple[ClipState, ...]:
    """The clip states a run looks at without waiting: clips whose shots
    were found and those judged from one still (camera raw, no shots found),
    and — on a review's last pass — the clips that never became ready.

    Such a clip is its shots whenever it has windows, and one item of the
    whole only when it has none (`items_for`). A look of the whole of a clip
    with windows is never current (`store._is_item`), so handing one out
    whole would keep it wanted for ever: that is how a review of clips with
    windows and a failed or missing analysis file asked about them again
    and again in its last pass ("20 of 8") and never ended. Their windows,
    the stills of them and their facts are on record, so their shots are
    what is looked at."""
    if stills_fallback:
        return ("shots", "stills_only", "need_copy", "need_shots")
    return ("shots", "stills_only")


# --- conditions -------------------------------------------------------------------


def _place(scope: Scope) -> list[ColumnElement[bool]]:
    """The library, the folder and everything beneath it, the time window:
    every catalogued file there, whatever it is and whether or not it is
    there."""
    conditions: list[ColumnElement[bool]] = [
        Asset.library_id == scope.library_id,
        catalogued(),
        *folders.under(scope.folder),
    ]
    if scope.since is not None:
        conditions.append(Asset.first_indexed_at >= scope.since)
    return conditions


def within(scope: Scope) -> list[ColumnElement[bool]]:
    """The conditions for "a file in this scope the Looker could look at":
    the scope's place; a photograph or a clip; not a file that is gone.

    A file whose share is away (`unmounted`) is in: looking reads only what
    FrameFound derived from it, on this machine. One the scanner found gone
    (`missing`) is out — its derived files may be all that is left, and a
    caption for a file that no longer exists helps nobody."""
    return [
        *_place(scope),
        Asset.media_type.in_(LOOKABLE),
        Asset.availability != "missing",
    ]


def needs_work(key: str, redo: bool) -> ColumnElement[bool]:
    """The asset-level filter: something in it may still need looking at.

    Without redo, anything not yet wholly looked at. With redo, also
    anything looked at under another build or question (`looked_by` is
    another key, or "mixed"). `looked_at` is the roll-up's "nothing left"
    (a clip with one shot still to do has a score and no `looked_at`), so
    this never skips a clip half done.

    It is only the first filter: an item a run has given up on keeps its
    asset here for good. A pass, and anything that decides whether a pass
    is worth starting, adds `has_items`."""
    if not redo:
        return Asset.looked_at.is_(None)
    return or_(Asset.looked_at.is_(None), Asset.looked_by.is_(None), Asset.looked_by != key)


def _ready(*kinds: str) -> ColumnElement[bool]:
    return exists().where(
        Derivative.asset_id == Asset.id,
        Derivative.kind.in_(kinds),
        Derivative.status == "ready",
    )


def _state_sql() -> ColumnElement[str]:
    """`clip_state` as a column of a query over `Asset` — also one over the
    windows joined to their asset (`count`'s shots)."""
    has_motion = _ready("motion")
    has_poster = _ready(*WHOLE_PICTURES["video"])
    # An alias, so that inside a query over `shot_windows` this EXISTS still
    # has a table of its own: with the plain table it would be correlated
    # away entirely, and SQLAlchemy refuses a SELECT with no FROM.
    window = aliased(ShotWindow)
    has_windows = exists().where(window.asset_id == Asset.id)
    stills_only = or_(has_motion, Asset.extension.in_(STILLS_ONLY_EXTENSIONS))
    return case(
        (and_(has_motion, has_windows), literal("shots")),
        (and_(stills_only, has_poster), literal("stills_only")),
        (stills_only, literal("not_ready")),
        (_ready("proxy"), literal("need_shots")),
        (has_poster, literal("need_copy")),
        else_=literal("not_ready"),
    )


Span = Literal["whole", "shot"]


def _on_span(span: Span) -> list[ColumnElement[bool]]:
    """Ledger rows of a span: the whole item of the `Asset` row in the query,
    or the shot of the `ShotWindow` row in the query.

    A shot's rows are matched on the window's own asset id, not `Asset.id`:
    an EXISTS correlates only with the query right around it, and inside
    `has_items`' EXISTS over the windows `Asset` would be joined in afresh
    — every asset's looks would then count for every window."""
    if span == "whole":
        return [Look.asset_id == Asset.id, Look.start_ms.is_(None), Look.end_ms.is_(None)]
    return [
        Look.asset_id == ShotWindow.asset_id,
        Look.start_ms == ShotWindow.start_ms,
        Look.end_ms == ShotWindow.end_ms,
    ]


def _look_on(span: Span, *, ok: bool, key: str | None = None) -> ColumnElement[bool]:
    """A look on this asset's span: a current, usable one (`ok`) — under
    `key` when given — or a failed one."""
    conditions = _on_span(span)
    if ok:
        conditions += [Look.ok.is_(True), Look.current.is_(True)]
        if key is not None:
            conditions.append(Look.key == key)
    else:
        conditions.append(Look.ok.is_(False))
    return exists().where(*conditions)


def _look_by_other(span: Span, key: str) -> ColumnElement[bool]:
    """The span's current, usable look is not under `key` (another build or
    another question): what redo would ask again."""
    return exists().where(
        *_on_span(span), Look.ok.is_(True), Look.current.is_(True), Look.key != key
    )


def _given_up(span: Span, *, key: str, run: Run) -> ColumnElement[bool]:
    """`run` will not ask about this span again: it failed in this review,
    or — looking at ingest — it has failed `INGEST_TRIES` times under `key`."""
    if run.request_id is not None:
        return exists().where(
            *_on_span(span), Look.ok.is_(False), Look.request_id == run.request_id
        )
    tries = (
        select(func.count())
        .select_from(Look)
        .where(*_on_span(span), Look.ok.is_(False), Look.key == key)
        .scalar_subquery()
    )
    return tries >= INGEST_TRIES


def _wanted(span: Span, *, key: str, redo: bool, run: Run | None) -> ColumnElement[bool]:
    """`items_for`'s rule for one span, in SQL: no current look that counts
    (any key's without redo; with redo, one under `key`), and — for a run —
    not given up. Without a run it is a new review's question, which tries
    every failure again."""
    wanted = ~_look_on(span, ok=True, key=key if redo else None)
    if run is None:
        return wanted
    return and_(wanted, ~_given_up(span, key=key, run=run))


def has_items(key: str, redo: bool, run: Run) -> ColumnElement[bool]:
    """The asset gives at least one item to this run: `items_for(asset,
    key=key, redo=redo, request_id=run.request_id,
    stills_fallback=run.stills_fallback)` is not empty. A test holds the two
    equal.

    Use it with `within(scope)` and `needs_work`. A clip still being made
    ready gives nothing yet, and an item the run has given up on gives
    nothing again, so a pass — or the scanner deciding whether to start
    one — does not keep coming back to an asset that has nothing left."""
    whole_wanted = _wanted("whole", key=key, redo=redo, run=run)
    shot_wanted = _wanted("shot", key=key, redo=redo, run=run)
    return or_(
        and_(Asset.media_type == "image", _ready(*WHOLE_PICTURES["image"]), whole_wanted),
        and_(
            Asset.media_type == "video",
            _state_sql().in_(judged_now(run.stills_fallback)),
            or_(
                exists().where(ShotWindow.asset_id == Asset.id, shot_wanted),
                and_(~exists().where(ShotWindow.asset_id == Asset.id), whole_wanted),
            ),
        ),
    )


def _one_if(condition: ColumnElement[bool]) -> Any:
    return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)


def _flag(condition: ColumnElement[bool]) -> Any:
    return case((condition, 1), else_=0)


# --- counting --------------------------------------------------------------------


async def count(
    db: AsyncSession, scope: Scope, *, key: str, redo: bool, run: Run | None = None
) -> Counts:
    """What the scope holds and what of it still needs looking at under
    `key` (with `redo`, also what another build or question looked at).

    Without `run` it is the dialog's question — a new review, which tries
    every earlier failure again. With one, what that run has given up on is
    not counted as still to look at (and not as `already` either).

    Three queries over the catalogue: the photographs (and the files that
    are never looked at), the clips by how far they are from being looked
    at, and the shots. Counted, never listed: no path is read into Python
    and nothing on disk is touched, so this is safe behind a dialog on a
    library of any size."""
    todo = needs_work(key, redo)
    whole: Span = "whole"
    counted = key if redo else None
    # An item to look at: something in its asset still needs work, and the
    # ledger has no look that counts (the roll-up should already say so; the
    # ledger is asked as well, so an item looked at mid-way through a
    # roll-up is never offered twice).
    whole_todo = and_(todo, _wanted(whole, key=key, redo=redo, run=run))
    # An item that needs nothing: its asset is wholly looked at, or its own
    # look counts.
    whole_already = or_(~todo, _look_on(whole, ok=True, key=counted))
    whole_other = _look_by_other(whole, key)

    photo = and_(Asset.media_type == "image", Asset.availability != "missing")
    photo_ready = _ready(*WHOLE_PICTURES["image"])
    photo_todo = and_(photo, whole_todo)
    assets = (
        await db.execute(
            select(
                _one_if(and_(photo_todo, photo_ready)),
                _one_if(and_(photo_todo, photo_ready, _look_on(whole, ok=False))),
                _one_if(and_(photo_todo, ~photo_ready)),
                _one_if(and_(photo, whole_already)),
                _one_if(and_(photo, whole_other)),
                _one_if(
                    and_(
                        Asset.media_type.in_(LOOKABLE),
                        Asset.availability == "missing",
                        todo,
                    )
                ),
                _one_if(Asset.media_type == "audio"),
            ).where(*_place(scope))
        )
    ).one()
    photos, photos_failed, photos_not_ready, photos_already, photos_other, missing, audio = (
        int(value) for value in assets
    )

    # What this run looks at without waiting (the dialog's count is a first
    # pass's): a clip in one of these states is its shots when it has
    # windows, one item of the whole when it has none.
    now = judged_now(run is not None and run.stills_fallback)
    has_windows = exists().where(ShotWindow.asset_id == Asset.id)

    # Clips, by how far each is from being looked at, whether its original
    # is reachable (a playable copy is made from the original), and whether
    # it has windows. Grouped through a subquery: the same CASE in SELECT
    # and GROUP BY carries different bound parameters, which PostgreSQL
    # refuses.
    clip_rows = (
        select(
            _state_sql().label("state"),
            _flag(Asset.availability == "unmounted").label("away"),
            _flag(has_windows).label("windowed"),
            _flag(whole_todo).label("todo"),
            case((whole_todo, func.coalesce(Asset.duration_s, 0.0)), else_=0.0).label("seconds"),
            _flag(and_(whole_todo, _look_on(whole, ok=False))).label("failed"),
            _flag(whole_already).label("already"),
            _flag(whole_other).label("other"),
        )
        .where(*within(scope), Asset.media_type == "video")
        .subquery()
    )
    by_state: dict[tuple[str, bool, bool], tuple[int, float, int, int, int]] = {}
    for state, away, windowed, clips_todo, seconds, failed, already, other in (
        await db.execute(
            select(
                clip_rows.c.state,
                clip_rows.c.away,
                clip_rows.c.windowed,
                func.coalesce(func.sum(clip_rows.c.todo), 0),
                func.coalesce(func.sum(clip_rows.c.seconds), 0.0),
                func.coalesce(func.sum(clip_rows.c.failed), 0),
                func.coalesce(func.sum(clip_rows.c.already), 0),
                func.coalesce(func.sum(clip_rows.c.other), 0),
            ).group_by(clip_rows.c.state, clip_rows.c.away, clip_rows.c.windowed)
        )
    ).all():
        by_state[(str(state), bool(away), bool(windowed))] = (
            int(clips_todo),
            float(seconds),
            int(failed),
            int(already),
            int(other),
        )

    def clips(state: str, field: int, *, away: bool | None = None) -> Any:
        """One column of the clips in `state` that are not counted by their
        shots below (both kinds when `away` is None): in a state looked at
        now, only those with no windows."""
        sides = (False, True) if away is None else (away,)
        windowed = (False,) if state in now else (False, True)
        return sum(
            by_state.get((state, side, has), (0, 0.0, 0, 0, 0))[field]
            for side in sides
            for has in windowed
        )

    # A clip judged as a whole (camera raw, no shots found, or the stills
    # fallback) is one item; one with windows that is looked at now is its
    # shots, below.
    whole_states = ("stills_only", "need_shots", "need_copy", "not_ready")
    clips_already = sum(clips(state, 3) for state in whole_states)
    clips_other = sum(clips(state, 4) for state in whole_states)

    # Shots, in the clips looked at now that have windows: those whose
    # shots have been found, and on the last pass those that never became
    # ready (`judged_now`).
    span: Span = "shot"
    shot_todo = and_(todo, _wanted(span, key=key, redo=redo, run=run))
    shot_already = or_(~todo, _look_on(span, ok=True, key=counted))
    shot_other = _look_by_other(span, key)

    def clips_with(condition: ColumnElement[bool]) -> Any:
        return func.count(func.distinct(case((condition, ShotWindow.asset_id))))

    (
        shot_count,
        shot_clips,
        shots_failed,
        shots_already,
        shot_clips_already,
        shots_other,
        shot_clips_other,
    ) = (
        int(value)
        for value in (
            await db.execute(
                select(
                    _one_if(shot_todo),
                    clips_with(shot_todo),
                    _one_if(and_(shot_todo, _look_on(span, ok=False))),
                    _one_if(shot_already),
                    clips_with(shot_already),
                    _one_if(shot_other),
                    clips_with(shot_other),
                )
                .select_from(ShotWindow)
                .join(Asset, Asset.id == ShotWindow.asset_id)
                .where(*within(scope), Asset.media_type == "video", _state_sql().in_(now))
            )
        ).one()
    )

    return Counts(
        photos=photos,
        clips=shot_clips,
        shots=shot_count,
        stills_only=clips("stills_only", 0),
        already=photos_already + shots_already + clips_already,
        by_other=photos_other + shots_other + clips_other,
        need_copy=clips("need_copy", 0, away=False),
        need_copy_footage_s=round(clips("need_copy", 1, away=False), 3),
        copy_away=clips("need_copy", 0, away=True),
        need_shots=clips("need_shots", 0),
        not_ready=photos_not_ready + clips("not_ready", 0),
        missing=missing,
        audio=audio,
        failed_before=photos_failed + shots_failed + clips("stills_only", 2),
        already_photos=photos_already,
        already_clips=clips_already + shot_clips_already,
        already_shots=shots_already,
        other_photos=photos_other,
        other_clips=clips_other + shot_clips_other,
        other_shots=shots_other,
    )


async def remaining(db: AsyncSession, scope: Scope, *, key: str, redo: bool, run: Run) -> int:
    """How many items `run` would still hand out: the sum of `items_for`
    over `next_assets` — the rest of a review's progress bar. What failed in
    this review, or ran out of tries at ingest, is not in it; on the stills
    fallback, clips that never became ready are (one item each, or their
    shots when they have windows: `count` already counts those as shots)."""
    counts = await count(db, scope, key=key, redo=redo, run=run)
    fallback = counts.need_copy + counts.copy_away + counts.need_shots
    return counts.items + (fallback if run.stills_fallback else 0)


# --- walking ---------------------------------------------------------------------


async def next_assets(
    db: AsyncSession,
    scope: Scope,
    *,
    key: str,
    redo: bool,
    run: Run,
    after: str = "",
    limit: int = 20,
) -> list[Asset]:
    """The next page of assets that give `run` something to look at, in
    path order, after the path `after` ("" = from the start).

    Only assets with an item for this run (`has_items`): one whose every
    item failed in this review, or ran out of tries at ingest, is not handed
    out again, nor is a clip still being made ready.

    Paged on the path, not by offset: what a pass looks at drops out of the
    filter as it goes, so an offset would skip; the path is unique in a
    library, so a pass never repeats an asset. A pass starts again from ""
    each time the job is re-entered — nothing is remembered between runs."""
    rows = await db.execute(
        select(Asset)
        .where(
            *within(scope),
            needs_work(key, redo),
            has_items(key, redo, run),
            Asset.relative_path > after,
        )
        .order_by(Asset.relative_path)
        .limit(max(1, limit))
    )
    return list(rows.scalars().all())


async def _ready_kinds(db: AsyncSession, asset_id: uuid.UUID) -> set[str]:
    rows = await db.execute(
        select(Derivative.kind).where(Derivative.asset_id == asset_id, Derivative.status == "ready")
    )
    return {str(kind) for kind in rows.scalars().all()}


async def items_for(
    db: AsyncSession,
    asset: Asset,
    *,
    key: str,
    redo: bool,
    request_id: uuid.UUID | None,
    stills_fallback: bool,
) -> list[Item]:
    """What to ask the model about this asset in this run, in order.

    An item is left out when its current look already counts (any build's
    without redo; with redo, one under `key`), or when it failed here
    before: in this review (`request_id`), or — looking at ingest, which
    has no review — `INGEST_TRIES` times under `key`. A new review is how a
    failed item is tried again.

    A photograph is one item once it has a picture (its preview or
    thumbnail). A clip whose shots have been found is one item per shot, in
    order; one whose analysis found none, and camera raw, are one item
    judged from the poster. With `stills_fallback`, so is a clip that never
    became ready (no playable copy, or no shots found from it) — or, when it
    has shot windows all the same, one item per shot: a clip with windows is
    never one item of the whole, which `store` would not count for it
    (`judged_now`). Anything else gives no items yet.

    `has_items` is this rule in SQL; a test holds the two equal."""
    if asset.media_type not in LOOKABLE or asset.availability == "missing":
        return []
    ready = await _ready_kinds(db, asset.id)
    looks: Any = (
        await db.execute(
            select(
                Look.start_ms, Look.end_ms, Look.ok, Look.current, Look.key, Look.request_id
            ).where(Look.asset_id == asset.id)
        )
    ).all()

    def wanted(start: int | None, end: int | None) -> bool:
        on_span = [row for row in looks if (row.start_ms, row.end_ms) == (start, end)]
        if any(row.ok and row.current and (not redo or row.key == key) for row in on_span):
            return False
        failures = [row for row in on_span if not row.ok]
        if request_id is not None:
            return not any(row.request_id == request_id for row in failures)
        return sum(1 for row in failures if row.key == key) < INGEST_TRIES

    def whole() -> list[Item]:
        if not wanted(None, None):
            return []
        return [Item(asset.id, asset.filename, asset.media_type, None, None, None)]

    if asset.media_type == "image":
        return whole() if ready & set(WHOLE_PICTURES["image"]) else []

    windows: Sequence[ShotWindow] = (
        (
            await db.execute(
                select(ShotWindow)
                .where(ShotWindow.asset_id == asset.id)
                .order_by(ShotWindow.idx, ShotWindow.start_ms)
            )
        )
        .scalars()
        .all()
    )
    state = clip_state(
        has_motion="motion" in ready,
        has_proxy="proxy" in ready,
        has_poster=bool(ready & set(WHOLE_PICTURES["video"])),
        extension=asset.extension,
        has_windows=bool(windows),
    )
    if state not in judged_now(stills_fallback):
        return []
    if windows:
        return [
            Item(asset.id, asset.filename, asset.media_type, w.start_ms, w.end_ms, w)
            for w in windows
            if wanted(w.start_ms, w.end_ms)
        ]
    return whole()


async def not_ready_clips(
    db: AsyncSession, scope: Scope, *, key: str = "", redo: bool = False
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Clips in the scope that still need looking at and are not ready to
    be: (those a playable copy can be made for now, those with a copy whose
    shots have not been found), each in path order.

    Camera raw is in neither: it can never have a copy here and is judged
    from its poster. Nor is a clip with no copy whose original is on a share
    that is away (`Counts.copy_away`): a copy is made from the original, so
    asking for one would only be skipped, and a review would wait hours for
    copies that never come. It is judged from one still with the rest that
    never became ready.

    Without `key`, "still need looking at" is "not wholly looked at"."""
    states = (
        select(
            Asset.id.label("id"),
            _state_sql().label("state"),
            Asset.availability.label("availability"),
            Asset.relative_path,
        )
        .where(*within(scope), Asset.media_type == "video", needs_work(key, redo))
        .subquery()
    )
    rows = await db.execute(
        select(states.c.id, states.c.state)
        .where(
            or_(
                states.c.state == "need_shots",
                and_(states.c.state == "need_copy", states.c.availability != "unmounted"),
            )
        )
        .order_by(states.c.relative_path)
    )
    need_copy: list[uuid.UUID] = []
    need_shots: list[uuid.UUID] = []
    for asset_id, state in rows.all():
        (need_copy if state == "need_copy" else need_shots).append(asset_id)
    return need_copy, need_shots
