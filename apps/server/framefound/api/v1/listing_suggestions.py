"""Asking the local model about a listing's photographs: the API (design L4 6.8).

The local mode of auto-edit is two steps. This router is the first: Ask
puts one job on the local-AI lane (`processing/colour_jobs.py`
`listing_look`) that looks at each photograph and proposes straightening,
whether the sky is worth replacing, a caption and a file name. The
proposals are stored and listed here; nothing about the photographs
changes. The second step, Apply, is the existing `POST
/listings/{id}/ai-edit` with mode `local`, which reads the proposals a
person ticked and never talks to a model.

- Asking is not an auto-edit run: it never sets the listing's
  `ai_edit_state`, so a job waiting for the overnight window cannot be
  taken for a dead run, and the existing one-run rule is untouched. One
  job per listing is the lane's own rule.
- Where models cannot run (the containers) every read works and says why
  Ask is not offered; Ask itself is 409 with the same sentence. The sentence
  is the listing's own (`CANNOT_ASK_HERE`): the clip card's speaks of the
  learned look and the LUT download, which a listing page does not have.
- The preview is the photograph rendered with the recipe Apply would write
  for the proposal, less the sky (a sky is the operator's pick, made at
  Apply).

Any signed-in user, as the listing's own routes (a read-only account is
refused every non-GET by `auth/deps.py`). Nothing here is an
administrator's setting, so nothing is audited; asking is logged. Every
refusal is the sentence the page shows, in the one error envelope.
"""

import asyncio
import io
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import structlog
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.api.v1.clip_looks import JobOut, ProvenanceOut, _Suggest, _suggest
from framefound.api.v1.local_models import QUEUE_DOWN, DepsDep
from framefound.auth.deps import CurrentUser, DbDep
from framefound.db.models import (
    Asset,
    Library,
    Listing,
    ListingEditSuggestion,
    ListingItem,
    LocalAiJob,
)
from framefound.local_models import jobs
from framefound.media import develop, looks, photo_judge
from framefound.processing import colour_jobs, local_ai_tasks

log = structlog.get_logger()
router = APIRouter(prefix="/listings", tags=["listings"])

# The preview's longest edge: the size the editor's own previews are judged at.
PREVIEW_EDGE = 1024
JPEG_QUALITY = 85

NO_SUCH_LISTING = "No such listing"
# Said in place of the clip card's CANNOT_SUGGEST_HERE. What it names as still
# working are the listing page's own two buttons, neither of which needs a
# model on this machine: Auto-edit uses Claude or the listing preset, naming
# uses Claude.
CANNOT_ASK_HERE = (
    "Suggestions for a listing's photographs come from a model running on the Mac install "
    "of FrameFound. This server cannot run one. Auto-edit photos and Name photos with AI "
    "work here as before."
)
NO_PHOTOGRAPHS = "No photographs to look at."
ALREADY_LOOKING = "The local model is already looking at this listing."
ALL_PROPOSED = (
    "Every photograph already has a suggestion. Ask again with 'look at all of them again' "
    "to redo them."
)
NOTHING_LOOKING = "Nothing is waiting for this listing."
NO_SUGGESTION_HERE = "There is no suggestion for this photograph."
NOT_REACHABLE = "Original file is not reachable"

SuggestionState = Literal["proposed", "applied", "dismissed"]


class _Out(BaseModel):
    # Several fields are called model_..., which is what they are.
    model_config = ConfigDict(protected_namespaces=(), populate_by_name=True)


class ToneOut(_Out):
    # "look": Apply takes the tone from the learned look; "preset": the listing preset.
    from_: Literal["look", "preset"] = Field(alias="from")
    # Shipped photographs the learned look was made from; 0 with none installed.
    examples: int


class LastRunOut(_Out):
    looked: int
    failed: int
    unreadable: int
    # "IMG_0412.jpg: <why>", the first photograph that has no suggestion.
    first_problem: str | None


class SuggestionOut(_Out):
    item_id: uuid.UUID
    asset_id: uuid.UUID
    filename: str
    # The photograph's place in the gallery.
    position: int
    # Degrees, and the verticals' correction (-1..1), as the editor names them.
    rotate: float
    keystone: float
    # "large — look first": the page starts this row unticked.
    large: bool
    sky_worth_replacing: bool
    caption: str
    slug: str
    note: str
    state: SuggestionState
    # The operator named this photograph: Apply keeps their name.
    name_is_yours: bool
    provenance: ProvenanceOut | None
    demo: bool
    made_at: str | None


class LocalEdit(_Out):
    mode: Literal["ready", "demo", "unavailable"]
    # False when Ask cannot be pressed; `note` says why.
    ok: bool
    note: str | None
    # The listing's newest job while it is waiting or under way, or when it failed.
    job: JobOut | None
    photographs: int
    proposed: int
    applied: int
    dismissed: int
    tone: ToneOut
    last_run: LastRunOut | None
    suggestions: list[SuggestionOut]


class AskIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Look at every photograph again, not only those with no suggestion.
    redo: bool = False


class AskedOut(_Out):
    job: JobOut
    to_look_at: int


class CancelledOut(_Out):
    job: JobOut


class DismissIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None: every proposal still waiting.
    item_ids: list[uuid.UUID] | None = None


# --- reading ----------------------------------------------------------------------


def _iso(moment: datetime | None) -> str | None:
    found = colour_jobs.utc(moment)
    return None if found is None else found.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _listing(db: AsyncSession, listing_id: uuid.UUID) -> Listing:
    listing = await db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(404, NO_SUCH_LISTING)
    return listing


async def _photographs(db: AsyncSession, listing_id: uuid.UUID) -> int:
    """The listing's photographs: the items auto-edit works on."""
    found = await db.scalar(
        select(func.count())
        .select_from(ListingItem)
        .join(Asset, Asset.id == ListingItem.asset_id)
        .where(ListingItem.listing_id == listing_id, Asset.media_type == "image")
    )
    return int(found or 0)


async def _asked_about(db: AsyncSession, listing_id: uuid.UUID) -> int:
    """The listing's photographs that already have a suggestion, in any state."""
    found = await db.scalar(
        select(func.count())
        .select_from(ListingEditSuggestion)
        .join(ListingItem, ListingItem.id == ListingEditSuggestion.item_id)
        .join(Asset, Asset.id == ListingItem.asset_id)
        .where(ListingEditSuggestion.listing_id == listing_id, Asset.media_type == "image")
    )
    return int(found or 0)


# Finished jobs looked back over for the last run that looked at anything.
RUNS_LOOKED_BACK = 20


async def _last_run(db: AsyncSession, subject: str) -> dict[str, Any] | None:
    """What the newest finished job that got as far as a photograph did. A
    job cancelled before it looked at any (Ask pressed again by mistake, then
    Cancel) has nothing to say, and must not hide the run before it: the
    proposals on the page are that run's, and so is the line saying how many
    failed and why."""
    finished = await db.scalars(
        select(LocalAiJob)
        .where(
            LocalAiJob.kind == colour_jobs.LISTING_KIND,
            LocalAiJob.subject == subject,
            LocalAiJob.state.in_(jobs.FINISHED),
        )
        .order_by(LocalAiJob.created_at.desc())
        .limit(RUNS_LOOKED_BACK)
    )
    for job in finished:
        ran = colour_jobs.last_run(job)
        if ran is not None:
            return ran
    return None


def _job_out(job: LocalAiJob | None, *, admin: bool) -> JobOut | None:
    """The listing's newest job while it is waiting or under way, or when it
    failed (so the failure is said until the next Ask). Done or cancelled: None."""
    shown = colour_jobs.job_out(job, admin=admin)
    if shown is None or (shown["state"] not in jobs.ACTIVE and shown["state"] != "failed"):
        return None
    return JobOut(**shown)


def _provenance(row: ListingEditSuggestion) -> ProvenanceOut | None:
    if not row.model:
        return None
    return ProvenanceOut(
        model=row.model,
        model_revision=row.model_revision,
        variant=row.variant or "",
        runtime=row.runtime,
        runtime_version=row.runtime_version,
        prompt_version=row.prompt_version,
    )


async def _can_ask(db: AsyncSession, deps: Any) -> _Suggest:
    """Whether the Looker can be asked, as the clip card decides it, with the
    reason in a listing's words where the clip card's would read wrongly here."""
    suggest = await _suggest(db, deps)
    if suggest.mode == "unavailable":
        return replace(suggest, note=CANNOT_ASK_HERE)
    return suggest


async def _answer(db: AsyncSession, listing: Listing, deps: Any, *, admin: bool) -> LocalEdit:
    suggest = await _can_ask(db, deps)
    subject = str(listing.id)
    rows = (
        await db.execute(
            select(ListingEditSuggestion, ListingItem, Asset)
            .join(ListingItem, ListingItem.id == ListingEditSuggestion.item_id)
            .join(Asset, Asset.id == ListingItem.asset_id)
            .where(ListingEditSuggestion.listing_id == listing.id)
            .order_by(ListingItem.position, Asset.filename)
            .execution_options(populate_existing=True)
        )
    ).all()
    counts = {state: 0 for state in ("proposed", "applied", "dismissed")}
    suggestions: list[SuggestionOut] = []
    for row, item, asset in rows:
        state = row.state if row.state in counts else colour_jobs.PROPOSED
        counts[state] += 1
        suggestions.append(
            SuggestionOut(
                item_id=item.id,
                asset_id=asset.id,
                filename=asset.filename,
                position=int(item.position or 0),
                rotate=float(row.rotate or 0.0),
                keystone=float(row.keystone or 0.0),
                large=photo_judge.is_large(float(row.rotate or 0.0), float(row.keystone or 0.0)),
                sky_worth_replacing=bool(row.sky_worth_replacing),
                caption=row.caption or "",
                slug=row.slug or "",
                note=row.note or "",
                state=cast(SuggestionState, state),
                name_is_yours=item.naming_source == "confirmed",
                provenance=_provenance(row),
                demo=bool(row.demo),
                made_at=_iso(row.created_at),
            )
        )
    examples = await asyncio.to_thread(looks.examples, deps.settings.data_dir)
    ran = await _last_run(db, subject)
    return LocalEdit(
        mode=suggest.mode,
        ok=suggest.ok,
        note=suggest.note,
        job=_job_out(await colour_jobs.job_for(db, colour_jobs.LISTING_KIND, subject), admin=admin),
        photographs=await _photographs(db, listing.id),
        proposed=counts["proposed"],
        applied=counts["applied"],
        dismissed=counts["dismissed"],
        tone=ToneOut(from_="look" if examples else "preset", examples=examples),
        last_run=None if ran is None else LastRunOut(**ran),
        suggestions=suggestions,
    )


@router.get("/{listing_id}/local-edit", response_model=LocalEdit)
async def local_edit(
    listing_id: uuid.UUID, user: CurrentUser, db: DbDep, deps: DepsDep
) -> LocalEdit:
    """What the local model proposes for the listing's photographs, and
    whether it can be asked. Works where models cannot run: it then says why."""
    listing = await _listing(db, listing_id)
    return await _answer(db, listing, deps, admin=user.role == "admin")


# --- asking -------------------------------------------------------------------------


@router.post("/{listing_id}/local-edit", status_code=202, response_model=AskedOut)
async def ask_local_edit(
    listing_id: uuid.UUID,
    user: CurrentUser,
    db: DbDep,
    deps: DepsDep,
    body: AskIn | None = None,
) -> AskedOut:
    """Ask the Looker about the photographs with no suggestion yet (or, with
    `redo`, every one). Answers at once: the job runs on the local-AI lane and
    the page follows it in `job`. A model that only fits overnight is not
    refused; the job waits and says until when."""
    body = body or AskIn()
    listing = await _listing(db, listing_id)
    suggest = await _can_ask(db, deps)
    if not suggest.ok:
        raise HTTPException(409, suggest.note)
    subject = str(listing.id)
    current = await colour_jobs.job_for(db, colour_jobs.LISTING_KIND, subject)
    if current is not None and await jobs.reap(db, current):
        # Its worker died: ended now, as the lane's next tick would, so it
        # does not stand in the way of asking again.
        await db.commit()
    elif current is not None and jobs.shown(current)[0] in jobs.ACTIVE:
        raise HTTPException(409, ALREADY_LOOKING)
    photographs = await _photographs(db, listing.id)
    if not photographs:
        raise HTTPException(400, NO_PHOTOGRAPHS)
    to_look_at = photographs if body.redo else photographs - await _asked_about(db, listing.id)
    if to_look_at <= 0:
        raise HTTPException(409, ALL_PROPOSED)
    try:
        job = await colour_jobs.queue_listing(db, listing, user_id=user.id, redo=body.redo)
    except jobs.JobRefused:
        raise HTTPException(409, ALREADY_LOOKING) from None
    job_id = job.id
    await db.commit()
    if not await asyncio.to_thread(local_ai_tasks.wake, deps.settings):
        # Nothing is left waiting for a worker that will never come.
        await db.execute(delete(LocalAiJob).where(LocalAiJob.id == job_id))
        await db.commit()
        raise HTTPException(503, QUEUE_DOWN)
    log.info(
        "listing.local_look_queued",
        listing_id=subject,
        job_id=str(job_id),
        to_look_at=to_look_at,
        redo=body.redo,
    )
    found = await db.get(LocalAiJob, job_id, populate_existing=True)
    shown = colour_jobs.shown_job(found or job, admin=user.role == "admin")
    return AskedOut(job=JobOut(**shown), to_look_at=to_look_at)


@router.post("/{listing_id}/local-edit/cancel", response_model=CancelledOut)
async def cancel_local_edit(listing_id: uuid.UUID, user: CurrentUser, db: DbDep) -> CancelledOut:
    """Stop looking. A job still waiting ends at once; one under way stops
    before the next photograph. What it already proposed is kept."""
    listing = await _listing(db, listing_id)
    job = await colour_jobs.job_for(db, colour_jobs.LISTING_KIND, str(listing.id))
    if job is None or jobs.shown(job)[0] not in jobs.ACTIVE:
        raise HTTPException(409, NOTHING_LOOKING)
    try:
        job = await jobs.request_cancel(db, job.id)
    except (jobs.JobRefused, jobs.NoSuchJob):
        raise HTTPException(409, NOTHING_LOOKING) from None
    await db.commit()
    log.info("listing.local_look_cancelled", listing_id=str(listing.id), job_id=str(job.id))
    return CancelledOut(job=JobOut(**colour_jobs.shown_job(job, admin=user.role == "admin")))


@router.post("/{listing_id}/local-edit/dismiss", response_model=LocalEdit)
async def dismiss_local_edit(
    listing_id: uuid.UUID,
    user: CurrentUser,
    db: DbDep,
    deps: DepsDep,
    body: DismissIn | None = None,
) -> LocalEdit:
    """Put proposals aside without applying them: those named, or with no
    names every one still waiting. Nothing about the photographs changes, and
    asking again with `redo` brings them back."""
    body = body or DismissIn()
    listing = await _listing(db, listing_id)
    where = [
        ListingEditSuggestion.listing_id == listing.id,
        ListingEditSuggestion.state == colour_jobs.PROPOSED,
    ]
    if body.item_ids is not None:
        where.append(ListingEditSuggestion.item_id.in_(body.item_ids))
    dismissed: Any = await db.execute(
        update(ListingEditSuggestion)
        .where(*where)
        .values(state="dismissed", decided_at=datetime.now(UTC), decided_by=user.id)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    log.info(
        "listing.local_look_dismissed",
        listing_id=str(listing.id),
        dismissed=int(dismissed.rowcount or 0),
    )
    return await _answer(db, listing, deps, admin=user.role == "admin")


# --- the preview ------------------------------------------------------------------


def _render(
    source: Path, normalized: bool, look: Any, rotate: float, keystone: float, straighten: bool
) -> bytes:
    """The photograph as Apply would write it for the proposal, less the sky."""
    from PIL import Image

    # Decoded at the preview's size, as the editor's preview is: one page shows
    # a preview per proposal, and a 45 MP original decoded and colour-converted
    # whole for each would hold gigabytes and every worker thread.
    image = develop.open_for_render(source, already_normalized=normalized, max_edge=PREVIEW_EDGE)
    image.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.Resampling.LANCZOS)
    tone = looks.predict(look, image) if look is not None else None
    recipe = develop.clean_recipe(
        photo_judge.proposed_recipe(tone, rotate, keystone, straighten=straighten)
    )
    out = io.BytesIO()
    develop.render(image, recipe).convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY)
    return out.getvalue()


async def _is_file(path: Path) -> bool:
    try:
        return await asyncio.wait_for(asyncio.to_thread(path.is_file), colour_jobs.SHARE_WAIT_S)
    except TimeoutError:
        return False


@router.get("/{listing_id}/local-edit/{item_id}/preview.jpg")
async def local_edit_preview(
    listing_id: uuid.UUID,
    item_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    deps: DepsDep,
    straighten: bool = Query(default=True),
) -> Response:
    """The proposal, rendered: the photograph where its render starts (the
    object-removed version when there is one), with the learned look's tone
    or the preset's and, unless `straighten=false`, the model's straightening."""
    await _listing(db, listing_id)
    found = (
        await db.execute(
            select(ListingEditSuggestion, ListingItem, Asset, Library)
            .join(ListingItem, ListingItem.id == ListingEditSuggestion.item_id)
            .join(Asset, Asset.id == ListingItem.asset_id)
            .join(Library, Library.id == Asset.library_id)
            .where(
                ListingEditSuggestion.item_id == item_id,
                ListingEditSuggestion.listing_id == listing_id,
                ListingItem.listing_id == listing_id,
            )
        )
    ).first()
    if found is None:
        raise HTTPException(404, NO_SUGGESTION_HERE)
    row, item, asset, library = found
    settings = deps.settings
    # Every stat on the share is in a thread, and given a limit: a share that
    # hung must not hold every other request of the server with it.
    sources = await colour_jobs.render_sources(db, settings.data_dir, [(item, asset, library)])
    source = None if sources is None else sources.get(asset.id)
    if source is None or not await _is_file(source[0]):
        raise HTTPException(404, NOT_REACHABLE)
    look = await asyncio.to_thread(looks.load, settings.data_dir)
    try:
        body = await asyncio.to_thread(
            _render,
            source[0],
            source[1],
            look,
            float(row.rotate or 0.0),
            float(row.keystone or 0.0),
            straighten,
        )
    except OSError:
        raise HTTPException(404, NOT_REACHABLE) from None
    return Response(body, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
