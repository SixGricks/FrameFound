"""What the connector says about a local model's looks (L3 D16, D15).

The shot lines themselves are in test_mcp_shots.py; this file covers the
rest of what Claude reads: the score sits before a local path, a made-up
score says so, `min_score` on a catalogue nobody has looked at says why it
found nothing, the review queue can be narrowed to the local model, and a
search hit found by a caption says "AI caption".
"""

import pytest
from test_mcp import connect, env, serving  # noqa: F401 - pytest fixture
from test_mcp_shots import _course, _looked_at, _text

from framefound.db.models import AssetTag, Library, Look, PathMapping, Tag
from framefound.mcp import compact, shots


async def test_the_score_comes_before_the_local_path(env: dict) -> None:  # noqa: F811
    ids = await _course(env, clips=1)
    orbit = ids["DJI_20261103051509_0639_D.MP4"]
    await _looked_at(env, orbit, 2000, 14000, 91)
    async with env["factory"]() as db:
        library = (await db.execute(Library.__table__.select())).first()
        db.add(
            PathMapping(
                library_id=library.id,
                profile_name="Mac Mini",
                platform="macos",
                mapped_prefix="/Volumes/GELCO",
            )
        )
        await db.commit()
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        mac = await session.call_tool("find_shots", {"profile": "Mac Mini"})
    line = _text(mac).splitlines()[1]
    assert line.endswith(
        "green,bunker score 91"
        " | /Volumes/GELCO/Brookside Country Club/2026/Drone/DJI_20261103051509_0639_D.MP4"
    )


async def test_a_made_up_score_says_so(env: dict) -> None:  # noqa: F811
    ids = await _course(env, clips=1)
    await _looked_at(env, ids["DJI_20261103051509_0639_D.MP4"], 2000, 14000, 77, demo=True)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        plain = await session.call_tool("find_shots", {})
        verbose = await session.call_tool("find_shots", {"verbose": True})
    assert _text(plain).splitlines()[1].endswith(" score 77 made-up")
    assert verbose.structured_content["result"][0]["made_up"] is True


async def test_min_score_on_footage_nobody_looked_at_says_why_nothing_came_back(
    env: dict,  # noqa: F811
) -> None:
    await _course(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("find_shots", {"min_score": 50})
    lines = _text(result).splitlines()
    assert lines[0].startswith("0 of 0 shots")
    assert "No shot here has a local model's score yet" in lines[-1]


async def test_the_review_queue_can_be_narrowed_to_the_local_model(env: dict) -> None:  # noqa: F811
    async with env["factory"]() as db:
        barn = Tag(name="Silo bin", slug="silo-bin")
        db.add(barn)
        await db.flush()
        db.add(
            AssetTag(
                asset_id=env["ids"]["barn-wide.jpg"],
                tag_id=barn.id,
                source="suggested",
                confidence=0.9,
                origin="local",
            )
        )
        await db.commit()
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        local = await session.call_tool("review_queue", {"origin": "local"})
        everything = await session.call_tool("review_queue", {})
    assert [(row["tag"], row["origin"]) for row in local.structured_content["result"]] == [
        ("Silo bin", "local")
    ]
    origins = {row["origin"] for row in everything.structured_content["result"]}
    assert origins == {"local", "clip"}


async def test_a_hit_found_by_a_caption_says_ai_caption(env: dict) -> None:  # noqa: F811
    kitchen = env["ids"]["kitchen.jpg"]
    async with env["factory"]() as db:
        db.add(
            Look(
                asset_id=kitchen,
                caption="A farmhouse kitchen with an island",
                score=72,
                key="google/gemma-4-E4B-it@abc123#look-1",
                model="google/gemma-4-E4B-it",
            )
        )
        await db.commit()
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        found = await session.call_tool("search_media", {"query": "farmhouse island"})
        verbose = await session.call_tool(
            "search_media", {"query": "farmhouse island", "verbose": True}
        )
    assert "[AI caption]" in _text(found)
    [hit] = [h for h in verbose.structured_content["hits"] if h["filename"] == "kitchen.jpg"]
    assert hit["matched"] == ["AI caption"]


async def test_min_score_and_score_order_look_past_the_scan_limit(
    env: dict,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In an archive larger than the scan limit, the scored shots must not
    depend on which windows the database happened to return first: the
    score filter and order are applied before the cut."""
    ids = await _course(env)
    glide = ids["DJI_20261103035806_0495_D.MP4"]
    await _looked_at(env, glide, 21000, 27000, 80)
    monkeypatch.setattr(shots, "SCAN_LIMIT", 1)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        kept = await session.call_tool("find_shots", {"min_score": 60})
        ordered = await session.call_tool("find_shots", {"order": "score"})
    kept_lines = _text(kept).splitlines()
    assert kept_lines[0].startswith("1 of 1 shots")
    assert kept_lines[1].startswith(compact.short_id(glide) + " DJI_0495 21.0-27.0s")
    assert _text(ordered).splitlines()[1].endswith(" score 80")


async def test_min_score_above_every_score_says_so_and_not_that_none_is_scored(
    env: dict,  # noqa: F811
) -> None:
    ids = await _course(env)
    await _looked_at(env, ids["DJI_20261103035806_0495_D.MP4"], 21000, 27000, 55)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("find_shots", {"min_score": 90})
    lines = _text(result).splitlines()
    assert lines[0].startswith("0 of 0 shots")
    assert lines[-1] == (
        "No shot here was scored 90 or more by a local model. "
        "Lower min_score, or ask without it for the measured shots."
    )


def test_the_key_a_shot_was_looked_at_under_is_described_with_its_variant() -> None:
    """A GGUF build is one of several in its repository; the schema Claude
    reads must say the key can carry it, or `/Q4_K_M` reads as part of the
    revision."""
    described = shots.ShotOut.model_fields["looked_by"].description or ""
    assert "<repo>@<revision>[/<variant>]#<prompt version>" in described
