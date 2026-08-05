"""Folder import, the new engine ops, and the AI recipe-picker.

The picker tests never talk to Anthropic: httpx is given a mock transport,
because what needs proving is ours — the request shape, the tool-result
parsing, the clamping — not their API.
"""

import asyncio
import uuid as uuidlib
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
from framefound.db.models import Asset, AssetEdit, Frame, Library
from framefound.media import develop

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


# ------------------------------------------------------------- engine ops


def test_auto_wb_neutralises_a_warm_cast() -> None:
    """A warm interior cast (the camera-JPEG default) must come out neutral
    at full strength — this is the single biggest gap to an MLS final."""
    warm = Image.new("RGB", (64, 64), (200, 180, 140))
    out = develop.apply_recipe(warm, {"auto_wb": 1.0})
    r, g, b = out.getpixel((32, 32))
    assert abs(r - b) < 12, f"cast removed (was 60 apart, now {abs(r - b)})"


def test_auto_wb_zero_changes_nothing() -> None:
    warm = Image.new("RGB", (32, 32), (200, 180, 140))
    assert develop.apply_recipe(warm, {"auto_wb": 0.0}) is warm


def test_auto_wb_is_bounded_on_a_legitimately_warm_scene() -> None:
    """A sunset is not a cast. Gains clamp at 1.4x, so even full strength
    cannot bleach a photograph that is genuinely one colour."""
    sunset = Image.new("RGB", (64, 64), (240, 120, 40))
    out = develop.apply_recipe(sunset, {"auto_wb": 1.0})
    r, _g, b = out.getpixel((32, 32))
    assert r > b + 60, "still recognisably warm"


def test_local_contrast_separates_regions_not_flats() -> None:
    image = Image.new("RGB", (96, 96), (110, 110, 110))
    for y in range(96):
        for x in range(48):
            image.putpixel((x, y), (80, 80, 80))  # left half darker
    out = develop.apply_recipe(image, {"local_contrast": 1.0})
    # The push is strongest near the region boundary (it is an unsharp at
    # ~6px radius on this size), so measure there; the far corners stay put.
    near_left = out.getpixel((42, 48))[0]
    near_right = out.getpixel((54, 48))[0]
    assert (near_right - near_left) > (110 - 80), "separation increases at the boundary"
    assert abs(out.getpixel((4, 4))[0] - 80) <= 4, "distant flats barely move"


# --------------------------------------------------------- recipe picker


def test_pick_recipe_parses_and_clamps(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from framefound.ai import recipe_picker

    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ANN001
        captured["url"] = url
        captured["body"] = json
        captured["headers"] = headers
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "name": "set_develop_recipe",
                        "input": {
                            "auto_wb": 0.8,
                            "exposure": 5.0,  # out of range: must clamp to 2.0
                            "shadows": 0.3,
                            "sharpen": 1,  # off-schema: must drop
                            "needs_sky_replacement": True,
                            "notes": "Neutralised warm cast, lifted shadows.",
                        },
                    }
                ]
            },
        )

    # pick_recipe imports httpx at call time, so patching the module works.
    monkeypatch.setattr(httpx, "post", fake_post)

    result = recipe_picker.pick_recipe(b"\xff\xd8fake", "sk-ant-test", "claude-sonnet-5")
    assert result["recipe"]["auto_wb"] == 0.8
    assert result["recipe"]["exposure"] == 2.0, "clamped, not trusted"
    assert "sharpen" not in result["recipe"], "off-schema keys dropped"
    assert result["needs_sky_replacement"] is True

    assert captured["headers"]["x-api-key"] == "sk-ant-test"
    assert captured["body"]["model"] == "claude-sonnet-5"
    assert captured["body"]["tool_choice"] == {"type": "tool", "name": "set_develop_recipe"}
    image_block = captured["body"]["messages"][0]["content"][0]
    assert image_block["type"] == "image", "the preview travels as an image block"


def test_pick_recipe_surfaces_api_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from framefound.ai import recipe_picker

    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(429, json={"error": "rate"}))
    with pytest.raises(recipe_picker.RecipePickUnavailable):
        recipe_picker.pick_recipe(b"x", "k", "m")


# ------------------------------------------------------------------ API


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'aiedit.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "ai-edit-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    get_settings.cache_clear()

    from framefound.processing import tasks as tasks_module

    monkeypatch.setattr(tasks_module.ai_edit_listing, "delay", lambda *args: None)

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
    (root / "00-00 5096 Old Philadelphia Pike Kinzers").mkdir(parents=True)
    (root / "00-00 5096 Old Philadelphia Pike Kinzers" / "RAW").mkdir()
    (root / "misc").mkdir()
    ids: dict[str, str] = {}
    async with factory() as db:
        library = Library(name="NAS", root_path=str(root))
        db.add(library)
        await db.flush()
        ids["library"] = str(library.id)

        async def add(relpath: str) -> str:
            name = relpath.rsplit("/", 1)[-1]
            Image.new("RGB", (160, 120), (200, 180, 140)).save(root / relpath, "JPEG")
            asset = Asset(
                library_id=library.id,
                relative_path=relpath,
                filename=name,
                extension="jpg",
                media_type="image",
                size_bytes=1000,
                mtime=BASE,
                availability="online",
            )
            db.add(asset)
            await db.flush()
            db.add(Frame(asset_id=asset.id, ts_ms=0, relative_path=f"f/{name}.jpeg"))
            return str(asset.id)

        ids["a1"] = await add("00-00 5096 Old Philadelphia Pike Kinzers/IMG_0001.jpg")
        ids["a2"] = await add("00-00 5096 Old Philadelphia Pike Kinzers/IMG_0002.jpg")
        ids["sub"] = await add("00-00 5096 Old Philadelphia Pike Kinzers/RAW/IMG_0001.jpg")
        ids["other"] = await add("misc/holiday.jpg")
        await db.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        assert (
            await client.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"client": client, "ids": ids, "factory": factory}
    await engine.dispose()
    get_settings.cache_clear()


async def test_folder_search_finds_the_shoot_by_address(env: dict) -> None:
    found = (await env["client"].get("/api/v1/listings/folders?q=5096")).json()
    paths = {f["path"] for f in found}
    assert "00-00 5096 Old Philadelphia Pike Kinzers" in paths
    assert "00-00 5096 Old Philadelphia Pike Kinzers/RAW" in paths
    top = next(f for f in found if f["path"] == "00-00 5096 Old Philadelphia Pike Kinzers")
    assert top["image_count"] == 2, "direct children only; the RAW subfolder counts itself"


async def test_folder_assets_lists_direct_children_only(env: dict) -> None:
    ids = env["ids"]
    listed = (
        await env["client"].get(
            "/api/v1/listings/folders/assets"
            f"?library_id={ids['library']}&path=00-00 5096 Old Philadelphia Pike Kinzers"
        )
    ).json()
    got = {a["asset_id"] for a in listed}
    assert got == {ids["a1"], ids["a2"]}, "the RAW/ subfolder is a different delivery"


async def test_ai_settings_seal_and_report_presence_only(env: dict) -> None:
    client = env["client"]
    state = (await client.get("/api/v1/develop/settings/ai")).json()
    assert state["configured"] is False

    resp = await client.put("/api/v1/develop/settings/ai", json={"api_key": "sk-ant-verysecret"})
    assert resp.status_code == 200
    assert resp.json()["configured"] is True
    assert "sk-ant" not in resp.text.replace("configured", ""), "the key itself never returns"

    async with env["factory"]() as db:
        from framefound.db.models import AppSetting

        row = await db.get(AppSetting, "ai_edit")
        assert row is not None
        assert "sk-ant-verysecret" not in str(row.value), "sealed at rest, not plaintext"

    cleared = (await client.put("/api/v1/develop/settings/ai", json={"api_key": ""})).json()
    assert cleared["configured"] is False


async def test_ai_edit_without_a_key_falls_back_to_the_preset(env: dict) -> None:
    """The flow must work on day one, key or no key — the preset mode is the
    whole point of tuning against the published listings."""
    listing = (
        await env["client"].post(
            "/api/v1/listings", json={"name": "L", "asset_ids": [env["ids"]["a1"]]}
        )
    ).json()
    resp = await env["client"].post(f"/api/v1/listings/{listing['id']}/ai-edit", json={})
    assert resp.status_code == 202, resp.text
    assert resp.json()["mode"] == "preset"


async def test_preset_mode_applies_the_tuned_recipe_and_chosen_sky(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.ai import skyseg
    from framefound.processing import tasks as tasks_module

    client = env["client"]
    listing = (
        await client.post("/api/v1/listings", json={"name": "P", "asset_ids": [env["ids"]["a1"]]})
    ).json()

    # Segmentation says: plenty of sky.
    monkeypatch.setattr(skyseg, "sky_fraction", lambda image: 0.3)
    await asyncio.to_thread(tasks_module.ai_edit_listing, listing["id"], "dusk.jpg", "preset")

    async with env["factory"]() as db:
        edit = (
            await db.execute(
                select(AssetEdit).where(AssetEdit.asset_id == uuidlib.UUID(env["ids"]["a1"]))
            )
        ).scalar_one()
        assert edit.recipe["auto_wb"] == 1.0, "the tuned preset landed"
        assert edit.recipe["sky"]["name"] == "dusk.jpg", "the operator's sky rode along"


async def test_preset_mode_skips_the_sky_on_interiors(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One choice must be safe across a whole shoot: no sky detected, no
    sky composited."""
    from framefound.ai import skyseg
    from framefound.processing import tasks as tasks_module

    client = env["client"]
    listing = (
        await client.post("/api/v1/listings", json={"name": "I", "asset_ids": [env["ids"]["a2"]]})
    ).json()
    monkeypatch.setattr(skyseg, "sky_fraction", lambda image: 0.0)
    await asyncio.to_thread(tasks_module.ai_edit_listing, listing["id"], "dusk.jpg", "preset")

    async with env["factory"]() as db:
        edit = (
            await db.execute(
                select(AssetEdit).where(AssetEdit.asset_id == uuidlib.UUID(env["ids"]["a2"]))
            )
        ).scalar_one()
        assert "sky" not in edit.recipe, "an interior stays an interior"


async def test_ai_edit_task_writes_a_recipe_per_photo(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.ai import recipe_picker
    from framefound.processing import tasks as tasks_module

    client = env["client"]
    await client.put("/api/v1/develop/settings/ai", json={"api_key": "sk-ant-test"})
    listing = (
        await client.post(
            "/api/v1/listings",
            json={"name": "AI", "asset_ids": [env["ids"]["a1"], env["ids"]["a2"]]},
        )
    ).json()
    assert (await client.post(f"/api/v1/listings/{listing['id']}/ai-edit")).status_code == 202

    monkeypatch.setattr(
        recipe_picker,
        "pick_recipe",
        lambda preview, key, model: {
            "recipe": {"auto_wb": 0.9, "exposure": 0.4, "shadows": 0.3},
            "needs_sky_replacement": False,
            "notes": "test",
        },
    )
    await asyncio.to_thread(tasks_module.ai_edit_listing, listing["id"], None, "ai")

    async with env["factory"]() as db:
        edits = (
            (
                await db.execute(
                    select(AssetEdit).where(
                        AssetEdit.asset_id.in_(
                            [uuidlib.UUID(env["ids"]["a1"]), uuidlib.UUID(env["ids"]["a2"])]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(edits) == 2, "one recipe per photograph"
        assert all(e.recipe["auto_wb"] == 0.9 for e in edits)

    detail = (await client.get(f"/api/v1/listings/{listing['id']}")).json()
    assert all(item["edited"] for item in detail["items"])


# --------------------------------------------------------------- curation


def test_suggest_removals_keeps_the_sharpest_of_a_duplicate_group() -> None:
    from framefound.media import curate

    vec = [0.0] * 512
    vec[0] = 1.0
    near = [0.0] * 512
    near[0] = 0.999
    near[1] = 0.04

    items = [
        {"id": "a", "room": "kitchen", "sharpness": 9.0, "embedding": vec},
        {"id": "b", "room": "kitchen", "sharpness": 4.0, "embedding": near},
        {"id": "c", "room": "kitchen", "sharpness": 8.0, "embedding": None},
    ]
    out = curate.suggest_removals(items)
    assert [s["id"] for s in out] == ["b"], "the softer twin goes, the sharp one stays"
    assert out[0]["keep_instead"] == "a"


def test_suggest_removals_never_empties_a_room() -> None:
    from framefound.media import curate

    blurry_barn = {"id": "barn1", "room": "barn", "sharpness": 0.5, "embedding": None}
    sharp_kitchens = [
        {"id": f"k{i}", "room": "kitchen", "sharpness": 10.0, "embedding": None} for i in range(4)
    ]
    out = curate.suggest_removals([blurry_barn, *sharp_kitchens])
    assert all(s["id"] != "barn1" for s in out), (
        "a blurry photo of the only barn is still the only barn"
    )


def test_sharpness_orders_blur_correctly() -> None:
    from PIL import ImageFilter

    from framefound.media import curate

    detailed = Image.new("RGB", (128, 128))
    for x in range(128):
        for y in range(128):
            detailed.putpixel((x, y), ((x * 7 + y * 13) % 256,) * 3)
    blurred = detailed.filter(ImageFilter.GaussianBlur(4))
    assert curate.sharpness(detailed) > curate.sharpness(blurred) * 2
