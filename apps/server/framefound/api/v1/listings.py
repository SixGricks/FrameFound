"""Listings: order and name a property shoot for upload.

The deliverable is a zip whose filenames sort into gallery order —
`01_front_exterior.jpg, 02_kitchen.jpg …` — because MLS galleries display in
upload order and renaming by hand is the chore this feature deletes.

Room labels come zero-shot from embeddings the catalogue already stores, and
they are suggestions until confirmed or overridden. Ordering is the
operator's: "arrange" applies the canonical walk-through as a starting point,
then explicit reorder wins and nothing shuffles it afterwards.
"""

import asyncio
import uuid

import structlog
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select

from framefound.ai import rooms as rooms_lib
from framefound.ai.embeddings import EmbeddingUnavailable
from framefound.auth.deps import CurrentUser, DbDep, SettingsDep, require_admin
from framefound.db.models import Asset, AuditLog, Frame, Listing, ListingItem

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


def _item_out(item: ListingItem, asset: Asset) -> ItemOut:
    return ItemOut(
        asset_id=item.asset_id,
        filename=asset.filename,
        media_type=asset.media_type,
        position=item.position,
        room=item.room,
        room_label=rooms_lib.ROOM_LABELS.get(item.room, ""),
        room_source=item.room_source,
        room_score=item.room_score,
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
    items = [_item_out(item, asset) for item, asset in rows]
    return ListingDetail(
        id=listing.id,
        name=listing.name,
        item_count=len(items),
        export_status=listing.export_status,
        export_error=listing.export_error,
        cover_asset_id=items[0].asset_id if items else None,
        items=items,
        classified=classified,
    )


@router.get("/rooms", response_model=list[RoomOut])
async def list_rooms(_user: CurrentUser) -> list[RoomOut]:
    """The taxonomy, in canonical listing order — the UI's label dropdown."""
    return [RoomOut(key=room.key, label=room.label) for room in rooms_lib.ROOMS]


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
    cover_rows = (
        await db.execute(
            select(ListingItem.listing_id, func.min(ListingItem.asset_id))
            .where(ListingItem.position == 0)
            .group_by(ListingItem.listing_id)
        )
    ).all()
    covers: dict[uuid.UUID, uuid.UUID] = {lid: aid for lid, aid in cover_rows}
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

        export_listing_zip.delay(str(listing_id), body.max_edge, body.quality)
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
    )
    return await _detail(db, listing, classified=True)


@router.get("/{listing_id}/export/download")
async def download_export(  # type: ignore[no-untyped-def]
    listing_id: uuid.UUID, _user: CurrentUser, db: DbDep, settings: SettingsDep
):
    listing = await _get(db, listing_id)
    if listing.export_status != "ready" or not listing.export_relpath:
        raise HTTPException(status_code=404, detail="No export is ready")
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
