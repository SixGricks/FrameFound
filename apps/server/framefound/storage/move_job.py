"""The move the Storage page starts: its record, and the job that does it.

A move (storage/moves.py) copies for hours. The page that asked for it will
be closed and opened again long before it ends, so where it has got to lives
in the database — one `AppSetting` row, key `storage_move` — and the page
reads that.

One move at a time. Among the page's own requests the row is the
turn-taking: a request writes it with a fresh id and queues the job with
that id; the job runs only while the row still carries its id. So a request
that replaced a dead move, and a queue message delivered a second time, can
never give two moves at once. (A move started with `framefound storage
move` is in no row. Against that, and anything else, `moves.move` takes a
turn of its own — a file in the data folder — and a job that finds it taken
ends as failed with that reason.)

The job runs in the worker that also writes proxies and renders. It never
stops or restarts a service to get those writers out of the way: the service
it would be stopping is the one it is running in, and the queue would hand
the killed job straight back to be run again. `moves.move` pauses the kind's
writers through the stores file instead.
"""

import asyncio
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from framefound.config import Settings, get_settings
from framefound.db.models import AppSetting
from framefound.storage import moves

log = structlog.get_logger()

MOVE_KEY = "storage_move"
# The page's request to stop, in a row of its own. The job rewrites the
# move's row every couple of seconds from a copy it read a moment earlier; a
# flag kept in that row would be wiped out whenever the two writes crossed,
# and Cancel would do nothing one time in a few.
CANCEL_KEY = "storage_move_cancel"

WAITING = "queued"
# The phases `moves.Progress` reports, in order, before "done".
RUNNING = ("checking", "copying", "finishing", "switching")
FINISHED = ("done", "failed", "cancelled")

# How often the job writes where it has got to, and looks for a request to
# stop. The copy itself is told about once a second; the page asks every two.
PROGRESS_EVERY_S = 2.0
# With nothing new to say the row is still touched this often, so that a
# move whose worker has died can be told from one copying a single huge file.
HEARTBEAT_S = 15.0
# A running move that has not touched its row for this long has no worker.
# Without this rule a worker killed mid-copy would leave "copying" on the
# page, and every later move refused, until somebody edited the database.
STALE_AFTER_S = 120.0
# How long the switch of a model library waits to learn which models have a
# role (one small query; the database is a local one).
ROLES_WAIT_S = 60.0

STOPPED = (
    "The move stopped without finishing: the worker running it was restarted or "
    "lost. What was copied is kept; start the move again to carry on."
)
CANCELLED = "The move was cancelled. Nothing was changed; what was copied so far is kept."
NEVER_STARTED = "The move was cancelled before it started."
UNEXPECTED = (
    "The move stopped because of an error in FrameFound (it is in the worker's "
    "log). What was copied is kept; start the move again to carry on."
)


@dataclass
class MoveState:
    """The row. Times are seconds since the epoch."""

    id: str
    kind: str
    target: str
    # "" when the kind had no folder to move from (no backup copy kept yet).
    source: str = ""
    mode: str = "copy"
    # queued | checking | copying | finishing | switching | done | failed | cancelled
    phase: str = WAITING
    # Of the pass in hand: the long copy, then each catch-up pass.
    files_total: int = 0
    files_done: int = 0
    bytes_total: int = 0
    bytes_done: int = 0
    # How many files this run had to copy (the rest were already there).
    copied: int | None = None
    error: str | None = None
    requested_by: str = ""
    requested_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    # When the job last touched the row.
    beat_at: float = 0.0
    # The page has asked for this move to stop (`ask_to_stop`). Read with
    # the row, never written with it.
    cancel: bool = False


def new(kind: str, target: Path, *, source: Path | None, mode: str, requested_by: str) -> MoveState:
    now = time.time()
    return MoveState(
        id=uuid.uuid4().hex,
        kind=kind,
        target=str(target),
        source="" if source is None else str(source),
        mode=mode,
        requested_by=requested_by,
        requested_at=now,
        beat_at=now,
    )


async def load(db: AsyncSession) -> MoveState | None:
    """The row as it is in the database now, or None before the first move."""
    row = await db.get(AppSetting, MOVE_KEY, populate_existing=True)
    if row is None or not row.value.get("id"):
        return None
    known = {field.name for field in fields(MoveState)} - {"cancel"}
    state = MoveState(**{key: value for key, value in row.value.items() if key in known})
    asked = await db.get(AppSetting, CANCEL_KEY, populate_existing=True)
    state.cancel = asked is not None and asked.value.get("id") == state.id
    return state


async def save(db: AsyncSession, state: MoveState) -> None:
    value = asdict(state)
    del value["cancel"]
    await _put(db, MOVE_KEY, value)


async def ask_to_stop(db: AsyncSession, state: MoveState) -> None:
    """Record that the move in `state` is to stop. The job sees it at its
    next look, within a few seconds."""
    state.cancel = True
    await _put(db, CANCEL_KEY, {"id": state.id})


async def _put(db: AsyncSession, key: str, value: dict[str, Any]) -> None:
    row = await db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await db.commit()


def settled(state: MoveState, now: float | None = None) -> MoveState:
    """The row as it should be read: a running move nobody has touched for
    STALE_AFTER_S is one that stopped, whatever its last phase says.

    A queued move never goes stale by itself. It may wait an hour behind a
    long transcode, and there is nothing to tell that from a worker that is
    down — it is cancelled by hand, or runs when its turn comes."""
    moment = time.time() if now is None else now
    if state.phase in RUNNING and moment - state.beat_at > STALE_AFTER_S:
        return replace(state, phase="failed", error=STOPPED, finished_at=state.beat_at)
    return state


def is_active(state: MoveState | None, now: float | None = None) -> bool:
    """True while a move is waiting its turn or running: no other may start."""
    return state is not None and settled(state, now).phase not in FINISHED


class _Stop(Exception):
    """Raised inside the copy, from its progress callback, to end it."""


class _Latest:
    """The newest progress, handed from the copying thread to the loop. One
    attribute assigned whole, so the reader never sees half of two reports."""

    def __init__(self) -> None:
        self.progress: tuple[str, int, int, int, int] | None = None

    def apply(self, state: MoveState) -> bool:
        """Copy the newest progress into `state`; True if it changed."""
        if self.progress is None:
            return False
        before = (
            state.phase,
            state.files_total,
            state.files_done,
            state.bytes_total,
            state.bytes_done,
        )
        (
            state.phase,
            state.files_total,
            state.files_done,
            state.bytes_total,
            state.bytes_done,
        ) = self.progress
        return before != self.progress


async def run(
    factory: async_sessionmaker[AsyncSession],
    move_id: str,
    settings: Settings | None = None,
    *,
    tick_s: float = PROGRESS_EVERY_S,
    **options: Any,
) -> None:
    """Carry out the move the row describes, if it is still this one's turn.

    `options` go to `moves.move` (tests shorten its waits). Never raises for
    anything about the move itself: how it ended is written in the row,
    which is where the page looks."""
    settings = settings or get_settings()
    async with factory() as db:
        state = await load(db)
        if state is None or state.id != move_id:
            log.info("storage.move_superseded", move_id=move_id)
            return
        if state.phase != WAITING:
            # Cancelled before its turn came — or this message has been
            # delivered before. Then either that run is still going (a copy
            # can outlast the queue's redelivery timeout) or it died, which
            # `settled` already shows as stopped. A second run is wrong both
            # ways: the operator asked once, and a move that starts by
            # itself half a day later is a surprise.
            log.info("storage.move_not_run", move_id=move_id, phase=state.phase)
            return
        state.phase = "checking"
        state.started_at = state.beat_at = time.time()
        await save(db, state)
        kind, target, mode = state.kind, Path(state.target), state.mode

    latest = _Latest()
    stop = threading.Event()

    def report(progress: moves.Progress) -> None:
        # In the copying thread, about once a second. Stopping is asked for
        # here because this is the only place the copy looks up from its
        # work — but never once it is done: the switch has happened by then,
        # and saying "cancelled" would be untrue.
        if stop.is_set() and progress.phase != "done":
            raise _Stop
        latest.progress = (
            progress.phase,
            progress.files_total,
            progress.files_done,
            progress.bytes_total,
            progress.bytes_done,
        )

    loop = asyncio.get_running_loop()

    def keep_roles() -> None:
        # In the copying thread, at the switch: the roles as they are then,
        # read through this job's own event loop (it is waiting on the copy).
        asked = asyncio.run_coroutine_threadsafe(_role_models(factory), loop)
        try:
            models = asked.result(timeout=ROLES_WAIT_S)
        except Exception as err:
            raise moves.MoveError(
                f"Which models have a role could not be read ({err}). Nothing was changed; "
                "start the move again."
            ) from err
        keep_roles_here(models, settings)

    def work(catalogued: int) -> moves.MoveResult:
        if kind == "models":
            options.setdefault("before_switch", keep_roles)
        return moves.move(
            kind,
            target,
            settings=settings,
            mode=mode,
            catalogued=catalogued,
            report=report,
            **options,
        )

    result: moves.MoveResult | None = None
    phase, error = "failed", UNEXPECTED
    try:
        async with factory() as db:
            catalogued = await moves.catalogued_under(db, target, settings)
        copying = asyncio.ensure_future(asyncio.to_thread(work, catalogued))
        last_write = time.monotonic()
        while not copying.done():
            await asyncio.wait({copying}, timeout=tick_s)
            if not copying.done():
                last_write = await _tick(factory, move_id, latest, stop, last_write)
        result = copying.result()
        phase, error = "done", ""
    except _Stop:
        phase, error = "cancelled", CANCELLED
    except moves.MoveError as err:
        error = str(err)
    except Exception:
        log.error("storage.move_crashed", move_id=move_id, kind=kind, exc_info=True)

    def finish(state: MoveState) -> None:
        latest.apply(state)
        state.phase = phase
        state.error = error or None
        state.finished_at = state.beat_at = time.time()
        if result is not None:
            # The whole of what now lives in the new folder, so the bar the
            # page drew for the last small catch-up pass ends on the total.
            state.files_total = state.files_done = result.files
            state.bytes_total = state.bytes_done = result.bytes
            state.copied = result.copied

    await _write_end(factory, move_id, finish)
    log.info("storage.move_finished", move_id=move_id, kind=kind, phase=phase, error=error or None)


async def _tick(
    factory: async_sessionmaker[AsyncSession],
    move_id: str,
    latest: _Latest,
    stop: threading.Event,
    last_write: float,
) -> float:
    """Write the newest progress, and look for a reason to stop. Returns
    when the row was last written."""
    try:
        async with factory() as db:
            state = await load(db)
            if state is None or state.id != move_id:
                # Replaced by a newer request (this one was taken for dead):
                # stop, and leave the row to the move that owns it now.
                stop.set()
                return last_write
            if state.cancel:
                stop.set()
            if state.phase in FINISHED:
                # Cancelled at the very moment this job took its turn, and
                # the page's write landed second. The page has been told it
                # is over; make that true rather than write over it.
                stop.set()
                return last_write
            changed = latest.apply(state)
            if changed or time.monotonic() - last_write >= HEARTBEAT_S:
                state.beat_at = time.time()
                await save(db, state)
                return time.monotonic()
    except Exception:
        # The database blinked. The copy matters more than its progress bar.
        log.warning("storage.move_progress_unwritten", move_id=move_id, exc_info=True)
    return last_write


async def _write_end(
    factory: async_sessionmaker[AsyncSession],
    move_id: str,
    finish: Callable[[MoveState], None],
    attempts: int = 5,
) -> None:
    """Record how the move ended. Tried more than once: a move that switched
    and then could not say so would be shown as stopped."""
    for attempt in range(attempts):
        try:
            async with factory() as db:
                state = await load(db)
                if state is None or state.id != move_id:
                    return  # the row belongs to a newer move
                finish(state)
                await save(db, state)
                return
        except Exception:
            log.warning("storage.move_end_unwritten", move_id=move_id, exc_info=True)
            if attempt + 1 < attempts:
                await asyncio.sleep(PROGRESS_EVERY_S)


async def _role_models(factory: async_sessionmaker[AsyncSession]) -> list[Any]:
    from framefound.local_models import registry

    async with factory() as db:
        return list(await registry.role_models(db))


def keep_roles_here(models: Iterable[Any], settings: Settings) -> None:
    """For a move of the model library, just before its switch: every model
    that has a role (or is waiting for one) gets its copy on this machine
    while the library is still here, so moving the library to the NAS never
    stops the Looker or the Editor. MoveError, with nothing changed, when a
    copy cannot be made."""
    from framefound.local_models import library

    try:
        kept = library.keep_on_this_machine(models, settings)
    except library.LibraryProblem as err:
        raise moves.MoveError(str(err)) from None
    if kept:
        log.info("storage.role_models_kept_here", models=kept)
