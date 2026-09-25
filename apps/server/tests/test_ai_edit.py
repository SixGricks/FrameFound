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


def test_auto_wb_does_not_read_a_lawn_as_a_green_cast() -> None:
    """Lawn fills the mid-bright band of a front exterior. Measured as a
    cast, its correction turned the overcast sky purple and the house
    magenta (Davis Rd, in the Fotello bake-off)."""
    scene = Image.new("RGB", (64, 64), (205, 208, 212))  # overcast sky
    scene.paste((90, 150, 50), (0, 24, 64, 64))  # lawn, most of the frame
    out = develop.apply_recipe(scene, {"auto_wb": 1.0})
    r, g, b = out.getpixel((32, 8))
    assert abs(r - g) <= 6 and b >= r, f"the grey sky stays grey, got {(r, g, b)}"
    assert out.getpixel((32, 50))[1] >= 145, "the grass stays green"


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
                            "caption": "Kitchen island with\npendant lights",
                            "seo_slug": "Kitchen Island / 130 Pendants",
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
    assert "caption" not in result["recipe"] and "seo_slug" not in result["recipe"]
    assert result["needs_sky_replacement"] is True
    # Named in the same call, and cleaned: one line, and a slug with no path
    # characters and no house number.
    assert result["caption"] == "Kitchen island with pendant lights"
    assert result["slug"] == "kitchen-island-pendants"
    schema = captured["body"]["tools"][0]["input_schema"]
    assert {"caption", "seo_slug"} <= set(schema["required"])

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


def test_thinking_models_get_the_tool_offered_not_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opus 5.5 and Fable 5.1 reject a forced tool_choice with a 400, and
    think before answering: auto choice, strict schema, low effort, and room
    in max_tokens for the thinking. Sonnet keeps the forced call."""
    import httpx

    from framefound.ai import recipe_picker

    bodies: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ANN001
        bodies.append(json)
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "thinking": "", "signature": "x"},
                    {
                        "type": "tool_use",
                        "name": "set_develop_recipe",
                        "input": {"exposure": 0.3, "caption": "Den", "seo_slug": "den"},
                    },
                ],
                "usage": {"input_tokens": 1900, "output_tokens": 640},
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    picked = recipe_picker.pick_recipe(b"\xff\xd8fake", "k", "claude-opus-5-5")
    assert picked["recipe"] == {"exposure": 0.3}, "the tool_use block is found past the thinking"
    assert picked["usage"]["output_tokens"] == 640
    opus = bodies[-1]
    assert opus["tool_choice"] == {"type": "auto"}
    assert opus["tools"][0]["strict"] is True
    assert opus["output_config"] == {"effort": "low"}
    assert opus["max_tokens"] >= 4096

    recipe_picker.pick_recipe(b"\xff\xd8fake", "k", "claude-sonnet-5")
    sonnet = bodies[-1]
    assert sonnet["tool_choice"] == {"type": "tool", "name": "set_develop_recipe"}
    assert "output_config" not in sonnet and "strict" not in sonnet["tools"][0]


def test_describe_photo_names_without_asking_for_sliders(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from framefound.ai import recipe_picker

    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: ANN001
        captured["body"] = json
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "name": "describe_photo",
                        "input": {
                            "caption": "Aerial of the horse barn and paddocks",
                            "seo_slug": "aerial-horse-barn",
                        },
                    }
                ]
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    named = recipe_picker.describe_photo(b"\xff\xd8fake", "sk-ant-test", "claude-sonnet-5")
    assert named["caption"] == "Aerial of the horse barn and paddocks"
    assert named["slug"] == "aerial-horse-barn"
    assert named["usage"]["input_tokens"] == 0, "absent usage reads as zero, not a crash"
    assert captured["body"]["tool_choice"] == {"type": "tool", "name": "describe_photo"}
    tool = captured["body"]["tools"][0]
    assert "exposure" not in tool["input_schema"]["properties"], "naming only; no sliders"


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


async def test_one_failing_photo_is_skipped_not_fatal(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run's promise is "40 edited and 2 skipped beats 0 edited and an
    exception". It used to break on the first failure: the rollback expired
    every ORM object in the session, and the next attribute read from async
    code raised — ending the run instead of skipping one photograph."""
    from framefound.ai import recipe_picker
    from framefound.processing import tasks as tasks_module

    client = env["client"]
    await client.put("/api/v1/develop/settings/ai", json={"api_key": "sk-ant-test"})
    listing = (
        await client.post(
            "/api/v1/listings",
            json={"name": "Flaky", "asset_ids": [env["ids"]["a1"], env["ids"]["a2"]]},
        )
    ).json()

    calls = {"n": 0}

    def overloaded_once(preview: bytes, key: str, model: str) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise recipe_picker.RecipePickUnavailable("Anthropic API returned 529")
        return {"recipe": {"exposure": 0.3}, "needs_sky_replacement": False, "notes": ""}

    monkeypatch.setattr(recipe_picker, "pick_recipe", overloaded_once)
    await asyncio.to_thread(tasks_module.ai_edit_listing, listing["id"], None, "ai")

    async with env["factory"]() as db:
        edited = set((await db.execute(select(AssetEdit.asset_id))).scalars())
    assert calls["n"] == 2, "the run carried on past the failure"
    assert len(edited) == 1, "the failed photograph was skipped, the other edited"


async def test_auto_edit_judges_the_object_removed_version(
    env: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Export renders the newest removal result, so that is what the model
    must see — not the original with the removed object still in it."""
    import io as io_module

    import numpy as np

    from framefound.ai import recipe_picker
    from framefound.db.models import AssetInpaint
    from framefound.processing import tasks as tasks_module

    client = env["client"]
    await client.put("/api/v1/develop/settings/ai", json={"api_key": "sk-ant-test"})
    asset_id = uuidlib.UUID(env["ids"]["a1"])
    relpath = f"inpaint/{asset_id}/v1.jpg"
    (tmp_path / "data" / "inpaint" / str(asset_id)).mkdir(parents=True)
    Image.new("RGB", (160, 120), (20, 40, 220)).save(tmp_path / "data" / relpath, "JPEG")
    async with env["factory"]() as db:
        db.add(AssetInpaint(asset_id=asset_id, version=1, status="ready", relative_path=relpath))
        await db.commit()
    listing = (
        await client.post("/api/v1/listings", json={"name": "R", "asset_ids": [str(asset_id)]})
    ).json()

    seen: list[bytes] = []

    def capture(preview: bytes, key: str, model: str) -> dict:
        seen.append(preview)
        return {"recipe": {}, "needs_sky_replacement": False, "notes": ""}

    monkeypatch.setattr(recipe_picker, "pick_recipe", capture)
    await asyncio.to_thread(tasks_module.ai_edit_listing, listing["id"], None, "ai")

    with Image.open(io_module.BytesIO(seen[0])) as judged:
        red, _green, blue = np.asarray(judged.convert("RGB"), dtype=float).mean(axis=(0, 1))
    assert blue > 150 and red < 80, "the model saw the removal result, not the original"


# ------------------------------------------------------------ learned look


async def test_an_installed_look_sets_the_tone_and_the_model_straightens(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bake-off's finding, wired in: tone from the learned look (closer
    to what shipped than any model), straightening and naming from Claude."""
    from test_looks import _install

    from framefound.ai import recipe_picker
    from framefound.processing import tasks as tasks_module

    _install(get_settings().data_dir, {"exposure": 0.33, "vibrance": 0.21})
    client = env["client"]
    await client.put("/api/v1/develop/settings/ai", json={"api_key": "sk-ant-test"})
    assert (await client.get("/api/v1/develop/settings/ai")).json()["look_examples"] == 12
    listing = (
        await client.post(
            "/api/v1/listings", json={"name": "Look", "asset_ids": [env["ids"]["a1"]]}
        )
    ).json()
    resp = await client.post(f"/api/v1/listings/{listing['id']}/ai-edit")
    assert resp.json()["look"] == 12

    monkeypatch.setattr(
        recipe_picker,
        "pick_recipe",
        lambda preview, key, model: {
            "recipe": {"exposure": 1.8, "rotate": -1.5},
            "needs_sky_replacement": False,
            "notes": "",
            "caption": "Front",
            "slug": "front",
        },
    )
    await asyncio.to_thread(tasks_module.ai_edit_listing, listing["id"], None, "ai")
    async with env["factory"]() as db:
        edit = (
            await db.execute(
                select(AssetEdit).where(AssetEdit.asset_id == uuidlib.UUID(env["ids"]["a1"]))
            )
        ).scalar_one()
    assert edit.recipe["exposure"] == 0.33, "tone from the look, not the model's 1.8"
    assert edit.recipe["vibrance"] == 0.21
    assert edit.recipe["rotate"] == -1.5, "the model's straightening kept"


# ----------------------------------------------------------------- naming


async def _named_listing(env: dict, name: str) -> dict:
    client = env["client"]
    await client.put("/api/v1/develop/settings/ai", json={"api_key": "sk-ant-test"})
    return (
        await client.post(
            "/api/v1/listings",
            json={"name": name, "asset_ids": [env["ids"]["a1"], env["ids"]["a2"]]},
        )
    ).json()


async def test_auto_edit_names_photos_but_never_overwrites_the_operator(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same contract as room labels: the AI suggests, typing confirms, and a
    confirmed name survives every later run."""
    from framefound.ai import recipe_picker
    from framefound.processing import tasks as tasks_module

    client = env["client"]
    listing = await _named_listing(env, "Naming")
    url = f"/api/v1/listings/{listing['id']}"
    resp = await client.put(
        f"{url}/items/{env['ids']['a1']}/naming",
        json={"caption": "Bank barn, south side", "slug": "Bank Barn South"},
    )
    assert resp.status_code == 200, resp.text

    monkeypatch.setattr(
        recipe_picker,
        "pick_recipe",
        lambda preview, key, model: {
            "recipe": {"exposure": 0.3},
            "needs_sky_replacement": False,
            "notes": "",
            "caption": "Front exterior, straight-on",
            "slug": "front-exterior",
        },
    )
    await asyncio.to_thread(tasks_module.ai_edit_listing, listing["id"], None, "ai")

    items = {i["asset_id"]: i for i in (await client.get(url)).json()["items"]}
    mine, theirs = items[env["ids"]["a1"]], items[env["ids"]["a2"]]
    assert (mine["caption"], mine["slug"], mine["naming_source"]) == (
        "Bank barn, south side",
        "bank-barn-south",
        "confirmed",
    )
    assert (theirs["caption"], theirs["slug"], theirs["naming_source"]) == (
        "Front exterior, straight-on",
        "front-exterior",
        "suggested",
    )
    assert theirs["named_at"] is not None
    assert (
        theirs["export_name"].startswith("0") and "front-exterior-naming" in (theirs["export_name"])
    ), "the slug and the listing's suffix make the file name"


async def test_describe_mode_names_without_editing_and_skips_confirmed(
    env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.ai import recipe_picker
    from framefound.processing import tasks as tasks_module

    client = env["client"]
    listing = await _named_listing(env, "Describe")
    url = f"/api/v1/listings/{listing['id']}"
    await client.put(f"{url}/items/{env['ids']['a1']}/naming", json={"caption": "Mine"})

    resp = await client.post(f"{url}/ai-edit", json={"mode": "describe"})
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert (body["queued"], body["mode"]) == (1, "describe"), "the confirmed photo is not counted"

    calls: list[bytes] = []

    def describe(preview: bytes, key: str, model: str) -> dict:
        calls.append(preview)
        return {"caption": "Primary bedroom with tray ceiling", "slug": "primary-bedroom"}

    monkeypatch.setattr(recipe_picker, "describe_photo", describe)
    monkeypatch.setattr(
        recipe_picker, "pick_recipe", lambda *a: pytest.fail("describe must not edit")
    )
    await asyncio.to_thread(tasks_module.ai_edit_listing, listing["id"], None, "describe")

    assert len(calls) == 1, "one paid call, for the one photo not already named"
    items = {i["asset_id"]: i for i in (await client.get(url)).json()["items"]}
    assert items[env["ids"]["a1"]]["caption"] == "Mine"
    assert items[env["ids"]["a2"]]["slug"] == "primary-bedroom"
    async with env["factory"]() as db:
        assert not (await db.execute(select(AssetEdit))).scalars().all(), "nothing edited"


async def test_describe_mode_needs_a_key(env: dict) -> None:
    listing = (
        await env["client"].post(
            "/api/v1/listings", json={"name": "K", "asset_ids": [env["ids"]["a1"]]}
        )
    ).json()
    resp = await env["client"].post(
        f"/api/v1/listings/{listing['id']}/ai-edit", json={"mode": "describe"}
    )
    assert resp.status_code == 400
    assert "Anthropic key" in resp.json()["error"]["message"]


async def test_clearing_a_name_hands_the_photo_back_to_the_ai(env: dict) -> None:
    listing = await _named_listing(env, "Clear")
    url = f"/api/v1/listings/{listing['id']}/items/{env['ids']['a1']}/naming"
    await env["client"].put(url, json={"caption": "Something"})
    items = (await env["client"].put(url, json={"caption": "  ", "slug": ""})).json()["items"]
    item = next(i for i in items if i["asset_id"] == env["ids"]["a1"])
    assert (item["caption"], item["naming_source"]) == ("", "")


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


def test_duplicate_grouping_matches_the_pairwise_definition() -> None:
    """The matrix version must group exactly as comparing every pair did."""
    import random

    from framefound.media import curate

    rng = random.Random(11)
    centres = [[rng.gauss(0, 1) for _ in range(24)] for _ in range(12)]
    embeddings: dict[str, list[float]] = {}
    for i in range(90):
        v = [x + rng.gauss(0, 0.2) for x in rng.choice(centres)]
        norm = sum(x * x for x in v) ** 0.5
        embeddings[f"p{i}"] = [x / norm for x in v]

    ids = list(embeddings)
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            x = parent[x]
        return x

    for a_index, a in enumerate(ids):
        for b in ids[a_index + 1 :]:
            dot = sum(x * y for x, y in zip(embeddings[a], embeddings[b], strict=True))
            if dot >= curate.DUPLICATE_SIMILARITY:
                parent[find(a)] = find(b)
    expected: dict[str, list[str]] = {}
    for item in ids:
        expected.setdefault(find(item), []).append(item)

    got = curate.group_duplicates(embeddings)
    assert sorted(got) == sorted(g for g in expected.values() if len(g) > 1)
    assert got, "the fixture does contain near-duplicates"
