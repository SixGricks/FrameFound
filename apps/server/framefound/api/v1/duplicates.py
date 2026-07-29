"""Duplicate detection.

Two kinds, because editors create two kinds:

- **Identical** — the same bytes in two places: render copies, backup folders,
  a project duplicated before a re-cut. Matched on (size, partial hash), which
  every asset already carries. Reclaimable space is real.
- **Near-identical** — the same picture re-encoded or resized: a 4K master and
  its 1080p export, a JPEG re-saved at another quality. Matched on the
  perceptual hash, where the bytes differ entirely but the image does not.

Nothing here deletes anything. FrameFound reports what it found and where the
copies live; removing files is the operator's decision, taken in their own
file manager, against originals this application cannot write to.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from framefound.auth.deps import CurrentUser, DbDep
from framefound.db.models import Asset, Frame

router = APIRouter(prefix="/duplicates", tags=["duplicates"])


class DuplicateMember(BaseModel):
    asset_id: uuid.UUID
    library_id: uuid.UUID
    relative_path: str
    filename: str
    size_bytes: int
    mtime: datetime
    content_hash_verified: bool


class DuplicateGroup(BaseModel):
    key: str
    kind: str  # identical | similar
    count: int
    size_bytes: int  # size of one copy
    reclaimable_bytes: int  # what removing the extras would free
    members: list[DuplicateMember]


class DuplicateReport(BaseModel):
    groups: list[DuplicateGroup]
    total_groups: int
    total_reclaimable_bytes: int
    note: str


def _member(asset: Asset) -> DuplicateMember:
    return DuplicateMember(
        asset_id=asset.id,
        library_id=asset.library_id,
        relative_path=asset.relative_path,
        filename=asset.filename,
        size_bytes=asset.size_bytes,
        mtime=asset.mtime,
        content_hash_verified=asset.content_hash is not None,
    )


@router.get("", response_model=DuplicateReport)
async def find_duplicates(
    _user: CurrentUser,
    db: DbDep,
    kind: str = Query(default="identical", pattern="^(identical|similar)$"),
    min_size_mb: float = Query(default=1.0, ge=0),
    library_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> DuplicateReport:
    min_bytes = int(min_size_mb * 1024 * 1024)

    if kind == "identical":
        grouped = (
            select(
                Asset.partial_hash.label("key"),
                Asset.size_bytes.label("size"),
                func.count().label("n"),
            )
            .where(
                Asset.partial_hash.is_not(None),
                Asset.availability == "online",
                Asset.size_bytes >= min_bytes,
            )
            .group_by(Asset.partial_hash, Asset.size_bytes)
            .having(func.count() > 1)
        )
        if library_id is not None:
            grouped = grouped.where(Asset.library_id == library_id)
        rows = (
            await db.execute(grouped.order_by((func.count() - 1) * Asset.size_bytes).limit(limit))
        ).all()

        groups = []
        for key, size, _count in rows:
            member_q = select(Asset).where(
                Asset.partial_hash == key,
                Asset.size_bytes == size,
                Asset.availability == "online",
            )
            # The same scope as the grouping query, or the member list and the
            # count describe different sets and the saving comes out wrong.
            if library_id is not None:
                member_q = member_q.where(Asset.library_id == library_id)
            members = (await db.execute(member_q)).scalars().all()
            groups.append(
                DuplicateGroup(
                    key=key,
                    kind="identical",
                    count=len(members),
                    size_bytes=size,
                    reclaimable_bytes=(len(members) - 1) * size,
                    members=[_member(a) for a in members],
                )
            )
    else:
        # Perceptual match: same picture, different bytes. Grouped on the
        # first frame's hash, so a still and a video's opening frame can pair.
        grouped_frames = (
            select(Frame.phash, func.count(func.distinct(Frame.asset_id)).label("n"))
            .where(Frame.phash.is_not(None), Frame.ts_ms == 0)
            .group_by(Frame.phash)
            .having(func.count(func.distinct(Frame.asset_id)) > 1)
            .limit(limit)
        )
        groups = []
        for phash, _count in (await db.execute(grouped_frames)).all():
            member_query = (
                select(Asset)
                .join(Frame, Frame.asset_id == Asset.id)
                .where(
                    Frame.phash == phash,
                    Frame.ts_ms == 0,
                    Asset.availability == "online",
                    Asset.size_bytes >= min_bytes,
                )
            )
            if library_id is not None:
                member_query = member_query.where(Asset.library_id == library_id)
            members = (await db.execute(member_query)).scalars().all()
            if len(members) < 2:
                continue
            largest = max(a.size_bytes for a in members)
            total = sum(a.size_bytes for a in members)
            groups.append(
                DuplicateGroup(
                    key=phash,
                    kind="similar",
                    count=len(members),
                    size_bytes=largest,
                    # Keeping the highest-quality copy is the usual intent, so
                    # the saving is everything except the largest file.
                    reclaimable_bytes=total - largest,
                    members=[_member(a) for a in members],
                )
            )

    groups.sort(key=lambda g: -g.reclaimable_bytes)
    return DuplicateReport(
        groups=groups,
        total_groups=len(groups),
        total_reclaimable_bytes=sum(g.reclaimable_bytes for g in groups),
        note=(
            "Identical matches are confirmed on size and a content hash of the "
            "file's edges. Run verification for a full-content check before "
            "deleting anything."
        )
        if kind == "identical"
        else (
            "Near-identical matches look the same but are different files — "
            "usually a master and its export. Compare before removing either."
        ),
    )


class VerifyRequest(BaseModel):
    asset_ids: list[uuid.UUID]


@router.post("/verify", status_code=202)
async def verify_group(body: VerifyRequest, _user: CurrentUser, db: DbDep) -> dict[str, str | int]:
    """Queue full-content hashing so a match can be trusted before deletion.

    The partial hash samples a file's edges; that is right for change
    detection but too weak to justify deleting someone's footage on.
    """
    known = (
        (
            await db.execute(
                select(Asset.id).where(
                    Asset.id.in_(body.asset_ids[:200]), Asset.availability == "online"
                )
            )
        )
        .scalars()
        .all()
    )
    try:
        from framefound.processing.tasks import verify_content_hash

        for asset_id in known:
            verify_content_hash.delay(str(asset_id))
    except Exception:
        return {"status": "queue unavailable", "queued": 0}
    return {"status": "queued", "queued": len(known)}
