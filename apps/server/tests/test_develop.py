"""The develop engine and its API: recipes, not pixels.

The engine tests pin behaviours, not exact pixel values — exposure brightens,
contrast spreads, a slider at zero changes nothing — because the promise to
the operator is directional and monotonic, and an implementation tweak that
shifts a pixel by one step of rounding should not break the suite.
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

from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, AssetEdit, Frame, Library, Listing
from framefound.media import develop

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def _mean(image: Image.Image) -> float:
    data = list(image.convert("L").getdata())
    return sum(data) / len(data)


def _gray(value: int = 100, size: int = 64) -> Image.Image:
    return Image.new("RGB", (size, size), (value, value, value))


# ---------------------------------------------------------------- engine


def test_identity_recipe_returns_the_image_unchanged() -> None:
    image = _gray()
    out = develop.apply_recipe(image, {"exposure": 0, "contrast": 0, "auto": False})
    assert out is image, "zero sliders must not even copy"


def test_exposure_brightens_and_darkens() -> None:
    image = _gray(100)
    brighter = develop.apply_recipe(image, {"exposure": 1.0})
    darker = develop.apply_recipe(image, {"exposure": -1.0})
    assert _mean(brighter) == pytest.approx(200, abs=3), "one stop doubles"
    assert _mean(darker) == pytest.approx(50, abs=3), "minus one halves"


def test_contrast_spreads_around_the_middle() -> None:
    image = Image.new("RGB", (2, 1))
    image.putpixel((0, 0), (80, 80, 80))
    image.putpixel((1, 0), (180, 180, 180))
    out = develop.apply_recipe(image, {"contrast": 0.5})
    dark, bright = out.getpixel((0, 0))[0], out.getpixel((1, 0))[0]
    assert dark < 80 and bright > 180, "below mid falls, above mid rises"


def test_temperature_warms_reds_and_cools_blues() -> None:
    out = develop.apply_recipe(_gray(120), {"temperature": 0.8})
    r, _g, b = out.getpixel((0, 0))
    assert r > 120 and b < 120


def test_shadow_lift_touches_shadows_not_highlights() -> None:
    image = Image.new("RGB", (2, 1))
    image.putpixel((0, 0), (30, 30, 30))
    image.putpixel((1, 0), (220, 220, 220))
    out = develop.apply_recipe(image, {"shadows": 1.0})
    lifted = out.getpixel((0, 0))[0]
    untouched = out.getpixel((1, 0))[0]
    assert lifted > 40, "shadows rise"
    assert abs(untouched - 220) <= 3, "highlights barely move"


def test_saturation_zero_is_not_grayscale_but_minus_one_is() -> None:
    image = Image.new("RGB", (1, 1), (200, 80, 60))
    gray = develop.apply_recipe(image, {"saturation": -1.0})
    r, g, b = gray.getpixel((0, 0))
    assert abs(r - g) <= 2 and abs(g - b) <= 2, "-100 is monochrome"


def test_auto_levels_opens_a_flat_exposure() -> None:
    # A murky image occupying 90..140 of the range.
    image = Image.new("RGB", (32, 32))
    for x in range(32):
        for y in range(32):
            v = 90 + (x + y) % 50
            image.putpixel((x, y), (v, v, v))
    out = develop.apply_recipe(image, {"auto": True})
    values = [p[0] for p in out.convert("RGB").getdata()]
    # Gains are deliberately bounded, so "opens up" means roughly doubled
    # spread, not a full-range stretch — a wall photographed flat should be
    # nudged, not shredded.
    assert max(values) - min(values) > 90, "the histogram roughly doubles its spread"


def test_extreme_sliders_never_produce_garbage() -> None:
    image = Image.new("RGB", (8, 8), (250, 5, 128))
    out = develop.apply_recipe(
        image,
        {
            "exposure": 2.0,
            "contrast": 1.0,
            "temperature": 1.0,
            "tint": -1.0,
            "shadows": 1.0,
            "highlights": 1.0,
            "vibrance": 1.0,
            "saturation": 1.0,
            "auto": True,
        },
    )
    assert out.size == image.size and out.mode == "RGB", "clipped, not crashed"


def test_clean_recipe_clamps_and_drops_junk() -> None:
    cleaned = develop.clean_recipe(
        {"exposure": 99, "contrast": "loud", "sharpen": 1, "tint": -0.5, "auto": True}
    )
    assert cleaned == {"exposure": 2.0, "tint": -0.5, "auto": True}


# ---------------------------------------------------------------- API


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'develop.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "develop-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    get_settings.cache_clear()

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
    Image.new("RGB", (200, 150), (100, 100, 100)).save(root / "dark.jpg", "JPEG")
    ids: dict[str, str] = {}
    async with factory() as db:
        library = Library(name="L", root_path=str(root))
        db.add(library)
        await db.flush()
        asset = Asset(
            library_id=library.id,
            relative_path="dark.jpg",
            filename="dark.jpg",
            extension="jpg",
            media_type="image",
            size_bytes=1000,
            mtime=BASE,
            availability="online",
        )
        db.add(asset)
        await db.flush()
        db.add(Frame(asset_id=asset.id, ts_ms=0, relative_path="f/d.jpeg"))
        video = Asset(
            library_id=library.id,
            relative_path="clip.mp4",
            filename="clip.mp4",
            extension="mp4",
            media_type="video",
            size_bytes=1000,
            mtime=BASE,
            availability="online",
        )
        db.add(video)
        await db.flush()
        ids["asset"] = str(asset.id)
        ids["video"] = str(video.id)
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"client": client, "ids": ids, "factory": factory}
    await engine.dispose()
    get_settings.cache_clear()


async def test_preview_renders_the_recipe(env: dict) -> None:
    asset_id = env["ids"]["asset"]
    resp = await env["client"].post(f"/api/v1/develop/{asset_id}/preview", json={"exposure": 1.0})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(resp.content)) as img:
        assert _mean(img) == pytest.approx(200, abs=6), "the preview shows the maths"


async def test_saving_appends_versions_and_clearing_reverts(env: dict) -> None:
    asset_id = env["ids"]["asset"]
    client = env["client"]

    first = (await client.put(f"/api/v1/develop/{asset_id}", json={"exposure": 0.5})).json()
    assert (first["version"], first["edited"]) == (1, True)
    second = (await client.put(f"/api/v1/develop/{asset_id}", json={"exposure": 0.8})).json()
    assert second["version"] == 2, "an edit is a new version, not an overwrite"

    state = (await client.get(f"/api/v1/develop/{asset_id}")).json()
    assert state["recipe"] == {"exposure": 0.8}

    # Saving the same recipe again minted no version.
    again = (await client.put(f"/api/v1/develop/{asset_id}", json={"exposure": 0.8})).json()
    assert again["version"] == 2

    cleared = (await client.delete(f"/api/v1/develop/{asset_id}")).json()
    assert (cleared["version"], cleared["edited"]) == (0, False)
    async with env["factory"]() as db:
        left = (
            await db.execute(select(AssetEdit).where(AssetEdit.asset_id == uuidlib.UUID(asset_id)))
        ).all()
        assert not left, "reverting deletes recipes; there were never pixels to delete"


async def test_only_photographs_can_be_developed(env: dict) -> None:
    resp = await env["client"].put(f"/api/v1/develop/{env['ids']['video']}", json={"exposure": 1.0})
    assert resp.status_code == 400


async def test_listing_export_applies_the_saved_recipe(env: dict) -> None:
    """The whole point of one engine: what the editor showed is what ships."""
    asset_id = env["ids"]["asset"]
    client = env["client"]
    await client.put(f"/api/v1/develop/{asset_id}", json={"exposure": 1.0})

    listing = (
        await client.post("/api/v1/listings", json={"name": "Edited", "asset_ids": [asset_id]})
    ).json()

    from framefound.processing import tasks as tasks_module

    await asyncio.to_thread(tasks_module.export_listing_zip, listing["id"], 2048, 85)

    async with env["factory"]() as db:
        row = await db.get(Listing, uuidlib.UUID(listing["id"]))
        assert row is not None and row.export_status == "ready"
        zip_path = get_settings().data_dir / str(row.export_relpath)
    with (
        zipfile.ZipFile(zip_path) as archive,
        archive.open(archive.namelist()[0]) as fh,
        Image.open(io.BytesIO(fh.read())) as img,
    ):
        assert _mean(img) == pytest.approx(200, abs=6), "the zip is one stop brighter"

    detail = (await client.get(f"/api/v1/listings/{listing['id']}")).json()
    item = detail["items"][0]
    assert item["edited"] is True, "the listing page can say which photos carry edits"


async def test_batch_apply_reaches_every_image_in_the_listing(env: dict) -> None:
    asset_id = env["ids"]["asset"]
    client = env["client"]
    listing = (
        await client.post(
            "/api/v1/listings",
            json={"name": "Batch", "asset_ids": [asset_id, env["ids"]["video"]]},
        )
    ).json()
    resp = await client.post(
        f"/api/v1/develop/listing/{listing['id']}/apply", json={"contrast": 0.3}
    )
    assert resp.status_code == 200
    assert resp.json() == {"applied": 1}, "images only; a video has no develop recipe"

    state = (await client.get(f"/api/v1/develop/{asset_id}")).json()
    assert state["recipe"] == {"contrast": 0.3}


# ---------------------------------------------------------------- sky


def _sky_scene(size: int = 64) -> tuple[Image.Image, "list[list[float]]"]:
    """A scene whose top half is 'sky' (bright) and bottom half ground
    (dark), with a mask that says exactly that."""
    image = Image.new("RGB", (size, size))
    for y in range(size):
        for x in range(size):
            image.putpixel((x, y), (200, 200, 220) if y < size // 2 else (60, 50, 40))
    mask = [[1.0 if y < size // 2 else 0.0 for _ in range(size)] for y in range(size)]
    return image, mask


def test_composite_replaces_sky_and_leaves_the_ground() -> None:
    import numpy as np

    from framefound.media.sky import composite_sky

    scene, mask_rows = _sky_scene()
    orange = Image.new("RGB", (64, 64), (240, 120, 30))
    out = composite_sky(
        scene, np.asarray(mask_rows, dtype="float32"), orange, feather=0.0, relight=0.0
    )
    r, g, b = out.getpixel((32, 8))
    assert r > 200 and b < 80, "the sky is now the replacement"
    ground = out.getpixel((32, 56))
    assert abs(ground[0] - 60) <= 4, "the house is still the house"


def test_composite_on_an_interior_is_a_silent_no_op() -> None:
    """The batch-apply guarantee: one recipe over a listing must not wreck
    the hallway photos."""
    import numpy as np

    from framefound.media.sky import composite_sky

    room = Image.new("RGB", (32, 32), (120, 110, 100))
    none = np.zeros((32, 32), dtype="float32")
    out = composite_sky(room, none, Image.new("RGB", (32, 32), (255, 0, 0)))
    assert out is room


def test_relight_pulls_the_ground_toward_the_sky_tone() -> None:
    import numpy as np

    from framefound.media.sky import composite_sky

    scene, mask_rows = _sky_scene()
    warm = Image.new("RGB", (64, 64), (250, 160, 60))
    plain = composite_sky(scene, np.asarray(mask_rows, dtype="float32"), warm, relight=0.0)
    lit = composite_sky(scene, np.asarray(mask_rows, dtype="float32"), warm, relight=1.0)
    assert lit.getpixel((32, 60))[0] > plain.getpixel((32, 60))[0], (
        "a warm sky warms the ground it shines on"
    )
    assert lit.getpixel((32, 60))[2] <= plain.getpixel((32, 60))[2] + 1


def test_clean_recipe_keeps_a_valid_sky_and_refuses_traversal() -> None:
    good = develop.clean_recipe({"sky": {"name": "dusk.jpg", "feather": 9, "relight": 0.5}})
    assert good["sky"]["name"] == "dusk.jpg"
    assert good["sky"]["feather"] == 0.2, "clamped, not trusted"
    assert good["sky"]["shift"] == 0.0, "defaults fill in"
    evil = develop.clean_recipe({"sky": {"name": "../../etc/passwd"}})
    assert "sky" not in evil, "a stored recipe is never a path"


def test_render_composites_then_grades() -> None:
    """Sky first, colour second: the sliders grade the finished picture."""
    import numpy as np

    scene, mask_rows = _sky_scene()
    dark_sky = Image.new("RGB", (64, 64), (40, 40, 80))

    out = develop.render(
        scene,
        {"exposure": 1.0, "sky": {"name": "night.jpg", "relight": 0.0, "feather": 0.0}},
        load_sky=lambda name: dark_sky,
        mask_for=lambda img: np.asarray(mask_rows, dtype="float32"),
    )
    r, _g, b = out.getpixel((32, 8))
    assert 60 < b < 200, "the replaced sky was then pushed a stop brighter"


def test_render_degrades_without_segmentation_or_sky_file() -> None:
    scene, _ = _sky_scene()
    out = develop.render(
        scene,
        {"exposure": 1.0, "sky": {"name": "gone.jpg"}},
        load_sky=lambda name: None,
        mask_for=lambda img: None,
    )
    assert _mean(out) > _mean(scene), "colour still applies when the sky cannot"


async def test_sky_library_upload_list_delete(env: dict) -> None:
    client = env["client"]
    buf = io.BytesIO()
    Image.new("RGB", (80, 40), (120, 160, 230)).save(buf, "JPEG")

    resp = await client.put("/api/v1/develop/skies/test-sky.jpg", content=buf.getvalue())
    assert resp.status_code == 201, resp.text

    listed = (await client.get("/api/v1/develop/skies")).json()
    assert [s["name"] for s in listed] == ["test-sky.jpg"]

    resp = await client.get("/api/v1/develop/skies/test-sky.jpg/image")
    assert resp.status_code == 200

    resp = await client.delete("/api/v1/develop/skies/test-sky.jpg")
    assert resp.status_code == 204
    assert (await client.get("/api/v1/develop/skies")).json() == []


async def test_sky_upload_refuses_garbage(env: dict) -> None:
    resp = await env["client"].put(
        "/api/v1/develop/skies/evil.jpg", content=b"#!/bin/sh\necho pwned"
    )
    assert resp.status_code == 400, "the compositor opens these unattended later"


async def test_export_with_a_sky_degrades_without_segmentation(env: dict) -> None:
    """This CI environment has no ONNX runtime, which is exactly the case:
    a recipe naming a sky must still export colour-only, not fail."""
    asset_id = env["ids"]["asset"]
    client = env["client"]
    await client.put(
        f"/api/v1/develop/{asset_id}",
        json={
            "exposure": 1.0,
            "sky": {"name": "dusk.jpg", "feather": 0.02, "shift": 0, "relight": 0.4},
        },
    )
    listing = (
        await client.post("/api/v1/listings", json={"name": "Sky", "asset_ids": [asset_id]})
    ).json()

    from framefound.processing import tasks as tasks_module

    await asyncio.to_thread(tasks_module.export_listing_zip, listing["id"], 2048, 85)
    async with env["factory"]() as db:
        row = await db.get(Listing, uuidlib.UUID(listing["id"]))
        assert row is not None
        assert row.export_status == "ready", row.export_error
