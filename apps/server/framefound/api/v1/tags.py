"""Tags: what the operator says a thing is, and what the system learned from it.

Every write here is training data. Adding a tag teaches the prototype;
accepting a suggestion confirms it; rejecting one is negative evidence that
stops it being offered again. So each of those actions re-runs the learner —
the point of the feature is that correcting it makes it better, immediately
and visibly.
"""

import re
import uuid
from datetime import datetime

import structlog
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.ai.tagging import slugify
from framefound.auth.deps import CurrentUser, DbDep, require_admin
from framefound.db.models import Asset, AssetTag, Library, Tag, catalogued

log = structlog.get_logger()

router = APIRouter(prefix="/tags", tags=["tags"])


class TagOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    example_count: int
    asset_count: int
    pending_count: int
    threshold: float | None
    threshold_reason: str
    learned_at: datetime | None
    suggest_enabled: bool


class AssetTagOut(BaseModel):
    tag_id: uuid.UUID
    name: str
    slug: str
    source: str
    confidence: float | None


class AddTagRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TagDecision(BaseModel):
    accept: bool


# The most assets one bulk answer may name. A tag's queue holds at most a
# few hundred (MAX_PENDING_PER_TAG plus arrivals), so this is never the
# thing that stops a real review — only a runaway client.
MAX_BULK_DECISIONS = 500


class BulkDecision(BaseModel):
    """Many answers about one tag at once: the assets that are it, and the
    assets that are not. Either list may be empty, not both."""

    accept: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_BULK_DECISIONS)
    reject: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_BULK_DECISIONS)


class BulkDecisionResult(BaseModel):
    accepted: int
    rejected: int
    # Named but not waiting for an answer: answered elsewhere in the
    # meantime (another tab, the asset page, Claude in chat), or never a
    # suggestion of this tag. Left exactly as they were.
    skipped: int


class PendingSuggestion(BaseModel):
    asset_id: uuid.UUID
    filename: str
    media_type: str
    confidence: float | None
    # Who suggested it: clip (the learner), claude (the API reviewer) or
    # mcp (Claude through the connector). The operator judges them alike,
    # but deserves to know which second opinion they are weighing.
    origin: str = "clip"


async def _counts(db: DbDep) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, int]]:
    """Confirmed and pending counts per tag, in two queries rather than 2N."""
    confirmed = {
        tag_id: count
        for tag_id, count in (
            await db.execute(
                select(AssetTag.tag_id, func.count())
                .where(AssetTag.source.in_(("manual", "confirmed")))
                .group_by(AssetTag.tag_id)
            )
        ).all()
    }
    pending = {
        tag_id: count
        for tag_id, count in (
            await db.execute(
                select(AssetTag.tag_id, func.count())
                .where(AssetTag.source == "suggested")
                .group_by(AssetTag.tag_id)
            )
        ).all()
    }
    return confirmed, pending


def _tag_out(tag: Tag, confirmed: int, pending: int) -> TagOut:
    return TagOut(
        id=tag.id,
        name=tag.name,
        slug=tag.slug,
        example_count=tag.example_count,
        asset_count=confirmed,
        pending_count=pending,
        threshold=tag.threshold,
        threshold_reason=tag.threshold_reason,
        learned_at=tag.learned_at,
        suggest_enabled=tag.suggest_enabled,
    )


@router.get("", response_model=list[TagOut])
async def list_tags(_user: CurrentUser, db: DbDep) -> list[TagOut]:
    tags = (await db.execute(select(Tag).order_by(Tag.name))).scalars().all()
    confirmed, pending = await _counts(db)
    return [_tag_out(tag, confirmed.get(tag.id, 0), pending.get(tag.id, 0)) for tag in tags]


class TagSuggestion(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    asset_count: int
    exact: bool


# Declared before `/{tag_id}/...` so "suggest" is never parsed as a tag id.
@router.get("/suggest", response_model=list[TagSuggestion])
async def suggest_tags(
    _user: CurrentUser,
    db: DbDep,
    q: str = Query(default="", max_length=120),
    limit: int = Query(default=8, ge=1, le=25),
) -> list[TagSuggestion]:
    """Existing tags matching what is being typed.

    Tags are matched by slug, so "Power Broom", "power broom" and "power-broom"
    are one tag rather than three. Without this the operator cannot see that
    until after they have created the duplicate — and by then the learned
    prototype is split across both, so each one matches worse than the single
    tag would have.

    Prefix matches rank above contains-matches, and the most-used tags first
    within each tier: the tag you want is usually the one you already use.
    """
    term = q.strip().lower()
    if not term:
        return []

    # Every match, then ranked here. A LIMIT without an ORDER BY kept an
    # arbitrary subset, so an exact match could be cut before ranking saw
    # it; the tag table is small enough to rank whole.
    tags = (
        (
            await db.execute(
                select(Tag).where(
                    func.lower(Tag.name).contains(term) | Tag.slug.contains(slugify(term))
                )
            )
        )
        .scalars()
        .all()
    )
    if not tags:
        return []

    confirmed, _pending = await _counts(db)

    def rank(tag: Tag) -> tuple[int, int, str]:
        lowered = tag.name.lower()
        tier = 0 if lowered == term else (1 if lowered.startswith(term) else 2)
        return (tier, -confirmed.get(tag.id, 0), lowered)

    return [
        TagSuggestion(
            id=tag.id,
            name=tag.name,
            slug=tag.slug,
            asset_count=confirmed.get(tag.id, 0),
            exact=tag.name.lower() == term,
        )
        for tag in sorted(tags, key=rank)[:limit]
    ]


def _relearn(tag_id: uuid.UUID) -> None:
    """Queue the learner. A queue that is down must not fail the operator's
    edit — the tag is saved either way, and the next edit will pick it up."""
    try:
        from framefound.processing.tag_tasks import learn_tag

        learn_tag.delay(str(tag_id))
    except Exception:
        log.warning("tagging.enqueue_failed", tag_id=str(tag_id))


@router.get("/assets/{asset_id}", response_model=list[AssetTagOut])
async def tags_for_asset(asset_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> list[AssetTagOut]:
    rows = (
        await db.execute(
            select(AssetTag, Tag)
            .join(Tag, Tag.id == AssetTag.tag_id)
            .where(AssetTag.asset_id == asset_id, AssetTag.source != "rejected")
            # Confirmed first, then the strongest suggestions.
            .order_by(AssetTag.source == "suggested", AssetTag.confidence.desc().nullslast())
        )
    ).all()
    return [
        AssetTagOut(
            tag_id=tag.id,
            name=tag.name,
            slug=tag.slug,
            source=link.source,
            confidence=link.confidence,
        )
        for link, tag in rows
    ]


@router.post("/assets/{asset_id}", response_model=list[AssetTagOut], status_code=201)
async def add_tag_to_asset(
    asset_id: uuid.UUID, body: AddTagRequest, user: CurrentUser, db: DbDep
) -> list[AssetTagOut]:
    """Tag an asset by name, creating the tag if it is new.

    This is the action the whole feature turns on: the operator says what a
    thing is, and the system goes and finds more of it.
    """
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="No such asset")

    slug = slugify(body.name)
    if not slug:
        raise HTTPException(status_code=400, detail="That tag name has no usable characters")

    tag = (await db.execute(select(Tag).where(Tag.slug == slug))).scalar_one_or_none()
    if tag is None:
        tag = Tag(name=body.name.strip(), slug=slug)
        db.add(tag)
        await db.flush()

    link = (
        await db.execute(
            select(AssetTag).where(AssetTag.asset_id == asset_id, AssetTag.tag_id == tag.id)
        )
    ).scalar_one_or_none()
    if link is None:
        db.add(AssetTag(asset_id=asset_id, tag_id=tag.id, source="manual", created_by=user.id))
    else:
        # Applying a tag by hand overrides a suggestion or a past rejection —
        # the operator is the authority, and this is now a positive example.
        link.source = "manual"
        link.confidence = None
        link.created_by = user.id
    await db.commit()
    _relearn(tag.id)
    return await tags_for_asset(asset_id, user, db)


@router.delete("/assets/{asset_id}/{tag_id}", status_code=204)
async def remove_tag_from_asset(
    asset_id: uuid.UUID, tag_id: uuid.UUID, _user: CurrentUser, db: DbDep
) -> None:
    """Remove a tag. Recorded as a rejection, not a deletion.

    Deleting the row would let the same suggestion come back on the next run.
    Keeping it as `rejected` is both a memory and a negative example, which is
    what makes the threshold tighten instead of drifting.
    """
    link = (
        await db.execute(
            select(AssetTag).where(AssetTag.asset_id == asset_id, AssetTag.tag_id == tag_id)
        )
    ).scalar_one_or_none()
    if link is None:
        return
    link.source = "rejected"
    link.confidence = None
    await db.commit()
    _relearn(tag_id)


@router.post("/assets/{asset_id}/{tag_id}/decide", response_model=list[AssetTagOut])
async def decide_suggestion(
    asset_id: uuid.UUID, tag_id: uuid.UUID, body: TagDecision, user: CurrentUser, db: DbDep
) -> list[AssetTagOut]:
    """Accept or reject a suggested tag. Both answers teach."""
    link = (
        await db.execute(
            select(AssetTag).where(AssetTag.asset_id == asset_id, AssetTag.tag_id == tag_id)
        )
    ).scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=404, detail="No such suggestion")
    link.source = "confirmed" if body.accept else "rejected"
    link.created_by = user.id
    if not body.accept:
        link.confidence = None
    await db.commit()
    _relearn(tag_id)
    return await tags_for_asset(asset_id, user, db)


async def decide_many(
    db: AsyncSession,
    tag_id: uuid.UUID,
    accept: list[uuid.UUID],
    reject: list[uuid.UUID],
    user_id: uuid.UUID | None,
) -> BulkDecisionResult:
    """Answer many suggestions of one tag in one transaction, then re-learn
    the tag once.

    Shared by the Tags page and the MCP connector. Answering one at a time
    queued a full re-learn per answer — and a re-learn now scores the whole
    library — so sixty answers were sixty library passes for one decision's
    worth of change.

    Only rows still waiting (`source == "suggested"`) change. Anything the
    operator already settled — a manual tag, a yes, a no — is left alone even
    if a stale page names it, because a bulk click must never overturn a
    decision someone made one at a time.
    """
    both = set(accept) & set(reject)
    if both:
        raise ValueError(f"{len(both)} item(s) are in both lists; answer each once")
    if not accept and not reject:
        raise ValueError("Nothing to decide: give assets to accept or reject")
    if await db.get(Tag, tag_id) is None:
        raise LookupError("No such tag")

    named = len(set(accept) | set(reject))

    async def settle(ids: list[uuid.UUID], **values: object) -> int:
        """One conditional UPDATE: the "still waiting" check and the write are
        the same statement. Reading the rows first and writing them by id
        let an answer committed in between (the asset page, another tab,
        Claude) be overwritten; under READ COMMITTED Postgres re-checks this
        WHERE after any row lock it waits on, so a row settled meanwhile is
        simply not matched — and counted as skipped."""
        if not ids:
            return 0
        result = await db.execute(
            update(AssetTag)
            .where(
                AssetTag.tag_id == tag_id,
                AssetTag.asset_id.in_(ids),
                AssetTag.source == "suggested",
            )
            .values(created_by=user_id, **values)
            .returning(AssetTag.asset_id)
            .execution_options(synchronize_session=False)
        )
        return len(result.all())

    accepted = await settle(list(set(accept)), source="confirmed")
    rejected = await settle(list(set(reject)), source="rejected", confidence=None)
    await db.commit()
    if accepted or rejected:
        _relearn(tag_id)
    skipped = named - accepted - rejected
    log.info(
        "tagging.bulk_decided",
        tag_id=str(tag_id),
        accepted=accepted,
        rejected=rejected,
        skipped=skipped,
    )
    return BulkDecisionResult(accepted=accepted, rejected=rejected, skipped=skipped)


@router.post("/{tag_id}/decide", response_model=BulkDecisionResult)
async def decide_suggestions_in_bulk(
    tag_id: uuid.UUID, body: BulkDecision, user: CurrentUser, db: DbDep
) -> BulkDecisionResult:
    """Answer many of one tag's suggestions at once (the Tags page's bulk
    review). Both answers teach; the tag is re-learned once."""
    try:
        return await decide_many(db, tag_id, body.accept, body.reject, user.id)
    except LookupError as err:
        raise HTTPException(status_code=404, detail=str(err)) from err
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err


@router.get("/{tag_id}/pending", response_model=list[PendingSuggestion])
async def pending_for_tag(
    tag_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    limit: int = Query(default=60, ge=1, le=200),
) -> list[PendingSuggestion]:
    """Suggestions awaiting judgement, strongest first — a review queue."""
    rows = (
        await db.execute(
            select(AssetTag, Asset)
            .join(Asset, Asset.id == AssetTag.asset_id)
            .where(AssetTag.tag_id == tag_id, AssetTag.source == "suggested", catalogued())
            .order_by(AssetTag.confidence.desc().nullslast())
            .limit(limit)
        )
    ).all()
    return [
        PendingSuggestion(
            asset_id=asset.id,
            filename=asset.filename,
            media_type=asset.media_type,
            confidence=link.confidence,
            origin=link.origin or "clip",
        )
        for link, asset in rows
    ]


# --- a second opinion from Claude, under the budget -----------------------
#
# Declared before `/{tag_id}/relearn` so "review" is never parsed as a tag id.


class ClaudeReviewState(BaseModel):
    state: str
    started_at: str
    finished_at: str
    requested: int
    reviewed: int
    suggested: int
    new_tags: int
    skipped: int
    failed: int
    spent_usd: float
    message: str


class ClaudeReviewStatus(BaseModel):
    configured: bool
    model: str
    budget_usd: float
    spent_this_month_usd: float
    spent_on_review_usd: float
    remaining_usd: float
    auto: bool
    batch: int
    untagged: int
    pending_from_claude: int
    last: ClaudeReviewState


class ClaudeReviewRequest(BaseModel):
    asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    # 0 = the configured batch size.
    limit: int = Field(default=0, ge=0, le=500)
    library_id: uuid.UUID | None = None


async def queue_claude_review(
    db: AsyncSession, asset_ids: list[uuid.UUID], limit: int, library_id: uuid.UUID | None = None
) -> tuple[int, str]:
    """Hand pictures to the reviewer. Refuses, with a reason, when there is
    no key, no budget left, a run already going, or nothing to review —
    each of which the caller (the page or Claude) should relay as it is.
    Shared by the endpoint below and the MCP connector."""
    from framefound.ai import spend, tag_review
    from framefound.media.maps_store import load_ai_edit_config, load_review_state

    config = await load_ai_edit_config(db)
    if not config.ready:
        raise ValueError("Reviewing with Claude needs an Anthropic key on the Security page")
    spent = await spend.spent_this_month(db)
    if spent >= config.monthly_budget_usd:
        raise ValueError(
            f"This month's AI budget is used up (${spent:.2f} of "
            f"${config.monthly_budget_usd:.2f}); raise it on the Security page to continue"
        )
    state = await load_review_state(db)
    if state.state == "running":
        raise ValueError("A review is already running — it will finish on its own")

    if asset_ids:
        chosen = asset_ids[:500]
    else:
        chosen = await tag_review.pick_untagged(db, limit or config.review_batch, library_id)
    if not chosen:
        return (
            0,
            "Nothing to review: every picture with a thumbnail has been tagged, answered, "
            "or already reviewed by Claude",
        )
    try:
        from framefound.processing.tag_review_tasks import review_tags_with_claude

        review_tags_with_claude.delay([str(asset_id) for asset_id in chosen])
    except Exception as err:  # queue down
        raise ValueError("The processing queue is not available right now") from err
    model = config.review_model or config.model
    return len(chosen), (
        f"{len(chosen)} picture{'' if len(chosen) == 1 else 's'} queued for {model}; "
        f"${config.monthly_budget_usd - spent:.2f} of this month's budget remains"
    )


@router.get("/review/claude", response_model=ClaudeReviewStatus)
async def claude_review_status(_user: CurrentUser, db: DbDep) -> ClaudeReviewStatus:
    """Where the reviewer stands: key, budget and what is left of it, how
    many pictures nobody has tagged, and what the last run did."""
    from framefound.ai import spend, tag_review
    from framefound.media.maps_store import load_ai_edit_config, load_review_state

    config = await load_ai_edit_config(db)
    spent = await spend.spent_this_month(db)
    on_review = await spend.spent_this_month(db, spend.TAG_REVIEW)
    pending = int(
        (
            await db.execute(
                select(func.count())
                .select_from(AssetTag)
                .where(AssetTag.source == "suggested", AssetTag.origin == "claude")
            )
        ).scalar_one()
    )
    state = await load_review_state(db)
    return ClaudeReviewStatus(
        configured=config.ready,
        model=config.review_model or config.model,
        budget_usd=config.monthly_budget_usd,
        spent_this_month_usd=round(spent, 4),
        spent_on_review_usd=round(on_review, 4),
        remaining_usd=round(max(0.0, config.monthly_budget_usd - spent), 4),
        auto=config.review_auto,
        batch=config.review_batch,
        untagged=await tag_review.count_untagged(db),
        pending_from_claude=pending,
        last=ClaudeReviewState(**state.__dict__),
    )


@router.post("/review/claude", status_code=202, dependencies=[require_admin])
async def start_claude_review(
    body: ClaudeReviewRequest, user: CurrentUser, db: DbDep
) -> dict[str, int | str]:
    """Send pictures to the Claude API for tag proposals, under the budget:
    the given assets, or the newest untagged ones. Suggestions land in the
    review queue marked "claude"; nothing is applied."""
    try:
        queued, message = await queue_claude_review(db, body.asset_ids, body.limit, body.library_id)
    except ValueError as err:
        raise HTTPException(status_code=409, detail=str(err)) from None
    log.info("tag_review.queued", queued=queued, by=str(user.id))
    return {"queued": queued, "message": message}


@router.post("/{tag_id}/relearn", status_code=202)
async def relearn_tag(tag_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> dict[str, str]:
    """Re-run learning by hand. Useful after new assets are embedded."""
    tag = await db.get(Tag, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="No such tag")
    _relearn(tag_id)
    return {"status": "queued"}


class ExportRequest(BaseModel):
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    name: str = Field(default="FrameFound results", max_length=120)
    # The workstation's path profile (Libraries page). Without one the bin
    # carries the server's /media/... paths and imports offline everywhere.
    profile: str = Field(default="", max_length=100)


@router.post("/export/fcp7")
async def export_fcp7(body: ExportRequest, _user: CurrentUser, db: DbDep) -> Response:
    """A search result set as FCP7 XML, importable by Premiere, Resolve or FCP.

    Deliberately not Premiere-specific: `.prproj` is undocumented, while FCP7
    XML is understood by every NLE the target users actually run. See ADR-0019.
    """
    from framefound.api.v1.panel import _profiles, _translate_for
    from framefound.nle.fcp7 import Clip, build_bin

    rows = (
        (
            await db.execute(
                select(Asset)
                .join(Library, Library.id == Asset.library_id)
                .where(Asset.id.in_(body.asset_ids), Asset.availability == "online", catalogued())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="None of those assets are available")

    libraries = {
        library.id: library for library in (await db.execute(select(Library))).scalars().all()
    }
    profiles = await _profiles(db, body.profile)
    clips = [
        Clip(
            name=asset.filename,
            # The workstation's path when the profile covers the library, as
            # the panel's export does; the server's otherwise, honestly.
            path=_translate_for(asset, profiles)
            or f"{libraries[asset.library_id].root_path}/{asset.relative_path}",
            duration_s=asset.duration_s,
            fps=asset.fps,
            width=asset.width,
            height=asset.height,
            has_audio=asset.audio_codec is not None or asset.media_type == "audio",
        )
        for asset in rows
    ]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", body.name).strip("-") or "framefound"
    return Response(
        content=build_bin(body.name, clips),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{safe}.xml"'},
    )
