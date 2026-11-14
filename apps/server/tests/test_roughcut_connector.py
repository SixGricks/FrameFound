"""The connector's `build_rough_cut` is the cut writer with a session and a
user put round it (framefound/mcp/video.py over framefound/nle/roughcut.py).

What Claude gets must not have changed when the body moved: the same XML,
byte for byte, and the same sentence for every refusal. Kept apart from
test_roughcut.py because this one needs the connector's package, and that
one must run without it.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from test_mcp import connect, env, serving  # noqa: F401 - pytest fixture
from test_mcp_video import BASE, _video

from framefound.db.models import Asset, Cut
from framefound.nle import fcp7, roughcut
from framefound.nle.roughcut import CutError, CutIn


async def test_the_function_and_the_tool_write_the_same_timeline(env: dict) -> None:  # noqa: F811
    """The tool's XML is the function's, and both are what the sequence
    writer makes of those clips by hand: nothing about the rate, the paths
    or the photo's hold moved with the code."""
    video = await _video(env)
    photo = env["ids"]["barn-wide.jpg"]
    cuts: list[dict[str, Any]] = [
        {"id": str(video), "in_s": 12.0, "out_s": 15.5, "note": "Look at that barn."},
        {"id": str(photo), "out_s": 3},
        {"id": str(video)[:8], "in_s": 40.0},
    ]
    asked = {"title": "Ledgerock 60", "cuts": cuts, "profile": "Mac Mini"}
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        tool = await session.call_tool("build_rough_cut", asked)
    assert not tool.is_error, tool.content
    async with env["factory"]() as db:
        made = await roughcut.build_rough_cut(
            db, "Ledgerock 60", [CutIn(**c) for c in cuts], profile="Mac Mini", record=False
        )
        photo_row = await db.get(Asset, photo)
        recorded = (await db.execute(select(Cut))).scalars().all()
    assert photo_row is not None

    drone = fcp7.Clip(
        name="DJI_0042.MP4",
        path="/Volumes/Intel/Ledgerock/DJI_0042.MP4",
        duration_s=60.0,
        fps=29.97,
        width=3840,
        height=2160,
        has_audio=True,
    )
    still = fcp7.Clip(
        name="barn-wide.jpg",
        path=f"/Volumes/Intel/{photo_row.relative_path}",
        has_audio=False,
    )
    by_hand = fcp7.build_sequence(
        "Ledgerock 60",
        [
            fcp7.Cut(drone, 12.0, 15.5, note="Look at that barn."),
            fcp7.Cut(still, 0.0, 3.0, is_still=True),
            fcp7.Cut(drone, 40.0, 60.0),
        ],
    )
    body = tool.structured_content
    assert body is not None
    assert body["xml"] == made.xml == by_hand
    assert {k: body[k] for k in ("filename", "duration_s", "cuts", "unmapped")} == {
        "filename": "Ledgerock 60.xml",
        "duration_s": 26.5,
        "cuts": 3,
        "unmapped": [],
    }
    assert body["note"] == "3 cuts, 26.5s. Recorded in cut history."
    # The tool's own cut is in history, and the answer names it.
    [row] = recorded
    assert body["recorded"] is True and body["cut_id"] == str(row.id)


async def test_a_refusal_reaches_claude_as_the_same_sentence(env: dict) -> None:  # noqa: F811
    """Each refusal the function raises is the text of the tool's error, and
    that text is the sentence the connector has always answered with."""
    video = str(await _video(env))
    async with env["factory"]() as db:
        clip = await db.get(Asset, uuid.UUID(video))
        assert clip is not None
        audio = Asset(
            library_id=clip.library_id,
            relative_path="Ledgerock/voice.wav",
            filename="voice.wav",
            extension="wav",
            media_type="audio",
            audio_codec="pcm_s16le",
            duration_s=30.0,
            size_bytes=10,
            mtime=BASE,
        )
        db.add(audio)
        await db.commit()
        voice = str(audio.id)
    asks: list[tuple[dict[str, Any], str]] = [
        ({"cuts": []}, "Give at least one cut"),
        ({"cuts": [{"id": video}] * 201}, "At most 200 cuts in one sequence"),
        (
            {"cuts": [{"id": video}], "profile": "iPad"},
            "No path profile called 'iPad'. Profiles: DJ's PC, Mac Mini "
            "(add one per library on the Libraries page).",
        ),
        ({"cuts": [{"id": "barn"}]}, "'barn' is not an asset id"),
        ({"cuts": [{"id": "00000000"}]}, "No asset 00000000"),
        ({"cuts": [{"id": voice}]}, "voice.wav is audio; a rough cut takes video and photos"),
        (
            {"cuts": [{"id": video, "in_s": 30, "out_s": 10}]},
            "DJI_0042.MP4: out_s (10) must be after in_s (30); the clip is 60s long",
        ),
    ]
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        for arguments, sentence in asks:
            result = await session.call_tool("build_rough_cut", {"title": "x", **arguments})
            assert result.is_error, arguments
            # The SDK's prefix for any refusal a tool raises, as before.
            assert _said(result) == f"Error executing tool build_rough_cut: {sentence}"
            async with env["factory"]() as db:
                with pytest.raises(CutError) as refused:
                    await roughcut.build_rough_cut(
                        db,
                        "x",
                        [CutIn(**c) for c in arguments["cuts"]],
                        profile=arguments.get("profile"),
                    )
            assert str(refused.value) == sentence
        # The other tools that look an id up answer in the same words.
        files = await session.call_tool("get_media_files", {"id": "barn"})
    assert files.is_error and _said(files).endswith(": 'barn' is not an asset id")
    async with env["factory"]() as db:
        assert (await db.execute(select(Cut))).first() is None


def _said(result: Any) -> str:
    return str(result.content[0].text)
