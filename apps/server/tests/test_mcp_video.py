"""The MCP tools for making video from the catalogue (framefound/mcp/video.py).

The workflow they exist for: Claude (in Cowork or Claude Desktop, on the
operator's Windows PC or Mac Mini) finds footage, reads what was said, picks
shots, and hands back a timeline that opens on *that* computer.
"""

import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

import pytest
from test_mcp import connect, env, serving  # noqa: F401 - pytest fixture

from framefound.db.models import (
    Asset,
    Derivative,
    Frame,
    Library,
    PathMapping,
    Transcript,
    TranscriptSegment,
)
from framefound.mcp import compact
from framefound.nle.fcp7 import Clip, Cut, build_sequence

BASE = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


async def _video(env: dict) -> uuid.UUID:  # noqa: F811
    """A 60 s drone clip with speech, three scenes, a proxy, and a profile
    for each of the operator's computers."""
    async with env["factory"]() as db:
        library = (await db.execute(Library.__table__.select())).first()
        library_id = library.id
        asset = Asset(
            library_id=library_id,
            relative_path="Ledgerock/DJI_0042.MP4",
            filename="DJI_0042.MP4",
            extension="mp4",
            media_type="video",
            audio_codec="aac",
            duration_s=60.0,
            fps=29.97,
            width=3840,
            height=2160,
            size_bytes=10_000,
            mtime=BASE,
            availability="online",
        )
        db.add(asset)
        await db.flush()
        # The shape extract_frames writes: interval ticks (scene_number None)
        # around numbered scene changes. An earlier fixture numbered every
        # frame, which production never does, and hid a tick swallowing a cut.
        for ts, scene in ((0, None), (4000, None), (21000, 1), (40000, 2)):
            db.add(
                Frame(
                    asset_id=asset.id,
                    ts_ms=ts,
                    scene_number=scene,
                    is_scene_change=scene is not None,
                    relative_path=f"frames/{asset.id}/{ts}.jpg",
                )
            )
        db.add(
            Derivative(
                asset_id=asset.id,
                kind="proxy",
                relative_path=f"derivatives/x/{asset.id}/proxy.mp4",
                media_format="mp4",
                status="ready",
            )
        )
        transcript = Transcript(
            asset_id=asset.id, language="en", model_name="fake/test", segment_count=2
        )
        db.add(transcript)
        await db.flush()
        db.add_all(
            [
                TranscriptSegment(
                    transcript_id=transcript.id,
                    start_ms=12_000,
                    end_ms=15_500,
                    text="Look at that barn.",
                ),
                TranscriptSegment(
                    transcript_id=transcript.id,
                    start_ms=30_000,
                    end_ms=33_250,
                    text="Starting bid is ten.",
                ),
            ]
        )
        db.add_all(
            [
                PathMapping(
                    library_id=library_id,
                    profile_name="DJ's PC",
                    platform="windows",
                    mapped_prefix="Y:\\",
                ),
                PathMapping(
                    library_id=library_id,
                    profile_name="Mac Mini",
                    platform="macos",
                    mapped_prefix="/Volumes/Intel",
                ),
            ]
        )
        await db.commit()
        return asset.id


async def test_the_video_tools_are_listed(env: dict) -> None:  # noqa: F811
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
    assert {
        "path_profiles",
        "get_media_files",
        "get_transcript",
        "get_scenes",
        "build_rough_cut",
        "build_selects",
        "find_shots",
        "contact_sheet",
        "recent_cuts",
    } <= names


async def test_profiles_name_each_computer_and_its_platform(env: dict) -> None:  # noqa: F811
    await _video(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("path_profiles", {})
    profiles = {p["name"]: p for p in result.structured_content["result"]}
    assert profiles["Mac Mini"]["platform"] == "macos"
    assert profiles["DJ's PC"]["libraries"] == {"Intel": "Y:\\"}


async def test_media_files_give_each_computer_its_own_path(env: dict) -> None:  # noqa: F811
    video = await _video(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        both = await session.call_tool("get_media_files", {"id": str(video)})
        mac = await session.call_tool("get_media_files", {"id": str(video), "profile": "Mac Mini"})
    paths = {p["profile"]: p["path"] for p in both.structured_content["local_paths"]}
    assert paths == {
        "DJ's PC": "Y:\\Ledgerock\\DJI_0042.MP4",
        "Mac Mini": "/Volumes/Intel/Ledgerock/DJI_0042.MP4",
    }
    assert [p["path"] for p in mac.structured_content["local_paths"]] == [
        "/Volumes/Intel/Ledgerock/DJI_0042.MP4"
    ]
    proxy = both.structured_content["downloads"]["proxy"]
    assert f"/api/v1/media/{video}/proxy?exp=" in proxy and "&sig=" in proxy


async def test_an_unknown_profile_names_the_real_ones(env: dict) -> None:  # noqa: F811
    video = await _video(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("get_media_files", {"id": str(video), "profile": "iPad"})
    assert result.is_error
    text = result.content[0].text
    assert "Mac Mini" in text and "DJ's PC" in text


async def test_the_transcript_comes_back_timed_and_narrowable(env: dict) -> None:  # noqa: F811
    video = await _video(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        whole = await session.call_tool("get_transcript", {"id": str(video)})
        later = await session.call_tool("get_transcript", {"id": str(video), "start_s": 20})
    body = whole.structured_content
    assert body["state"] == "speech"
    assert body["cues"][0] == {"start_s": 12.0, "end_s": 15.5, "text": "Look at that barn."}
    assert [c["text"] for c in later.structured_content["cues"]] == ["Starting bid is ten."]


async def test_scenes_become_ranges_with_pictures(env: dict) -> None:  # noqa: F811
    video = await _video(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("get_scenes", {"id": str(video)})
    scenes = result.structured_content["scenes"]
    assert [(s["start_s"], s["end_s"]) for s in scenes] == [
        (0.0, 21.0),
        (21.0, 40.0),
        (40.0, 60.0),
    ]
    # The 4 s tick is a picture inside the opening shot, not a shot of its own.
    assert scenes[0]["sampled_ms"] == [0, 4000]
    assert [s["frame_ms"] for s in scenes] == [0, 21000, 40000]
    assert f"/api/v1/media/{video}/frame?ts=21000&exp=" in scenes[1]["frame_url"]


async def _video_with_frames(env: dict, frames: list[tuple[int, int | None]]) -> uuid.UUID:  # noqa: F811
    async with env["factory"]() as db:
        library = (await db.execute(Library.__table__.select())).first()
        asset = Asset(
            library_id=library.id,
            relative_path="Ledgerock/DJI_0099.MP4",
            filename="DJI_0099.MP4",
            extension="mp4",
            media_type="video",
            duration_s=120.0,
            fps=29.97,
            size_bytes=10_000,
            mtime=BASE,
            availability="online",
        )
        db.add(asset)
        await db.flush()
        for ts, scene in frames:
            db.add(
                Frame(
                    asset_id=asset.id,
                    ts_ms=ts,
                    scene_number=scene,
                    is_scene_change=scene is not None,
                    relative_path=f"frames/{asset.id}/{ts}.jpg",
                )
            )
        await db.commit()
        return asset.id


async def test_a_tick_never_swallows_a_later_scene(env: dict) -> None:  # noqa: F811
    """What plan_samples really gives a 120 s clip cut at 7/13/31/47 s. The
    tick at row index 3 (20 s) used to take dedup key 3 and swallow scene 3,
    so one "scene" ran from 20 s to 40 s across the hard cut at 31 s."""
    rows = [
        (0, None),
        (7000, 1),
        (13000, 2),
        (20000, None),
        (31000, 3),
        (40000, None),
        (47000, 4),
        (60000, None),
    ]
    video = await _video_with_frames(env, rows)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("get_scenes", {"id": str(video)})
    scenes = result.structured_content["scenes"]
    assert [s["start_s"] for s in scenes] == [0.0, 7.0, 13.0, 31.0, 47.0]
    assert [s["end_s"] for s in scenes] == [7.0, 13.0, 31.0, 47.0, 120.0]
    assert scenes[2]["sampled_ms"] == [13000, 20000]
    assert result.structured_content["note"] == "Scene changes as detected."


async def test_without_detection_each_sample_is_a_range(env: dict) -> None:  # noqa: F811
    video = await _video_with_frames(env, [(0, None), (10000, None), (20000, None)])
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("get_scenes", {"id": str(video)})
    scenes = result.structured_content["scenes"]
    assert [(s["start_s"], s["end_s"]) for s in scenes] == [
        (0.0, 10.0),
        (10.0, 20.0),
        (20.0, 120.0),
    ]
    assert "evenly sampled" in result.structured_content["note"]


def _write_frames(video: uuid.UUID, colours: dict[int, tuple[int, int, int]]) -> None:
    from PIL import Image

    from framefound.config import get_settings

    for ts, colour in colours.items():
        target = get_settings().data_dir / f"frames/{video}/{ts}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 36), colour).save(target, "JPEG")


async def test_claude_can_look_at_a_moment_of_a_video(env: dict) -> None:  # noqa: F811
    """get_scenes' frame_url and search_media's at_ms are LAN links Claude
    Desktop cannot open; view_media(ts_ms) is how it actually sees a shot.
    25 s falls between samples (a transcript cue, say), so it gets the
    nearest one, at 21 s, and is told which moment that is."""
    video = await _video(env)
    _write_frames(video, {0: (0, 0, 0), 21000: (200, 30, 30), 40000: (30, 200, 30)})
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("view_media", {"id": str(video), "ts_ms": 25000})
    assert not result.is_error, result.content
    image = next(block for block in result.content if block.type == "image")
    assert image.mime_type == "image/jpeg"
    import base64
    import io

    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(image.data))) as picture:
        red, green, _ = picture.convert("RGB").getpixel((5, 5))
    assert red > 150 and green < 80, "the 21 s frame, not the 40 s one"
    text = " ".join(b.text for b in result.content if b.type == "text")
    assert "21.00s" in text and "ts_ms 21000" in text


async def test_a_moment_needs_a_video_with_frames(env: dict) -> None:  # noqa: F811
    bare = await _video_with_frames(env, [])
    photo = env["ids"]["barn-wide.jpg"]
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        unsampled = await session.call_tool("view_media", {"id": str(bare), "ts_ms": 1000})
        still = await session.call_tool("view_media", {"id": str(photo), "ts_ms": 0})
    assert unsampled.is_error and "No frames have been sampled" in unsampled.content[0].text
    assert still.is_error and "ts_ms is for a moment in a video" in still.content[0].text


async def test_every_at_ms_search_hands_back_opens_in_view_media(
    env: dict,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A photo's single Frame row sits at ts_ms 0, so a visual hit on one
    came back with at_ms 0; passed to view_media as the guidance says, it
    was refused ("ts_ms is for a moment in a video"). CLIP is not loaded in
    tests, so the visual search is stood in for — fusion and the MCP tool
    run for real."""
    from framefound.api.v1 import search as search_api

    video = await _video(env)
    _write_frames(video, {ts: (200, 30, 30) for ts in (0, 4000, 21000, 40000)})
    photo = env["ids"]["barn-wide.jpg"]
    async with env["factory"]() as db:
        library_id = (await db.execute(Library.__table__.select())).first().id

    async def visual(*_args: object, **_kwargs: object) -> tuple[list[search_api.VisualHit], str]:
        return [
            search_api.VisualHit(
                asset_id=photo,
                filename="barn-wide.jpg",
                library_id=library_id,
                media_type="image",
                ts_ms=0,
                similarity=0.31,
            ),
            search_api.VisualHit(
                asset_id=video,
                filename="DJI_0042.MP4",
                library_id=library_id,
                media_type="video",
                ts_ms=21000,
                similarity=0.29,
            ),
        ], "ready"

    monkeypatch.setattr(search_api, "_visual_search", visual)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        found = await session.call_tool("search_media", {"query": "barn", "verbose": True})
        hits = {h["id"]: h for h in found.structured_content["hits"]}
        assert hits[str(photo)]["at_ms"] is None
        assert hits[str(video)]["at_ms"] is not None
        for hit in hits.values():
            if hit["at_ms"] is not None:
                seen = await session.call_tool(
                    "view_media", {"id": hit["id"], "ts_ms": hit["at_ms"]}
                )
                assert not seen.is_error, (hit["filename"], seen.content)


async def test_scene_and_search_guidance_points_at_view_media(env: dict) -> None:  # noqa: F811
    from framefound.mcp.server import INSTRUCTIONS

    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    assert "ts_ms" in tools["view_media"].input_schema["properties"]
    assert "view_media" in (tools["get_scenes"].description or "")
    assert "view_media" in (tools["search_media"].description or "")
    assert "view_media(id, ts_ms" in INSTRUCTIONS


async def test_nothing_promises_a_final_cut_timeline(env: dict) -> None:  # noqa: F811
    """The rough cut is FCP7 xmeml. Final Cut Pro 10/11 reads only FCPXML,
    so a Final Cut mention that does not say "converter" sends the operator
    to an import that fails. Checked where Claude reads it and where the
    operator does."""
    from pathlib import Path

    from framefound.mcp.server import INSTRUCTIONS

    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        tools = (await session.list_tools()).tools
    docs = next(
        (
            parent / "docs" / "mcp.md"
            for parent in Path(__file__).resolve().parents
            if (parent / "docs" / "mcp.md").is_file()
        ),
        None,
    )
    sources = {"INSTRUCTIONS": INSTRUCTIONS}
    # The VM gate copies apps/server alone; the docs are checked where the
    # whole repository is (locally and in CI).
    if docs is not None:
        sources["docs/mcp.md"] = docs.read_text(encoding="utf-8")
    sources.update({tool.name: tool.description or "" for tool in tools})
    for name, text in sources.items():
        flat = " ".join(text.split())
        at = flat.find("Final Cut")
        while at != -1:
            nearby = flat[max(0, at - 120) : at + 160]
            assert "convert" in nearby, f"{name}: {nearby!r}"
            at = flat.find("Final Cut", at + 1)


async def test_a_scene_picture_link_opens_without_a_session(env: dict) -> None:  # noqa: F811
    """The signed frame URL must carry `ts` and still verify."""
    video = await _video(env)
    async with env["factory"]() as db:
        frame = (await db.execute(Frame.__table__.select().where(Frame.ts_ms == 21000))).first()
    from framefound.config import get_settings

    target = get_settings().data_dir / frame.relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8jpeg")
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("get_scenes", {"id": str(video)})
    url = result.structured_content["scenes"][1]["frame_url"]
    path = url[url.index("/api/v1/") :]
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://t"
    ) as fresh:
        response = await fresh.get(path)
    assert response.status_code == 200


async def test_a_rough_cut_opens_on_the_mac(env: dict) -> None:  # noqa: F811
    video = await _video(env)
    photo = env["ids"]["barn-wide.jpg"]
    cuts = [
        {"id": str(video), "in_s": 12.0, "out_s": 15.5, "note": "Look at that barn."},
        {"id": str(photo), "out_s": 3},
        {"id": str(video), "in_s": 40.0, "out_s": 46.0},
    ]
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool(
            "build_rough_cut", {"title": "Ledgerock 60", "cuts": cuts, "profile": "Mac Mini"}
        )
    assert not result.is_error, result.content
    body = result.structured_content
    assert body["cuts"] == 3 and body["duration_s"] == pytest.approx(12.5)
    root = ET.fromstring(body["xml"].split("\n", 2)[2])  # noqa: S314 - our own output
    track = root.find("./sequence/media/video/track")
    items = track.findall("clipitem")
    assert [int(i.find("start").text) for i in items] == [0, 104, 194]
    urls = [p.text for p in root.iter("pathurl")]
    assert urls[0] == "file://localhost/Volumes/Intel/Ledgerock/DJI_0042.MP4"
    # The second use of the drone clip refers back to the first <file>.
    assert items[2].find("file").attrib == {"id": "file-1"}
    # Photo and clip share the library the Mac profile covers.
    assert body["unmapped"] == []


async def test_a_rough_cut_refuses_an_out_before_its_in(env: dict) -> None:  # noqa: F811
    video = await _video(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool(
            "build_rough_cut",
            {"title": "x", "cuts": [{"id": str(video), "in_s": 30, "out_s": 10}]},
        )
    assert result.is_error
    assert "must be after" in result.content[0].text


async def test_search_media_can_answer_with_mac_paths(env: dict) -> None:  # noqa: F811
    await _video(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool(
            "search_media",
            {"query": "barn", "profile": "Mac Mini", "media_type": "image", "verbose": True},
        )
    hits = result.structured_content["hits"]
    assert hits and all(h["media_type"] == "image" for h in hits)
    assert all(h["local_path"].startswith("/Volumes/Intel/") for h in hits)


def test_a_sequence_keeps_ntsc_timing_and_audio() -> None:
    clip = Clip(
        name="a.mov", path="Z:\\Shoot\\a.mov", duration_s=100, fps=29.97, width=1920, height=1080
    )
    xml = build_sequence("T", [Cut(clip, 10.0, 20.0)])
    root = ET.fromstring(xml.split("\n", 2)[2])  # noqa: S314 - our own output
    item = root.find("./sequence/media/video/track/clipitem")
    assert item.find("in").text == "299"  # 10 s at 29.97 is frame 299, not 300
    assert item.find("./rate/ntsc").text == "TRUE"
    assert len(root.findall("./sequence/media/audio/track/clipitem")) == 2
    assert root.find(".//pathurl").text == "file://localhost/Z%3A/Shoot/a.mov"


async def test_selects_mark_the_words_and_string_them_out(env: dict) -> None:  # noqa: F811
    """Claude's route to a selects reel: moments in, one document out, at
    the Mac's paths, with the sentence as the marker comment."""
    video = await _video(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool(
            "build_selects",
            {
                "title": "Ledgerock",
                # The short id the compact answers print, as Claude has it.
                "moments": [{"id": compact.short_id(video), "at_s": 30.5}],
                "profile": "Mac Mini",
            },
        )
        wrong = await session.call_tool(
            "build_selects", {"title": "x", "moments": [{"id": str(video)}], "profile": "Nope"}
        )
    assert not result.is_error, result.content
    body = result.structured_content
    assert body["filename"] == "Ledgerock selects.xml"
    assert [s["reason"] for s in body["selects"]] == ["Starting bid is ten."]
    assert (body["selects"][0]["in_s"], body["selects"][0]["out_s"]) == (30.0, 33.25)
    root = ET.fromstring(body["xml"].split("\n", 2)[2])  # noqa: S314 - our own output
    assert root.find("./project/children/bin/children/clip/marker/comment").text == (
        "Starting bid is ten."
    )
    assert [p.text for p in root.iter("pathurl")] == [
        "file://localhost/Volumes/Intel/Ledgerock/DJI_0042.MP4"
    ]
    assert body["unmapped"] == [] and body["offline"] == []
    assert wrong.is_error and "No path profile called" in wrong.content[0].text
