"""Asset browsing endpoints (read-only in M2; media serving arrives in M3)."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from framefound.auth.deps import CurrentUser, DbDep
from framefound.db.models import Asset

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

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(
            query.order_by(Asset.first_indexed_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
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
