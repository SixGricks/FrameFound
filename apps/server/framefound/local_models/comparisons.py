"""Comparisons on the database: the rows of a comparison, the turns of its
models, and what a page reads off them.

A comparison is made once from a drawn sample (`create`) and never redrawn:
its items, the sentence each is told, the vocabulary and the ground truth
are copies as of that moment. Each model added to it is a column
(`ComparisonModel`) with one job on the `local_ai` lane that asks it every
item (`compare_job`). The column's own row is the record; the job row is
only how the turn is run, and may be pruned, so what a page shows is worked
out from both (`shown`): a worker that died reads as stopped, not as running
for ever.

The figures (`measures`) are counts with what they are counts of, counted
over the items every finished column answered, so no column is counted on an
easier or a harder set than its neighbour. They say how far a model agrees
with decisions already made; they never say which model is good, and nothing
here ranks, orders or marks a column by them.

Nothing here writes to the catalogue. Every function that writes flushes and
leaves the commit to the caller, so a change and its audit row are one
transaction; the frozen pictures on disk are the caller's
(`sampling.freeze` / `drop_pictures`).
"""

import statistics
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete as delete_rows
from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.config import Settings
from framefound.db.models import (
    Comparison,
    ComparisonAnswer,
    ComparisonItem,
    ComparisonModel,
    LocalAiJob,
    LocalModel,
)
from framefound.local_models import agreement, ask, jobs, registry
from framefound.local_models.kinds import RuntimeKind
from framefound.local_models.sampling import Drawn, SampleSpec
from framefound.local_models.settings_store import LocalModelsConfig
from framefound.looking import prompt

# The job kind of one model's turn. Registered by `compare_job` (imported at
# the foot of `handlers`); kept here because this module makes the jobs.
KIND = "compare_model"
# Six, because a seventh column cannot be read.
MAX_MODELS = 6
# A comparison's turns run at "a person asked for it": a Test (0) goes
# first, background looking (9) waits.
PRIORITY = 5
# A column whose turn has not finished.
ACTIVE = ("queued", "running", "waiting")
FINISHED = ("done", "stopped", "failed")
# In a turn's payload: delete the column's failed answers when the turn
# starts. Not when Try again is pressed: a queue that turns out to be down
# then answers "Nothing was started", and that must be true.
RETRY_FAILED = "retry_failed"
# The problem of an item the model was never shown (its picture is gone or
# damaged): recorded, but the model did nothing with it.
UNSHOWN = "no_picture"
NAME_CHARS = 200
NOTE_CHARS = 2000
VERDICTS = ("", "chosen", "none")

CANCELLED = "Cancelled. What it had answered is kept."
WAITING_TURN = "Waiting its turn."
# The column's own words for a worker that died, and for a job row that is
# gone: what it had answered is kept, and Carry on starts where it stopped.
STOPPED = (
    "It stopped without finishing: the worker running it was restarted. "
    "What it had answered is kept."
)
LOST = "It stopped without finishing. What it had answered is kept."
TOO_MANY = "A comparison holds at most six models."
ALREADY_RUNNING = "It is already running."
ANSWERED_ALL = "It has answered everything."
NOT_INSTALLED = "This model is no longer installed."
NOT_RUNNING = "It is not running."
STILL_RUNNING = "It is still running. Cancel it first."
MODEL_RUNNING = "A model is still running. Cancel it first."
SAY_WHICH = "Say which model."
NOT_HERE = "That model is not in this comparison."
NO_SUCH_DECISION = "Choose a model, none of them, or no decision yet."
GIVE_NAME = "Give it a name."
NAME_TOO_LONG = f"Keep the name under {NAME_CHARS} characters."
NOTE_TOO_LONG = f"Keep the note under {NOTE_CHARS:,} characters."


class CompareRefused(RuntimeError):
    """Not possible as things stand; the message is the sentence (HTTP 409)."""


class BadRequest(ValueError):
    """The request cannot be done as written; the message is the sentence (HTTP 400)."""


def older(version: str) -> str:
    """Why a comparison made under another question cannot be added to."""
    return (
        f"Made with an earlier version of the question ({version}). "
        "It can be read but not added to."
    )


def is_current(comparison: Comparison) -> bool:
    """Was it made with the question this build asks? Only then may it be added to."""
    return comparison.prompt_version == prompt.PROMPT_VERSION


# --- names -----------------------------------------------------------------------


def name_of(model: LocalModel) -> str:
    """What a column calls its model: the repository's name, and the build
    inside it when the repository holds several."""
    name = model.repo_id.rpartition("/")[2] or model.repo_id
    return f"{name} {model.variant}" if model.variant else name


def build_of(model: LocalModel) -> str:
    """ "gemma-4-e4b-it-4bit (build 475b908)": a model, as one build of it."""
    return f"{name_of(model)} (build {model.revision[:7]})"


def subject(comparison_id: uuid.UUID, entry_id: uuid.UUID) -> str:
    """The job's subject, `<comparison id>:<entry id>`: one turn of one column,
    so a second Carry on pressed while the first is queued is refused."""
    return f"{comparison_id}:{entry_id}"


# --- may this model be compared ----------------------------------------------------


def missing_programs(kinds: Mapping[str, RuntimeKind], settings: Settings) -> dict[str, str]:
    """Each of the install's programs that is not installed here, by kind name,
    with the sentence a model's turn on it would stop with. Disk work: call it
    in a thread, once per request."""
    found: dict[str, str] = {}
    for name in kinds:
        problem = ask.program_problem(name, settings, kinds)
        if problem is not None:
            found[name] = problem
    return found


def compare_problem(
    model: LocalModel,
    config: LocalModelsConfig,
    known_kinds: Collection[str],
    missing: Mapping[str, str] | None = None,
) -> str | None:
    """None when the model may be compared; else why not, as the AI models
    page says it ("It does not read pictures."). The Looker's rule: a model
    is compared on the question the Looker will ask.

    Unlike a role, a comparison runs the model now, so its program must be
    installed too: `missing` is `missing_programs`, and a model on one of
    them is refused with the sentence its turn would otherwise have stopped
    with straight away.
    """
    problem = registry.role_problem(model, "looker", config.lists(), config.budgets(), known_kinds)
    if problem is None and missing is not None:
        problem = missing.get(model.runtime)
    return problem


def may_compare(
    model: LocalModel,
    config: LocalModelsConfig,
    known_kinds: Collection[str],
    missing: Mapping[str, str] | None = None,
) -> str | None:
    """None when the model may be compared; else the sentence naming it, for a
    refusal that has to say which of several ticked models it was
    ("gemma-4-12B-it-OptiQ-4bit does not read pictures.")."""
    problem = compare_problem(model, config, known_kinds, missing)
    if problem is None:
        return None
    name = name_of(model)
    if problem.startswith("Its "):
        return f"{name}'s {problem[4:]}"
    if problem.startswith("It "):
        return f"{name} {problem[3:]}"
    return f"{name}: {problem}"


# --- making a comparison --------------------------------------------------------------


def _source(spec: SampleSpec) -> dict[str, Any]:
    """The sample as asked, ids as text: a record of the request, not a key."""
    if spec.kind == "folder":
        return {
            "kind": "folder",
            "library_id": None if spec.library_id is None else str(spec.library_id),
            "folder": spec.folder,
            "truth_cut_ids": (
                None if spec.truth_cut_ids is None else [str(i) for i in spec.truth_cut_ids]
            ),
        }
    if spec.kind == "listing":
        return {
            "kind": "listing",
            "listing_id": None if spec.listing_id is None else str(spec.listing_id),
        }
    if spec.kind == "cuts":
        return {
            "kind": "cuts",
            "cut_ids": [str(i) for i in spec.cut_ids],
            "neighbours": spec.neighbours,
        }
    return {
        "kind": "comparison",
        "comparison_id": None if spec.comparison_id is None else str(spec.comparison_id),
    }


def _counts(drawn: Drawn) -> dict[str, Any]:
    """What the sample holds and what truth there is to check against. Tags
    belong to footage, not to a shot of it, so the decided tags are counted
    once per piece of footage."""
    items = drawn.items
    shots = [item for item in items if item.kind == "shot"]
    decided: dict[uuid.UUID, tuple[set[str], set[str]]] = {}
    for item in items:
        yes, no = decided.setdefault(item.asset_id, (set(), set()))
        yes.update(slug for slug, _name in item.truth_yes)
        no.update(slug for slug, _name in item.truth_no)
    with_tags = [pair for pair in decided.values() if pair[0] or pair[1]]
    return {
        "photos": sum(1 for item in items if item.kind == "photo"),
        "shots": len(shots),
        "clips": len({shot.asset_id for shot in shots}),
        "used": sum(1 for shot in shots if shot.used is True),
        "unused": sum(1 for shot in shots if shot.used is False),
        "footage_with_tags": len(with_tags),
        "yes": sum(len(yes) for yes, _no in with_tags),
        "no": sum(len(no - yes) for yes, no in with_tags),
        "pool": dict(drawn.pool),
        "left_out": dict(drawn.left_out),
    }


def counts_of(drawn: Drawn) -> dict[str, Any]:
    """What a drawn sample holds and what truth it carries, as `create` records
    it on the comparison (`counts`): the preview says the same before Start."""
    return _counts(drawn)


def _default_name(label: str, moment: datetime) -> str:
    when = f"{moment.day} {moment:%b %Y}"
    room = NAME_CHARS - len(when) - 2
    shown = label if len(label) <= room else label[: room - 1] + "…"
    return f"{shown}, {when}"


def clean_name(name: str | None) -> str:
    """A name as given, on one line. BadRequest when blank or too long."""
    text = " ".join((name or "").split())
    if not text:
        raise BadRequest(GIVE_NAME)
    if len(text) > NAME_CHARS:
        raise BadRequest(NAME_TOO_LONG)
    return text


async def create(
    db: AsyncSession,
    spec: SampleSpec,
    drawn: Drawn,
    *,
    name: str | None,
    user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> Comparison:
    """The comparison and its items, as drawn; flushed.

    `spec` is the request as asked (recorded as the source); what was
    actually drawn — the what, size, stills and seed every value filled in —
    is `drawn.spec`. Item `i` is position `i`, as `sampling.freeze` numbers
    the pictures. An item's `picture_ms` starts as the pictures the draw
    found; once the caller has frozen them, `set_pictures` records the ones
    actually written. No name means the sample's label and the date.

    The caller freezes the pictures, adds the models and commits.
    """
    moment = now if now is not None else datetime.now().astimezone()
    truth_at = moment.astimezone(UTC)
    filled = drawn.spec
    given = name is not None and name.strip() != ""
    comparison = Comparison(
        name=clean_name(name) if given else _default_name(drawn.label, moment),
        source_kind=spec.kind,
        source=_source(spec),
        source_label=drawn.label[:300],
        what=filled.what or "both",
        size=len(drawn.items),
        stills=filled.stills or 3,
        seed=filled.seed,
        prompt_version=prompt.PROMPT_VERSION,
        vocabulary=[[word, count] for word, count in drawn.vocabulary],
        counted_cuts=[
            {"id": str(cut_id), "title": title, "source": source}
            for cut_id, title, source, _when in drawn.counted_cuts
        ],
        counts=_counts(drawn),
        truth_at=truth_at,
        created_by=user_id,
        # Written here rather than by the database: the list is newest first,
        # and SQLite's clock has whole seconds.
        created_at=datetime.now(UTC),
    )
    db.add(comparison)
    await db.flush()
    for position, item in enumerate(drawn.items):
        db.add(
            ComparisonItem(
                comparison_id=comparison.id,
                position=position,
                kind=item.kind,
                asset_id=item.asset_id,
                library_name=item.library_name[:200],
                relative_path=item.relative_path[:2048],
                filename=item.filename[:512],
                media_type=item.media_type[:10],
                start_ms=item.start_ms,
                end_ms=item.end_ms,
                facts=dict(item.facts),
                facts_line=item.facts_line[:400],
                picture_ms=[moment_ms for moment_ms, _path in item.pictures],
                truth_yes=[{"slug": slug, "name": word} for slug, word in item.truth_yes],
                truth_no=[{"slug": slug, "name": word} for slug, word in item.truth_no],
                used=item.used,
                used_in=list(item.used_in),
            )
        )
    await db.flush()
    return comparison


async def set_pictures(
    db: AsyncSession, comparison: Comparison, frozen: Sequence[Sequence[int]]
) -> None:
    """Record, per position, the moments of the pictures `sampling.freeze`
    actually wrote (one that would not decode is not there). Flushed."""
    for item in await items_of(db, comparison.id):
        if item.position < len(frozen):
            item.picture_ms = [int(ms) for ms in frozen[item.position]]
    await db.flush()


async def add_models(
    db: AsyncSession,
    comparison: Comparison,
    models: Sequence[LocalModel],
    *,
    user_id: uuid.UUID | None,
) -> list[ComparisonModel]:
    """One column and one queued job for each model, in the order given;
    flushed. The caller has asked `may_compare` of each, commits, and then
    wakes the lane.

    CompareRefused: the comparison was made with an earlier question; it
    would hold more than MAX_MODELS; a build is already in it (or given
    twice).
    """
    if not is_current(comparison):
        raise CompareRefused(older(comparison.prompt_version))
    entries = await entries_of(db, comparison.id)
    if len(entries) + len(models) > MAX_MODELS:
        raise CompareRefused(TOO_MANY)
    builds = {(e.model, e.model_revision, e.variant) for e in entries}
    for model in models:
        build = (model.repo_id, model.revision, model.variant)
        if build in builds:
            raise CompareRefused(f"{build_of(model)} is already in this comparison.")
        builds.add(build)

    made: list[ComparisonModel] = []
    for model in models:
        entry = ComparisonModel(
            comparison_id=comparison.id,
            model_id=model.id,
            model=model.repo_id,
            model_revision=model.revision,
            variant=model.variant,
            model_name=name_of(model)[:200],
            prompt_version=comparison.prompt_version,
            state="queued",
            message="",
            added_by=user_id,
            # The order of the columns, to the microsecond (SQLite's own
            # clock would give every model added in one request one second).
            created_at=datetime.now(UTC),
        )
        db.add(entry)
        try:
            await db.flush()
        except IntegrityError as err:
            # Another request added the same build a moment ago. The session
            # is unusable until the caller rolls it back.
            raise CompareRefused(f"{build_of(model)} is already in this comparison.") from err
        job = await _queue(db, comparison, entry, model.id, name_of(model), user_id)
        entry.job_id = job.id
        made.append(entry)
    await db.flush()
    return made


async def _queue(
    db: AsyncSession,
    comparison: Comparison,
    entry: ComparisonModel,
    model_id: uuid.UUID,
    model_name: str,
    user_id: uuid.UUID | None,
    *,
    retry_failed: bool = False,
) -> LocalAiJob:
    payload: dict[str, Any] = {
        "comparison_id": str(comparison.id),
        "entry_id": str(entry.id),
        "model_name": model_name,
        "comparison_name": comparison.name,
    }
    if retry_failed:
        payload[RETRY_FAILED] = True
    try:
        return await jobs.create(
            db,
            KIND,
            subject=subject(comparison.id, entry.id),
            model_id=model_id,
            payload=payload,
            priority=PRIORITY,
            user_id=user_id,
        )
    except jobs.JobRefused as err:
        raise CompareRefused(ALREADY_RUNNING) from err


# --- changing a column ------------------------------------------------------------------


async def carry_on(
    db: AsyncSession,
    entry: ComparisonModel,
    *,
    retry_failed: bool,
    user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> LocalAiJob:
    """Run this column's turn again from the items it has no answer for:
    **Carry on**, or with `retry_failed` **Try the failed ones again** (the
    turn deletes its failed answers when it starts, so those items are
    asked again; until then, and if it never starts, they are kept).
    Flushed; the caller commits and wakes the lane, and puts the column
    back as it was if the lane cannot be woken.

    A turn whose worker died reads as stopped and may be carried on: its
    job row is ended first, the way the lane's tick would. So may a column
    that has an answer for every item but never closed `done` (its worker
    died, or the last write failed, after the last answer): its turn asks
    nothing and closes it.

    CompareRefused: it is still active; its model is no longer installed;
    the comparison was made with an earlier question; it is done and there
    is nothing left to ask. Whether the model is still approved is the
    caller's question (`may_compare`), as when it was added.
    """
    job = await _job(db, entry)
    if shown(entry, job, now)[0] in ACTIVE:
        raise CompareRefused(ALREADY_RUNNING)
    model = None if entry.model_id is None else await registry.get_model(db, entry.model_id)
    if model is None:
        raise CompareRefused(NOT_INSTALLED)
    comparison = await db.get(Comparison, entry.comparison_id)
    if comparison is None:
        raise CompareRefused(NOT_INSTALLED)
    if not is_current(comparison):
        raise CompareRefused(older(comparison.prompt_version))
    if entry.prompt_version != comparison.prompt_version:
        raise CompareRefused(older(entry.prompt_version))
    failed = await _count_answers(db, entry.id, ok=False)
    left = len(await unanswered(db, entry))
    if left == 0 and not (retry_failed and failed) and entry.state == "done":
        raise CompareRefused(ANSWERED_ALL)

    if job is not None and job.state == "running":
        # Dead (shown said so): end it now, so the new turn is not refused
        # as "already waiting its turn" by its unfinished row.
        await jobs.reap(db, job, now)
    entry.done = await _count_answers(db, entry.id, ok=True)
    entry.failed = failed
    fresh = await _queue(
        db,
        comparison,
        entry,
        model.id,
        entry.model_name or name_of(model),
        user_id,
        retry_failed=retry_failed and failed > 0,
    )
    entry.state = "queued"
    entry.message = ""
    entry.detail = None
    entry.not_before = None
    entry.finished_at = None
    entry.job_id = fresh.id
    await db.flush()
    return fresh


async def cancel(db: AsyncSession, entry: ComparisonModel, now: datetime | None = None) -> None:
    """Cancel this column's turn. A queued or waiting turn stops at once; a
    running one is asked to, and stops within one item (its handler then
    closes the column). What it had answered is kept. Flushed.

    CompareRefused(NOT_RUNNING) when its turn is not active."""
    job = await _job(db, entry)
    if job is None or shown(entry, job, now)[0] not in ACTIVE:
        raise CompareRefused(NOT_RUNNING)
    try:
        job = await jobs.request_cancel(db, job.id)
    except (jobs.NoSuchJob, jobs.JobRefused) as err:
        raise CompareRefused(NOT_RUNNING) from err
    if job.state == "cancelled":
        _close(entry, "stopped", CANCELLED)
    await db.flush()


async def remove_entry(
    db: AsyncSession, entry: ComparisonModel, now: datetime | None = None
) -> None:
    """Take a column and its answers out of the comparison. A decision that
    named this column is cleared (its note is kept). Flushed.

    CompareRefused(STILL_RUNNING) while its turn is active."""
    job = await _job(db, entry)
    if shown(entry, job, now)[0] in ACTIVE:
        raise CompareRefused(STILL_RUNNING)
    if job is not None and job.state == "running":
        await jobs.reap(db, job, now)
    comparison = await db.get(Comparison, entry.comparison_id)
    if comparison is not None and comparison.verdict_entry_id == entry.id:
        comparison.verdict = ""
        comparison.verdict_entry_id = None
        comparison.verdict_at = None
        comparison.verdict_by = None
    # Each table by hand: SQLite does not enforce the cascades in the tests,
    # and the code never relies on one having happened.
    await db.execute(delete_rows(ComparisonAnswer).where(ComparisonAnswer.entry_id == entry.id))
    await db.delete(entry)
    await db.flush()


async def delete(db: AsyncSession, comparison: Comparison, now: datetime | None = None) -> None:
    """Delete the comparison's rows. Flushed; the caller commits, then removes
    the pictures (`sampling.drop_pictures`). Job rows are history and stay.

    CompareRefused(MODEL_RUNNING) while any column's turn is active."""
    entries = await entries_of(db, comparison.id)
    found = await jobs_of(db, entries)
    for entry in entries:
        if shown(entry, found.get(entry.id), now)[0] in ACTIVE:
            raise CompareRefused(MODEL_RUNNING)
    for job in found.values():
        if job.state == "running":
            await jobs.reap(db, job, now)
    ids = [entry.id for entry in entries]
    if ids:
        await db.execute(delete_rows(ComparisonAnswer).where(ComparisonAnswer.entry_id.in_(ids)))
        await db.execute(delete_rows(ComparisonModel).where(ComparisonModel.id.in_(ids)))
    await db.execute(
        delete_rows(ComparisonItem).where(ComparisonItem.comparison_id == comparison.id)
    )
    await db.delete(comparison)
    await db.flush()


async def decide(
    db: AsyncSession,
    comparison: Comparison,
    *,
    verdict: str,
    entry_id: uuid.UUID | None,
    note: str,
    user_id: uuid.UUID | None,
    now: datetime | None = None,
) -> None:
    """Record the owner's decision: `chosen` (this column's model looks),
    `none` (none is good enough yet) or "" (no decision yet), with a note.
    A record only: it changes no role. Flushed.

    BadRequest: an unknown verdict; `chosen` with no column; a column that
    is not this comparison's; a note too long."""
    if verdict not in VERDICTS:
        raise BadRequest(NO_SUCH_DECISION)
    text = (note or "").strip()
    if len(text) > NOTE_CHARS:
        raise BadRequest(NOTE_TOO_LONG)
    chosen: uuid.UUID | None = None
    if verdict == "chosen":
        if entry_id is None:
            raise BadRequest(SAY_WHICH)
        entry = await db.get(ComparisonModel, entry_id)
        if entry is None or entry.comparison_id != comparison.id:
            raise BadRequest(NOT_HERE)
        chosen = entry.id
    comparison.verdict = verdict
    comparison.verdict_entry_id = chosen
    comparison.verdict_note = text
    if verdict:
        comparison.verdict_at = (now or datetime.now(UTC)).astimezone(UTC)
        comparison.verdict_by = user_id
    else:
        comparison.verdict_at = None
        comparison.verdict_by = None
    await db.flush()


async def rename(db: AsyncSession, comparison: Comparison, name: str) -> None:
    """Give the comparison the owner's name. BadRequest when blank or too long."""
    comparison.name = clean_name(name)
    await db.flush()


def _close(entry: ComparisonModel, state: str, message: str) -> None:
    entry.state = state
    entry.message = message[:500]
    entry.not_before = None


# --- reading ------------------------------------------------------------------------------


def _utc(moment: datetime | None) -> datetime | None:
    """A moment read from the table, in UTC: SQLite hands back what was
    written without its zone, and everything written here is UTC."""
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def shown(
    entry: ComparisonModel, job: LocalAiJob | None, now: datetime | None = None
) -> tuple[str, str, datetime | None]:
    """(state, message, not_before) as the page should read a column.

    A finished column (done, stopped, failed) is itself. Otherwise its job
    decides, read through `jobs.shown`: a worker that died, or a job row that
    is gone, reads as stopped — never as running for ever — and the column
    can be carried on from where it got to.
    """
    if entry.state in FINISHED:
        return entry.state, entry.message, None
    if job is None:
        return "stopped", LOST, None
    state, message = jobs.shown(job, now)
    if state == "queued":
        # The column's own message only when the column put itself back in
        # the queue ("Stepped aside…"). A column still `waiting` whose job was
        # released (Resume, the overnight window opening) would otherwise
        # still say "Local AI is paused." until a worker took it.
        own = entry.message if entry.state == "queued" else ""
        return "queued", own or WAITING_TURN, None
    if state == "waiting":
        return "waiting", message or entry.message, _utc(job.not_before or entry.not_before)
    if state == "running":
        return "running", entry.message, None
    if state == "cancelled":
        return "stopped", CANCELLED, None
    if state == "failed":
        if message == jobs.STOPPED:
            return "stopped", STOPPED, None
        return "failed", message, None
    # `done` with the column still open: the handler returned without
    # closing it, which it should not do. Read as stopped, so it can carry on.
    return "stopped", LOST, None


def cancelling(job: LocalAiJob | None) -> bool:
    """Cancel was pressed and the running turn has not noticed yet."""
    return job is not None and job.state == "running" and bool(job.cancel_requested)


def overall(states: Sequence[str]) -> str:
    """A comparison's state for the list, from its columns' shown states:
    `running` (any queued or running), `waiting` (none running, some
    waiting), `stopped` (some stopped or failed, none active), `finished`,
    or `empty` (no columns)."""
    if not states:
        return "empty"
    if any(state in ("queued", "running") for state in states):
        return "running"
    if "waiting" in states:
        return "waiting"
    if any(state in ("stopped", "failed") for state in states):
        return "stopped"
    return "finished"


async def entries_of(db: AsyncSession, comparison_id: uuid.UUID) -> list[ComparisonModel]:
    """The columns, in the order they were added."""
    rows = await db.scalars(
        select(ComparisonModel)
        .where(ComparisonModel.comparison_id == comparison_id)
        .order_by(ComparisonModel.created_at, ComparisonModel.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


async def items_of(db: AsyncSession, comparison_id: uuid.UUID) -> list[ComparisonItem]:
    """The items, by position."""
    rows = await db.scalars(
        select(ComparisonItem)
        .where(ComparisonItem.comparison_id == comparison_id)
        .order_by(ComparisonItem.position)
    )
    return list(rows.all())


async def answers_of(db: AsyncSession, entry_ids: Collection[uuid.UUID]) -> list[ComparisonAnswer]:
    """Every answer of these columns."""
    if not entry_ids:
        return []
    rows = await db.scalars(
        select(ComparisonAnswer)
        .where(ComparisonAnswer.entry_id.in_(list(entry_ids)))
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


async def unanswered(db: AsyncSession, entry: ComparisonModel) -> list[ComparisonItem]:
    """The items this column has no answer row for, by position: where every
    turn starts — a first run, a second delivery, a restart, a step aside
    and Carry on alike."""
    answered = select(ComparisonAnswer.id).where(
        ComparisonAnswer.entry_id == entry.id,
        ComparisonAnswer.item_id == ComparisonItem.id,
    )
    rows = await db.scalars(
        select(ComparisonItem)
        .where(ComparisonItem.comparison_id == entry.comparison_id, ~exists(answered))
        .order_by(ComparisonItem.position)
    )
    return list(rows.all())


async def _count_answers(db: AsyncSession, entry_id: uuid.UUID, *, ok: bool) -> int:
    found = await db.scalar(
        select(func.count())
        .select_from(ComparisonAnswer)
        .where(ComparisonAnswer.entry_id == entry_id, ComparisonAnswer.ok.is_(ok))
    )
    return int(found or 0)


async def _job(db: AsyncSession, entry: ComparisonModel) -> LocalAiJob | None:
    if entry.job_id is None:
        return None
    return await db.get(LocalAiJob, entry.job_id, populate_existing=True)


async def jobs_of(
    db: AsyncSession, entries: Sequence[ComparisonModel]
) -> dict[uuid.UUID, LocalAiJob]:
    """Each column's job as the table has it now, by column id; a column whose
    job row is gone (pruned) is not in it."""
    wanted = {entry.job_id: entry.id for entry in entries if entry.job_id is not None}
    if not wanted:
        return {}
    rows = await db.scalars(
        select(LocalAiJob)
        .where(LocalAiJob.id.in_(list(wanted)))
        .execution_options(populate_existing=True)
    )
    return {wanted[job.id]: job for job in rows.all()}


# --- the figures ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Measures:
    """What one column's answers come to. Counts with what they are counts
    of — never a share, a rank or a grade."""

    over: int  # items every finished column answered: what tags and scores are counted on
    form: agreement.Form  # over everything it was asked
    shots: agreement.Pace
    photos: agreement.Pace
    # One per agreement.FLOORS; None until the column is done, or when the
    # sample has no decided tag.
    tags: tuple[agreement.TagAgreement, ...] | None
    # None until the column is done, or with no used and unused shot to set apart.
    scores: agreement.ScoreAgreement | None


def truth_of(items: Sequence[ComparisonItem]) -> list[agreement.ItemTruth]:
    """What was decided about each item, as frozen on it."""
    return [
        agreement.ItemTruth(
            item=str(item.id),
            footage=str(item.asset_id if item.asset_id is not None else item.id),
            kind=item.kind,
            yes=frozenset(_slugs(item.truth_yes)),
            no=frozenset(_slugs(item.truth_no)),
            used=item.used,
        )
        for item in items
    ]


def _slugs(rows: object) -> list[str]:
    if not isinstance(rows, list):
        return []
    return [
        row["slug"] for row in rows if isinstance(row, dict) and isinstance(row.get("slug"), str)
    ]


def answer_of(row: ComparisonAnswer) -> agreement.ItemAnswer:
    """A stored answer as the counts take it. A tag that does not read as one
    is skipped, never guessed at."""
    tags: list[tuple[str, float, bool]] = []
    for tag in row.tags if isinstance(row.tags, list) else []:
        if not isinstance(tag, dict):
            continue
        slug, confidence = tag.get("slug"), tag.get("confidence")
        if not isinstance(slug, str) or not slug:
            continue
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        tags.append((slug, float(confidence), tag.get("in_vocabulary") is True))
    return agreement.ItemAnswer(
        item=str(row.item_id),
        ok=bool(row.ok),
        tags=tuple(tags),
        score=row.score,
        seconds=row.seconds,
        attempts=row.attempts or 1,
        problem=row.problem,
    )


def count(
    items: Sequence[ComparisonItem],
    entries: Sequence[ComparisonModel],
    answers: Sequence[ComparisonAnswer],
) -> dict[uuid.UUID, Measures]:
    """The figures of every column, from rows already read. Tags and scores
    are counted over the items every `done` column answered, and only for a
    `done` column: a model still running shows its progress and pace."""
    truth = truth_of(items)
    kinds = {str(item.id): item.kind for item in items}
    by_entry: dict[uuid.UUID, dict[str, agreement.ItemAnswer]] = {e.id: {} for e in entries}
    for row in answers:
        if row.entry_id in by_entry:
            by_entry[row.entry_id][str(row.item_id)] = answer_of(row)
    finished = {str(e.id): by_entry[e.id] for e in entries if e.state == "done"}
    over = agreement.common(finished)
    out: dict[uuid.UUID, Measures] = {}
    for entry in entries:
        given = by_entry[entry.id]
        tags: tuple[agreement.TagAgreement, ...] | None = None
        scores: agreement.ScoreAgreement | None = None
        if entry.state == "done":
            floors = [
                agreement.tag_agreement(truth, given, over=over, floor=floor)
                for floor in agreement.FLOORS
            ]
            tags = tuple(f for f in floors if f is not None) or None
            scores = agreement.score_agreement(truth, given, over=over)
        out[entry.id] = Measures(
            over=len(over),
            form=agreement.form(given, asked=len(items)),
            shots=agreement.pace(given, kinds, "shot"),
            photos=agreement.pace(given, kinds, "photo"),
            tags=tags,
            scores=scores,
        )
    return out


async def measures(db: AsyncSession, comparison: Comparison) -> dict[uuid.UUID, Measures]:
    """Every column's figures, by column id (see `count`)."""
    items = await items_of(db, comparison.id)
    entries = await entries_of(db, comparison.id)
    answers = await answers_of(db, [entry.id for entry in entries])
    return count(items, entries, answers)


def over_items(
    entries: Sequence[ComparisonModel], answers: Sequence[ComparisonAnswer]
) -> frozenset[str]:
    """The ids (as text) of the items every `done` column answered: the set the
    figures are counted on, for marking which items count."""
    done = {entry.id for entry in entries if entry.state == "done"}
    by_entry: dict[str, dict[str, agreement.ItemAnswer]] = {str(e): {} for e in done}
    for row in answers:
        if row.entry_id in done:
            by_entry[str(row.entry_id)][str(row.item_id)] = answer_of(row)
    return agreement.common(by_entry)


async def last_finished_for(db: AsyncSession, model_id: uuid.UUID) -> datetime | None:
    """When a column of this model last finished (`done`) with at least one
    answer stored; None if none ever has. "Has this model been through
    Compare?" — the one question L6's gate asks of this module.

    An answer is something the model was asked: a bad answer or a timeout
    counts, an item whose picture was gone (it was never shown one) does
    not — a column that ran over a folder of missing pictures has not
    been through anything."""
    with_answers = exists().where(
        ComparisonAnswer.entry_id == ComparisonModel.id,
        or_(ComparisonAnswer.problem.is_(None), ComparisonAnswer.problem != UNSHOWN),
    )
    found = await db.scalar(
        select(ComparisonModel.finished_at)
        .where(
            ComparisonModel.model_id == model_id,
            ComparisonModel.state == "done",
            ComparisonModel.finished_at.is_not(None),
            with_answers,
        )
        .order_by(ComparisonModel.finished_at.desc())
        .limit(1)
    )
    return _utc(found)


async def seconds_hint(db: AsyncSession, model_id: uuid.UUID) -> float | None:
    """About how long this model takes over one item: the middle of its answer
    times in the newest comparison it finished, else its newest Test's time.
    None when it has done neither. A hint for the builder, not a promise."""
    newest = await db.scalar(
        select(ComparisonModel.id)
        .where(
            ComparisonModel.model_id == model_id,
            ComparisonModel.state == "done",
            exists().where(
                ComparisonAnswer.entry_id == ComparisonModel.id,
                ComparisonAnswer.ok.is_(True),
                ComparisonAnswer.seconds.is_not(None),
            ),
        )
        .order_by(ComparisonModel.finished_at.desc(), ComparisonModel.created_at.desc())
        .limit(1)
    )
    if newest is not None:
        times = (
            await db.scalars(
                select(ComparisonAnswer.seconds).where(
                    ComparisonAnswer.entry_id == newest,
                    ComparisonAnswer.ok.is_(True),
                    ComparisonAnswer.seconds.is_not(None),
                )
            )
        ).all()
        given = [float(t) for t in times if t is not None]
        if given:
            return float(statistics.median(given))
    tested = await db.scalar(
        select(LocalAiJob.seconds)
        .where(
            LocalAiJob.kind == "test",
            LocalAiJob.model_id == model_id,
            LocalAiJob.state == "done",
            LocalAiJob.seconds.is_not(None),
        )
        .order_by(LocalAiJob.finished_at.desc(), LocalAiJob.created_at.desc())
        .limit(1)
    )
    return None if tested is None else float(tested)
