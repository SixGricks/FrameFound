"""Compare on your footage: the API (design L2 section 6).

The owner draws a sample of the catalogue, ticks the installed models that
read pictures, and gets one column per model beside what the catalogue
already knows about that footage. Everything a model is shown is frozen
when the comparison is made (`sampling`), each model's turn is one job on
the `local_ai` lane (`compare_job`), and the rows are the record
(`comparisons`). Nothing here, or anywhere a comparison reaches, writes to
the catalogue.

Reading is for anyone signed in: the pages show everything to everybody and
give only an administrator the buttons (`can_manage`). Every change is an
administrator's, leaves an audit row and is committed with it. Where models
cannot run (the containers) every change is refused with L1's sentence
(409), and what rows there are still read. Every refusal is the sentence the
page shows, in the one error envelope: 400 the request cannot be read or the
sample cannot be made, 404 no such thing, 409 not possible as things stand,
503 the queue is down.

The figures are counts with what they are counts of. Nothing here orders,
ranks or marks a column by them: columns stand in the order they were added.

A non-administrator is never sent a server path: a column's `detail` (a
runtime's last lines, which can name files) is an administrator's. Disk work
— drawing reads stills, freezing writes them — runs in a thread.
"""

import asyncio
import re
import secrets
import uuid
from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.api.v1.local_models import QUEUE_DOWN, Changing, DepsDep
from framefound.auth import service as auth_service
from framefound.auth.deps import CurrentUser, DbDep, client_ip, require_admin
from framefound.config import Settings
from framefound.db.models import (
    Asset,
    Comparison,
    ComparisonAnswer,
    ComparisonItem,
    ComparisonModel,
    Cut,
    CutPiece,
    Library,
    Listing,
    LocalAiJob,
    LocalModel,
    ShotWindow,
    User,
    catalogued,
)
from framefound.local_models import (
    ROLES,
    agreement,
    availability,
    comparisons,
    demo,
    registry,
    sampling,
    schedule,
)
from framefound.local_models.ask import NO_SUCH_MODEL
from framefound.local_models.demo import DEMO_NOTE
from framefound.local_models.deps import Deps
from framefound.local_models.kinds import is_demo_runtime
from framefound.local_models.sampling import Drawn, SampleError, SampleSpec
from framefound.local_models.settings_store import load_config
from framefound.looking import prompt

# The lane's task module (it registers every kind, compare_model among them,
# and brings the queue's client).
from framefound.processing import local_ai_tasks

log = structlog.get_logger()
router = APIRouter(prefix="/local-models/compare", tags=["local-models"])

PREFIX = "/api/v1/local-models/compare"
# The newest comparisons the list shows.
LISTED = 100
# Folders and cuts a source search answers with.
FOLDERS_LISTED = 30
CUTS_LISTED = 200
# Ids asked about in one IN list (SQLite's bound-value limit).
CHUNK = 500
# A frozen picture never changes.
PICTURE_CACHE = "private, max-age=86400"
# Fresh seeds are drawn below this, so a seed fits every database's integer.
SEED_LIMIT = 2**31
# What Carry on changes on a column, put back when the lane cannot be woken.
_CARRIED_ON = (
    "state",
    "message",
    "detail",
    "not_before",
    "finished_at",
    "job_id",
    "done",
    "failed",
)
# A frozen picture's number: at most MAX_PICTURES of them, so a few digits.
_PICTURE_NUMBER = re.compile(r"[0-9]{1,3}")

NO_SUCH_COMPARISON = "This comparison is not here any more."
NO_SUCH_ITEM = "That item is not in this comparison."
NO_SUCH_PICTURE = "This picture is no longer on this machine."
NO_SOURCE = "Say where the sample comes from: a folder, a listing, cuts or an earlier comparison."
BAD_ID = "That is not something FrameFound listed. Choose it again from the list."
NO_LIBRARY = "That library is no longer in the catalogue."
NO_LISTING = "That listing is no longer there."
NO_CUT = "One of the chosen cuts is no longer in cut history."
NO_EARLIER = "That comparison is no longer there."
BAD_NEIGHBOURS = "Say yes or no to taking the clips beside the used ones."
BAD_STILLS = "A shot is shown as one still or three."
BAD_SEED = "The sample's seed is a whole number."
BAD_RETRY = "Say yes or no to trying the failed ones again."
NOTE_AS_TEXT = "Write the note as text."
CHOOSE_A_MODEL = "Choose at least one model."
AT_MOST_SIX = "Choose at most six models."
# How `sampling.draw` starts the sentence for a sample with nothing in it:
# the preview answers that as `ok: false`, not as a refusal.
NOTHING_HERE = "There is nothing to compare on here:"
NO_TAG_TRUTH = (
    "Nobody has decided any tags on this footage, so there will be nothing to check tags against."
)
NO_SCORE_TRUTH = (
    "None of these shots was used in a recorded cut, so there will be nothing to check "
    "scores against."
)
PER_CLIP = "Tags are checked per clip, using the shots of it that are in the sample."


# --- what the pages are sent --------------------------------------------------------------


class _Out(BaseModel):
    # Several fields are called model_…, which is what they are.
    model_config = ConfigDict(protected_namespaces=())


class Limits(_Out):
    min_size: int
    max_size: int
    default_size: int
    max_models: int


class ModelChoice(_Out):
    id: uuid.UUID
    name: str
    repo_id: str
    revision_short: str
    variant: str
    can_compare: bool
    reason: str | None
    fits: str
    memory_gb: float
    roles: list[str]
    seconds_hint: float | None
    demo: bool
    # Its program is not installed here (the reason says so): the builder
    # links to Software that runs models, where it is installed.
    program_missing: bool


class EntrySummary(_Out):
    entry_id: uuid.UUID
    name: str
    state: str
    done: int
    failed: int


class ComparisonSummary(_Out):
    id: uuid.UUID
    name: str
    source_kind: str
    source_label: str
    size: int
    photos: int
    shots: int
    current: bool
    models: list[EntrySummary]
    state: str
    verdict: str
    verdict_model: str | None
    created_at: datetime | None
    created_by: str | None


class CompareOverview(_Out):
    mode: str
    note: str | None
    can_manage: bool
    prompt_version: str
    limits: Limits
    models: list[ModelChoice]
    comparisons: list[ComparisonSummary]


class FolderOut(_Out):
    library_id: uuid.UUID
    library_name: str
    path: str
    photos: int
    clips: int
    clips_with_shots: int
    shots: int


class CutOut(_Out):
    id: uuid.UUID
    title: str
    client: str
    source: str
    created_at: datetime | None
    pieces: int
    clips: int
    clips_with_shots: int
    duration_s: float


class CountedCut(_Out):
    id: uuid.UUID
    title: str
    source: str
    created_at: datetime | None


class LeftOut(_Out):
    why: str
    count: int
    sentence: str


class TagTruth(_Out):
    footage: int
    yes: int
    no: int


class UsedTruth(_Out):
    used: int
    unused: int
    cuts: list[CountedCut]


class Truth(_Out):
    tags: TagTruth
    used: UsedTruth


class Pool(_Out):
    photos: int
    shots: int
    clips: int


class PreviewOut(_Out):
    ok: bool
    label: str
    what: str | None
    size: int | None
    stills: int | None
    items: int
    photos: int
    shots: int
    clips: int
    pool: Pool
    truth: Truth
    left_out: list[LeftOut]
    notes: list[str]


class FormOut(_Out):
    asked: int
    answered: int
    second_tries: int
    failed: dict[str, int]


class PaceOut(_Out):
    answered: int
    median_s: float | None
    slowest_s: float | None
    per_hour: int | None


class TagsOut(_Out):
    floor: float
    footage: int
    yes_total: int
    yes_found: int
    no_total: int
    no_repeated: int
    proposed: int
    unjudged: int
    unjudged_new: int


class ScoresOut(_Out):
    used: int
    unused: int
    pairs: int
    higher: int
    lower: int
    same: int
    scored: int
    distinct: int
    few: bool


class MeasuresOut(_Out):
    form: FormOut
    shots: PaceOut
    photos: PaceOut
    tags: list[TagsOut] | None
    scores: ScoresOut | None


class EntryOut(_Out):
    id: uuid.UUID
    model_id: uuid.UUID | None
    installed: bool
    name: str
    repo_id: str
    revision_short: str
    variant: str
    runtime: str | None
    runtime_label: str | None
    runtime_version: str | None
    demo: bool
    state: str
    message: str
    detail: str | None
    not_before: datetime | None
    cancelling: bool
    done: int
    failed: int
    total: int
    load_seconds: float | None
    ran_from: str | None
    memory_gb: float | None
    memory_source: str | None
    fits: str | None
    roles: list[str] | None
    measures: MeasuresOut
    can_cancel: bool
    can_carry_on: bool
    can_retry_failed: bool
    can_remove: bool
    started_at: datetime | None
    finished_at: datetime | None
    added_by: str | None


class Counts(_Out):
    photos: int
    shots: int
    clips: int
    used: int
    unused: int
    footage_with_tags: int
    yes: int
    no: int


class CutRef(_Out):
    id: str
    title: str
    source: str


class VerdictOut(_Out):
    verdict: str
    entry_id: uuid.UUID | None
    note: str
    at: datetime | None
    by: str | None


class ComparisonOut(_Out):
    id: uuid.UUID
    name: str
    source_kind: str
    source_label: str
    what: str
    size: int
    stills: int
    seed: int
    prompt_version: str
    current: bool
    truth_at: datetime | None
    created_at: datetime | None
    created_by: str | None
    counts: Counts
    counted_cuts: list[CutRef]
    left_out: list[LeftOut]
    vocabulary_size: int
    over: int
    answers_stored: int
    entries: list[EntryOut]
    verdict: VerdictOut
    can_add_models: bool
    mode: str
    note: str | None
    can_manage: bool


class PictureOut(_Out):
    n: int
    at_s: float
    url: str


class ItemTruthOut(_Out):
    yes: list[str]
    no: list[str]


class AnswerTag(_Out):
    name: str
    confidence: float | None
    in_vocabulary: bool
    ruling: Literal["yes", "no"] | None


class AnswerOut(_Out):
    ok: bool
    problem: str | None
    message: str
    tags: list[AnswerTag]
    caption: str
    score: int | None
    reason: str
    seconds: float | None
    attempts: int
    raw: str | None


class ItemOut(_Out):
    id: uuid.UUID
    position: int
    kind: str
    asset_id: uuid.UUID | None
    in_catalogue: bool
    media_type: str
    filename: str
    library: str
    folder: str
    start_s: float | None
    end_s: float | None
    facts_line: str
    pictures: list[PictureOut]
    truth: ItemTruthOut
    used: bool | None
    used_in: list[str]
    counted: bool
    answers: dict[str, AnswerOut]


class ItemsOut(_Out):
    total: int
    items: list[ItemOut]


# --- what the pages send ------------------------------------------------------------------
# Loose on purpose: every field is read here, so a request that cannot be
# read is refused with a sentence (400) rather than a validator's list.


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SampleBody(_Body):
    source: Any = None
    what: Any = None
    size: Any = None
    stills: Any = None
    truth_cut_ids: Any = None


class StartBody(SampleBody):
    name: Any = None
    model_ids: Any = None
    seed: Any = None


class ModelsBody(_Body):
    model_ids: Any = None


class RunBody(_Body):
    retry_failed: Any = False


class ChangeBody(_Body):
    name: Any = None
    verdict: Any = None
    entry_id: Any = None
    note: Any = None


# --- small things -------------------------------------------------------------------------


def _utc(moment: datetime | None) -> datetime | None:
    """A moment read from a table, in UTC: SQLite hands back what was written
    without its zone, and everything written here is UTC."""
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _ids(value: object) -> list[uuid.UUID]:
    """A list of ids as the page sends them; 400 when it is not one."""
    if not isinstance(value, list):
        raise HTTPException(400, BAD_ID)
    found: list[uuid.UUID] = []
    for each in value:
        parsed = _uuid(each)
        if parsed is None:
            raise HTTPException(400, BAD_ID)
        found.append(parsed)
    return found


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [each for each in value if isinstance(each, str)]


def _folder_of(relative_path: str) -> str:
    return relative_path.rpartition("/")[0]


def _chunks(ids: Sequence[uuid.UUID]) -> list[Sequence[uuid.UUID]]:
    return [ids[start : start + CHUNK] for start in range(0, len(ids), CHUNK)]


def _like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build(entry: ComparisonModel) -> str:
    """A column's model in an audit row: `repo@revision`."""
    return f"{entry.model}@{entry.model_revision}"


async def _wake(deps: Deps) -> bool:
    """Wake the lane; False when the queue is down."""
    return await asyncio.to_thread(local_ai_tasks.wake, deps.settings)


async def _emails(db: AsyncSession, ids: Collection[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {each for each in ids if each is not None}
    if not wanted:
        return {}
    rows = await db.execute(select(User.id, User.email).where(User.id.in_(list(wanted))))
    return {row.id: row.email for row in rows}


async def _models_by_id(
    db: AsyncSession, ids: Collection[uuid.UUID | None]
) -> dict[uuid.UUID, LocalModel]:
    wanted = [each for each in {i for i in ids if i is not None}]
    if not wanted:
        return {}
    rows = await db.scalars(
        select(LocalModel)
        .where(LocalModel.id.in_(wanted))
        .execution_options(populate_existing=True)
    )
    return {model.id: model for model in rows.all()}


async def _held(db: AsyncSession) -> dict[uuid.UUID, list[str]]:
    """The roles each model holds now (not the ones it waits for): a model
    with one is on this Mac."""
    held: dict[uuid.UUID, list[str]] = {}
    found = await registry.roles(db)
    for role in ROLES:
        model_id = found[role].model_id
        if model_id is not None:
            held.setdefault(model_id, []).append(role)
    return held


def _mode(deps: Deps) -> tuple[str, str | None, bool]:
    """(mode, note, ok), as the AI models page reads them."""
    found = availability(deps.settings)
    notes = {"unavailable": found.reason, "demo": DEMO_NOTE}
    if found.mode == "demo":
        # The demo has no beat: whatever is left waiting is looked at once
        # somebody opens a page (as L1's overview does).
        demo.keep_going(deps.settings)
    return found.mode, notes.get(found.mode), found.ok


async def _comparison_or_404(db: AsyncSession, comparison_id: str) -> Comparison:
    parsed = _uuid(comparison_id)
    found = None if parsed is None else await db.get(Comparison, parsed, populate_existing=True)
    if found is None:
        raise HTTPException(404, NO_SUCH_COMPARISON)
    return found


async def _entry_or_404(db: AsyncSession, comparison: Comparison, entry_id: str) -> ComparisonModel:
    parsed = _uuid(entry_id)
    found = (
        None if parsed is None else await db.get(ComparisonModel, parsed, populate_existing=True)
    )
    if found is None or found.comparison_id != comparison.id:
        raise HTTPException(404, comparisons.NOT_HERE)
    return found


async def _models_asked(
    db: AsyncSession, deps: Deps, value: object, *, adding: bool = False
) -> list[LocalModel]:
    """The ticked models, each installed and allowed to be compared, in the
    order ticked (a model ticked twice is one). 400, 404 or 409 otherwise —
    the 409 naming the first that may not be compared."""
    if not isinstance(value, list):
        raise HTTPException(400, CHOOSE_A_MODEL)
    ids: list[uuid.UUID] = []
    for each in value:
        parsed = _uuid(each)
        if parsed is None:
            raise HTTPException(404, NO_SUCH_MODEL)
        if parsed not in ids:
            ids.append(parsed)
    if not ids:
        raise HTTPException(400, CHOOSE_A_MODEL)
    # Adding to a saved one, the comparison's own limit says it (409).
    if not adding and len(ids) > comparisons.MAX_MODELS:
        raise HTTPException(400, AT_MOST_SIX)
    found = await _models_by_id(db, ids)
    models: list[LocalModel] = []
    for model_id in ids:
        model = found.get(model_id)
        if model is None:
            raise HTTPException(404, NO_SUCH_MODEL)
        models.append(model)
    config = await load_config(db)
    missing = await _missing(deps)
    for model in models:
        problem = comparisons.may_compare(model, config, deps.kinds, missing)
        if problem is not None:
            raise HTTPException(409, problem)
    return models


async def _missing(deps: Deps) -> dict[str, str]:
    """The programs not installed here, with the sentence for each: a model on
    one is refused before anything starts, not stopped once it has."""
    return await asyncio.to_thread(comparisons.missing_programs, deps.kinds, deps.settings)


# --- reading a sample request ----------------------------------------------------------


def _spec(body: SampleBody, *, seed: int = 0) -> SampleSpec:
    """The sample as asked. Only the shape is checked here; what the values
    mean is `sampling`'s (its SampleError is the 400's sentence)."""
    source = body.source
    if not isinstance(source, dict) or source.get("kind") not in sampling.KINDS:
        raise HTTPException(400, NO_SOURCE)
    if isinstance(body.stills, bool):
        raise HTTPException(400, BAD_STILLS)
    kind = source["kind"]
    common: dict[str, Any] = {
        "what": body.what,
        "size": body.size,
        "stills": body.stills,
        "seed": seed,
    }
    if kind == "folder":
        truth: tuple[uuid.UUID, ...] | None = None
        if body.truth_cut_ids is not None:
            truth = tuple(_ids(body.truth_cut_ids))
        return SampleSpec(
            kind="folder",
            library_id=_optional_id(source.get("library_id")),
            # As sent: `folders.clean_folder` says what is wrong with it.
            folder=_as_sent(source.get("folder"), ""),
            truth_cut_ids=truth,
            **common,
        )
    if kind == "listing":
        return SampleSpec(
            kind="listing", listing_id=_optional_id(source.get("listing_id")), **common
        )
    if kind == "cuts":
        neighbours = source.get("neighbours", True)
        if not isinstance(neighbours, bool):
            raise HTTPException(400, BAD_NEIGHBOURS)
        cut_ids = source.get("cut_ids")
        return SampleSpec(
            kind="cuts",
            cut_ids=() if cut_ids is None else tuple(_ids(cut_ids)),
            neighbours=neighbours,
            **common,
        )
    return SampleSpec(
        kind="comparison", comparison_id=_optional_id(source.get("comparison_id")), **common
    )


def _as_sent(value: Any, default: Any) -> Any:
    return default if value is None else value


def _optional_id(value: object) -> uuid.UUID | None:
    """An id that may be missing (sampling says what is missing); 400 when it
    is there and is not an id."""
    if value is None:
        return None
    parsed = _uuid(value)
    if parsed is None:
        raise HTTPException(400, BAD_ID)
    return parsed


async def _there(db: AsyncSession, spec: SampleSpec) -> None:
    """404 for a library, listing, cut or comparison that does not exist."""
    looked_for: list[tuple[type[Any], uuid.UUID | None, str]] = [
        (Library, spec.library_id if spec.kind == "folder" else None, NO_LIBRARY),
        (Listing, spec.listing_id if spec.kind == "listing" else None, NO_LISTING),
        (Comparison, spec.comparison_id if spec.kind == "comparison" else None, NO_EARLIER),
    ]
    for table, wanted, sentence in looked_for:
        if wanted is not None and await db.get(table, wanted) is None:
            raise HTTPException(404, sentence)
    cut_ids = set(spec.cut_ids) if spec.kind == "cuts" else set()
    if spec.kind == "folder" and spec.truth_cut_ids:
        cut_ids |= set(spec.truth_cut_ids)
    if cut_ids:
        found = await db.scalar(
            select(func.count()).select_from(Cut).where(Cut.id.in_(list(cut_ids)))
        )
        if int(found or 0) < len(cut_ids):
            raise HTTPException(404, NO_CUT)


def _seed(value: object) -> int:
    if value is None:
        return secrets.randbelow(SEED_LIMIT)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**63:
        raise HTTPException(400, BAD_SEED)
    return value


def _left_out(left: Mapping[str, Any], size: int | None) -> list[LeftOut]:
    return [
        LeftOut(
            why=reason,
            count=_int(count),
            sentence=sampling.left_out_sentence(reason, _int(count), size=size),
        )
        for reason, count in left.items()
        if _int(count) > 0
    ]


def _notes(counts: Mapping[str, Any]) -> list[str]:
    """What the preview says about the truth there is to check against."""
    notes: list[str] = []
    if _int(counts.get("footage_with_tags")) == 0:
        notes.append(NO_TAG_TRUTH)
    shots, used = _int(counts.get("shots")), _int(counts.get("used"))
    if shots:
        if used == 0:
            notes.append(NO_SCORE_TRUTH)
        elif used < agreement.FEW_USED:
            were = "was" if used == 1 else "were"
            notes.append(
                f"Only {used} of these shots {were} used: read the score figures as a hint."
            )
        notes.append(PER_CLIP)
    return notes


def _preview_out(drawn: Drawn) -> PreviewOut:
    counts = comparisons.counts_of(drawn)
    filled = drawn.spec
    return PreviewOut(
        ok=True,
        label=drawn.label,
        what=filled.what,
        size=filled.size,
        stills=filled.stills,
        items=len(drawn.items),
        photos=counts["photos"],
        shots=counts["shots"],
        clips=counts["clips"],
        pool=Pool(
            photos=_int(drawn.pool.get("photos")),
            shots=_int(drawn.pool.get("shots")),
            clips=_int(drawn.pool.get("clips")),
        ),
        truth=Truth(
            tags=TagTruth(footage=counts["footage_with_tags"], yes=counts["yes"], no=counts["no"]),
            used=UsedTruth(
                used=counts["used"],
                unused=counts["unused"],
                cuts=[
                    CountedCut(id=cut_id, title=title, source=source, created_at=_utc(when))
                    for cut_id, title, source, when in drawn.counted_cuts
                ],
            ),
        ),
        left_out=_left_out(drawn.left_out, filled.size),
        notes=_notes(counts),
    )


def _nothing_out(sentence: str) -> PreviewOut:
    return PreviewOut(
        ok=False,
        label="",
        what=None,
        size=None,
        stills=None,
        items=0,
        photos=0,
        shots=0,
        clips=0,
        pool=Pool(photos=0, shots=0, clips=0),
        truth=Truth(
            tags=TagTruth(footage=0, yes=0, no=0), used=UsedTruth(used=0, unused=0, cuts=[])
        ),
        left_out=[],
        notes=[sentence],
    )


# --- a comparison, as the page reads it ---------------------------------------------------


def _measures_out(found: comparisons.Measures) -> MeasuresOut:
    def pace(p: agreement.Pace) -> PaceOut:
        return PaceOut(
            answered=p.answered, median_s=p.median_s, slowest_s=p.slowest_s, per_hour=p.per_hour
        )

    return MeasuresOut(
        form=FormOut(
            asked=found.form.asked,
            answered=found.form.answered,
            second_tries=found.form.second_tries,
            failed=dict(found.form.failed),
        ),
        shots=pace(found.shots),
        photos=pace(found.photos),
        tags=None
        if found.tags is None
        else [
            TagsOut(
                floor=t.floor,
                footage=t.footage,
                yes_total=t.yes_total,
                yes_found=t.yes_found,
                no_total=t.no_total,
                no_repeated=t.no_repeated,
                proposed=t.proposed,
                unjudged=t.unjudged,
                unjudged_new=t.unjudged_new,
            )
            for t in found.tags
        ],
        scores=None
        if found.scores is None
        else ScoresOut(
            used=found.scores.used,
            unused=found.scores.unused,
            pairs=found.scores.pairs,
            higher=found.scores.higher,
            lower=found.scores.lower,
            same=found.scores.same,
            scored=found.scores.scored,
            distinct=found.scores.distinct,
            few=found.scores.few,
        ),
    )


async def _comparison_out(
    db: AsyncSession, deps: Deps, user: User, comparison: Comparison
) -> ComparisonOut:
    mode, note, ok = _mode(deps)
    admin = user.role == "admin"
    can_manage = admin and ok
    now = deps.now()
    current = comparisons.is_current(comparison)
    items = await comparisons.items_of(db, comparison.id)
    entries = await comparisons.entries_of(db, comparison.id)
    answers = await comparisons.answers_of(db, [entry.id for entry in entries])
    running = await comparisons.jobs_of(db, entries)
    figures = comparisons.count(items, entries, answers)
    over = comparisons.over_items(entries, answers)
    models = await _models_by_id(db, [entry.model_id for entry in entries])
    held = await _held(db)
    budgets = (await load_config(db)).budgets()
    emails = await _emails(
        db,
        [comparison.created_by, comparison.verdict_by, *(entry.added_by for entry in entries)],
    )
    stored: dict[uuid.UUID, tuple[int, int]] = {entry.id: (0, 0) for entry in entries}
    for row in answers:
        good, bad = stored.get(row.entry_id, (0, 0))
        stored[row.entry_id] = (good + 1, bad) if row.ok else (good, bad + 1)

    out: list[EntryOut] = []
    for entry in entries:
        job = running.get(entry.id)
        state, message, not_before = comparisons.shown(entry, job, now)
        model = None if entry.model_id is None else models.get(entry.model_id)
        installed = model is not None
        good, bad = stored[entry.id]
        left = len(items) - good - bad
        active = state in comparisons.ACTIVE
        cancelling = comparisons.cancelling(job)
        may_run = (
            can_manage
            and not active
            and installed
            and current
            and entry.prompt_version == comparison.prompt_version
        )
        runtime = entry.runtime or (model.runtime if model is not None else None)
        kind = None if runtime is None else deps.kinds.get(runtime)
        out.append(
            EntryOut(
                id=entry.id,
                model_id=entry.model_id,
                installed=installed,
                name=entry.model_name or entry.model.rpartition("/")[2],
                repo_id=entry.model,
                revision_short=entry.model_revision[:7],
                variant=entry.variant or "",
                runtime=runtime,
                runtime_label=None if runtime is None else (kind.label if kind else runtime),
                runtime_version=entry.runtime_version,
                demo=runtime is not None and is_demo_runtime(runtime),
                state=state,
                message=message,
                detail=entry.detail if admin else None,
                not_before=not_before,
                cancelling=cancelling,
                done=good,
                failed=bad,
                total=len(items),
                load_seconds=entry.load_seconds,
                ran_from=entry.ran_from,
                memory_gb=entry.memory_gb,
                memory_source=entry.memory_source,
                fits=None if model is None else schedule.fits(registry.memory_gb(model), budgets),
                roles=None if model is None else held.get(model.id, []),
                measures=_measures_out(figures[entry.id]),
                can_cancel=can_manage and active and not cancelling,
                # A column with every item answered that never closed `done`
                # (its worker died after the last answer) can be carried on:
                # its turn asks nothing and closes it.
                can_carry_on=may_run and (left > 0 or state != "done"),
                can_retry_failed=may_run and bad > 0,
                can_remove=can_manage and not active,
                started_at=_utc(entry.started_at),
                finished_at=_utc(entry.finished_at),
                added_by=None if entry.added_by is None else emails.get(entry.added_by),
            )
        )

    counts = comparison.counts if isinstance(comparison.counts, dict) else {}
    left_out = counts.get("left_out")
    vocabulary = comparison.vocabulary if isinstance(comparison.vocabulary, list) else []
    return ComparisonOut(
        id=comparison.id,
        name=comparison.name,
        source_kind=comparison.source_kind,
        source_label=comparison.source_label,
        what=comparison.what,
        size=comparison.size,
        stills=comparison.stills,
        seed=comparison.seed,
        prompt_version=comparison.prompt_version,
        current=current,
        truth_at=_utc(comparison.truth_at),
        created_at=_utc(comparison.created_at),
        created_by=None if comparison.created_by is None else emails.get(comparison.created_by),
        counts=Counts(
            **{
                key: _int(counts.get(key))
                for key in (
                    "photos",
                    "shots",
                    "clips",
                    "used",
                    "unused",
                    "footage_with_tags",
                    "yes",
                    "no",
                )
            }
        ),
        counted_cuts=[
            CutRef(
                id=str(cut.get("id", "")),
                title=str(cut.get("title", "")),
                source=str(cut.get("source", "")),
            )
            for cut in (comparison.counted_cuts or [])
            if isinstance(cut, dict)
        ],
        left_out=_left_out(left_out if isinstance(left_out, dict) else {}, comparison.size),
        vocabulary_size=len(vocabulary),
        over=len(over),
        answers_stored=len(answers),
        entries=out,
        verdict=VerdictOut(
            verdict=comparison.verdict,
            entry_id=comparison.verdict_entry_id,
            note=comparison.verdict_note,
            at=_utc(comparison.verdict_at),
            by=None if comparison.verdict_by is None else emails.get(comparison.verdict_by),
        ),
        can_add_models=can_manage and current and len(entries) < comparisons.MAX_MODELS,
        mode=mode,
        note=note,
        can_manage=can_manage,
    )


async def _one(db: AsyncSession, deps: Deps, user: User, comparison_id: uuid.UUID) -> ComparisonOut:
    found = await db.get(Comparison, comparison_id, populate_existing=True)
    if found is None:
        raise HTTPException(404, NO_SUCH_COMPARISON)
    return await _comparison_out(db, deps, user, found)


# --- 6.1 the list, and the builder's models -----------------------------------------------


@router.get("", response_model=CompareOverview)
async def compare_overview(user: CurrentUser, db: DbDep, deps: DepsDep) -> CompareOverview:
    """The saved comparisons, newest first, and every installed model with
    whether it can be compared. Where models cannot run there are no models
    to choose; what comparisons there are still read."""
    mode, note, ok = _mode(deps)
    models_out: list[ModelChoice] = []
    if ok:
        config = await load_config(db)
        budgets = config.budgets()
        held = await _held(db)
        missing = await _missing(deps)
        models = (
            await db.scalars(
                select(LocalModel)
                .order_by(LocalModel.created_at.desc(), LocalModel.id)
                .execution_options(populate_existing=True)
            )
        ).all()
        for model in models:
            reason = comparisons.compare_problem(model, config, deps.kinds, missing)
            models_out.append(
                ModelChoice(
                    id=model.id,
                    name=comparisons.name_of(model),
                    repo_id=model.repo_id,
                    revision_short=model.revision[:7],
                    variant=model.variant or "",
                    can_compare=reason is None,
                    reason=reason,
                    fits=schedule.fits(registry.memory_gb(model), budgets),
                    memory_gb=registry.memory_gb(model),
                    roles=held.get(model.id, []),
                    seconds_hint=await comparisons.seconds_hint(db, model.id),
                    demo=is_demo_runtime(model.runtime),
                    program_missing=reason is not None and reason == missing.get(model.runtime),
                )
            )

    rows = list(
        (
            await db.scalars(
                select(Comparison)
                .order_by(Comparison.created_at.desc(), Comparison.id)
                .limit(LISTED)
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    entries: dict[uuid.UUID, list[ComparisonModel]] = {row.id: [] for row in rows}
    if rows:
        found = await db.scalars(
            select(ComparisonModel)
            .where(ComparisonModel.comparison_id.in_(list(entries)))
            .order_by(ComparisonModel.created_at, ComparisonModel.id)
            .execution_options(populate_existing=True)
        )
        for entry in found.all():
            entries[entry.comparison_id].append(entry)
    every = [entry for listed in entries.values() for entry in listed]
    running = await comparisons.jobs_of(db, every)
    counted = await _answer_counts(db, [entry.id for entry in every])
    emails = await _emails(db, [row.created_by for row in rows])
    now = deps.now()
    summaries: list[ComparisonSummary] = []
    for row in rows:
        counts = row.counts if isinstance(row.counts, dict) else {}
        columns: list[EntrySummary] = []
        for entry in entries[row.id]:
            state, _message, _until = comparisons.shown(entry, running.get(entry.id), now)
            good, bad = counted.get(entry.id, (0, 0))
            columns.append(
                EntrySummary(
                    entry_id=entry.id,
                    name=entry.model_name or entry.model.rpartition("/")[2],
                    state=state,
                    done=good,
                    failed=bad,
                )
            )
        chosen = next(
            (column.name for column in columns if column.entry_id == row.verdict_entry_id), None
        )
        summaries.append(
            ComparisonSummary(
                id=row.id,
                name=row.name,
                source_kind=row.source_kind,
                source_label=row.source_label,
                size=row.size,
                photos=_int(counts.get("photos")),
                shots=_int(counts.get("shots")),
                current=comparisons.is_current(row),
                models=columns,
                state=comparisons.overall([column.state for column in columns]),
                verdict=row.verdict,
                verdict_model=chosen if row.verdict == "chosen" else None,
                created_at=_utc(row.created_at),
                created_by=None if row.created_by is None else emails.get(row.created_by),
            )
        )
    return CompareOverview(
        mode=mode,
        note=note,
        can_manage=user.role == "admin" and ok,
        prompt_version=prompt.PROMPT_VERSION,
        limits=Limits(
            min_size=sampling.MIN_SIZE,
            max_size=sampling.MAX_SIZE,
            default_size=sampling.DEFAULT_SIZE,
            max_models=comparisons.MAX_MODELS,
        ),
        models=models_out,
        comparisons=summaries,
    )


async def _answer_counts(
    db: AsyncSession, entry_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    """(answered, failed) per column, from the answer rows."""
    found: dict[uuid.UUID, tuple[int, int]] = {}
    for chunk in _chunks(list(entry_ids)):
        rows = await db.execute(
            select(ComparisonAnswer.entry_id, ComparisonAnswer.ok, func.count())
            .where(ComparisonAnswer.entry_id.in_(list(chunk)))
            .group_by(ComparisonAnswer.entry_id, ComparisonAnswer.ok)
        )
        for entry_id, good, many in rows.all():
            answered, failed = found.get(entry_id, (0, 0))
            found[entry_id] = (answered + many, failed) if good else (answered, failed + many)
    return found


# --- 6.2 and 6.3 where a sample can come from ---------------------------------------------


async def _shot_counts(db: AsyncSession, video_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    found: dict[uuid.UUID, int] = {}
    for chunk in _chunks(list(video_ids)):
        rows = await db.execute(
            select(ShotWindow.asset_id, func.count())
            .where(ShotWindow.asset_id.in_(list(chunk)))
            .group_by(ShotWindow.asset_id)
        )
        for asset_id, many in rows.all():
            found[uuid.UUID(str(asset_id))] = int(many)
    return found


@router.get("/sources/folders", response_model=list[FolderOut])
async def source_folders(
    _user: CurrentUser,
    db: DbDep,
    q: str = Query(default="", max_length=200),
    library_id: str | None = Query(default=None),
) -> list[FolderOut]:
    """Folders whose path holds `q` (any case; the filename is not matched),
    each counted with everything under it; most shots first, at most 30.
    With no `q`, every folder, a library's whole self ("") among them.

    Grouped here rather than in SQL, over one scan of the paths, as
    `listings.search_folders` does: "this folder and everything under it" is
    then the same on PostgreSQL and SQLite."""
    needle = q.strip().strip("/").lower()
    conditions: list[Any] = [catalogued(), Asset.media_type.in_(("image", "video"))]
    if library_id:
        wanted = _uuid(library_id)
        if wanted is None:
            raise HTTPException(404, NO_LIBRARY)
        conditions.append(Asset.library_id == wanted)
    if needle:
        conditions.append(Asset.relative_path.ilike(f"%{_like_literal(needle)}%", escape="\\"))
    rows = (
        await db.execute(
            select(Asset.id, Asset.library_id, Asset.relative_path, Asset.media_type).where(
                *conditions
            )
        )
    ).all()
    shots = await _shot_counts(db, [row.id for row in rows if row.media_type == "video"])
    tally: dict[tuple[uuid.UUID, str], list[int]] = {}
    for row in rows:
        parts = _folder_of(row.relative_path).split("/")
        ancestors = [""] + ["/".join(parts[: n + 1]) for n in range(len(parts)) if parts[0]]
        for folder in ancestors:
            if needle and needle not in folder.lower():
                continue
            counts = tally.setdefault((row.library_id, folder), [0, 0, 0, 0])
            if row.media_type == "image":
                counts[0] += 1
            else:
                counts[1] += 1
                many = shots.get(row.id, 0)
                if many:
                    counts[2] += 1
                    counts[3] += many
    names = {
        library.id: library.name
        for library in (
            await db.scalars(select(Library).where(Library.id.in_(list({k[0] for k in tally}))))
        ).all()
    }
    ranked = sorted(tally.items(), key=lambda kv: (-kv[1][3], -kv[1][0], kv[0][1], str(kv[0][0])))
    return [
        FolderOut(
            library_id=library,
            library_name=names.get(library, ""),
            path=folder,
            photos=counts[0],
            clips=counts[1],
            clips_with_shots=counts[2],
            shots=counts[3],
        )
        for (library, folder), counts in ranked[:FOLDERS_LISTED]
    ]


@router.get("/sources/cuts", response_model=list[CutOut])
async def source_cuts(_user: CurrentUser, db: DbDep) -> list[CutOut]:
    """The finished cuts in cut history (`shotlist.counts_as_used`: built with
    the connector or imported), newest first, at most 200. A draft is not
    offered: it cannot say which shots were used."""
    from framefound.nle import shotlist

    cuts = list(
        (
            await db.scalars(
                select(Cut)
                .where(shotlist.counts_as_used())
                .order_by(Cut.created_at.desc(), Cut.id)
                .limit(CUTS_LISTED)
            )
        ).all()
    )
    pieces: dict[uuid.UUID, list[uuid.UUID | None]] = {cut.id: [] for cut in cuts}
    for chunk in _chunks([cut.id for cut in cuts]):
        rows = await db.execute(
            select(CutPiece.cut_id, CutPiece.asset_id).where(CutPiece.cut_id.in_(list(chunk)))
        )
        for cut_id, asset_id in rows.all():
            pieces[cut_id].append(None if asset_id is None else uuid.UUID(str(asset_id)))
    clips = sorted({a for listed in pieces.values() for a in listed if a is not None}, key=str)
    shots = await _shot_counts(db, clips)
    out: list[CutOut] = []
    for cut in cuts:
        used = {a for a in pieces[cut.id] if a is not None}
        out.append(
            CutOut(
                id=cut.id,
                title=cut.title,
                client=cut.client,
                source=cut.source,
                created_at=_utc(cut.created_at),
                pieces=len(pieces[cut.id]),
                clips=len(used),
                clips_with_shots=sum(1 for a in used if shots.get(a)),
                duration_s=float(cut.duration_s or 0.0),
            )
        )
    return out


# --- 6.4 the preview ------------------------------------------------------------------------


@router.post("/preview", response_model=PreviewOut)
async def preview(body: SampleBody, _user: CurrentUser, db: DbDep, deps: DepsDep) -> PreviewOut:
    """What a sample would be, drawn now with seed 0. Writes nothing. A sample
    with nothing in it is `ok: false` with the sentence (the owner is still
    choosing); a sample that cannot be made as asked is a 400."""
    spec = _spec(body)
    await _there(db, spec)
    try:
        drawn = await sampling.draw(db, spec, deps.settings)
    except SampleError as err:
        if str(err).startswith(NOTHING_HERE):
            return _nothing_out(str(err))
        raise HTTPException(400, str(err)) from None
    return _preview_out(drawn)


# --- 6.5 starting a comparison ------------------------------------------------------------


async def _undo(
    db: AsyncSession, comparison_id: uuid.UUID, job_ids: Sequence[uuid.UUID], deps: Deps
) -> None:
    """Remove a comparison the lane was never told about: its job rows first
    (then nothing reads as running), its rows, then its pictures."""
    if job_ids:
        await db.execute(delete(LocalAiJob).where(LocalAiJob.id.in_(list(job_ids))))
    made = await db.get(Comparison, comparison_id, populate_existing=True)
    if made is not None:
        await comparisons.delete(db, made, deps.now())
    await db.commit()
    await asyncio.to_thread(_drop_quietly, comparison_id, deps.settings)


def _drop_quietly(comparison_id: uuid.UUID, settings: Settings) -> None:
    """Remove a comparison's pictures once its rows are gone (or were never
    kept). Blocking. A file that cannot be removed (one being served at that
    moment, on Windows) is logged, not answered as a 500 for a request whose
    real work is already done."""
    try:
        sampling.drop_pictures(comparison_id, settings)
    except OSError:
        log.warning(
            "local_models.compare_pictures_left", comparison=str(comparison_id), exc_info=True
        )


def _drop_when_done(
    freezing: "asyncio.Future[Any]", comparison_id: uuid.UUID, settings: Settings
) -> None:
    """Drop the pictures once the freezing thread has finished, whenever that
    is: the request that started it may already be gone."""
    loop = asyncio.get_running_loop()

    def drop(done: "asyncio.Future[Any]") -> None:
        if not done.cancelled():
            done.exception()  # seen here, so it is not reported as never retrieved
        loop.run_in_executor(None, _drop_quietly, comparison_id, settings)

    freezing.add_done_callback(drop)


@router.post("", response_model=ComparisonOut, status_code=201, dependencies=[require_admin])
async def start(
    body: StartBody, request: Request, user: CurrentUser, db: DbDep, deps: Changing
) -> ComparisonOut:
    """Draw the sample, freeze it, and queue one turn per ticked model. The
    sample is fixed from here: every model is shown exactly the same thing.
    Nothing is left behind when any step fails, and nothing waits for a
    worker that will never come."""
    models = await _models_asked(db, deps, body.model_ids)
    name: str | None = None
    if body.name is not None:
        if not isinstance(body.name, str):
            raise HTTPException(400, comparisons.GIVE_NAME)
        if body.name.strip():
            try:
                name = comparisons.clean_name(body.name)
            except comparisons.BadRequest as err:
                raise HTTPException(400, str(err)) from None
    spec = _spec(body, seed=_seed(body.seed))
    await _there(db, spec)
    try:
        drawn = await sampling.draw(db, spec, deps.settings)
    except SampleError as err:
        raise HTTPException(400, str(err)) from None

    comparison = await comparisons.create(db, spec, drawn, name=name, user_id=user.id)
    comparison_id = comparison.id
    freezing = asyncio.ensure_future(
        asyncio.to_thread(sampling.freeze, comparison_id, drawn.items, deps.settings)
    )
    try:
        # Shielded, because a cancelled request cannot stop the thread: it
        # goes on writing the folder, which is dropped once it has finished.
        frozen = await asyncio.shield(freezing)
    except SampleError as err:
        # Short of space: nothing was kept on disk (freeze removes what it wrote).
        await db.rollback()
        raise HTTPException(409, str(err)) from None
    except BaseException:
        await db.rollback()
        _drop_when_done(freezing, comparison_id, deps.settings)
        raise
    try:
        await comparisons.set_pictures(db, comparison, frozen)
        entries = await comparisons.add_models(db, comparison, models, user_id=user.id)
        job_ids = [entry.job_id for entry in entries if entry.job_id is not None]
        builds = [_build(entry) for entry in entries]
        # Inside: a commit that fails (the database went away) leaves no row,
        # and the pictures must not outlive it with nothing to remove them.
        await db.commit()
    except comparisons.CompareRefused as err:
        await db.rollback()
        await asyncio.to_thread(_drop_quietly, comparison_id, deps.settings)
        raise HTTPException(409, str(err)) from None
    except BaseException:
        await db.rollback()
        await asyncio.to_thread(_drop_quietly, comparison_id, deps.settings)
        raise
    if not await _wake(deps):
        await _undo(db, comparison_id, job_ids, deps)
        raise HTTPException(503, QUEUE_DOWN)
    await auth_service.audit(
        db,
        "local_models.compare_started",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={
            "comparison": str(comparison_id),
            "source_kind": spec.kind,
            "items": len(drawn.items),
            "models": builds,
        },
    )
    await db.commit()
    log.info("local_models.compare_started", comparison=str(comparison_id), models=builds)
    return await _one(db, deps, user, comparison_id)


# --- 6.6 to 6.8 reading one ---------------------------------------------------------------


@router.get("/{comparison_id}", response_model=ComparisonOut)
async def one_comparison(
    comparison_id: str, user: CurrentUser, db: DbDep, deps: DepsDep
) -> ComparisonOut:
    """One comparison: its sample, its columns with their figures, and the
    decision. Small on purpose — the page asks every few seconds while a
    model runs; the items are a separate request."""
    found = await _comparison_or_404(db, comparison_id)
    return await _comparison_out(db, deps, user, found)


def _ruling(slug: object, yes: set[str], no: set[str]) -> Literal["yes", "no"] | None:
    if not isinstance(slug, str):
        return None
    if slug in yes:
        return "yes"
    if slug in no:
        return "no"
    return None


def _names(rows: object) -> tuple[list[str], set[str]]:
    names: list[str] = []
    slugs: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name, slug = row.get("name"), row.get("slug")
        if isinstance(name, str):
            names.append(name)
        if isinstance(slug, str):
            slugs.add(slug)
    return names, slugs


def _answer_out(row: ComparisonAnswer, yes: set[str], no: set[str]) -> AnswerOut:
    tags: list[AnswerTag] = []
    for tag in row.tags if isinstance(row.tags, list) else []:
        if not isinstance(tag, dict) or not isinstance(tag.get("name"), str):
            continue
        confidence = tag.get("confidence")
        tags.append(
            AnswerTag(
                name=tag["name"],
                confidence=None
                if isinstance(confidence, bool) or not isinstance(confidence, int | float)
                else float(confidence),
                in_vocabulary=tag.get("in_vocabulary") is True,
                ruling=_ruling(tag.get("slug"), yes, no),
            )
        )
    attempts = row.attempts or 1
    return AnswerOut(
        ok=bool(row.ok),
        problem=row.problem,
        message=row.message or "",
        tags=tags,
        caption=row.caption or "",
        score=row.score,
        reason=row.reason or "",
        seconds=row.seconds,
        attempts=attempts,
        # What it wrote, only where it is worth reading: a failure, a second try.
        raw=row.raw if (not row.ok or attempts > 1) else None,
    )


@router.get("/{comparison_id}/items", response_model=ItemsOut)
async def comparison_items(comparison_id: str, _user: CurrentUser, db: DbDep) -> ItemsOut:
    """Every item (at most 200): its frozen pictures, what was decided about
    it, and each column's answer by column id. A column with no answer for an
    item yet has no key in its `answers`."""
    comparison = await _comparison_or_404(db, comparison_id)
    items = await comparisons.items_of(db, comparison.id)
    entries = await comparisons.entries_of(db, comparison.id)
    answers = await comparisons.answers_of(db, [entry.id for entry in entries])
    over = comparisons.over_items(entries, answers)
    by_item: dict[uuid.UUID, list[ComparisonAnswer]] = {}
    for row in answers:
        by_item.setdefault(row.item_id, []).append(row)
    asset_ids = sorted({item.asset_id for item in items if item.asset_id is not None}, key=str)
    there: set[uuid.UUID] = set()
    for chunk in _chunks(asset_ids):
        found = await db.scalars(select(Asset.id).where(Asset.id.in_(list(chunk)), catalogued()))
        there.update(uuid.UUID(str(each)) for each in found.all())
    out: list[ItemOut] = []
    for item in items:
        yes_names, yes = _names(item.truth_yes)
        no_names, no = _names(item.truth_no)
        moments = [ms for ms in (item.picture_ms or []) if isinstance(ms, int)]
        out.append(
            ItemOut(
                id=item.id,
                position=item.position,
                kind=item.kind,
                asset_id=item.asset_id,
                in_catalogue=item.asset_id is not None and item.asset_id in there,
                media_type=item.media_type,
                filename=item.filename,
                library=item.library_name,
                folder=_folder_of(item.relative_path),
                start_s=None if item.start_ms is None else item.start_ms / 1000,
                end_s=None if item.end_ms is None else item.end_ms / 1000,
                facts_line=item.facts_line,
                pictures=[
                    PictureOut(
                        n=n,
                        at_s=ms / 1000,
                        url=f"{PREFIX}/{comparison.id}/items/{item.id}/pictures/{n}",
                    )
                    for n, ms in enumerate(moments)
                ],
                truth=ItemTruthOut(yes=yes_names, no=no_names),
                used=item.used,
                used_in=_strings(item.used_in),
                counted=str(item.id) in over,
                answers={
                    str(row.entry_id): _answer_out(row, yes, no) for row in by_item.get(item.id, [])
                },
            )
        )
    return ItemsOut(total=len(out), items=out)


@router.get("/{comparison_id}/items/{item_id}/pictures/{n}")
async def item_picture(
    comparison_id: str, item_id: str, n: str, _user: CurrentUser, db: DbDep, deps: DepsDep
) -> Response:
    """One frozen picture. Its path is worked out from the comparison's id and
    two whole numbers read from the database — nothing from the request
    reaches it."""
    comparison = await _comparison_or_404(db, comparison_id)
    parsed = _uuid(item_id)
    item = None if parsed is None else await db.get(ComparisonItem, parsed)
    if item is None or item.comparison_id != comparison.id:
        raise HTTPException(404, NO_SUCH_ITEM)
    count = len(item.picture_ms or [])
    # ASCII digits only: `str.isdigit` also passes "²", which `int` refuses.
    if _PICTURE_NUMBER.fullmatch(n) is None or int(n) >= count:
        raise HTTPException(404, NO_SUCH_PICTURE)
    path = sampling.picture_path(comparison.id, item.position, int(n), deps.settings)
    if not await asyncio.to_thread(path.is_file):
        raise HTTPException(404, NO_SUCH_PICTURE)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": PICTURE_CACHE})


# --- 6.9 changing a saved comparison ------------------------------------------------------


@router.post(
    "/{comparison_id}/models",
    response_model=ComparisonOut,
    status_code=202,
    dependencies=[require_admin],
)
async def add_models(
    comparison_id: str,
    body: ModelsBody,
    request: Request,
    user: CurrentUser,
    db: DbDep,
    deps: Changing,
) -> ComparisonOut:
    """Add models to a saved comparison: each faces the very sample the
    others did."""
    comparison = await _comparison_or_404(db, comparison_id)
    if not comparisons.is_current(comparison):
        raise HTTPException(409, comparisons.older(comparison.prompt_version))
    models = await _models_asked(db, deps, body.model_ids, adding=True)
    try:
        entries = await comparisons.add_models(db, comparison, models, user_id=user.id)
    except comparisons.CompareRefused as err:
        await db.rollback()
        raise HTTPException(409, str(err)) from None
    made = [entry.id for entry in entries]
    job_ids = [entry.job_id for entry in entries if entry.job_id is not None]
    builds = [_build(entry) for entry in entries]
    the_id = comparison.id
    await db.commit()
    if not await _wake(deps):
        await db.execute(delete(LocalAiJob).where(LocalAiJob.id.in_(job_ids)))
        for entry_id in made:
            entry = await db.get(ComparisonModel, entry_id, populate_existing=True)
            if entry is not None:
                await comparisons.remove_entry(db, entry, deps.now())
        await db.commit()
        raise HTTPException(503, QUEUE_DOWN)
    await auth_service.audit(
        db,
        "local_models.compare_model_added",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"comparison": str(the_id), "models": builds},
    )
    await db.commit()
    return await _one(db, deps, user, the_id)


@router.post(
    "/{comparison_id}/models/{entry_id}/run",
    response_model=ComparisonOut,
    status_code=202,
    dependencies=[require_admin],
)
async def run_model(
    comparison_id: str,
    entry_id: str,
    request: Request,
    user: CurrentUser,
    db: DbDep,
    deps: Changing,
    body: RunBody | None = None,
) -> ComparisonOut:
    """**Carry on** from the items it has no answer for, or with
    `retry_failed` **Try the failed ones again** (the turn deletes those
    answers when it starts, so they are asked again). A queue that is down
    leaves the column exactly as it was: "Nothing was started" is true."""
    comparison = await _comparison_or_404(db, comparison_id)
    entry = await _entry_or_404(db, comparison, entry_id)
    retry = False if body is None else body.retry_failed
    if not isinstance(retry, bool):
        raise HTTPException(400, BAD_RETRY)
    # A turn still under way is said as that, before anything about its model.
    running = (await comparisons.jobs_of(db, [entry])).get(entry.id)
    if comparisons.shown(entry, running, deps.now())[0] in comparisons.ACTIVE:
        raise HTTPException(409, comparisons.ALREADY_RUNNING)
    model = None if entry.model_id is None else await registry.get_model(db, entry.model_id)
    if model is not None and comparisons.is_current(comparison):
        problem = comparisons.may_compare(
            model, await load_config(db), deps.kinds, await _missing(deps)
        )
        if problem is not None:
            raise HTTPException(409, problem)
    before = {name: getattr(entry, name) for name in _CARRIED_ON}
    try:
        job = await comparisons.carry_on(
            db, entry, retry_failed=retry, user_id=user.id, now=deps.now()
        )
    except comparisons.CompareRefused as err:
        await db.rollback()
        raise HTTPException(409, str(err)) from None
    job_id, the_id, column = job.id, comparison.id, entry.id
    build = _build(entry)
    await db.commit()
    if not await _wake(deps):
        # The new job goes and the column is put back as it was — its state,
        # its sentence and details, when it finished (L6's gate reads that) —
        # so the page shows what it showed, with Carry on still offered. Its
        # answers were never touched: a retry deletes them only when its
        # turn starts.
        await db.execute(delete(LocalAiJob).where(LocalAiJob.id == job_id))
        restored = await db.get(ComparisonModel, column, populate_existing=True)
        if restored is not None:
            for name, value in before.items():
                setattr(restored, name, value)
        await db.commit()
        raise HTTPException(503, QUEUE_DOWN)
    await auth_service.audit(
        db,
        "local_models.compare_resumed",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"comparison": str(the_id), "model": build, "retry_failed": retry},
    )
    await db.commit()
    return await _one(db, deps, user, the_id)


@router.post(
    "/{comparison_id}/models/{entry_id}/cancel",
    response_model=ComparisonOut,
    dependencies=[require_admin],
)
async def cancel_model(
    comparison_id: str,
    entry_id: str,
    request: Request,
    user: CurrentUser,
    db: DbDep,
    deps: Changing,
) -> ComparisonOut:
    """A queued or waiting turn stops at once; a running one within one item
    (`cancelling` until then). What it had answered is kept."""
    comparison = await _comparison_or_404(db, comparison_id)
    entry = await _entry_or_404(db, comparison, entry_id)
    try:
        await comparisons.cancel(db, entry, deps.now())
    except comparisons.CompareRefused as err:
        await db.rollback()
        raise HTTPException(409, str(err)) from None
    the_id, build = comparison.id, _build(entry)
    await auth_service.audit(
        db,
        "local_models.compare_cancelled",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"comparison": str(the_id), "model": build},
    )
    await db.commit()
    return await _one(db, deps, user, the_id)


@router.delete(
    "/{comparison_id}/models/{entry_id}",
    response_model=ComparisonOut,
    dependencies=[require_admin],
)
async def remove_model(
    comparison_id: str,
    entry_id: str,
    request: Request,
    user: CurrentUser,
    db: DbDep,
    deps: Changing,
) -> ComparisonOut:
    """Take a column and its answers out. A decision that chose it is cleared
    (its note is kept)."""
    comparison = await _comparison_or_404(db, comparison_id)
    entry = await _entry_or_404(db, comparison, entry_id)
    the_id, build = comparison.id, _build(entry)
    try:
        await comparisons.remove_entry(db, entry, deps.now())
    except comparisons.CompareRefused as err:
        await db.rollback()
        raise HTTPException(409, str(err)) from None
    await auth_service.audit(
        db,
        "local_models.compare_model_removed",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"comparison": str(the_id), "model": build},
    )
    await db.commit()
    return await _one(db, deps, user, the_id)


@router.put("/{comparison_id}", response_model=ComparisonOut, dependencies=[require_admin])
async def change(
    comparison_id: str,
    body: ChangeBody,
    request: Request,
    user: CurrentUser,
    db: DbDep,
    deps: Changing,
) -> ComparisonOut:
    """Rename it, or record the decision: `chosen` with the column whose model
    looks, `none` (none is good enough yet) or "" (not decided), and a note.
    A field left out is left as it is. Saving a decision changes no role."""
    comparison = await _comparison_or_404(db, comparison_id)
    given = body.model_fields_set
    the_id = comparison.id
    renamed: tuple[str, str] | None = None
    try:
        if "name" in given:
            if not isinstance(body.name, str):
                raise comparisons.BadRequest(comparisons.GIVE_NAME)
            before = comparison.name
            await comparisons.rename(db, comparison, body.name)
            if comparison.name != before:
                renamed = (before, comparison.name)
        deciding = given & {"verdict", "entry_id", "note"}
        decided = False
        if deciding:
            verdict = body.verdict if "verdict" in given else comparison.verdict
            if not isinstance(verdict, str):
                raise comparisons.BadRequest(comparisons.NO_SUCH_DECISION)
            note = body.note if "note" in given else comparison.verdict_note
            if note is None:
                note = ""
            if not isinstance(note, str):
                raise comparisons.BadRequest(NOTE_AS_TEXT)
            if "entry_id" in given:
                chosen = None if body.entry_id is None else _uuid(body.entry_id)
                if body.entry_id is not None and chosen is None:
                    raise comparisons.BadRequest(comparisons.NOT_HERE)
            else:
                chosen = comparison.verdict_entry_id
            before_decision = (
                comparison.verdict,
                comparison.verdict_entry_id,
                comparison.verdict_note,
            )
            await comparisons.decide(
                db,
                comparison,
                verdict=verdict,
                entry_id=chosen if verdict == "chosen" else None,
                note=note,
                user_id=user.id,
            )
            decided = before_decision != (
                comparison.verdict,
                comparison.verdict_entry_id,
                comparison.verdict_note,
            )
    except comparisons.BadRequest as err:
        await db.rollback()
        raise HTTPException(400, str(err)) from None
    ip = client_ip(request)
    if renamed is not None:
        await auth_service.audit(
            db,
            "local_models.compare_renamed",
            actor_user_id=user.id,
            ip=ip,
            detail={"comparison": str(the_id), "from": renamed[0], "to": renamed[1]},
        )
    if decided:
        chosen_entry = (
            None
            if comparison.verdict_entry_id is None
            else await db.get(ComparisonModel, comparison.verdict_entry_id)
        )
        await auth_service.audit(
            db,
            "local_models.compare_decided",
            actor_user_id=user.id,
            ip=ip,
            detail={
                "comparison": str(the_id),
                "verdict": comparison.verdict,
                "model": None if chosen_entry is None else _build(chosen_entry),
            },
        )
    await db.commit()
    return await _one(db, deps, user, the_id)


@router.delete("/{comparison_id}", status_code=204, dependencies=[require_admin])
async def delete_comparison(
    comparison_id: str,
    request: Request,
    user: CurrentUser,
    db: DbDep,
    deps: Changing,
) -> Response:
    """Delete the comparison: its rows, then its copies of the pictures.
    Nothing in the catalogue changes."""
    comparison = await _comparison_or_404(db, comparison_id)
    the_id, name = comparison.id, comparison.name
    try:
        await comparisons.delete(db, comparison, deps.now())
    except comparisons.CompareRefused as err:
        await db.rollback()
        raise HTTPException(409, str(err)) from None
    await auth_service.audit(
        db,
        "local_models.compare_deleted",
        actor_user_id=user.id,
        ip=client_ip(request),
        detail={"comparison": str(the_id), "name": name},
    )
    await db.commit()
    await asyncio.to_thread(_drop_quietly, the_id, deps.settings)
    return Response(status_code=204)
