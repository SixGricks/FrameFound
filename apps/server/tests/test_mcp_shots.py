"""Choosing shots through the connector (framefound/mcp/shots.py), and the
compact answers that replaced jq-trimming a 100-item find_media.

The windows are written straight into the database here, as the motion
pass would leave them; the analysis itself is tested in test_motion.py and
test_shots_ffmpeg.py.
"""

import base64
import io
import uuid
from datetime import UTC, datetime

import pytest
from test_mcp import connect, env, serving  # noqa: F401 - pytest fixture

from framefound.config import get_settings
from framefound.db.models import Asset, Frame, Library, ShotWindow
from framefound.mcp import compact
from framefound.mcp.shots import parse_shot
from framefound.ops.shots import parse_timeline

BASE = datetime(2026, 11, 2, 20, 0, tzinfo=UTC)


def _smooth(slow: float, normal: float, fast: float) -> dict[str, float]:
    return {"25": slow, "50": slow, "100": normal, "200": fast, "400": fast, "800": fast}


async def _course(env: dict, clips: int = 3) -> dict[str, uuid.UUID]:  # noqa: F811
    """Brookside drone clips with windows: a golden orbit that holds up
    slowed down, a jittery glide that only works sped up, a busy static
    shot of a machine, and a 29.97 clip whose 50% would duplicate frames."""
    from PIL import Image

    from framefound.processing import shots

    ids: dict[str, uuid.UUID] = {}
    async with env["factory"]() as db:
        library = (await db.execute(Library.__table__.select())).first()
        specs = [
            ("DJI_20261103051509_0639_D.MP4", 59.94, 21.0),
            ("DJI_20261103035806_0495_D.MP4", 59.94, 45.8),
            ("MVI_4945.MP4", 29.97, 125.0),
        ]
        for name, fps, length in specs[:clips]:
            folder = "Drone" if name.startswith("DJI") else "Ground"
            asset = Asset(
                library_id=library.id,
                relative_path=f"Brookside Country Club/2026/{folder}/{name}",
                filename=name,
                extension="mp4",
                media_type="video",
                size_bytes=10_000,
                mtime=BASE,
                fps=fps,
                duration_s=length,
                width=3840,
                height=2160,
                availability="online",
                captured_at=BASE,
            )
            db.add(asset)
            await db.flush()
            ids[name] = asset.id
        windows = [
            # 0639: one golden orbit, steady at every speed.
            (
                "DJI_20261103051509_0639_D.MP4",
                0,
                2000,
                14000,
                "orbit",
                "right",
                _smooth(0.92, 0.95, 0.97),
                "low",
                "golden hour",
                {"green": 2.1, "bunker": 1.6},
            ),
            # 0495: a glide whose jitter shows slowed down, not sped up.
            (
                "DJI_20261103035806_0495_D.MP4",
                0,
                21000,
                27000,
                "lateral glide",
                "left",
                _smooth(0.31, 0.62, 0.93),
                "low",
                "low sun",
                {"fairway": 1.8},
            ),
            (
                "DJI_20261103035806_0495_D.MP4",
                1,
                27000,
                31000,
                "orbit",
                "left",
                _smooth(0.4, 0.7, 0.9),
                "low",
                "golden hour",
                {"bunker": 1.2},
            ),
            # The ground clip: a static shot of an excavator at work.
            (
                "MVI_4945.MP4",
                0,
                10000,
                30000,
                "static",
                None,
                _smooth(0.99, 0.99, 0.99),
                "high",
                "mid sun",
                {"machine operating": 2.4, "trench": 1.3},
            ),
        ]
        for name, idx, start, end, move, direction, by, energy, light, subjects in windows:
            if name not in ids:
                continue
            asset_id = ids[name]
            fps = next(f for n, f, _ in specs if n == name)
            db.add(
                ShotWindow(
                    asset_id=asset_id,
                    idx=idx,
                    start_ms=start,
                    end_ms=end,
                    move=move,
                    direction=direction,
                    speed=0.08,
                    smooth=by["100"],
                    smooth_by_speed=by,
                    energy=energy,
                    energy_score=0.9 if energy == "high" else 0.1,
                    source_fps=fps,
                    features={"tx": -0.05},
                    sun_elevation=7.0,
                    light=light,
                    look={"sky": "direct", "warmth": 0.08},
                    framing={"view": "aerial", "angle": "oblique", "size": "wide"},
                    subjects=subjects,
                )
            )
            for ts_ms in shots.window_moments(start, end):
                target = get_settings().data_dir / shots.still_relpath(asset_id, ts_ms)
                target.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (480, 270), (200, 140, 60)).save(target, "JPEG")
        await db.commit()
    return ids


def _text(result) -> str:  # type: ignore[no-untyped-def]
    return "\n".join(b.text for b in result.content if getattr(b, "type", "") == "text")


async def test_find_shots_answers_in_compact_lines(env: dict) -> None:  # noqa: F811
    ids = await _course(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("find_shots", {"folder": "brookside"})
    assert not result.is_error, _text(result)
    assert result.structured_content is None
    text = _text(result)
    lines = text.splitlines()
    assert lines[0].startswith("4 of 4 shots")
    orbit = next(line for line in lines if "DJI_0639" in line)
    assert orbit.startswith(compact.short_id(ids["DJI_20261103051509_0639_D.MP4"]) + " DJI_0639 ")
    assert "2.0-14.0s orbit right smooth .95 energy low golden green,bunker" in orbit
    for leak in ("http", "/media", "sig=", "Brookside Country Club/"):
        assert leak not in text


async def test_a_paired_editing_proxy_is_not_offered_as_a_shot(env: dict) -> None:  # noqa: F811
    """A Premiere proxy analysed before it was paired (ADR-0021) is the
    original's footage again: find_shots leaves it out, like every listing."""
    from sqlalchemy import update

    ids = await _course(env)
    async with env["factory"]() as db:
        await db.execute(
            update(Asset)
            .where(Asset.id == ids["DJI_20261103051509_0639_D.MP4"])
            .values(proxy_of_id=ids["DJI_20261103035806_0495_D.MP4"])
        )
        await db.commit()
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("find_shots", {"folder": "brookside"})
    assert not result.is_error, _text(result)
    text = _text(result)
    assert text.splitlines()[0].startswith("3 of 3 shots")
    assert "DJI_0639" not in text


async def test_twenty_shots_fit_in_two_kilobytes(env: dict) -> None:  # noqa: F811
    """The prompt's budget: a 20-shot list under about 2 KB."""
    ids = await _course(env)
    async with env["factory"]() as db:
        for idx in range(2, 24):
            db.add(
                ShotWindow(
                    asset_id=ids["DJI_20261103051509_0639_D.MP4"],
                    idx=idx,
                    start_ms=20000 + idx * 1000,
                    end_ms=20900 + idx * 1000,
                    move="push-in",
                    direction="in",
                    smooth=0.9,
                    smooth_by_speed=_smooth(0.8, 0.9, 0.95),
                    energy="medium",
                    light="golden hour",
                    look={"sky": "direct"},
                    features={},
                    framing={},
                    subjects={"clubhouse": 2.0, "pond": 1.5, "cart path": 1.1},
                    source_fps=59.94,
                )
            )
        await db.commit()
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("find_shots", {"limit": 20})
    text = _text(result)
    assert text.splitlines()[0].startswith("20 of")
    assert len(text.encode("utf-8")) < 2048, len(text.encode("utf-8"))


async def test_smoothness_is_judged_at_the_speed_asked_for(env: dict) -> None:  # noqa: F811
    """0495's glide is jittery slowed down and fine sped up: the same
    filter keeps it for a 400% edit and drops it for a 50% one."""
    await _course(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        slow = await session.call_tool(
            "find_shots", {"folder": "Brookside", "min_smooth": 0.8, "at_speed": 50}
        )
        fast = await session.call_tool(
            "find_shots", {"folder": "Brookside", "min_smooth": 0.8, "at_speed": 400}
        )
    assert "DJI_0495" not in _text(slow)
    assert "DJI_0639" in _text(slow)
    assert "smooth@50% .92" in _text(slow)
    fast_lines = [line for line in _text(fast).splitlines() if "DJI_0495" in line]
    assert any("lateral-glide" in line and "smooth@400% .93" in line for line in fast_lines)


async def test_golden_orbits_that_hold_six_seconds_at_half_speed(env: dict) -> None:  # noqa: F811
    """The done-when query: smooth golden-hour orbits at Brookside that hold
    6 s at 50%. 0495's orbit is 4 s (8 s at 50%) but not smooth slowed
    down; 0639's is 12 s, steady, and from a 59.94 source, so its slow
    motion is real frames."""
    await _course(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool(
            "find_shots",
            {
                "folder": "Brookside",
                "moves": ["orbits"],
                "light": ["golden"],
                "at_speed": 50,
                "min_smooth": 0.8,
                "min_length_s": 6,
            },
        )
    lines = _text(result).splitlines()[1:]
    assert len(lines) == 1 and "DJI_0639" in lines[0]
    assert "->24.0s" in lines[0] and "dup" not in lines[0]


async def test_high_energy_machine_shots_that_work_sped_up(env: dict) -> None:  # noqa: F811
    await _course(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool(
            "find_shots",
            {"energy": ["high"], "subjects": ["machine"], "at_speed": 200, "min_smooth": 0.8},
        )
    lines = _text(result).splitlines()[1:]
    assert len(lines) == 1 and "MVI_4945" in lines[0] and "machine operating" in lines[0]


async def test_slow_motion_from_a_29_97_source_is_flagged(env: dict) -> None:  # noqa: F811
    await _course(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        flagged = await session.call_tool("find_shots", {"at_speed": 50})
        real = await session.call_tool("find_shots", {"at_speed": 50, "real_frames_only": True})
    ground = next(line for line in _text(flagged).splitlines() if "MVI_4945" in line)
    assert ground.endswith("dup")
    assert "MVI_4945" not in _text(real)


async def test_unknown_moves_and_speeds_are_refused_with_the_choices(env: dict) -> None:  # noqa: F811
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        move = await session.call_tool("find_shots", {"moves": ["barrel roll"]})
        speed = await session.call_tool("find_shots", {"at_speed": 33})
    assert move.is_error and "orbit" in _text(move)
    assert speed.is_error and "400" in _text(speed)


async def test_local_paths_only_when_a_computer_is_named(env: dict) -> None:  # noqa: F811
    from framefound.db.models import PathMapping

    await _course(env, clips=1)
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
        plain = await session.call_tool("find_shots", {})
        mac = await session.call_tool("find_shots", {"profile": "Mac Mini"})
    assert "/Volumes" not in _text(plain)
    line = _text(mac).splitlines()[1]
    assert line.endswith(
        "| /Volumes/GELCO/Brookside Country Club/2026/Drone/DJI_20261103051509_0639_D.MP4"
    )


async def test_verbose_gives_every_field(env: dict) -> None:  # noqa: F811
    await _course(env, clips=1)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("find_shots", {"verbose": True, "at_speed": 50})
    [shot] = result.structured_content["result"]
    assert shot["filename"] == "DJI_20261103051509_0639_D.MP4"
    assert shot["smooth"] == 0.92
    assert shot["speed_fit"]["50"] == {"length_s": 24.0, "real_frames": True}
    assert shot["framing"]["angle"] == "oblique"


async def test_a_cut_is_recorded_and_its_shots_flagged_used(env: dict) -> None:  # noqa: F811
    ids = await _course(env)
    orbit = compact.short_id(ids["DJI_20261103051509_0639_D.MP4"])
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        built = await session.call_tool(
            "build_rough_cut",
            {
                "title": "Brookside Quick 30",
                # The short id from a find_shots line works as it is.
                "cuts": [{"id": orbit, "in_s": 3.0, "out_s": 7.0}],
            },
        )
        assert not built.is_error, _text(built)
        assert built.structured_content["recorded"] is True
        again = await session.call_tool("find_shots", {"folder": "Brookside"})
        fresh = await session.call_tool("find_shots", {"folder": "Brookside", "exclude_used": True})
        cuts = await session.call_tool("recent_cuts", {"library": "Intel"})
    used = next(line for line in _text(again).splitlines() if "DJI_0639" in line)
    assert used.endswith(" used")
    assert "DJI_0639" not in _text(fresh)
    summary = _text(cuts)
    assert "“Brookside Quick 30” Intel" in summary
    assert "1 shots" in summary and "moves orbit 1" in summary and "light golden 1" in summary


async def test_a_throwaway_draft_need_not_be_recorded(env: dict) -> None:  # noqa: F811
    ids = await _course(env, clips=1)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        built = await session.call_tool(
            "build_rough_cut",
            {
                "title": "draft",
                "record": False,
                "cuts": [{"id": str(ids["DJI_20261103051509_0639_D.MP4"]), "in_s": 3, "out_s": 5}],
            },
        )
        cuts = await session.call_tool("recent_cuts", {})
    assert built.structured_content["recorded"] is False
    assert "No cuts recorded" in _text(cuts)


async def test_one_contact_sheet_instead_of_a_dozen_looks(env: dict) -> None:  # noqa: F811
    from PIL import Image

    await _course(env)
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        found = await session.call_tool("find_shots", {"folder": "Brookside"})
        lines = _text(found).splitlines()[1:]
        grid = await session.call_tool("contact_sheet", {"shots": lines, "title": "Brookside"})
        strip = await session.call_tool("contact_sheet", {"shots": lines[:2], "mode": "strip"})
    for result, tiles in ((grid, 4), (strip, 6)):
        assert not result.is_error, _text(result)
        image = next(b for b in result.content if b.type == "image")
        with Image.open(io.BytesIO(base64.b64decode(image.data))) as picture:
            assert 1500 <= picture.width <= 1700
        assert len([c for c in _text(result).splitlines() if c[:1].isdigit()]) == (
            tiles if result is grid else 2
        )
    assert "a (start), b (middle), c (end)" in _text(strip)


async def test_a_contact_sheet_of_moments_falls_back_to_sampled_frames(env: dict) -> None:  # noqa: F811
    from PIL import Image

    ids = await _course(env, clips=1)
    asset_id = ids["DJI_20261103051509_0639_D.MP4"]
    rel = f"frames/{asset_id}/40000.jpg"
    target = get_settings().data_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 36), (20, 180, 20)).save(target, "JPEG")
    async with env["factory"]() as db:
        db.add(Frame(asset_id=asset_id, ts_ms=40000, relative_path=rel))
        await db.commit()
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool(
            "contact_sheet", {"frames": [{"id": str(asset_id), "ts_ms": 40200}]}
        )
    assert not result.is_error, _text(result)
    assert "40.2s" in _text(result)


async def test_scenes_list_their_windows(env: dict) -> None:  # noqa: F811
    ids = await _course(env, clips=1)
    asset_id = ids["DJI_20261103051509_0639_D.MP4"]
    async with env["factory"]() as db:
        db.add(Frame(asset_id=asset_id, ts_ms=0, relative_path="f/0.jpg"))
        await db.commit()
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        result = await session.call_tool("get_scenes", {"id": str(asset_id)})
    [scene] = result.structured_content["scenes"]
    assert scene["windows"] == ["2.0-14.0s orbit right smooth .95 energy low golden green,bunker"]


async def test_find_media_is_compact_unless_asked(env: dict) -> None:  # noqa: F811
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        plain = await session.call_tool("find_media", {"tags": ["barn"], "match": "any"})
        chosen = await session.call_tool(
            "find_media", {"tags": ["barn"], "match": "any", "fields": ["id", "path"]}
        )
        bad = await session.call_tool("find_media", {"fields": ["colour"]})
        searched = await session.call_tool("search_media", {"query": "barn"})
    text = _text(plain)
    assert text.splitlines()[0].startswith("2 of 2")
    assert "http" not in text and "/2026/" not in text
    assert "barn-wide jpg" not in text and "barn-wide.jpg image 2026-06-01 tags:Barn" in text
    assert all("/2026/barn" in line for line in _text(chosen).splitlines()[1:])
    assert bad.is_error and "preview_url" in _text(bad)
    assert "[tag,name]" in _text(searched) or "[name,tag]" in _text(searched)
    assert "http" not in _text(searched)


async def test_every_tool_takes_the_short_id(env: dict) -> None:  # noqa: F811
    kitchen = env["ids"]["kitchen.jpg"]
    async with serving(env["app"]), connect(env["app"], env["reader"]) as session:
        found = await session.call_tool("get_media", {"id": compact.short_id(kitchen)})
        bogus = await session.call_tool("get_media", {"id": "zz12"})
    assert found.structured_content["id"] == str(kitchen)
    assert bogus.is_error


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("ab12cd34 DJI_0639 12.0-19.5s orbit left .91", ("ab12cd34", 12.0, 19.5)),
        # Numbers in the clip name are not the shot's time range.
        ("ab12cd34 2026-11-03_course 12.0-19.5s orbit", ("ab12cd34", 12.0, 19.5)),
        ("ab12cd34 Hole 7-8 tee 3.5s static", ("ab12cd34", 3.5, None)),
        ("ab12cd34@12.0-19.5", ("ab12cd34", 12.0, 19.5)),
        ("ab12cd34@40.2", ("ab12cd34", 40.2, None)),
    ],
)
def test_a_shot_line_gives_its_id_and_time_range(
    line: str, expected: tuple[str, float, float | None]
) -> None:
    assert parse_shot(line) == expected


def test_a_premiere_timeline_reads_back_into_pieces() -> None:
    """The shape of Brookside Rev 2: a 29.97 sequence whose clip items run at
    timebase 30 over 59.94 files; in/out are in the clip item's rate."""
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="5"><sequence id="sequence-1"><name>Brookside Rev 2</name>
<media><video><track>
<clipitem id="clipitem-v1"><name>DJI_20261103051509_0639_D.MP4</name>
<rate><timebase>30</timebase><ntsc>TRUE</ntsc></rate><start>0</start><end>128</end>
<in>41</in><out>169</out>
<file id="file-1"><name>DJI_20261103051509_0639_D.MP4</name>
<pathurl>file://localhost/Volumes/GELCO/Brookside%20Country%20Club/2026/Drone/DJI_20261103051509_0639_D.MP4</pathurl>
<rate><timebase>60</timebase><ntsc>TRUE</ntsc></rate></file></clipitem>
<clipitem id="clipitem-v2"><name>DJI_20261103051509_0639_D.MP4</name>
<rate><timebase>30</timebase><ntsc>TRUE</ntsc></rate><start>128</start><end>213</end>
<in>300</in><out>385</out><file id="file-1"/></clipitem>
</track></video></media></sequence></xmeml>"""
    name, pieces = parse_timeline(xml)
    assert name == "Brookside Rev 2"
    assert [p.name for p in pieces] == ["DJI_20261103051509_0639_D.MP4"] * 2
    assert pieces[0].in_s == pytest.approx(41 / (30000 / 1001))
    assert pieces[0].path.endswith(
        "Brookside Country Club/2026/Drone/DJI_20261103051509_0639_D.MP4"
    )
    assert pieces[1].path == pieces[0].path, "a repeat use refers back to the first <file>"
