"""Develop: non-destructive colour editing for photographs.

The recipe is the artefact. Pixels are rendered from it on demand — a
preview now, the export later — and the original file is never written,
which is not a policy choice here so much as a physical one: the media
mounts are read-only.

Preview renders at PREVIEW_EDGE from the original, downscaled *before* the
maths because every adjustment is per-pixel and scale-free: the small render
and the full-size export go through one engine and look the same, which is
the property that makes a preview trustworthy.
"""

import asyncio
import io
import uuid
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select

from framefound.auth.deps import CurrentUser, DbDep
from framefound.db.models import Asset, AssetEdit, Library, Listing, ListingItem
from framefound.media import develop as develop_lib
from framefound.scanner.paths import PathValidationError, safe_join

log = structlog.get_logger()

router = APIRouter(prefix="/develop", tags=["develop"])

PREVIEW_EDGE = 1600


class RecipeIn(BaseModel):
    """Slider values. Bounds mirror develop.RECIPE_FIELDS; anything outside
    them is clamped rather than refused — a slider cannot be *wrong*, only
    at its end stop."""

    exposure: float = Field(default=0.0, ge=-2.0, le=2.0)
    contrast: float = Field(default=0.0, ge=-1.0, le=1.0)
    temperature: float = Field(default=0.0, ge=-1.0, le=1.0)
    tint: float = Field(default=0.0, ge=-1.0, le=1.0)
    shadows: float = Field(default=0.0, ge=-1.0, le=1.0)
    highlights: float = Field(default=0.0, ge=-1.0, le=1.0)
    vibrance: float = Field(default=0.0, ge=-1.0, le=1.0)
    saturation: float = Field(default=0.0, ge=-1.0, le=1.0)
    auto: bool = False


class EditState(BaseModel):
    asset_id: uuid.UUID
    recipe: dict[str, Any]
    version: int  # 0 = no edits, the original
    edited: bool


async def current_recipe(db: DbDep, asset_id: uuid.UUID) -> tuple[dict[str, Any], int]:
    """The latest saved recipe for an asset, or ({}, 0)."""
    row = (
        await db.execute(
            select(AssetEdit)
            .where(AssetEdit.asset_id == asset_id)
            .order_by(AssetEdit.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return {}, 0
    return develop_lib.clean_recipe(row.recipe), row.version


async def _original_path(db: DbDep, asset: Asset) -> Path:
    library = await db.get(Library, asset.library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library no longer exists")
    try:
        path = safe_join(Path(library.root_path), asset.relative_path)
    except PathValidationError as err:
        raise HTTPException(status_code=400, detail="Path escapes the library root") from err
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Original file is not reachable")
    return path


async def _get_image_asset(db: DbDep, asset_id: uuid.UUID) -> Asset:
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="No such asset")
    if asset.media_type != "image":
        raise HTTPException(status_code=400, detail="Only photographs can be developed")
    return asset


@router.get("/{asset_id}", response_model=EditState)
async def get_edit(asset_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> EditState:
    await _get_image_asset(db, asset_id)
    recipe, version = await current_recipe(db, asset_id)
    return EditState(asset_id=asset_id, recipe=recipe, version=version, edited=version > 0)


@router.post("/{asset_id}/preview")
async def preview(  # type: ignore[no-untyped-def]
    asset_id: uuid.UUID, body: RecipeIn, _user: CurrentUser, db: DbDep
):
    """Render the recipe onto a preview-sized copy of the original."""
    asset = await _get_image_asset(db, asset_id)
    path = await _original_path(db, asset)

    def render() -> bytes:
        from PIL import Image, ImageOps

        with Image.open(path) as img:
            image = ImageOps.exif_transpose(img) or img
            image = image.convert("RGB")
            image.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.Resampling.LANCZOS)
            image = develop_lib.apply_recipe(image, body.model_dump())
            out = io.BytesIO()
            image.save(out, "JPEG", quality=80)
            return out.getvalue()

    data = await asyncio.to_thread(render)
    # Never cached: the whole point is that the recipe just changed.
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.put("/{asset_id}", response_model=EditState)
async def save_edit(
    asset_id: uuid.UUID, body: RecipeIn, _user: CurrentUser, db: DbDep
) -> EditState:
    """Append the recipe as a new version. Saving the identity recipe on an
    unedited photograph is a no-op rather than a version of nothing."""
    await _get_image_asset(db, asset_id)
    cleaned = develop_lib.clean_recipe(body.model_dump())
    current, version = await current_recipe(db, asset_id)
    if cleaned == current:
        return EditState(asset_id=asset_id, recipe=current, version=version, edited=version > 0)
    db.add(AssetEdit(asset_id=asset_id, version=version + 1, recipe=cleaned))
    await db.commit()
    log.info("develop.saved", asset_id=str(asset_id), version=version + 1)
    return EditState(asset_id=asset_id, recipe=cleaned, version=version + 1, edited=True)


@router.delete("/{asset_id}", response_model=EditState)
async def clear_edit(asset_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> EditState:
    """Back to the original. Deleting recipes is safe precisely because they
    were never pixels."""
    await _get_image_asset(db, asset_id)
    await db.execute(sql_delete(AssetEdit).where(AssetEdit.asset_id == asset_id))
    await db.commit()
    log.info("develop.cleared", asset_id=str(asset_id))
    return EditState(asset_id=asset_id, recipe={}, version=0, edited=False)


class BatchApplyResponse(BaseModel):
    applied: int


@router.post("/listing/{listing_id}/apply", response_model=BatchApplyResponse)
async def apply_to_listing(
    listing_id: uuid.UUID, body: RecipeIn, _user: CurrentUser, db: DbDep
) -> BatchApplyResponse:
    """One recipe across every photograph in a listing.

    The consistency move: a property is shot in one light, so one correction
    usually fits the whole set. Each image gets its own new version row, so
    a single photo can still be re-edited individually afterwards.
    """
    if await db.get(Listing, listing_id) is None:
        raise HTTPException(status_code=404, detail="No such listing")
    cleaned = develop_lib.clean_recipe(body.model_dump())
    asset_ids = (
        (
            await db.execute(
                select(ListingItem.asset_id)
                .join(Asset, Asset.id == ListingItem.asset_id)
                .where(ListingItem.listing_id == listing_id, Asset.media_type == "image")
            )
        )
        .scalars()
        .all()
    )
    version_rows = (
        await db.execute(
            select(AssetEdit.asset_id, func.max(AssetEdit.version))
            .where(AssetEdit.asset_id.in_(asset_ids))
            .group_by(AssetEdit.asset_id)
        )
    ).all()
    versions: dict[uuid.UUID, int] = {aid: v for aid, v in version_rows}
    applied = 0
    for asset_id in asset_ids:
        db.add(AssetEdit(asset_id=asset_id, version=versions.get(asset_id, 0) + 1, recipe=cleaned))
        applied += 1
    await db.commit()
    log.info("develop.batch_applied", listing_id=str(listing_id), applied=applied)
    return BatchApplyResponse(applied=applied)
