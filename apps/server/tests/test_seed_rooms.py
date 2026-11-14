"""The seed's room labels (`infrastructure/scripts/seed_dev.py` `DAVIS_ROOMS`, `correct_rooms`).

Found in final QA: the Davis Rd listing was seeded with "exterior" and
"living-room", which are not keys in the room list, so five of its tiles read
"— clear label —" and the viewer said "no room label yet". A fresh seed now
uses the room list's own keys, and seeding again corrects a catalogue seeded
before, touching nothing a reviewer chose.

Runs the script as a reviewer does (the helpers of `test_seed_colour.py`), and
starting a program or reaching a real address fails the test as it does there.
"""

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_seed_colour import _get, _nothing_runs, _review, _run_seed

from framefound.db.base import Base
from framefound.db.models import Listing, ListingItem

__all__ = ["_nothing_runs"]  # the autouse fixture, imported to apply here as well

DAVIS = "130 Davis Rd Auction"


async def _davis(
    client: Any, factory: async_sessionmaker[AsyncSession]
) -> tuple[str, list[dict[str, Any]]]:
    async with factory() as db:
        listing = await db.scalar(select(Listing.id).where(Listing.name == DAVIS))
    assert listing is not None
    detail = await _get(client, f"/api/v1/listings/{listing}")
    return str(listing), detail["items"]


async def test_every_seeded_room_is_one_the_room_list_knows_and_an_old_catalogue_is_corrected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "ff-ui"
    root.mkdir()
    engine = create_async_engine(f"sqlite+aiosqlite:///{(root / 'ui.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()

    _run_seed(root, monkeypatch)
    assert not [line for line in capsys.readouterr().out.splitlines() if line.startswith("Rooms:")]

    async with _review(monkeypatch) as (client, factory):
        rooms = {
            row["key"]: row["label"] for row in (await client.get("/api/v1/listings/rooms")).json()
        }
        listing_id, items = await _davis(client, factory)
        named = [item for item in items if item["room"]]
        assert named, "the seed gave the listing no rooms"
        # Every tile's select has an option for its room, and the viewer a label.
        assert {item["room"] for item in named} <= set(rooms)
        assert all(item["room_label"] == rooms[item["room"]] for item in named)
        assert {"front_exterior", "living_room"} <= {item["room"] for item in named}
        first = items[0]
        assert first["caption"] == "Davis Rd — Front exterior"

        # A catalogue seeded before the fix: the old keys and the captions the
        # seed wrote with them. On one photograph a reviewer has written a
        # caption of their own, and on another confirmed a room.
        async with factory() as db:
            rows = (
                await db.scalars(
                    select(ListingItem)
                    .where(ListingItem.listing_id == uuid.UUID(listing_id))
                    .order_by(ListingItem.position)
                )
            ).all()
            old = {"front_exterior": "exterior", "living_room": "living-room"}
            for row in rows:
                if row.room in old:
                    row.room = old[row.room]
                    row.caption = f"Davis Rd — {row.room}"
            rows[0].caption = "Porch at dusk"
            rows[1].room, rows[1].room_source = "office", "confirmed"
            await db.commit()
            stale = sum(1 for row in rows if row.room in ("exterior", "living-room"))

    _run_seed(root, monkeypatch, "--local-ai-only")
    said = [line for line in capsys.readouterr().out.splitlines() if line.startswith("Rooms:")]
    assert said == [
        f"Rooms: gave {stale} listing photographs the room list's own key for their room."
    ]

    async with _review(monkeypatch) as (client, factory):
        _, after = await _davis(client, factory)
        assert {item["room"] for item in after if item["room"]} <= set(rooms)
        assert all(item["room_label"] for item in after if item["room"])
        # Its room corrected, the reviewer's caption kept.
        assert (after[0]["room"], after[0]["caption"]) == ("front_exterior", "Porch at dusk")
        assert (after[1]["room"], after[1]["room_source"]) == ("office", "confirmed")
        # Everything else is as a fresh seed would have it.
        assert [(item["room"], item["caption"]) for item in after[2:]] == [
            (item["room"], item["caption"]) for item in items[2:]
        ]

    # Nothing left to correct the next time.
    _run_seed(root, monkeypatch, "--local-ai-only")
    assert not [line for line in capsys.readouterr().out.splitlines() if line.startswith("Rooms:")]
