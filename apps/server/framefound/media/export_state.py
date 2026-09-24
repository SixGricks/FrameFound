"""Does a listing's exported zip still match the listing?

The download used to serve whatever zip existed. Export, notice a mislabelled
room, fix it, press Download: the MLS upload got the old order and the old
edits, and nothing said so. Now the export records a fingerprint of
everything that decides its bytes, and a zip whose fingerprint no longer
matches the listing is stale — shown as out of date, and not served.

Fingerprinted rather than invalidated by each mutation: a listing changes in
a dozen ways (reorder, relabel, add, remove, edit, revert, auto-edit, apply
to all, object removal, undo), and a fingerprint cannot miss the one that is
added next year.
"""

import hashlib
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import Asset, AssetEdit, AssetInpaint, ListingItem


async def listing_fingerprint(db: AsyncSession, listing_id: uuid.UUID) -> str:
    """A digest of the export's inputs: the photographs in gallery order,
    their room labels, their current recipes and their object-removal state.
    Mirrors the rows export_listing_zip reads, videos excluded as it does."""
    items = (
        await db.execute(
            select(ListingItem.asset_id, ListingItem.room)
            .join(Asset, Asset.id == ListingItem.asset_id)
            .where(ListingItem.listing_id == listing_id, Asset.media_type == "image")
            .order_by(ListingItem.position, ListingItem.created_at)
        )
    ).all()
    ids = [asset_id for asset_id, _room in items]

    recipes: dict[uuid.UUID, str] = {}
    removals: dict[uuid.UUID, str] = {}
    if ids:
        edits = (
            await db.execute(
                select(AssetEdit)
                .where(AssetEdit.asset_id.in_(ids))
                .order_by(AssetEdit.asset_id, AssetEdit.version)
            )
        ).scalars()
        for edit in edits:  # ascending: the newest version wins
            # The recipe itself, not its version number: reverting deletes the
            # rows, so a later "version 1" can hold a different recipe.
            recipes[edit.asset_id] = json.dumps(edit.recipe, sort_keys=True)
        rounds = (
            await db.execute(
                select(AssetInpaint.asset_id, AssetInpaint.id)
                .where(AssetInpaint.asset_id.in_(ids), AssetInpaint.status == "ready")
                .order_by(AssetInpaint.asset_id, AssetInpaint.version)
            )
        ).all()
        for asset_id, round_id in rounds:
            # Row identity, for the same reason: undo then redo reuses a
            # version number with a different mask.
            removals[asset_id] = str(round_id)

    digest = hashlib.sha256()
    for asset_id, room in items:
        line = f"{asset_id}|{room}|{recipes.get(asset_id, '')}|{removals.get(asset_id, '')}\n"
        digest.update(line.encode())
    return digest.hexdigest()
