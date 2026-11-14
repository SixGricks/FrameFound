"""Asking the local Editor for a rough cut: the request, and the job that runs it.

A draft is asked for in words ("thirty seconds of smooth aerials at golden
hour") about some footage (a folder, or clips ticked in Search). FrameFound
decides which shots the Editor is offered (`ai/cut_editor.choose`, at most
80, remembered on the request), the Editor answers with numbered shots and
seconds, FrameFound checks every number (`cut_editor.check`), and the cut is
written by the one function Claude's cuts use (`nle/roughcut.build_rough_cut`)
with `source` "local". Nothing a model says reaches a timeline unchecked.

The request is a `cut_drafts` row; the work is a `draft_cut` row of L1's work
table, run on the `local_ai` lane like every other local-AI job. So "waiting
until 21:30", "paused", Cancel and "the worker died" are the job's states,
read through `shown`, and the page, the API and the chat tool never call a
model: they write a request and read how it went (L1: `ask` only from a
handler on the lane).

Only the handler writes a draft's state once it is queued. The two places
that end a draft without it are L1's: a Cancel of a job that was not yet
running, and a job whose worker died; both reach the draft through the
kind's `abandoned` tidy-up, in the session that ends the job.
"""

import asyncio
import inspect
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.ai import cut_editor
from framefound.config import Settings, get_settings
from framefound.db.models import (
    Asset,
    Cut,
    CutDraft,
    CutPiece,
    Library,
    LocalAiJob,
    LocalModel,
    ShotWindow,
    catalogued,
)
from framefound.local_models import ROLE_TITLES, UNAVAILABLE, availability, fake, jobs, registry
from framefound.local_models import schedule as model_schedule
from framefound.local_models.ask import Answer, ModelFailed, NotNow, ask
from framefound.local_models.kinds import is_demo_runtime, kinds
from framefound.local_models.settings_store import load_config
from framefound.media import folders
from framefound.nle import fcp7, roughcut, shotlist

log = structlog.get_logger()

KIND = "draft_cut"  # the local_ai job kind
# Active as *stored*: `waiting` is never stored on a draft, only read off its job.
ACTIVE = ("queued", "waiting", "running")
_STORED_ACTIVE = ("queued", "running")
FINISHED = ("done", "failed", "cancelled")
# Characters of the model's text kept on a draft: enough to show what it
# wrote instead of a cut, not a store of everything it ever said.
ANSWER_KEPT = 20_000
# What the job row keeps under Details when the answer was unusable.
DETAIL_KEPT = 4000
# One question per draft, a long one: a 12B model writing a few hundred
# tokens after reading 80 lines (L5 section 12 item 3 says this is unmeasured).
TIMEOUT_S = 600.0
# "A person pressed a button and is watching" (L1 13.2).
PRIORITY = 0
TITLE_MAX = 120
DEFAULT_TARGET_S = 30.0
# Clips one draft may be asked to choose from, when they are named one by one.
MOST_CLIPS = 300

STOPPED = "It stopped without finishing. Draft it again."
QUEUED_SAYS = "Waiting its turn."
RUNNING_SAYS = "The Editor is drafting it."
NO_EDITOR = "No model has the Editor role yet. Give one the role on the AI models page."
EDITOR_COPYING = (
    "The Editor's model is still being copied to this Mac. A draft can be asked for once it is "
    "there."
)
NO_SCOPE = "Choose a folder, or tick some clips in Search first."
NO_SHOTS_FOLDER = (
    "None of the clips in this folder have been analysed into shots yet. That happens after a "
    "clip's preview copy is made — Processing shows how far along it is."
)
NO_SHOTS_SELECTION = "None of the ticked clips have been analysed into shots yet."
# Nothing to analyse at all: no clip matched, or every one has gone since.
NO_VIDEO_FOLDER = "This folder holds no video on the share now."
NO_VIDEO_MATCHING = 'No video on the share is in a folder matching "{contains}".'
NO_VIDEO_SELECTION = "None of these clips is on the share now."
# Footage holding more shots than one shot search reads (`shotlist.SCAN_LIMIT`):
# the search would stop at that many in no particular order, and the Editor
# would be offered an arbitrary part of it, a different part each time (P22).
# Word for word the sentences `POST /cuts/drafts` answers with (api/v1/cuts.py),
# so the chat panel's tool is refused as the page is.
TOO_MANY_SHOTS_FOLDER = (
    "This folder holds more shots than one draft can read ({most:,}). Choose a folder inside "
    "it, such as one shoot."
)
TOO_MANY_SHOTS_MATCHING = (
    'The folders matching "{contains}" hold more shots than one draft can read ({most:,}). '
    "Name a narrower folder."
)
TOO_MANY_SHOTS_SELECTION = (
    "These clips hold more shots than one draft can read ({most:,}). Choose fewer clips."
)
ALL_USED = (
    "Every analysed shot here has been used in a cut already. Include shots already used, or "
    "choose other footage."
)
SHORTEST_LONGER = "The shortest shot cannot be longer than the longest."
NO_LIBRARY = "No such library."
NO_CUT = "No such cut."
ALREADY_WAITING = "A draft with this title for this footage is already waiting."
QUEUE_DOWN = "The processing queue is unavailable. Nothing was started."
NOT_A_FORM = "The Editor answered, but not in the form asked for."
OUT_OF_ROOM = (
    "The Editor ran out of room before it finished. Ask for a shorter cut, or longer shots."
)
NOTHING_USABLE = "The Editor did not choose any shot FrameFound could use."
PARENT_GONE = (
    "The cut it was to revise has been deleted since, so this draft starts a cut of its own."
)
BY = "local editor"


class DraftError(ValueError):
    """The request cannot become a draft; the message is the sentence. `.status`
    is the HTTP code the API answers with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# --- what is asked for ----------------------------------------------------------


class Scope(BaseModel):
    """The footage. A scope is always a folder or a list of clips, never "the
    whole library": the shot search reads at most `SCAN_LIMIT` rows in no
    particular order (P22)."""

    kind: Literal["folder", "selection"]
    library_id: uuid.UUID | None = None  # folder
    # This folder and everything beneath it, exactly as the picker gave it.
    folder: str = Field(default="", max_length=1024)
    # find_shots' meaning: these letters anywhere in the path (the chat tool).
    contains: str = Field(default="", max_length=200)
    asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MOST_CLIPS)  # selection


class DraftRequest(BaseModel):
    brief: str = Field(min_length=1, max_length=1000)
    title: str = Field(default="", max_length=TITLE_MAX)  # the brief, shortened, when empty
    scope: Scope
    target_s: float | None = Field(default=None, ge=5, le=600)  # None: read from the brief, else 30
    min_shot_s: float = Field(default=1.5, ge=0.5, le=30)
    max_shot_s: float = Field(default=8.0, ge=1, le=60)
    exclude_used: bool = False
    revises: uuid.UUID | None = None  # "Draft again": the next version of this cut's family


def short_title(raw: str, limit: int = TITLE_MAX) -> str:
    """A title that fits, cut at the last word before `limit` with an
    ellipsis; also a safe file name stem's length (P24)."""
    text = " ".join(raw.split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    head = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return head.rstrip(" ,;:-–") + "…"


def constraints(request: DraftRequest) -> cut_editor.Constraints:
    """The length asked for: the form's figure, else the one the brief names, else 30 s.
    `DraftError` when the shortest shot asked for is longer than the longest."""
    if request.min_shot_s > request.max_shot_s:
        raise DraftError(SHORTEST_LONGER)
    target = request.target_s or cut_editor.read_brief(request.brief).target_s or DEFAULT_TARGET_S
    return cut_editor.Constraints(
        target_s=float(target), min_shot_s=request.min_shot_s, max_shot_s=request.max_shot_s
    )


# --- whether the Editor can draft here --------------------------------------------


@dataclass(frozen=True)
class EditorStatus:
    mode: Literal["ready", "demo", "unavailable"]
    note: str | None  # UNAVAILABLE or the demo's banner
    model_id: uuid.UUID | None
    model_name: str | None  # the repository's last part
    runs: Literal["now", "overnight", "never"] | None
    starts_at: datetime | None  # when "overnight" and it is day: this Mac's wall clock
    paused: bool
    why_not: str | None  # None: a draft may be asked for


def _name(model: LocalModel) -> str:
    return model.repo_id.rsplit("/", 1)[-1]


async def editor_status(
    db: AsyncSession, *, now: datetime | None = None, settings: Settings | None = None
) -> EditorStatus:
    """What the form says before the button is pressed. A model that only fits
    overnight is not a refusal (L1 D21): the draft is accepted and waits, and
    `starts_at` says until when. A pause is not one either: the draft waits
    for Resume."""
    settings = settings or get_settings()
    found = availability(settings)
    if not found.ok:
        return EditorStatus("unavailable", found.reason, None, None, None, None, False, UNAVAILABLE)
    note: str | None = None
    if found.mode == "demo":
        # The banner's words; the module is imported only where demo mode is on.
        from framefound.local_models.demo import DEMO_NOTE

        note = DEMO_NOTE
    config = await load_config(db)
    role = (await registry.roles(db))["editor"]
    model = None if role.model_id is None else await registry.get_model(db, role.model_id)
    if model is None:
        why = EDITOR_COPYING if role.pending_model_id is not None else NO_EDITOR
        return EditorStatus(found.mode, note, None, None, None, None, config.paused, why)
    budgets = config.budgets()
    needed = registry.memory_gb(model)
    fits = model_schedule.fits(needed, budgets)
    runs: Literal["now", "overnight", "never"] = (
        "now" if fits == "day" else "overnight" if fits == "night" else "never"
    )
    starts_at = None
    if runs == "overnight":
        decision = model_schedule.decide(needed, now or datetime.now(), budgets)
        if decision.why == "window":
            starts_at = decision.not_before
    problem = registry.role_problem(model, "editor", config.lists(), budgets, kinds(settings))
    why_not = None
    if problem is not None:
        # L1's sentences begin "It …": said of the Editor, by name, on this page.
        why_not = f"The {ROLE_TITLES['editor']} ({_name(model)}) cannot draft right now. {problem}"
    return EditorStatus(
        found.mode, note, model.id, _name(model), runs, starts_at, config.paused, why_not
    )


# --- which shots ------------------------------------------------------------------


class Offer(NamedTuple):
    """What the Editor will be offered, and the figures for "62 of 480 shots,
    from 41 clips"."""

    candidates: list[cut_editor.Candidate]
    shots_in_scope: int
    clips: int
    label: str  # "GELCO › 2026/Pete Dye", "9 clips ticked in Search"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


async def candidates_for(
    db: AsyncSession, request: DraftRequest, *, origin: Literal["web", "chat"] = "web"
) -> Offer:
    """The shots in scope, and the ones the Editor is offered. `DraftError`
    (400) when the scope names nothing, holds no analysed shot, or holds more
    shots than one search reads (`TOO_MANY_SHOTS_*`); 404 for a library that
    does not exist."""
    scope = request.scope
    library: Library | None = None
    if scope.library_id is not None:
        library = await db.get(Library, scope.library_id)
        if library is None:
            raise DraftError(NO_LIBRARY, 404)
    base = shotlist.ShotFilter(
        library_id=scope.library_id if scope.kind == "folder" else None,
        min_length_s=request.min_shot_s,
        exclude_used=request.exclude_used,
    )
    if scope.kind == "selection":
        ids = list(dict.fromkeys(scope.asset_ids))
        if not ids:
            raise DraftError(NO_SCOPE)
        base.asset_ids = ids
        where = "named in the chat" if origin == "chat" else "ticked in Search"
        label = f"{_plural(len(ids), 'clip')} {where}"
    else:
        try:
            folder = folders.clean_folder(scope.folder) if scope.folder else ""
        except folders.FolderError as err:
            raise DraftError(str(err)) from None
        contains = scope.contains.strip()
        if folder:
            base.folder_prefix = folder
            label = folder
        elif contains:
            base.folder = contains
            label = f'folders matching "{contains}"'
        else:
            raise DraftError(NO_SCOPE)
        if library is not None:
            label = f"{library.name} › {label}"
        elif not folder:
            label = label[0].upper() + label[1:]
    too_many = await _too_many_shots(db, base, scope)
    if too_many is not None:
        raise DraftError(too_many)
    try:
        scan = await shotlist.scan_shots(db, base)
    except shotlist.ShotError as err:
        raise DraftError(str(err)) from None
    c = constraints(request)
    offered = cut_editor.choose(
        scan.shots, cut_editor.read_brief(request.brief), c, look_of=scan.look_of
    )
    if not offered:
        raise DraftError(await _why_nothing(db, base, scope))
    clips = len({asset.id for _window, asset, _used in scan.shots})
    return Offer(offered, len(scan.shots), clips, label)


async def _why_nothing(db: AsyncSession, base: shotlist.ShotFilter, scope: Scope) -> str:
    """Nothing to offer, said for what it is: every shot here was used, or
    every one is shorter than the shortest asked for, or the footage was
    never analysed into shots, or there is no footage here at all. Asked
    only when a draft is refused."""
    shortest = base.min_length_s
    if base.exclude_used:
        base.exclude_used = False
        if await shotlist.select_shots(db, base):
            return ALL_USED
    base.min_length_s = 0.0
    if await shotlist.select_shots(db, base):
        return (
            f"No analysed shot here is at least {shortest:g} s long. Ask for shorter shots, or "
            "choose other footage."
        )
    if not await _any_video(db, base):
        # "Not analysed yet, see Processing" would send the person to wait
        # for footage that is not there: a folder name Claude misheard, or
        # a folder whose clips have all gone from the share since.
        if scope.kind == "selection":
            return NO_VIDEO_SELECTION
        if base.folder:
            return NO_VIDEO_MATCHING.format(contains=base.folder)
        return NO_VIDEO_FOLDER
    return NO_SHOTS_SELECTION if scope.kind == "selection" else NO_SHOTS_FOLDER


def _clips_in(base: shotlist.ShotFilter) -> list[Any]:
    """The clips `shotlist.scan_shots` reads for the scope part of `base`:
    online, catalogued video in its library, folder or list of clips."""
    where: list[Any] = [Asset.availability == "online", Asset.media_type == "video", catalogued()]
    if base.library_id is not None:
        where.append(Asset.library_id == base.library_id)
    if base.folder:
        where.append(func.lower(Asset.relative_path).contains(base.folder.lower(), autoescape=True))
    if base.folder_prefix:
        where.extend(folders.under(base.folder_prefix))
    if base.asset_ids:
        where.append(Asset.id.in_(list(base.asset_ids)))
    return where


async def _any_video(db: AsyncSession, base: shotlist.ShotFilter) -> bool:
    """Whether the scope holds any online, catalogued video, analysed or not:
    the clips `shotlist.scan_shots` would read, before shot windows."""
    return await db.scalar(select(Asset.id).where(*_clips_in(base)).limit(1)) is not None


async def _too_many_shots(db: AsyncSession, base: shotlist.ShotFilter, scope: Scope) -> str | None:
    """The sentence when the scope holds more shot windows than one shot
    search reads (`shotlist.SCAN_LIMIT`, read when asked), else None. Past
    it the search stops in no particular order and the Editor would be
    offered an arbitrary part of the footage. Checked here, where the page
    and the chat panel's tool both pass, so neither can draft from a sample
    (the page's endpoint asks the same before the Editor's status). Counting
    stops just past the bound: a whole client folder is not counted to its
    last shot to learn that it is too much."""
    most = shotlist.SCAN_LIMIT
    windows = (
        select(ShotWindow.id)
        .join(Asset, Asset.id == ShotWindow.asset_id)
        .where(*_clips_in(base))
        .limit(most + 1)
        .subquery()
    )
    if int(await db.scalar(select(func.count()).select_from(windows)) or 0) <= most:
        return None
    if scope.kind == "selection":
        return TOO_MANY_SHOTS_SELECTION.format(most=most)
    if base.folder_prefix:
        return TOO_MANY_SHOTS_FOLDER.format(most=most)
    return TOO_MANY_SHOTS_MATCHING.format(contains=base.folder, most=most)


def scope_json(request: DraftRequest, label: str) -> dict[str, Any]:
    """The scope as a draft keeps it: what was asked, and its label."""
    return {**request.scope.model_dump(mode="json"), "label": label}


# --- asking for a draft -------------------------------------------------------------


def _subject(title: str, label: str) -> str:
    # What the job is called where jobs are listed ("Drafting <subject>").
    return f"{title} ({label})"[:300]


def _footage(scope: Any) -> tuple[str, tuple[str, ...]]:
    """What "the same footage" is judged on: the label (a folder, by library
    and path) and, for clips named one by one, which clips. The label alone
    says only how many ("9 clips ticked in Search"), and nine other clips
    are another draft."""
    kept = scope if isinstance(scope, dict) else {}
    ids = kept.get("asset_ids") or [] if kept.get("kind") == "selection" else []
    return str(kept.get("label", "")), tuple(sorted({str(each) for each in ids}))


async def _already_waiting(db: AsyncSession, title: str, scope: dict[str, Any]) -> bool:
    """A draft with this title for this footage is still to come: its job is
    queued, waiting or running. Judged here rather than by the job's subject,
    which names the footage only as a person reads it."""
    wanted = _footage(scope)
    active = await db.scalars(
        select(CutDraft)
        .join(LocalAiJob, LocalAiJob.id == CutDraft.job_id)
        .where(
            CutDraft.title == title,
            CutDraft.state.in_(_STORED_ACTIVE),
            LocalAiJob.kind == KIND,
            LocalAiJob.state.in_(jobs.ACTIVE),
        )
    )
    return any(_footage(row.scope) == wanted for row in active)


def _provenance_of(model: LocalModel | None) -> dict[str, Any]:
    """Which build is being asked, before it answers: kept on the draft so one
    that fails still says which model failed."""
    if model is None:
        return {}
    return {
        "model": model.repo_id[:200],
        "model_revision": model.revision[:64] if model.revision else None,
        "variant": (model.variant or "")[:100],
        "runtime": model.runtime[:30] if model.runtime else None,
        "prompt_version": cut_editor.PROMPT_VERSION,
    }


async def request_draft(
    db: AsyncSession,
    request: DraftRequest,
    *,
    user_id: uuid.UUID | None,
    origin: Literal["web", "chat"] = "web",
    wake: Callable[[], bool | Awaitable[bool]] | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> CutDraft:
    """Ask the Editor for a draft: check, choose the shots, queue the job,
    commit, wake the lane. `wake` may answer at once or be awaited (the API
    wakes the lane in a thread); None wakes the real lane in a thread.
    `DraftError` with the sentence and its status;
    nothing is written when it is raised. A queue that is down leaves
    nothing behind (503): a draft waiting for a worker that will never come
    is worse than a plain "try again"."""
    status = await editor_status(db, now=now, settings=settings)
    if status.why_not is not None:
        raise DraftError(status.why_not, 409)
    if request.min_shot_s > request.max_shot_s:
        raise DraftError(SHORTEST_LONGER)
    if request.revises is not None and await db.get(Cut, request.revises) is None:
        raise DraftError(NO_CUT, 404)
    offer = await candidates_for(db, request, origin=origin)
    c = constraints(request)
    given = " ".join(request.title.split())
    title = short_title(given or request.brief)
    scope = scope_json(request, offer.label)
    if await _already_waiting(db, title, scope):
        raise DraftError(ALREADY_WAITING, 409)
    draft_id = uuid.uuid4()
    job = await jobs.create(
        db,
        KIND,
        subject=_subject(title, offer.label),
        role="editor",
        payload={"draft_id": str(draft_id), "title_given": bool(given)},
        priority=PRIORITY,
        user_id=user_id,
        # Two drafts of one title for different clips share a subject; whether
        # it is the same draft is `_already_waiting`'s to say.
        unique=False,
    )
    model = None if status.model_id is None else await registry.get_model(db, status.model_id)
    draft = CutDraft(
        id=draft_id,
        state="queued",
        title=title,
        brief=request.brief.strip(),
        scope=scope,
        target_s=c.target_s,
        min_shot_s=c.min_shot_s,
        max_shot_s=c.max_shot_s,
        exclude_used=request.exclude_used,
        candidates=[candidate.as_json() for candidate in offer.candidates],
        shots_in_scope=offer.shots_in_scope,
        clips=offer.clips,
        revises_cut_id=request.revises,
        job_id=job.id,
        origin=origin,
        requested_by=user_id,
        created_at=datetime.now(UTC),
        **_provenance_of(model),
    )
    db.add(draft)
    await db.commit()
    if wake is None:
        # Imported here: that module imports this one at its foot, to register the kind.
        from framefound.processing import local_ai_tasks

        def in_a_thread() -> Awaitable[bool]:
            # Reaching the broker blocks (it retries when the queue is down),
            # so it is done off the event loop that is serving other requests.
            return asyncio.to_thread(local_ai_tasks.wake, settings)

        wake = in_a_thread
    woke = wake()
    if inspect.isawaitable(woke):
        woke = await woke
    if not woke:
        await db.delete(draft)
        await db.delete(job)
        await db.commit()
        raise DraftError(QUEUE_DOWN, 503)
    log.info(
        "cut_drafts.requested", draft_id=str(draft.id), origin=origin, shots=len(offer.candidates)
    )
    return draft


async def cancel(db: AsyncSession, draft: CutDraft) -> CutDraft:
    """Cancel. A draft whose job had not started ends at once (the job's own
    tidy-up writes it); a running one is asked to stop, and the handler ends
    it when the model's answer comes back (P13). `DraftError(…, 409)` once it
    has finished. Flushed; the caller commits."""
    if draft.state not in _STORED_ACTIVE or draft.job_id is None:
        raise DraftError(jobs.FINISHED_ALREADY, 409)
    try:
        job = await jobs.request_cancel(db, draft.job_id)
    except jobs.NoSuchJob:
        raise DraftError(jobs.FINISHED_ALREADY, 409) from None
    except jobs.JobRefused as err:
        raise DraftError(str(err), 409) from None
    if job.state == "cancelled":
        # The kind's tidy-up has already said so; said again, guarded, in
        # case it could not.
        await _end_draft(db, draft.id, "cancelled", job.message or jobs.CANCELLED)
    await db.flush()
    await db.refresh(draft)
    return draft


def shown(
    draft: CutDraft, job: LocalAiJob | None, now: datetime | None = None
) -> tuple[str, str, bool]:
    """(state, message, stopping) as a draft should be read. A finished draft
    is as stored; an active one is its job, which is the truth while it
    runs: `waiting` and its sentence ("Waiting for the overnight window…")
    live only there."""
    if draft.state in FINISHED:
        return draft.state, draft.message, False
    if job is None:
        return "failed", STOPPED, False
    state, message = jobs.shown(job, now)
    if state in ("queued", "waiting", "running"):
        said = message or (QUEUED_SAYS if state == "queued" else RUNNING_SAYS)
        return state, said, bool(job.cancel_requested)
    if state == "done":
        # The handler wrote the cut and died before it wrote the draft.
        return "failed", STOPPED, False
    if message == jobs.STOPPED:
        message = STOPPED
    return state, message, False


async def job_of(db: AsyncSession, draft: CutDraft) -> LocalAiJob | None:
    """The draft's job as the database has it now (None once pruned)."""
    if draft.job_id is None:
        return None
    return await db.get(LocalAiJob, draft.job_id, populate_existing=True)


# --- the job ------------------------------------------------------------------------


async def _end_draft(
    db: AsyncSession, draft_id: uuid.UUID, state: str, message: str, **more: Any
) -> bool:
    """End an active draft, guarded on it still being active: a draft that
    already ended keeps how it ended."""
    ended: Any = await db.execute(
        update(CutDraft)
        .where(CutDraft.id == draft_id, CutDraft.state.in_(_STORED_ACTIVE))
        .values(
            state=state,
            message=message[: jobs.MESSAGE_LENGTH],
            finished_at=datetime.now(UTC),
            **more,
        )
        .execution_options(synchronize_session=False)
    )
    return bool(ended.rowcount)


def _draft_id(job: LocalAiJob) -> uuid.UUID | None:
    payload = job.payload if isinstance(job.payload, dict) else {}
    try:
        return uuid.UUID(str(payload.get("draft_id")))
    except ValueError:
        return None


async def _abandoned(db: AsyncSession, job: LocalAiJob, why: jobs.Why) -> None:
    """The job ended without its handler (cancelled before it ran, its worker
    died, this install cannot run models, the handler crashed): the draft
    says so too, so it reads right even after the job row is pruned."""
    draft_id = _draft_id(job)
    if draft_id is None:
        return
    if why == "cancelled":
        await _end_draft(db, draft_id, "cancelled", job.message or jobs.CANCELLED)
    elif why == "stopped":
        await _end_draft(db, draft_id, "failed", STOPPED)
    else:
        await _end_draft(db, draft_id, "failed", job.message or STOPPED)


async def _cancel_pressed(ctx: jobs.JobContext) -> bool:
    """Read now: `ctx.progress` reads Cancel only when it writes, and an answer
    that arrives after Cancel must not be kept as if nobody had pressed it."""
    async with ctx.factory() as db:
        return bool(
            await db.scalar(select(LocalAiJob.cancel_requested).where(LocalAiJob.id == ctx.job_id))
        )


def _kept(text: str, limit: int = ANSWER_KEPT) -> str:
    # What a model wrote, as text a database (PostgreSQL refuses a NUL) and a
    # page can hold.
    return fcp7.xml_text(text)[:limit]


async def _fail(
    db: AsyncSession, draft: CutDraft, message: str, *, text: str = "", **more: Any
) -> None:
    if text:
        more["answer_text"] = _kept(text)
    await _end_draft(db, draft.id, "failed", message, **more)
    await db.commit()


def _answer_provenance(answer: Answer, model: LocalModel | None) -> dict[str, str]:
    # `Answer.provenance()` has the five names; the build's variant is the model row's.
    return {**answer.provenance(), "variant": (model.variant or "") if model is not None else ""}


async def run_draft(ctx: jobs.JobContext, job: LocalAiJob) -> dict[str, Any] | None:
    """The `draft_cut` handler: ask the Editor once, check its answer, write the cut.

    A model that may not run yet (`NotNow` that can retry: the overnight
    window, a pause, a busy server) propagates untouched, so the runner makes
    the job `waiting` with the sentence and runs it again (P10). Every other
    failure ends the draft `failed` in its own words and re-raises, so the
    job row says the same (P11) — except an answer with no usable shot,
    which is a finding about the draft: the job is done, the draft failed.
    """
    draft_id = _draft_id(job)
    async with ctx.factory() as db:
        draft = None if draft_id is None else await db.get(CutDraft, draft_id)
        if draft is None or draft.state in FINISHED:
            # A second delivery, or a draft cleared from the list meanwhile.
            return {"skipped": True}
        role = (await registry.roles(db))["editor"]
        model = None if role.model_id is None else await registry.get_model(db, role.model_id)
        draft.state = "running"
        draft.started_at = draft.started_at or datetime.now(UTC)
        for name, value in _provenance_of(model).items():
            setattr(draft, name, value)
        await db.commit()

        c = cut_editor.Constraints(draft.target_s, draft.min_shot_s, draft.max_shot_s)
        candidates = [cut_editor.Candidate.from_json(row) for row in draft.candidates]
        try:
            answer = await ask(
                db,
                "editor",
                prompt=cut_editor.prompt(draft.brief, c, candidates),
                prompt_version=cut_editor.PROMPT_VERSION,
                instructions=cut_editor.INSTRUCTIONS,
                schema=cut_editor.EditorAnswer,
                max_tokens=cut_editor.max_tokens(c),
                timeout_s=TIMEOUT_S,
                server=ctx.deps.server,
                now=ctx.deps.now(),
                settings=ctx.deps.settings,
            )
        except NotNow as err:
            if err.retry:
                # What `ask` noted (nothing, usually) is kept; the wait is the job's.
                await db.commit()
                raise
            await _fail(db, draft, str(err))
            raise
        except ModelFailed as err:
            if err.reason == "bad_answer":
                ran_out = err.finish_reason == "length" or cut_editor.cut_off(err.text)
                said = OUT_OF_ROOM if ran_out else NOT_A_FORM
                await _fail(db, draft, said, text=err.text)
                raise jobs.Failed(said, detail=err.text[:DETAIL_KEPT]) from None
            await _fail(db, draft, str(err), text=err.text)
            raise
        # Also commits what `ask` noted on the model row (the memory it saw).
        await db.commit()

        stop = False
        try:
            await ctx.progress(phase="checking")
        except jobs.Cancelled:
            stop = True
        if stop or await _cancel_pressed(ctx):
            # Pressed while the model was answering: the page said "stopping",
            # so the answer is thrown away. What answered is kept on the job.
            await _end_draft(db, draft.id, "cancelled", jobs.CANCELLED)
            await db.commit()
            await ctx.record(answer)
            raise jobs.Cancelled()

        model = await registry.get_model(db, answer.model_id)
        provenance = _answer_provenance(answer, model)
        parsed = answer.parsed
        if not isinstance(parsed, cut_editor.EditorAnswer):  # a schema was given: never
            await _fail(db, draft, NOT_A_FORM, text=answer.text, **provenance)
            raise jobs.Failed(NOT_A_FORM, detail=answer.text[:DETAIL_KEPT])
        pieces, problems = cut_editor.check(parsed, candidates, c)
        pieces, gone = await _still_there(db, pieces, candidates)
        problems = [*problems, *gone]
        # What the draft keeps of the answer however it ends; `problems` is
        # the same list a deleted parent's sentence is added to below.
        finding: dict[str, Any] = {
            "problems": problems,
            "answer_text": _kept(answer.text),
            "seconds": answer.seconds,
            "demo": answer.demo,
            **provenance,
        }
        if not pieces:
            await _end_draft(db, draft.id, "failed", NOTHING_USABLE, **finding)
            await db.commit()
            await ctx.record(answer)
            log.info("cut_drafts.nothing_usable", draft_id=str(draft.id), problems=len(problems))
            return {"cuts": 0}

        parent = draft.revises_cut_id
        if parent is not None and await db.get(Cut, parent) is None:
            parent = None
            problems.append(PARENT_GONE)
        assets = {
            asset.id: asset
            for asset in await db.scalars(
                select(Asset).where(Asset.id.in_({piece.asset_id for piece in pieces}))
            )
        }
        title = draft.title
        if not (job.payload or {}).get("title_given") and parsed.title.strip():
            title = short_title(parsed.title) or title
        try:
            built = await roughcut.build_rough_cut(
                db,
                title,
                [
                    roughcut.CutIn(
                        id=str(piece.asset_id),
                        in_s=piece.in_s,
                        out_s=piece.out_s,
                        note=piece.reason,
                    )
                    for piece in pieces
                ],
                profile=None,
                record=True,
                must_record=True,
                source="local",
                created_by=draft.requested_by,
                by=BY,
                parent=parent,
                provenance={k: v for k, v in provenance.items() if v is not None},
                sequence_fps=roughcut.majority_rate(
                    [(assets[piece.asset_id].fps, piece.length) for piece in pieces]
                ),
            )
        except roughcut.CutError as err:
            await _fail(db, draft, str(err), **finding)
            raise jobs.Failed(str(err)) from None
        await _end_draft(
            db,
            draft.id,
            "done",
            "",
            cut_id=uuid.UUID(built.cut_id) if built.cut_id else None,
            **finding,
        )
        await db.commit()
    await ctx.record(answer)
    log.info(
        "cut_drafts.done",
        draft_id=str(draft.id),
        cut_id=built.cut_id,
        cuts=built.cuts,
        demo=answer.demo,
    )
    return {"cut_id": built.cut_id, "cuts": built.cuts, "seconds": built.duration_s}


async def _still_there(
    db: AsyncSession, pieces: Sequence[cut_editor.Piece], candidates: Sequence[cut_editor.Candidate]
) -> tuple[list[cut_editor.Piece], list[str]]:
    """The pieces whose clip is still an online, catalogued video, and a
    sentence for each that is not: a draft can wait hours for 21:30."""
    ids = {piece.asset_id for piece in pieces}
    found = {
        asset.id: asset
        for asset in await db.scalars(
            select(Asset).where(Asset.id.in_(ids)).execution_options(populate_existing=True)
        )
    }
    names = {candidate.asset_id: candidate.filename for candidate in candidates}
    kept: list[cut_editor.Piece] = []
    said: list[str] = []
    for piece in pieces:
        asset = found.get(piece.asset_id)
        clip = shotlist.clip_name(names.get(piece.asset_id, ""))
        if asset is None or asset.proxy_of_id is not None or asset.media_type != "video":
            said.append(f"Shot {piece.n} ({clip}) was left out: its clip has been deleted.")
        elif asset.availability != "online":
            said.append(
                f"Shot {piece.n} ({clip}) was left out: its clip is no longer on the share."
            )
        else:
            kept.append(piece)
    return kept, said


def _title(job: LocalAiJob) -> str:
    return f"Drafting {job.subject}"


# --- asking Claude to review a cut ---------------------------------------------------

# What each source is called on the page, and in a review request's label.
SOURCE_LABELS = {"local": "Local draft", "mcp": "Claude", "import": "Imported timeline"}
_SOURCE_WORDS = {
    "local": "a local draft",
    "mcp": "made by Claude",
    "import": "an imported timeline",
}
# The chat panel refuses a message over 20,000 characters
# (`chat.engine.MAX_TEXT_CHARS`), and the operator may add words of their own
# before sending, so the request leaves room.
REVIEW_CHARS = 18_000
# What of the check's sentences and the brief a request carries at most: with
# the cut and the list trimmed away, the rest always fits.
_PROBLEMS_KEPT = 3000
_BRIEF_KEPT = 1000
REVIEW_OPENS = "Please review this rough cut and improve it if you can."
_CUT_IS = "in order (number, clip id, clip, in-out seconds of the source, why it is there)"
_LIST_IS = "(find_shots lines; the id that starts a line is accepted by every tool)"
NOT_RECORDED = (
    "The shots it was chosen from were not recorded for this cut; find_shots lists what else "
    "there is."
)


@dataclass(frozen=True)
class ReviewRequest:
    """The message the page hands the chat panel, unchanged, and what it holds."""

    text: str
    label: str  # "Local draft, version 1 — 9 shots, and the 62 it was chosen from"
    shots: int  # the cut's pieces listed
    candidates: int  # the shots it was chosen from listed


def _flat(text: str) -> str:
    """Words on one line. A model's reason, a brief or a file name stays an
    item in a list, never a paragraph of its own (P17)."""
    return " ".join(str(text).split())


def _s(value: float) -> str:
    return f"{round(value, 1):g}"


def _piece_line(number: int, piece: CutPiece) -> str:
    clip_id = shotlist.short_id(piece.asset_id) if piece.asset_id is not None else "(deleted)"
    line = (
        f"{number} {clip_id} {shotlist.clip_name(piece.filename)} "
        f"{piece.in_ms / 1000:.1f}-{piece.out_ms / 1000:.1f}s"
    )
    if piece.speed and abs(piece.speed - 1.0) > 1e-6:
        line += f" at {round(piece.speed * 100)}%"
    note = _flat(piece.note)
    return f"{line} - {note}" if note else line


def demonstration(cut: Cut, draft: CutDraft | None) -> bool:
    """Whether no model judged this cut: the demo's runtime built it, or the
    model server that answered its draft said its answer was made up. The
    one rule the page's label, the review request and the chat tool share."""
    return is_demo_runtime(cut.runtime or "") or bool(draft is not None and draft.demo)


def _made_by(cut: Cut, draft: CutDraft | None) -> str:
    said = _SOURCE_WORDS.get(cut.source, f"from {cut.source}")
    if cut.model:
        build = f"{cut.model.rsplit('/', 1)[-1]}"
        if cut.model_revision:
            build += f", build {cut.model_revision[:7]}"
        if demonstration(cut, draft):
            build += ", a demonstration that judged nothing"
        said += f" ({build})"
    return said


async def made_from(db: AsyncSession, cut: Cut) -> CutDraft | None:
    """The draft this cut was made from, or None (Claude's, an imported timeline)."""
    found: CutDraft | None = await db.scalar(
        select(CutDraft)
        .where(CutDraft.cut_id == cut.id)
        .order_by(CutDraft.created_at.desc(), CutDraft.id)
        .limit(1)
    )
    return found


async def review_request(db: AsyncSession, cut: Cut, *, limit: int = REVIEW_CHARS) -> ReviewRequest:
    """The message that asks Claude to review `cut`: what it is, its full id,
    the brief, the cut piece by piece with the reasons given, the shots it
    was chosen from as `find_shots` prints them, and how to keep a better
    version beside it (`build_rough_cut(parent=…)`).

    The full id is the way in for Claude: `recent_cuts` never lists a local
    draft nobody downloaded. Everything a model or a file name says is a list
    item on one line, never an instruction. Kept to `limit` characters: the
    list it was chosen from is cut from its end first, then the cut's own
    list, and each heading says how many of how many are there."""
    pieces = list(
        await db.scalars(
            select(CutPiece).where(CutPiece.cut_id == cut.id).order_by(CutPiece.position)
        )
    )
    draft = await made_from(db, cut)
    offered: list[cut_editor.Candidate] = []
    if draft is not None:
        for row in draft.candidates or []:
            try:
                offered.append(cut_editor.Candidate.from_json(row))
            except (KeyError, TypeError, ValueError):
                continue  # a row written by another version: left out, not a failure

    title = _flat(cut.title) or roughcut.DEFAULT_TITLE
    head = [
        REVIEW_OPENS,
        "",
        f'Cut: "{title}", version {cut.version}, {_made_by(cut, draft)}. Cut id: {cut.id}.',
    ]
    length = f"It is {_s(cut.duration_s)} s in {_plural(len(pieces), 'shot')}."
    if draft is not None:
        head.append(f"Brief: {_flat(draft.brief)[:_BRIEF_KEPT]}")
        head.append(
            f"Asked for: about {_s(draft.target_s)} s, shots of {_s(draft.min_shot_s)} to "
            f"{_s(draft.max_shot_s)} s. {length}"
        )
        problems = _flat(" ".join(draft.problems or []))
        if problems:
            if len(problems) > _PROBLEMS_KEPT:
                problems = problems[: _PROBLEMS_KEPT - 1] + "…"
            head.append(f"Changed when it was checked: {problems}")
    else:
        head.append(length)
    closing = (
        "Look before you judge: contact_sheet takes these lines as they are. Say what you would "
        "change and why. If you build a better version, call build_rough_cut with "
        f'parent="{cut.id}" so it is kept beside this one as the next version. '
        'A shot marked "used" may be marked '
        "because of this very cut. Do not ask the local Editor again unless I ask you to."
    )
    cut_lines = [_piece_line(n, piece) for n, piece in enumerate(pieces, start=1)]
    offer_lines = [_flat(candidate.claude_line()) for candidate in offered]

    def written(shots: int, chosen_from: int) -> str:
        if shots == len(cut_lines):
            cut_head = f"The cut, {_CUT_IS}:"
        else:
            cut_head = f"The first {shots} of the cut's {len(cut_lines)} shots, {_CUT_IS}:"
        if not offer_lines:
            listed = [NOT_RECORDED]
        elif chosen_from == len(offer_lines):
            listed = [f"The shots it was chosen from {_LIST_IS}:", *offer_lines]
        elif chosen_from == 0:
            listed = [
                f"The {len(offer_lines)} shots it was chosen from are left out to keep this "
                "message short; find_shots lists them."
            ]
        else:
            listed = [
                f"The first {chosen_from} of {len(offer_lines)} shots it was chosen from "
                f"{_LIST_IS}:",
                *offer_lines[:chosen_from],
            ]
        return "\n".join([*head, "", cut_head, *cut_lines[:shots], "", *listed, "", closing])

    shots, chosen_from = len(cut_lines), len(offer_lines)
    text = written(shots, chosen_from)
    while len(text) > limit and chosen_from > 0:
        chosen_from -= 1
        text = written(shots, chosen_from)
    while len(text) > limit and shots > 0:
        shots -= 1
        text = written(shots, chosen_from)

    label = f"{SOURCE_LABELS.get(cut.source, cut.source)}, version {cut.version} — "
    whole = shots == len(cut_lines)
    label += _plural(shots, "shot") if whole else f"{shots} of {len(cut_lines)} shots"
    if offer_lines and chosen_from == len(offer_lines):
        label += f", and the {chosen_from} it was chosen from"
    elif chosen_from:
        label += f", and {chosen_from} of the {len(offer_lines)} it was chosen from"
    return ReviewRequest(text=text, label=label, shots=shots, candidates=chosen_from)


jobs.register(KIND, run_draft, needs_model=True, title=_title, abandoned=_abandoned)
# What the demo's model, and the tests' fake, answer for the Editor: a real,
# checkable cut taken evenly from the list. Read only by the fake (L1 3.17).
fake.FakeModelServer.answer_for(cut_editor.EditorAnswer.__name__, cut_editor.demo_answer)
