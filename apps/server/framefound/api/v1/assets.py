"""Asset browsing endpoints + signed media URLs + admin reprocessing."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.sql.elements import UnaryExpression

from framefound.auth.deps import CurrentUser, DbDep, SettingsDep, require_admin
from framefound.db.models import Asset, Derivative
from framefound.media.signing import SigningError, sign_media_url

router = APIRouter(prefix="/assets", tags=["assets"])


class AssetSummary(BaseModel):
    id: uuid.UUID
    library_id: uuid.UUID
    relative_path: str
    filename: str
    media_type: str
    size_bytes: int
    mtime: datetime
    availability: str
    processing_status: str
    duration_s: float | None
    width: int | None
    height: int | None
    captured_at: datetime | None

    model_config = {"from_attributes": True}


class AssetDetail(AssetSummary):
    extension: str
    mime_type: str | None
    partial_hash: str | None
    content_hash: str | None
    fps: float | None
    video_codec: str | None
    audio_codec: str | None
    sample_rate: int | None
    channels: int | None
    bitrate: int | None
    orientation: int | None
    camera_make: str | None
    camera_model: str | None
    lens: str | None
    focal_length_mm: float | None
    aperture_f: float | None
    shutter_speed: str | None
    iso: int | None
    gps_lat: float | None
    gps_lon: float | None
    rating: int | None
    favorite: bool
    archived: bool
    title: str | None
    description: str | None
    custom_fields: dict[str, Any]
    first_indexed_at: datetime


class AssetPage(BaseModel):
    items: list[AssetSummary]
    total: int
    page: int
    page_size: int


@router.get("", response_model=AssetPage)
async def list_assets(
    _user: CurrentUser,
    db: DbDep,
    library_id: uuid.UUID | None = None,
    media_type: str | None = Query(default=None, pattern="^(image|video|audio)$"),
    availability: str | None = Query(default=None, pattern="^(online|missing|unmounted)$"),
    status: str | None = Query(default=None, max_length=30),
    previewable: bool = Query(
        default=False, description="Only assets that already have a thumbnail"
    ),
    sort: str = Query(default="recent", pattern="^(recent|captured|name|size)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> AssetPage:
    query = select(Asset)
    if library_id is not None:
        query = query.where(Asset.library_id == library_id)
    if media_type is not None:
        query = query.where(Asset.media_type == media_type)
    if availability is not None:
        query = query.where(Asset.availability == availability)
    if status is not None:
        query = query.where(Asset.processing_status == status)
    if previewable:
        ready_thumbs = select(Derivative.asset_id).where(
            Derivative.kind == "thumbnail", Derivative.status == "ready"
        )
        query = query.where(Asset.id.in_(ready_thumbs))

    orders: dict[str, UnaryExpression[Any]] = {
        "recent": Asset.first_indexed_at.desc(),
        "captured": Asset.captured_at.desc().nullslast(),
        "name": Asset.filename.asc(),
        "size": Asset.size_bytes.desc(),
    }
    order = orders[sort]

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(query.order_by(order).offset((page - 1) * page_size).limit(page_size))
    ).scalars()
    return AssetPage(
        items=[AssetSummary.model_validate(a) for a in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{asset_id}", response_model=AssetDetail)
async def get_asset(asset_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> AssetDetail:
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    return AssetDetail.model_validate(asset)


class TranscriptSegmentOut(BaseModel):
    start_ms: int
    end_ms: int
    text: str
    speaker: str | None
    confidence: float | None

    model_config = {"from_attributes": True}


class TranscriptOut(BaseModel):
    language: str
    language_confidence: float | None
    model_name: str
    processed_at: datetime
    segment_count: int
    segments: list[TranscriptSegmentOut]


@router.get("/{asset_id}/transcript", response_model=TranscriptOut)
async def get_transcript(asset_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> TranscriptOut:
    from sqlalchemy.orm import selectinload

    from framefound.db.models import Transcript

    transcript = (
        await db.execute(
            select(Transcript)
            .where(Transcript.asset_id == asset_id)
            .options(selectinload(Transcript.segments))
        )
    ).scalar_one_or_none()
    if transcript is None:
        raise HTTPException(404, "No transcript is available for this item")
    return TranscriptOut(
        language=transcript.language,
        language_confidence=transcript.language_confidence,
        model_name=transcript.model_name,
        processed_at=transcript.processed_at,
        segment_count=transcript.segment_count,
        segments=[TranscriptSegmentOut.model_validate(s) for s in transcript.segments],
    )


class MediaUrls(BaseModel):
    """Signed, short-lived URLs for each ready derivative of an asset."""

    urls: dict[str, str]
    expires_in_seconds: int


@router.get("/{asset_id}/urls", response_model=MediaUrls)
async def signed_media_urls(
    asset_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    settings: SettingsDep,
    ttl: int = Query(default=3600, ge=60, le=86400),
) -> MediaUrls:
    if await db.get(Asset, asset_id) is None:
        raise HTTPException(404, "Asset not found")
    kinds = (
        (
            await db.execute(
                select(Derivative.kind).where(
                    Derivative.asset_id == asset_id, Derivative.status == "ready"
                )
            )
        )
        .scalars()
        .all()
    )
    urls: dict[str, str] = {}
    for kind in kinds:
        try:
            expires, sig = sign_media_url(settings.secret_key, asset_id, kind, ttl)
        except SigningError as err:
            raise HTTPException(503, "Media links are not configured on this server") from err
        urls[kind] = f"/api/v1/media/{asset_id}/{kind}?exp={expires}&sig={sig}"
    return MediaUrls(urls=urls, expires_in_seconds=ttl)


@router.post("/{asset_id}/reprocess", status_code=202, dependencies=[require_admin])
async def reprocess_asset(asset_id: uuid.UUID, db: DbDep, _user: CurrentUser) -> dict[str, str]:
    """Re-run derivative generation for one asset (admin)."""
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(404, "Asset not found")
    try:
        from framefound.processing.tasks import generate_derivatives, generate_proxy

        generate_derivatives.delay(str(asset_id))
        if asset.media_type == "video":
            generate_proxy.delay(str(asset_id))
    except Exception as err:  # queue down
        raise HTTPException(503, "Processing queue is not available right now") from err
    return {"status": "queued"}
