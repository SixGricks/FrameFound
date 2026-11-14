"""Looking through a folder with the local model: the API (design L3 sections 6.5-6.9).

A review is somebody asking for one folder (or a whole library) to be looked
at by the model on this Mac, now or overnight. The rules — who may start
one, every refusal and its sentence, how a running review reads — live in
`looking/reviews.py`, which the chat panel's tool calls too; this module
only answers with them.

- Reading is for anyone signed in: the dialog's counts, the estimate,
  progress and the two settings are shown to everybody (D24). Starting,
  cancelling and the settings are an administrator's, refused to anyone
  else (403), audited, and committed once with their audit row.
- A review starts only when an administrator asks for one here (or in the
  chat panel, which asks first). No read starts anything; a read of the
  progress only writes back what a job that already ended means.
- Where models cannot run (the containers) every read answers with that
  sentence and nothing else, and every write is refused with it (409).
- `can_cancel` is an administrator's: a review is cancellable only when it
  is active AND the reader may cancel it, so the page never offers a
  button that would answer 403.
- No answer carries a server path. A folder is a path inside its library,
  as Browse shows it; a drive that is away is named by what it holds.

The outside world (the clock, which runtime kinds exist, the model
server) comes in through L1's `get_deps`, and waking the lane through
`get_wake`, so a test swaps each with one `dependency_overrides` entry.
The clock is the Mac's wall clock, naive, as L1's `Deps.now` gives it:
the overnight window is the wall clock's.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

# Imported for what importing it does: the chat panel gains
# `start_folder_review` where models can run, so the tool is there whenever
# the API is (L3 3.11).
import framefound.looking.chat_tool  # noqa: F401 - registers the chat panel's tool
from framefound.api.v1.local_models import QUEUE_DOWN, Changing, DepsDep
from framefound.auth import service as auth_service
from framefound.auth.deps import CurrentUser, DbDep, client_ip, require_admin
from framefound.config import Settings
from framefound.db.models import AssetTag, Library, LookRequest, User
from framefound.local_models import availability, comparisons, registry, schedule
from framefound.local_models.demo import DEMO_NOTE
from framefound.local_models.deps import Deps
from framefound.local_models.settings_store import load_config as load_local_ai_config
from framefound.looking import ORIGIN_LOCAL, reviews
from framefound.looking.settings_store import STILLS_CHOICES, LookingConfig
from framefound.looking.settings_store import load_config as load_looking_config
from framefound.looking.settings_store import save_config as save_looking_config

# Registers the look kinds in the API's process, so a review can be queued
# (and its jobs named) from the first request. The worker that RUNS them
# registers them through `processing/local_ai_tasks.py`'s own import.
from framefound.processing import local_ai_tasks

router = APIRouter(prefix="/looks", tags=["looks"])

BAD_STILLS = "Choose 1 or 3 pictures a shot."

Wake = Callable[[], Awaitable[bool]]


def get_wake(deps: DepsDep) -> Wake:
    """Wake the local-AI lane, off the event loop (reaching the broker blocks);
    False when the queue is down. Tests override this one dependency."""

    def wake() -> Awaitable[bool]:
        return asyncio.to_thread(local_ai_tasks.wake, deps.settings)

    return wake


WakeDep = Annotated[Wake, Depends(get_wake)]


def _demo_beat(settings: Settings) -> None:
    """The demo has no beat: its lane is a thread in this process, gone when
    the API restarts, and a review left queued would read "Waiting its turn."
    for ever. Every read of a page that shows reviews starts it again (as the
    AI models page does); `keep_going` does so at most once a tick however
    often the page asks."""
    if availability(settings).mode == "demo":
        # Imported only where demo mode is on.
        from framefound.local_models import demo

        demo.keep_going(settings)


def _kinds(deps: Deps) -> tuple[str, ...]:
    """The runtime kinds this install has, by name: the demo's in demo mode."""
    return tuple(deps.kinds)


async def _missing(deps: Deps) -> dict[str, str]:
    """The programs not installed here, with the sentence for each, as Compare
    asks it: a model on one is refused before a review starts, not failed
    once it has. Asks the disk, so off the event loop. Where models cannot
    run there is nothing to ask: every answer is that sentence already."""
    if not availability(deps.settings).ok:
        return {}
    return await asyncio.to_thread(comparisons.missing_programs, deps.kinds, deps.settings)


def _refused(err: reviews.ReviewRefused) -> HTTPException:
    return HTTPException(err.status, str(err))


# --- what the pages are sent ------------------------------------------------------------


class _Out(BaseModel):
    # Several fields are called model_…, which is what they are.
    model_config = ConfigDict(protected_namespaces=())


class Preparing(_Out):
    # "" (nothing to make ready) | running | done
    state: str
    total: int
    ready: int
    gave_up: int


class Review(_Out):
    """One review as the dialog and Processing read it (6.7)."""

    id: str
    library_id: str
    library_name: str
    folder: str
    when: str
    redo: bool
    prepare: bool
    model_name: str
    # tonight | waiting | paused | looking | done | failed | cancelled
    state: str
    message: str
    not_before: str | None
    done: int
    total: int
    failed: int
    tags_suggested: int
    new_tags: int
    held: int
    stills_only: int
    preparing: Preparing
    seconds_left: int | None
    requested_by: str | None
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    can_cancel: bool
    demo: bool


class IngestLine(_Out):
    """A library that looks at new footage as it arrives."""

    library_id: str
    library_name: str
    waiting: int
    # idle | waiting | paused | looking | failed
    state: str
    message: str
    done: int
    total: int
    # Set only while the library is being left alone after a job that failed
    # (state "failed", no job at work): when looking at its new footage is
    # tried again, UTC with a Z. The message says the same in words.
    retry_at: str | None = None


class Listing(_Out):
    available: bool
    reviews: list[Review]
    ingest: list[IngestLine]


class Todo(_Out):
    photos: int
    clips: int
    shots: int
    stills_only: int


class Already(_Out):
    items: int
    by_another_model: int


class Kinds(_Out):
    """Items by kind: photographs, clips (each counted once) and the shots in
    those clips. Without it the dialog can only say a bare sum, which read as
    "12 items" for a folder of 8 files."""

    photos: int
    clips: int
    shots: int


class First(_Out):
    need_copy: int
    need_copy_footage_s: float
    copy_away: int
    need_shots: int
    still_processing: int
    problem: str | None


class Skipped(_Out):
    missing: int
    audio: int


class PreviewModel(_Out):
    id: str
    name: str
    fits: str
    is_looker: bool
    problem: str | None
    # `problem` is that its program is not installed (Software that runs
    # models on the AI models page puts it right).
    program_missing: bool = False
    why_not_now: str | None
    tonight_problem: str | None


class Estimate(_Out):
    seconds: int | None
    # measured | test | none
    basis: str
    per_photo_s: float | None
    per_shot_s: float | None
    sample: int
    words: str


class Tonight(_Out):
    # The Mac's wall clock, no zone: when the overnight window next opens.
    starts_at: str | None
    open_now: bool
    problem: str | None


class Preview(_Out):
    """What the review dialog shows for one folder (6.6)."""

    available: bool
    reason: str | None
    library_id: str
    library_name: str
    folder: str
    folders_inside: int
    todo: Todo
    already: Already
    # The same counts as already.items / already.by_another_model, by kind.
    # Left off, the dialog falls back to the bare sum, said as such.
    already_kinds: Kinds | None = None
    by_another_kinds: Kinds | None = None
    first: First
    skipped: Skipped
    failed_before: int
    no_looker: str | None = None
    model: PreviewModel | None
    estimate: Estimate
    tonight: Tonight
    paused: bool = False
    active: Review | None


class LookerOut(_Out):
    model_id: str | None
    name: str | None
    problem: str | None


class ModelChoice(_Out):
    id: str
    name: str
    repo_id: str
    memory_gb: float
    # day | night
    fits: str
    is_looker: bool
    why_not_now: str | None
    # Why it cannot be used at all as things stand (its program is not
    # installed here), else None; `program_missing` says it is the program.
    problem: str | None = None
    program_missing: bool = False


class NowOut(_Out):
    overnight: bool
    night_start: str
    night_end: str
    paused: bool


class LookingSettings(_Out):
    new_tags: bool
    stills_per_shot: int


class Status(_Out):
    """What the pages need to know before offering anything (6.5)."""

    available: bool
    # ready | demo | unavailable
    mode: str
    reason: str | None
    can_manage: bool
    looker: LookerOut | None
    models: list[ModelChoice]
    now: NowOut
    settings: LookingSettings
    pending_suggestions: int


class ReviewBody(BaseModel):
    library_id: uuid.UUID
    # A path inside the library as Browse shows it; "" is the whole library.
    folder: str = ""
    # "now" | "tonight"; anything else is refused with a sentence.
    when: str = "now"
    # A model other than the Looker; null uses the Looker.
    model_id: uuid.UUID | None = None
    # Also look again at what another model (or question) already looked at.
    redo: bool = False
    # Make clips ready (a playable copy, their shots) when they are not.
    prepare: bool = True

    model_config = ConfigDict(protected_namespaces=())


class SettingsBody(BaseModel):
    """Either field may be left out (or null): it is left as it is."""

    new_tags: StrictBool | None = None
    stills_per_shot: StrictInt | None = None


def _review(found: dict[str, Any], user: User) -> Review:
    """A review for this reader: cancellable only by an administrator."""
    review = Review.model_validate(found)
    review.can_cancel = review.can_cancel and user.role == "admin"
    return review


# --- reads ------------------------------------------------------------------------------


async def _looker(db: AsyncSession, known: tuple[str, ...]) -> LookerOut:
    """The Looker and why it may not look, if it may not. A role whose model
    row is gone reads as no Looker, as `reviews.start` treats it."""
    looker_id = (await registry.roles(db))["looker"].model_id
    model = None if looker_id is None else await registry.get_model(db, looker_id)
    if model is None:
        return LookerOut(model_id=None, name=None, problem=reviews.NO_LOOKER)
    config = await load_local_ai_config(db)
    problem = registry.role_problem(model, "looker", config.lists(), config.budgets(), known)
    return LookerOut(model_id=str(model.id), name=reviews.name_of(model), problem=problem)


async def _pending_local(db: AsyncSession) -> int:
    found = await db.scalar(
        select(func.count())
        .select_from(AssetTag)
        .where(AssetTag.source == "suggested", AssetTag.origin == ORIGIN_LOCAL)
    )
    return int(found or 0)


@router.get("/status", response_model=Status)
async def look_status(user: CurrentUser, db: DbDep, deps: DepsDep) -> Status:
    """Whether a review can be offered, with which models, and the settings.
    Never an error and never slow: no share and no model server is asked."""
    _demo_beat(deps.settings)
    found = availability(deps.settings)
    config = await load_local_ai_config(db)
    budgets = config.budgets()
    now = deps.now()
    looking = await load_looking_config(db)
    looker: LookerOut | None = None
    models: list[ModelChoice] = []
    reason = found.reason
    if found.ok:
        reason = DEMO_NOTE if found.mode == "demo" else None
        known = _kinds(deps)
        looker = await _looker(db, known)
        missing = await _missing(deps)
        models = [
            ModelChoice(
                id=str(c.model.id),
                name=reviews.name_of(c.model),
                repo_id=c.model.repo_id,
                memory_gb=registry.memory_gb(c.model),
                fits=c.fits,
                is_looker=c.is_looker,
                why_not_now=c.why_not_now,
                problem=c.program_missing,
                program_missing=c.program_missing is not None,
            )
            for c in await reviews.candidates(db, now, known_kinds=known, missing=missing)
        ]
    return Status(
        available=found.ok,
        mode=found.mode,
        reason=reason,
        can_manage=found.ok and user.role == "admin",
        looker=looker,
        models=models,
        now=NowOut(
            overnight=schedule.has_window(budgets) and schedule.is_overnight(now, budgets),
            night_start=schedule.clock_text(budgets.night_start),
            night_end=schedule.clock_text(budgets.night_end),
            paused=config.paused,
        ),
        settings=LookingSettings(
            new_tags=looking.new_tags, stills_per_shot=looking.stills_per_shot
        ),
        pending_suggestions=await _pending_local(db),
    )


@router.get("/preview", response_model=Preview)
async def preview_review(
    user: CurrentUser,
    db: DbDep,
    deps: DepsDep,
    library_id: uuid.UUID,
    folder: str = "",
    model_id: uuid.UUID | None = None,
    redo: bool = False,
) -> Preview:
    """What the review dialog shows for a folder: what is left to look at,
    what must be made ready first, the model, about how long, and a review of
    this folder already running. Reads only; anyone signed in may ask."""
    library = await db.get(Library, library_id)
    if library is None:
        raise HTTPException(404, reviews.NO_SUCH_LIBRARY)
    try:
        found = await reviews.preview(
            db,
            library,
            folder,
            model_id=model_id,
            redo=redo,
            now=deps.now(),
            known_kinds=_kinds(deps),
            missing=await _missing(deps),
        )
    except reviews.ReviewRefused as err:
        raise _refused(err) from None
    active = found.get("active")
    found["active"] = None if active is None else _review(active, user)
    return Preview.model_validate(found)


@router.get("/reviews", response_model=Listing)
async def list_reviews(user: CurrentUser, db: DbDep, deps: DepsDep) -> Listing:
    """Every active review, then the newest twenty finished, and one line per
    library that looks at new footage. A job that ended under a review still
    marked active (cancelled on the AI models page, failed, pruned) is written
    back here, so the review does not read active for ever."""
    _demo_beat(deps.settings)
    found = await reviews.listing(db, deps.now())
    await db.commit()
    return Listing(
        available=found["available"],
        reviews=[_review(one, user) for one in found["reviews"]],
        ingest=[IngestLine.model_validate(one) for one in found["ingest"]],
    )


# --- writes -----------------------------------------------------------------------------


@router.post("/reviews", response_model=Review, status_code=202, dependencies=[require_admin])
async def start_review(
    body: ReviewBody,
    request: Request,
    user: CurrentUser,
    db: DbDep,
    deps: Changing,
    wake: WakeDep,
) -> Review:
    """Start now or Tonight. Only ever because an administrator asked: this
    route (and the chat panel's tool, which asks first) is how a folder
    review begins."""
    now = deps.now()
    try:
        started = await reviews.start(
            db,
            user,
            library_id=body.library_id,
            folder=body.folder,
            when=cast(Literal["now", "tonight"], body.when),
            model_id=body.model_id,
            redo=body.redo,
            prepare=body.prepare,
            now=now,
            known_kinds=_kinds(deps),
            # A model whose program is not installed is refused (409) with
            # the sentence its review would have failed with at once.
            missing=await _missing(deps),
        )
    except reviews.ReviewRefused as err:
        raise _refused(err) from None
    # Counted before the lane is woken: in demo mode it runs in this process
    # and would already be looking by the time the audit row is written.
    detail = (await reviews.asked(db, started)).detail
    request_id = started.id
    await db.commit()
    # The lane could not be woken: the review is taken back, unless the lane
    # has it already (the once-a-minute tick claimed one of its jobs while
    # the wake-up was failing). `reviews.withdraw` is the one place that
    # decides which, for this route and the chat panel's tool alike.
    if not await wake() and await reviews.withdraw(db, request_id):
        # Nothing is left waiting for a worker that will never come.
        await db.commit()
        raise HTTPException(503, QUEUE_DOWN)
    again = await db.get(LookRequest, request_id, populate_existing=True)
    if again is None:
        # Removed with its library in the moment since: nothing was started.
        raise HTTPException(404, reviews.NO_SUCH_LIBRARY)
    await auth_service.audit(
        db, reviews.AUDIT_STARTED, actor_user_id=user.id, ip=client_ip(request), detail=detail
    )
    await db.commit()
    return _review(await reviews.review(db, again, now), user)


@router.post("/reviews/{review_id}/cancel", response_model=Review, dependencies=[require_admin])
async def cancel_review(
    review_id: uuid.UUID, request: Request, user: CurrentUser, db: DbDep, deps: Changing
) -> Review:
    """Stop a review between two items. What was looked at is kept, and
    playable copies already asked for are still made (they are ordinary
    copies, useful anyway); the closing sentence says so."""
    now = deps.now()
    try:
        cancelled = await reviews.cancel(db, review_id, now=now)
    except reviews.ReviewRefused as err:
        raise _refused(err) from None
    library = await db.get(Library, cancelled.library_id)
    await auth_service.audit(
        db,
        "looks.review_cancelled",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={
            "library": library.name if library is not None else "",
            "folder": cancelled.folder,
            "done": cancelled.done,
            "total": cancelled.total,
        },
    )
    await db.commit()
    return _review(await reviews.review(db, cancelled, now), user)


@router.put("/settings", response_model=LookingSettings, dependencies=[require_admin])
async def save_settings(
    body: SettingsBody, request: Request, user: CurrentUser, db: DbDep, _deps: Changing
) -> LookingSettings:
    """Whether the Looker may propose tags not yet in the catalogue, and how
    many pictures it is shown of each shot. Takes effect at the next item."""
    if body.stills_per_shot is not None and body.stills_per_shot not in STILLS_CHOICES:
        raise HTTPException(400, BAD_STILLS)
    config = await load_looking_config(db)
    wanted = LookingConfig(
        new_tags=config.new_tags if body.new_tags is None else body.new_tags,
        stills_per_shot=(
            config.stills_per_shot if body.stills_per_shot is None else body.stills_per_shot
        ),
    )
    changed = [
        name
        for name in ("new_tags", "stills_per_shot")
        if getattr(wanted, name) != getattr(config, name)
    ]
    if changed:
        await auth_service.audit(
            db,
            "looks.settings_updated",
            actor_user_id=user.id,
            ip=client_ip(request),
            detail={"changed": changed},
        )
        try:
            await save_looking_config(db, wanted)  # commits, with the audit row
        except ValueError as err:  # checked above; the store's own guard
            raise HTTPException(400, str(err)) from None
    return LookingSettings(new_tags=wanted.new_tags, stills_per_shot=wanted.stills_per_shot)
