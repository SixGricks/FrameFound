"""Asking the Looker about colour: the `local_ai` kinds of L4 (design L4 3.6).

`clip_look` asks the Looker for a look for one clip: the clip's reference
still (the one every look of the clip is measured on) and what was measured
about its shot go to the model through `ask()`, and the answer — slider
values, bounded, cleaned and narrowed to what a LUT carries — is stored as
the clip's suggestion (`ClipLook`, `kind="suggested"`). Nothing here ever
writes the `chosen` row: only a person's press does (api/v1/clip_looks.py).

The jobs run on the lane like every other local-AI job (local_models/jobs.py):
a model that only fits overnight leaves the job waiting with the window's
sentence, a pause holds it, Cancel ends it. What a handler cannot carry on
from says so in one sentence (`jobs.Failed`) and stores nothing.

`adopt` is the one way a model's colour for a clip becomes its suggestion,
so a later question that carries colour (a future version of L3's look)
stores it the same way without asking a second one.

`listing_look` asks the Looker about each photograph of a listing: the
straightening, whether the sky is worth replacing, a caption and a file
name (`media/photo_judge.py`). Each answer becomes the photograph's
proposal (`ListingEditSuggestion`), and nothing else is touched: no edit
version, no name. Applying is a separate step a person takes
(`ai_edit_listing` mode `local`, which never talks to a model). A long
listing gives way to anything more urgent between photographs and carries
on from where it was; one photograph the model fails on is counted and the
rest are looked at.
"""

import asyncio
import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.ai import recipe_picker
from framefound.db.models import (
    Asset,
    ClipLook,
    Frame,
    Library,
    Listing,
    ListingEditSuggestion,
    ListingItem,
    LocalAiJob,
    ShotWindow,
)
from framefound.local_models import jobs, registry
from framefound.local_models.ask import Answer, ModelFailed, ask
from framefound.media import clip_look, develop, lut, photo_judge
from framefound.processing import shots, tasks
from framefound.storage.reachability import unreachable_reason
from framefound.storage.worker_roots import UnmappedRoot, local_root

log = structlog.get_logger()

CLIP_KIND = "clip_look"
# A suggestion is stored on the clip's one `suggested` row; the page's choice
# lives on its `chosen` row, which nothing here touches.
SUGGESTED = "suggested"

CLIP_GONE = "That clip is no longer in the catalogue."
NO_PICTURE = "No picture could be read from this clip."
NOT_SLIDERS = "The model looked but did not answer with slider values. Nothing was stored."
# What the model wrote when it did not answer in the form asked for, kept
# under Details for whoever compares models (L2 counts them per model).
RAW_LENGTH = 4000


def _uuid(raw: object) -> uuid.UUID | None:
    try:
        return None if raw is None else uuid.UUID(str(raw))
    except ValueError:
        return None


# --- storing a suggestion ----------------------------------------------------------


def narrowed(sliders: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """A model's sliders as a clip look, and the keys a LUT could not carry.

    What a LUT cannot carry is decided first, on the values as they were
    said: a shadow lift (positive `shadows`) reads the room and is named,
    never clamped to nothing and dropped in silence — that look would be a
    darker picture than the one asked for. Then every slider a model may
    suggest is held to `clip_look.LIMITS` and dropped inside the dead band,
    whoever let it through; anything else it said (a window pull, a
    straightening) is not a clip look's and is named as well."""
    said: dict[str, Any] = {
        key: value
        for key, value in sliders.items()
        if not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    }
    carried, left_out = lut.lut_recipe(said)
    kept: dict[str, Any] = {}
    for key, value in carried.items():
        if key in clip_look.LIMITS:
            low, high = clip_look.LIMITS[key]
            value = round(min(high, max(low, float(value))), 3)
            if abs(value) < clip_look.DEAD_BAND:
                continue
        kept[key] = value
    recipe = clip_look.clean_look(kept)
    # Carried by a LUT but not a clip look's (auto-levels): named as well.
    extra = [key for key in kept if key not in clip_look.SLIDERS]
    return recipe, (*left_out, *extra)


def _said(provenance: Mapping[str, str], key: str, length: int) -> str | None:
    value = provenance.get(key)
    return str(value)[:length] if value else None


async def adopt(
    db: AsyncSession,
    asset_id: uuid.UUID,
    sliders: Mapping[str, Any],
    *,
    reason: str,
    still_ts_ms: int,
    still: Image.Image,
    provenance: Mapping[str, str],
    demo: bool = False,
) -> ClipLook:
    """Store a model's colour for a clip as the suggestion (replacing an older one).

    Cleans and narrows the sliders itself, freezes the white balance and
    levels on `still` (the picture at `still_ts_ms`), and keeps what the still
    looks like (`features`), so a choice of this look later teaches the video
    look as any other choice does. Flushes; the caller commits."""
    recipe, left_out = narrowed(sliders)
    measured = await asyncio.to_thread(develop.measure, still, recipe)
    features = await asyncio.to_thread(clip_look.features_of, still)
    row = await db.scalar(
        select(ClipLook)
        .where(ClipLook.asset_id == asset_id, ClipLook.kind == SUGGESTED)
        .execution_options(populate_existing=True)
    )
    if row is None:
        row = ClipLook(asset_id=asset_id, kind=SUGGESTED)
        db.add(row)
    row.source = "model"
    row.recipe = recipe
    row.measured = measured
    row.left_out = list(left_out)
    row.still_ts_ms = max(0, int(still_ts_ms))
    row.features = features
    row.reason = " ".join(str(reason).split())[:300]
    row.model = _said(provenance, "model", 200)
    row.model_revision = _said(provenance, "model_revision", 64)
    row.variant = _said(provenance, "variant", 100) or ""
    row.runtime = _said(provenance, "runtime", 30)
    row.runtime_version = _said(provenance, "runtime_version", 40)
    row.prompt_version = _said(provenance, "prompt_version", 40)
    row.demo = bool(demo)
    row.decided_by = None
    row.updated_at = datetime.now(UTC)
    await db.flush()
    return row


# --- the job ----------------------------------------------------------------------------


async def _cancel_pressed(ctx: jobs.JobContext) -> bool:
    """Whether Cancel was pressed, read now: `ctx.progress` reads it only when
    it writes, at most every few seconds. A model that answers in one must
    not be asked about two more photographs after Cancel, and an answer that
    arrives after Cancel must not be stored as if nobody had pressed it."""
    async with ctx.factory() as db:
        return bool(
            await db.scalar(select(LocalAiJob.cancel_requested).where(LocalAiJob.id == ctx.job_id))
        )


def _holding(windows: Sequence[Any], ts_ms: int) -> Any | None:
    """The shot whose stretch holds the moment the still shows."""
    for window in windows:
        if int(window.start_ms) <= ts_ms < int(window.end_ms):
            return window
    return None


async def suggest_clip_look(ctx: jobs.JobContext, job: LocalAiJob) -> dict[str, Any] | None:
    """Ask the Looker for a look for one clip, and store it as the suggestion.

    `NotNow` (paused, the overnight window, no Looker) and `ModelFailed` (it
    would not load, died, timed out) propagate, so the runner writes the
    job's state in L1's words. An answer that is not slider values, twice,
    fails the job in this kind's own words with the model's text under
    Details. A clip with no readable picture is never shown to the model."""
    payload = job.payload if isinstance(job.payload, dict) else {}
    asset_id = _uuid(payload.get("asset_id") or job.subject)
    settings = ctx.deps.settings
    async with ctx.factory() as db:
        asset = None if asset_id is None else await db.get(Asset, asset_id)
        if asset is None or asset.proxy_of_id is not None or asset.media_type != "video":
            raise jobs.Failed(CLIP_GONE)
        windows = list(
            await db.scalars(
                select(ShotWindow)
                .where(ShotWindow.asset_id == asset.id)
                .order_by(ShotWindow.start_ms)
            )
        )
        frames = [
            int(ts)
            for ts in await db.scalars(select(Frame.ts_ms).where(Frame.asset_id == asset.id))
        ]
        duration_ms = int(asset.duration_s * 1000) if asset.duration_s else None
        reference = clip_look.reference_moment(
            [(int(w.start_ms), int(w.end_ms)) for w in windows], frames, duration_ms
        )
        if reference is None:
            raise jobs.Failed(NO_PICTURE)
        still, shown = await shots.still_at(db, asset, reference, data_dir=settings.data_dir)
        if shown < 0:
            # The grey blank is "no picture": a model is never asked about it.
            raise jobs.Failed(NO_PICTURE)
        jpeg = await asyncio.to_thread(recipe_picker.preview_bytes, still)
        window = _holding(windows, shown)
        facts = clip_look.facts_line(
            window.look if window is not None and isinstance(window.look, dict) else None,
            window.light if window is not None else None,
            window.move if window is not None else None,
        )
        await ctx.progress(phase="looking", done=0, total=1)
        try:
            answer = await ask(
                db,
                "looker",
                prompt=clip_look.prompt(facts),
                prompt_version=clip_look.PROMPT_VERSION,
                instructions=clip_look.INSTRUCTIONS,
                pictures=[jpeg],
                schema=clip_look.ColourSuggestion,
                server=ctx.deps.server,
                now=ctx.deps.now(),
                settings=settings,
            )
        except ModelFailed as err:
            if err.reason == "bad_answer":
                raise jobs.Failed(NOT_SLIDERS, detail=err.text[:RAW_LENGTH]) from None
            raise
        suggestion = answer.parsed
        if not isinstance(suggestion, clip_look.ColourSuggestion):  # a schema was given: never
            raise jobs.Failed(NOT_SLIDERS, detail=answer.text[:RAW_LENGTH])
        recipe, reason = clip_look.clean_suggestion(suggestion)
        model = await registry.get_model(db, answer.model_id)
        # Asked about for a minute or more: the clip may have gone meanwhile.
        if await db.get(Asset, asset.id, populate_existing=True) is None:
            raise jobs.Failed(CLIP_GONE)
        if await _cancel_pressed(ctx):
            # Pressed while the model was answering: the page said "cancelling",
            # so its answer is not stored. What `ask` noted on the model row
            # (the memory it saw) and on the job row is kept.
            await db.commit()
            await ctx.record(answer)
            raise jobs.Cancelled()
        await adopt(
            db,
            asset.id,
            recipe,
            reason=reason,
            still_ts_ms=shown,
            still=still,
            provenance={
                **answer.provenance(),
                "variant": (model.variant or "") if model is not None else "",
            },
            demo=answer.demo,
        )
        # Also commits what `ask` noted on the model row (the memory it saw).
        await db.commit()
    await ctx.record(answer)
    ctx.latest.set(phase="looking", done=1, total=1)
    log.info("clip_looks.suggested", asset_id=str(asset.id), demo=answer.demo)
    return {"asset_id": str(asset.id)}


def _title(job: LocalAiJob) -> str:
    payload = job.payload if isinstance(job.payload, dict) else {}
    return f"Suggesting a look for {payload.get('filename') or 'a clip'}"


jobs.register(CLIP_KIND, suggest_clip_look, needs_model=True, title=_title)


# --- asking for one, and reading it ---------------------------------------------------


async def queue_clip(
    db: AsyncSession, asset: Asset, *, user_id: uuid.UUID | None, priority: int = 0
) -> LocalAiJob:
    """Ask for a suggestion for `asset`. `jobs.JobRefused` while one for the
    same clip is still waiting or under way. Flushed; the caller commits, then
    wakes the lane."""
    return await jobs.create(
        db,
        CLIP_KIND,
        subject=str(asset.id),
        role="looker",
        payload={"asset_id": str(asset.id), "filename": asset.filename},
        priority=priority,
        user_id=user_id,
    )


# --- a listing's photographs ----------------------------------------------------------

LISTING_KIND = "listing_look"
# Proposals are written in this state; Apply moves them on (applied), and so
# does Dismiss (dismissed). Asking again puts a photograph back here.
PROPOSED = "proposed"
APPLIED = "applied"
# Model failures in a row before the run stops: one photograph a model
# cannot answer about is that photograph's problem; three in a row is the
# model's, and every further one would fail the same way.
MAX_RUN_OF_FAILURES = 3
# The ways a model can fail on one photograph and still be asked about the
# next. Not loading (`load_failed`) and damaged files (`bad_files`) end the
# run at once: nothing after would load either.
ONE_PHOTOGRAPH = frozenset({"bad_answer", "died", "timeout", "runtime_error"})
# How far a listing has got, kept in the job's payload under this key: the
# rows already stored are not enough to carry on from when every photograph
# is being looked at again, and what failed must not be asked about twice in
# one run.
PROGRESS = "_progress"
# How a listing's job names a photograph in its first problem.
NAME_LENGTH = 120

# How long the files of a listing are given to answer (a share half asleep
# answers in seconds; one that hung may never).
SHARE_WAIT_S = 60.0
# How long a listing whose library is away waits before it looks again.
LIBRARY_RETRY = timedelta(minutes=5)

LISTING_GONE = "That listing is no longer in the catalogue."
UNREADABLE = "The photograph could not be read."
STEPPED_ASIDE = "Stepped aside for something more urgent. It carries on by itself."
SHARE_SILENT = "The library's network share did not answer."
# An outage is not damaged files: the photographs are not counted as
# unreadable, and the job waits for the library to come back.
LIBRARY_AWAY = (
    "Waiting for the photographs' library. {reason} It carries on by itself once it is back."
)
RUN_OF_FAILURES = (
    "The model failed on {count} photographs in a row, so the rest were not looked at. "
    "The last: {message}"
)


@dataclass
class _Progress:
    """How far one listing's job has got, across the runs it took. Every
    photograph it has dealt with is in `seen`, whatever became of it."""

    seen: list[str] = field(default_factory=list)
    looked: int = 0
    failed: int = 0
    unreadable: int = 0
    first_problem: str = ""
    # Model failures since the last photograph it answered about.
    run: int = 0

    @classmethod
    def of(cls, raw: object) -> "_Progress":
        """Read back from the payload; anything unreadable starts afresh."""
        if not isinstance(raw, dict):
            return cls()

        def number(key: str) -> int:
            value = raw.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        seen = raw.get("seen")
        problem = raw.get("first_problem")
        return cls(
            seen=[str(each) for each in seen] if isinstance(seen, list) else [],
            looked=number("looked"),
            failed=number("failed"),
            unreadable=number("unreadable"),
            first_problem=problem if isinstance(problem, str) else "",
            run=number("run"),
        )

    def stored(self) -> dict[str, Any]:
        return {
            "seen": list(self.seen),
            "looked": self.looked,
            "failed": self.failed,
            "unreadable": self.unreadable,
            "first_problem": self.first_problem,
            "run": self.run,
        }

    def result(self) -> dict[str, Any]:
        """The job's result, as the listing's page reads it (`last_run`)."""
        return {
            "looked": self.looked,
            "failed": self.failed,
            "unreadable": self.unreadable,
            "first_problem": self.first_problem or None,
        }

    def problem(self, filename: str, sentence: str) -> None:
        if not self.first_problem:
            self.first_problem = f"{filename[:NAME_LENGTH]}: {sentence}"[:300]


def last_run(job: LocalAiJob | None) -> dict[str, Any] | None:
    """What a finished listing job did: its result, or — for one that failed
    or was cancelled part-way — how far it had got. None for a job that is
    not finished, or one that never looked at a photograph."""
    if job is None or job.state not in jobs.FINISHED:
        return None
    if isinstance(job.result, dict) and "looked" in job.result:
        return {
            key: job.result.get(key) for key in ("looked", "failed", "unreadable", "first_problem")
        }
    payload = job.payload if isinstance(job.payload, dict) else {}
    if PROGRESS not in payload:
        return None
    return _Progress.of(payload[PROGRESS]).result()


async def render_sources(
    db: AsyncSession, data_dir: Path, rows: Sequence[Any]
) -> dict[uuid.UUID, tuple[Path, bool]] | None:
    """Where each photograph's render starts — its newest object-removal
    result if it has one, else the original — decided by the same two halves
    as `tasks._render_sources`, so the model and the preview judge the pixels
    Apply and the zip will use. `rows` are (item, asset, library).

    Unlike that helper, nothing here touches a share on the event loop:
    `safe_join` resolves the path, which stats every part of it, and a share
    that hung would hold every request of the server (or the lane's own
    heartbeat, so a job still working would read as stopped). The database
    half runs here, the files in a thread. None when the files did not answer
    within SHARE_WAIT_S."""
    places, removed = await tasks.plan_render_sources(db, rows)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(tasks.locate_render_sources, data_dir, places, removed),
            SHARE_WAIT_S,
        )
    except TimeoutError:
        # The thread is left to the kernel; nothing waits on it any more.
        log.warning("listing_look.share_silent", photographs=len(places))
        return None


def _away(root_path: str, known: int) -> str | None:
    """Why a library's files cannot be read right now, or None if they can
    (a thread: it stats the share)."""
    try:
        return unreachable_reason(local_root(root_path), known)
    except UnmappedRoot:
        # Not mapped on this machine at all: a setting, not an outage.
        return None


async def library_away(root_path: str, known: int) -> str | None:
    """`_away`, given SHARE_WAIT_S: a share that does not answer is away."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(_away, root_path, known), SHARE_WAIT_S)
    except TimeoutError:
        return SHARE_SILENT


def _photograph(source: tuple[Path, bool] | None) -> bytes | None:
    """The picture a model is shown of a photograph — where its render starts
    (the object-removed version when there is one, as auto-edit judges it),
    at the size the Claude mode sends — or None when it cannot be read."""
    if source is None:
        return None
    path, normalized = source
    try:
        # Decoded at about the size it is sent: a 45 MP original is not
        # decoded and colour-converted whole to be shrunk to 768 px.
        image = develop.open_for_render(
            path, already_normalized=normalized, max_edge=recipe_picker.PREVIEW_EDGE
        )
        return recipe_picker.preview_bytes(image)
    except Exception:  # an unreadable photograph is counted and skipped, never the run
        log.warning("listing_look.unreadable", path=path.name, exc_info=True)
        return None


async def _keep(
    db: AsyncSession, job_id: uuid.UUID, payload: dict[str, Any], progress: _Progress
) -> None:
    """Write how far the job has got into its payload, in the transaction that
    stores the photograph's proposal. Only this handler writes the payload
    while the job runs (the runner and the API name other columns)."""
    payload[PROGRESS] = progress.stored()
    await db.execute(
        update(LocalAiJob)
        .where(LocalAiJob.id == job_id)
        .values(payload=dict(payload))
        .execution_options(synchronize_session=False)
    )


async def _propose(
    db: AsyncSession,
    listing_id: uuid.UUID,
    item_id: uuid.UUID,
    asset_id: uuid.UUID,
    judged: photo_judge.Judged,
    answer: Answer,
    variant: str,
) -> ListingEditSuggestion | None:
    """Store a photograph's proposal, replacing an older one, back to
    `proposed`. None when the photograph left the listing meanwhile. Flushes;
    the caller commits."""
    item = await db.get(ListingItem, item_id, populate_existing=True)
    if item is None or item.listing_id != listing_id:
        return None
    row = await db.scalar(
        select(ListingEditSuggestion)
        .where(ListingEditSuggestion.item_id == item_id)
        .execution_options(populate_existing=True)
    )
    if row is None:
        row = ListingEditSuggestion(listing_id=listing_id, item_id=item_id, asset_id=asset_id)
        db.add(row)
    provenance = answer.provenance()
    row.asset_id = asset_id
    row.rotate = judged.rotate
    row.keystone = judged.keystone
    row.sky_worth_replacing = judged.sky
    row.caption = judged.caption
    row.slug = judged.slug[:80]
    row.note = judged.note
    row.state = PROPOSED
    row.applied_version = None
    row.model = _said(provenance, "model", 200)
    row.model_revision = _said(provenance, "model_revision", 64)
    row.variant = variant[:100]
    row.runtime = _said(provenance, "runtime", 30)
    row.runtime_version = _said(provenance, "runtime_version", 40)
    row.prompt_version = _said(provenance, "prompt_version", 40)
    row.demo = bool(answer.demo)
    # When this proposal was made: an older one it replaces says nothing now.
    row.created_at = datetime.now(UTC)
    row.decided_at = None
    row.decided_by = None
    await db.flush()
    return row


async def look_at_listing(ctx: jobs.JobContext, job: LocalAiJob) -> dict[str, Any] | None:
    """Ask the Looker about each photograph of a listing, and store what it
    proposes for each. Writes `listing_edit_suggestions` rows and nothing
    else: no edit version, no caption, no file name.

    The listing's photographs in gallery order, less those that already have
    a proposal (unless `redo`) and those this job has already dealt with.
    Between photographs it gives way to anything more urgent (`jobs.Later`)
    and is run again from where it was; `NotNow` (paused, the overnight
    window, a busy server) leaves it waiting the same way, and so does a
    library that is away: an unmounted share is not told as every
    photograph being unreadable. A photograph that cannot be read is counted
    and skipped; so is one the model fails on, unless that is the third in a
    row, or the model would not load at all."""
    payload = dict(job.payload) if isinstance(job.payload, dict) else {}
    listing_id = _uuid(payload.get("listing_id") or job.subject)
    redo = payload.get("redo") is True
    progress = _Progress.of(payload.get(PROGRESS))
    settings = ctx.deps.settings
    async with ctx.factory() as db:
        listing = None if listing_id is None else await db.get(Listing, listing_id)
        if listing is None or listing_id is None:
            raise jobs.Failed(LISTING_GONE)
        rows = (
            await db.execute(
                select(ListingItem, Asset, Library)
                .join(Asset, Asset.id == ListingItem.asset_id)
                .join(Library, Library.id == Asset.library_id)
                .where(ListingItem.listing_id == listing_id, Asset.media_type == "image")
                .order_by(ListingItem.position)
            )
        ).all()
        proposed = set(
            await db.scalars(
                select(ListingEditSuggestion.item_id).where(
                    ListingEditSuggestion.listing_id == listing_id
                )
            )
        )
        seen = set(progress.seen)
        todo = [
            row
            for row in rows
            if str(row[0].id) not in seen and (redo or row[0].id not in proposed)
        ]
        sources = await render_sources(db, settings.data_dir, todo)
        if sources is None:
            raise jobs.Later(
                LIBRARY_AWAY.format(reason=SHARE_SILENT),
                not_before=ctx.deps.now() + LIBRARY_RETRY,
            )
        # Plain values: nothing loaded outlives this session.
        photos = [
            (item.id, asset.id, asset.filename, sources.get(asset.id), library.root_path)
            for item, asset, library in todo
        ]
    # How many of these photographs each library holds: an empty mountpoint
    # where the catalogue holds files is a share that is not mounted.
    held: dict[str, int] = {}
    for *_ids, root_path in photos:
        held[root_path] = held.get(root_path, 0) + 1

    total = len(progress.seen) + len(photos)
    for item_id, asset_id, filename, source, root_path in photos:
        await ctx.progress(phase="looking", done=len(progress.seen), total=total, message=filename)
        if await _cancel_pressed(ctx):
            raise jobs.Cancelled()
        if await ctx.should_yield():
            raise jobs.Later(STEPPED_ASIDE)
        jpeg = await asyncio.to_thread(_photograph, source)
        if jpeg is None and (source is None or not source[1]):
            # Not read from the library: is it the photograph, or the library?
            away = await library_away(root_path, held[root_path])
            if away is not None:
                raise jobs.Later(
                    LIBRARY_AWAY.format(reason=away), not_before=ctx.deps.now() + LIBRARY_RETRY
                )
        async with ctx.factory() as db:
            progress.seen.append(str(item_id))
            if jpeg is None:
                progress.unreadable += 1
                progress.problem(filename, UNREADABLE)
                await _keep(db, job.id, payload, progress)
                await db.commit()
                continue
            try:
                answer = await ask(
                    db,
                    "looker",
                    prompt=photo_judge.PROMPT,
                    prompt_version=photo_judge.PROMPT_VERSION,
                    instructions=photo_judge.INSTRUCTIONS,
                    pictures=[jpeg],
                    schema=photo_judge.PhotoJudgement,
                    server=ctx.deps.server,
                    now=ctx.deps.now(),
                    settings=settings,
                )
                judgement = answer.parsed
                if not isinstance(judgement, photo_judge.PhotoJudgement):  # a schema was given
                    raise ModelFailed(
                        "bad_answer", "Its answer was not the form asked for.", text=answer.text
                    )
            except ModelFailed as err:
                progress.failed += 1
                progress.run += 1
                progress.problem(filename, str(err))
                # Also keeps what `ask` noted on the model row before it failed.
                await _keep(db, job.id, payload, progress)
                await db.commit()
                if err.reason not in ONE_PHOTOGRAPH:
                    raise
                if progress.run >= MAX_RUN_OF_FAILURES:
                    raise jobs.Failed(
                        RUN_OF_FAILURES.format(count=progress.run, message=str(err)),
                        detail=err.detail or err.text[:RAW_LENGTH],
                    ) from None
                continue
            model = await registry.get_model(db, answer.model_id)
            stored = await _propose(
                db,
                listing_id,
                item_id,
                asset_id,
                photo_judge.clean_judgement(judgement),
                answer,
                (model.variant or "") if model is not None else "",
            )
            if stored is None and await db.get(Listing, listing_id, populate_existing=True) is None:
                raise jobs.Failed(LISTING_GONE)
            if stored is not None:
                progress.looked += 1
            progress.run = 0
            await _keep(db, job.id, payload, progress)
            # Also commits what `ask` noted on the model row (the memory it saw).
            await db.commit()
        await ctx.record(answer)
    ctx.latest.set(phase="looking", done=total, total=total, message="")
    log.info(
        "listing_look.looked",
        listing_id=str(listing_id),
        looked=progress.looked,
        failed=progress.failed,
        unreadable=progress.unreadable,
    )
    return progress.result()


def _listing_title(job: LocalAiJob) -> str:
    payload = job.payload if isinstance(job.payload, dict) else {}
    return f"Looking at the photographs of {payload.get('name') or 'a listing'}"


jobs.register(LISTING_KIND, look_at_listing, needs_model=True, title=_listing_title)


async def queue_listing(
    db: AsyncSession, listing: Listing, *, user_id: uuid.UUID | None, redo: bool = False
) -> LocalAiJob:
    """Ask the Looker about a listing's photographs: those with no proposal
    yet, or with `redo` every one. `jobs.JobRefused` while a job for the same
    listing is still waiting or under way. Flushed; the caller commits, then
    wakes the lane. Priority 5: a person asked, and one clip somebody is
    looking at (priority 0) goes first."""
    return await jobs.create(
        db,
        LISTING_KIND,
        subject=str(listing.id),
        role="looker",
        payload={"listing_id": str(listing.id), "name": listing.name, "redo": bool(redo)},
        priority=5,
        user_id=user_id,
    )


async def job_for(db: AsyncSession, kind: str, subject: str) -> LocalAiJob | None:
    """The newest job of that kind about that subject, as it is now."""
    found: LocalAiJob | None = await db.scalar(
        select(LocalAiJob)
        .where(LocalAiJob.kind == kind, LocalAiJob.subject == subject)
        .order_by(LocalAiJob.created_at.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return found


def utc(moment: datetime | None) -> datetime | None:
    """A moment read from the table, in UTC: SQLite hands back what was
    written without its zone, and everything written is UTC."""
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _iso(moment: datetime | None) -> str | None:
    found = utc(moment)
    return None if found is None else found.strftime("%Y-%m-%dT%H:%M:%SZ")


def job_out(
    job: LocalAiJob | None, now: datetime | None = None, *, admin: bool = False
) -> dict[str, Any] | None:
    """A job as a page shows it, read through `jobs.shown` (a running job whose
    worker died reads as failed). `detail` is an administrator's only: it can
    hold a path on the server or what the model wrote."""
    return None if job is None else shown_job(job, now, admin=admin)


def shown_job(
    job: LocalAiJob, now: datetime | None = None, *, admin: bool = False
) -> dict[str, Any]:
    """`job_out` of a job that is there."""
    state, message = jobs.shown(job, now)
    active = state in jobs.ACTIVE
    return {
        "id": str(job.id),
        "state": state,
        "message": message,
        "detail": (job.detail or None) if admin else None,
        "not_before": _iso(job.not_before) if state == "waiting" else None,
        "done": int(job.done or 0),
        "total": int(job.total or 0),
        "cancelling": active and bool(job.cancel_requested),
        "can_cancel": active and not job.cancel_requested,
    }
