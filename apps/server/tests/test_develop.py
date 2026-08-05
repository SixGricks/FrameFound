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

    # No broker in tests: endpoints queue, tests run the tasks inline.
    from framefound.processing import tasks as tasks_module

    monkeypatch.setattr(tasks_module.export_listing_zip, "delay", lambda *args: None)
    monkeypatch.setattr(tasks_module.inpaint_asset, "delay", lambda *args: None)

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


# ------------------------------------------------------- geometry + pull


def test_rotate_keeps_size_and_leaves_no_black_corners() -> None:
    white = Image.new("RGB", (120, 80), (240, 240, 240))
    out = develop.apply_geometry(white, {"rotate": 3.0})
    assert out.size == (120, 80), "geometry must not change the canvas"
    for corner in ((1, 1), (118, 1), (1, 78), (118, 78)):
        assert out.getpixel(corner)[0] > 200, f"corner {corner} went dark"


def test_keystone_maps_the_inset_to_the_corner() -> None:
    """The contract, tested at the pixel: for positive correction the output
    top-left corner samples from the inset point of the source top edge."""
    image = Image.new("RGB", (100, 100), (0, 0, 0))
    # 18% inset at full strength -> the source pixel at (18, 0).
    for dx in range(-2, 3):
        for dy in range(0, 3):
            image.putpixel((18 + dx, dy), (250, 40, 40))
    out = develop.apply_geometry(image, {"keystone": 1.0})
    r, _g, _b = out.getpixel((0, 0))
    assert r > 150, "top-left must now show what sat at the inset"


def test_keystone_negative_works_on_the_bottom() -> None:
    image = Image.new("RGB", (100, 100), (0, 0, 0))
    for dx in range(-2, 3):
        for dy in range(97, 100):
            image.putpixel((18 + dx, dy), (40, 250, 40))
    out = develop.apply_geometry(image, {"keystone": -1.0})
    assert out.getpixel((0, 99))[1] > 150


def test_geometry_zero_is_identity() -> None:
    image = _gray(90)
    assert develop.apply_geometry(image, {"rotate": 0, "keystone": 0}) is image


def test_window_pull_darkens_the_bright_region_and_keeps_its_contrast() -> None:
    """A bright 'window' should come down as a region while the detail
    inside it keeps separating — that is the difference from a plain
    highlights pull, and the reason the slider exists."""
    size = 96
    image = Image.new("RGB", (size, size), (70, 70, 70))
    # A window: a bright block with internal detail (two tones).
    for y in range(8, 40):
        for x in range(8, 88):
            v = 250 if (x // 8) % 2 == 0 else 225
            image.putpixel((x, y), (v, v, v))

    out = develop.apply_recipe(image, {"window_pull": 1.0})
    bright_a = out.getpixel((12, 20))[0]
    bright_b = out.getpixel((20, 20))[0]
    wall = out.getpixel((48, 70))[0]
    assert bright_a < 220, "the window came down"
    assert abs(wall - 70) <= 8, "the dim wall stayed put"
    assert abs(bright_a - bright_b) >= 8, "detail inside the window survives"


def test_window_pull_zero_changes_nothing() -> None:
    image = _gray(180)
    out = develop.apply_recipe(image, {"window_pull": 0.0})
    assert out is image


# --------------------------------------------------------------- inpaint


def test_crop_box_squares_up_with_margin_and_clamps() -> None:
    import numpy as np

    from framefound.ai import inpaint

    mask = np.zeros((1000, 1500), dtype="float32")
    mask[100:160, 200:280] = 1.0  # an 80x60 object
    left, top, right, bottom = inpaint.crop_box(mask, 1500, 1000)
    assert left <= 200 and right >= 280 and top <= 100 and bottom >= 160
    assert (right - left) == (bottom - top), "square, as the model wants"
    assert right - left >= 80 + 2 * inpaint.MIN_MARGIN, "context floor holds"

    corner = np.zeros((1000, 1500), dtype="float32")
    corner[0:40, 0:40] = 1.0
    box = inpaint.crop_box(corner, 1500, 1000)
    assert box[0] >= 0 and box[1] >= 0, "clamped to the frame"


def test_crop_box_refuses_an_empty_mask() -> None:
    import numpy as np
    import pytest as _pytest

    from framefound.ai import inpaint

    with _pytest.raises(ValueError):
        inpaint.crop_box(np.zeros((100, 100), dtype="float32"), 100, 100)


def test_remove_region_touches_only_the_hole() -> None:
    """The paste-through-mask property: unmasked pixels inside the crop box
    must come back byte-identical, not softened by a 512 round-trip."""
    import numpy as np

    from framefound.ai import inpaint

    image = Image.new("RGB", (800, 600), (90, 120, 90))
    for x in range(0, 800, 7):  # texture, so degradation would show
        for y in range(0, 600, 7):
            image.putpixel((x, y), (200, 40, 40))
    mask = np.zeros((600, 800), dtype="float32")
    mask[250:310, 350:430] = 1.0

    green_fill = lambda img512, hole: np.where(  # noqa: E731
        hole[..., None] > 0.5, np.array([0.1, 0.9, 0.1], dtype="float32"), img512
    )
    out = inpaint.remove_region(image, mask, run_model=green_fill)

    assert out.getpixel((390, 280))[1] > 180, "the hole was filled"
    assert out.getpixel((10, 10)) == image.getpixel((10, 10)), "far pixels untouched"
    # Inside the crop box but outside the (dilated, feathered) hole:
    assert out.getpixel((350 - 60, 280)) == image.getpixel((350 - 60, 280)), (
        "context inside the box survives byte-for-byte"
    )


async def test_inpaint_request_queue_and_guards(env: dict) -> None:
    import base64

    client = env["client"]
    asset_id = env["ids"]["asset"]

    def mask_png(fraction_white: float) -> str:
        img = Image.new("L", (100, 100), 0)
        rows = int(100 * fraction_white)
        if rows:
            img.paste(255, (0, 0, 100, rows))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return base64.b64encode(buf.getvalue()).decode()

    resp = await client.post(
        f"/api/v1/develop/{asset_id}/inpaint", json={"mask_png": mask_png(0.0)}
    )
    assert resp.status_code == 400, "an empty mask removes nothing"

    resp = await client.post(
        f"/api/v1/develop/{asset_id}/inpaint", json={"mask_png": mask_png(0.8)}
    )
    assert resp.status_code == 400, "this removes objects, it does not repaint scenes"

    resp = await client.post(
        f"/api/v1/develop/{asset_id}/inpaint", json={"mask_png": mask_png(0.1)}
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["busy"] is True

    resp = await client.post(
        f"/api/v1/develop/{asset_id}/inpaint", json={"mask_png": mask_png(0.1)}
    )
    assert resp.status_code == 409, "one at a time; rounds chain on each other"


async def test_inpaint_task_runs_and_export_uses_the_result(
    env: dict, monkeypatch: __import__("pytest").MonkeyPatch
) -> None:
    """The full chain with the model stubbed: queue, run, base switch, undo."""
    import base64

    import numpy as np

    from framefound.ai import inpaint as inpaint_lib
    from framefound.db.models import AssetInpaint
    from framefound.processing import tasks as tasks_module

    client = env["client"]
    asset_id = env["ids"]["asset"]

    monkeypatch.setattr(
        inpaint_lib,
        "_run_lama",
        lambda img512, hole: np.where(
            hole[..., None] > 0.5, np.array([0.05, 0.9, 0.05], dtype="float32"), img512
        ),
    )

    mask = Image.new("L", (200, 150), 0)
    mask.paste(255, (80, 50, 130, 100))
    buf = io.BytesIO()
    mask.save(buf, "PNG")
    resp = await client.post(
        f"/api/v1/develop/{asset_id}/inpaint",
        json={"mask_png": base64.b64encode(buf.getvalue()).decode()},
    )
    assert resp.status_code == 202

    async with env["factory"]() as db:
        row = (
            await db.execute(
                select(AssetInpaint).where(AssetInpaint.asset_id == uuidlib.UUID(asset_id))
            )
        ).scalar_one()
        inpaint_id = str(row.id)

    await asyncio.to_thread(tasks_module.inpaint_asset, inpaint_id)

    state = (await client.get(f"/api/v1/develop/{asset_id}/inpaint")).json()
    assert state["versions"][-1]["status"] == "ready", state
    result_path = get_settings().data_dir / f"inpaint/{asset_id}/v1.jpg"
    assert result_path.is_file()
    with Image.open(result_path) as done:
        r, g, b = done.getpixel((105, 75))
        assert g > 150 and r < 100, "the marked region was filled by the model stub"

    # The export must start from the inpainted base.
    listing = (
        await client.post("/api/v1/listings", json={"name": "Inpainted", "asset_ids": [asset_id]})
    ).json()
    await asyncio.to_thread(tasks_module.export_listing_zip, listing["id"], 2048, 85)
    async with env["factory"]() as db:
        lrow = await db.get(Listing, uuidlib.UUID(listing["id"]))
        assert lrow is not None and lrow.export_status == "ready"
        zip_path = get_settings().data_dir / str(lrow.export_relpath)
    with (
        zipfile.ZipFile(zip_path) as archive,
        Image.open(io.BytesIO(archive.read(archive.namelist()[0]))) as exported,
    ):
        # Export upscales nothing (source 200x150), so coordinates map 1:1.
        r, g, b = exported.getpixel((105, 75))
        assert g > 140 and r < 110, "the zip contains the removal"

    # Undo: newest only, file goes with it.
    resp = await client.delete(f"/api/v1/develop/{asset_id}/inpaint/1")
    assert resp.status_code == 200
    assert not result_path.exists(), "undo deletes the invented pixels"


def test_sky_matte_keeps_dark_branches() -> None:
    """Segmentation at 512 cannot resolve twigs; their darkness gives them
    away. A dark branch inside the matte must survive the swap instead of
    being painted over with sky."""
    import numpy as np

    from framefound.media.sky import composite_sky

    size = 96
    image = Image.new("RGB", (size, size), (60, 50, 40))
    for y in range(size // 2):  # bright sky above
        for x in range(size):
            image.putpixel((x, y), (210, 215, 225))
    for x in range(20, 76):  # a dark branch crossing the sky
        for y in range(20, 24):
            image.putpixel((x, y), (35, 30, 25))
    mask = np.zeros((size, size), dtype="float32")
    mask[: size // 2] = 1.0  # segmentation says: all of it is sky

    red = Image.new("RGB", (size, size), (240, 40, 40))
    out = composite_sky(image, mask, red, feather=0.0, relight=0.0)
    r, g, b = out.getpixel((48, 22))
    assert r < 120, "the branch stayed a branch"
    sky_r = out.getpixel((48, 8))[0]
    assert sky_r > 180, "open sky was still replaced"
