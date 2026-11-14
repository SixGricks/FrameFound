"""Folder reviews: who may start what, what the dialog is told, how a review reads.

A review is somebody asking for one folder (and everything beneath it, ""
being the whole library) to be looked at by a local model, now or
overnight. It is a `look_requests` row — what was asked and how far it got
— and one `look_folder` job on L1's lane, plus a `look_prepare` job when
clips must be made ready first. The API and the chat panel's tool both go
through here, so the rules (and their sentences) are said once.

A review starts only because somebody asked for it. Nothing here starts one
by itself: looking at what arrives is a library's own switch (ingest), and
that is not a review.

Every write here names its columns (the look job, the make-ready job and
the API all write a request row), flushes, and leaves the commit to the
caller, which then wakes the lane.

`now` is the Mac's wall clock, naive, as L1's `Deps.now` gives it: the
overnight window is the wall clock's. It is handed to L1's `schedule` and
`jobs` as they expect, and never mixed with the time-zoned columns; what is
written to those is `datetime.now(UTC)`.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import (
    Asset,
    Library,
    LocalAiJob,
    LocalModel,
    LookRequest,
    User,
    catalogued,
)
from framefound.local_models import availability, jobs, policy, registry, schedule
from framefound.local_models.ask import NO_SUCH_MODEL
from framefound.local_models.kinds import kinds as runtime_kinds
from framefound.local_models.settings_store import load_config
from framefound.looking import PRIORITY_REVIEW, estimate, scope, store
from framefound.media import folders
from framefound.storage import stores

LOOK_KIND = "look_folder"
PREPARE_KIND = "look_prepare"
INGEST_KIND = "look_ingest"

# --- the sentences ------------------------------------------------------------------

NO_LOOKER = "No model has the Looker role yet. Give one the role on the AI models page."
NOTHING_NEW = "Everything here has been looked at."
# Nothing here was ever looked at either: an empty folder, a mistyped one, or
# one holding only what a model is never shown. Saying it was all reviewed
# would be false and would hide the typo.
NOTHING_HERE = "There is nothing here a model can look at."
ALREADY_RUNNING = "A review of this folder is already running."
ADMIN_ONLY = "Only an administrator can start a review."
# The lane could not be woken and the review was taken back (`withdraw`).
# L1's sentence, word for word (`api.v1.local_models.QUEUE_DOWN`; a test
# holds the two equal): kept here too because the chat panel's tool says it
# and nothing in this package imports the API.
QUEUE_DOWN = "The processing queue is unavailable. Nothing was started."
# The audit event of a review that started, whoever asked (`asked`).
AUDIT_STARTED = "looks.review_started"
NO_SUCH_LIBRARY = "No such library."
NO_SUCH_REVIEW = "There is no such review."
FINISHED_ALREADY = jobs.FINISHED_ALREADY
BAD_WHEN = 'Choose "now" or "tonight".'
CHOOSE_TONIGHT = "Choose Tonight."
DEMO_LINE = "Demonstration only: the captions, scores and tags written here are made up."

PAUSED_SHOWN = "Paused. It carries on when local AI is resumed."
WAITING_TURN = "Waiting its turn."
RECORD_GONE = "Its record is gone; start it again."
STILL_ASKED = "The playable copies already asked for are still being made."
# The make-ready job of a review that ended before it did.
PREPARE_OVER = "The review it was for has ended."

# What a review's two jobs say in `phase` while one waits on the other, so
# the page can tell why without reading a sentence for its meaning: the look
# job has looked at everything ready and waits for clips to be made ready;
# the make-ready job cannot go on because the drive for playable copies is
# away or does not answer (its message is that sentence).
PHASE_CLIPS = "waiting for clips"
PHASE_DRIVE = "drive away"

# After a library's ingest job failed because the Looker itself could not be
# used — or kept stopping with its worker before it had looked at anything —
# new footage in that library is left alone this long. Nothing is recorded
# against an item for such a failure, so without the hold every file that
# arrived and every scanner pass made another job that failed the same way.
INGEST_HOLD = timedelta(hours=6)
# Kept in the failed ingest job's payload (`hold_mark`): which model was the
# Looker, that model's row as it was then, and what stopped the job. Every
# hold ends early when the Looker is another model. One the Looker itself
# caused also ends when its row has changed since (its files were fetched
# again, or it answered something: a Test, a review) — it works again. The
# other two do not: an answer about other footage says nothing about the
# file that stopped the worker here, and with the Looker busy elsewhere
# every answer would lift the hold for the next scanner pass to kill the
# worker again.
HELD = "_held"
# What stopped it: the Looker could not be used; the job's worker stopped
# time after time; something nobody expected.
HELD_BY_MODEL = "model"
HELD_BY_STOPPING = "stopped"
HELD_BY_ERROR = "error"

# A job's subject is at most this long (`local_ai_jobs.subject`). A longer
# one is cut and given the request's first eight hex digits, so two long
# folders that share a beginning are still two subjects.
SUBJECT_CHARS = 300

ShownState = Literal["tonight", "waiting", "paused", "looking", "done", "failed", "cancelled"]


class ReviewRefused(RuntimeError):
    """Not started, or not changed. `str()` is the sentence to show and
    `.status` the HTTP status the API answers with (400, 403, 404, 409)."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Candidate:
    """A model a review could use."""

    model: LocalModel
    fits: Literal["day", "night"]
    is_looker: bool
    # None: it may start now. Otherwise why not ("This model only runs overnight …").
    why_not_now: str | None
    # The program that runs it is not installed here: the sentence its review
    # would stop with at once. None when it is (or nobody asked the disk).
    program_missing: str | None = None


@dataclass(frozen=True)
class _Seen:
    """L1's settings and the Looker, read once for one question."""

    lists: policy.PublisherLists
    budgets: schedule.Budgets
    paused: bool
    kinds: Collection[str]
    looker_id: uuid.UUID | None
    # The programs not installed here, by kind name, with the sentence a
    # model's turn on one stops with (`comparisons.missing_programs`). Empty
    # when the caller did not ask the disk.
    missing: Mapping[str, str]


async def _seen(
    db: AsyncSession,
    known_kinds: Collection[str] | None,
    missing: Mapping[str, str] | None = None,
) -> _Seen:
    config = await load_config(db)
    roles = await registry.roles(db)
    return _Seen(
        lists=config.lists(),
        budgets=config.budgets(),
        paused=config.paused,
        kinds=tuple(runtime_kinds()) if known_kinds is None else tuple(known_kinds),
        looker_id=roles["looker"].model_id,
        missing={} if missing is None else dict(missing),
    )


# --- which model ----------------------------------------------------------------------


def name_of(model: LocalModel) -> str:
    """The model as the pages name it: the last part of its repository."""
    return model.repo_id.rpartition("/")[2] or model.repo_id


def why_not_now(model: LocalModel, now: datetime, budgets: schedule.Budgets) -> str | None:
    """None when the model's memory fits the budget in force at `now`; else the
    sentence. A pause is not a reason here: a review started while paused
    waits for Resume, and its row says so."""
    needed = registry.memory_gb(model)
    decision = schedule.decide(needed, now, budgets)
    if decision.run:
        return None
    if decision.why == "window" and not schedule.is_overnight(now, budgets):
        return (
            f"This model only runs overnight (needs about {schedule.gb_text(needed)} GB; "
            f"the daytime budget is {schedule.gb_text(budgets.day_gb)} GB)."
        )
    # The budgets the unusual way round (only a hand-edited row can be):
    # L1's own sentence says it properly.
    return decision.message


def _problem(model: LocalModel, seen: _Seen) -> str | None:
    return registry.role_problem(model, "looker", seen.lists, seen.budgets, seen.kinds)


def _program_missing(model: LocalModel, seen: _Seen) -> str | None:
    """The sentence when the program that runs this model is not installed
    here; else None. A review runs the model, so such a model is refused up
    front, as Compare refuses it: started, its review failed at once."""
    return seen.missing.get(model.runtime)


def _usable(model: LocalModel, seen: _Seen) -> str | None:
    """None when a review may use this model; else why not: the Looker's rule,
    then its program."""
    return _problem(model, seen) or _program_missing(model, seen)


async def candidates(
    db: AsyncSession,
    now: datetime,
    *,
    known_kinds: Collection[str] | None = None,
    missing: Mapping[str, str] | None = None,
) -> list[Candidate]:
    """The models a review could use: installed, reading pictures, approved
    under today's lists, run by a program this install has, and fitting one of
    the two budgets — exactly the models that could be given the Looker role.
    The Looker first, then by name.

    `known_kinds` is the names of this install's runtime kinds (the API passes
    its `Deps.kinds`); by default they are asked of `local_models.kinds`.
    `missing` is `comparisons.missing_programs`: a model whose program is not
    installed stays a choice, with `program_missing` saying why it cannot be
    used, so the dialog can name it and say what to install."""
    seen = await _seen(db, known_kinds, missing)
    models: Sequence[LocalModel] = (
        await db.scalars(
            select(LocalModel)
            .where(LocalModel.state == registry.INSTALLED)
            .execution_options(populate_existing=True)
        )
    ).all()
    found: list[Candidate] = []
    for model in models:
        if _problem(model, seen) is not None:
            continue
        fits = schedule.fits(registry.memory_gb(model), seen.budgets)
        if fits == "never":  # role_problem already refuses it; said again for the type
            continue
        found.append(
            Candidate(
                model=model,
                fits=fits,
                is_looker=model.id == seen.looker_id,
                why_not_now=why_not_now(model, now, seen.budgets),
                program_missing=_program_missing(model, seen),
            )
        )
    found.sort(key=lambda c: (not c.is_looker, name_of(c.model).casefold(), str(c.model.id)))
    return found


async def _chosen(
    db: AsyncSession, seen: _Seen, model_id: uuid.UUID | None
) -> tuple[LocalModel | None, str | None]:
    """The model a review would use and, when it may not be used, why.

    The one named (ReviewRefused 404 when it is gone), else the Looker. No
    Looker, or a Looker whose row is gone, is (None, NO_LOOKER). A model whose
    program is not installed (as `seen.missing` has it) may not be used: its
    review would only fail."""
    if model_id is not None:
        model = await registry.get_model(db, model_id)
        if model is None:
            raise ReviewRefused(NO_SUCH_MODEL, 404)
        return model, _usable(model, seen)
    if seen.looker_id is None:
        return None, NO_LOOKER
    model = await registry.get_model(db, seen.looker_id)
    if model is None:
        return None, NO_LOOKER
    return model, _usable(model, seen)


def _clean(folder: str) -> str:
    try:
        return folders.clean_folder(folder)
    except folders.FolderError as err:
        raise ReviewRefused(str(err), 400) from None


# --- the overnight window ---------------------------------------------------------------


def _next_start(now: datetime, budgets: schedule.Budgets) -> datetime:
    """The next time the wall clock reads the window's start, strictly after `now`."""
    at = budgets.night_start
    moment = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return moment if moment > now else moment + timedelta(days=1)


def tonight_problem(budgets: schedule.Budgets) -> str | None:
    """Why a review cannot be asked for overnight, or None. Every candidate
    fits the overnight budget (L1 refuses one smaller than the day's), so
    the only reason is a window that never opens."""
    if not schedule.has_window(budgets):
        clock = schedule.clock_text(budgets.night_start)
        return (
            f"There is no overnight window: it starts and ends at {clock}. Change it under "
            "Sharing this Mac, or choose Start now."
        )
    return None


def _tonight(now: datetime, budgets: schedule.Budgets) -> dict[str, Any]:
    if not schedule.has_window(budgets):
        clock = schedule.clock_text(budgets.night_start)
        return {
            "starts_at": None,
            "open_now": False,
            "problem": f"There is no overnight window: it starts and ends at {clock}.",
        }
    if schedule.is_overnight(now, budgets):
        return {
            "starts_at": None,
            "open_now": True,
            "problem": "The overnight window is open now — Start now uses it.",
        }
    return {
        "starts_at": _next_start(now, budgets).replace(tzinfo=None).isoformat(timespec="seconds"),
        "open_now": False,
        "problem": None,
    }


# --- what the dialog shows --------------------------------------------------------------


async def _folders_inside(db: AsyncSession, library_id: uuid.UUID, folder: str) -> int:
    """How many folders are directly inside `folder`."""
    dialect = db.get_bind().dialect.name
    beneath = (
        select(folders.child_name(folder, dialect).label("name"))
        .where(Asset.library_id == library_id, catalogued(), *folders.under(folder))
        .subquery()
    )
    found = await db.scalar(
        select(func.count(func.distinct(beneath.c.name))).where(beneath.c.name != "")
    )
    return int(found or 0)


async def _active_request(
    db: AsyncSession, library_id: uuid.UUID, folder: str
) -> LookRequest | None:
    """The review still active for exactly this library and folder. This
    table, not the job's subject, answers that: a subject is cut at 300
    characters, a folder is not."""
    return (
        await db.scalars(
            select(LookRequest)
            .where(
                LookRequest.library_id == library_id,
                LookRequest.folder == folder,
                LookRequest.state == "active",
            )
            .order_by(LookRequest.created_at.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).first()


def _kinds(photos: int, clips: int, shots: int) -> dict[str, int]:
    """Items by kind: photographs, clips (each once), and the shots counted
    one by one in those clips. A clip judged whole is a clip with no shots."""
    return {"photos": photos, "clips": clips, "shots": shots}


def _zeros(library: Library, folder: str, reason: str | None) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "library_id": str(library.id),
        "library_name": library.name,
        "folder": folder,
        "folders_inside": 0,
        "todo": {"photos": 0, "clips": 0, "shots": 0, "stills_only": 0},
        "already": {"items": 0, "by_another_model": 0},
        "already_kinds": _kinds(0, 0, 0),
        "by_another_kinds": _kinds(0, 0, 0),
        "first": {
            "need_copy": 0,
            "need_copy_footage_s": 0.0,
            "copy_away": 0,
            "need_shots": 0,
            "still_processing": 0,
            "problem": None,
        },
        "skipped": {"missing": 0, "audio": 0},
        "failed_before": 0,
        "model": None,
        "estimate": estimate.as_dict(None, estimate.NONE),
        "tonight": {"starts_at": None, "open_now": False, "problem": None},
        "active": None,
    }


async def preview(
    db: AsyncSession,
    library: Library,
    folder: str,
    *,
    model_id: uuid.UUID | None,
    redo: bool,
    now: datetime,
    known_kinds: Collection[str] | None = None,
    missing: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """What the review dialog shows for one folder (section 6.6), with the
    estimate's own words. Reads only; anyone signed in may ask.

    ReviewRefused 400 for a folder that is not one, 404 for a model that is
    gone. Where models cannot run: `available` false, the reason, every
    count 0. `missing` is as `start` takes it: a model whose program is not
    installed has that sentence as its `problem`, and `program_missing`."""
    folder = _clean(folder)
    found = availability()
    if not found.ok:
        return _zeros(library, folder, found.reason)
    seen = await _seen(db, known_kinds, missing)
    model, problem = await _chosen(db, seen, model_id)
    key = store.key_for(model) if model is not None else ""
    counts = await scope.count(db, scope.Scope(library.id, folder), key=key, redo=redo)
    speed = await estimate.speed(db, model) if model is not None else estimate.NONE
    seconds = estimate.seconds_for(counts, speed) if model is not None else None
    drive = None
    if counts.need_copy or counts.need_shots:
        # Asks the disk whether the store is there: off the event loop.
        drive = await asyncio.to_thread(stores.away, "proxies")
    active = await _active_request(db, library.id, folder)
    chosen = None
    if model is not None:
        fits = schedule.fits(registry.memory_gb(model), seen.budgets)
        chosen = {
            "id": str(model.id),
            "name": name_of(model),
            "fits": fits,
            "is_looker": model.id == seen.looker_id,
            "problem": problem,
            # What is wrong is its program, which the AI models page installs.
            "program_missing": problem is not None and problem == _program_missing(model, seen),
            "why_not_now": problem or why_not_now(model, now, seen.budgets),
            "tonight_problem": problem or tonight_problem(seen.budgets),
        }
    return {
        "available": True,
        "reason": DEMO_LINE if found.mode == "demo" else None,
        "library_id": str(library.id),
        "library_name": library.name,
        "folder": folder,
        "folders_inside": await _folders_inside(db, library.id, folder),
        "todo": {
            "photos": counts.photos,
            "clips": counts.clips,
            "shots": counts.shots,
            "stills_only": counts.stills_only,
        },
        "already": {"items": counts.already, "by_another_model": counts.by_other},
        # The same two numbers by kind, for the dialog to say them as it says
        # what is still to do. Beside `already`, not in it, so what reads
        # `already` reads what it always did.
        "already_kinds": _kinds(counts.already_photos, counts.already_clips, counts.already_shots),
        "by_another_kinds": _kinds(counts.other_photos, counts.other_clips, counts.other_shots),
        "first": {
            "need_copy": counts.need_copy,
            "need_copy_footage_s": counts.need_copy_footage_s,
            "copy_away": counts.copy_away,
            "need_shots": counts.need_shots,
            "still_processing": counts.not_ready,
            "problem": drive,
        },
        "skipped": {"missing": counts.missing, "audio": counts.audio},
        "failed_before": counts.failed_before,
        "no_looker": NO_LOOKER if model is None else None,
        "model": chosen,
        "estimate": estimate.as_dict(seconds, speed, unready=estimate.to_make_ready(counts)),
        "tonight": _tonight(now, seen.budgets),
        "paused": seen.paused,
        "active": None if active is None else await review(db, active, now),
    }


def _anything_to_do(counts: scope.Counts) -> bool:
    """Something to look at now, or something that will be once made ready (or
    judged from one still when it cannot be)."""
    return bool(counts.items or counts.need_copy or counts.need_shots or counts.copy_away)


def _are(count: int, what: str) -> str:
    return f"{count} {'is' if count == 1 else 'are'} {what}"


def nothing_to_do_text(counts: scope.Counts) -> str:
    """Why a folder with nothing to do was refused: all of it was looked at,
    or there was nothing a model could look at — and then what is there
    instead (files still being processed, missing, or sound only)."""
    if counts.already:
        return NOTHING_NEW
    parts = [
        _are(n, what)
        for n, what in (
            (counts.not_ready, "still being processed"),
            (counts.missing, "missing"),
            (counts.audio, "sound only"),
        )
        if n
    ]
    if not parts:
        return NOTHING_HERE
    listed = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
    return f"{NOTHING_HERE} Of the files here, {listed}."


def _subject(library: Library, folder: str, request_id: uuid.UUID) -> str:
    text = f"{library.name} / {folder}" if folder else library.name
    if len(text) <= SUBJECT_CHARS:
        return text
    return f"{text[: SUBJECT_CHARS - 10]}… {request_id.hex[:8]}"


# --- starting, cancelling -------------------------------------------------------------


async def start(
    db: AsyncSession,
    user: User,
    *,
    library_id: uuid.UUID,
    folder: str,
    when: Literal["now", "tonight"],
    model_id: uuid.UUID | None,
    redo: bool,
    prepare: bool,
    now: datetime,
    known_kinds: Collection[str] | None = None,
    missing: Mapping[str, str] | None = None,
) -> LookRequest:
    """Start a review: the request row, its look job and, when clips are not
    ready and `prepare`, its make-ready job. Flushes; the caller commits, then
    wakes the lane (and, if the wake-up fails, calls `withdraw`).

    Refused (ReviewRefused), in this order, each with its sentence: models
    cannot run here (409, L1's); not an administrator (403); `when` (400);
    the folder (400); the library (404); the model — the one named (404, or
    409 with why it may not look, its program not being installed among
    them: `missing` is `comparisons.missing_programs`) or the Looker (409);
    Start now with a
    model that does not fit the hour's budget (409), Tonight with no window
    to wait for (409); a review of this folder still
    active (409); nothing to look at or make ready (409, saying whether it
    was all looked at or there was nothing a model could look at).

    Nothing here starts without being asked: this is the only way a folder
    review begins."""
    found = availability()
    if not found.ok:
        raise ReviewRefused(found.reason or "", 409)
    if user.role != "admin":
        raise ReviewRefused(ADMIN_ONLY, 403)
    if when not in ("now", "tonight"):
        raise ReviewRefused(BAD_WHEN, 400)
    folder = _clean(folder)
    library = await db.get(Library, library_id)
    if library is None:
        raise ReviewRefused(NO_SUCH_LIBRARY, 404)
    seen = await _seen(db, known_kinds, missing)
    model, problem = await _chosen(db, seen, model_id)
    if model is None or problem is not None:
        raise ReviewRefused(problem or NO_LOOKER, 409)
    not_before: datetime | None = None
    if when == "now":
        wait = why_not_now(model, now, seen.budgets)
        if wait is not None:
            raise ReviewRefused(f"{wait} {CHOOSE_TONIGHT}", 409)
    else:
        night = tonight_problem(seen.budgets)
        if night is not None:
            raise ReviewRefused(night, 409)
        if not schedule.is_overnight(now, seen.budgets):
            not_before = _next_start(now, seen.budgets)
    # Before the counts: a folder under review may well have nothing new
    # left, and "already running" is the truer answer. Settled first, so a
    # review whose job was cancelled on the AI models page, or failed, does
    # not refuse the next one until somebody happens to open Processing.
    running = await _active_request(db, library.id, folder)
    if running is not None:
        ids = [i for i in (running.look_job_id, running.prepare_job_id) if i is not None]
        by_id = await _jobs_by_id(db, ids)
        await _settle(
            db,
            running,
            by_id.get(running.look_job_id) if running.look_job_id else None,
            by_id.get(running.prepare_job_id) if running.prepare_job_id else None,
            now=now,
        )
        if running.state == "active":
            raise ReviewRefused(ALREADY_RUNNING, 409)
    counts = await scope.count(
        db, scope.Scope(library.id, folder), key=store.key_for(model), redo=redo
    )
    if not _anything_to_do(counts):
        raise ReviewRefused(nothing_to_do_text(counts), 409)

    request_id = uuid.uuid4()
    subject = _subject(library, folder, request_id)
    # A job of an ended review (cancelled while its worker was alive, which
    # then died) can still be "running" under this subject, and
    # `jobs.create` would refuse the new one as already running while the
    # page reads it as stopped. Ended here the way the lane's tick would.
    await _reap_dead(db, subject, now)
    payload = {"request_id": str(request_id)}
    preparing = prepare and bool(counts.need_copy or counts.need_shots)
    try:
        prepare_job = None
        if preparing:
            # Made first, so at the same priority it is claimed first and the
            # clips are on their way before the first item is looked at.
            prepare_job = await jobs.create(
                db,
                PREPARE_KIND,
                subject=subject,
                payload=payload,
                priority=PRIORITY_REVIEW,
                user_id=user.id,
            )
        look_job = await jobs.create(
            db,
            LOOK_KIND,
            subject=subject,
            model_id=model.id if model_id is not None else None,
            role=None if model_id is not None else "looker",
            payload=payload,
            priority=PRIORITY_REVIEW,
            not_before=not_before,
            user_id=user.id,
        )
    except jobs.JobRefused:
        raise ReviewRefused(ALREADY_RUNNING, 409) from None
    request = LookRequest(
        id=request_id,
        library_id=library.id,
        folder=folder,
        run_when=when,
        redo=redo,
        prepare=prepare,
        model_id=model_id,
        model_name=name_of(model),
        state="active",
        look_job_id=look_job.id,
        prepare_job_id=None if prepare_job is None else prepare_job.id,
        total=counts.items,
        # Set now, not when the make-ready job first runs: a look job that
        # got in first would otherwise read "" as "not asked for" and judge
        # the clips from one still before anyone tried to make them ready.
        prep_state="running" if preparing else "",
        requested_by=user.id,
        created_at=datetime.now(UTC),
    )
    db.add(request)
    await db.flush()
    return request


@dataclass(frozen=True)
class Asked:
    """What a review that has just been started was asked to do, counted
    once for everyone who says so: the audit row the API and the chat
    panel's tool both write, and what the tool tells Claude."""

    library_name: str
    # None only when it was removed in the moment since `start` chose it.
    model: LocalModel | None
    counts: scope.Counts
    # The audit row's detail (`AUDIT_STARTED`), the same whoever asked.
    detail: dict[str, Any]


async def asked(db: AsyncSession, request: LookRequest) -> Asked:
    """What `start` just counted for `request`, for the audit row and the
    answer. Reads only, and is read before the lane is woken: in demo mode
    the lane runs in this process and would already be looking by then."""
    library = await db.get(Library, request.library_id)
    name = library.name if library is not None else ""
    model_id = request.model_id
    if model_id is None:
        model_id = (await registry.roles(db))["looker"].model_id
    model = None if model_id is None else await registry.get_model(db, model_id)
    counts = await scope.count(
        db,
        scope.Scope(request.library_id, request.folder),
        key=store.key_for(model) if model is not None else "",
        redo=request.redo,
    )
    detail = {
        "library": name,
        "folder": request.folder,
        "when": request.run_when,
        "model": request.model_name,
        "redo": request.redo,
        "photos": counts.photos,
        "shots": counts.shots,
        "need_copy": counts.need_copy,
    }
    return Asked(library_name=name, model=model, counts=counts, detail=detail)


async def withdraw(db: AsyncSession, request_id: uuid.UUID) -> bool:
    """Take back a review that could not be started (the lane could not be
    woken): its request and its jobs. Whether it was taken back (True also
    when nothing of it is left). Flushes; the caller commits.

    Only while neither of its jobs has ever been run. The review was
    committed before the wake-up, so the once-a-minute tick may have claimed
    one of its jobs while the wake-up was failing — and a wake-up can fail
    after its message was sent. That job may already have asked for playable
    copies: its row is not deleted under it, and nobody is told nothing
    started.

    "Never run" is `started_at` still empty, not the job's state alone: the
    make-ready job's first run asks for the copies and then steps back to
    `waiting`, where it rests between looks, so a job that has run reads
    exactly like one that has not. The lane writes `started_at` when it
    first claims a job and nothing clears it; a job set to wait before its
    turn (Tonight, Pause) has none.

    The jobs go in one statement that says so; if it removes fewer than
    both, that statement alone is undone (a savepoint: whatever else the
    caller has written in this transaction stays) and the review stands as
    started, False."""
    request = await db.get(LookRequest, request_id, populate_existing=True)
    if request is None:
        return True
    ids = [i for i in (request.look_job_id, request.prepare_job_id) if i is not None]
    point = await db.begin_nested()
    removed: Any = await db.execute(
        delete(LocalAiJob)
        .where(
            LocalAiJob.id.in_(ids),
            LocalAiJob.state.in_(("queued", "waiting")),
            LocalAiJob.started_at.is_(None),
        )
        .execution_options(synchronize_session=False)
    )
    if removed.rowcount != len(ids):
        # One was claimed (or has even finished): put back whichever was not.
        await point.rollback()
        return False
    await db.execute(
        delete(LookRequest)
        .where(LookRequest.id == request_id)
        .execution_options(synchronize_session=False)
    )
    await point.commit()
    await db.flush()
    return True


async def end_prepare(db: AsyncSession, request_id: uuid.UUID) -> None:
    """The review has ended: end its make-ready job too, if it is still at
    work. Flushes; the caller commits.

    Left alone, that job sat `waiting` until its next look found the review
    over — and a job of the same kind about the same folder may not be made
    while one is active, so starting the folder again in those minutes was
    refused as "already running" when nothing was. Nothing is done while the
    review is active, and a make-ready job that already ended is left as it
    ended. Copies and analyses already asked for are not withdrawn.

    Whoever ends a review calls this: the look job's handler, and `_settle`
    for a review whose look job ended without its handler."""
    request = await db.get(LookRequest, request_id, populate_existing=True)
    if request is None or request.state == "active":
        return
    if request.prepare_job_id is not None:
        ended: Any = await db.execute(
            update(LocalAiJob)
            .where(
                LocalAiJob.id == request.prepare_job_id,
                LocalAiJob.kind == PREPARE_KIND,
                LocalAiJob.state.in_(("queued", "waiting")),
            )
            .values(
                state="done",
                message=PREPARE_OVER,
                not_before=None,
                finished_at=datetime.now(UTC),
            )
            .execution_options(synchronize_session=False)
        )
        if not ended.rowcount:
            # Running this moment (a second runner), or gone, or finished:
            # a running one is asked to stop, and ends instead of waiting.
            with contextlib.suppress(jobs.NoSuchJob, jobs.JobRefused):
                await jobs.request_cancel(db, request.prepare_job_id)
    await db.execute(
        update(LookRequest)
        .where(LookRequest.id == request_id, LookRequest.prep_state == "running")
        .values(prep_state="done")
        .execution_options(synchronize_session=False)
    )
    await db.flush()


def cancelled_text(request: LookRequest) -> str:
    """The closing sentence of a cancelled review, from its counters as they are."""
    text = f"Cancelled. {request.done} of {request.total} were looked at; their results are kept."
    return f"{text} {STILL_ASKED}" if request.prep_queued_at is not None else text


async def cancel(
    db: AsyncSession, request_id: uuid.UUID, *, now: datetime | None = None
) -> LookRequest:
    """Cancel a review: both its jobs are asked to stop (a running one stops
    between two items) and the request becomes `cancelled`. Playable copies
    already queued are not withdrawn — they are ordinary copies, useful
    anyway — and the closing sentence says so. Flushes; the caller commits.

    ReviewRefused 404 for no such review, 409 when it has already finished."""
    request = await db.get(LookRequest, request_id, populate_existing=True)
    if request is None:
        raise ReviewRefused(NO_SUCH_REVIEW, 404)
    if request.state != "active":
        raise ReviewRefused(FINISHED_ALREADY, 409)
    moment = now if now is not None else datetime.now(UTC)
    for job_id in (request.look_job_id, request.prepare_job_id):
        if job_id is None:
            continue
        job = await db.get(LocalAiJob, job_id, populate_existing=True)
        if job is not None and job.state == "running":
            # Its worker died: asking it to stop would leave the row running
            # for ever, and the next review of the folder refused by it.
            await jobs.reap(db, job, moment)
        # Pruned, or it had finished: nothing of it is left to stop.
        with contextlib.suppress(jobs.NoSuchJob, jobs.JobRefused):
            await jobs.request_cancel(db, job_id)
    ended: Any = await db.execute(
        update(LookRequest)
        .where(LookRequest.id == request_id, LookRequest.state == "active")
        .values(state="cancelled", message=cancelled_text(request), finished_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    await db.refresh(request)
    if not ended.rowcount:
        raise ReviewRefused(FINISHED_ALREADY, 409)
    return request


# --- how a review reads -----------------------------------------------------------------


def shown(
    request: LookRequest,
    look_job: LocalAiJob | None,
    prepare_job: LocalAiJob | None,
    now: datetime,
    *,
    budgets: schedule.Budgets | None = None,
) -> tuple[ShownState, str]:
    """(state, message) as the page reads them.

    The request's own state is final once it is done, failed or cancelled.
    While it is active the look job says where it is (read through
    `jobs.shown`, so a worker that died reads as stopped before the lane's
    tick has said so). When the look job has nothing of its own to say, the
    make-ready job's sentence is shown ("Getting 12 clips ready …", or that
    the drive for playable copies is away).

    One case turns that round. A look job that has looked at everything
    ready says "Waiting for 3 clips to be made ready." — and if those clips
    are not being made because the drive for playable copies is away or not
    answering, that sentence leaves the operator waiting for something only
    they can put right. Then the make-ready job's sentence, which names the
    drive, is the one shown. The two jobs say which case they are in by
    their `phase` (PHASE_CLIPS, PHASE_DRIVE), not by their wording."""
    budgets = budgets or schedule.Budgets()
    if request.state == "cancelled":
        return "cancelled", cancelled_text(request)
    if request.state in ("done", "failed"):
        return ("done" if request.state == "done" else "failed"), request.message
    if look_job is None:
        return "failed", RECORD_GONE
    state, message = jobs.shown(look_job, now)
    preparing = ""
    drive_away = False
    if prepare_job is not None and prepare_job.state in jobs.ACTIVE:
        at_work, preparing = jobs.shown(prepare_job, now)
        # Still held by the drive as far as anybody knows: waiting to look
        # again, or looking this moment. Not one whose worker died, which
        # reads as stopped and whose last sentence is no longer news.
        drive_away = at_work in jobs.ACTIVE and prepare_job.phase == PHASE_DRIVE and bool(preparing)
    if state == "waiting":
        if message == schedule.PAUSED:
            return "paused", PAUSED_SHOWN
        if request.run_when == "tonight" and look_job.started_at is None:
            return "tonight", f"Starts tonight at {schedule.clock_text(budgets.night_start)}."
        if drive_away and look_job.phase == PHASE_CLIPS:
            return "waiting", preparing
        return "waiting", message or preparing or WAITING_TURN
    if state == "queued":
        return "waiting", preparing or WAITING_TURN
    if state == "running":
        return "looking", message
    if state == "cancelled":
        return "cancelled", cancelled_text(request)
    if state == "done":
        return "done", message or _finished_text(request)
    return "failed", message


def _finished_text(request: LookRequest) -> str:
    return f"Finished: {request.done} of {request.total} were looked at."


def _iso(moment: datetime | None) -> str | None:
    """A moment from the table as UTC with a Z. SQLite hands back what was
    written without its zone; everything written is UTC."""
    if moment is None:
        return None
    aware = moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    return aware.isoformat(timespec="seconds").replace("+00:00", "Z")


async def _settle(
    db: AsyncSession,
    request: LookRequest,
    look_job: LocalAiJob | None,
    prepare_job: LocalAiJob | None,
    *,
    now: datetime | None = None,
) -> None:
    """Write back what a finished job means for an active request.

    A look job cancelled from the AI models page, failed, or pruned would
    otherwise leave the review "active" for ever and block the next review
    of the folder. So would one whose worker died: a running job that
    `jobs.shown` reads as stopped is ended first (`jobs.reap`, as the lane's
    tick would), so Start agrees with what the page shows even while the
    lane is down. A make-ready job that ended without saying so is marked
    done, so the look job does not wait on clips nobody is making. And a
    review ended here takes its make-ready job with it (`end_prepare`), as
    one ended by its handler does: that job would otherwise wait two more
    minutes under the folder's subject, and Start be refused meanwhile as
    "already running". Each is one guarded UPDATE naming its columns."""
    if request.state != "active":
        return
    moment = now if now is not None else datetime.now(UTC)
    for job in (look_job, prepare_job):
        if job is not None and job.state == "running":
            await jobs.reap(db, job, moment)
    if (
        request.prep_state == "running"
        and request.prepare_job_id is not None
        and (prepare_job is None or prepare_job.state in jobs.FINISHED)
    ):
        await db.execute(
            update(LookRequest)
            .where(LookRequest.id == request.id, LookRequest.prep_state == "running")
            .values(prep_state="done")
            .execution_options(synchronize_session=False)
        )
    ending: tuple[str, str] | None = None
    if look_job is None:
        ending = ("failed", RECORD_GONE)
    elif look_job.state == "cancelled":
        ending = ("cancelled", cancelled_text(request))
    elif look_job.state == "failed":
        ending = ("failed", look_job.message)
    elif look_job.state == "done":
        ending = ("done", look_job.message or _finished_text(request))
    if ending is not None:
        await db.execute(
            update(LookRequest)
            .where(LookRequest.id == request.id, LookRequest.state == "active")
            .values(state=ending[0], message=ending[1][:500], finished_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
    await db.flush()
    await db.refresh(request)
    if request.state != "active":
        # Ended just now — here, or by its handler in the moment since the
        # caller read it. Either way nothing is left for that job to do.
        await end_prepare(db, request.id)
        await db.refresh(request)


async def _reap_dead(db: AsyncSession, subject: str, now: datetime) -> None:
    """End the running review jobs about `subject` whose worker died."""
    running: Sequence[LocalAiJob] = (
        await db.scalars(
            select(LocalAiJob)
            .where(
                LocalAiJob.kind.in_((LOOK_KIND, PREPARE_KIND)),
                LocalAiJob.subject == subject,
                LocalAiJob.state == "running",
            )
            .execution_options(populate_existing=True)
        )
    ).all()
    for job in running:
        await jobs.reap(db, job, now)


async def _jobs_by_id(db: AsyncSession, ids: Collection[uuid.UUID]) -> dict[uuid.UUID, LocalAiJob]:
    if not ids:
        return {}
    found = await db.scalars(
        select(LocalAiJob)
        .where(LocalAiJob.id.in_(list(ids)))
        .execution_options(populate_existing=True)
    )
    return {job.id: job for job in found}


@dataclass
class _Lookup:
    """What several reviews share, read once: names, emails, speeds."""

    lists: policy.PublisherLists
    budgets: schedule.Budgets
    looker_id: uuid.UUID | None
    demo: bool
    libraries: dict[uuid.UUID, str]
    emails: dict[uuid.UUID, str]
    speeds: dict[uuid.UUID, estimate.Speed]


async def _lookup(db: AsyncSession, requests: Sequence[LookRequest]) -> _Lookup:
    config = await load_config(db)
    roles = await registry.roles(db)
    library_ids = {r.library_id for r in requests}
    user_ids = {r.requested_by for r in requests if r.requested_by is not None}
    libraries: dict[uuid.UUID, str] = {}
    if library_ids:
        rows = await db.execute(
            select(Library.id, Library.name).where(Library.id.in_(list(library_ids)))
        )
        libraries = {row[0]: row[1] for row in rows}
    emails: dict[uuid.UUID, str] = {}
    if user_ids:
        rows = await db.execute(select(User.id, User.email).where(User.id.in_(list(user_ids))))
        emails = {row[0]: row[1] for row in rows}
    return _Lookup(
        lists=config.lists(),
        budgets=config.budgets(),
        looker_id=roles["looker"].model_id,
        demo=availability().mode == "demo",
        libraries=libraries,
        emails=emails,
        speeds={},
    )


async def _speed_for(db: AsyncSession, request: LookRequest, seen: _Lookup) -> estimate.Speed:
    model_id = request.model_id or seen.looker_id
    if model_id is None:
        return estimate.NONE
    if model_id not in seen.speeds:
        model = await registry.get_model(db, model_id)
        seen.speeds[model_id] = estimate.NONE if model is None else await estimate.speed(db, model)
    return seen.speeds[model_id]


async def _review_out(
    db: AsyncSession,
    request: LookRequest,
    jobs_by_id: Mapping[uuid.UUID, LocalAiJob],
    seen: _Lookup,
    now: datetime,
) -> dict[str, Any]:
    look_job = jobs_by_id.get(request.look_job_id) if request.look_job_id else None
    prepare_job = jobs_by_id.get(request.prepare_job_id) if request.prepare_job_id else None
    state, message = shown(request, look_job, prepare_job, now, budgets=seen.budgets)
    active = request.state == "active"
    seconds_left = None
    if active:
        seconds_left = estimate.seconds_left(
            request.total - request.done, await _speed_for(db, request, seen)
        )
    return {
        "id": str(request.id),
        "library_id": str(request.library_id),
        "library_name": seen.libraries.get(request.library_id, ""),
        "folder": request.folder,
        "when": request.run_when,
        "redo": request.redo,
        "prepare": request.prepare,
        "model_name": request.model_name,
        "state": state,
        "message": message,
        "not_before": _iso(look_job.not_before) if look_job is not None and active else None,
        "done": request.done,
        "total": request.total,
        "failed": request.failed,
        "tags_suggested": request.tags_suggested,
        "new_tags": request.new_tags,
        "held": request.held,
        "stills_only": request.stills_only,
        "preparing": {
            "state": request.prep_state,
            "total": request.prep_total,
            "ready": request.prep_ready,
            "gave_up": request.prep_gave_up,
        },
        "seconds_left": seconds_left,
        "requested_by": (
            seen.emails.get(request.requested_by) if request.requested_by is not None else None
        ),
        "created_at": _iso(request.created_at),
        "started_at": _iso(request.started_at),
        "finished_at": _iso(request.finished_at),
        "can_cancel": active,
        "demo": seen.demo,
    }


async def review(db: AsyncSession, request: LookRequest, now: datetime) -> dict[str, Any]:
    """One review as the pages read it (section 6.7's `Review`). Reads only:
    a finished job under an active request is written back by `listing`."""
    seen = await _lookup(db, [request])
    ids = [i for i in (request.look_job_id, request.prepare_job_id) if i is not None]
    return await _review_out(db, request, await _jobs_by_id(db, ids), seen, now)


# --- new footage: a library that is being left alone -----------------------------------


@dataclass(frozen=True)
class Hold:
    """Looking at a library's new footage is held after a job that failed."""

    until: datetime  # UTC: the first moment another job may be made
    # What stopped the job that set it: HELD_BY_MODEL, _STOPPING or _ERROR.
    cause: str


def _row_stamp(model: LocalModel | None) -> str:
    """The model's row as it is now, as one comparable word: when it was
    last written. Fetching its files again, and every answer it gives, write
    the row."""
    changed = None if model is None else model.updated_at
    if changed is None:
        return ""
    aware = changed.replace(tzinfo=UTC) if changed.tzinfo is None else changed.astimezone(UTC)
    return aware.isoformat()


def hold_mark(looker: LocalModel, cause: str) -> dict[str, str]:
    """What an ingest job that failed keeps under `HELD` in its payload, so
    the library is left alone for INGEST_HOLD: who the Looker was, its row
    as it is now (read after the failure: whatever the failed question
    wrote to the row is part of "as it was then"), and what stopped the job."""
    return {"model": str(looker.id), "seen": _row_stamp(looker), "why": cause}


async def ingest_hold(
    db: AsyncSession, subject: str, looker: LocalModel, now: datetime
) -> Hold | None:
    """The hold on new footage in the library `subject`, or None when it may
    be looked at: its newest ingest job failed and kept a mark (`hold_mark`),
    less than INGEST_HOLD ago, and the Looker is still that model. `now` is
    the Mac's wall clock, naive, or an aware moment.

    A hold the Looker caused (HELD_BY_MODEL) is also over once the model's
    row has been written since: it answered something, so it works again.
    A hold after the worker kept stopping, or after an unexpected error,
    lasts its full time whatever the Looker answers elsewhere — that says
    nothing about what went wrong here.

    A job that failed without a mark (its worker died, once) holds nothing:
    the files it left are picked up at once."""
    newest = await db.scalar(
        select(LocalAiJob)
        .where(LocalAiJob.kind == INGEST_KIND, LocalAiJob.subject == subject)
        .order_by(LocalAiJob.created_at.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if newest is None or newest.state != "failed" or newest.finished_at is None:
        return None
    held = newest.payload.get(HELD) if isinstance(newest.payload, dict) else None
    if not isinstance(held, dict):
        return None
    if held.get("model") != str(looker.id):
        return None
    # A mark from before the cause was kept was always the Looker's.
    cause = str(held.get("why") or HELD_BY_MODEL)
    if cause == HELD_BY_MODEL and held.get("seen") != _row_stamp(looker):
        return None
    ended = newest.finished_at
    ended = ended.replace(tzinfo=UTC) if ended.tzinfo is None else ended.astimezone(UTC)
    until = ended + INGEST_HOLD
    if now.astimezone(UTC) >= until:
        return None
    return Hold(until=until, cause=cause)


def _in_about(seconds: float) -> str:
    """How long until something, as somebody would say it. It happens at the
    scanner's next pass after that (every five minutes), so nothing finer."""
    minutes = seconds / 60
    if minutes >= 90:
        return f"in about {int(minutes / 60 + 0.5)} hours"
    if minutes >= 50:
        return "in about an hour"
    if minutes >= 8:
        return f"in about {int(minutes / 5 + 0.5) * 5} minutes"
    return "in a few minutes"


def held_text(hold: Hold, now: datetime) -> str:
    """The one sentence a held library's line reads: what stopped looking at
    its new footage, and when it is tried again. `now` as `ingest_hold`'s."""
    when = _in_about((hold.until - now.astimezone(UTC)).total_seconds())
    if hold.cause == HELD_BY_MODEL:
        return (
            "Looking at new footage stopped because of a problem with the model, and is "
            f"tried again {when}."
        )
    return f"Looking at new footage stopped after something went wrong, and is tried again {when}."


def cannot_look_text(problem: str) -> str:
    """The one sentence a library's line reads while the Looker may not look
    (`registry.role_problem`'s sentence, which says what to put right): no
    time is promised, because nothing is tried until somebody does."""
    return f"Looking at new footage is stopped until the Looker can be used. {problem}"


async def _ingest_out(
    db: AsyncSession, seen: _Lookup, now: datetime, looker: LocalModel | None
) -> list[dict[str, Any]]:
    """One line per library that looks at new footage: what waits, and its job.

    A library with no job at work is `idle` — unless nothing is coming for
    the files that wait there, and then the line reads `failed` and says why
    in one sentence:

    - The Looker may not look as things stand (its files are not whole, it
      is no longer approved, it fits no budget, its program is not one this
      install has). No job is made for such a Looker (`runner._may_look`
      asks the same question of the same kinds), so nothing is tried until
      somebody puts it right: the sentence says what, and promises no time.
      Asked before the hold: a job that failed for this very reason left a
      hold behind, and "tried again in about 6 hours" would be untrue.
    - Otherwise, the library is being left alone after a job that failed
      (`ingest_hold`): the sentence says when it is tried again, and the
      line carries `retry_at` (UTC), the one key a line has only then."""
    key = "" if looker is None else store.key_for(looker)
    problem = (
        None
        if looker is None
        else registry.role_problem(
            looker, "looker", seen.lists, seen.budgets, tuple(runtime_kinds())
        )
    )
    libraries: Sequence[Library] = (
        await db.scalars(
            select(Library).where(Library.look_at_ingest.is_(True)).order_by(Library.name)
        )
    ).all()
    if not libraries:
        return []
    running: Sequence[LocalAiJob] = (
        await db.scalars(
            select(LocalAiJob)
            .where(LocalAiJob.kind == INGEST_KIND, LocalAiJob.state.in_(jobs.ACTIVE))
            .order_by(LocalAiJob.created_at.desc())
            .execution_options(populate_existing=True)
        )
    ).all()
    by_library: dict[str, LocalAiJob] = {}
    for one in running:
        named = one.payload.get("library_id") if isinstance(one.payload, dict) else None
        if isinstance(named, str):
            by_library.setdefault(named, one)
    found: list[dict[str, Any]] = []
    for library in libraries:
        waiting = await scope.remaining(
            db,
            scope.Scope(library.id, "", since=library.look_since),
            key=key,
            redo=False,
            run=scope.Run(None),
        )
        job = by_library.get(str(library.id))
        state, message, done, total = "idle", "", 0, 0
        if job is not None:
            job_state, job_message = jobs.shown(job, now)
            done, total = int(job.done), int(job.total)
            if job_state == "running":
                state, message = "looking", job_message
            elif job_state == "waiting" and job_message == schedule.PAUSED:
                state, message = "paused", PAUSED_SHOWN
            elif job_state in ("waiting", "queued"):
                state, message = "waiting", job_message or WAITING_TURN
            else:
                state, message = "failed", job_message
        line: dict[str, Any] = {
            "library_id": str(library.id),
            "library_name": library.name,
            "waiting": waiting,
            "state": state,
            "message": message,
            "done": done,
            "total": total,
        }
        if job is None and problem is not None:
            line.update(state="failed", message=cannot_look_text(problem))
        elif job is None and looker is not None:
            hold = await ingest_hold(db, library.name, looker, now)
            if hold is not None:
                line.update(state="failed", message=held_text(hold, now), retry_at=_iso(hold.until))
        found.append(line)
    return found


async def listing(db: AsyncSession, now: datetime, recent: int = 20) -> dict[str, Any]:
    """Progress for the Processing page and the dialog (section 6.8): every
    active review, then the newest `recent` finished, and one line per
    library that looks at new footage.

    A finished job under an active request is written back here (one guarded
    UPDATE each), so a review cancelled from the AI models page, or whose job
    failed or was pruned, does not read "active" for ever. Those writes are
    flushed; the caller commits, and losing them costs nothing — the next
    listing writes them again."""
    if not availability().ok:
        return {"available": False, "reviews": [], "ingest": []}
    active: Sequence[LookRequest] = (
        await db.scalars(
            select(LookRequest)
            .where(LookRequest.state == "active")
            .order_by(LookRequest.created_at, LookRequest.id)
            .execution_options(populate_existing=True)
        )
    ).all()
    ids = {i for r in active for i in (r.look_job_id, r.prepare_job_id) if i is not None}
    by_id = await _jobs_by_id(db, ids)
    for request in active:
        look_job = by_id.get(request.look_job_id) if request.look_job_id else None
        prepare_job = by_id.get(request.prepare_job_id) if request.prepare_job_id else None
        await _settle(db, request, look_job, prepare_job, now=now)
    still = [r for r in active if r.state == "active"]
    finished: Sequence[LookRequest] = (
        await db.scalars(
            select(LookRequest)
            .where(LookRequest.state != "active")
            .order_by(LookRequest.finished_at.desc(), LookRequest.created_at.desc())
            .limit(recent)
            .execution_options(populate_existing=True)
        )
    ).all()
    shown_requests = [*still, *finished]
    seen = await _lookup(db, shown_requests)
    reviews = [await _review_out(db, r, by_id, seen, now) for r in shown_requests]
    looker = None if seen.looker_id is None else await registry.get_model(db, seen.looker_id)
    return {
        "available": True,
        "reviews": reviews,
        "ingest": await _ingest_out(db, seen, now, looker),
    }
