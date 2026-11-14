"""A clip's colour look through the API (design L4 section 6, step S5).

Three looks on the same still, always in the same order; every picture read
back through the table the file would hold; a choice only ever made by a
person; a `.cube` that reads back as the table it was written from; and a
folder's chosen looks in one zip. Nothing here asks a model: a suggestion is
a row put in the table by hand, as the lane's job writes it (asking is
tested in test_colour_jobs.py).

No ffmpeg runs (the stills are drawn files) and no network is touched.
"""

import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.api.v1 import clip_looks as api
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, ClipLook, Library, ShotWindow
from framefound.media import clip_look, compare, develop, looks, lut
from framefound.processing import shots

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)
FOLDER = "Brookside Country Club/2026/Drone"
# (start, end) of the clip's three shots: the middle one is the longest, so
# its middle (9 s) is the still every look is measured on.
WINDOWS = ((0, 4000), (4000, 14000), (14000, 20000))
REFERENCE = 9000
MIDDLES = (2000, 9000, 17000)
# Provenance of the suggestion rows these tests write, as the job will.
MADE_BY = {
    "model": "mlx-community/gemma-4-e4b-it-4bit",
    "model_revision": "475b9088d29754a3379866cf5aeb6b41acd313c2",
    "runtime": "mlx-vlm",
    "runtime_version": "0.7.12",
    "prompt_version": "colour-1",
}
SUGGESTED = {"auto_wb": 0.6, "exposure": 0.25, "temperature": 0.08, "vibrance": 0.2}


def _scene(warmth: int) -> Image.Image:
    """A drawn still with a colour cast and a range of tones, so a white
    balance and a grade both have something to do."""
    y, x = np.mgrid[0:270, 0:480]
    arr = np.stack(
        [
            40 + x * 160 / 480 + warmth,
            50 + y * 150 / 270,
            90 + (x + y) * 60 / 750 - warmth,
        ],
        axis=-1,
    )
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8"), "RGB")


def _asset(library_id: uuid.UUID, relative: str, media_type: str = "video") -> Asset:
    return Asset(
        library_id=library_id,
        relative_path=relative,
        filename=relative.rsplit("/", 1)[-1],
        extension=relative.rsplit(".", 1)[-1].lower(),
        media_type=media_type,
        size_bytes=1,
        mtime=BASE,
        duration_s=20.0 if media_type == "video" else None,
        availability="online",
    )


@pytest.fixture(autouse=True)
def _forget_learned() -> Iterator[None]:
    api.clear_learned()
    looks._cache.clear()
    yield
    api.clear_learned()
    looks._cache.clear()


@pytest.fixture()
async def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """A library with one drone clip that has three shots and their stills,
    a clip beside it, a clip in a folder inside, and a photograph."""
    url = f"sqlite+aiosqlite:///{(tmp_path / 'clip-looks.db').as_posix()}"
    data = tmp_path / "data"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "clip-looks-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(data))
    monkeypatch.setenv("FRAMEFOUND_MEDIA_ROOT", str(tmp_path))
    monkeypatch.delenv("FRAMEFOUND_RUNTIMES_DIR", raising=False)
    monkeypatch.delenv("FRAMEFOUND_LOCAL_AI_DEMO", raising=False)
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
    async with factory() as db:
        library = Library(name="GELCO", root_path=str(tmp_path / "GELCO"))
        db.add(library)
        await db.flush()
        clip = _asset(library.id, f"{FOLDER}/DJI_0639.MP4")
        beside = _asset(library.id, f"{FOLDER}/DJI_0640.MP4")
        inside = _asset(library.id, f"{FOLDER}/Raw/DJI_0700.MP4")
        photo = _asset(library.id, f"{FOLDER}/IMG_0001.JPG", "image")
        db.add_all([clip, beside, inside, photo])
        await db.flush()
        for idx, (start, end) in enumerate(WINDOWS):
            db.add(ShotWindow(asset_id=clip.id, idx=idx, start_ms=start, end_ms=end, move="orbit"))
        await db.commit()
    stills = {}
    for warmth, ts in zip((30, 20, -10), MIDDLES, strict=True):
        stills[ts] = _still(data, clip.id, ts, warmth)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        assert (
            await http.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {
            "http": http,
            "factory": factory,
            "data": data,
            "library": library,
            "clip": clip,
            "beside": beside,
            "inside": inside,
            "photo": photo,
            "stills": stills,
        }
    await engine.dispose()
    get_settings.cache_clear()


def _still(data: Path, asset_id: uuid.UUID, ts: int, warmth: int) -> Image.Image:
    path = data / shots.still_relpath(asset_id, ts)
    path.parent.mkdir(parents=True, exist_ok=True)
    _scene(warmth).save(path, "JPEG", quality=95)
    with Image.open(path) as saved:
        return saved.convert("RGB")


async def _suggest(env: dict[str, Any], recipe: dict[str, float] | None = None) -> ClipLook:
    """A suggestion as the lane's job will store it: frozen on the reference."""
    recipe = dict(recipe or SUGGESTED)
    still = env["stills"][REFERENCE]
    async with env["factory"]() as db:
        row = ClipLook(
            asset_id=env["clip"].id,
            kind="suggested",
            source="model",
            recipe=recipe,
            measured=develop.measure(still, recipe),
            still_ts_ms=REFERENCE,
            features=clip_look.features_of(still),
            reason="Overcast and slightly blue; lifted and warmed.",
            demo=True,
            **MADE_BY,
        )
        db.add(row)
        await db.commit()
        return row


async def _get(env: dict[str, Any], asset: Asset | None = None) -> dict[str, Any]:
    response = await env["http"].get(f"/api/v1/clip-looks/{(asset or env['clip']).id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _by_source(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {candidate["source"]: candidate for candidate in body["candidates"]}


def _jpeg(picture: Image.Image) -> bytes:
    out = io.BytesIO()
    picture.convert("RGB").save(out, "JPEG", quality=api.JPEG_QUALITY)
    return out.getvalue()


def _install_photo_look(data: Path, recipe: dict[str, float], n: int = 12) -> None:
    feats = [compare.features(_scene(5 * i)) for i in range(n)]
    learned = compare.LearnedLook.fit(feats, [recipe] * n)
    path = data / looks.LOOK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "features": list(compare.FEATURE_NAMES),
                "mean": learned.mean.tolist(),
                "scale": learned.scale.tolist(),
                "examples": [
                    {"source": f"s/{i}", "features": [float(x) for x in f], "recipe": recipe}
                    for i, f in enumerate(feats)
                ],
            }
        )
    )


async def _other_clips_chosen(env: dict[str, Any], count: int, recipe: dict[str, float]) -> None:
    """`count` clips elsewhere in the library, each with a chosen look."""
    async with env["factory"]() as db:
        for i in range(count):
            other = _asset(env["library"].id, f"Elsewhere/clip_{uuid.uuid4().hex[:8]}.MP4")
            db.add(other)
            await db.flush()
            db.add(
                ClipLook(
                    asset_id=other.id,
                    kind="chosen",
                    source="custom",
                    recipe=recipe,
                    features=clip_look.features_of(_scene(3 * i)),
                )
            )
        await db.commit()


# --- three looks --------------------------------------------------------------------


async def test_a_clip_always_offers_three_looks_in_the_same_order(env: dict[str, Any]) -> None:
    body = await _get(env)
    assert [c["source"] for c in body["candidates"]] == ["model", "learned", "none"]
    assert body["filename"] == "DJI_0639.MP4" and body["problem"] is None
    assert body["reference_ts_ms"] == REFERENCE
    assert body["stills"] == [{"ts_ms": ts, "from": "shot"} for ts in MIDDLES]
    model, learned, none = (_by_source(body)[s] for s in ("model", "learned", "none"))
    # Nothing suggested, and this server cannot run a model: the tile says why.
    assert model["available"] is False and model["note"] == api.CANNOT_SUGGEST_HERE
    assert model["recipe"] == {}
    # No photograph look installed and no clip chosen yet.
    assert learned["available"] is False and learned["note"] == clip_look.NO_LEARNED_LOOK
    assert none["available"] is True
    assert (none["recipe"], none["in_words"], none["version"]) == ({}, clip_look.AS_SHOT, "0")
    assert body["chosen"] is None
    assert body["suggest"] == {
        "mode": "unavailable",
        "ok": False,
        "note": api.CANNOT_SUGGEST_HERE,
        "job": None,
    }
    assert body["lut"] == {"sizes": [33, 65], "default": 33, "assumes": lut.ASSUMES}

    # Once there is a suggestion it leads, still in the same place.
    await _suggest(env)
    body = await _get(env)
    assert [c["source"] for c in body["candidates"]] == ["model", "learned", "none"]
    model = _by_source(body)["model"]
    assert model["available"] is True and model["recipe"] == SUGGESTED
    assert model["in_words"] == clip_look.in_words(SUGGESTED)
    assert model["measured_at_ms"] == REFERENCE
    assert model["provenance"] == {**MADE_BY, "variant": ""}
    assert model["demo"] is True and model["made_at"].endswith("Z")
    assert model["version"] == clip_look.version_of(
        SUGGESTED, develop.measure(env["stills"][REFERENCE], SUGGESTED)
    )


async def test_the_folder_line_counts_the_clips_beside_this_one(env: dict[str, Any]) -> None:
    await _suggest(env)
    async with env["factory"]() as db:
        db.add(ClipLook(asset_id=env["beside"].id, kind="chosen", source="none"))
        await db.commit()
    folder = (await _get(env))["folder"]
    # The photograph and the clip in the folder inside are not beside it.
    assert (folder["path"], folder["clips"], folder["chosen"], folder["suggested"]) == (
        FOLDER,
        2,
        1,
        1,
    )
    assert folder["others"] == [
        {"asset_id": str(env["beside"].id), "filename": "DJI_0640.MP4", "state": "chosen"}
    ]


async def test_where_a_model_could_run_the_page_is_told_who_would_answer(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FRAMEFOUND_RUNTIMES_DIR", str(tmp_path / "runtimes"))
    get_settings.cache_clear()
    body = await _get(env)
    assert body["suggest"] == {"mode": "ready", "ok": False, "note": api.NO_LOOKER, "job": None}
    assert _by_source(body)["model"]["note"] == api.NO_LOOKER


# --- the pictures -------------------------------------------------------------------


async def test_a_preview_is_the_still_through_the_table(env: dict[str, Any]) -> None:
    await _suggest(env)
    still = env["stills"][REFERENCE]
    measured = develop.measure(still, SUGGESTED)
    table = lut.build(SUGGESTED, size=33, measured=measured)
    response = await env["http"].get(
        f"/api/v1/clip-looks/{env['clip'].id}/preview.jpg", params={"source": "model"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == _jpeg(lut.apply(still, table))

    # Another moment shows that still through the SAME frozen white balance:
    # that is how the operator sees whether one correction holds.
    other = env["stills"][17000]
    response = await env["http"].get(
        f"/api/v1/clip-looks/{env['clip'].id}/preview.jpg",
        params={"source": "model", "ts": 17000, "size": 65},
    )
    frozen = lut.build(SUGGESTED, size=65, measured=measured)
    assert response.content == _jpeg(lut.apply(other, frozen))
    measured_there = lut.build(SUGGESTED, size=65, measured=develop.measure(other, SUGGESTED))
    assert response.content != _jpeg(lut.apply(other, measured_there))


async def test_no_change_is_the_still_itself(env: dict[str, Any]) -> None:
    for ts in (None, 2000):
        params: dict[str, Any] = {"source": "none"}
        if ts is not None:
            params["ts"] = ts
        response = await env["http"].get(
            f"/api/v1/clip-looks/{env['clip'].id}/preview.jpg", params=params
        )
        assert response.status_code == 200
        assert response.content == _jpeg(env["stills"][ts or REFERENCE])


async def test_sliders_set_by_hand_are_shown_the_same_way(env: dict[str, Any]) -> None:
    sliders = {"exposure": 0.4, "auto_wb": 1.0, "shadows": -0.3}
    response = await env["http"].post(
        f"/api/v1/clip-looks/{env['clip'].id}/preview", params={"ts": 2000}, json=sliders
    )
    assert response.status_code == 200, response.text
    reference = env["stills"][REFERENCE]
    table = lut.build(sliders, measured=develop.measure(reference, sliders))
    assert response.content == _jpeg(lut.apply(env["stills"][2000], table))

    # A slider a LUT cannot carry, or one past the engine's end, is refused.
    for wrong in ({"window_pull": 0.5}, {"exposure": 3.0}, {"shadows": 0.2}):
        refused = await env["http"].post(f"/api/v1/clip-looks/{env['clip'].id}/preview", json=wrong)
        assert refused.status_code == 422, wrong


async def test_a_look_that_is_not_there_is_not_shown(env: dict[str, Any]) -> None:
    url = f"/api/v1/clip-looks/{env['clip'].id}/preview.jpg"
    for source, sentence in (
        ("model", api.NO_SUGGESTION),
        ("chosen", api.NOTHING_CHOSEN),
        ("learned", api.NO_LEARNED),
    ):
        response = await env["http"].get(url, params={"source": source})
        assert response.status_code == 404
        assert response.json()["error"]["message"] == sentence
    for params in ({"source": "best"}, {"source": "none", "size": 17}):
        assert (await env["http"].get(url, params=params)).status_code == 400


# --- choosing -------------------------------------------------------------------------


async def test_choosing_never_happens_by_itself(env: dict[str, Any]) -> None:
    await _suggest(env)
    body = await _get(env)
    assert body["chosen"] is None
    async with env["factory"]() as db:
        kinds = list(
            await db.scalars(select(ClipLook.kind).where(ClipLook.asset_id == env["clip"].id))
        )
    assert kinds == ["suggested"]


async def test_a_chosen_model_look_keeps_who_suggested_it(env: dict[str, Any]) -> None:
    suggestion = await _suggest(env)
    response = await env["http"].put(
        f"/api/v1/clip-looks/{env['clip'].id}/choice", json={"source": "model"}
    )
    assert response.status_code == 200, response.text
    chosen = response.json()["chosen"]
    assert chosen["source"] == "model" and chosen["recipe"] == SUGGESTED
    assert chosen["provenance"] == {**MADE_BY, "variant": ""}
    assert chosen["demo"] is True
    assert chosen["chosen_by"] == ADMIN["email"] and chosen["chosen_at"].endswith("Z")
    assert chosen["measured_at_ms"] == REFERENCE

    # The clip is asked again and a different build answers: the choice still
    # says what it was and who made it.
    async with env["factory"]() as db:
        row = await db.get(ClipLook, suggestion.id)
        assert row is not None
        row.recipe, row.model, row.demo = {"exposure": -0.5}, "google/gemma-4-12b-it", False
        await db.commit()
    body = await _get(env)
    assert body["chosen"]["recipe"] == SUGGESTED
    assert body["chosen"]["provenance"]["model"] == MADE_BY["model"]
    assert _by_source(body)["model"]["recipe"] == {"exposure": -0.5}


async def test_a_choice_replaces_the_last_one_and_can_be_cleared(env: dict[str, Any]) -> None:
    url = f"/api/v1/clip-looks/{env['clip'].id}/choice"
    first = await env["http"].put(url, json={"source": "none"})
    assert first.status_code == 200 and first.json()["chosen"]["source"] == "none"
    mine = {"exposure": 0.31, "contrast": 0.1}
    second = await env["http"].put(url, json={"source": "custom", "recipe": mine})
    assert second.json()["chosen"]["source"] == "custom"
    assert second.json()["chosen"]["recipe"] == mine
    async with env["factory"]() as db:
        rows = list(await db.scalars(select(ClipLook).where(ClipLook.asset_id == env["clip"].id)))
    assert [(row.kind, row.still_ts_ms) for row in rows] == [("chosen", REFERENCE)]
    assert len(rows[0].features) == len(compare.FEATURE_NAMES)

    cleared = await env["http"].delete(url)
    assert cleared.status_code == 200 and cleared.json()["chosen"] is None


async def test_what_cannot_be_chosen_is_refused_in_words(env: dict[str, Any]) -> None:
    url = f"/api/v1/clip-looks/{env['clip'].id}/choice"
    for body, status, sentence in (
        ({"source": "custom"}, 400, api.SEND_SLIDERS),
        ({"source": "model"}, 409, api.NO_SUGGESTION_TO_USE),
        ({"source": "learned"}, 409, api.NO_LEARNED_TO_USE),
    ):
        response = await env["http"].put(url, json=body)
        assert response.status_code == status, body
        assert response.json()["error"]["message"] == sentence
    assert (await env["http"].put(url, json={"source": "best"})).status_code == 422


# --- the learned look ----------------------------------------------------------------


async def test_a_clip_is_not_its_own_example(env: dict[str, Any]) -> None:
    look = {"exposure": 0.5, "vibrance": 0.2}
    await _other_clips_chosen(env, clip_look.MIN_CLIP_EXAMPLES - 1, look)
    # This clip's own choice would make eight; it does not count.
    choice = await env["http"].put(
        f"/api/v1/clip-looks/{env['clip'].id}/choice",
        json={"source": "custom", "recipe": {"exposure": -0.8}},
    )
    assert choice.status_code == 200
    learned = _by_source(choice.json())["learned"]
    assert learned["available"] is False

    await _other_clips_chosen(env, 1, look)
    learned = _by_source(await _get(env))["learned"]
    assert learned["available"] is True
    assert (learned["basis"], learned["examples"]) == ("clips", clip_look.MIN_CLIP_EXAMPLES)
    assert learned["recipe"] == look
    count = clip_look.MIN_CLIP_EXAMPLES
    assert learned["note"] == f"From the {count} clips you have chosen a look for."


async def test_the_photograph_look_is_offered_refitted_and_worked_out_once(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_photo_look(env["data"], dict(develop.LISTING_PRESET))
    calls: list[int] = []
    real = clip_look.learned

    def counted(*args: Any, **kwargs: Any) -> clip_look.Candidate:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(clip_look, "learned", counted)
    first = _by_source(await _get(env))["learned"]
    assert first["available"] is True and first["basis"] == "photos"
    assert first["note"] == clip_look.FROM_PHOTOS_NARROWED
    assert first["left_out"] and all(not any(c.isdigit() for c in w) for w in first["left_out"])
    assert first["measured_at_ms"] == REFERENCE
    again = _by_source(await _get(env))["learned"]
    assert again == first
    assert len(calls) == 1, "the fit runs once per clip, still and what it learned from"

    # Choosing it stores exactly what was offered.
    chosen = (
        await env["http"].put(
            f"/api/v1/clip-looks/{env['clip'].id}/choice", json={"source": "learned"}
        )
    ).json()["chosen"]
    assert chosen["recipe"] == first["recipe"] and chosen["version"] == first["version"]
    assert chosen["left_out"] == first["left_out"]


# --- the file --------------------------------------------------------------------------


async def test_the_downloaded_file_reads_back_and_names_its_assumptions(
    env: dict[str, Any],
) -> None:
    mine = {"exposure": 0.3, "auto_wb": 0.6, "temperature": 0.1, "vibrance": 0.3}
    await env["http"].put(
        f"/api/v1/clip-looks/{env['clip'].id}/choice", json={"source": "custom", "recipe": mine}
    )
    measured = develop.measure(env["stills"][REFERENCE], mine)
    for size in (33, 65):
        response = await env["http"].get(
            f"/api/v1/clip-looks/{env['clip'].id}/look.cube", params={"size": size}
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "text/plain; charset=us-ascii"
        disposition = response.headers["content-disposition"]
        assert 'filename="DJI_0639 - FrameFound chosen.cube"' in disposition
        assert b"\r" not in response.content
        response.content.decode("ascii")
        table = lut.parse_cube(response.content)
        assert table.shape == (size, size, size, 3)
        expected = lut.build(mine, size=size, measured=measured)
        # Six decimals in the file, read back as float32.
        assert np.abs(table - expected).max() <= 1e-6
        text = response.text
        for line in (
            "# FrameFound look for DJI_0639.MP4 - the chosen look (set by hand)",
            "# Made for ordinary (Rec. 709) video as it looks in FrameFound's preview.",
            "# Not for log or HDR footage unless Premiere shows it the same way.",
            "# White balance measured on the still at 0:09.000 - gains",
        ):
            assert line in text


async def test_a_suggested_file_says_which_model_and_whether_it_was_made_up(
    env: dict[str, Any],
) -> None:
    await _suggest(env)
    response = await env["http"].get(
        f"/api/v1/clip-looks/{env['clip'].id}/look.cube", params={"source": "model"}
    )
    lines = response.text.splitlines()
    assert lines[0] == 'TITLE "DJI_0639 - FrameFound model"'
    assert lines[1].startswith("# DEMONSTRATION")
    assert f"# Model: {MADE_BY['model']} @ 475b9088 - mlx-vlm 0.7.12 - prompt colour-1" in lines


async def test_the_clip_as_it_was_shot_needs_no_file(env: dict[str, Any]) -> None:
    await env["http"].put(f"/api/v1/clip-looks/{env['clip'].id}/choice", json={"source": "none"})
    response = await env["http"].get(f"/api/v1/clip-looks/{env['clip'].id}/look.cube")
    assert response.status_code == 400
    assert response.json()["error"]["message"] == api.NO_LUT_NEEDED


async def test_a_reading_that_will_not_build_is_taken_again(env: dict[str, Any]) -> None:
    row = await _suggest(env)
    async with env["factory"]() as db:
        stored = await db.get(ClipLook, row.id)
        assert stored is not None
        stored.measured = {"wb_gains": [1.0, "warm", 0.9]}
        await db.commit()
    response = await env["http"].get(
        f"/api/v1/clip-looks/{env['clip'].id}/look.cube", params={"source": "model"}
    )
    assert response.status_code == 200
    expected = lut.build(SUGGESTED, measured=develop.measure(env["stills"][REFERENCE], SUGGESTED))
    assert np.abs(lut.parse_cube(response.content) - expected).max() <= 1e-6


# --- the folder -------------------------------------------------------------------------


async def test_the_folder_zip_holds_one_file_per_chosen_clip(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "/api/v1/clip-looks/folder.zip"
    params = {"asset_id": str(env["clip"].id)}
    nothing = await env["http"].get(url, params=params)
    assert nothing.status_code == 404
    assert nothing.json()["error"]["message"] == api.NONE_CHOSEN_IN_FOLDER

    async with env["factory"]() as db:
        twin = _asset(env["library"].id, f"{FOLDER}/DJI_0640.MOV")
        db.add(twin)
        await db.flush()
        look = {"exposure": 0.2}
        for asset_id, recipe in (
            (env["beside"].id, look),
            (twin.id, {"contrast": 0.2}),
            # "No change" is a choice, but there is no file to make of it.
            (env["clip"].id, {}),
            # In the folder inside: not this folder's.
            (env["inside"].id, look),
        ):
            db.add(ClipLook(asset_id=asset_id, kind="chosen", source="custom", recipe=recipe))
        await db.commit()

    response = await env["http"].get(url, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="Drone - FrameFound looks.zip"' in response.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    names = sorted(archive.namelist())
    # Two clips with one stem are told apart rather than one overwriting the other.
    assert names == ["DJI_0640 (2).cube", "DJI_0640.cube", "READ ME.txt"]
    for name in names[:2]:
        assert lut.parse_cube(archive.read(name)).shape == (33, 33, 33, 3)
    read_me = archive.read("READ ME.txt").decode("ascii")
    assert lut.ASSUMES in read_me and "Lumetri Color" in read_me

    monkeypatch.setattr(api, "ZIP_MOST_FINE", 1)
    too_fine = await env["http"].get(url, params={**params, "size": 65})
    assert too_fine.status_code == 400
    assert too_fine.json()["error"]["message"] == api.FOLDER_TOO_FINE


# --- where it cannot be done -------------------------------------------------------------


async def test_a_photograph_has_no_looks(env: dict[str, Any]) -> None:
    for path in ("", "/preview.jpg?source=none", "/look.cube"):
        response = await env["http"].get(f"/api/v1/clip-looks/{env['photo'].id}{path}")
        assert response.status_code == 400, path
        assert response.json()["error"]["message"] == api.NOT_VIDEO
    missing = await env["http"].get(f"/api/v1/clip-looks/{uuid.uuid4()}")
    assert missing.status_code == 404


async def test_a_clip_with_no_picture_says_so_and_is_not_an_error(env: dict[str, Any]) -> None:
    body = await _get(env, env["beside"])
    assert body["problem"] == api.NO_STILL
    assert body["stills"] == [] and body["reference_ts_ms"] is None
    assert all(c["available"] is False for c in body["candidates"])
    picture = await env["http"].get(
        f"/api/v1/clip-looks/{env['beside'].id}/preview.jpg", params={"source": "none"}
    )
    assert picture.status_code == 409
    assert picture.json()["error"]["message"] == api.NO_STILL
    choice = await env["http"].put(
        f"/api/v1/clip-looks/{env['beside'].id}/choice", json={"source": "none"}
    )
    assert choice.status_code == 409


async def test_looks_work_where_models_cannot_run(env: dict[str, Any]) -> None:
    await _other_clips_chosen(env, clip_look.MIN_CLIP_EXAMPLES, {"exposure": 0.4})
    body = await _get(env)
    assert body["suggest"]["mode"] == "unavailable" and body["suggest"]["ok"] is False
    assert body["suggest"]["note"] == api.CANNOT_SUGGEST_HERE
    assert _by_source(body)["learned"]["available"] is True
    for source in ("learned", "none"):
        chose = await env["http"].put(
            f"/api/v1/clip-looks/{env['clip'].id}/choice", json={"source": source}
        )
        assert chose.status_code == 200 and chose.json()["chosen"]["source"] == source
    await env["http"].put(f"/api/v1/clip-looks/{env['clip'].id}/choice", json={"source": "learned"})
    cube = await env["http"].get(f"/api/v1/clip-looks/{env['clip'].id}/look.cube")
    assert cube.status_code == 200 and lut.parse_cube(cube.content).shape == (33, 33, 33, 3)
    # Only asking the model is refused, in the tile's own words.
    asked = await env["http"].post(f"/api/v1/clip-looks/{env['clip'].id}/suggest")
    assert asked.status_code == 409
    assert asked.json()["error"]["message"] == api.CANNOT_SUGGEST_HERE


async def test_nothing_the_api_returns_is_a_colour_difference(env: dict[str, Any]) -> None:
    _install_photo_look(env["data"], dict(develop.LISTING_PRESET))
    await _suggest(env)
    answers = [await _get(env)]
    url = f"/api/v1/clip-looks/{env['clip'].id}/choice"
    for body in ({"source": "model"}, {"source": "learned"}, {"source": "none"}):
        answers.append((await env["http"].put(url, json=body)).json())
    answers.append((await env["http"].delete(url)).json())
    answers.append(await _get(env, env["beside"]))

    banned = ("delta", "score", "distance", "loss", "rank", "error")

    def walk(value: Any, where: str) -> None:
        if isinstance(value, dict):
            for key, inner in value.items():
                assert not any(word in key.lower() for word in banned), f"{where}.{key}"
                walk(inner, f"{where}.{key}")
        elif isinstance(value, list):
            for index, inner in enumerate(value):
                walk(inner, f"{where}[{index}]")

    for answer in answers:
        walk(answer, "answer")
    # The words for a look carry no figure either.
    for answer in answers[:-1]:
        for candidate in answer["candidates"]:
            assert not any(char.isdigit() for char in candidate["in_words"])


# --- what a page may make the server do -------------------------------------------------


def _no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Every grab from a proxy, counted; each one fails, as a proxy that will not seek."""
    grabs: list[float] = []

    def grab(_src: Path, _dst: Path, at_s: float, _width: int) -> Any:
        grabs.append(at_s)
        raise RuntimeError("it will not seek")

    monkeypatch.setattr(shots, "_grab", grab)
    return grabs


def _proxy(data: Path, asset_id: uuid.UUID) -> None:
    proxy = shots.proxy_path(data, asset_id)
    proxy.parent.mkdir(parents=True, exist_ok=True)
    proxy.write_bytes(b"a proxy")


async def test_only_the_clips_own_moments_are_shown(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    grabs = _no_ffmpeg(monkeypatch)
    # With a proxy, any moment that has no still of its own would be grabbed
    # from it, and kept in the data folder, for whoever asks.
    _proxy(env["data"], env["clip"].id)
    base = f"/api/v1/clip-looks/{env['clip'].id}"
    for ts in (1, 9001, 10**9):
        shown = await env["http"].get(f"{base}/preview.jpg", params={"source": "none", "ts": ts})
        assert shown.status_code == 400, ts
        assert shown.json()["error"]["message"] == api.NOT_A_MOMENT
    by_hand = await env["http"].post(f"{base}/preview", params={"ts": 4321}, json={"exposure": 0.2})
    assert by_hand.status_code == 400
    assert by_hand.json()["error"]["message"] == api.NOT_A_MOMENT
    listed = await env["http"].get(f"{base}/preview.jpg", params={"source": "none", "ts": 17000})
    assert listed.status_code == 200
    assert grabs == []


async def test_a_moment_the_proxy_cannot_give_is_not_grabbed_on_every_look(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    grabs = _no_ffmpeg(monkeypatch)
    async with env["factory"]() as db:
        # Shots found, but its still was never saved and it has no sampled frame.
        bare = _asset(env["library"].id, f"{FOLDER}/DJI_0701.MP4")
        db.add(bare)
        await db.flush()
        db.add(ShotWindow(asset_id=bare.id, idx=0, start_ms=0, end_ms=6000, move="static"))
        await db.commit()
    _proxy(env["data"], bare.id)
    # The page asks for its clip every few seconds while a suggestion is on its way.
    for _ in range(3):
        assert (await _get(env, bare))["problem"] == api.NO_STILL
    picture = await env["http"].get(
        f"/api/v1/clip-looks/{bare.id}/preview.jpg", params={"source": "none"}
    )
    assert picture.status_code == 409
    # Tried once, at the middle of its one shot; not once a request.
    assert grabs == [3.0]


async def test_the_folder_line_counts_the_looks_its_zip_holds(env: dict[str, Any]) -> None:
    async with env["factory"]() as db:
        beside = await db.get(Asset, env["beside"].id)
        assert beside is not None
        # Its card is unplugged: it is not one of the folder's clips today.
        beside.availability = "offline"
        for asset_id in (env["clip"].id, env["beside"].id):
            db.add(
                ClipLook(
                    asset_id=asset_id, kind="chosen", source="custom", recipe={"exposure": 0.2}
                )
            )
        await db.commit()

    async def zipped(asset: Asset) -> list[str]:
        response = await env["http"].get(
            "/api/v1/clip-looks/folder.zip", params={"asset_id": str(asset.id)}
        )
        assert response.status_code == 200, response.text
        return sorted(zipfile.ZipFile(io.BytesIO(response.content)).namelist())

    folder = (await _get(env))["folder"]
    assert (folder["clips"], folder["chosen"], folder["others"]) == (1, 1, [])
    assert await zipped(env["clip"]) == ["DJI_0639.cube", "READ ME.txt"]
    # On the offline clip's own page it is counted, and its look is in the zip.
    folder = (await _get(env, env["beside"]))["folder"]
    assert (folder["clips"], folder["chosen"]) == (2, 2)
    assert await zipped(env["beside"]) == ["DJI_0639.cube", "DJI_0640.cube", "READ ME.txt"]


async def test_two_presses_at_once_both_get_an_answer(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = api._rows
    raced: list[int] = []

    async def other_press_first(db: AsyncSession, asset_id: uuid.UUID) -> dict[str, ClipLook]:
        found = await rows(db, asset_id)
        if not raced and api.CHOSEN not in found:
            # The other press stores its choice after this one looked and found none.
            raced.append(1)
            async with env["factory"]() as other:
                other.add(
                    ClipLook(
                        asset_id=asset_id, kind="chosen", source="custom", recipe={"exposure": 0.4}
                    )
                )
                await other.commit()
        return found

    monkeypatch.setattr(api, "_rows", other_press_first)
    response = await env["http"].put(
        f"/api/v1/clip-looks/{env['clip'].id}/choice", json={"source": "none"}
    )
    assert raced == [1]
    assert response.status_code == 200, response.text
    assert response.json()["chosen"]["source"] == "none"
    assert response.json()["chosen"]["chosen_by"] == ADMIN["email"]
    async with env["factory"]() as db:
        stored = list(await db.scalars(select(ClipLook).where(ClipLook.asset_id == env["clip"].id)))
    assert [(row.kind, row.source) for row in stored] == [("chosen", "none")]
