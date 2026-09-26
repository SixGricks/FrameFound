"""Listings: room suggestions, operator ordering, and the exported zip.

The export filename sequence is the product — MLS galleries display in upload
order — so most of these tests are about order surviving the trip: canonical
arrangement, explicit reorder, and numbering that stays contiguous even when
a source file cannot be read.
"""

import asyncio
import io
import uuid as uuidlib
import zipfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.ai import rooms as rooms_lib
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, AuditLog, Frame, Library, Listing, PathMapping

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def _unit(index: int) -> list[float]:
    vector = [0.0] * 512
    vector[index] = 1.0
    return vector


# One fake text vector per room, in ROOMS order: room i lives on axis i, so an
# asset embedded on axis i is unambiguously that room.
FAKE_ROOM_VECTORS = [_unit(i) for i in range(len(rooms_lib.ROOMS))]
ROOM_INDEX = {room.key: i for i, room in enumerate(rooms_lib.ROOMS)}


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'listings.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "listing-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    get_settings.cache_clear()
    monkeypatch.setattr(rooms_lib, "room_vectors", lambda: FAKE_ROOM_VECTORS)
    # No broker in tests: the endpoint queues, the test runs the task inline.
    from framefound.processing import tasks as tasks_module

    monkeypatch.setattr(tasks_module.export_listing_zip, "delay", lambda *args: None)

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override

    root = tmp_path / "media"
    root.mkdir()
    ids: dict[str, str] = {}
    async with factory() as db:
        library = Library(name="L", root_path=str(root))
        db.add(library)
        await db.flush()

        async def add_asset(
            name: str, room_key: str | None, media_type: str = "image", on_disk: bool = True
        ) -> str:
            if on_disk and media_type == "image":
                img = Image.new("RGB", (120, 90), (200, 60 + len(name), 40))
                img.save(root / name, "JPEG")
            asset = Asset(
                library_id=library.id,
                relative_path=name,
                filename=name,
                extension=name.rsplit(".", 1)[-1],
                media_type=media_type,
                size_bytes=1000,
                mtime=BASE,
                availability="online",
            )
            db.add(asset)
            await db.flush()
            frame = Frame(asset_id=asset.id, ts_ms=0, relative_path=f"f/{name}.jpeg")
            if room_key is not None:
                frame.embedding = _unit(ROOM_INDEX[room_key])
            db.add(frame)
            await db.flush()
            return str(asset.id)

        ids["kitchen"] = await add_asset("kit.jpg", "kitchen")
        ids["front"] = await add_asset("front.jpg", "front_exterior")
        ids["bedroom"] = await add_asset("bed.jpg", "bedroom")
        ids["mystery"] = await add_asset("mys.jpg", None)  # no embedding
        ids["ghost"] = await add_asset("ghost.jpg", "bathroom", on_disk=False)
        ids["video"] = await add_asset("walk.mp4", "living_room", media_type="video")
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"client": client, "ids": ids, "factory": factory, "tmp": tmp_path}
    await engine.dispose()
    get_settings.cache_clear()


async def _create(env: dict, keys: list[str], name: str = "12 Maple St") -> dict:
    resp = await env["client"].post(
        "/api/v1/listings",
        json={"name": name, "asset_ids": [env["ids"][k] for k in keys]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_rooms_endpoint_lists_the_taxonomy_in_walkthrough_order(env: dict) -> None:
    rooms = (await env["client"].get("/api/v1/listings/rooms")).json()
    keys = [r["key"] for r in rooms]
    assert keys[0] == "front_exterior", "kerb appeal leads"
    assert keys[-1] == "floor_plan", "plans close"
    assert "kitchen" in keys


async def test_creating_a_listing_suggests_rooms_and_arranges_them(env: dict) -> None:
    body = await _create(env, ["bedroom", "kitchen", "front", "mystery"])
    by_asset = {i["asset_id"]: i for i in body["items"]}
    assert by_asset[env["ids"]["kitchen"]]["room"] == "kitchen"
    assert by_asset[env["ids"]["front"]]["room"] == "front_exterior"
    assert by_asset[env["ids"]["kitchen"]]["room_source"] == "suggested", (
        "a guess is a guess until someone says otherwise"
    )
    # Canonical walk-through: front door before the kitchen, kitchen before
    # bedrooms, and the unclassifiable photo at the end rather than lost.
    ordered = [i["asset_id"] for i in sorted(body["items"], key=lambda i: i["position"])]
    assert ordered == [
        env["ids"]["front"],
        env["ids"]["kitchen"],
        env["ids"]["bedroom"],
        env["ids"]["mystery"],
    ]
    assert by_asset[env["ids"]["mystery"]]["room"] == ""


async def test_an_override_is_confirmed_and_survives_reclassification(env: dict) -> None:
    body = await _create(env, ["kitchen", "front"])
    listing_id = body["id"]
    resp = await env["client"].put(
        f"/api/v1/listings/{listing_id}/items/{env['ids']['kitchen']}/room",
        json={"room": "dining_room"},
    )
    assert resp.status_code == 200
    resp = await env["client"].post(f"/api/v1/listings/{listing_id}/classify")
    by_asset = {i["asset_id"]: i for i in resp.json()["items"]}
    assert by_asset[env["ids"]["kitchen"]]["room"] == "dining_room", (
        "the operator said dining room; the model does not get to argue"
    )
    assert by_asset[env["ids"]["kitchen"]]["room_source"] == "confirmed"


async def test_unknown_room_is_refused(env: dict) -> None:
    body = await _create(env, ["kitchen"])
    resp = await env["client"].put(
        f"/api/v1/listings/{body['id']}/items/{env['ids']['kitchen']}/room",
        json={"room": "ballroom"},
    )
    assert resp.status_code == 400


async def test_partial_reorder_keeps_the_rest_stable(env: dict) -> None:
    body = await _create(env, ["front", "kitchen", "bedroom", "mystery"])
    listing_id = body["id"]
    # Drag only the bedroom to the top; everyone else keeps their relative order.
    resp = await env["client"].post(
        f"/api/v1/listings/{listing_id}/reorder",
        json={"asset_ids": [env["ids"]["bedroom"]]},
    )
    ordered = [i["asset_id"] for i in sorted(resp.json()["items"], key=lambda i: i["position"])]
    assert ordered == [
        env["ids"]["bedroom"],
        env["ids"]["front"],
        env["ids"]["kitchen"],
        env["ids"]["mystery"],
    ]


async def test_export_names_files_in_order_and_closes_ranks_on_a_bad_file(env: dict) -> None:
    """ghost.jpg is catalogued but unreadable: it must be skipped by name,
    and the numbering must stay contiguous — a gallery with a hole in its
    sequence reads as a mistake."""
    body = await _create(env, ["front", "kitchen", "bedroom", "ghost", "video"])
    listing_id = body["id"]

    resp = await env["client"].post(f"/api/v1/listings/{listing_id}/export", json={})
    assert resp.status_code == 202

    from framefound.processing.tasks import export_listing_zip

    await asyncio.to_thread(export_listing_zip, listing_id, 3840, 85)

    async with env["factory"]() as db:
        listing = await db.get(Listing, uuidlib.UUID(listing_id))
        assert listing is not None
        assert listing.export_status == "ready"
        assert listing.export_error is not None and "ghost.jpg" in listing.export_error
        zip_path = get_settings().data_dir / str(listing.export_relpath)
        assert zip_path.is_file()

    with zipfile.ZipFile(zip_path) as archive:
        photos = [n for n in archive.namelist() if not n.startswith("_index/")]
        assert photos == [
            "01-front-exterior-12-maple-st.jpg",
            "02-kitchen-12-maple-st.jpg",
            "03-bedroom-12-maple-st.jpg",
        ], "ordered, contiguous, and no video in a photo gallery"
        with archive.open(photos[0]) as fh, Image.open(io.BytesIO(fh.read())) as img:
            assert img.format == "JPEG"
            assert max(img.size) <= 3840
        index = archive.read("_index/Photo Index.md").decode()
        assert "ghost.jpg" not in index, "the index lists what the zip holds, not what failed"

    resp = await env["client"].get(f"/api/v1/listings/{listing_id}/export/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"


async def test_export_resizes_to_the_requested_edge(env: dict, tmp_path: Path) -> None:
    root = Path(tmp_path / "media")
    Image.new("RGB", (4000, 3000), (10, 120, 200)).save(root / "big.jpg", "JPEG")
    async with env["factory"]() as db:
        library_id = (await db.execute(select(Library.id))).scalar_one()
        asset = Asset(
            library_id=library_id,
            relative_path="big.jpg",
            filename="big.jpg",
            extension="jpg",
            media_type="image",
            size_bytes=5,
            mtime=BASE,
            availability="online",
        )
        db.add(asset)
        await db.flush()
        big_id = str(asset.id)
        await db.commit()

    env["ids"]["big"] = big_id
    body = await _create(env, ["big"], name="Resize Test")
    from framefound.processing.tasks import export_listing_zip

    await asyncio.to_thread(export_listing_zip, body["id"], 2048, 85)
    async with env["factory"]() as db:
        listing = await db.get(Listing, uuidlib.UUID(body["id"]))
        assert listing is not None and listing.export_status == "ready"
        zip_path = get_settings().data_dir / str(listing.export_relpath)
    with (
        zipfile.ZipFile(zip_path) as archive,
        archive.open(archive.namelist()[0]) as fh,
        Image.open(io.BytesIO(fh.read())) as img,
    ):
        assert max(img.size) == 2048


async def _export_now(env: dict, listing_id: str, *args: object) -> zipfile.ZipFile:
    from framefound.processing.tasks import export_listing_zip

    await env["client"].post(f"/api/v1/listings/{listing_id}/export", json={})
    await asyncio.to_thread(export_listing_zip, listing_id, 3840, 85, *args)
    zip_path = get_settings().data_dir / "exports" / "listings" / f"{listing_id}.zip"
    return zipfile.ZipFile(zip_path)


async def test_the_export_is_the_delivery_package(env: dict) -> None:
    """Named files, a Photo Index that says what each one shows, a CSV of
    the same, and contact sheets — the package that used to be assembled by
    hand before every paid editing batch."""
    client = env["client"]
    body = await _create(env, ["front", "kitchen", "bedroom"], name="09-24 130 Davis Rd")
    url = f"/api/v1/listings/{body['id']}"
    resp = await client.patch(
        url, json={"file_suffix": "130 Davis Rd. Auction!", "notes": "Auction Oct 12, 10 AM"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["file_suffix"] == "130-davis-rd-auction", "cleaned to what a name can hold"
    resp = await client.put(
        f"{url}/items/{env['ids']['kitchen']}/naming",
        json={"caption": "Kitchen | island with pendants", "slug": "kitchen island"},
    )
    detail = resp.json()

    with await _export_now(env, body["id"]) as archive:
        names = archive.namelist()
        photos = [n for n in names if not n.startswith("_index/")]
        assert photos == [
            "01-front-exterior-130-davis-rd-auction.jpg",
            "02-kitchen-island-130-davis-rd-auction.jpg",
            "03-bedroom-130-davis-rd-auction.jpg",
        ]
        assert [i["export_name"] for i in detail["items"]] == photos, (
            "the page previews exactly the names the zip holds"
        )
        index = archive.read("_index/Photo Index.md").decode()
        assert index.startswith("# 09-24 130 Davis Rd — Photo Index")
        assert "Auction Oct 12, 10 AM" in index, "the notes head the index"
        assert "| 02 | 02-kitchen-island-130-davis-rd-auction.jpg |" in index
        assert "Kitchen \\| island with pendants" in index, "a pipe cannot split the table"
        assert "| kit.jpg |" in index, "the original file name, for finding the source"
        assert "| Bedroom |" in index, "an unnamed photo falls back to its room"
        csv_text = archive.read("_index/photo-index.csv").decode("utf-8-sig")
        assert csv_text.splitlines()[0].startswith("number,filename,what_it_shows")
        assert len(csv_text.splitlines()) == 4
        with Image.open(io.BytesIO(archive.read("_index/sheet01.jpg"))) as sheet:
            assert sheet.format == "JPEG"
            assert sheet.width > sheet.height, "one row of three on a four-column sheet"


async def test_simple_naming_without_the_index_is_the_old_zip(env: dict) -> None:
    body = await _create(env, ["front", "kitchen"])
    with await _export_now(env, body["id"], "simple", False) as archive:
        assert archive.namelist() == ["01_front_exterior.jpg", "02_kitchen.jpg"]


async def test_a_suffix_falls_back_to_the_name_without_its_date(env: dict) -> None:
    body = await _create(env, ["front"], name="00-00 5096 Old Philadelphia Pike Kinzers")
    assert body["suggested_suffix"] == "5096-old-philadelphia-pike-kinzers"
    assert body["items"][0]["export_name"] == (
        "01-front-exterior-5096-old-philadelphia-pike-kinzers.jpg"
    )
    cleared = (
        await env["client"].patch(f"/api/v1/listings/{body['id']}", json={"file_suffix": ""})
    ).json()
    assert cleared["file_suffix"] == ""
    assert cleared["items"][0]["export_name"].endswith("-5096-old-philadelphia-pike-kinzers.jpg")


async def test_renaming_a_photo_or_the_listing_makes_the_zip_stale(env: dict) -> None:
    client = env["client"]
    body = await _create(env, ["front", "kitchen"])
    url = f"/api/v1/listings/{body['id']}"
    (await _export_now(env, body["id"])).close()
    assert (await client.get(url)).json()["export_stale"] is False

    await client.put(f"{url}/items/{env['ids']['kitchen']}/naming", json={"caption": "New words"})
    assert (await client.get(url)).json()["export_stale"] is True

    (await _export_now(env, body["id"])).close()
    await client.patch(url, json={"file_suffix": "somewhere-else"})
    assert (await client.get(url)).json()["export_stale"] is True

    (await _export_now(env, body["id"])).close()
    await client.patch(url, json={"notes": "Terms: 10% down"})
    assert (await client.get(url)).json()["export_stale"] is True


async def test_deleting_a_listing_removes_the_zip_and_leaves_a_trace(env: dict) -> None:
    body = await _create(env, ["front"])
    listing_id = body["id"]
    from framefound.processing.tasks import export_listing_zip

    await env["client"].post(f"/api/v1/listings/{listing_id}/export", json={})
    await asyncio.to_thread(export_listing_zip, listing_id, 3840, 85)
    zip_path = get_settings().data_dir / "exports" / "listings" / f"{listing_id}.zip"
    assert zip_path.is_file()

    resp = await env["client"].delete(f"/api/v1/listings/{listing_id}")
    assert resp.status_code == 204
    assert not zip_path.exists(), "the export goes with the listing"

    async with env["factory"]() as db:
        events = (
            (await db.execute(select(AuditLog).where(AuditLog.event == "listing.deleted")))
            .scalars()
            .all()
        )
        assert len(events) == 1, "destructive + admin-only = audited"
        assert events[0].detail["listing_id"] == listing_id
        # The photographs themselves are untouched.
        assert (
            await db.execute(select(Asset).where(Asset.id == uuidlib.UUID(env["ids"]["front"])))
        ).scalar_one_or_none() is not None


async def test_a_zip_that_no_longer_matches_the_listing_is_not_served(env: dict) -> None:
    """Export, spot a mistake, fix it, press Download — the old zip used to
    come back, and MLS got the old gallery. Every kind of change counts."""
    from framefound.processing.tasks import export_listing_zip

    client = env["client"]
    body = await _create(env, ["front", "kitchen", "bedroom"])
    listing_id = body["id"]
    url = f"/api/v1/listings/{listing_id}"

    async def export() -> None:
        await client.post(f"{url}/export", json={})
        await asyncio.to_thread(export_listing_zip, listing_id, 3840, 85)

    await export()
    assert (await client.get(url)).json()["export_stale"] is False
    assert (await client.get(f"{url}/export/download")).status_code == 200

    # A reorder.
    await client.post(f"{url}/reorder", json={"asset_ids": [env["ids"]["bedroom"]]})
    assert (await client.get(url)).json()["export_stale"] is True
    refused = await client.get(f"{url}/export/download")
    assert refused.status_code == 409
    assert "export again" in refused.json()["error"]["message"]

    await export()
    assert (await client.get(url)).json()["export_stale"] is False

    # An edit to one photograph's recipe.
    resp = await client.put(f"/api/v1/develop/{env['ids']['kitchen']}", json={"exposure": 0.4})
    assert resp.status_code == 200, resp.text
    assert (await client.get(url)).json()["export_stale"] is True

    await export()
    # A relabelled room.
    await client.put(f"{url}/items/{env['ids']['front']}/room", json={"room": "backyard"})
    assert (await client.get(url)).json()["export_stale"] is True


async def test_an_export_from_before_fingerprints_counts_as_stale(env: dict) -> None:
    """A zip that cannot prove it matches must not ship."""
    body = await _create(env, ["front"])
    async with env["factory"]() as db:
        listing = await db.get(Listing, uuidlib.UUID(body["id"]))
        assert listing is not None
        listing.export_status = "ready"
        listing.export_relpath = f"exports/listings/{listing.id}.zip"
        await db.commit()
    detail = (await env["client"].get(f"/api/v1/listings/{body['id']}")).json()
    assert detail["export_stale"] is True


async def test_export_with_no_images_is_refused(env: dict) -> None:
    body = await _create(env, ["video"], name="Video Only")
    resp = await env["client"].post(f"/api/v1/listings/{body['id']}/export", json={})
    assert resp.status_code == 400


async def test_the_cover_is_the_first_photo_even_after_the_first_was_removed(env: dict) -> None:
    """The index took min(asset_id) at position 0: Postgres has no min() for
    UUIDs (the Listings page failed in production), and a listing whose first
    photo was removed had no position 0 and so no cover."""
    body = await _create(env, ["front", "kitchen", "bedroom"])
    items = sorted(body["items"], key=lambda i: i["position"])
    first, second = items[0]["asset_id"], items[1]["asset_id"]
    resp = await env["client"].delete(f"/api/v1/listings/{body['id']}/items/{first}")
    assert resp.status_code in (200, 204), resp.text

    listed = (await env["client"].get("/api/v1/listings")).json()
    mine = next(entry for entry in listed if entry["id"] == body["id"])
    assert mine["cover_asset_id"] == second


async def test_a_panel_opens_a_listing_at_workstation_paths_with_its_raw_originals(
    env: dict,
) -> None:
    """Lightroom's "Import FrameFound listing…": the photos in order, at the
    paths this machine sees, each with the DNG the drone wrote beside it."""
    listing = await _create(env, ["front", "kitchen"], name="GELCO calendar")
    async with env["factory"]() as db:
        library = (await db.execute(select(Library))).scalar_one()
        db.add(
            PathMapping(
                library_id=library.id,
                profile_name="Studio",
                platform="windows",
                mapped_prefix="Y:\\",
            )
        )
        db.add(
            Asset(
                library_id=library.id,
                relative_path="front.DNG",
                filename="front.DNG",
                extension="DNG",
                media_type="image",
                size_bytes=1000,
                mtime=BASE,
                availability="online",
            )
        )
        await db.commit()
    client = env["client"]
    listings = (await client.get("/api/v1/panel/listings")).json()
    assert [(x["name"], x["photos"]) for x in listings] == [("GELCO calendar", 2)]

    url = f"/api/v1/panel/listings/{listing['id']}"
    detail = (await client.get(url, params={"profile": "Studio"})).json()
    assert [i["position"] for i in detail["items"]] == [1, 2]
    by_name = {i["filename"]: i for i in detail["items"]}
    assert by_name["front.jpg"]["path"] == "Y:\\front.jpg"
    assert by_name["front.jpg"]["raw_path"] == "Y:\\front.DNG"
    assert by_name["kit.jpg"]["raw_path"] is None
    assert "1 of 2 have a RAW original" in detail["note"]
    unmapped = (await client.get(url)).json()
    assert all(i["path"] is None for i in unmapped["items"]), "no profile, no guessed paths"
    assert (await client.get(f"/api/v1/panel/listings/{uuidlib.uuid4()}")).status_code == 404
