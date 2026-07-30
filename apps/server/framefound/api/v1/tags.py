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
from sqlalchemy import func, select

from framefound.auth.deps import CurrentUser, DbDep
from framefound.db.models import Asset, AssetTag, Library, Tag

log = structlog.get_logger()

router = APIRouter(prefix="/tags", tags=["tags"])

# Slugs are for lookup and de-duplication; the display name keeps the
# operator's own capitalisation and spacing.
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return _SLUG_STRIP.sub("-", name.strip().lower()).strip("-")


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


class PendingSuggestion(BaseModel):
    asset_id: uuid.UUID
    filename: str
    media_type: str
    confidence: float | None


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
            .where(AssetTag.tag_id == tag_id, AssetTag.source == "suggested")
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
        )
        for link, asset in rows
    ]


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


@router.post("/export/fcp7")
async def export_fcp7(body: ExportRequest, _user: CurrentUser, db: DbDep) -> Response:
    """A search result set as FCP7 XML, importable by Premiere, Resolve or FCP.

    Deliberately not Premiere-specific: `.prproj` is undocumented, while FCP7
    XML is understood by every NLE the target users actually run. See ADR-0019.
    """
    from framefound.nle.fcp7 import Clip, build_bin

    rows = (
        (
            await db.execute(
                select(Asset)
                .join(Library, Library.id == Asset.library_id)
                .where(Asset.id.in_(body.asset_ids), Asset.availability == "online")
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
    clips = [
        Clip(
            name=asset.filename,
            # The path this server sees. A workstation mapping belongs here
            # eventually; until then the export is honest about what it wrote.
            path=f"{libraries[asset.library_id].root_path}/{asset.relative_path}",
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
