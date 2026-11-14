"""The Looker's work on L1's lane: a folder review, making its clips ready,
and looking at what arrives in a library.

Three kinds of job, all re-entrant:

- `look_folder` — a review somebody asked for (below).
- `look_prepare` — beside a review whose clips are not ready: queues their
  playable copies and shot analysis on the ordinary pipeline, each clip
  once, then watches them arrive. It runs no model, so Pause does not hold
  it (D11). It ends with its review, whichever ends first.
- `look_ingest` — one per library whose "look at new footage" switch is
  on, created by `nudge` (the pipeline's hook, as each file is embedded or
  its shots described) or by `ensure_ingest` (the scanner's safety net). It
  looks at what came in since the switch went on, and ends when nothing is
  left; the next arrival starts another (P19). Not while the Looker cannot
  be used: neither makes a job for a Looker that may not look, and after a
  job failed on the Looker itself the library is left alone for a while
  (`INGEST_HOLD`), since nothing is recorded against a file for that. The
  same after a job that stopped its worker `INGEST_DEATHS` times in a row
  without looking at anything (`look_ingest_abandoned`).

A review is one `look_requests` row (what was asked, how far it got) and
one `look_folder` job. The job's handler keeps nothing that matters in
memory: every time it is run — the first time, after stepping aside for a
person's Test, after the overnight window closed, after Resume, after the
worker was restarted — it asks the database what in the folder still needs
looking at and carries on from there (P7). So one row is watched and
cancelled however many times it hands the lane back, and no second row is
ever made to continue the first.

Per item: the pictures already on this machine (never grabbed from a
proxy), one question to the model through `ask()` — called bare: L1's
runner keeps the job's heartbeat while a load or an answer takes minutes —
then the look, its tag suggestions and the asset's roll-up, committed
together. A failed item is recorded and the review goes on; it is tried
again by the next review, never by this one (D7).

The request row is written by three parties (this job, the make-ready job,
the API's Cancel), so every write here names its columns, and every write
that ends the review is guarded on `state == 'active'`: a Cancel pressed
while an item was being answered stands.

`ctx.deps.now()` is the Mac's wall clock, naive (L1's D24): the overnight
window is read from it and handed to L1's `schedule` and `jobs` as they
expect; what is written to the time-zoned columns is UTC.
"""

import asyncio
import contextlib
import json
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

import structlog
from sqlalchemy import exists, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.ai import tag_review
from framefound.ai.tagging import slugify
from framefound.config import Settings, get_settings
from framefound.db.models import Asset, Library, LocalAiJob, LocalModel, Look, LookRequest
from framefound.local_models import availability, jobs, registry, schedule
from framefound.local_models.ask import NO_SUCH_MODEL, Answer, ModelFailed, NotNow, ask
from framefound.local_models.kinds import kinds as runtime_kinds
from framefound.local_models.settings_store import load_config as load_local_ai_config
from framefound.looking import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_NEW_TAGS_PER_REVIEW,
    PREPARE_EVERY_S,
    PREPARE_STALLED_S,
    PRIORITY_INGEST,
    SLICE_SECONDS,
    demo,
    pictures,
    prompt,
    reviews,
    scope,
    store,
)
from framefound.looking.settings_store import load_config
from framefound.storage import stores

log = structlog.get_logger()

LOOK_KIND = reviews.LOOK_KIND
PREPARE_KIND = reviews.PREPARE_KIND
INGEST_KIND = reviews.INGEST_KIND

# --- the sentences ------------------------------------------------------------------

STEPPED_ASIDE = "Stepped aside for something more urgent. It carries on by itself."
NO_SHOT_PICTURE = "There is no picture of this shot to show the model."
NO_PICTURE = "There is no picture of it to show the model."
RESTARTED = "The worker running it was restarted; it carries on where it was."
NOTHING_LEFT = "Nothing here needed looking at."
# An item whose answer did not count as its look (or that was handed out
# again after its answer was counted): asking again would only get another
# answer that does not count, for ever. It is counted as not looked at, and
# the review goes on. The answer itself stays on record (and any tags it
# suggested stay in Tag review), so the sentence says it did not count, not
# that it was lost.
NOT_AGAIN = (
    "It was asked about once and its answer did not count as its look, so it was not asked again."
)
# A model that will not load, or whose files are not whole, fails every item
# alike and is no item's fault: the review stops at the first such answer
# and records nothing against the item, so a new review asks it again once
# the model loads (compare_job does the same).
_AT_ONCE = frozenset({"load_failed", "bad_files"})
QUEUE_DOWN = "The processing queue is unavailable, so the clips could not be made ready."
# How long a review waits before it looks again at clips being made ready,
# and how long the make-ready job waits for a drive that is away.
PREPARE_WAIT = timedelta(minutes=5)
# The assets read at a time on one pass through a folder.
PAGE = 20
# The queues that make clips ready: playable copies, then shot analysis.
PREPARE_QUEUES = ("media", "frames")
# Kept in an ingest job's payload: what its earlier runs looked at, so the
# figures it ends with (and its new-tag allowance) cover the whole job, not
# only the slice since it last stepped aside.
_FIGURES = "_figures"
_FIGURE_NAMES = ("looked", "failed", "tags", "new_tags", "held")
# After an ingest job failed because of the Looker itself, new footage in
# that library is left alone this long (`reviews.ingest_hold`, which the
# Processing page reads too).
INGEST_HOLD = reviews.INGEST_HOLD
HELD_FOR = (
    f"Looking at new footage is tried again in about {int(INGEST_HOLD.total_seconds() // 3600)} "
    "hours."
)
# Kept in a make-ready job's payload: the clips it has asked the pipeline
# for and that are still not ready, so each is asked for once.
_ASKED = "_asked"
# The make-ready job of a review that ended before it did.
PREPARE_OVER = reviews.PREPARE_OVER
# An ingest job whose worker stops under it is replaced at once — a restart
# is no fault of the footage. But one that stops this many times in a row
# without having looked at anything is stopping its worker (a model too
# large for the memory left, a file that crashes the program), and making
# another every five minutes only does it again: the library is left alone
# for INGEST_HOLD, as after a Looker that would not load.
INGEST_DEATHS = 3
# Kept in the dead job's payload: how many in a row that made (`n`), and
# which model was the Looker (`model`). The count is that model's: the way
# out of a hold is to give the role to a smaller model, and its first death
# is a restart like anybody's, not the fourth of the one it replaced.
_DEATHS = "_deaths"
KEEPS_STOPPING = (
    f"It stopped without finishing {INGEST_DEATHS} times in a row, each time before it had "
    "looked at anything."
)
# How long the drive for playable copies is given to say whether it is there
# (a share half asleep answers in seconds; one that hung may never).
SHARE_WAIT_S = 60.0
COPIES_SILENT = (
    "The drive for playable copies is not answering. The clips are made ready once it does."
)
# kombu's hash of messages a worker has been handed and has not finished
# (celery_app sets no other name and no key prefix).
UNACKED = "unacked"


def in_a_row(why: str) -> str:
    """Why a review stopped after MAX_CONSECUTIVE_FAILURES failures in a row."""
    return f"Stopped: the model failed on {MAX_CONSECUTIVE_FAILURES} items in a row — {why}"


def could_not_use(why: str) -> str:
    """Why a review stopped at once: the model itself could not be used."""
    return f"Stopped: the model could not be used — {why}"


def stopped_for_the_day(done: int, total: int, budgets: schedule.Budgets) -> str:
    """A Tonight review at the end of the overnight window (D8)."""
    return (
        f"Stopped for the day at {schedule.clock_text(budgets.night_end)} — {done} of "
        f"{total} done. Carries on at {schedule.clock_text(budgets.night_start)}."
    )


def waiting_for_clips(count: int) -> str:
    clips = "1 clip" if count == 1 else f"{count} clips"
    return f"Waiting for {clips} to be made ready."


def getting_ready(total: int, left: int) -> str:
    """The make-ready job's line (1.4 row 5)."""
    clips = "1 clip" if total == 1 else f"{total} clips"
    return f"Getting {clips} ready ({left} to go)."


def closing_text(request: LookRequest) -> str:
    """The sentence a finished review keeps: how many were looked at, and
    what the operator may want to know about the rest."""
    if request.done + request.failed == 0:
        return NOTHING_LEFT
    parts = [f"Finished: {request.done} of {request.total} were looked at."]
    if request.failed:
        could = "1 could" if request.failed == 1 else f"{request.failed} could"
        parts.append(f"{could} not be looked at; reviewing the folder again tries them again.")
    if request.stills_only:
        clips = "1 clip" if request.stills_only == 1 else f"{request.stills_only} clips"
        parts.append(
            f"{clips} could not be made ready and "
            f"{'was' if request.stills_only == 1 else 'were'} judged from one still each."
        )
    if request.held:
        held = "1 suggestion was" if request.held == 1 else f"{request.held} suggestions were"
        parts.append(
            f"{held} held back: its tag already had a full queue on the Tags page, or this "
            f"review had proposed its {MAX_NEW_TAGS_PER_REVIEW} new tags."
        )
    return " ".join(parts)


# --- the request row ------------------------------------------------------------------


def _request_id(job: LocalAiJob) -> uuid.UUID | None:
    payload = job.payload if isinstance(job.payload, dict) else {}
    try:
        raw = payload.get("request_id")
        return None if raw is None else uuid.UUID(str(raw))
    except ValueError:
        return None


async def _request(db: AsyncSession, request_id: uuid.UUID) -> LookRequest | None:
    return await db.get(LookRequest, request_id, populate_existing=True)


async def _bump(db: AsyncSession, request_id: uuid.UUID, **more: int) -> None:
    """Add to the request's counters, naming only those columns (P8)."""
    values = {name: getattr(LookRequest, name) + n for name, n in more.items() if n}
    if values:
        await db.execute(
            update(LookRequest)
            .where(LookRequest.id == request_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )


async def _set_total(db: AsyncSession, request_id: uuid.UUID, total: int) -> None:
    await db.execute(
        update(LookRequest)
        .where(LookRequest.id == request_id)
        .values(total=total)
        .execution_options(synchronize_session=False)
    )


async def _finish(db: AsyncSession, request_id: uuid.UUID, state: str, message: str) -> bool:
    """End the review, unless it has already ended (a Cancel pressed meanwhile
    stands). Commits. Whether this write ended it."""
    ended: Any = await db.execute(
        update(LookRequest)
        .where(LookRequest.id == request_id, LookRequest.state == "active")
        .values(state=state, message=message[:500], finished_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return bool(ended.rowcount)


async def _end_prepare(db: AsyncSession, request_id: uuid.UUID) -> None:
    """The review has ended: end its make-ready job too, if it is still at
    work (`reviews.end_prepare`, which a review ended without its handler
    goes through as well). Commits."""
    await reviews.end_prepare(db, request_id)
    await db.commit()


async def _fail(db: AsyncSession, request_id: uuid.UUID | None, message: str) -> NoReturn:
    """End the review `failed` with `message`, and the job with it: the job
    did not do what it was for, and the AI models page should say so too.
    Looking at ingest (`request_id` None) has only the job to end."""
    if request_id is not None:
        await _finish(db, request_id, "failed", message)
        await _end_prepare(db, request_id)
    raise jobs.Failed(message)


# --- one run of the handler -----------------------------------------------------------


# An item's place in the ledger beside its asset (`scope.Item.span`).
Span = tuple[int | None, int | None]


@dataclass
class _Run:
    """What one run of the handler holds: read once at entry, and nothing in
    it that the next run could not read again from the database.

    `request_id` is the review's; None when looking at ingest, which has no
    request row and keeps its figures in `figures` (and its job's payload)."""

    ctx: jobs.JobContext
    db: AsyncSession
    request_id: uuid.UUID | None
    place: scope.Scope
    redo: bool
    tonight: bool
    model: LocalModel
    who: str | uuid.UUID
    key: str
    budgets: schedule.Budgets
    stills: int
    new_tags: bool
    shown: list[tuple[str, int]]
    slugs: set[str]
    in_demo: bool
    began: float
    done: int
    failed: int
    total: int
    new_tags_made: int
    streak: int = 0
    figures: dict[str, int] = field(default_factory=dict)
    # Which pass is running: what `_keep_total` counts the rest with.
    fallback: bool = False
    # Items this run recorded a counted answer for (asset, span), and those
    # it counted as not looked at instead of asking again (`_pass`).
    answered: set[tuple[uuid.UUID, Span]] = field(default_factory=set)
    refused: set[tuple[uuid.UUID, Span]] = field(default_factory=set)


async def _begin(
    ctx: jobs.JobContext,
    db: AsyncSession,
    *,
    request_id: uuid.UUID | None,
    place: scope.Scope,
    redo: bool,
    tonight: bool,
    model: LocalModel,
    who: str | uuid.UUID,
    done: int,
    failed: int,
    total: int,
    new_tags_made: int,
) -> _Run:
    """A run's settings, read afresh: the Looker's settings, the vocabulary it
    is shown, L1's budgets."""
    config = await load_config(db)
    vocabulary = await tag_review.vocabulary(db)
    shown = prompt.most_used(vocabulary)
    return _Run(
        ctx=ctx,
        db=db,
        request_id=request_id,
        place=place,
        redo=redo,
        tonight=tonight,
        model=model,
        who=who,
        key=store.key_for(model),
        budgets=(await load_local_ai_config(db)).budgets(),
        stills=config.stills_per_shot,
        new_tags=config.new_tags,
        shown=shown,
        slugs={slugify(name) for name, _count in shown},
        in_demo=availability(ctx.deps.settings).mode == "demo",
        began=time.monotonic(),
        done=done,
        failed=failed,
        total=total,
        new_tags_made=new_tags_made,
    )


async def _looker(db: AsyncSession) -> LocalModel | None:
    """Whoever has the Looker role now, or None."""
    looker = (await registry.roles(db))["looker"].model_id
    return None if looker is None else await registry.get_model(db, looker)


async def _model_for(db: AsyncSession, request: LookRequest) -> tuple[LocalModel | None, str]:
    """The model this review asks, and how it is asked: the one named, by
    id; else whoever is the Looker now, by role (so it runs from this Mac's
    own copy). None with the sentence when there is none."""
    if request.model_id is not None:
        model = await registry.get_model(db, request.model_id)
        return (model, NO_SUCH_MODEL) if model is None else (model, "")
    return await _looker(db), reviews.NO_LOOKER


async def look_folder(ctx: jobs.JobContext, job: LocalAiJob) -> dict[str, Any] | None:
    """Look at everything in the review's folder that still needs it, then
    end the review.

    Every way out is one of (P6): the review written `done` (returns its
    figures) or `failed` (`jobs.Failed`, the same sentence on the job);
    `jobs.Later` — the same row runs again and re-enters here from the top;
    `jobs.Cancelled`; `NotNow` (L1's runner makes the job wait, or fail —
    and a review that can never run is written `failed` here first)."""
    request_id = _request_id(job)
    if request_id is None:
        # A job that names no review has nothing to look at.
        return {"gone": True}
    async with ctx.factory() as db:
        request = await _request(db, request_id)
        if request is None or request.state != "active":
            # Its review was removed with its library, or ended while this
            # job was waiting its turn: there is nothing left to do.
            return {"gone": True}
        try:
            return await _review(ctx, db, request)
        except jobs.Cancelled:
            await db.rollback()
            ended = await _request(db, request_id)
            if ended is not None:
                await _finish(db, ended.id, "cancelled", reviews.cancelled_text(ended))
                # Cancel on the AI models page stops this job alone; the
                # review's own Cancel has already ended both.
                await _end_prepare(db, ended.id)
            raise
        except NotNow as err:
            await db.rollback()
            if not err.retry:
                # It will never run as things stand (the model is no longer
                # approved, its program is not installed, or it never fits):
                # the review says so now, rather than reading "active" until
                # somebody opens Processing. `request_id`, not `request.id`:
                # the rollback expired `request`, and reading an expired
                # attribute loads it — I/O an async session cannot do here.
                await _finish(db, request_id, "failed", str(err))
                await _end_prepare(db, request_id)
            raise


async def _review(ctx: jobs.JobContext, db: AsyncSession, request: LookRequest) -> dict[str, Any]:
    model, missing = await _model_for(db, request)
    if model is None:
        await _fail(db, request.id, missing)
    if request.started_at is None:
        await db.execute(
            update(LookRequest)
            .where(LookRequest.id == request.id, LookRequest.started_at.is_(None))
            .values(started_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
    run = await _begin(
        ctx,
        db,
        request_id=request.id,
        place=scope.Scope(request.library_id, request.folder),
        redo=request.redo,
        tonight=request.run_when == "tonight",
        model=model,
        who=model.id if request.model_id is not None else "looker",
        done=request.done,
        failed=request.failed,
        total=request.total,
        new_tags_made=request.new_tags,
    )
    await _count_up(run, fallback=False)
    await db.commit()

    # Passes until one finds nothing: what a pass looks at drops out of the
    # folder's "still needs looking at", and clips made ready meanwhile join it.
    while await _pass(run, fallback=False):
        pass
    waiting = await _clips_being_made_ready(run, request.id)
    if waiting:
        # Said in the job's phase too: a page that shows the review reads
        # from it that this wait is for clips (`reviews.shown`).
        ctx.latest.set(phase=reviews.PHASE_CLIPS)
        raise jobs.Later(waiting_for_clips(waiting), not_before=ctx.deps.now() + PREPARE_WAIT)
    # What never became ready (no playable copy could be made, or its shots
    # were never found) is judged from one still, and counted (1.4 row 6).
    await _count_up(run, fallback=True)
    await db.commit()
    while await _pass(run, fallback=True):
        pass

    ended = await _request(db, request.id)
    if ended is None:
        return {"gone": True}
    await _finish(db, ended.id, "done", closing_text(ended))
    # The review looks every five minutes and the make-ready job every two,
    # so the review often sees the last clip ready first.
    await _end_prepare(db, ended.id)
    ctx.latest.set(phase="looking", done=run.done + run.failed, total=run.total)
    return {
        "looked": ended.done,
        "failed": ended.failed,
        "tags": ended.tags_suggested,
        "new_tags": ended.new_tags,
        "held": ended.held,
    }


async def _count_up(run: _Run, *, fallback: bool) -> None:
    """The review's total: what is done, plus what this run would still hand
    out. Written, so a reload of the page shows the same figure."""
    left = await scope.remaining(
        run.db,
        run.place,
        key=run.key,
        redo=run.redo,
        run=scope.Run(run.request_id, stills_fallback=fallback),
    )
    run.total = run.done + run.failed + left
    if run.request_id is not None:
        await _set_total(run.db, run.request_id, run.total)
    await run.ctx.progress(phase="looking", done=run.done + run.failed, total=run.total)


async def _clips_being_made_ready(run: _Run, request_id: uuid.UUID) -> int:
    """How many clips the review should wait for: 0 unless its make-ready job
    is still at work and clips in the folder are still not ready."""
    request = await _request(run.db, request_id)
    if request is None or request.prep_state != "running":
        return 0
    preparing = (
        None
        if request.prepare_job_id is None
        else await run.db.get(LocalAiJob, request.prepare_job_id, populate_existing=True)
    )
    now = run.ctx.deps.now()
    if preparing is None or jobs.shown(preparing, now)[0] not in jobs.ACTIVE:
        # The make-ready job ended without saying so (cancelled on the AI
        # models page, its worker died, pruned): nobody is making the clips.
        await run.db.execute(
            update(LookRequest)
            .where(LookRequest.id == request_id, LookRequest.prep_state == "running")
            .values(prep_state="done")
            .execution_options(synchronize_session=False)
        )
        await run.db.commit()
        return 0
    need_copy, need_shots = await scope.not_ready_clips(
        run.db, run.place, key=run.key, redo=run.redo
    )
    return len(need_copy) + len(need_shots)


async def _keep_total(run: _Run) -> None:
    """Count the rest again when what has been done passes the total: clips
    made ready during a pass join it, and a progress bar must never read
    "20 of 8". The count is done + failed + what is left, so it can only
    catch up."""
    if run.done + run.failed > run.total:
        await _count_up(run, fallback=run.fallback)
        await run.db.commit()


async def _pass(run: _Run, *, fallback: bool) -> bool:
    """One walk through the folder in path order; whether it asked anything.

    The guard against a review that never ends has two halves. An answer
    that does not count as its item's look is counted as not looked at
    when it is recorded (`_record`): its failure row ends the item's
    hand-outs for the review, across a restart too. And this run never asks
    again about an item it recorded a counted answer for: should one be
    handed out again all the same (the rule that hands items out and the
    rule that counts a look disagreeing some other way), it is moved to
    "could not be looked at" once (`_not_again`) instead of being asked for
    ever, its count climbing past its total.

    Neither half reads answers from an earlier run under another key: a
    review that looks again (redo) whose Looker was handed to another model,
    or whose model was updated, starts again under the new key and asks the
    new one about everything — that is what looking again means."""
    asked = False
    after = ""
    run.fallback = fallback
    which = scope.Run(run.request_id, stills_fallback=fallback)
    while True:
        page = await scope.next_assets(
            run.db, run.place, key=run.key, redo=run.redo, run=which, after=after, limit=PAGE
        )
        if not page:
            return asked
        for asset in page:
            after = asset.relative_path
            items = await scope.items_for(
                run.db,
                asset,
                key=run.key,
                redo=run.redo,
                request_id=run.request_id,
                stills_fallback=fallback,
            )
            # Read now, while every row is loaded: what the model is told
            # about each shot (a rollback later would expire the windows).
            questions = [
                (item, prompt.facts_line(item.window) if item.window is not None else "")
                for item in items
            ]
            judged_whole = fallback and asset.media_type == "video"
            for item, facts in questions:
                which_item = (item.asset_id, item.span)
                if which_item in run.refused:
                    # Already counted as not looked at in this run (looking
                    # at ingest hands it out until INGEST_TRIES).
                    continue
                if which_item in run.answered:
                    run.refused.add(which_item)
                    await _not_again(run, item, judged_whole=judged_whole)
                    continue
                asked = True
                await _one(run, item, facts, judged_whole=judged_whole)


async def _cancel_pressed(run: _Run) -> bool:
    """Read now: `ctx.progress` reads Cancel only when it writes, at most every
    two seconds, and a model that answers in one must not be asked two more."""
    async with run.ctx.factory() as db:
        return bool(
            await db.scalar(
                select(LocalAiJob.cancel_requested).where(LocalAiJob.id == run.ctx.job_id)
            )
        )


class _SwitchedOff(Exception):
    """The library's "look at new footage" switch went off while its ingest
    job ran (or the library was removed): the job ends between two items."""


async def _still_wanted(run: _Run) -> bool:
    """The library an ingest job looks at still has its switch on. Read from
    the table between two items: the switch is how somebody at the Mac makes
    the Looker stop, and a job that read it only when it started went on
    asking the model for the rest of its slice."""
    return bool(
        await run.db.scalar(
            select(Library.look_at_ingest).where(Library.id == run.place.library_id)
        )
    )


async def _before(run: _Run, item: scope.Item) -> None:
    """Between two items: Cancel, the library's switch (looking at ingest),
    the end of the night, and the lane."""
    if await _cancel_pressed(run):
        raise jobs.Cancelled()
    if run.request_id is None and not await _still_wanted(run):
        raise _SwitchedOff()
    now = run.ctx.deps.now()
    if run.tonight and not schedule.is_overnight(now, run.budgets):
        # Tonight means only inside the window, however many nights it takes
        # — even for a model that would fit by day (D8).
        raise jobs.Later(
            stopped_for_the_day(run.done + run.failed, run.total, run.budgets),
            not_before=schedule.next_change(now, run.budgets),
        )
    if await run.ctx.should_yield() or time.monotonic() - run.began >= SLICE_SECONDS:
        # A more urgent job runs between two items; and every slice the lane
        # is handed back anyway, so a waiting job whose time has come is
        # promoted (D10). The same row runs again after it.
        raise jobs.Later(STEPPED_ASIDE)
    await run.ctx.progress(
        phase="looking", done=run.done + run.failed, total=run.total, message=item.filename
    )


async def _one(run: _Run, item: scope.Item, facts: str, *, judged_whole: bool) -> None:
    """Ask about one item and record what came of it, committed at once."""
    await _before(run, item)
    db = run.db
    shown = await pictures.for_item(
        db, item, stills=run.stills, data_dir=run.ctx.deps.settings.data_dir
    )
    if not facts and len(shown) > 1:
        # A shot with no measured facts is asked about as one picture: the
        # question's wording for three needs the sentence (build_prompt).
        shown = shown[len(shown) // 2 : len(shown) // 2 + 1]
    if not shown:
        # D22: no still, no look. Recorded, so this review does not ask again.
        note = NO_SHOT_PICTURE if item.start_ms is not None else NO_PICTURE
        await _failed(run, item, note, streak=False)
        return
    try:
        answer = await ask(
            db,
            run.who,
            prompt=prompt.build_prompt(
                facts=facts, pictures=len(shown), vocabulary=run.shown, new_tags=run.new_tags
            ),
            prompt_version=prompt.PROMPT_VERSION,
            instructions=prompt.INSTRUCTIONS,
            pictures=shown,
            schema=prompt.LookAnswer,
            max_tokens=prompt.MAX_TOKENS,
            timeout_s=prompt.TIMEOUT_S,
            server=run.ctx.deps.server,
            now=run.ctx.deps.now(),
            settings=run.ctx.deps.settings,
        )
    except ModelFailed as err:
        if err.reason in _AT_ONCE:
            await db.rollback()
            await _fail(db, run.request_id, could_not_use(str(err)))
        await _failed(run, item, str(err), streak=True)
        return
    look = answer.parsed
    if not isinstance(look, prompt.LookAnswer):  # a schema was given: never
        await _failed(run, item, "Its answer was not the form asked for.", streak=True)
        return
    if answer.model_id != run.model.id:
        # The Looker was given to another model while this one answered. Its
        # key is not the one this run asks with, so the run starts again
        # from the top under the new one rather than record it here.
        await db.rollback()
        raise jobs.Later()
    if answer.demo and run.in_demo:
        answer, look = _made_up(run, item, answer)
    await _record(run, item, answer, look, judged_whole=judged_whole)


def _made_up(run: _Run, item: scope.Item, answer: Answer) -> tuple[Answer, prompt.LookAnswer]:
    """The demo's look in place of the demo server's random JSON (D25). Only
    for an answer the demo server gave, in demo mode: the fake the tests
    hand in marks its answers `demo` too, and those are kept as given."""
    seed = f"{answer.repo_id}:{item.asset_id}:{item.start_ms}:{item.end_ms}"
    look = demo.answer_for(
        seed, [name for name, _count in run.shown], shot=item.start_ms is not None
    )
    seconds, _memory = demo.figures_for(seed)
    return replace(answer, text=look.model_dump_json(), parsed=look, seconds=seconds), look


async def _record(
    run: _Run, item: scope.Item, answer: Answer, look: prompt.LookAnswer, *, judged_whole: bool
) -> None:
    db = run.db
    cleaned = prompt.clean(look, run.slugs, new_tags=run.new_tags)
    try:
        stored = await store.record(
            db, item, answer, cleaned, request_id=run.request_id, model=run.model
        )
        suggested = await store.suggest(
            db,
            stored,
            cleaned.proposals,
            new_tags_left=max(0, MAX_NEW_TAGS_PER_REVIEW - run.new_tags_made),
        )
        counted = stored.current
        if not counted:
            # Kept, but not as the item's look (`store.record`): the item is
            # counted as not looked at now, in the same transaction. The
            # failure row is what stops this review handing it out again —
            # read from the ledger, so it holds after a restart or a step
            # aside — and at ingest it is one of the item's tries. Counted
            # here, and only here, the figures never count it twice.
            await store.record_failure(
                db, item, model=run.model, note=NOT_AGAIN, request_id=run.request_id
            )
        await store.roll_up(db, item.asset_id)
        if run.request_id is not None:
            await _bump(
                db,
                run.request_id,
                done=1 if counted else 0,
                failed=0 if counted else 1,
                tags_suggested=suggested.written,
                new_tags=suggested.new_tags,
                held=len(suggested.held),
                stills_only=1 if counted and judged_whole and item.start_ms is None else 0,
            )
        await db.commit()
    except IntegrityError:
        # The asset was removed while the model answered (the scanner found
        # its file gone; its rows cascade): nothing of it is kept. The run
        # starts again from the top, on a session with nothing stale in it.
        await db.rollback()
        log.info("looks.item_vanished", asset_id=str(item.asset_id))
        raise jobs.Later() from None
    # A tag this review created is the catalogue's word from now on: a
    # second shot proposing it again meets the existing floor (as Claude's
    # reviewer does).
    for told in (stored.answer or {}).get("tags", []):
        if told.get("outcome") == tag_review.OUTCOME_NEW_TAG:
            run.slugs.add(slugify(str(told.get("name", ""))))
    run.new_tags_made += suggested.new_tags
    run.streak = 0  # the model answered; whether the answer counts is not its doing
    if counted:
        run.done += 1
        run.answered.add((item.asset_id, item.span))
    else:
        run.failed += 1
        run.refused.add((item.asset_id, item.span))
        log.warning("looks.answer_not_counted", asset_id=str(item.asset_id), span=list(item.span))
    _add(
        run,
        looked=1 if counted else 0,
        failed=0 if counted else 1,
        tags=suggested.written,
        new_tags=suggested.new_tags,
        held=len(suggested.held),
    )
    await _keep_total(run)
    await run.ctx.record(answer)
    await run.ctx.progress(
        phase="looking", done=run.done + run.failed, total=run.total, message=item.filename
    )


async def _failed(run: _Run, item: scope.Item, note: str, *, streak: bool) -> None:
    """`item` gave nothing usable: recorded against this review, which goes
    on. `streak`: it was the model's failure, and enough in a row stop it."""
    db = run.db
    try:
        await store.record_failure(db, item, model=run.model, note=note, request_id=run.request_id)
        if run.request_id is not None:
            await _bump(db, run.request_id, failed=1)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        log.info("looks.item_vanished", asset_id=str(item.asset_id))
        raise jobs.Later() from None
    run.failed += 1
    _add(run, failed=1)
    await _keep_total(run)
    if streak:
        run.streak += 1
        if run.streak >= MAX_CONSECUTIVE_FAILURES:
            await _fail(db, run.request_id, in_a_row(note))
    await run.ctx.progress(
        phase="looking", done=run.done + run.failed, total=run.total, message=item.filename
    )


async def _not_again(run: _Run, item: scope.Item, *, judged_whole: bool) -> None:
    """`item` was handed out again in this run after an answer about it was
    recorded and counted: counted as not looked at instead of asked again.
    That answer was counted as looked at; it moves to "could not be looked
    at", so every item is counted once and the closing sentence adds up.
    The answer and its suggestions are left as they are: they were made,
    and the owner judges them in Tag review."""
    db = run.db
    try:
        await store.record_failure(
            db, item, model=run.model, note=NOT_AGAIN, request_id=run.request_id
        )
        if run.request_id is not None:
            await _bump(db, run.request_id, done=-1, failed=1)
            if judged_whole and item.start_ms is None:
                # Its earlier answer was counted as judged from one still
                # too, when it came in this same last pass.
                await db.execute(
                    update(LookRequest)
                    .where(LookRequest.id == run.request_id, LookRequest.stills_only > 0)
                    .values(stills_only=LookRequest.stills_only - 1)
                    .execution_options(synchronize_session=False)
                )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        log.info("looks.item_vanished", asset_id=str(item.asset_id))
        raise jobs.Later() from None
    log.warning("looks.asked_again_refused", asset_id=str(item.asset_id), span=list(item.span))
    run.done -= 1
    run.failed += 1
    _add(run, looked=-1, failed=1)
    await _keep_total(run)


def _add(run: _Run, **more: int) -> None:
    """Count toward an ingest job's figures (a review counts on its request)."""
    if run.request_id is None:
        for name, n in more.items():
            run.figures[name] = run.figures.get(name, 0) + n


# --- looking at what arrives ------------------------------------------------------------


def _library_id(job: LocalAiJob) -> uuid.UUID | None:
    payload = job.payload if isinstance(job.payload, dict) else {}
    try:
        raw = payload.get("library_id")
        return None if raw is None else uuid.UUID(str(raw))
    except ValueError:
        return None


def _figures_of(job: LocalAiJob) -> dict[str, int]:
    payload = job.payload if isinstance(job.payload, dict) else {}
    kept = payload.get(_FIGURES)
    figures: dict[str, int] = {}
    for name in _FIGURE_NAMES:
        try:
            figures[name] = int(kept.get(name, 0)) if isinstance(kept, dict) else 0
        except (TypeError, ValueError):
            figures[name] = 0
    return figures


async def _keep_figures(
    ctx: jobs.JobContext,
    figures: dict[str, int],
    *,
    held_by: uuid.UUID | None = None,
    cause: str = reviews.HELD_BY_MODEL,
) -> None:
    """Write an ingest job's figures into its payload before it steps aside,
    waits or ends, in a session of its own (the run's may hold a failed
    transaction). A database that blinks costs the figures, never the job.

    `held_by`: the job is ending because the Looker (this model) could not
    be used, or (`cause`) on something nobody expected. That is kept too,
    with the model's row as it is now (`reviews.hold_mark`), so `nudge` and
    `ensure_ingest` leave the library alone for a while (`_held_off`)
    instead of making the same failing job again."""
    try:
        async with ctx.factory() as db:
            job = await db.get(LocalAiJob, ctx.job_id, populate_existing=True)
            if job is None:
                return
            payload = dict(job.payload) if isinstance(job.payload, dict) else {}
            payload[_FIGURES] = dict(figures)
            if held_by is not None:
                # Read after the failure: whatever the failed question wrote
                # to the row is part of "as it was then".
                model = await registry.get_model(db, held_by)
                if model is not None:
                    payload[reviews.HELD] = reviews.hold_mark(model, cause)
            await db.execute(
                update(LocalAiJob)
                .where(LocalAiJob.id == ctx.job_id)
                .values(payload=payload)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    except Exception:
        log.warning("looks.ingest_figures_unwritten", job_id=str(ctx.job_id), exc_info=True)


async def look_ingest(ctx: jobs.JobContext, job: LocalAiJob) -> dict[str, Any] | None:
    """Look at what came into a library since its switch went on, until
    nothing is left; then end. Another arrival starts another (P19).

    Always the Looker, by role; never redo. An item gets `INGEST_TRIES`
    tries per build and question, then is left be (D7). It steps aside for
    anything more urgent — a review, a Test — between two items, and every
    slice (D10). A library whose switch is off (or that is gone) when the
    job gets its turn ends it with nothing done; one switched off while it
    runs ends it before the next item, keeping what was looked at.

    A job that fails because of the Looker itself (it would not load, may
    no longer run, or failed on five items in a row) says in its payload
    which model that was, and looking at this library's new footage is
    held for INGEST_HOLD: nothing was recorded against any item for it, so
    the next file to arrive would otherwise start the same failure again."""
    library_id = _library_id(job)
    async with ctx.factory() as db:
        library = (
            None
            if library_id is None
            else await db.get(Library, library_id, populate_existing=True)
        )
        if library is None or not library.look_at_ingest or library.look_since is None:
            return {"off": True}
        model = await _looker(db)
        if model is None:
            raise jobs.Failed(reviews.NO_LOOKER)
        figures = _figures_of(job)
        run = await _begin(
            ctx,
            db,
            request_id=None,
            place=scope.Scope(library.id, "", since=library.look_since),
            redo=False,
            tonight=False,
            model=model,
            who="looker",
            done=int(job.done or 0),
            failed=0,
            total=int(job.total or 0),
            new_tags_made=figures["new_tags"],
        )
        run.figures = figures
        # Read now: a rollback below makes the session forget the row.
        looker_id = model.id
        off = False
        try:
            await _count_up(run, fallback=False)
            while await _pass(run, fallback=False):
                pass
        except _SwitchedOff:
            await db.rollback()
            off = True
        except (jobs.Later, jobs.Cancelled):
            await db.rollback()
            await _keep_figures(ctx, run.figures)
            raise
        except NotNow as err:
            await db.rollback()
            if err.retry:
                await _keep_figures(ctx, run.figures)
                raise
            # It may not run as things stand (no longer approved, its
            # program gone, never fits): the Looker's fault, not a file's.
            await _keep_figures(ctx, run.figures, held_by=looker_id)
            raise NotNow(
                err.reason, f"{err} {HELD_FOR}", err.not_before, detail=err.detail
            ) from None
        except jobs.Failed as err:
            await db.rollback()
            await _keep_figures(ctx, run.figures, held_by=looker_id)
            raise jobs.Failed(f"{err} {HELD_FOR}", err.detail) from None
        except Exception:
            # Something nobody expected. Held all the same: whatever it was
            # would meet the next job made for this library too.
            with contextlib.suppress(Exception):
                await db.rollback()
            await _keep_figures(ctx, run.figures, held_by=looker_id, cause=reviews.HELD_BY_ERROR)
            raise
        await _keep_figures(ctx, run.figures)
        ctx.latest.set(phase="looking", done=run.done + run.failed, total=run.total)
        result: dict[str, Any] = {name: run.figures.get(name, 0) for name in _FIGURE_NAMES}
        if off:
            result["off"] = True
        return result


def _ingest_title(job: LocalAiJob) -> str:
    return f"Looking at new footage in {job.subject}"


def wake_lane(settings: Settings | None = None) -> None:
    """Wake the lane for an ingest job just committed. Never raises: the
    once-a-minute tick finds the row anyway.

    Only where models really run: in demo mode the API runs the lane itself
    and picks the row up when a page is next read (P30) — a pipeline worker
    or the scanner must not start the demo's lane in its own process."""
    try:
        if availability(settings).mode != "ready":
            return
        from framefound.processing import local_ai_tasks

        local_ai_tasks.wake(settings)
    except Exception:
        log.warning("looks.wake_failed", exc_info=True)


def _utc(moment: datetime | None) -> datetime | None:
    """A moment from the table in UTC (SQLite gives back what was written
    without its zone; everything written is UTC)."""
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def wall_clock() -> datetime:
    """The Mac's wall clock, naive, as L1's `Deps.now` gives it (D24): for
    the pipeline's hook and the scanner, which have no `Deps`. Jobs write
    when they ended from that clock, so a hold is measured against it."""
    return datetime.now()


async def _may_look(db: AsyncSession, looker: LocalModel) -> bool:
    """The Looker may look as things stand: complete, still approved, within
    a budget, of a kind this install has (what `reviews.start` and the
    status route refuse with). A job for one that may not could only fail
    saying so."""
    config = await load_local_ai_config(db)
    return (
        registry.role_problem(
            looker, "looker", config.lists(), config.budgets(), tuple(runtime_kinds())
        )
        is None
    )


async def _held_off(db: AsyncSession, subject: str, looker: LocalModel, now: datetime) -> bool:
    """New footage in the library `subject` is being left alone: its newest
    ingest job failed and kept a hold mark — because of the Looker
    (`_keep_figures(held_by=...)`), or because it kept stopping with its
    worker (`look_ingest_abandoned`) — less than INGEST_HOLD ago, and the
    Looker is still that model (for a hold the Looker caused: with its row
    unchanged too). `now` is the wall clock, naive (`wall_clock`). The rule
    itself is `reviews.ingest_hold`, which the Processing page reads to say
    so.

    A job that failed with no mark (its worker died, once) holds nothing:
    the files it left are picked up at once."""
    return await reviews.ingest_hold(db, subject, looker, now) is not None


async def _ingest_active(db: AsyncSession, subject: str) -> bool:
    return bool(
        await db.scalar(
            select(
                exists().where(
                    LocalAiJob.kind == INGEST_KIND,
                    LocalAiJob.subject == subject,
                    LocalAiJob.state.in_(jobs.ACTIVE),
                )
            )
        )
    )


async def _waiting(db: AsyncSession, library: Library, key: str, *more: Any) -> bool:
    """Ingest would find something to look at in `library` (and `more`):
    what `look_ingest`'s first pass would hand out, asked as one EXISTS."""
    place = scope.Scope(library.id, "", since=library.look_since)
    return bool(
        await db.scalar(
            select(
                exists().where(
                    *scope.within(place),
                    scope.needs_work(key, False),
                    scope.has_items(key, False, scope.Run(None)),
                    *more,
                )
            )
        )
    )


async def _create_ingest(db: AsyncSession, library: Library) -> bool:
    """The library's ingest job; False when one is already active."""
    try:
        await jobs.create(
            db,
            INGEST_KIND,
            subject=library.name,
            role="looker",
            # `reviews.listing` finds a library's ingest job by this.
            payload={"library_id": str(library.id)},
            priority=PRIORITY_INGEST,
        )
    except jobs.JobRefused:
        return False
    return True


async def nudge(db: AsyncSession, asset: Asset, library: Library) -> None:
    """The pipeline's hook: `asset` has just become something the Looker
    could look at (embedded, or its shots described). When its library looks
    at new footage, make sure an ingest job is on its way.

    Called once per file of a card dump, so the usual answer — the switch is
    off, or a job is already active — costs no query or one (P18). No job
    is made for a Looker that may not look, nor while the library is held
    after a job that could not use it (`_held_off`): either would fail the
    same way, once for every file. Never raises and leaves the session
    clean (P17): looking must never fail the stage that called it. Read
    before anything is asked of the database, and after a rollback in the
    stage only through a refresh, so an expired row is never lazily loaded."""
    try:
        for row in (asset, library):
            state: Any = inspect(row)
            if state.expired_attributes:
                await db.refresh(row)
        if not library.look_at_ingest or library.look_since is None:
            return
        if not availability().ok or asset.media_type not in scope.LOOKABLE:
            return
        since = _utc(library.look_since)
        arrived = _utc(asset.first_indexed_at)
        if since is not None and arrived is not None and arrived < since:
            # Older than the switch: ingest's scope would not hold it.
            return
        if await _ingest_active(db, library.name):
            return
        looker = await _looker(db)
        if looker is None or not await _may_look(db, looker):
            # The job would only fail saying so, once per file that arrives.
            return
        if await _held_off(db, library.name, looker, wall_clock()):
            return
        if not await _waiting(db, library, store.key_for(looker), Asset.id == asset.id):
            # Already looked at (a file embedded again), or not ready to be
            # (a clip whose shots are still to come: their hook nudges again).
            return
        made = await _create_ingest(db, library)
        await db.commit()
        if made:
            log.info("looks.ingest_started", library=library.name)
            wake_lane()
    except Exception:
        log.warning("looks.nudge_failed", exc_info=True)
        try:
            await db.rollback()
        except Exception:
            log.warning("looks.nudge_rollback_failed")


async def ensure_ingest(db: AsyncSession, now: datetime) -> int:
    """The scanner's safety net: an ingest job for every library that looks
    at new footage, has something waiting, and has no active job. A hook lost
    to a database blip — or run on a remote worker, where models cannot run
    and so the hook does nothing — would otherwise leave the last files of a
    card dump unlooked at until the next one arrives.

    `now` is the wall clock, naive (L1's D24): a running job whose worker
    died is ended first, as the lane's tick would, so it does not hold the
    library's subject. Returns how many jobs were made. Flushes; the caller
    commits, then wakes the lane. Nothing where models cannot run, while
    nobody has the Looker role or the Looker may not look, or for a library
    whose last job could not use the Looker a short while ago (`_held_off`):
    this runs every five minutes for as long as the switch is on."""
    if not availability().ok:
        return 0
    looker = await _looker(db)
    if looker is None or not await _may_look(db, looker):
        return 0
    key = store.key_for(looker)
    libraries: list[Library] = list(
        (
            await db.scalars(
                select(Library)
                .where(Library.look_at_ingest.is_(True), Library.look_since.is_not(None))
                .order_by(Library.name)
            )
        ).all()
    )
    made = 0
    for library in libraries:
        running = (
            await db.scalars(
                select(LocalAiJob).where(
                    LocalAiJob.kind == INGEST_KIND,
                    LocalAiJob.subject == library.name,
                    LocalAiJob.state == "running",
                )
            )
        ).all()
        for job in running:
            await jobs.reap(db, job, now)
        if await _ingest_active(db, library.name):
            continue
        if await _held_off(db, library.name, looker, now):
            continue
        if await _waiting(db, library, key) and await _create_ingest(db, library):
            made += 1
    return made


# --- making clips ready -----------------------------------------------------------------


def _handed_out(entries: list[Any], name: str) -> int:
    """How many of kombu's unacked messages belong to the queue `name`.

    Each is JSON `[message, exchange, routing key]`, and a task sent to a
    queue by name carries that name as both. One that cannot be read is
    counted: it may be a clip on its way."""
    found = 0
    for entry in entries:
        try:
            _message, exchange, routing_key = json.loads(entry)
        except (TypeError, ValueError):
            found += 1
            continue
        found += name in (exchange, routing_key)
    return found


def queue_depth(name: str, settings: Settings | None = None) -> int | None:
    """How many tasks of a pipeline queue are still to be finished: those
    waiting on it (its Redis list) and those a worker has been handed and
    has not acknowledged (kombu's `unacked` hash). None when it cannot be
    read. Blocking: called in a thread.

    The second half matters because tasks are acknowledged late
    (celery_app): one that is running — a copy can take hours — is not on
    the list, and nor is one whose worker was restarted under it, which the
    broker hands out again only when its visibility timeout runs out. Read
    as "nothing on its way", either had its clip judged from one still
    while the copy was still being made."""
    settings = settings or get_settings()
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2)
        try:
            waiting = int(client.llen(name))  # type: ignore[arg-type, unused-ignore]
            entries: Any = client.hvals(UNACKED)
            return waiting + _handed_out(list(entries), name)
        finally:
            client.close()
    except Exception:
        return None


async def _queues_empty(settings: Settings) -> bool:
    """Nothing is waiting to be made ready, and nothing is being made. A
    depth that cannot be read counts as busy: giving up on clips that are
    still on their way is the worse error."""
    for name in PREPARE_QUEUES:
        if await asyncio.to_thread(queue_depth, name, settings) != 0:
            return False
    return True


# The check of the drive for playable copies that has not answered yet, if
# any: at most one is ever outstanding (below).
_store_asked: Future[str | None] | None = None


def _ask_store(settings: Settings) -> Future[str | None]:
    """Ask, in a thread of its own, whether the drive for playable copies is
    away. A daemon thread rather than the event loop's pool: one stuck in a
    share that hung is never joined, so it holds up neither the end of the
    lane's tick (asyncio.run waits minutes for its pool's threads) nor the
    worker's shutdown."""
    answer: Future[str | None] = Future()

    def look() -> None:
        try:
            answer.set_result(stores.away("proxies", settings))
        except Exception as err:
            answer.set_exception(err)

    threading.Thread(target=look, name="looks-copies-drive", daemon=True).start()
    return answer


async def _copies_away(settings: Settings) -> str | None:
    """Why the drive for playable copies cannot be used right now, or None.

    `stores.away` looks at the store's folder, and a network share that
    hangs does not fail that look: it never returns. Made on the event loop
    it stopped the lane's own heartbeat, so the job read as stopped while
    its worker sat wedged and no other local-AI job ran. Here the look is
    made in a thread and given SHARE_WAIT_S; a drive that does not answer
    in that time is away. While one look is still unanswered no second is
    started, so a share that hangs all night leaves one thread waiting on
    it, not one for every time the job looked."""
    global _store_asked
    asked = _store_asked
    if asked is None or asked.done():
        asked = _store_asked = _ask_store(settings)
    waited, step = 0.0, 0.01
    while not asked.done():
        if waited >= SHARE_WAIT_S:
            log.warning("looks.copies_drive_silent")
            return COPIES_SILENT
        await asyncio.sleep(step)
        waited += step
        step = min(step * 2, 1.0)
    return asked.result()


def _queue_ready(need_copy: list[uuid.UUID], need_shots: list[uuid.UUID], asked: set[str]) -> None:
    """Ask the ordinary pipeline for the clips: a playable copy (asked for by
    name, so it is made even where the library has copies off), whose
    arrival queues its own shot analysis; or the analysis alone. Each clip
    asked for is added to `asked` as it is, so a queue that goes down
    partway leaves a true record of how far it got."""
    from framefound.processing import tasks

    for asset_id in need_copy:
        tasks.generate_proxy.delay(str(asset_id), requested=True)
        asked.add(str(asset_id))
    for asset_id in need_shots:
        tasks.analyze_motion.delay(str(asset_id))
        asked.add(str(asset_id))


def _asked_of(job: LocalAiJob) -> set[str] | None:
    """The clips this make-ready job has asked for and was still waiting on
    when it last looked; None when it has kept no list."""
    kept = job.payload.get(_ASKED) if isinstance(job.payload, dict) else None
    return {str(one) for one in kept} if isinstance(kept, list) else None


async def _keep_asked(db: AsyncSession, job: LocalAiJob, asked: set[str]) -> None:
    """Write the list into the job's payload (only this handler writes a
    make-ready job's payload once it exists). Not committed."""
    payload = dict(job.payload) if isinstance(job.payload, dict) else {}
    payload[_ASKED] = sorted(asked)
    await db.execute(
        update(LocalAiJob)
        .where(LocalAiJob.id == job.id)
        .values(payload=payload)
        .execution_options(synchronize_session=False)
    )


async def _prepare_key(db: AsyncSession, request: LookRequest) -> tuple[str, bool]:
    """The key and redo "still needs looking at" is judged by: the review's.
    With no model to say whose, every clip not wholly looked at."""
    model, _missing = await _model_for(db, request)
    if model is None:
        return "", False
    return store.key_for(model), request.redo


async def _prep_write(db: AsyncSession, request_id: uuid.UUID, **values: Any) -> None:
    """Write the make-ready columns only (P8), while the review is active."""
    await db.execute(
        update(LookRequest)
        .where(LookRequest.id == request_id, LookRequest.state == "active")
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def look_prepare(ctx: jobs.JobContext, job: LocalAiJob) -> dict[str, Any] | None:
    """Make a review's clips ready to be looked at (D11), and watch them.

    Every run: each clip in the folder that needs a playable copy or its
    shots, and has not been asked for yet, is queued on the ordinary
    pipeline — all of them on the first run (`prep_queued_at` is None;
    `prep_state` is already "running", set when the review was started),
    and after that the ones that came into the folder, or got far enough
    to be asked for, since. Which clips were asked for is kept in the job's
    payload, so none is asked for twice. Then what is still not ready is
    counted. Ends when nothing is left, or when nothing has become ready
    (and nothing new was asked for) for PREPARE_STALLED_S and the queues
    that make clips ready are empty — those left are judged from one still
    by the review (1.4 row 6). Meanwhile it waits PREPARE_EVERY_S between
    looks; a drive for playable copies that is away, or does not answer,
    holds it without counting against the clips (1.4 row 7)."""
    request_id = _request_id(job)
    async with ctx.factory() as db:
        request = None if request_id is None else await _request(db, request_id)
        if request is None or request.state != "active" or request.prep_state != "running":
            return {"gone": True}
        now = ctx.deps.now()
        moment = now.astimezone(UTC)
        first = request.prep_queued_at is None
        away = await _copies_away(ctx.deps.settings)
        if away is not None:
            # Copies cannot be written, nor shots found from them: wait for
            # the drive, and the stall clock does not run meanwhile.
            if not first:
                await _prep_write(db, request.id, prep_progress_at=moment)
            # Said in the job's phase too, so the review's own line can name
            # the drive instead of "waiting for clips" (`reviews.shown`).
            ctx.latest.set(
                phase=reviews.PHASE_DRIVE, done=request.prep_ready, total=request.prep_total
            )
            raise jobs.Later(away, not_before=now + PREPARE_WAIT)
        key, redo = await _prepare_key(db, request)
        place = scope.Scope(request.library_id, request.folder)
        need_copy, need_shots = await scope.not_ready_clips(db, place, key=key, redo=redo)
        waiting = {str(asset_id) for asset_id in (*need_copy, *need_shots)}
        left = len(waiting)
        kept = set() if first else _asked_of(job)
        if kept is None:
            # A job from before the list was kept: whatever it waits for was
            # asked for on its first run.
            kept = set(waiting)
        # Asked for and no longer waiting: ready (or gone from the folder).
        became_ready = len(kept - waiting)
        asked = kept & waiting
        late_copy = [one for one in need_copy if str(one) not in asked]
        late_shots = [one for one in need_shots if str(one) not in asked]
        if late_copy or late_shots:
            # After the first run: a clip filmed, copied in or first given
            # its poster since. Where the library makes no copies of its
            # own nobody else would ever ask for it, and the review would
            # wait out the stall for a clip that was never on its way.
            try:
                _queue_ready(late_copy, late_shots, asked)
            except Exception:
                log.warning("looks.prepare_queue_down", exc_info=True)
                if first:
                    await _prep_write(db, request.id, prep_state="done", prep_gave_up=left)
                    raise jobs.Failed(QUEUE_DOWN) from None
                # Later on, those not reached are asked for at the next look.
        # Something became ready, or was asked for just now: either way the
        # hours without progress are counted from here.
        moved = first or bool(became_ready) or len(asked) > len(kept & waiting)
        if first or asked != kept:
            await _keep_asked(db, job, asked)
        ready = (0 if first else request.prep_ready) + became_ready
        total = ready + left
        if moved or total != request.prep_total:
            values: dict[str, Any] = {"prep_total": total, "prep_ready": ready}
            if moved:
                values["prep_progress_at"] = moment
            if first:
                values["prep_queued_at"] = moment
            await _prep_write(db, request.id, **values)
        progress_at = moment if moved else _utc(request.prep_progress_at) or moment
        if not left:
            await _prep_write(db, request.id, prep_state="done", prep_gave_up=0)
            return {"ready": ready, "gave_up": 0}
        stalled = (moment - progress_at).total_seconds() >= PREPARE_STALLED_S
        if stalled and await _queues_empty(ctx.deps.settings):
            # Nothing became ready for hours and nothing is on its way: these
            # clips cannot be made ready (a failed copy, a damaged file).
            await _prep_write(db, request.id, prep_state="done", prep_gave_up=left)
            return {"ready": ready, "gave_up": left}
        await ctx.progress(phase="preparing", done=ready, total=total)
        raise jobs.Later(
            getting_ready(total, left),
            not_before=now + timedelta(seconds=PREPARE_EVERY_S),
        )


def _prepare_title(job: LocalAiJob) -> str:
    return f"Getting {job.subject} ready to be looked at"


# --- a job that ended without its handler -----------------------------------------------


# Kept in the job's payload: how far the review had got when its worker was
# last found dead, so a review that kills its worker every time it runs is
# carried on once per bit of progress, not for ever.
_STOPPED_AT = "_stopped_at"


async def look_folder_abandoned(db: AsyncSession, job: LocalAiJob, why: jobs.Why) -> None:
    """A look job that ended without its handler finishing it.

    Its worker was restarted mid-review (`stopped`, by the lane's tick or by
    a page that reaped it): the same row goes back to the queue and carries
    on where it was (1.4 row 10), as long as the review got further since
    the last time this happened, nobody pressed Cancel, and the review is
    still active. The caller flushes or commits.

    Every other ending (cancelled before its turn, the install cannot run
    models, the handler crashed) is written to the review by
    `reviews._settle`, which reads the job — not here: `reviews.cancel`
    cancels the job first and then ends the review itself, guarded on
    `active`, and a review this hook had already ended would answer that
    Cancel with "It has already finished." instead."""
    if why != "stopped" or job.cancel_requested:
        return
    request_id = _request_id(job)
    request = None if request_id is None else await _request(db, request_id)
    if request is None or request.state != "active":
        return
    payload = dict(job.payload) if isinstance(job.payload, dict) else {}
    progress = request.done + request.failed
    try:
        before = int(payload.get(_STOPPED_AT, -1))
    except (TypeError, ValueError):
        before = -1
    if progress <= before:
        return
    payload[_STOPPED_AT] = progress
    await db.execute(
        update(LocalAiJob)
        .where(LocalAiJob.id == job.id, LocalAiJob.state == "failed")
        .values(state="queued", message=RESTARTED, finished_at=None, payload=payload)
        .execution_options(synchronize_session=False)
    )
    # The caller (the lane's tick, `jobs.reap`) reads the job again after this.
    await db.refresh(job)


async def _looked_since(db: AsyncSession, library_id: uuid.UUID, since: datetime) -> bool:
    """An item of the library's new footage was finished since `since`: a
    look by ingest (no review's), kept or recorded as failed. Both moments
    are the server's own clock in UTC — when the job row was made, when the
    look was written — never the Mac's wall clock."""
    return bool(
        await db.scalar(
            select(
                exists().where(
                    Look.asset_id == Asset.id,
                    Asset.library_id == library_id,
                    Look.request_id.is_(None),
                    Look.created_at >= since,
                )
            )
        )
    )


async def _deaths_before(db: AsyncSession, job: LocalAiJob, looker: str) -> int:
    """How many ingest jobs in a row had stopped with their worker under the
    Looker `looker` (its id), having looked at nothing, when `job` was made:
    the count the library's job before this one kept, or 0 when that one
    ended any other way or its count was another model's."""
    before = await db.scalar(
        select(LocalAiJob)
        .where(
            LocalAiJob.kind == INGEST_KIND,
            LocalAiJob.subject == job.subject,
            LocalAiJob.id != job.id,
            LocalAiJob.created_at <= job.created_at,
        )
        .order_by(LocalAiJob.created_at.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if before is None or before.state != "failed" or not isinstance(before.payload, dict):
        return 0
    kept = before.payload.get(_DEATHS)
    if not isinstance(kept, dict) or kept.get("model") != looker:
        return 0
    try:
        return max(0, int(kept.get("n", 0)))
    except (TypeError, ValueError):
        return 0


async def look_ingest_abandoned(db: AsyncSession, job: LocalAiJob, why: jobs.Why) -> None:
    """An ingest job that ended without its handler finishing it.

    Its worker stopped under it (`stopped`, found by the lane's tick or by
    the scanner's sweep). Once or twice that is a restart: the row is left
    as it ended and the next file, or the sweep, makes another job at once.
    But a dead worker writes nothing, so a job that stops its worker every
    time it runs was replaced at every sweep without end. So the deaths in
    a row are counted here — only those in which no item was finished; a
    job that got somewhere starts the count again, and so does a change of
    Looker — and at INGEST_DEATHS the job keeps the same hold mark as one
    that could not use the Looker (`reviews.hold_mark`) and says so in
    plain words: the library is left alone for INGEST_HOLD, then tried once
    more, and held again at once if that try dies the same way. The caller
    flushes or commits.

    The death is counted against whoever is the Looker now, when the dead
    job is found: the job row does not say which model it had loaded.

    Every other ending needs nothing here: the handler wrote what it had to
    before it raised."""
    if why != "stopped":
        return
    library_id = _library_id(job)
    if library_id is None or await _looked_since(db, library_id, job.created_at):
        return
    looker = await _looker(db)
    who = "" if looker is None else str(looker.id)
    deaths = await _deaths_before(db, job, who) + 1
    payload = dict(job.payload) if isinstance(job.payload, dict) else {}
    payload[_DEATHS] = {"n": deaths, "model": who}
    values: dict[str, Any] = {"payload": payload}
    if deaths >= INGEST_DEATHS and looker is not None:
        payload[reviews.HELD] = reviews.hold_mark(looker, reviews.HELD_BY_STOPPING)
        values["message"] = f"{KEEPS_STOPPING} {HELD_FOR}"
    await db.execute(
        update(LocalAiJob)
        .where(LocalAiJob.id == job.id, LocalAiJob.state == "failed")
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    # The caller (the lane's tick, `jobs.reap`) reads the job again after this.
    await db.refresh(job)


def _look_title(job: LocalAiJob) -> str:
    return f"Looking through {job.subject}"


jobs.register(
    LOOK_KIND, look_folder, needs_model=True, title=_look_title, abandoned=look_folder_abandoned
)
jobs.register(
    INGEST_KIND,
    look_ingest,
    needs_model=True,
    title=_ingest_title,
    abandoned=look_ingest_abandoned,
)
# Runs no model: Pause gives memory back to Adobe, and transcoding uses none (D11).
jobs.register(PREPARE_KIND, look_prepare, needs_model=False, title=_prepare_title)
