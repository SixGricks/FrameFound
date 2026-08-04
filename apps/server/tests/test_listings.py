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
from framefound.db.models import Asset, AuditLog, Frame, Library, Listing

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
        names = archive.namelist()
        assert names == [
            "01_front_exterior.jpg",
            "02_kitchen.jpg",
            "03_bedroom.jpg",
        ], "ordered, contiguous, and no video in a photo gallery"
        with archive.open(names[0]) as fh, Image.open(io.BytesIO(fh.read())) as img:
            assert img.format == "JPEG"
            assert max(img.size) <= 3840

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


async def test_export_with_no_images_is_refused(env: dict) -> None:
    body = await _create(env, ["video"], name="Video Only")
    resp = await env["client"].post(f"/api/v1/listings/{body['id']}/export", json={})
    assert resp.status_code == 400
