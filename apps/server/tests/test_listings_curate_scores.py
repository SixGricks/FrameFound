"""The curate endpoint passes a local model's scores to culling (L3 D17).

media/curate.py's own rules are in test_curate_scores.py; this pins the
route's half: it reads `assets.look_score` / `look_reason`, and each
suggestion says the score of the photograph it offers.
"""

import uuid as uuidlib

from PIL import Image
from sqlalchemy import select, update
from test_listings import BASE, ROOM_INDEX, _unit, env  # noqa: F401 - pytest fixture

from framefound.db.models import Asset, Frame, Library
from framefound.media import curate as curate_lib


async def _kitchen_twin(env: dict) -> str:  # noqa: F811
    """A second frame of the kitchen, as near as the first in CLIP's eyes
    and flat where the first is full of detail: sharpness alone keeps the
    first."""
    import random

    rng = random.Random(7)
    detailed = Image.new("RGB", (120, 90))
    detailed.putdata([(rng.randrange(256),) * 3 for _ in range(120 * 90)])
    detailed.save(env["tmp"] / "media" / "kit.jpg", "JPEG")
    async with env["factory"]() as db:
        library = (await db.execute(select(Library))).scalar_one()
        Image.new("RGB", (120, 90), (200, 70, 40)).save(env["tmp"] / "media" / "kit2.jpg", "JPEG")
        twin = Asset(
            library_id=library.id,
            relative_path="kit2.jpg",
            filename="kit2.jpg",
            extension="jpg",
            media_type="image",
            size_bytes=1000,
            mtime=BASE,
            availability="online",
        )
        db.add(twin)
        await db.flush()
        db.add(
            Frame(
                asset_id=twin.id,
                ts_ms=0,
                relative_path="f/kit2.jpg.jpeg",
                embedding=_unit(ROOM_INDEX["kitchen"]),
            )
        )
        await db.commit()
        return str(twin.id)


async def _curate(env: dict, keys: list[str]) -> list[dict]:  # noqa: F811
    created = await env["client"].post(
        "/api/v1/listings", json={"name": "4 Elm St", "asset_ids": keys}
    )
    assert created.status_code == 201, created.text
    resp = await env["client"].post(f"/api/v1/listings/{created.json()['id']}/curate")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_without_scores_a_suggestion_says_none(env: dict) -> None:  # noqa: F811
    twin = await _kitchen_twin(env)
    keys = [env["ids"]["kitchen"], twin, env["ids"]["front"], env["ids"]["bedroom"]]
    [suggestion] = await _curate(env, keys)
    assert (suggestion["asset_id"], suggestion["keep_instead"]) == (twin, env["ids"]["kitchen"])
    assert suggestion["reason"] == curate_lib.DUPLICATE_SOFTER
    assert suggestion["score"] is None


async def test_the_twin_the_model_preferred_is_kept_and_the_score_is_shown(
    env: dict,  # noqa: F811
) -> None:
    """Sharpness would keep the detailed frame; the model liked the flat
    twin far better (88 against 41), so the detailed one is offered."""
    twin = await _kitchen_twin(env)
    kitchen = env["ids"]["kitchen"]
    async with env["factory"]() as db:
        for asset_id, score, reason in (
            (kitchen, 41, "the counter is cut off"),
            (twin, 88, "the whole room in good light"),
        ):
            await db.execute(
                update(Asset)
                .where(Asset.id == uuidlib.UUID(asset_id))
                .values(look_score=score, look_reason=reason)
            )
        await db.commit()
    keys = [kitchen, twin, env["ids"]["front"], env["ids"]["bedroom"]]
    [suggestion] = await _curate(env, keys)
    assert (suggestion["asset_id"], suggestion["keep_instead"]) == (kitchen, twin)
    assert suggestion["reason"] == curate_lib.DUPLICATE_PREFERRED
    assert suggestion["score"] == 41
