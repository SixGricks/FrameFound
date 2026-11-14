"""The work table of local AI and its one runner.

Every piece of local-AI work — a download, a copy to this machine, a runtime
install, a test, and whatever later phases ask of a model — is a row of
`local_ai_jobs`. The Celery task on the `local_ai` queue is only a wake-up:
`run_next` claims the most urgent runnable row and runs its handler. So
progress, Cancel, "waiting until 21:30", "paused" and "the worker died" are
all states a page can read, they outlive a tab and a process, and a wake-up
delivered twice finds nothing left to do (D12).

Two parties write a row. The runner names the columns it writes (progress,
heartbeat, the end); the API writes `cancel_requested` and nothing else
while a job runs. Neither writes a whole row back from a copy read earlier:
that is how `storage/move_job.py` once lost a Cancel.

Times written here are UTC. The clock the runner is handed (`Deps.now`) is
the Mac's own wall clock, naive (D24), because the overnight window is the
wall clock's; it is turned into UTC once, where it is read.
"""

import asyncio
import contextlib
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from framefound.db.models import LocalAiJob
from framefound.local_models import UNAVAILABLE, availability, library, schedule
from framefound.local_models.ask import Answer, ModelFailed, NotNow, sweep
from framefound.local_models.deps import Deps
from framefound.local_models.hub import HubError, Stopped
from framefound.local_models.runtimes import RuntimeProblem
from framefound.local_models.settings_store import load_config

log = structlog.get_logger()

ACTIVE = ("queued", "waiting", "running")
FINISHED = ("done", "failed", "cancelled")
# A running row whose heartbeat is older than this belonged to a worker that died.
STALE_AFTER_S = 120.0
# The runner beats for the row it is running, whatever the handler is doing:
# one ask() can outlast STALE_AFTER_S (a load is allowed five minutes and
# more), and a job must not read as dead while it waits for an answer.
HEARTBEAT_S = 30.0
# How often progress is written, and Cancel looked for, while work runs.
PROGRESS_EVERY_S = 2.0
# How long a job that may not run now waits before it is tried again, when
# the reason gives no time of its own. A pause is somebody's decision: it
# is looked at less often, and Resume releases the job at once anyway.
RETRY_AFTER = timedelta(seconds=60)
PAUSED_RETRY_AFTER = timedelta(minutes=5)
# The end of a job is written this many times before giving up: a job that
# finished and could not say so would read as stopped.
END_ATTEMPTS = 5
# A Cancel the work has not answered for this long is honoured by the runner
# itself: what the work waits on (a share that hung, a model server that holds
# the request) may never let it look, and the lane runs one job at a time.
CANCEL_GRACE_S = STALE_AFTER_S
# How long work in a thread is given to stop once it is told to, before it is
# left behind (a read blocked in the kernel never looks at `should_stop`).
LET_GO_S = 10.0

STOPPED = (
    "It stopped without finishing: the worker running it was restarted. "
    "Nothing is lost; start it again to carry on."
)
UNKNOWN_KIND = "This version of FrameFound does not know how to do this."
CANCELLED = "Cancelled."
LEFT_BEHIND = (
    "Cancelled. What it was waiting on (a drive, a share or the model server) did not "
    "answer, so it was left unfinished."
)
ALREADY = "That is already waiting its turn or under way."
FINISHED_ALREADY = "It has already finished."
MESSAGE_LENGTH = 500


class Cancelled(Exception):
    """Cancel was pressed: the job ends `cancelled`. `progress` raises it; a
    handler lets it propagate (after tidying what it owns)."""


class Later(Exception):
    """Raised by a handler: not finished, nothing wrong — run me again.

    With no `not_before` the row goes back to `queued` (so a more urgent
    job, or a waiting one whose time has come, runs first); with one it
    becomes `waiting`. `phase`, `done`, `total` and `started_at` are kept;
    `message` is shown.
    """

    def __init__(self, message: str = "", *, not_before: datetime | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.not_before = not_before


class Failed(RuntimeError):
    """Raised by a handler that failed for a reason of its own ("No picture
    could be read from this clip."): the job ends `failed` with `message` in
    the row and `detail` under Details."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class JobRefused(RuntimeError):
    """Not started, or not changed; the message is the sentence (the API answers 409 with it)."""


class NoSuchJob(LookupError):
    """There is no job with that id (the API answers 404)."""


Handler = Callable[["JobContext", LocalAiJob], Awaitable[dict[str, Any] | None]]
# How a job ended without its handler finishing it: cancelled before it ran
# (or while it ran, without the handler seeing it), its worker died, this
# install cannot run local AI at all, or the handler crashed on something it
# did not expect (the job's message says what).
Why = Literal["cancelled", "stopped", "unavailable", "failed"]
Abandoned = Callable[[AsyncSession, LocalAiJob, Why], Awaitable[None]]


@dataclass(frozen=True)
class Kind:
    handler: Handler
    # Runs a model: held while local AI is paused, and never claimed then.
    needs_model: bool
    title: Callable[[LocalAiJob], str]
    # Called, in the session that ends the job, when it ends without its
    # handler (cancelled while queued, its worker died): what the handler
    # would have tidied (a model left "downloading", a role left waiting).
    abandoned: Abandoned | None = None


_KINDS: dict[str, Kind] = {}


def register(
    kind: str,
    handler: Handler,
    *,
    needs_model: bool,
    title: Callable[[LocalAiJob], str],
    abandoned: Abandoned | None = None,
) -> None:
    """Make `kind` a job the lane can run. Registered at import (13.2)."""
    _KINDS[kind] = Kind(handler, needs_model, title, abandoned)


def registered() -> tuple[str, ...]:
    return tuple(_KINDS)


def title_of(job: LocalAiJob) -> str:
    """What Recent activity calls the job."""
    known = _KINDS.get(job.kind)
    if known is None:
        return job.subject or job.kind
    try:
        return known.title(job)
    except Exception:  # a title is words on a page: never a reason to fail one
        return job.subject or job.kind


# --- the clock -----------------------------------------------------------------------


def _utc(moment: datetime) -> datetime:
    """A moment from a clock, in UTC. A naive one is the Mac's wall clock (D24)."""
    return moment.astimezone(UTC)


def _stored(moment: datetime | None) -> datetime | None:
    """A moment read from the table, in UTC. SQLite hands back what was written
    without its zone; everything written here is UTC."""
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _cut(text: str) -> str:
    return text if len(text) <= MESSAGE_LENGTH else text[: MESSAGE_LENGTH - 1] + "…"


# The time a requester asked a job to wait for ("tonight"), kept in its payload:
# `not_before` is also written by the runner for waits it worked out itself
# (the window, a pause, a server that did not answer), and only those are
# released early when the settings change.
_ASKED = "_not_before"


def _asked_until(job: LocalAiJob) -> datetime | None:
    raw = job.payload.get(_ASKED) if isinstance(job.payload, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        return _stored(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _held_by_request(job: LocalAiJob, now: datetime) -> bool:
    """The requester asked for this job not to start before a time still to come."""
    asked = _asked_until(job)
    return asked is not None and asked > now


# --- asking for work, and looking at it --------------------------------------------


async def create(
    db: AsyncSession,
    kind: str,
    *,
    subject: str,
    model_id: uuid.UUID | None = None,
    role: str | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 5,
    not_before: datetime | None = None,
    user_id: uuid.UUID | None = None,
    unique: bool = True,
) -> LocalAiJob:
    """Add a job and flush. The caller commits, then calls `local_ai_tasks.wake()`.

    `JobRefused(ALREADY)` when `unique` and a job of the same kind about the
    same subject is still active. A `not_before` (a naive one is this Mac's
    wall clock) makes it `waiting` until then: how "tonight" is asked for.
    That time is the requester's choice, and no change of settings (Resume,
    a new budget) starts the job before it.
    """
    if kind not in _KINDS:
        raise ValueError(f"No job of the kind {kind!r} is known (it was never registered).")
    if unique:
        taken = await db.scalar(
            select(
                exists().where(
                    LocalAiJob.kind == kind,
                    LocalAiJob.subject == subject,
                    LocalAiJob.state.in_(ACTIVE),
                )
            )
        )
        if taken:
            raise JobRefused(ALREADY)
    kept = dict(payload or {})
    if not_before is not None:
        kept[_ASKED] = _utc(not_before).isoformat()
    job = LocalAiJob(
        kind=kind,
        subject=subject,
        model_id=model_id,
        role=role,
        payload=kept,
        priority=priority,
        state="queued" if not_before is None else "waiting",
        not_before=None if not_before is None else _utc(not_before),
        requested_by=user_id,
        # Set here rather than by the database: the order of two jobs asked
        # for within one second (SQLite's clock) must still be their order.
        created_at=datetime.now(UTC),
    )
    db.add(job)
    await db.flush()
    return job


async def request_cancel(db: AsyncSession, job_id: uuid.UUID) -> LocalAiJob:
    """Cancel. A job not yet running ends at once; a running one is asked to stop
    (`cancel_requested`) and does so between pieces of its work. Flushed; the
    caller commits. `NoSuchJob`; `JobRefused(FINISHED_ALREADY)`.

    Each change is one statement guarded on the state it saw, and the state
    is read again when the guard did not hold: a job claimed a moment ago is
    running now, and one whose run just ended is queued or waiting again —
    either way the Cancel lands on the row as it is."""
    job = await db.get(LocalAiJob, job_id, populate_existing=True)
    if job is None:
        raise NoSuchJob(str(job_id))
    for _ in range(5):
        if job.state in ("queued", "waiting"):
            ended: Any = await db.execute(
                update(LocalAiJob)
                .where(LocalAiJob.id == job_id, LocalAiJob.state.in_(("queued", "waiting")))
                .values(state="cancelled", message=CANCELLED, finished_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
            await db.refresh(job)
            if ended.rowcount:
                await _abandon(db, job, "cancelled")
                await db.flush()
                return job
        elif job.state == "running":
            asked: Any = await db.execute(
                update(LocalAiJob)
                .where(LocalAiJob.id == job_id, LocalAiJob.state == "running")
                .values(cancel_requested=True)
                .execution_options(synchronize_session=False)
            )
            await db.refresh(job)
            if asked.rowcount:
                await db.flush()
                return job
        else:
            raise JobRefused(FINISHED_ALREADY)
    await db.flush()
    return job


async def release_waiting(db: AsyncSession, now: datetime | None = None) -> int:
    """Waiting jobs back to the queue, now (after Resume, or a change to a budget
    or the window). Flushed; the caller commits, then wakes the lane. A job
    that still may not run simply waits again, with today's sentence.

    A job its requester asked to wait until a time ("tonight") keeps waiting
    for that time: nothing about the settings changed the requester's mind.
    `now` is this Mac's clock (naive) or an aware moment."""
    moment = _utc(now) if now is not None else datetime.now(UTC)
    waiting = (
        await db.scalars(
            select(LocalAiJob)
            .where(LocalAiJob.state == "waiting")
            .execution_options(populate_existing=True)
        )
    ).all()
    free = [job.id for job in waiting if not _held_by_request(job, moment)]
    if not free:
        return 0
    released: Any = await db.execute(
        update(LocalAiJob)
        .where(LocalAiJob.id.in_(free), LocalAiJob.state == "waiting")
        .values(state="queued", not_before=None)
        .execution_options(synchronize_session=False)
    )
    await db.flush()
    return int(released.rowcount or 0)


async def reap(db: AsyncSession, job: LocalAiJob, now: datetime | None = None) -> bool:
    """End now a running job that `shown` reads as stopped (its worker died),
    the way the lane's tick would, so its kind tidies up (a download's model
    stops reading as downloading). True when it was ended here. Flushed; the
    caller commits. For a page that must act on the job before the next tick."""
    moment = _utc(now) if now is not None else datetime.now(UTC)
    if job.state != "running" or shown(job, moment)[0] != "failed":
        return False
    await _end_abandoned(db, job, "stopped", STOPPED, moment, only_if_beat=job.beat_at)
    await db.flush()
    return job.state == "failed"


async def in_use(db: AsyncSession, model_id: uuid.UUID, now: datetime | None = None) -> bool:
    """A job about this model is running (and its worker alive): its files may
    be being read this moment."""
    moment = _utc(now) if now is not None else datetime.now(UTC)
    running = (
        await db.scalars(
            select(LocalAiJob)
            .where(LocalAiJob.model_id == model_id, LocalAiJob.state == "running")
            .execution_options(populate_existing=True)
        )
    ).all()
    return any(shown(job, moment)[0] == "running" for job in running)


def shown(job: LocalAiJob, now: datetime | None = None) -> tuple[str, str]:
    """(state, message) as they should be read. A running job whose worker has
    not beaten for STALE_AFTER_S reads as failed with STOPPED, before the
    lane's next tick has said so in the table."""
    if job.state == "running":
        beat = _stored(job.beat_at or job.started_at)
        moment = _utc(now) if now is not None else datetime.now(UTC)
        if beat is None or (moment - beat).total_seconds() > STALE_AFTER_S:
            return "failed", STOPPED
    return job.state, job.message


async def recent(db: AsyncSession, limit: int = 15) -> list[LocalAiJob]:
    """Every active job, most urgent first; then the last `limit` finished, newest first."""
    active = await db.scalars(
        select(LocalAiJob)
        .where(LocalAiJob.state.in_(ACTIVE))
        .order_by(LocalAiJob.priority, LocalAiJob.created_at)
        .execution_options(populate_existing=True)
    )
    ended = await db.scalars(
        select(LocalAiJob)
        .where(LocalAiJob.state.in_(FINISHED))
        .order_by(LocalAiJob.finished_at.desc(), LocalAiJob.created_at.desc())
        .limit(limit)
        .execution_options(populate_existing=True)
    )
    return [*active, *ended]


async def depth(db: AsyncSession) -> int:
    """Jobs queued, waiting or running: the Processing page's figure for the lane."""
    found = await db.scalar(
        select(func.count()).select_from(LocalAiJob).where(LocalAiJob.state.in_(ACTIVE))
    )
    return int(found or 0)


# --- what a handler is handed ------------------------------------------------------


@dataclass
class _Latest:
    """Progress as the work last said it, handed from a thread to the loop."""

    phase: str = ""
    done: int = 0
    total: int = 0
    message: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    changed: bool = False

    def set(self, **values: Any) -> None:
        with self.lock:
            for name, value in values.items():
                if getattr(self, name) != value:
                    setattr(self, name, value)
                    self.changed = True

    def take(self) -> tuple[bool, str, int, int, str]:
        with self.lock:
            changed, self.changed = self.changed, False
            return changed, self.phase, self.done, self.total, self.message


@dataclass
class JobContext:
    job_id: uuid.UUID
    deps: Deps
    factory: async_sessionmaker[AsyncSession]
    priority: int = 5
    latest: _Latest = field(default_factory=_Latest)
    _written_at: float = float("-inf")

    async def progress(
        self, *, phase: str = "", done: int = 0, total: int = 0, message: str = ""
    ) -> None:
        """Say where the work has got to. Written at most every PROGRESS_EVERY_S
        (with the heartbeat); raises `Cancelled` once Cancel was pressed."""
        self.latest.set(phase=phase, done=done, total=total, message=message)
        if time.monotonic() - self._written_at < PROGRESS_EVERY_S:
            return
        if await self._flush():
            raise Cancelled()

    def say(self, message: str) -> None:
        """The line the row shows, from any thread (an installer's "Installing…")."""
        self.latest.set(message=message)

    async def run_blocking[T](self, work: Callable[[library.Report, Callable[[], bool]], T]) -> T:
        """Run `work(report, should_stop)` in a thread. `report(phase, done, total)`
        feeds the row's progress; `should_stop()` turns true once Cancel was
        pressed, and the work stops in its own way (`hub.Stopped`). A thread
        never touches a session: what it reports is written from here."""
        stop = threading.Event()

        def report(phase: str, done: int, total: int) -> None:
            self.latest.set(phase=phase, done=done, total=total)

        working = asyncio.ensure_future(asyncio.to_thread(work, report, stop.is_set))
        try:
            while not working.done():
                await asyncio.wait({working}, timeout=PROGRESS_EVERY_S)
                if await self._flush():
                    stop.set()
            return working.result()
        finally:
            if not working.done():
                # This coroutine was cancelled: tell the thread, and let it end
                # before anything else uses what it holds — for a while. A
                # thread blocked in a read from a share that hung never looks
                # at `should_stop`, and waiting for it would hold the lane.
                stop.set()
                await asyncio.wait({working}, timeout=LET_GO_S)
                if not working.done():
                    log.warning("local_ai.work_left_behind", job_id=str(self.job_id))
            await self._flush()

    async def should_yield(self) -> bool:
        """A strictly more urgent job is queued: a long batch raises `Later()`
        between items and is run again after it (13.2)."""
        async with self.factory() as db:
            return bool(
                await db.scalar(
                    select(
                        exists().where(
                            LocalAiJob.state == "queued", LocalAiJob.priority < self.priority
                        )
                    )
                )
            )

    async def record(self, answer: Answer) -> None:
        """Write what answered on this job's row: the five provenance columns
        (13.3), the seconds and the memory."""
        async with self.factory() as db:
            await db.execute(
                update(LocalAiJob)
                .where(LocalAiJob.id == self.job_id)
                .values(
                    **answer.provenance(),
                    seconds=answer.seconds,
                    memory_gb=answer.memory_gb,
                )
                .execution_options(synchronize_session=False)
            )
            await db.commit()

    async def _flush(self) -> bool:
        """Write the newest progress and the heartbeat; whether Cancel was pressed.
        A database that blinks costs a progress bar, never the job."""
        changed, phase, done, total, message = self.latest.take()
        values: dict[str, Any] = {"beat_at": _utc(self.deps.now())}
        if changed:
            values.update(phase=phase[:20], done=done, total=total, message=_cut(message))
        try:
            async with self.factory() as db:
                await db.execute(
                    update(LocalAiJob)
                    .where(LocalAiJob.id == self.job_id)
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                cancel = await db.scalar(
                    select(LocalAiJob.cancel_requested).where(LocalAiJob.id == self.job_id)
                )
                await db.commit()
        except Exception:
            log.warning("local_ai.progress_unwritten", job_id=str(self.job_id), exc_info=True)
            if changed:
                self.latest.changed = True  # try again next time
            return False
        self._written_at = time.monotonic()
        return bool(cancel)


# --- the runner ----------------------------------------------------------------------


async def run_next(factory: async_sessionmaker[AsyncSession], deps: Deps) -> bool:
    """Run at most one job; whether more are queued.

    In order: on an install that cannot run models, close every active job
    with the reason; reap running jobs whose worker died; release waiting
    jobs whose time has come; unload a model that may no longer be loaded
    (D3); claim the most urgent queued job in one conditional statement (so
    two runners, or a wake-up delivered twice, never run one job twice);
    run it with the heartbeat beside it; write how it ended.
    """
    now = _utc(deps.now())
    if not availability(deps.settings).ok:
        await _close_all(factory, now)
        return False
    await _tidy(factory, now)
    try:
        async with factory() as db:
            await sweep(db, deps.server, now=deps.now(), settings=deps.settings)
            await db.commit()
    except Exception:
        # The sweep is housekeeping; the work in the queue matters more.
        log.warning("local_ai.sweep_failed", exc_info=True)

    claimed = await _claim(factory, now)
    if claimed is None:
        return False
    if isinstance(claimed, _Taken):
        return True
    job, kind = claimed
    await _run(factory, deps, job, kind)
    return await _queued_left(factory)


class _Taken:
    """Another runner claimed the job first."""


_TAKEN = _Taken()


async def _close_all(factory: async_sessionmaker[AsyncSession], now: datetime) -> None:
    """The containers: nothing here can run a model, and nothing was meant to be
    queued. Whatever is active ends at once with the sentence (D26)."""
    async with factory() as db:
        rows = (await db.scalars(select(LocalAiJob).where(LocalAiJob.state.in_(ACTIVE)))).all()
        for job in rows:
            await _end_abandoned(db, job, "unavailable", UNAVAILABLE, now)
        await db.commit()


async def _tidy(factory: async_sessionmaker[AsyncSession], now: datetime) -> None:
    """Reap the dead, and release the waiting whose time has come."""
    cutoff = now - timedelta(seconds=STALE_AFTER_S)
    async with factory() as db:
        running = (await db.scalars(select(LocalAiJob).where(LocalAiJob.state == "running"))).all()
        for job in running:
            beat = _stored(job.beat_at or job.started_at)
            if beat is None or beat < cutoff:
                await _end_abandoned(db, job, "stopped", STOPPED, now, only_if_beat=job.beat_at)
        await db.execute(
            update(LocalAiJob)
            .where(
                LocalAiJob.state == "waiting",
                LocalAiJob.not_before.is_not(None),
                LocalAiJob.not_before <= now,
            )
            .values(state="queued")
            .execution_options(synchronize_session=False)
        )
        await db.commit()


async def _end_abandoned(
    db: AsyncSession,
    job: LocalAiJob,
    why: Why,
    message: str,
    now: datetime,
    *,
    only_if_beat: datetime | None = None,
) -> None:
    """End a job its handler will never finish, and let its kind tidy up. Guarded
    on the state (and, for a reaped job, the beat that was seen), so a job
    that came back to life a moment ago is left alone."""
    guard = [LocalAiJob.id == job.id, LocalAiJob.state == job.state]
    if why == "stopped":
        guard.append(
            LocalAiJob.beat_at.is_(None)
            if only_if_beat is None
            else LocalAiJob.beat_at == only_if_beat
        )
    ended: Any = await db.execute(
        update(LocalAiJob)
        .where(*guard)
        .values(state="failed", message=_cut(message), finished_at=now)
        .execution_options(synchronize_session=False)
    )
    if ended.rowcount:
        await db.refresh(job)
        await _abandon(db, job, why)


async def _abandon(db: AsyncSession, job: LocalAiJob, why: Why) -> None:
    kind = _KINDS.get(job.kind)
    if kind is None or kind.abandoned is None:
        return
    try:
        await kind.abandoned(db, job, why)
    except Exception:
        log.warning("local_ai.abandon_failed", job_id=str(job.id), kind=job.kind, exc_info=True)


async def _claim(
    factory: async_sessionmaker[AsyncSession], now: datetime
) -> tuple[LocalAiJob, Kind] | _Taken | None:
    async with factory() as db:
        config = await load_config(db)
        while True:
            job = await db.scalar(
                select(LocalAiJob)
                .where(LocalAiJob.state == "queued")
                .order_by(LocalAiJob.priority, LocalAiJob.created_at)
                .limit(1)
                .execution_options(populate_existing=True)
            )
            if job is None:
                return None
            kind = _KINDS.get(job.kind)
            if kind is None:
                # Written by a phase this worker does not have (an older
                # worker, a phase taken out): said, not left queued for ever.
                await _set(
                    db, job, ("queued",), state="failed", message=UNKNOWN_KIND, finished_at=now
                )
                await db.commit()
                continue
            if job.cancel_requested:
                # Cancel was pressed while it last ran, and that run ended
                # without seeing it: the Cancel stands, it is not run again.
                ended: Any = await db.execute(
                    update(LocalAiJob)
                    .where(LocalAiJob.id == job.id, LocalAiJob.state == "queued")
                    .values(state="cancelled", message=CANCELLED, finished_at=now)
                    .execution_options(synchronize_session=False)
                )
                if ended.rowcount:
                    await db.refresh(job)
                    await _abandon(db, job, "cancelled")
                await db.commit()
                continue
            if _held_by_request(job, now):
                # Its requester asked for later ("tonight"): it waits for that.
                await _set(db, job, ("queued",), state="waiting", not_before=_asked_until(job))
                await db.commit()
                continue
            if config.paused and kind.needs_model:
                # Never claimed only to be refused: a model job started while
                # paused would begin a load the pause exists to prevent (D21).
                await _set(
                    db,
                    job,
                    ("queued",),
                    state="waiting",
                    message=schedule.PAUSED,
                    not_before=now + PAUSED_RETRY_AFTER,
                )
                await db.commit()
                continue
            claimed: Any = await db.execute(
                update(LocalAiJob)
                .where(LocalAiJob.id == job.id, LocalAiJob.state == "queued")
                # `cancel_requested` is left as it is: it is only ever set on a
                # running row, so clearing it here could only lose a Cancel.
                .values(
                    state="running",
                    started_at=job.started_at or now,
                    beat_at=now,
                    not_before=None,
                )
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            if not claimed.rowcount:
                return _TAKEN
            await db.refresh(job)
            return job, kind


async def _set(db: AsyncSession, job: LocalAiJob, states: Sequence[str], **values: Any) -> None:
    await db.execute(
        update(LocalAiJob)
        .where(LocalAiJob.id == job.id, LocalAiJob.state.in_(states))
        .values(**values)
        .execution_options(synchronize_session=False)
    )


async def _run(
    factory: async_sessionmaker[AsyncSession], deps: Deps, job: LocalAiJob, kind: Kind
) -> None:
    ctx = JobContext(job.id, deps, factory, priority=job.priority)
    handling = asyncio.ensure_future(kind.handler(ctx, job))
    watching = asyncio.ensure_future(_watch(factory, job.id, deps))
    end: dict[str, Any]
    # The kind's tidy-up, when the job ends without the handler having done it.
    abandoned: Why | None = None
    try:
        result = await _outcome(handling, watching)
        end = {"state": "done", "message": "", "result": result}
    except _GaveUp:
        log.warning("local_ai.cancel_unanswered", job_id=str(job.id), kind=job.kind)
        end = {"state": "cancelled", "message": LEFT_BEHIND}
        abandoned = "cancelled"
    except (Cancelled, Stopped) as err:
        end = {"state": "cancelled", "message": str(err) or CANCELLED}
    except Later as err:
        end = {
            "state": "queued" if err.not_before is None else "waiting",
            "message": err.message,
            "not_before": None if err.not_before is None else _utc(err.not_before),
        }
    except NotNow as err:
        if err.retry:
            wait = PAUSED_RETRY_AFTER if err.reason == "paused" else RETRY_AFTER
            at = _utc(err.not_before) if err.not_before is not None else _utc(deps.now()) + wait
            end = {"state": "waiting", "message": str(err), "not_before": at}
        else:
            end = {"state": "failed", "message": str(err)}
        end["detail"] = err.detail or None
    except (Failed, ModelFailed, RuntimeProblem) as err:
        end = {"state": "failed", "message": str(err), "detail": err.detail or None}
    except (library.LibraryProblem, HubError) as err:
        end = {"state": "failed", "message": str(err)}
    except Exception as err:
        log.error("local_ai.job_crashed", job_id=str(job.id), kind=job.kind, exc_info=True)
        end = {"state": "failed", "message": f"It failed unexpectedly ({type(err).__name__})."}
        # It never reached its own tidy-up (a model set downloading, a role
        # left waiting for a copy): the kind does it, as for a job that died.
        abandoned = "failed"
    finally:
        watching.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watching
        if not handling.done():
            # This runner itself was stopped (the worker is shutting down).
            handling.cancel()
    # Whatever the handler last said about its progress is kept with the end.
    _changed, phase, done, total, _message = ctx.latest.take()
    if phase or done or total:
        end.update(phase=phase[:20], done=done, total=total)
    end["message"] = _cut(end.get("message") or "")
    moment = _utc(deps.now())
    if end["state"] in FINISHED:
        end["finished_at"] = moment
    end["beat_at"] = moment
    await _write_end(factory, job.id, end, abandoned)


class _GaveUp(Exception):
    """Cancel went unanswered for CANCEL_GRACE_S: the runner ended the job itself."""


async def _outcome(
    handling: "asyncio.Future[dict[str, Any] | None]", watching: "asyncio.Future[None]"
) -> dict[str, Any] | None:
    """What the handler returned or raised. `watching` ends only when a Cancel
    has gone unanswered too long: the handler is then cancelled, given a moment
    to end in its own way, and otherwise left behind (`_GaveUp`)."""
    either: set[asyncio.Future[Any]] = {handling, watching}
    await asyncio.wait(either, return_when=asyncio.FIRST_COMPLETED)
    if not handling.done():
        handling.cancel()
        # run_blocking waits LET_GO_S for its thread, then writes once more.
        await asyncio.wait({handling}, timeout=LET_GO_S * 2)
        if not handling.done() or handling.cancelled():
            raise _GaveUp()
    return handling.result()


async def _watch(factory: async_sessionmaker[AsyncSession], job_id: uuid.UUID, deps: Deps) -> None:
    """The runner's own heartbeat for the row it is running (13.2), and its watch
    on Cancel. Returns only when Cancel was pressed CANCEL_GRACE_S ago and the
    work has still not stopped; it then stops beating, so even a runner that
    cannot write the end leaves a row the next tick reads as stopped."""
    asked: float | None = None
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        try:
            async with factory() as db:
                await db.execute(
                    update(LocalAiJob)
                    .where(LocalAiJob.id == job_id, LocalAiJob.state == "running")
                    .values(beat_at=_utc(deps.now()))
                    .execution_options(synchronize_session=False)
                )
                cancel = await db.scalar(
                    select(LocalAiJob.cancel_requested).where(LocalAiJob.id == job_id)
                )
                await db.commit()
        except Exception:
            log.warning("local_ai.heartbeat_unwritten", job_id=str(job_id), exc_info=True)
            continue
        if cancel:
            asked = time.monotonic() if asked is None else asked
            if time.monotonic() - asked >= CANCEL_GRACE_S:
                return


async def _write_end(
    factory: async_sessionmaker[AsyncSession],
    job_id: uuid.UUID,
    values: dict[str, Any],
    abandoned: Why | None = None,
) -> None:
    """How the job ended, as one targeted statement, tried more than once."""
    for attempt in range(END_ATTEMPTS):
        try:
            async with factory() as db:
                await _end(db, job_id, values, abandoned)
                await db.commit()
                return
        except Exception:
            log.warning("local_ai.end_unwritten", job_id=str(job_id), exc_info=True)
            if attempt + 1 < END_ATTEMPTS:
                await asyncio.sleep(PROGRESS_EVERY_S)


async def _end(
    db: AsyncSession, job_id: uuid.UUID, values: dict[str, Any], abandoned: Why | None
) -> None:
    """Write the end. A job going back to the queue (`Later`, a wait) goes only
    if Cancel was not pressed while it ran: that Cancel ends it here instead,
    since the next claim would otherwise run it as if nobody had pressed it."""
    again = values["state"] not in FINISHED
    query = update(LocalAiJob).where(LocalAiJob.id == job_id)
    if again:
        query = query.where(LocalAiJob.cancel_requested.is_(False))
    written: Any = await db.execute(
        query.values(**values).execution_options(synchronize_session=False)
    )
    if again and not written.rowcount:
        ended: dict[str, Any] = {
            **{name: values[name] for name in ("phase", "done", "total") if name in values},
            "state": "cancelled",
            "message": CANCELLED,
            "not_before": None,
            "beat_at": values["beat_at"],
            "finished_at": values["beat_at"],
        }
        written = await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == job_id, LocalAiJob.state == "running")
            .values(**ended)
            .execution_options(synchronize_session=False)
        )
        abandoned = "cancelled"
    if abandoned is not None and written.rowcount:
        job = await db.get(LocalAiJob, job_id, populate_existing=True)
        if job is not None:
            await _abandon(db, job, abandoned)


async def _queued_left(factory: async_sessionmaker[AsyncSession]) -> bool:
    async with factory() as db:
        return bool(await db.scalar(select(exists().where(LocalAiJob.state == "queued"))))
