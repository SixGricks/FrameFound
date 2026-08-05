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
from collections import OrderedDict
from pathlib import Path
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select

from framefound.ai.embeddings import EmbeddingUnavailable
from framefound.auth.deps import CurrentUser, DbDep, SettingsDep
from framefound.config import Settings
from framefound.db.models import Asset, AssetEdit, AssetInpaint, Library, Listing, ListingItem
from framefound.media import develop as develop_lib
from framefound.scanner.paths import PathValidationError, safe_join

log = structlog.get_logger()

router = APIRouter(prefix="/develop", tags=["develop"])

PREVIEW_EDGE = 1600


class SkyIn(BaseModel):
    """A sky replacement: which sky from the operator's library, and how it
    sits. Name characters are restricted so a recipe can never traverse."""

    name: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9][A-Za-z0-9._ -]*$")
    feather: float = Field(default=0.02, ge=0.0, le=0.2)
    shift: float = Field(default=0.0, ge=-0.5, le=0.5)
    relight: float = Field(default=0.4, ge=0.0, le=1.0)


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
    rotate: float = Field(default=0.0, ge=-5.0, le=5.0)
    keystone: float = Field(default=0.0, ge=-1.0, le=1.0)
    window_pull: float = Field(default=0.0, ge=0.0, le=1.0)
    auto: bool = False
    sky: SkyIn | None = None


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


# ---------------------------------------------------------------- skies
#
# The sky library is operator-supplied photographs in the data directory.
# FrameFound ships none and fetches none: licensing stays clean and the
# skies match the light the properties are actually shot in. Declared
# before the /{asset_id} routes so the literal path wins the match.

MAX_SKY_BYTES = 25 * 1024 * 1024


def _skies_dir(settings: Settings) -> Path:
    path = settings.data_dir / "skies"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sky_path(settings: Settings, name: str) -> Path:
    # The pattern in SkyIn already refuses separators; belt and braces here.
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="Not a sky name")
    return _skies_dir(settings) / name


class SkyOut(BaseModel):
    name: str
    size_bytes: int


@router.get("/skies", response_model=list[SkyOut])
async def list_skies(_user: CurrentUser, settings: SettingsDep) -> list[SkyOut]:
    skies = [
        SkyOut(name=p.name, size_bytes=p.stat().st_size)
        for p in sorted(_skies_dir(settings).iterdir())
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
    ]
    return skies


@router.put("/skies/{name}", response_model=SkyOut, status_code=201)
async def upload_sky(  # type: ignore[no-untyped-def]
    name: str, request: Request, _user: CurrentUser, settings: SettingsDep
):
    """Add a sky photograph. Raw image bytes in the body — no multipart.

    The bytes must decode as an image before anything lands on disk; the sky
    library is content the compositor will open unattended later, so garbage
    is refused at the door rather than discovered mid-export.
    """
    data = await request.body()
    if not data or len(data) > MAX_SKY_BYTES:
        raise HTTPException(status_code=400, detail="Empty or oversized upload")

    def check() -> None:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img.verify()

    try:
        await asyncio.to_thread(check)
    except Exception as err:
        raise HTTPException(status_code=400, detail="Not a readable image") from err

    path = _sky_path(settings, name)
    path.write_bytes(data)
    log.info("develop.sky_added", name=name, kilobytes=len(data) // 1024)
    return SkyOut(name=name, size_bytes=len(data))


@router.get("/skies/{name}/image")
async def sky_image(  # type: ignore[no-untyped-def]
    name: str, _user: CurrentUser, settings: SettingsDep
):
    path = _sky_path(settings, name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="No such sky")
    from fastapi.responses import FileResponse

    return FileResponse(path)


@router.delete("/skies/{name}", status_code=204)
async def delete_sky(name: str, _user: CurrentUser, settings: SettingsDep) -> None:
    _sky_path(settings, name).unlink(missing_ok=True)
    log.info("develop.sky_removed", name=name)


# ------------------------------------------------------- segmentation cache
#
# The mask depends only on the original pixels, and a slider drag re-renders
# the preview many times. A tiny per-process LRU means segmentation runs
# once per photograph per editing session, not once per slider notch.

_mask_cache: "OrderedDict[tuple[uuid.UUID, int, int, int], Any]" = OrderedDict()
_MASK_CACHE_MAX = 8


def _cached_mask(asset_id: uuid.UUID, image: Any, base_version: int = 0) -> Any:
    key = (asset_id, base_version, image.size[0], image.size[1])
    if key in _mask_cache:
        _mask_cache.move_to_end(key)
        return _mask_cache[key]
    from framefound.ai import skyseg

    mask = skyseg.sky_mask(image)
    _mask_cache[key] = mask
    while len(_mask_cache) > _MASK_CACHE_MAX:
        _mask_cache.popitem(last=False)
    return mask


class SkyInfo(BaseModel):
    fraction: float
    usable: bool
    available: bool  # False when the segmentation runtime is not installed


@router.get("/{asset_id}/sky-info", response_model=SkyInfo)
async def sky_info(
    asset_id: uuid.UUID, _user: CurrentUser, db: DbDep, settings: SettingsDep
) -> SkyInfo:
    """How much sky this photograph has — the editor greys the sky picker
    out for interiors instead of letting a replacement silently no-op."""
    from framefound.media.sky import MIN_SKY_FRACTION

    asset = await _get_image_asset(db, asset_id)
    path, is_inpaint, base_version = await _base_image_path(db, asset, settings)

    def measure() -> float:
        import numpy as np
        from PIL import Image, ImageOps

        with Image.open(path) as img:
            image = img if is_inpaint else (ImageOps.exif_transpose(img) or img)
            image = image.convert("RGB")
            image.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.Resampling.LANCZOS)
            return float(np.mean(_cached_mask(asset_id, image, base_version)))

    try:
        fraction = await asyncio.to_thread(measure)
    except EmbeddingUnavailable:
        return SkyInfo(fraction=0.0, usable=False, available=False)
    return SkyInfo(fraction=fraction, usable=fraction >= MIN_SKY_FRACTION, available=True)


@router.get("/{asset_id}", response_model=EditState)
async def get_edit(asset_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> EditState:
    await _get_image_asset(db, asset_id)
    recipe, version = await current_recipe(db, asset_id)
    return EditState(asset_id=asset_id, recipe=recipe, version=version, edited=version > 0)


# --------------------------------------------------------------- inpaint
#
# Object removal is the one edit that is a queued task rather than a live
# render: LaMa costs tens of seconds per region on these CPUs, and the
# editor says so. The result becomes the photograph's new BASE — every
# recipe (sky, geometry, colour) then renders on top of it, and undo
# deletes the newest version's file.


class InpaintRequest(BaseModel):
    # The brush mask as a base64 PNG (any size; scaled to the original).
    # White = remove. Kept small by the UI (preview resolution).
    mask_png: str = Field(min_length=8, max_length=2_000_000)


class InpaintOut(BaseModel):
    version: int
    status: str
    error: str | None
    created_at: Any


class InpaintState(BaseModel):
    asset_id: uuid.UUID
    versions: list[InpaintOut]
    busy: bool  # a round is queued or running


async def latest_ready_inpaint(db: DbDep, asset_id: uuid.UUID) -> AssetInpaint | None:
    return (
        await db.execute(
            select(AssetInpaint)
            .where(AssetInpaint.asset_id == asset_id, AssetInpaint.status == "ready")
            .order_by(AssetInpaint.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _base_image_path(db: DbDep, asset: Asset, settings: Settings) -> tuple[Path, bool, int]:
    """The image every render starts from: the newest ready inpaint result,
    or the original. The bool says whether it is an inpaint file (already
    orientation-applied and sRGB, so the caller must not transpose again);
    the int is the inpaint version, for cache keys — the base's pixels are
    a function of (asset, version), and so is anything derived from them."""
    inpaint = await latest_ready_inpaint(db, asset.id)
    if inpaint is not None and inpaint.relative_path:
        path = settings.data_dir / inpaint.relative_path
        if path.is_file():
            return path, True, inpaint.version
        log.warning("develop.inpaint_file_missing", asset_id=str(asset.id))
    return await _original_path(db, asset), False, 0


@router.get("/{asset_id}/inpaint", response_model=InpaintState)
async def inpaint_state(asset_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> InpaintState:
    await _get_image_asset(db, asset_id)
    rows = (
        (
            await db.execute(
                select(AssetInpaint)
                .where(AssetInpaint.asset_id == asset_id)
                .order_by(AssetInpaint.version)
            )
        )
        .scalars()
        .all()
    )
    return InpaintState(
        asset_id=asset_id,
        versions=[
            InpaintOut(version=r.version, status=r.status, error=r.error, created_at=r.created_at)
            for r in rows
        ],
        busy=any(r.status in ("queued", "running") for r in rows),
    )


@router.post("/{asset_id}/inpaint", response_model=InpaintState, status_code=202)
async def request_inpaint(
    asset_id: uuid.UUID, body: InpaintRequest, _user: CurrentUser, db: DbDep
) -> InpaintState:
    """Queue one removal. One at a time per photograph — each round runs on
    the previous result, so parallel rounds would race for the same base."""
    import base64
    import binascii

    await _get_image_asset(db, asset_id)
    pending = (
        await db.execute(
            select(func.count())
            .select_from(AssetInpaint)
            .where(
                AssetInpaint.asset_id == asset_id,
                AssetInpaint.status.in_(("queued", "running")),
            )
        )
    ).scalar_one()
    if pending:
        raise HTTPException(status_code=409, detail="A removal is already running")

    try:
        raw = base64.b64decode(body.mask_png, validate=True)
    except (binascii.Error, ValueError) as err:
        raise HTTPException(status_code=400, detail="Mask is not valid base64") from err

    def decode() -> tuple[int, int, float]:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as img:
            gray = img.convert("L")
            import numpy as np

            arr = np.asarray(gray, dtype=np.float32) / 255.0
            return gray.size[0], gray.size[1], float(arr.mean())

    try:
        mask_w, mask_h, coverage = await asyncio.to_thread(decode)
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(status_code=400, detail="Mask is not a readable image") from err
    if coverage <= 0.0:
        raise HTTPException(status_code=400, detail="The mask selects nothing")
    if coverage > 0.5:
        raise HTTPException(
            status_code=400,
            detail="That selects more than half the photograph — this removes objects, "
            "it does not repaint scenes",
        )

    version = (
        await db.execute(
            select(func.coalesce(func.max(AssetInpaint.version), 0)).where(
                AssetInpaint.asset_id == asset_id
            )
        )
    ).scalar_one() + 1
    row = AssetInpaint(
        asset_id=asset_id,
        version=version,
        mask_meta={
            "png_base64": body.mask_png,
            "mask_size": [mask_w, mask_h],
            "coverage": round(coverage, 4),
        },
        status="queued",
    )
    db.add(row)
    await db.commit()

    try:
        from framefound.processing.tasks import inpaint_asset

        inpaint_asset.delay(str(row.id))
    except Exception:
        row.status = "failed"
        row.error = "The processing queue is unavailable"
        await db.commit()
        raise HTTPException(status_code=503, detail="The processing queue is unavailable") from None
    log.info("develop.inpaint_queued", asset_id=str(asset_id), version=version, coverage=coverage)
    return await inpaint_state(asset_id, _user, db)


@router.delete("/{asset_id}/inpaint/{version}", response_model=InpaintState)
async def undo_inpaint(
    asset_id: uuid.UUID, version: int, _user: CurrentUser, db: DbDep, settings: SettingsDep
) -> InpaintState:
    """Undo one removal. Only the newest version can go — the chain renders
    each round on the previous result, so pulling a middle version out
    would misdescribe every file after it."""
    await _get_image_asset(db, asset_id)
    newest = (
        await db.execute(
            select(AssetInpaint)
            .where(AssetInpaint.asset_id == asset_id)
            .order_by(AssetInpaint.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if newest is None or newest.version != version:
        raise HTTPException(status_code=409, detail="Only the newest removal can be undone")
    if newest.relative_path:
        (settings.data_dir / newest.relative_path).unlink(missing_ok=True)
    await db.delete(newest)
    await db.commit()
    # The base image changed; cached segmentation masks are now stale.
    _mask_cache.clear()
    log.info("develop.inpaint_undone", asset_id=str(asset_id), version=version)
    return await inpaint_state(asset_id, _user, db)


def sky_loader(settings: Settings) -> Any:
    """A load-by-name callable for the render pipeline. Returns None for a
    missing file — a deleted sky degrades the render, never fails it."""

    def load(name: str) -> Any:
        from PIL import Image

        path = _sky_path(settings, name)
        if not path.is_file():
            log.warning("develop.sky_missing", name=name)
            return None
        return Image.open(path)

    return load


@router.post("/{asset_id}/preview")
async def preview(  # type: ignore[no-untyped-def]
    asset_id: uuid.UUID, body: RecipeIn, _user: CurrentUser, db: DbDep, settings: SettingsDep
):
    """Render the recipe onto a preview-sized copy of the original."""
    asset = await _get_image_asset(db, asset_id)
    path, is_inpaint, base_version = await _base_image_path(db, asset, settings)

    def mask_for(image: Any) -> Any:
        try:
            return _cached_mask(asset_id, image, base_version)
        except EmbeddingUnavailable:
            return None

    def render() -> bytes:
        from PIL import Image, ImageOps

        with Image.open(path) as img:
            image = img if is_inpaint else (ImageOps.exif_transpose(img) or img)
            image = image.convert("RGB")
            image.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.Resampling.LANCZOS)
            image = develop_lib.render(
                image, body.model_dump(), load_sky=sky_loader(settings), mask_for=mask_for
            )
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
