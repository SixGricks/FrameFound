"""Search: transcript text + filenames with timestamped results (M4 slice).

This is the seed of the unified hybrid search (M5 adds vectors and RRF —
docs/architecture.md §search). On Postgres, transcript matching uses
websearch-style full-text queries against the GIN index from migration 0005;
the SQLite test dialect falls back to LIKE.
"""

import asyncio
import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from framefound.auth.deps import CurrentUser, DbDep
from framefound.db.models import Asset, AssetTag, Tag, Transcript, TranscriptSegment

router = APIRouter(prefix="/search", tags=["search"])


def _dot(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity for L2-normalised vectors (SQLite fallback)."""
    if not a or not b:
        return 0.0
    return float(sum(x * y for x, y in zip(a, b, strict=False)))


class TranscriptHit(BaseModel):
    asset_id: uuid.UUID
    filename: str
    library_id: uuid.UUID
    media_type: str
    start_ms: int
    end_ms: int
    text: str


class FilenameHit(BaseModel):
    asset_id: uuid.UUID
    filename: str
    library_id: uuid.UUID
    media_type: str
    captured_at: datetime | None


class TagHit(BaseModel):
    """An asset carrying a tag whose name matches the query.

    Ranked above everything else, because a tag is a human judgement about what
    the thing is. A filename or a CLIP score is a guess; someone typed this.
    """

    asset_id: uuid.UUID
    filename: str
    library_id: uuid.UUID
    media_type: str
    tag_name: str
    tag_slug: str
    confirmed: bool  # false for a suggestion still awaiting judgement


class VisualHit(BaseModel):
    asset_id: uuid.UUID
    filename: str
    library_id: uuid.UUID
    media_type: str
    ts_ms: int
    similarity: float


class SearchResponse(BaseModel):
    query: str
    transcript_hits: list[TranscriptHit]
    filename_hits: list[FilenameHit]
    visual_hits: list[VisualHit] = []
    visual_available: bool = True  # false when nothing has been indexed yet
    tag_hits: list[TagHit] = []


async def _visual_search(
    db: DbDep, query: str, library_id: uuid.UUID | None, limit: int
) -> tuple[list[VisualHit], bool]:
    """Nearest frames to the query in CLIP space.

    Postgres uses the HNSW index via pgvector's cosine operator. SQLite (the
    test dialect) has no vector type, so similarity is computed in Python over
    the small fixture set — same results, different scale.
    """
    from framefound.ai.embeddings import EmbeddingUnavailable, get_embedding_provider
    from framefound.db.models import Frame

    indexed = (
        await db.execute(
            select(func.count()).select_from(Frame).where(Frame.embedding.is_not(None))
        )
    ).scalar_one()
    if not indexed:
        return [], False

    try:
        vector = (await asyncio.to_thread(get_embedding_provider().embed_text, query)).vector
    except (EmbeddingUnavailable, Exception):
        return [], False

    if db.get_bind().dialect.name == "postgresql":
        stmt = (
            select(Frame, Asset, Frame.embedding.cosine_distance(vector).label("distance"))
            .join(Asset, Asset.id == Frame.asset_id)
            .where(Frame.embedding.is_not(None))
        )
        if library_id is not None:
            stmt = stmt.where(Asset.library_id == library_id)
        rows = (await db.execute(stmt.order_by("distance").limit(limit))).all()
        scored = [(frame, asset, 1.0 - float(distance)) for frame, asset, distance in rows]
    else:
        stmt = (
            select(Frame, Asset)
            .join(Asset, Asset.id == Frame.asset_id)
            .where(Frame.embedding.is_not(None))
        )
        if library_id is not None:
            stmt = stmt.where(Asset.library_id == library_id)
        candidates = (await db.execute(stmt)).all()
        scored = sorted(
            ((frame, asset, _dot(vector, frame.embedding)) for frame, asset in candidates),
            key=lambda row: -row[2],
        )[:limit]

    return [
        VisualHit(
            asset_id=asset.id,
            filename=asset.filename,
            library_id=asset.library_id,
            media_type=asset.media_type,
            ts_ms=frame.ts_ms,
            similarity=round(similarity, 4),
        )
        for frame, asset, similarity in scored
    ], True


@router.get("", response_model=SearchResponse)
async def search(
    _user: CurrentUser,
    db: DbDep,
    q: str = Query(min_length=2, max_length=200),
    library_id: uuid.UUID | None = None,
    limit: int = Query(default=25, ge=1, le=100),
) -> SearchResponse:
    dialect = db.get_bind().dialect.name

    seg_query = (
        select(TranscriptSegment, Asset)
        .join(Transcript, Transcript.id == TranscriptSegment.transcript_id)
        .join(Asset, Asset.id == Transcript.asset_id)
    )
    if dialect == "postgresql":
        seg_query = seg_query.where(
            func.to_tsvector("english", TranscriptSegment.text).op("@@")(
                func.websearch_to_tsquery("english", q)
            )
        )
    else:  # tests / SQLite
        seg_query = seg_query.where(TranscriptSegment.text.ilike(f"%{q}%"))
    if library_id is not None:
        seg_query = seg_query.where(Asset.library_id == library_id)
    seg_rows = (await db.execute(seg_query.order_by(TranscriptSegment.start_ms).limit(limit))).all()

    name_query = select(Asset).where(
        or_(Asset.filename.ilike(f"%{q}%"), Asset.relative_path.ilike(f"%{q}%"))
    )
    if library_id is not None:
        name_query = name_query.where(Asset.library_id == library_id)
    name_rows = (await db.execute(name_query.limit(limit))).scalars().all()

    visual_hits, visual_available = await _visual_search(db, q, library_id, limit)
    tag_hits = await _tag_search(db, q, library_id, limit)

    return SearchResponse(
        query=q,
        visual_hits=visual_hits,
        visual_available=visual_available,
        tag_hits=tag_hits,
        transcript_hits=[
            TranscriptHit(
                asset_id=asset.id,
                filename=asset.filename,
                library_id=asset.library_id,
                media_type=asset.media_type,
                start_ms=seg.start_ms,
                end_ms=seg.end_ms,
                text=seg.text,
            )
            for seg, asset in seg_rows
        ],
        filename_hits=[
            FilenameHit(
                asset_id=a.id,
                filename=a.filename,
                library_id=a.library_id,
                media_type=a.media_type,
                captured_at=a.captured_at,
            )
            for a in name_rows
        ],
    )


async def _tag_search(
    db: DbDep, query: str, library_id: uuid.UUID | None, limit: int
) -> list["TagHit"]:
    """Assets carrying a tag whose name matches.

    Confirmed tags come first: a suggestion the operator has not judged yet is
    a claim, and mixing the two would present a guess with the same authority
    as a decision.

    Rejected links are excluded — the operator has already said no, and
    resurrecting it in search results would be the same nagging the tagging
    model was designed to avoid.
    """
    stmt = (
        select(Asset, Tag, AssetTag.source)
        .join(AssetTag, AssetTag.asset_id == Asset.id)
        .join(Tag, Tag.id == AssetTag.tag_id)
        .where(
            Tag.name.ilike(f"%{query}%"),
            AssetTag.source != "rejected",
            Asset.availability == "online",
        )
        .order_by(AssetTag.source == "suggested", Asset.mtime.desc())
        .limit(limit)
    )
    if library_id is not None:
        stmt = stmt.where(Asset.library_id == library_id)
    return [
        TagHit(
            asset_id=asset.id,
            filename=asset.filename,
            library_id=asset.library_id,
            media_type=asset.media_type,
            tag_name=tag.name,
            tag_slug=tag.slug,
            confirmed=source != "suggested",
        )
        for asset, tag, source in (await db.execute(stmt)).all()
    ]


@router.get("/similar/{asset_id}", response_model=list[VisualHit])
async def similar_assets(
    asset_id: uuid.UUID, _user: CurrentUser, db: DbDep, limit: int = Query(default=12, ge=1, le=50)
) -> list[VisualHit]:
    """Visually closest frames from *other* assets — 'more like this'."""
    from framefound.db.models import Frame

    reference = (
        await db.execute(
            select(Frame)
            .where(Frame.asset_id == asset_id, Frame.embedding.is_not(None))
            .order_by(Frame.ts_ms)
            .limit(1)
        )
    ).scalar_one_or_none()
    if reference is None:
        raise HTTPException(404, "This item has not been visually indexed yet")

    stmt = (
        select(Frame, Asset)
        .join(Asset, Asset.id == Frame.asset_id)
        .where(Frame.embedding.is_not(None), Frame.asset_id != asset_id)
    )
    if db.get_bind().dialect.name == "postgresql":
        distance = Frame.embedding.cosine_distance(reference.embedding).label("distance")
        rows = (
            await db.execute(
                select(Frame, Asset, distance)
                .join(Asset, Asset.id == Frame.asset_id)
                .where(Frame.embedding.is_not(None), Frame.asset_id != asset_id)
                .order_by("distance")
                .limit(limit)
            )
        ).all()
        scored = [(f, a, 1.0 - float(d)) for f, a, d in rows]
    else:
        candidates = (await db.execute(stmt)).all()
        scored = sorted(
            (
                (frame, asset, _dot(reference.embedding, frame.embedding))
                for frame, asset in candidates
            ),
            key=lambda row: -row[2],
        )[:limit]

    # One hit per asset: twenty frames of the same clip is not "similar media".
    seen: set[uuid.UUID] = set()
    results: list[VisualHit] = []
    for frame, asset, similarity in scored:
        if asset.id in seen:
            continue
        seen.add(asset.id)
        results.append(
            VisualHit(
                asset_id=asset.id,
                filename=asset.filename,
                library_id=asset.library_id,
                media_type=asset.media_type,
                ts_ms=frame.ts_ms,
                similarity=round(similarity, 4),
            )
        )
    return results
