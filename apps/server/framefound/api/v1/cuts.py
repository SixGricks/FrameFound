"""Cuts: cut history, and drafts asked of the local Editor (design L5 section 6).

Cut history has been written since migration 0028 (Claude's cuts through the
connector, imported timelines) and since L5 also holds local drafts; this is
the first page that reads it. Part one (L5 S6) is the list and drafting:

- `GET /cuts` is everything the list page draws in one request: whether the
  Editor can draft here and when it would run, the drafts that are not a cut
  (yet), and one line per family of versions.
- `GET /cuts/folders` is the form's folder picker: folders that hold video.
- `POST /cuts/drafts` asks for a draft and answers at once (202); the work is
  a job on the local-AI lane (`nle/drafts.py`), and the page reads how it is
  getting on from `GET /cuts/drafts/{id}`. A model that only fits overnight
  is not refused: the draft is accepted and says until when it waits.
- Cancel, and clearing a failed or cancelled draft from the list.

Part two (L5 S7) is one cut:

- `GET /cuts/{id}` is a cut's whole family (any version's id finds it), each
  version with its pieces, the reasons given for them, which build drafted
  it, and the draft it was made from with the shots it was chosen from.
- `GET /cuts/{id}/pieces/{position}/still` is the picture for one piece,
  from what is already on disk; 404 when there is none (never 500).
- `POST /cuts/{id}/send` is the Premiere file, rebuilt from history for the
  computer named; the first download marks the cut sent, which is when a
  local draft starts to count as footage that was used (D4).
- `GET /cuts/{id}/review-request` is the message that asks Claude to review
  the cut, written here and passed on by the page unchanged.
- `DELETE /cuts/{id}` deletes one version, for its maker or an administrator.

Nothing here calls a model, and nothing imports `framefound.mcp` (the
connector is an optional extra; a test hides it and imports this module).
Reading is for everyone signed in; a read-only account is refused every
POST and DELETE by `auth/deps.py`. Drafting is for anyone who can change
things (D11); cancelling or clearing a draft is for the person who asked for
it, or an administrator; deleting a cut is for whoever made it, or an
administrator. No answer carries a server path, except the Premiere file,
whose whole point is the paths: a folder is a path inside its library, as
Browse shows it. Every refusal is the sentence the page shows, in the one
error envelope: 400 the request cannot be served as asked, 403 not yours,
404 no such cut, draft, library or picture, 409 not possible as things stand
(including drafting where models cannot run), 503 the queue is down.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

# Imported for what importing it does: the chat panel gains `local_cut_draft`
# where models can run, so the tool is there whenever the API is (L5 3.5).
import framefound.nle.draft_chat  # noqa: F401 - registers the chat panel's tool
from framefound.ai import cut_editor
from framefound.api.v1.local_models import DepsDep
from framefound.auth import service as auth_service
from framefound.auth.deps import CurrentUser, DbDep, client_ip
from framefound.config import Settings
from framefound.db.models import (
    Asset,
    Cut,
    CutDraft,
    CutPiece,
    Library,
    ShotWindow,
    User,
    catalogued,
)
from framefound.local_models import availability, jobs
from framefound.local_models.kinds import is_demo_runtime
from framefound.media import folders as folders_lib
from framefound.nle import drafts, roughcut, shotlist

# The lane's task module brings the queue's client for the wake-up, and with
# it every kind of job (`draft_cut` among them, registered by nle.drafts).
from framefound.processing import local_ai_tasks

log = structlog.get_logger()
router = APIRouter(prefix="/cuts", tags=["cuts"])

NO_SUCH_DRAFT = "No such draft."
NOT_YOUR_DRAFT = "Only the person who asked for this draft, or an administrator, can change it."
CANCEL_FIRST = "Cancel it first."
DRAFT_IS_A_CUT = "This draft became a cut. Delete the cut instead."
NO_SUCH_CUT = drafts.NO_CUT
NOT_YOUR_CUT = "Only the person who made this cut, or an administrator, can delete it."
NO_PICTURE = "There is no picture for this shot yet."
# A piece's picture does not change once it is on disk; the page shows a cut's
# every piece, so it is kept a while (private: it is someone's footage).
STILL_CACHE = "private, max-age=3600"

# Drafts that are not a cut, listed at most: the active ones always, then the
# newest failed and cancelled ones.
DRAFTS_LISTED = 20
# Video paths read to group into folders (as /listings/folders does for
# photographs): grouping in Python is the one way that reads "the folder of a
# path" the same on PostgreSQL and SQLite.
FOLDER_SCAN = 20_000
# Footage holding more shots than the shot search reads (`shotlist.SCAN_LIMIT`):
# the search would stop at that many in no particular order, and the Editor
# would be offered an arbitrary part of it, a different part each time (P22).
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
# What each source is called on the page.
LABELS = drafts.SOURCE_LABELS


# --- the outside world ----------------------------------------------------------------


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
    the API restarts, and a draft left queued or waiting would read "Waiting
    its turn." for ever. Every read of a page that shows drafts starts it
    again (as the AI models page does); `keep_going` does so at most once a
    tick however often the page asks."""
    if availability(settings).mode == "demo":
        # Imported only where demo mode is on.
        from framefound.local_models import demo

        demo.keep_going(settings)


# --- what the page is sent ------------------------------------------------------------


class _Out(BaseModel):
    # Several fields are called model_..., which is what they are.
    model_config = ConfigDict(protected_namespaces=())


class EditorOut(_Out):
    """Whether the Editor can draft here, and when it would run."""

    mode: Literal["ready", "demo", "unavailable"]
    note: str | None
    model_id: uuid.UUID | None
    model_name: str | None
    runs: Literal["now", "overnight", "never"] | None
    # This Mac's wall clock, naive: when an overnight-only Editor starts.
    starts_at: datetime | None
    paused: bool
    why_not: str | None


class ScopeOut(_Out):
    kind: Literal["folder", "selection"]
    library_id: uuid.UUID | None = None
    folder: str = ""
    contains: str = ""
    asset_ids: list[uuid.UUID] = []
    label: str = ""


class DraftOut(_Out):
    id: uuid.UUID
    # queued | waiting | running | done | failed | cancelled, as read through the job.
    state: str
    # Cancel was pressed while the Editor was answering; it ends when the answer comes.
    stopping: bool
    message: str
    title: str
    brief: str
    scope: ScopeOut
    target_s: float
    min_shot_s: float
    max_shot_s: float
    exclude_used: bool
    shots_offered: int
    shots_in_scope: int
    clips: int
    cut_id: uuid.UUID | None
    revises_cut_id: uuid.UUID | None
    origin: str
    problems: list[str]
    # What the Editor wrote, only on a failed draft.
    answer_text: str | None
    model_name: str | None
    demo: bool
    seconds: float | None
    requested_by: str | None
    can_cancel: bool
    can_clear: bool
    created_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class VersionOut(_Out):
    id: uuid.UUID
    version: int
    source: str
    label: str
    duration_s: float
    piece_count: int
    created_at: datetime | None
    demo: bool


class FamilyOut(_Out):
    id: uuid.UUID  # the newest version's
    root_id: uuid.UUID  # the family's
    title: str
    client: str
    versions: int
    newest: VersionOut
    has_local_draft: bool
    sent: bool


class CutsOut(_Out):
    editor: EditorOut
    can_draft: bool
    drafts: list[DraftOut]
    cuts: list[FamilyOut]
    # More families come after this page: ask again with `offset` moved on.
    more: bool = False


class FolderOut(_Out):
    library_id: uuid.UUID
    library_name: str
    path: str  # inside the library, as Browse shows it
    clips: int
    analysed: int  # clips that have been analysed into shots
    newest: datetime | None


class ShotOut(_Out):
    """The shot a piece came from, as it was described when the cut was made."""

    move: str | None
    direction: str | None
    light: str | None
    sky: str | None
    subjects: list[str]
    smooth: float | None
    energy: str | None


class PieceOut(_Out):
    position: int
    # Null once the clip has been deleted from the catalogue.
    asset_id: uuid.UUID | None
    filename: str
    clip: str  # DJI_0639
    media_type: str  # "video" when the clip is gone
    # False when the clip has been deleted or is not on the share now.
    available: bool
    in_s: float
    out_s: float
    length_s: float
    # Why it is there. A local model's words: shown only as text.
    note: str
    shot: ShotOut | None
    still_url: str | None


class CandidateOut(_Out):
    """A shot the Editor was offered."""

    n: int
    asset_id: uuid.UUID
    clip: str
    start_s: float
    end_s: float
    line: str  # what find_shots prints after the clip's id and name
    chosen: bool  # a piece of this version came from it


class MadeFromOut(_Out):
    """The draft a version was made from."""

    id: uuid.UUID
    brief: str
    scope: ScopeOut
    target_s: float
    min_shot_s: float
    max_shot_s: float
    shots_offered: int
    shots_in_scope: int
    clips: int
    seconds: float | None
    problems: list[str]
    candidates: list[CandidateOut]


class ProvenanceOut(_Out):
    """The local build that drafted a version."""

    model: str
    model_name: str
    model_revision: str | None
    revision_short: str | None
    variant: str
    runtime: str | None
    runtime_version: str | None
    prompt_version: str | None


class CutVersionOut(_Out):
    id: uuid.UUID
    version: int
    # The version it revises; it may have been deleted since.
    parent_id: uuid.UUID | None
    source: str
    label: str
    demo: bool
    title: str
    duration_s: float
    piece_count: int
    created_at: datetime | None
    created_by: str | None
    sent_at: datetime | None
    can_delete: bool
    provenance: ProvenanceOut | None  # null for a cut no local model made
    draft: MadeFromOut | None  # null for a version that did not come from a draft
    pieces: list[PieceOut]


class CutOut(_Out):
    id: uuid.UUID  # the version asked for
    root_id: uuid.UUID
    title: str  # the newest version's
    client: str
    editor: EditorOut
    can_draft: bool
    # Drafts asked to revise a version of this cut that are not a version yet:
    # waiting, running or failed.
    drafts: list[DraftOut]
    versions: list[CutVersionOut]  # oldest first


class SendIn(BaseModel):
    profile: str = Field(default="", max_length=100)  # "" for none


class SendOut(_Out):
    filename: str
    xml: str
    profile: str | None
    duration_s: float
    cuts: int
    unmapped: list[str]  # no path for this computer: they import offline
    missing: list[str]  # deleted from the catalogue: left out
    offline: list[str]  # not on the share now: written, relink once it is back
    note: str
    sent_at: datetime | None


class ReviewOut(_Out):
    cut_id: uuid.UUID
    label: str
    shots: int
    candidates: int
    chars: int
    text: str


# --- small things ---------------------------------------------------------------------


def _utc(moment: datetime | None) -> datetime | None:
    """A moment read from the table, in UTC: SQLite hands back what was
    written without its zone, and everything here is written in UTC."""
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _admin(user: User) -> bool:
    return user.role == "admin"


def _may_change(user: User, draft: CutDraft) -> bool:
    if user.role == "readonly":
        return False
    return _admin(user) or (draft.requested_by is not None and draft.requested_by == user.id)


async def _emails(db: AsyncSession, ids: Iterable[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {each for each in ids if each is not None}
    if not wanted:
        return {}
    rows = await db.execute(select(User.id, User.email).where(User.id.in_(wanted)))
    return {user_id: email for user_id, email in rows.all()}


def _editor_out(status: drafts.EditorStatus) -> EditorOut:
    return EditorOut(
        mode=status.mode,
        note=status.note,
        model_id=status.model_id,
        model_name=status.model_name,
        runs=status.runs,
        starts_at=status.starts_at,
        paused=status.paused,
        why_not=status.why_not,
    )


def _scope_out(raw: Any) -> ScopeOut:
    scope = raw if isinstance(raw, dict) else {}
    try:
        return ScopeOut.model_validate(scope)
    except ValueError:
        # A row written by another version: the label is what the page needs.
        return ScopeOut(kind="folder", label=str(scope.get("label", "")))


async def _draft_out(
    db: AsyncSession,
    draft: CutDraft,
    user: User,
    *,
    emails: dict[uuid.UUID, str] | None = None,
    now: datetime | None = None,
) -> DraftOut:
    """A draft as the page reads it: an active one through its job (`waiting`
    and its sentence live only there)."""
    job = await drafts.job_of(db, draft)
    state, message, stopping = drafts.shown(draft, job, now)
    if emails is None:
        emails = await _emails(db, [draft.requested_by])
    allowed = _may_change(user, draft)
    active = state in drafts.ACTIVE
    return DraftOut(
        id=draft.id,
        state=state,
        stopping=stopping,
        message=message,
        title=draft.title,
        brief=draft.brief,
        scope=_scope_out(draft.scope),
        target_s=draft.target_s,
        min_shot_s=draft.min_shot_s,
        max_shot_s=draft.max_shot_s,
        exclude_used=draft.exclude_used,
        shots_offered=len(draft.candidates or []),
        shots_in_scope=draft.shots_in_scope,
        clips=draft.clips,
        cut_id=draft.cut_id,
        revises_cut_id=draft.revises_cut_id,
        origin=draft.origin,
        problems=list(draft.problems or []),
        answer_text=draft.answer_text if state == "failed" else None,
        model_name=draft.model.rsplit("/", 1)[-1] if draft.model else None,
        demo=draft.demo,
        seconds=draft.seconds,
        requested_by=emails.get(draft.requested_by) if draft.requested_by else None,
        can_cancel=allowed and active and not stopping,
        can_clear=allowed and state in ("failed", "cancelled"),
        created_at=_utc(draft.created_at),
        started_at=_utc(draft.started_at),
        finished_at=_utc(draft.finished_at),
    )


async def _draft(db: AsyncSession, draft_id: uuid.UUID) -> CutDraft:
    draft = await db.get(CutDraft, draft_id, populate_existing=True)
    if draft is None:
        raise HTTPException(404, NO_SUCH_DRAFT)
    return draft


# --- the list -------------------------------------------------------------------------


async def _listed_drafts(
    db: AsyncSession, user: User, library_id: uuid.UUID | None, now: datetime
) -> list[DraftOut]:
    """Every draft that is not a cut: active ones first, then failed and
    cancelled, newest first, at most DRAFTS_LISTED. A draft whose worker died
    is stored active and read as failed, so it sorts with the failed ones."""
    newest = CutDraft.created_at.desc()
    active = list(
        await db.scalars(select(CutDraft).where(CutDraft.state.in_(("queued", "running"))))
    )
    ended = list(
        await db.scalars(
            select(CutDraft)
            .where(CutDraft.state.in_(("failed", "cancelled")))
            .order_by(newest, CutDraft.id)
            .limit(DRAFTS_LISTED * 5 if library_id is not None else DRAFTS_LISTED)
        )
    )
    rows = [*active, *ended]
    if library_id is not None:
        # Only a folder draft names a library; clips ticked in Search may span several.
        rows = [row for row in rows if str((row.scope or {}).get("library_id")) == str(library_id)]
    emails = await _emails(db, [row.requested_by for row in rows])
    shown = [await _draft_out(db, row, user, emails=emails, now=now) for row in rows]
    epoch = datetime.min.replace(tzinfo=UTC)
    shown.sort(key=lambda d: d.created_at or epoch, reverse=True)
    shown.sort(key=lambda d: d.state not in drafts.ACTIVE)
    return shown[:DRAFTS_LISTED]


def _demo_cuts(cuts: Sequence[Cut], demo_drafted: set[uuid.UUID]) -> set[uuid.UUID]:
    # `drafts.demonstration`'s rule, for many cuts at once: `demo_drafted`
    # holds the cuts whose draft says its answer was made up.
    return {cut.id for cut in cuts if is_demo_runtime(cut.runtime or "") or cut.id in demo_drafted}


async def _families(
    db: AsyncSession, limit: int, library_id: uuid.UUID | None, offset: int = 0
) -> tuple[list[FamilyOut], bool]:
    """One line per family of versions, newest activity first: `limit` of
    them after the first `offset`, and whether any come after those."""
    family = func.coalesce(Cut.root_id, Cut.id)
    latest = func.max(Cut.created_at)
    # One more than asked for, to know whether there is another page. The
    # family breaks a tie in time, so the pages neither repeat nor skip one.
    query = (
        select(family, latest)
        .group_by(family)
        .order_by(latest.desc(), family)
        .offset(offset)
        .limit(limit + 1)
    )
    if library_id is not None:
        query = query.where(Cut.library_id == library_id)
    order = [root for root, _when in (await db.execute(query)).all()]
    more = len(order) > limit
    order = order[:limit]
    if not order:
        return [], more
    members = list(
        await db.scalars(
            select(Cut)
            .where(or_(Cut.id.in_(order), Cut.root_id.in_(order)))
            .order_by(Cut.version, Cut.created_at, Cut.id)
        )
    )
    demo_drafted = set(
        await db.scalars(
            select(CutDraft.cut_id).where(
                CutDraft.cut_id.in_([cut.id for cut in members]), CutDraft.demo.is_(True)
            )
        )
    )
    demo = _demo_cuts(members, {each for each in demo_drafted if each is not None})
    grouped: dict[uuid.UUID, list[Cut]] = {}
    for cut in members:
        grouped.setdefault(cut.root_id or cut.id, []).append(cut)
    out: list[FamilyOut] = []
    for root in order:
        versions = grouped.get(root)
        if not versions:
            continue
        newest = versions[-1]
        out.append(
            FamilyOut(
                id=newest.id,
                root_id=root,
                title=newest.title,
                client=newest.client,
                versions=len(versions),
                newest=VersionOut(
                    id=newest.id,
                    version=newest.version,
                    source=newest.source,
                    label=LABELS.get(newest.source, newest.source),
                    duration_s=newest.duration_s,
                    piece_count=newest.piece_count,
                    created_at=_utc(newest.created_at),
                    demo=newest.id in demo,
                ),
                has_local_draft=any(cut.source == "local" for cut in versions),
                sent=any(cut.sent_at is not None for cut in versions),
            )
        )
    return out, more


@router.get("", response_model=CutsOut)
async def list_cuts(
    user: CurrentUser,
    db: DbDep,
    deps: DepsDep,
    limit: int = Query(default=30, ge=1, le=200),
    library_id: uuid.UUID | None = None,
    offset: int = Query(default=0, ge=0, le=1_000_000),
) -> CutsOut:
    """Everything the list page draws: the Editor, the drafts that are not a
    cut, and one line per family of versions. Works where models cannot run:
    the cuts still list, and `editor.why_not` says why there is no form.

    The families come `limit` at a time: `offset` skips that many, and `more`
    says whether any come after this page (the list stopped at 200 with no
    way on). The drafts are the same on every page."""
    _demo_beat(deps.settings)
    status = await drafts.editor_status(db, now=deps.now(), settings=deps.settings)
    now = datetime.now(UTC)
    families, more = await _families(db, limit, library_id, offset)
    return CutsOut(
        editor=_editor_out(status),
        can_draft=status.why_not is None and user.role != "readonly",
        drafts=await _listed_drafts(db, user, library_id, now),
        cuts=families,
        more=more,
    )


# --- the folder picker ----------------------------------------------------------------


def _like_literal(value: str) -> str:
    """`value` for a LIKE pattern, its own wildcards escaped with a backslash."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _online_video() -> tuple[Any, ...]:
    """The clips a draft can be made from: online, catalogued video, as
    `shotlist.scan_shots` reads them."""
    return (Asset.media_type == "video", Asset.availability == "online", catalogued())


async def _shots_past_bound(db: AsyncSession, *where: Any) -> bool:
    """Whether the clips matching `where` hold more shot windows than one
    shot search reads (`shotlist.SCAN_LIMIT`, read when asked). Counting
    stops just past the bound: a whole library is not counted to the last
    shot to learn that it is too much."""
    most = shotlist.SCAN_LIMIT
    windows = (
        select(ShotWindow.id)
        .join(Asset, Asset.id == ShotWindow.asset_id)
        .where(*_online_video(), *where)
        .limit(most + 1)
        .subquery()
    )
    return int(await db.scalar(select(func.count()).select_from(windows)) or 0) > most


async def _too_many_shots(db: AsyncSession, scope: drafts.Scope) -> str | None:
    """The sentence when the footage asked for holds more shots than a draft
    can read, else None. Without it the shot search stops at its bound in no
    particular order and the Editor is offered an arbitrary part of the
    footage while the form counted all of it. Counted as the search reads it
    (`drafts.candidates_for`), before its filters in Python; a scope that is
    not well formed is left for `candidates_for` to refuse in its own words."""
    most = shotlist.SCAN_LIMIT
    if scope.kind == "selection":
        ids = list(dict.fromkeys(scope.asset_ids))
        if ids and await _shots_past_bound(db, Asset.id.in_(ids)):
            return TOO_MANY_SHOTS_SELECTION.format(most=most)
        return None
    where: list[Any] = []
    if scope.library_id is not None:
        where.append(Asset.library_id == scope.library_id)
    contains = scope.contains.strip()
    if scope.folder:
        try:
            where.extend(folders_lib.under(folders_lib.clean_folder(scope.folder)))
        except folders_lib.FolderError:
            return None
        sentence = TOO_MANY_SHOTS_FOLDER.format(most=most)
    elif contains:
        where.append(func.lower(Asset.relative_path).contains(contains.lower(), autoescape=True))
        sentence = TOO_MANY_SHOTS_MATCHING.format(contains=contains, most=most)
    else:
        return None
    return sentence if await _shots_past_bound(db, *where) else None


@router.get("/folders", response_model=list[FolderOut])
async def list_folders(
    _user: CurrentUser,
    db: DbDep,
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
) -> list[FolderOut]:
    """Folders that hold video, directly or in a folder inside them, for the
    draft form's picker. With `q`, the folders whose path holds it (any case),
    most clips first; without, the folders with the most recently shot video,
    a folder before the one it sits in when their newest clip is the same.

    `clips` and `analysed` count everything beneath the folder, because a
    folder draft takes everything beneath it (`folders.under` in nle/drafts):
    counting only a folder's own clips made the form's readiness line
    undercount a folder with sub-folders, and left a folder holding only
    sub-folders out of the picker. `analysed` counts the clips that have
    shots, which is what a draft is made from. A clip at the top of a library
    has no folder and is not offered: a draft is never "the whole library"
    (P22). Nor is a folder whose clips hold more shots than one draft reads
    (`shotlist.SCAN_LIMIT`): counting everything beneath made every top
    folder of a library laid out client/year/shoot a candidate, and a draft
    of one would read an arbitrary part of it. Its sub-folders are offered."""
    shot = exists(select(ShotWindow.id).where(ShotWindow.asset_id == Asset.id))
    windows = (
        select(func.count(ShotWindow.id))
        .where(ShotWindow.asset_id == Asset.id)
        .correlate(Asset)
        .scalar_subquery()
    )
    when = func.coalesce(Asset.captured_at, Asset.mtime)
    video = _online_video()
    most = shotlist.SCAN_LIMIT
    query = (
        select(Asset.library_id, Asset.relative_path, when, windows)
        .where(*video)
        .order_by(when.desc())
        .limit(FOLDER_SCAN)
    )
    wanted = " ".join(q.split()).lower()
    if wanted:
        # Every clip beneath a folder whose path holds the words has them in
        # its own path too, so this narrowing never loses one of its clips.
        query = query.where(Asset.relative_path.ilike(f"%{_like_literal(wanted)}%", escape="\\"))
    scanned = (await db.execute(query)).all()
    # Per folder: clips, analysed clips, the newest clip, shot windows.
    groups: dict[tuple[uuid.UUID, str], list[Any]] = {}
    for library_id, relative_path, shot_at, shots in scanned:
        parts = relative_path.rpartition("/")[0].split("/") if "/" in relative_path else []
        moment = _utc(shot_at)
        # The clip's own folder and every folder it sits in.
        for depth in range(1, len(parts) + 1):
            folder = "/".join(parts[:depth])
            # Matched on the folder, not the file name: "5096" finds the shoot's folder.
            if wanted and wanted not in folder.lower():
                continue
            entry = groups.setdefault((library_id, folder), [0, 0, None, 0])
            entry[0] += 1
            entry[1] += 1 if shots else 0
            entry[3] += int(shots or 0)
            if moment is not None and (entry[2] is None or moment > entry[2]):
                entry[2] = moment
    # Too much for one draft. A scan cut short only ever undercounts, so a
    # folder past the bound here is past it in full.
    fitting = [kv for kv in groups.items() if kv[1][3] <= most]
    epoch = datetime.min.replace(tzinfo=UTC)
    if wanted:
        ranked = sorted(fitting, key=lambda kv: (-kv[1][0], kv[0][1]))
    else:
        # On the same newest clip, the deeper folder (the shoot) before the
        # folders it sits in (the year, the client).
        ranked = sorted(
            fitting,
            key=lambda kv: (kv[1][2] or epoch, kv[0][1].count("/"), kv[0][1]),
            reverse=True,
        )
    ranked = ranked[:limit]
    if len(scanned) >= FOLDER_SCAN:
        # The scan stopped at its bound, so a folder's clips beyond it were not
        # counted: count the folders shown exactly (at most `limit` of them),
        # leave out any that turns out to hold too many shots for one draft,
        # and rank them again on what they really hold.
        exact: list[tuple[tuple[uuid.UUID, str], list[Any]]] = []
        for (library_id, folder), entry in ranked:
            try:
                beneath = folders_lib.under(folder)
            except folders_lib.FolderError:
                continue
            place = (Asset.library_id == library_id, *beneath)
            if await _shots_past_bound(db, *place):
                continue
            counted = (
                await db.execute(
                    select(func.count(Asset.id), func.count(Asset.id).filter(shot)).where(
                        *video, *place
                    )
                )
            ).one()
            entry[0], entry[1] = int(counted[0]), int(counted[1])
            exact.append(((library_id, folder), entry))
        ranked = exact
        if wanted:
            ranked.sort(key=lambda kv: (-kv[1][0], kv[0][1]))
    names = {
        library.id: library.name
        for library in await db.scalars(
            select(Library).where(Library.id.in_({key[0] for key, _ in ranked}))
        )
    }
    return [
        FolderOut(
            library_id=library_id,
            library_name=names.get(library_id, ""),
            path=folder,
            clips=clips,
            analysed=analysed,
            newest=newest,
        )
        for (library_id, folder), (clips, analysed, newest, _shots) in ranked
    ]


# --- drafts ---------------------------------------------------------------------------


@router.post("/drafts", status_code=202, response_model=DraftOut)
async def ask_for_draft(
    body: drafts.DraftRequest, user: CurrentUser, db: DbDep, deps: DepsDep, wake: WakeDep
) -> DraftOut:
    """Ask the Editor for a draft. Answers at once: the draft runs on the
    local-AI lane and the page follows it at `GET /cuts/drafts/{id}`. A model
    that only fits overnight is not refused; the draft waits and says until
    when.

    Footage holding more shots than one draft can read is refused with a
    sentence that says to choose less (a link or Try again can name such a
    folder, though the picker never offers one)."""
    too_many = await _too_many_shots(db, body.scope)
    if too_many is not None:
        raise HTTPException(400, too_many)
    try:
        draft = await drafts.request_draft(
            db,
            body,
            user_id=user.id,
            origin="web",
            wake=wake,
            now=deps.now(),
            settings=deps.settings,
        )
    except drafts.DraftError as err:
        raise HTTPException(err.status, str(err)) from None
    return await _draft_out(db, await _draft(db, draft.id), user)


@router.get("/drafts/{draft_id}", response_model=DraftOut)
async def read_draft(draft_id: uuid.UUID, user: CurrentUser, db: DbDep, deps: DepsDep) -> DraftOut:
    """One draft as it stands; the page asks again while it is active."""
    _demo_beat(deps.settings)
    return await _draft_out(db, await _draft(db, draft_id), user)


@router.post("/drafts/{draft_id}/cancel", response_model=DraftOut)
async def cancel_draft(
    draft_id: uuid.UUID, user: CurrentUser, db: DbDep, request: Request
) -> DraftOut:
    """Cancel. A draft that has not started ends at once; one the Editor is
    answering reads `stopping` until the answer comes back and is thrown away
    (one request to the model cannot be cut short)."""
    draft = await _draft(db, draft_id)
    if not _may_change(user, draft):
        raise HTTPException(403, NOT_YOUR_DRAFT)
    try:
        draft = await drafts.cancel(db, draft)
    except drafts.DraftError as err:
        raise HTTPException(err.status, str(err)) from None
    if draft.requested_by != user.id:
        # An administrator stopping someone else's draft.
        await auth_service.audit(
            db,
            "cut_drafts.cancelled",
            actor_user_id=user.id,
            ip=client_ip(request),
            detail={"draft_id": str(draft.id), "title": draft.title},
        )
    await db.commit()
    log.info("cut_drafts.cancel", draft_id=str(draft.id), by=str(user.id))
    return await _draft_out(db, await _draft(db, draft_id), user)


@router.delete("/drafts/{draft_id}", status_code=204)
async def clear_draft(
    draft_id: uuid.UUID, user: CurrentUser, db: DbDep, request: Request
) -> Response:
    """Clear a failed or cancelled draft from the list. An active one must be
    cancelled first; a finished one is part of its cut."""
    draft = await _draft(db, draft_id)
    if not _may_change(user, draft):
        raise HTTPException(403, NOT_YOUR_DRAFT)
    job = await drafts.job_of(db, draft)
    state, _message, _stopping = drafts.shown(draft, job)
    if state in drafts.ACTIVE:
        raise HTTPException(409, CANCEL_FIRST)
    if state == "done":
        raise HTTPException(409, DRAFT_IS_A_CUT)
    if job is not None and await jobs.reap(db, job):
        # Its worker died: ended the way the lane's next tick would, so the
        # job does not read as running once the draft is gone.
        log.info("cut_drafts.reaped", draft_id=str(draft.id), job_id=str(job.id))
    if draft.requested_by != user.id:
        # An administrator clearing someone else's draft.
        await auth_service.audit(
            db,
            "cut_drafts.cleared",
            actor_user_id=user.id,
            ip=client_ip(request),
            detail={"draft_id": str(draft.id), "title": draft.title},
        )
    await db.delete(draft)
    await db.commit()
    log.info("cut_drafts.cleared", draft_id=str(draft_id), by=str(user.id))
    return Response(status_code=204)


# --- one cut --------------------------------------------------------------------------
# Declared after /folders and /drafts…: a path is matched in the order the
# routes were declared, and "folders" must never be read as a cut's id.


async def _cut(db: AsyncSession, cut_id: uuid.UUID) -> Cut:
    cut = await db.get(Cut, cut_id, populate_existing=True)
    if cut is None:
        raise HTTPException(404, NO_SUCH_CUT)
    return cut


def _may_delete(user: User, cut: Cut) -> bool:
    if user.role == "readonly":
        return False
    return _admin(user) or (cut.created_by is not None and cut.created_by == user.id)


def _shot_out(snapshot: Any) -> ShotOut | None:
    shot = snapshot if isinstance(snapshot, dict) else {}
    if not shot:
        return None
    smooth = shot.get("smooth")
    return ShotOut(
        move=shot.get("move"),
        direction=shot.get("direction"),
        light=shot.get("light"),
        sky=shot.get("sky"),
        subjects=[str(each) for each in shot.get("subjects") or []],
        smooth=float(smooth) if isinstance(smooth, int | float) else None,
        energy=shot.get("energy"),
    )


def _piece_out(cut: Cut, piece: CutPiece, assets: dict[uuid.UUID, Any]) -> PieceOut:
    found = assets.get(piece.asset_id) if piece.asset_id is not None else None
    in_s, out_s = piece.in_ms / 1000, piece.out_ms / 1000
    speed = piece.speed if piece.speed and piece.speed > 0 else 1.0
    return PieceOut(
        position=piece.position,
        asset_id=piece.asset_id if found is not None else None,
        filename=piece.filename,
        clip=shotlist.clip_name(piece.filename),
        media_type=found.media_type if found is not None else "video",
        available=found is not None and found.availability == "online",
        in_s=in_s,
        out_s=out_s,
        length_s=round((out_s - in_s) / speed, 3),
        note=piece.note,
        shot=_shot_out(piece.shot),
        still_url=(
            f"/api/v1/cuts/{cut.id}/pieces/{piece.position}/still" if found is not None else None
        ),
    )


def _chosen(candidates: list[cut_editor.Candidate], pieces: list[CutPiece]) -> set[int]:
    """The offered shots a version's pieces came from: for each piece, the
    offered shot of its clip it overlaps most (pieces lie inside their shot
    once checked, but two shots of a clip touch at their ends)."""
    chosen: set[int] = set()
    for piece in pieces:
        p0, p1 = piece.in_ms / 1000, piece.out_ms / 1000
        best, overlap = None, 0.0
        for candidate in candidates:
            if candidate.asset_id != piece.asset_id:
                continue
            found = min(p1, candidate.end_s) - max(p0, candidate.start_s)
            if found > overlap:
                best, overlap = candidate.n, found
        if best is not None:
            chosen.add(best)
    return chosen


def _made_from_out(draft: CutDraft, pieces: list[CutPiece]) -> MadeFromOut:
    candidates: list[cut_editor.Candidate] = []
    for row in draft.candidates or []:
        try:
            candidates.append(cut_editor.Candidate.from_json(row))
        except (KeyError, TypeError, ValueError):
            continue  # a row written by another version
    chosen = _chosen(candidates, pieces)
    return MadeFromOut(
        id=draft.id,
        brief=draft.brief,
        scope=_scope_out(draft.scope),
        target_s=draft.target_s,
        min_shot_s=draft.min_shot_s,
        max_shot_s=draft.max_shot_s,
        shots_offered=len(candidates),
        shots_in_scope=draft.shots_in_scope,
        clips=draft.clips,
        seconds=draft.seconds,
        problems=list(draft.problems or []),
        candidates=[
            CandidateOut(
                n=c.n,
                asset_id=c.asset_id,
                clip=shotlist.clip_name(c.filename),
                start_s=c.start_s,
                end_s=c.end_s,
                line=c.line,
                chosen=c.n in chosen,
            )
            for c in candidates
        ],
    )


def _provenance_out(cut: Cut) -> ProvenanceOut | None:
    if not cut.model:
        return None
    return ProvenanceOut(
        model=cut.model,
        model_name=cut.model.rsplit("/", 1)[-1],
        model_revision=cut.model_revision,
        revision_short=cut.model_revision[:7] if cut.model_revision else None,
        variant=cut.variant or "",
        runtime=cut.runtime,
        runtime_version=cut.runtime_version,
        prompt_version=cut.prompt_version,
    )


async def _family_drafts(
    db: AsyncSession, user: User, versions: list[Cut], now: datetime
) -> list[DraftOut]:
    """Drafts asked to revise a version of this family that are not a
    version yet: active first, then failed, newest first."""
    rows = list(
        await db.scalars(
            select(CutDraft).where(
                CutDraft.revises_cut_id.in_([cut.id for cut in versions]),
                CutDraft.state.in_(("queued", "running", "failed")),
            )
        )
    )
    emails = await _emails(db, [row.requested_by for row in rows])
    shown = [await _draft_out(db, row, user, emails=emails, now=now) for row in rows]
    shown = [each for each in shown if each.state in drafts.ACTIVE or each.state == "failed"]
    epoch = datetime.min.replace(tzinfo=UTC)
    shown.sort(key=lambda d: d.created_at or epoch, reverse=True)
    shown.sort(key=lambda d: d.state not in drafts.ACTIVE)
    return shown[:DRAFTS_LISTED]


@router.get("/{cut_id}", response_model=CutOut)
async def read_cut(cut_id: uuid.UUID, user: CurrentUser, db: DbDep, deps: DepsDep) -> CutOut:
    """A cut and every version of it (any version's id finds the family):
    each version's pieces with their reasons and pictures, which local build
    drafted it, and the draft it was made from with the shots it was offered.
    A clip deleted since is still listed, as not available."""
    _demo_beat(deps.settings)
    cut = await _cut(db, cut_id)
    versions = await shotlist.family(db, cut)
    ids = [version.id for version in versions]
    pieces: dict[uuid.UUID, list[CutPiece]] = {}
    for piece in await db.scalars(
        select(CutPiece).where(CutPiece.cut_id.in_(ids)).order_by(CutPiece.position)
    ):
        pieces.setdefault(piece.cut_id, []).append(piece)
    clip_ids = {p.asset_id for each in pieces.values() for p in each if p.asset_id is not None}
    assets: dict[uuid.UUID, Any] = {}
    if clip_ids:
        rows = await db.execute(
            select(Asset.id, Asset.media_type, Asset.availability).where(Asset.id.in_(clip_ids))
        )
        assets = {row.id: row for row in rows.all()}
    made: dict[uuid.UUID, CutDraft] = {}
    for draft in await db.scalars(
        select(CutDraft).where(CutDraft.cut_id.in_(ids)).order_by(CutDraft.created_at)
    ):
        if draft.cut_id is not None:
            made[draft.cut_id] = draft  # the newest, should there ever be two
    demo = _demo_cuts(versions, {made_id for made_id, d in made.items() if d.demo})
    emails = await _emails(db, [version.created_by for version in versions])
    status = await drafts.editor_status(db, now=deps.now(), settings=deps.settings)
    newest = versions[-1]
    return CutOut(
        id=cut.id,
        root_id=cut.root_id or cut.id,
        title=newest.title,
        client=newest.client,
        editor=_editor_out(status),
        can_draft=status.why_not is None and user.role != "readonly",
        drafts=await _family_drafts(db, user, versions, datetime.now(UTC)),
        versions=[
            CutVersionOut(
                id=version.id,
                version=version.version,
                parent_id=version.parent_id,
                source=version.source,
                label=LABELS.get(version.source, version.source),
                demo=version.id in demo,
                title=version.title,
                duration_s=version.duration_s,
                piece_count=version.piece_count,
                created_at=_utc(version.created_at),
                created_by=emails.get(version.created_by) if version.created_by else None,
                sent_at=_utc(version.sent_at),
                can_delete=_may_delete(user, version),
                provenance=_provenance_out(version),
                draft=(
                    _made_from_out(made[version.id], pieces.get(version.id, []))
                    if version.id in made
                    else None
                ),
                pieces=[_piece_out(version, p, assets) for p in pieces.get(version.id, [])],
            )
            for version in versions
        ],
    )


@router.get("/{cut_id}/pieces/{position}/still")
async def piece_still(
    cut_id: uuid.UUID,
    position: int,
    _user: CurrentUser,
    db: DbDep,
    deps: DepsDep,
) -> Response:
    """The picture for one piece: the shot it came from at its middle (the
    snapshot kept with the cut), else the piece's own middle. Only what is
    already on disk, never grabbed from a proxy; 404 when there is none, and
    the page shows the clip's poster instead (P23)."""
    await _cut(db, cut_id)
    piece = await db.scalar(
        select(CutPiece).where(CutPiece.cut_id == cut_id, CutPiece.position == position)
    )
    if piece is None or piece.asset_id is None:
        raise HTTPException(404, NO_PICTURE)
    shot = piece.shot if isinstance(piece.shot, dict) else {}
    start, end = shot.get("start_ms"), shot.get("end_ms")
    if isinstance(start, int) and isinstance(end, int) and end > start:
        moment = (start + end) // 2
    else:
        moment = (piece.in_ms + piece.out_ms) // 2
    path = await shotlist.still_path(db, piece.asset_id, moment, deps.settings)
    if path is None:
        raise HTTPException(404, NO_PICTURE)
    try:
        # Read here rather than streamed: a file that went between the lookup
        # and the answer is a 404, not a response that breaks half sent.
        picture = await asyncio.to_thread(path.read_bytes)
    except OSError:
        raise HTTPException(404, NO_PICTURE) from None
    return Response(picture, media_type="image/jpeg", headers={"Cache-Control": STILL_CACHE})


@router.post("/{cut_id}/send", response_model=SendOut)
async def send_cut(
    cut_id: uuid.UUID, user: CurrentUser, db: DbDep, body: SendIn | None = None
) -> SendOut:
    """The Premiere file for this version, for the computer the profile names
    (none: the server's own paths, which import offline until relinked). A
    clip deleted since is left out and named; one off the share is written
    and named. The first download marks the cut sent: from then on a local
    draft's shots count as used, like any cut that went somewhere (D4)."""
    cut = await _cut(db, cut_id)
    profile = (body.profile if body is not None else "").strip() or None
    try:
        rebuilt = await roughcut.rebuild(db, cut, profile=profile)
    except roughcut.NothingToSend as err:
        raise HTTPException(409, str(err)) from None
    except roughcut.CutError as err:
        raise HTTPException(400, str(err)) from None
    # Guarded: the first download is the one that counts, and two at once
    # must not move it.
    await db.execute(
        update(Cut)
        .where(Cut.id == cut_id, Cut.sent_at.is_(None))
        .values(sent_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    sent_at = await db.scalar(select(Cut.sent_at).where(Cut.id == cut_id))
    built = rebuilt.cut
    log.info(
        "cuts.sent",
        cut_id=str(cut_id),
        profile=profile,
        cuts=built.cuts,
        missing=len(rebuilt.missing),
        by=str(user.id),
    )
    return SendOut(
        filename=built.filename,
        xml=built.xml,
        profile=built.profile,
        duration_s=built.duration_s,
        cuts=built.cuts,
        unmapped=built.unmapped,
        missing=rebuilt.missing,
        offline=rebuilt.offline,
        note=built.note,
        sent_at=_utc(sent_at),
    )


@router.get("/{cut_id}/review-request", response_model=ReviewOut)
async def review_request(cut_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> ReviewOut:
    """The message that asks Claude to review this version, with its full id
    (the one way Claude can name a local draft nobody downloaded). The page
    hands it to the chat panel unchanged; nothing is sent from here."""
    cut = await _cut(db, cut_id)
    asked = await drafts.review_request(db, cut)
    return ReviewOut(
        cut_id=cut.id,
        label=asked.label,
        shots=asked.shots,
        candidates=asked.candidates,
        chars=len(asked.text),
        text=asked.text,
    )


@router.delete("/{cut_id}", status_code=204)
async def delete_cut(cut_id: uuid.UUID, user: CurrentUser, db: DbDep, request: Request) -> Response:
    """Delete one version and its pieces. The other versions keep their
    numbers and their family; a draft it was made from keeps its brief and is
    no longer listed. For whoever made it, or an administrator; written down."""
    cut = await _cut(db, cut_id)
    if not _may_delete(user, cut):
        raise HTTPException(403, NOT_YOUR_CUT)
    detail = {
        "cut_id": str(cut.id),
        "title": cut.title,
        "source": cut.source,
        "version": cut.version,
    }
    # Said here as well as by the keys: SQLite enforces neither unless asked.
    await db.execute(
        update(CutDraft)
        .where(CutDraft.cut_id == cut_id)
        .values(cut_id=None)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        delete(CutPiece)
        .where(CutPiece.cut_id == cut_id)
        .execution_options(synchronize_session=False)
    )
    await db.execute(
        delete(Cut).where(Cut.id == cut_id).execution_options(synchronize_session=False)
    )
    await auth_service.audit(
        db, "cuts.deleted", actor_user_id=user.id, ip=client_ip(request), detail=detail
    )
    await db.commit()
    log.info("cuts.deleted", cut_id=str(cut_id), by=str(user.id))
    return Response(status_code=204)
