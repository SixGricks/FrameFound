"""Listings: order and name a property shoot for upload.

The deliverable is a zip whose filenames sort into gallery order —
`01-front-exterior-brick-colonial-130-davis-rd-auction.jpg, 02-kitchen-… ` —
because MLS galleries display in upload order, portals and search engines
read the words, and renaming by hand is the chore this feature deletes. The
zip carries the photo index and contact sheets alongside (media/photo_index.py).

Room labels come zero-shot from embeddings the catalogue already stores, and
they are suggestions until confirmed or overridden. Ordering is the
operator's: "arrange" applies the canonical walk-through as a starting point,
then explicit reorder wins and nothing shuffles it afterwards.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Any, Literal

import structlog
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select

from framefound.ai import rooms as rooms_lib
from framefound.ai.embeddings import EmbeddingUnavailable
from framefound.auth.deps import CurrentUser, DbDep, SettingsDep, require_admin
from framefound.db.models import Asset, AssetEdit, AuditLog, Frame, Listing, ListingItem
from framefound.media import photo_index
from framefound.media.export_state import listing_fingerprint

log = structlog.get_logger()

router = APIRouter(prefix="/listings", tags=["listings"])

MAX_ITEMS = 500


class RoomOut(BaseModel):
    key: str
    label: str


class ItemOut(BaseModel):
    asset_id: uuid.UUID
    filename: str
    media_type: str
    position: int
    room: str
    room_label: str
    room_source: str
    room_score: float | None
    # A develop recipe exists for this photograph; the export will apply it.
    edited: bool = False
    # When its newest recipe was saved. Auto-edit progress compares these
    # against a snapshot taken at the start of the run — "edited" alone is
    # already true for every photo when a listing is edited a second time.
    edited_at: datetime | None = None
    # What it shows (the photo index) and the words in its file name.
    # naming_source: "" unnamed, "suggested" by the AI, "confirmed" by you.
    caption: str = ""
    slug: str = ""
    naming_source: str = ""
    # When the AI last named it — naming-run progress, as edited_at is for edits.
    named_at: datetime | None = None
    # The name it will have in an SEO-named export; None for videos, which
    # stay out of the photo zip.
    export_name: str | None = None


class ListingOut(BaseModel):
    id: uuid.UUID
    name: str
    item_count: int
    export_status: str
    export_error: str | None
    cover_asset_id: uuid.UUID | None


class ListingDetail(ListingOut):
    items: list[ItemOut]
    # True when classification was skipped because the CLIP runtime is not
    # installed — the UI says so instead of showing silent blanks.
    classified: bool
    # The zip exists but the listing has changed since it was made (order,
    # rooms, edits, removals). The download refuses it; the UI offers a
    # re-export instead of shipping the old gallery to MLS.
    export_stale: bool = False
    # The file-name suffix as typed ("" = not set), and what is used instead
    # when it is empty — the UI shows the latter as the placeholder.
    file_suffix: str = ""
    suggested_suffix: str = ""
    notes: str = ""


class CreateListingRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_ITEMS)


class AddItemsRequest(BaseModel):
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_ITEMS)


class SetRoomRequest(BaseModel):
    room: str = Field(max_length=40)


class ReorderRequest(BaseModel):
    asset_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_ITEMS)


class ExportRequest(BaseModel):
    max_edge: int = Field(default=3840, ge=1024, le=8192)
    quality: int = Field(default=85, ge=60, le=95)
    # "seo": 01-kitchen-island-pantry-130-davis-rd-auction.jpg.
    # "simple": 01_kitchen.jpg, for portals that rename on upload anyway.
    naming: Literal["seo", "simple"] = "seo"
    # Photo Index (Markdown + CSV) and contact sheets, in _index/.
    include_index: bool = True


class SetNamingRequest(BaseModel):
    caption: str = Field(default="", max_length=300)
    slug: str = Field(default="", max_length=200)


class UpdateListingRequest(BaseModel):
    """Only the fields present change."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    file_suffix: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)


def _suffix_for(listing: Listing) -> str:
    return listing.file_suffix or photo_index.default_suffix(listing.name)


def _item_out(
    item: ListingItem,
    asset: Asset,
    edited_at: datetime | None = None,
    export_name: str | None = None,
) -> ItemOut:
    return ItemOut(
        asset_id=item.asset_id,
        filename=asset.filename,
        media_type=asset.media_type,
        position=item.position,
        room=item.room,
        room_label=rooms_lib.ROOM_LABELS.get(item.room, ""),
        room_source=item.room_source,
        room_score=item.room_score,
        edited=edited_at is not None,
        edited_at=edited_at,
        caption=item.caption,
        slug=item.slug,
        naming_source=item.naming_source,
        named_at=item.named_at,
        export_name=export_name,
    )


async def _classify_items(db: DbDep, listing_id: uuid.UUID, only_unconfirmed: bool = True) -> bool:
    """Suggest a room for each item from its stored frame embedding.

    Returns False when the CLIP runtime is unavailable (tests, a minimal
    install): items stay unlabelled and the operator labels by hand. The
    listing must still work without AI — the AI is a head start, not a
    dependency.
    """
    try:
        vectors = await asyncio.to_thread(rooms_lib.room_vectors)
    except EmbeddingUnavailable:
        return False

    stmt = select(ListingItem).where(ListingItem.listing_id == listing_id)
    if only_unconfirmed:
        stmt = stmt.where(ListingItem.room_source != "confirmed")
    items = (await db.execute(stmt)).scalars().all()
    if not items:
        return True

    rows = (
        await db.execute(
            select(Frame.asset_id, Frame.embedding)
            .where(
                Frame.asset_id.in_([i.asset_id for i in items]),
                Frame.embedding.is_not(None),
            )
            # Images have one frame; last-write-wins over a descending sort
            # leaves each video represented by its earliest sampled frame.
            .order_by(Frame.asset_id, Frame.ts_ms.desc())
        )
    ).all()
    embeddings: dict[uuid.UUID, list[float] | None] = {aid: emb for aid, emb in rows}
    for item in items:
        embedding = embeddings.get(item.asset_id)
        if not embedding:
            continue
        room, score = rooms_lib.classify(embedding, vectors)
        item.room = room
        item.room_source = "suggested"
        item.room_score = score
    await db.commit()
    return True


async def _arrange(db: DbDep, listing_id: uuid.UUID) -> None:
    """Apply the canonical walk-through order as positions."""
    rows = (
        await db.execute(
            select(ListingItem, Asset.filename)
            .join(Asset, Asset.id == ListingItem.asset_id)
            .where(ListingItem.listing_id == listing_id)
        )
    ).all()
    ordered = sorted(
        rows,
        key=lambda row: (
            rooms_lib.canonical_sort_key(row[0].room, row[0].room_score),
            row[1],  # filename keeps unlabelled runs stable rather than arbitrary
        ),
    )
    for position, (item, _fname) in enumerate(ordered):
        item.position = position
    await db.commit()


async def _detail(db: DbDep, listing: Listing, classified: bool) -> ListingDetail:
    rows = (
        await db.execute(
            select(ListingItem, Asset)
            .join(Asset, Asset.id == ListingItem.asset_id)
            .where(ListingItem.listing_id == listing.id)
            .order_by(ListingItem.position, ListingItem.created_at)
        )
    ).all()
    edited_at: dict[uuid.UUID, datetime] = {
        asset_id: when
        for asset_id, when in (
            await db.execute(
                select(AssetEdit.asset_id, func.max(AssetEdit.created_at))
                .where(AssetEdit.asset_id.in_([item.asset_id for item, _ in rows]))
                .group_by(AssetEdit.asset_id)
            )
        ).all()
    }
    # The names an SEO export would give, numbered over images only, as the
    # export numbers them — so the page previews exactly what the zip holds
    # (short of a file that turns out to be unreadable, which closes ranks).
    suffix = _suffix_for(listing)
    total = sum(1 for _item, asset in rows if asset.media_type == "image")
    names: dict[uuid.UUID, str] = {}
    for item, asset in rows:
        if asset.media_type != "image":
            continue
        room = item.room if item.room in rooms_lib.ROOM_LABELS else ""
        names[item.asset_id] = photo_index.export_filename(
            len(names) + 1, total, slug=item.slug, room=room, suffix=suffix
        )
    items = [
        _item_out(item, asset, edited_at.get(item.asset_id), names.get(item.asset_id))
        for item, asset in rows
    ]
    return ListingDetail(
        id=listing.id,
        name=listing.name,
        item_count=len(items),
        export_status=listing.export_status,
        export_error=listing.export_error,
        cover_asset_id=items[0].asset_id if items else None,
        items=items,
        classified=classified,
        export_stale=await _export_stale(db, listing),
        file_suffix=listing.file_suffix,
        suggested_suffix=photo_index.default_suffix(listing.name),
        notes=listing.notes,
    )


async def _export_stale(db: DbDep, listing: Listing) -> bool:
    """A ready zip that no longer matches the listing. An export from before
    fingerprints existed counts as stale — "cannot tell" must not ship."""
    if listing.export_status != "ready":
        return False
    if listing.export_fingerprint is None:
        return True
    return listing.export_fingerprint != await listing_fingerprint(db, listing.id)


@router.get("/rooms", response_model=list[RoomOut])
async def list_rooms(_user: CurrentUser) -> list[RoomOut]:
    """The taxonomy, in canonical listing order — the UI's label dropdown."""
    return [RoomOut(key=room.key, label=room.label) for room in rooms_lib.ROOMS]


# --------------------------------------------------------------- folders
#
# A shoot is a folder. The operator dumps a card to the NAS as
# "00-00 5096 Old Philadelphia Pike Kinzers/", the scanner indexes it, and
# building the listing should start from that folder — not from a search
# that happens to hit some of its filenames.


class FolderOut(BaseModel):
    library_id: uuid.UUID
    library_name: str
    # Folder path relative to the library root; "" is the root itself.
    path: str
    image_count: int


class FolderAssetOut(BaseModel):
    asset_id: uuid.UUID
    filename: str
    media_type: str


def _dirname(relative_path: str) -> str:
    head, _, _tail = relative_path.rpartition("/")
    return head


@router.get("/folders", response_model=list[FolderOut])
async def search_folders(
    _user: CurrentUser,
    db: DbDep,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=20, ge=1, le=50),
) -> list[FolderOut]:
    """Folders whose path matches, with how many photos each holds.

    Grouping happens here rather than in SQL because "the folder" is a
    substring operation on relative_path, and the portable way to do that
    identically on PostgreSQL and the SQLite suite is to not ask SQL at all —
    at this catalogue's size the whole image-path column is a cheap scan.
    """
    from framefound.db.models import Library as LibraryModel

    rows = (
        await db.execute(
            select(Asset.library_id, Asset.relative_path)
            .where(Asset.media_type == "image", Asset.relative_path.ilike(f"%{q}%"))
            .limit(20_000)
        )
    ).all()
    counts: dict[tuple[uuid.UUID, str], int] = {}
    lowered = q.lower()
    for library_id, relative_path in rows:
        folder = _dirname(relative_path)
        # Match on the folder path, not the filename: a query of "5096"
        # should find the shoot folder even if no filename contains it.
        if lowered not in folder.lower():
            continue
        counts[(library_id, folder)] = counts.get((library_id, folder), 0) + 1

    names = {
        lib.id: lib.name
        for lib in (
            (
                await db.execute(
                    select(LibraryModel).where(LibraryModel.id.in_({k[0] for k in counts}))
                )
            )
            .scalars()
            .all()
        )
    }
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][1]))[:limit]
    return [
        FolderOut(
            library_id=library_id,
            library_name=names.get(library_id, ""),
            path=folder,
            image_count=count,
        )
        for (library_id, folder), count in ranked
    ]


@router.get("/folders/assets", response_model=list[FolderAssetOut])
async def folder_assets(
    _user: CurrentUser,
    db: DbDep,
    library_id: uuid.UUID,
    path: str = Query(default="", max_length=1024),
    limit: int = Query(default=500, ge=1, le=1000),
) -> list[FolderAssetOut]:
    """Every image directly inside one folder — no recursion into
    subfolders, because "MLS/" and "RAW/" siblings are different deliveries
    of the same shoot and mixing them would double every photograph."""
    stmt = select(Asset).where(Asset.library_id == library_id, Asset.media_type == "image")
    rows = (await db.execute(stmt.limit(20_000))).scalars().all()
    picked = [a for a in rows if _dirname(a.relative_path) == path]
    picked.sort(key=lambda a: a.filename)
    return [
        FolderAssetOut(asset_id=a.id, filename=a.filename, media_type=a.media_type)
        for a in picked[:limit]
    ]


@router.post("", response_model=ListingDetail, status_code=201)
async def create_listing(
    body: CreateListingRequest, _user: CurrentUser, db: DbDep
) -> ListingDetail:
    listing = Listing(name=body.name.strip())
    db.add(listing)
    await db.flush()
    classified = True
    if body.asset_ids:
        classified = await _add_assets(db, listing, body.asset_ids)
        await _arrange(db, listing.id)
    else:
        await db.commit()
    log.info("listing.created", listing_id=str(listing.id), items=len(body.asset_ids))
    return await _detail(db, listing, classified)


async def _add_assets(db: DbDep, listing: Listing, asset_ids: list[uuid.UUID]) -> bool:
    """Add assets that exist and are not already in the listing."""
    existing = {
        row
        for row in (
            await db.execute(
                select(ListingItem.asset_id).where(ListingItem.listing_id == listing.id)
            )
        ).scalars()
    }
    valid = (await db.execute(select(Asset.id).where(Asset.id.in_(asset_ids)))).scalars().all()
    tail = (
        await db.execute(
            select(func.coalesce(func.max(ListingItem.position), -1)).where(
                ListingItem.listing_id == listing.id
            )
        )
    ).scalar_one()
    added = 0
    for asset_id in asset_ids:  # request order, deduped
        if asset_id in existing or asset_id not in valid:
            continue
        existing.add(asset_id)
        added += 1
        db.add(ListingItem(listing_id=listing.id, asset_id=asset_id, position=tail + added))
    await db.commit()
    return await _classify_items(db, listing.id)


@router.get("", response_model=list[ListingOut])
async def list_listings(_user: CurrentUser, db: DbDep) -> list[ListingOut]:
    listings = (
        (await db.execute(select(Listing).order_by(Listing.created_at.desc()))).scalars().all()
    )
    count_rows = (
        await db.execute(
            select(ListingItem.listing_id, func.count()).group_by(ListingItem.listing_id)
        )
    ).all()
    counts: dict[uuid.UUID, int] = {lid: n for lid, n in count_rows}
    # The cover is each listing's first photograph in gallery order. This was
    # min(asset_id) over position 0: SQLite accepts min() of a UUID, Postgres
    # has no such function — the Listings page failed in production while
    # every test passed — and a listing whose first item was removed had no
    # position 0 and so no cover at all.
    cover_rows = (
        await db.execute(
            select(ListingItem.listing_id, ListingItem.asset_id).order_by(
                ListingItem.listing_id, ListingItem.position, ListingItem.created_at
            )
        )
    ).all()
    covers: dict[uuid.UUID, uuid.UUID] = {}
    for listing_id, asset_id in cover_rows:
        covers.setdefault(listing_id, asset_id)
    return [
        ListingOut(
            id=listing.id,
            name=listing.name,
            item_count=counts.get(listing.id, 0),
            export_status=listing.export_status,
            export_error=listing.export_error,
            cover_asset_id=covers.get(listing.id),
        )
        for listing in listings
    ]


async def _get(db: DbDep, listing_id: uuid.UUID) -> Listing:
    listing = await db.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="No such listing")
    return listing


@router.get("/{listing_id}", response_model=ListingDetail)
async def get_listing(listing_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> ListingDetail:
    return await _detail(db, await _get(db, listing_id), classified=True)


@router.patch("/{listing_id}", response_model=ListingDetail)
async def update_listing(
    listing_id: uuid.UUID, body: UpdateListingRequest, _user: CurrentUser, db: DbDep
) -> ListingDetail:
    """Rename the listing, set the file-name suffix, or write the notes that
    head the photo index. The suffix is cleaned to what a file name can
    carry; clearing it falls back to the one derived from the name."""
    listing = await _get(db, listing_id)
    if body.name is not None:
        listing.name = body.name.strip() or listing.name
    if body.file_suffix is not None:
        listing.file_suffix = photo_index.clean_suffix(body.file_suffix)
    if body.notes is not None:
        listing.notes = body.notes.strip()
    await db.commit()
    return await _detail(db, listing, classified=True)


@router.post("/{listing_id}/items", response_model=ListingDetail)
async def add_items(
    listing_id: uuid.UUID, body: AddItemsRequest, _user: CurrentUser, db: DbDep
) -> ListingDetail:
    listing = await _get(db, listing_id)
    classified = await _add_assets(db, listing, body.asset_ids)
    return await _detail(db, listing, classified)


@router.delete("/{listing_id}/items/{asset_id}", response_model=ListingDetail)
async def remove_item(
    listing_id: uuid.UUID, asset_id: uuid.UUID, _user: CurrentUser, db: DbDep
) -> ListingDetail:
    listing = await _get(db, listing_id)
    await db.execute(
        sql_delete(ListingItem).where(
            ListingItem.listing_id == listing_id, ListingItem.asset_id == asset_id
        )
    )
    await db.commit()
    return await _detail(db, listing, classified=True)


@router.put("/{listing_id}/items/{asset_id}/room", response_model=ListingDetail)
async def set_room(
    listing_id: uuid.UUID,
    asset_id: uuid.UUID,
    body: SetRoomRequest,
    _user: CurrentUser,
    db: DbDep,
) -> ListingDetail:
    """Override a label. The operator saying so is what 'confirmed' means."""
    if body.room and body.room not in rooms_lib.ROOM_ORDER:
        raise HTTPException(status_code=400, detail="Not a known room")
    listing = await _get(db, listing_id)
    item = (
        await db.execute(
            select(ListingItem).where(
                ListingItem.listing_id == listing_id, ListingItem.asset_id == asset_id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Not in this listing")
    item.room = body.room
    item.room_source = "confirmed"
    item.room_score = None
    await db.commit()
    return await _detail(db, listing, classified=True)


@router.put("/{listing_id}/items/{asset_id}/naming", response_model=ListingDetail)
async def set_naming(
    listing_id: uuid.UUID,
    asset_id: uuid.UUID,
    body: SetNamingRequest,
    _user: CurrentUser,
    db: DbDep,
) -> ListingDetail:
    """Set what a photograph shows and the words in its file name. Typing
    them confirms them — a later AI run leaves them alone. Clearing both
    hands the photograph back to the AI."""
    listing = await _get(db, listing_id)
    item = (
        await db.execute(
            select(ListingItem).where(
                ListingItem.listing_id == listing_id, ListingItem.asset_id == asset_id
            )
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Not in this listing")
    item.caption = " ".join(body.caption.split())
    item.slug = photo_index.clean_slug(body.slug)
    item.naming_source = "confirmed" if (item.caption or item.slug) else ""
    await db.commit()
    return await _detail(db, listing, classified=True)


@router.post("/{listing_id}/classify", response_model=ListingDetail)
async def reclassify(listing_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> ListingDetail:
    """Re-suggest labels for everything not operator-confirmed."""
    listing = await _get(db, listing_id)
    classified = await _classify_items(db, listing_id)
    return await _detail(db, listing, classified)


@router.post("/{listing_id}/arrange", response_model=ListingDetail)
async def arrange(listing_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> ListingDetail:
    """Reset positions to the canonical walk-through order."""
    listing = await _get(db, listing_id)
    await _arrange(db, listing_id)
    return await _detail(db, listing, classified=True)


@router.post("/{listing_id}/reorder", response_model=ListingDetail)
async def reorder(
    listing_id: uuid.UUID, body: ReorderRequest, _user: CurrentUser, db: DbDep
) -> ListingDetail:
    """Set the exact order. Ids omitted from the request keep their relative
    order after the listed ones — a partial drag must not scramble the rest."""
    listing = await _get(db, listing_id)
    items = (
        (
            await db.execute(
                select(ListingItem)
                .where(ListingItem.listing_id == listing_id)
                .order_by(ListingItem.position)
            )
        )
        .scalars()
        .all()
    )
    explicit = {asset_id: index for index, asset_id in enumerate(body.asset_ids)}
    tail = len(explicit)
    for item in items:
        if item.asset_id not in explicit:
            explicit[item.asset_id] = tail
            tail += 1
    for item in items:
        item.position = explicit[item.asset_id]
    await db.commit()
    return await _detail(db, listing, classified=True)


@router.post("/{listing_id}/export", response_model=ListingDetail, status_code=202)
async def export_listing(
    listing_id: uuid.UUID, body: ExportRequest, _user: CurrentUser, db: DbDep
) -> ListingDetail:
    listing = await _get(db, listing_id)
    images = (
        await db.execute(
            select(func.count())
            .select_from(ListingItem)
            .join(Asset, Asset.id == ListingItem.asset_id)
            .where(ListingItem.listing_id == listing_id, Asset.media_type == "image")
        )
    ).scalar_one()
    if not images:
        raise HTTPException(status_code=400, detail="No images to export")
    if listing.export_status in ("queued", "exporting"):
        raise HTTPException(status_code=409, detail="An export is already running")

    listing.export_status = "queued"
    listing.export_error = None
    await db.commit()

    try:
        from framefound.processing.tasks import export_listing_zip

        export_listing_zip.delay(
            str(listing_id), body.max_edge, body.quality, body.naming, body.include_index
        )
    except Exception:
        # Same contract as slideshow rendering: a dead broker is a plain
        # answer, not a listing stuck saying "queued" for ever.
        listing.export_status = "failed"
        listing.export_error = "The processing queue is unavailable"
        await db.commit()
        raise HTTPException(status_code=503, detail="The processing queue is unavailable") from None
    log.info(
        "listing.export_queued",
        listing_id=str(listing_id),
        images=images,
        max_edge=body.max_edge,
        naming=body.naming,
    )
    return await _detail(db, listing, classified=True)


class ProcessRequest(BaseModel):
    # A sky from the library to composite onto photographs that have sky, or
    # None to leave every sky as shot. The operator chooses; nothing decides
    # for them.
    sky_name: str | None = Field(default=None, max_length=120)
    # "edit": edit (and, with a key, name) every photograph.
    # "describe": name them only — for a shoot still edited elsewhere.
    mode: Literal["edit", "describe"] = "edit"


@router.post("/{listing_id}/ai-edit", status_code=202)
async def ai_edit(
    listing_id: uuid.UUID,
    _user: CurrentUser,
    db: DbDep,
    body: ProcessRequest | None = None,
) -> dict[str, Any]:
    """Auto-edit every photograph in the listing.

    With an Anthropic key configured this is the AI recipe-picker — one
    768px preview per photograph leaves for the API, slider values come
    back. Without a key it applies the tuned listing preset, entirely
    locally. Either way the renders happen here at full resolution, the
    chosen sky is composited wherever segmentation finds sky, results land
    as ordinary recipe versions, and the operator tweaks from there.

    With mode "describe", nothing is edited: each photograph not already
    named by the operator gets a caption and file-name slug from the same
    API. That needs the key — there is no local fallback for describing.
    """
    from framefound.media.maps_store import load_ai_edit_config

    body = body or ProcessRequest()
    await _get(db, listing_id)
    config = await load_ai_edit_config(db)
    if body.mode == "describe":
        if not config.ready:
            raise HTTPException(
                status_code=400,
                detail="Naming photos uses the Claude API — add an Anthropic key on Security",
            )
        mode = "describe"
    else:
        mode = "ai" if config.ready else "preset"
    if body.sky_name and ("/" in body.sky_name or "\\" in body.sky_name or ".." in body.sky_name):
        raise HTTPException(status_code=400, detail="Not a sky name")
    stmt = (
        select(func.count())
        .select_from(ListingItem)
        .join(Asset, Asset.id == ListingItem.asset_id)
        .where(ListingItem.listing_id == listing_id, Asset.media_type == "image")
    )
    if mode == "describe":
        # The worker skips these too; counting them would promise progress
        # that never comes.
        stmt = stmt.where(ListingItem.naming_source != "confirmed")
    images = (await db.execute(stmt)).scalar_one()
    if not images:
        if mode == "describe":
            return {"queued": 0, "mode": mode}
        raise HTTPException(status_code=400, detail="No photographs to edit")

    try:
        from framefound.processing.tasks import ai_edit_listing

        ai_edit_listing.delay(str(listing_id), body.sky_name, mode)
    except Exception:
        raise HTTPException(status_code=503, detail="The processing queue is unavailable") from None
    log.info(
        "listing.ai_edit_queued",
        listing_id=str(listing_id),
        images=images,
        mode=mode,
        sky=body.sky_name or "",
    )
    return {"queued": images, "mode": mode}


class RemovalSuggestion(BaseModel):
    asset_id: uuid.UUID
    filename: str
    reason: str
    keep_instead: uuid.UUID | None


@router.post("/{listing_id}/curate", response_model=list[RemovalSuggestion])
async def curate(listing_id: uuid.UUID, _user: CurrentUser, db: DbDep) -> list[RemovalSuggestion]:
    """Which photographs this listing can afford to lose.

    Near-duplicate groups keep their sharpest frame; markedly soft frames
    are offered up — but a room never loses its last photograph, because
    coverage beats polish. Suggestions only: nothing is removed until the
    operator says so.
    """
    from pathlib import Path as PathLib

    from PIL import Image, ImageOps

    from framefound.db.models import Library as LibraryModel
    from framefound.media import curate as curate_lib
    from framefound.scanner.paths import PathValidationError, safe_join

    rows = (
        await db.execute(
            select(ListingItem, Asset, LibraryModel)
            .join(Asset, Asset.id == ListingItem.asset_id)
            .join(LibraryModel, LibraryModel.id == Asset.library_id)
            .where(ListingItem.listing_id == listing_id, Asset.media_type == "image")
        )
    ).all()
    if not rows:
        return []

    embedding_rows = (
        await db.execute(
            select(Frame.asset_id, Frame.embedding).where(
                Frame.asset_id.in_([a.id for _i, a, _l in rows]),
                Frame.embedding.is_not(None),
            )
        )
    ).all()
    embeddings: dict[uuid.UUID, list[float] | None] = {aid: emb for aid, emb in embedding_rows}

    def measure(path: PathLib) -> float:
        with Image.open(path) as img:
            image = ImageOps.exif_transpose(img) or img
            small = image.convert("RGB")
            small.thumbnail((384, 384), Image.Resampling.BILINEAR)
            return curate_lib.sharpness(small)

    items = []
    names = {}
    for item, asset, library in rows:
        names[str(asset.id)] = asset.filename
        try:
            path = safe_join(PathLib(library.root_path), asset.relative_path)
            sharp = await asyncio.to_thread(measure, path)
        except (PathValidationError, OSError):
            continue
        items.append(
            {
                "id": str(asset.id),
                "room": item.room or "",
                "sharpness": sharp,
                "embedding": embeddings.get(asset.id),
            }
        )

    suggestions = curate_lib.suggest_removals(items)
    log.info("listing.curated", listing_id=str(listing_id), suggestions=len(suggestions))
    return [
        RemovalSuggestion(
            asset_id=uuid.UUID(s["id"]),
            filename=names.get(s["id"], ""),
            reason=s["reason"],
            keep_instead=uuid.UUID(s["keep_instead"]) if s["keep_instead"] else None,
        )
        for s in suggestions
    ]


@router.get("/{listing_id}/export/download")
async def download_export(  # type: ignore[no-untyped-def]
    listing_id: uuid.UUID, _user: CurrentUser, db: DbDep, settings: SettingsDep
):
    listing = await _get(db, listing_id)
    if listing.export_status != "ready" or not listing.export_relpath:
        raise HTTPException(status_code=404, detail="No export is ready")
    if await _export_stale(db, listing):
        raise HTTPException(
            status_code=409,
            detail="This listing changed after the zip was made — export again so the "
            "download matches what you see",
        )
    path = settings.data_dir / listing.export_relpath
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Export file is missing")
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in listing.name)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"{safe_name.strip() or 'listing'}.zip",
    )


@router.delete("/{listing_id}", status_code=204, dependencies=[require_admin])
async def delete_listing(
    listing_id: uuid.UUID, user: CurrentUser, db: DbDep, settings: SettingsDep
) -> None:
    """Delete the listing and its export. The photographs stay — a listing is
    an arrangement of assets, never their owner."""
    listing = await db.get(Listing, listing_id)
    if listing is None:
        return
    if listing.export_relpath:
        (settings.data_dir / listing.export_relpath).unlink(missing_ok=True)
    # Unlike delete_slideshow (a recorded gap): destructive + admin-only means
    # the audit log gets a row saying who removed it.
    db.add(
        AuditLog(
            event="listing.deleted",
            actor_user_id=user.id,
            detail={"listing_id": str(listing_id), "name": listing.name},
        )
    )
    await db.delete(listing)
    await db.commit()
    log.info("listing.deleted", listing_id=str(listing_id))
