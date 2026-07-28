"""Search: transcript text + filenames with timestamped results (M4 slice).

This is the seed of the unified hybrid search (M5 adds vectors and RRF —
docs/architecture.md §search). On Postgres, transcript matching uses
websearch-style full-text queries against the GIN index from migration 0005;
the SQLite test dialect falls back to LIKE.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from framefound.auth.deps import CurrentUser, DbDep
from framefound.db.models import Asset, Transcript, TranscriptSegment

router = APIRouter(prefix="/search", tags=["search"])


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


class SearchResponse(BaseModel):
    query: str
    transcript_hits: list[TranscriptHit]
    filename_hits: list[FilenameHit]


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

    return SearchResponse(
        query=q,
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
