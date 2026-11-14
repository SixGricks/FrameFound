"""One model's turn in a comparison: the `compare_model` job on the `local_ai` lane.

A turn asks one model every item of a comparison it has no answer for yet,
one at a time, through `ask()` by the model's id, and stores each answer the
moment it has it. "The items with no answer row" is where every turn starts —
a first run, a second delivery, a restart after the worker died, a step
aside for a Test and Carry on alike — so none of them asks anything twice.

The column's own row (`ComparisonModel`) is the record; this handler writes
its state, and `abandoned` writes it when the turn ends without the handler
(cancelled before its turn, its worker died). What a model says goes into
the comparison's own tables and nowhere else: nothing here writes to the
catalogue, and the model is told only what the frozen item says — never the
filename, the folder, the tags decided on it or whether it was used.

Every model is shown the same thing: the frozen pictures and the frozen
sentence of each item, and the vocabulary frozen on the comparison.

In demo mode the model server is the demo's stand-in, whose answers fit the
schema and mean nothing; there, and only there, an answer it marked `demo`
is stored as `looking.demo`'s made-up look instead (`_made_up`). The fake
the tests hand in marks its answers `demo` too, so the mode is asked as
well: outside demo mode an answer is always stored as it was given.
"""

import asyncio
import os
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.ai import tag_review
from framefound.ai.tagging import slugify
from framefound.db.models import Comparison, ComparisonAnswer, ComparisonModel, LocalAiJob
from framefound.local_models import availability, comparisons, jobs, registry, sampling
from framefound.local_models.ask import (
    MAX_PICTURE_BYTES,
    MAX_PICTURES,
    Answer,
    ModelFailed,
    NotNow,
    ask,
)
from framefound.looking import demo, prompt

log = structlog.get_logger()

KIND = comparisons.KIND
subject = comparisons.subject
# Three broken answers in a row stop the turn: a runtime that dies on every
# item would otherwise spend an hour failing.
FAILS_IN_A_ROW = 3
RAW_LENGTH = 4000

STEPPED_ASIDE = "Stepped aside for something more urgent. It carries on by itself."
REMOVED = "The model was removed."
NO_PICTURE = "Its picture is no longer there."
DAMAGED = "Its picture is damaged, so it was not shown to the model."
PICTURES_GONE = (
    "The comparison's pictures are not on this machine any more, so nothing was asked. "
    "If the drive that holds FrameFound's data is disconnected, connect it and press "
    "Carry on."
)
UNEXPECTED = "It failed unexpectedly."
# How each failure that counts toward the streak is said when it stops the turn.
_BROKE = {
    "died": "the model stopped unexpectedly",
    "timeout": "it took too long to answer",
    "runtime_error": "the program running it could not answer",
}
# A model that will not load, or whose files are not whole, fails every item
# the same way: the turn stops at the first, not after sixty.
_AT_ONCE = frozenset({"load_failed", "bad_files"})


def in_a_row(reason: str) -> str:
    """Why a turn was stopped after FAILS_IN_A_ROW broken answers."""
    why = _BROKE.get(reason, _BROKE["died"])
    return (
        f"It failed three items in a row ({why}), so it was stopped rather than left to "
        "grind through the rest."
    )


# --- what a comparison keeps of an answer ---------------------------------------------


@dataclass(frozen=True)
class KeptTag:
    name: str
    slug: str
    confidence: float
    in_vocabulary: bool


@dataclass(frozen=True)
class Kept:
    tags: tuple[KeptTag, ...]  # every proposal, strongest first
    caption: str
    score: int
    reason: str


def kept(answer: prompt.LookAnswer, vocabulary_slugs: set[str]) -> Kept:
    """What a comparison stores of a look.

    Not the Looker's `prompt.clean`, which applies an admission policy and
    drops what falls under it: Compare is what measures where that policy's
    floors should sit, so every proposal is kept with its confidence. Tags go
    through `tag_review.proposals_from_answer`: one per slug at its stronger
    confidence, and "in the vocabulary" decided by the slug, whichever list
    the model put the tag in — a reworded match is a new word.
    """
    proposals, _description = tag_review.proposals_from_answer(
        answer.model_dump(), vocabulary_slugs
    )
    return Kept(
        tags=tuple(
            KeptTag(
                name=proposal.name,
                slug=slugify(proposal.name),
                confidence=float(proposal.confidence or 0.0),
                in_vocabulary=proposal.existing,
            )
            for proposal in proposals
        ),
        caption=answer.caption,
        score=answer.score,
        reason=answer.reason,
    )


# --- the turn ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    """What a turn needs of an item, read once: the session may be rolled back
    (a refusal from `ask`), and a rolled-back row cannot be read again."""

    id: uuid.UUID
    position: int
    facts_line: str
    pictures: int
    shot: bool


@dataclass(frozen=True)
class _Turn:
    job_id: uuid.UUID
    entry_id: uuid.UUID
    comparison_id: uuid.UUID
    model_id: uuid.UUID
    repo_id: str
    prompt_version: str
    vocabulary: list[Any]
    total: int
    todo: tuple[_Item, ...]
    done: int
    failed: int
    memory_gb: float | None
    memory_source: str | None


def _uuid(payload: object, key: str) -> uuid.UUID | None:
    raw = payload.get(key) if isinstance(payload, dict) else None
    try:
        return None if raw is None else uuid.UUID(str(raw))
    except ValueError:
        return None


def _utc(moment: datetime | None) -> datetime | None:
    """A moment from the Mac's clock (naive) or a refusal, in UTC, as stored."""
    return None if moment is None else moment.astimezone(UTC)


async def _write(db: AsyncSession, turn_job: uuid.UUID, entry_id: uuid.UUID, **values: Any) -> bool:
    """Change the column, but only while this job is still its turn: a column
    carried on since (a newer job) or removed is not written by an old one."""
    if "message" in values:
        values["message"] = str(values["message"])[:500]
    written: Any = await db.execute(
        update(ComparisonModel)
        .where(ComparisonModel.id == entry_id, ComparisonModel.job_id == turn_job)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return bool(written.rowcount)


async def _count(db: AsyncSession, entry_id: uuid.UUID, *, ok: bool) -> int:
    found = await db.scalar(
        select(func.count())
        .select_from(ComparisonAnswer)
        .where(ComparisonAnswer.entry_id == entry_id, ComparisonAnswer.ok.is_(ok))
    )
    return int(found or 0)


async def _begin(ctx: jobs.JobContext, db: AsyncSession, job: LocalAiJob) -> _Turn | dict[str, Any]:
    """Load the turn and mark its column running; or close it, and say why not."""
    comparison_id = _uuid(job.payload, "comparison_id")
    entry_id = _uuid(job.payload, "entry_id")
    comparison = None if comparison_id is None else await db.get(Comparison, comparison_id)
    entry = (
        None
        if entry_id is None
        else await db.get(ComparisonModel, entry_id, populate_existing=True)
    )
    if comparison is None or entry is None or entry.comparison_id != comparison.id:
        return {"gone": True}
    if entry.job_id is not None and entry.job_id != job.id:
        # Not this column's turn any more (it was carried on under a newer job).
        return {"gone": True}
    entry.job_id = job.id
    model = None if entry.model_id is None else await registry.get_model(db, entry.model_id)
    if model is None:
        await _end(ctx, db, job.id, entry.id, "failed", REMOVED)
        return {"refused": REMOVED}
    if not comparisons.is_current(comparison) or entry.prompt_version != comparison.prompt_version:
        said = comparisons.older(entry.prompt_version or comparison.prompt_version)
        await _end(ctx, db, job.id, entry.id, "failed", said)
        return {"refused": said}

    payload = job.payload if isinstance(job.payload, dict) else {}
    if payload.get(comparisons.RETRY_FAILED):
        # Try the failed ones again: their answers go now that the turn has
        # really started, and the flag goes with them in the same commit, so
        # a turn that steps aside and comes back does not throw away the
        # failures it has just recorded.
        await db.execute(
            delete(ComparisonAnswer).where(
                ComparisonAnswer.entry_id == entry.id, ComparisonAnswer.ok.is_(False)
            )
        )
        await db.execute(
            update(LocalAiJob)
            .where(LocalAiJob.id == job.id)
            .values(payload={k: v for k, v in payload.items() if k != comparisons.RETRY_FAILED})
            .execution_options(synchronize_session=False)
        )

    entry.state = "running"
    entry.message = ""
    entry.detail = None
    entry.not_before = None
    entry.finished_at = None
    if entry.started_at is None:
        entry.started_at = _utc(ctx.deps.now())
    items = await comparisons.items_of(db, comparison.id)
    todo = {item.id for item in await comparisons.unanswered(db, entry)}
    turn = _Turn(
        job_id=job.id,
        entry_id=entry.id,
        comparison_id=comparison.id,
        model_id=model.id,
        repo_id=model.repo_id,
        prompt_version=comparison.prompt_version,
        vocabulary=list(comparison.vocabulary or []),
        total=len(items),
        todo=tuple(
            _Item(
                id=item.id,
                position=item.position,
                facts_line=item.facts_line or "",
                pictures=len(item.picture_ms or []),
                shot=item.kind == "shot",
            )
            for item in items
            if item.id in todo
        ),
        done=await _count(db, entry.id, ok=True),
        failed=await _count(db, entry.id, ok=False),
        memory_gb=entry.memory_gb,
        memory_source=entry.memory_source,
    )
    entry.done, entry.failed = turn.done, turn.failed
    await db.commit()
    return turn


async def _end(
    ctx: jobs.JobContext,
    db: AsyncSession,
    job_id: uuid.UUID,
    entry_id: uuid.UUID,
    state: str,
    message: str,
    **more: Any,
) -> None:
    values: dict[str, Any] = {"state": state, "message": message, "not_before": None, **more}
    if state == "done":
        values["finished_at"] = _utc(ctx.deps.now())
    await _write(db, job_id, entry_id, **values)
    await db.commit()


async def run(ctx: jobs.JobContext, job: LocalAiJob) -> dict[str, Any] | None:
    """Ask the column's model every item it has no answer for, by position.

    Lets `NotNow`, `ModelFailed` (one that stops the turn), `jobs.Failed`
    (the pictures are gone), `jobs.Cancelled` and `jobs.Later` out after
    writing the column's state, so the runner
    ends the job the same way (a turn that swallowed them would read `done`
    and an overnight wait would never be retried).
    """
    async with ctx.factory() as db:
        begun = await _begin(ctx, db, job)
        if isinstance(begun, dict):
            return begun
        turn = begun
        try:
            return await _ask_each(ctx, db, turn)
        except (jobs.Later, jobs.Failed, ModelFailed):
            raise  # the column's state was written where it was raised
        except jobs.Cancelled:
            await db.rollback()
            await _end(ctx, db, turn.job_id, turn.entry_id, "stopped", comparisons.CANCELLED)
            raise
        except NotNow as err:
            # What `ask` noted on the model row half way is not kept: the
            # column's state is written on a clean session (P11).
            await db.rollback()
            if err.retry:
                await _write(
                    db,
                    turn.job_id,
                    turn.entry_id,
                    state="waiting",
                    message=str(err),
                    not_before=_utc(err.not_before),
                )
                await db.commit()
            else:
                await _end(
                    ctx,
                    db,
                    turn.job_id,
                    turn.entry_id,
                    "failed",
                    str(err),
                    detail=err.detail or None,
                )
            raise
        except Exception:
            await db.rollback()
            await _end(ctx, db, turn.job_id, turn.entry_id, "failed", UNEXPECTED)
            raise


async def _ask_each(ctx: jobs.JobContext, db: AsyncSession, turn: _Turn) -> dict[str, Any]:
    settings = ctx.deps.settings
    shown_words = [name for name, _count in prompt.most_used(turn.vocabulary)]
    vocabulary_slugs = {slugify(name) for name in shown_words}
    # The demo's model server answers with random JSON that fits the schema;
    # in demo mode alone its answers are replaced with made-up looks (D23).
    in_demo = availability(settings).mode == "demo"
    done, failed = turn.done, turn.failed
    memory, source = turn.memory_gb, turn.memory_source
    streak = 0
    last: Answer | None = None
    folder = sampling.pictures_dir(turn.comparison_id, settings)

    for item in turn.todo:
        # Progress (and the heartbeat) is written at most every two seconds,
        # so Cancel is also asked about here, at every item: a model that
        # answers in a second must not be asked two more after Cancel.
        await ctx.progress(phase="looking", done=done + failed, total=turn.total)
        if await _cancel_pressed(ctx, turn.job_id):
            raise jobs.Cancelled()
        if await ctx.should_yield():
            await _write(db, turn.job_id, turn.entry_id, state="queued", message=STEPPED_ASIDE)
            await db.commit()
            raise jobs.Later(STEPPED_ASIDE)

        pictures = await asyncio.to_thread(
            sampling.read_pictures, turn.comparison_id, item.position, item.pictures, settings
        )
        row: ComparisonAnswer
        entry_values: dict[str, Any] = {}
        failure: ModelFailed | None = None
        asked = False
        if not pictures:
            if not await asyncio.to_thread(_readable, folder):
                # The whole folder is not there (the data drive not mounted,
                # or unplugged mid-turn): not this item's finding, and every
                # item after it would say the same.
                await _pictures_gone(ctx, db, turn)
            row = _not_answered(turn, item, comparisons.UNSHOWN, NO_PICTURE)
        elif _damaged(pictures):
            # A frozen file cut short (a crash while it was written): `ask`
            # would refuse it as a caller's mistake and the turn would fail on
            # this item at every Carry on. Recorded, so Try again can retry it.
            row = _not_answered(turn, item, comparisons.UNSHOWN, DAMAGED)
        else:
            asked = True
            try:
                answer = await ask(
                    db,
                    turn.model_id,
                    prompt=prompt.build_prompt(
                        facts=item.facts_line, pictures=len(pictures), vocabulary=turn.vocabulary
                    ),
                    prompt_version=turn.prompt_version,
                    instructions=prompt.INSTRUCTIONS,
                    pictures=pictures,
                    schema=prompt.LookAnswer,
                    max_tokens=prompt.MAX_TOKENS,
                    timeout_s=prompt.TIMEOUT_S,
                    server=ctx.deps.server,
                    now=ctx.deps.now(),
                    settings=settings,
                )
            except ModelFailed as err:
                if err.reason in _AT_ONCE:
                    # Not the item's fault, so no answer is recorded for it:
                    # Carry on asks it again once the model loads.
                    await _end(
                        ctx,
                        db,
                        turn.job_id,
                        turn.entry_id,
                        "failed",
                        str(err),
                        detail=err.detail or None,
                    )
                    raise
                failure = err
                row = _not_answered(
                    turn,
                    item,
                    err.reason,
                    str(err),
                    raw=err.text,
                    attempts=2 if err.reason == "bad_answer" else 1,
                )
            else:
                look = answer.parsed
                if not isinstance(look, prompt.LookAnswer):  # a schema was given: never
                    failure = ModelFailed("bad_answer", "Its answer was not the form asked for.")
                    row = _not_answered(turn, item, "bad_answer", str(failure), raw=answer.text)
                else:
                    if answer.demo and in_demo:
                        answer, look = _made_up(turn, item, answer, shown_words)
                    row = _answered(turn, item, answer, kept(look, vocabulary_slugs))
                    last = answer
                    if answer.memory_gb is not None and (
                        memory is None or answer.memory_gb > memory
                    ):
                        memory, source = answer.memory_gb, answer.memory_source
                    entry_values = {
                        "runtime": answer.runtime[:30],
                        "runtime_version": answer.runtime_version[:40],
                        "ran_from": answer.ran_from,
                        "memory_gb": memory,
                        "memory_source": source,
                    }
                    if answer.load_seconds > 0:
                        entry_values["load_seconds"] = answer.load_seconds

        now_done = done + (1 if row.ok else 0)
        now_failed = failed + (0 if row.ok else 1)
        if not await _write(
            db, turn.job_id, turn.entry_id, done=now_done, failed=now_failed, **entry_values
        ):
            # The column was removed, or carried on under another job, meanwhile.
            await db.rollback()
            return {"gone": True}
        db.add(row)
        try:
            # One answer, committed at once (P10); this also commits what
            # `ask` noted on the model row (which runtime, the memory seen).
            await db.commit()
        except IntegrityError:
            # Another delivery stored this item a moment ago: nothing is asked twice.
            await db.rollback()
            log.info("local_ai.compare_answer_doubled", entry_id=str(turn.entry_id))
            continue
        done, failed = now_done, now_failed

        if failure is None:
            if asked:
                streak = 0
            continue
        if failure.reason in _BROKE:
            streak += 1
            if streak >= FAILS_IN_A_ROW:
                await _end(ctx, db, turn.job_id, turn.entry_id, "stopped", in_a_row(failure.reason))
                raise failure
        else:
            streak = 0  # the model answered, in prose: the runtime itself is working

    await _end(ctx, db, turn.job_id, turn.entry_id, "done", "")
    if last is not None:
        await ctx.record(last)
    # The job row's last progress is what it ends with: everything asked.
    ctx.latest.set(phase="looking", done=done + failed, total=turn.total)
    return {"answered": done, "failed": failed}


def _readable(folder: Path) -> bool:
    """The comparison's pictures folder is there and can be listed. Blocking.
    `read_pictures` says [] for one missing file and for a whole drive that
    is not mounted alike; only this tells them apart."""
    try:
        with os.scandir(folder) as found:
            next(found, None)
    except OSError:
        return False
    return True


async def _pictures_gone(ctx: jobs.JobContext, db: AsyncSession, turn: _Turn) -> NoReturn:
    """End the turn `failed`, recording nothing against the items: a turn over
    a folder that is not there would otherwise mark every item "no picture"
    in seconds and close `done`, as if the model had been compared."""
    await db.rollback()
    await _end(ctx, db, turn.job_id, turn.entry_id, "failed", PICTURES_GONE)
    raise jobs.Failed(PICTURES_GONE)


def _damaged(pictures: Sequence[bytes]) -> bool:
    """What `ask` would refuse as not a picture for a model: too many, too
    large, or not a JPEG (an empty or truncated frozen file)."""
    return len(pictures) > MAX_PICTURES or any(
        len(jpeg) > MAX_PICTURE_BYTES or bytes(jpeg[:3]) != b"\xff\xd8\xff" for jpeg in pictures
    )


async def _cancel_pressed(ctx: jobs.JobContext, job_id: uuid.UUID) -> bool:
    """Whether Cancel was pressed, read now: `ctx.progress` reads it only when
    it writes, at most every PROGRESS_EVERY_S. On a session of its own, as
    `should_yield` does, so the turn's session holds nothing open."""
    async with ctx.factory() as db:
        return bool(
            await db.scalar(select(LocalAiJob.cancel_requested).where(LocalAiJob.id == job_id))
        )


def _made_up(
    turn: _Turn, item: _Item, answer: Answer, words: Sequence[str]
) -> tuple[Answer, prompt.LookAnswer]:
    """The demo's look in place of the demo server's random JSON (D23).

    Seeded by model and item, so two demo models differ and a reload changes
    nothing. The seconds are invented too: the fake server reports one answer
    time for every model. The memory is left as the server gave it — the demo
    reports each model it knows a little under its estimate, as the AI models
    page's Test shows it — and stays unmeasured when it gave none: a number
    invented here would be shown as one "the software reports", and for the
    seed's demo models (whose pages the running demo does not hold) it would
    contradict the estimate on the same page.
    Called only for an answer the demo server gave, in demo mode.
    """
    seed = f"{turn.repo_id}:{item.id}"
    look = demo.answer_for(seed, words, shot=item.shot)
    seconds, _memory = demo.figures_for(seed)
    made = replace(answer, text=look.model_dump_json(), parsed=look, seconds=seconds)
    return made, look


def _answered(turn: _Turn, item: _Item, answer: Answer, look: Kept) -> ComparisonAnswer:
    return ComparisonAnswer(
        entry_id=turn.entry_id,
        item_id=item.id,
        ok=True,
        problem=None,
        message="",
        tags=[asdict(tag) for tag in look.tags],
        caption=look.caption[:300],
        score=look.score,
        reason=look.reason[:300],
        # What the model wrote is kept when it needed a second try: how far
        # off the form its first answer was is part of what is measured.
        raw=answer.text[:RAW_LENGTH] if answer.attempts > 1 else None,
        seconds=answer.seconds,
        attempts=answer.attempts,
        memory_gb=answer.memory_gb,
        created_at=datetime.now(UTC),
    )


def _not_answered(
    turn: _Turn,
    item: _Item,
    problem: str,
    message: str,
    *,
    raw: str = "",
    attempts: int = 1,
) -> ComparisonAnswer:
    """An item the model gave nothing usable for: a finding, recorded, and the turn goes on."""
    return ComparisonAnswer(
        entry_id=turn.entry_id,
        item_id=item.id,
        ok=False,
        problem=problem[:20],
        message=message[:300],
        tags=[],
        raw=raw[:RAW_LENGTH] or None,
        attempts=attempts,
        created_at=datetime.now(UTC),
    )


# --- a turn that ended without its handler ----------------------------------------------


async def abandoned(db: AsyncSession, job: LocalAiJob, why: jobs.Why) -> None:
    """Close the column of a turn that ended without the handler: cancelled
    before its turn (or while it ran, unseen), its worker died, this install
    cannot run models, or the handler crashed before it could say so. A
    column that is gone, already closed, or carried on under a newer job is
    left alone. The caller flushes or commits."""
    entry_id = _uuid(job.payload, "entry_id")
    entry = None if entry_id is None else await db.get(ComparisonModel, entry_id)
    if entry is None or entry.state not in comparisons.ACTIVE:
        return
    if entry.job_id is not None and entry.job_id != job.id:
        return
    if why == "cancelled":
        state, message = "stopped", comparisons.CANCELLED
    elif why == "stopped":
        state, message = "stopped", comparisons.STOPPED
    else:
        state, message = "failed", job.message or UNEXPECTED
    entry.state = state
    entry.message = message[:500]
    entry.not_before = None


def _title(job: LocalAiJob) -> str:
    payload = job.payload if isinstance(job.payload, dict) else {}
    return (
        f"Comparing {payload.get('model_name') or 'a model'} "
        f"on {payload.get('comparison_name') or 'a sample'}"
    )


jobs.register(KIND, run, needs_model=True, title=_title, abandoned=abandoned)
