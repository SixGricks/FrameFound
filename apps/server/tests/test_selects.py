"""Send selects to Premiere (nle/selects.py, api/v1/selects.py).

Golden files under tests/golden/ are the whole XML a case produces. When the
writer changes on purpose, regenerate them and read the diff before
committing — a moved frame number is exactly what these exist to catch:

    FRAMEFOUND_UPDATE_GOLDEN=1 pytest tests/test_selects.py

The fixture is a GELCO shoot: a 29.97 interview with a superintendent and a
23.976 drone clip of the same day, plus the Premiere proxy of the interview.
"""

import os
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from test_scan import db  # noqa: F401 - fixture

from framefound.api.v1.selects import (
    TITLE_MAX,
    MomentIn,
    SelectsError,
    SelectsRequest,
    assemble,
    short_title,
)
from framefound.db.models import (
    Asset,
    Frame,
    Library,
    PathMapping,
    Transcript,
    TranscriptSegment,
)
from framefound.nle import selects as nle

GOLDEN = Path(__file__).parent / "golden"
SHOOT = "Pete Dye/2025 10 28"
# A Mac profile for the tests only. The real Mac mini's mount path is the
# operator's to enter on the Libraries page; nothing here assumes it.
MAC = "Mac mini (test)"
WINDOWS = "DJ's PC (Windows)"


def assert_golden(name: str, xml: str) -> None:
    path = GOLDEN / name
    if os.environ.get("FRAMEFOUND_UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(xml, encoding="utf-8", newline="\n")
    assert path.exists(), f"{name} missing: run with FRAMEFOUND_UPDATE_GOLDEN=1 and read it"
    assert xml == path.read_text(encoding="utf-8")


class Shoot:
    interview: uuid.UUID
    drone: uuid.UUID
    proxy: uuid.UUID
    photo: uuid.UUID


async def _shoot(db: AsyncSession) -> Shoot:  # noqa: F811
    library = Library(name="GELCO", root_path="/media/gelco")
    db.add(library)
    await db.flush()
    db.add_all(
        [
            PathMapping(
                library_id=library.id,
                profile_name=WINDOWS,
                platform="windows",
                mapped_prefix="Y:\\",
            ),
            PathMapping(
                library_id=library.id,
                profile_name=MAC,
                platform="macos",
                mapped_prefix="/Volumes/GELCO",
            ),
        ]
    )

    def video(path: str, **fields: object) -> Asset:
        fields.setdefault("processing_status", "ready")
        return Asset(
            library_id=library.id,
            relative_path=path,
            filename=path.rsplit("/", 1)[-1],
            extension=path.rsplit(".", 1)[-1].lower(),
            media_type="video",
            size_bytes=1,
            mtime=datetime(2025, 10, 28, tzinfo=UTC),
            availability="online",
            **fields,
        )

    interview = video(
        f"{SHOOT}/MVI_4513.MP4",
        fps=29.97,
        width=1920,
        height=1080,
        duration_s=120.0,
        audio_codec="aac",
        captured_at=datetime(2025, 10, 28, 16, 2, tzinfo=UTC),
    )
    drone = video(
        f"{SHOOT}/DJI_0042.MP4",
        fps=23.976,
        width=3840,
        height=2160,
        duration_s=60.0,
        captured_at=datetime(2025, 10, 28, 15, 0, tzinfo=UTC),
    )
    photo = Asset(
        library_id=library.id,
        relative_path="Pete Dye/2026/Photo/IMG_0007.JPG",
        filename="IMG_0007.JPG",
        extension="jpg",
        media_type="image",
        width=6000,
        height=4000,
        size_bytes=1,
        mtime=datetime(2026, 5, 1, tzinfo=UTC),
        availability="online",
        processing_status="ready",
    )
    db.add_all([interview, drone, photo])
    await db.flush()
    proxy = video(
        f"{SHOOT}/Proxies/MVI_4513_Proxy.mp4",
        fps=29.97,
        width=1024,
        height=540,
        duration_s=120.0,
        audio_codec="aac",
        captured_at=datetime(2026, 2, 12, 16, 25, tzinfo=UTC),
        proxy_of_id=interview.id,
        processing_status="paired_proxy",
    )
    db.add(proxy)
    transcript = Transcript(
        asset_id=interview.id, language="en", model_name="fake", segment_count=4
    )
    db.add(transcript)
    await db.flush()
    for start, end, text in (
        (10_000, 13_200, "The mainline was the first thing"),
        (13_400, 16_000, "we replaced this fall."),
        (30_000, 33_000, "Every head on the course is new."),
        (33_500, 36_000, "Coverage is better than it has ever been."),
    ):
        db.add(
            TranscriptSegment(transcript_id=transcript.id, start_ms=start, end_ms=end, text=text)
        )
    # extract_frames' shape: interval ticks around numbered scene changes.
    for ts, scene in ((0, None), (5_000, None), (21_000, 1), (30_000, None), (40_000, 2)):
        db.add(Frame(asset_id=drone.id, ts_ms=ts, scene_number=scene, relative_path=f"f/{ts}.jpg"))
    await db.commit()
    shoot = Shoot()
    shoot.interview, shoot.drone, shoot.proxy, shoot.photo = (
        interview.id,
        drone.id,
        proxy.id,
        photo.id,
    )
    return shoot


def _request(profile: str, *moments: MomentIn, **options: object) -> SelectsRequest:
    return SelectsRequest(
        title="Irrigation",
        profile=profile,
        query="mainline irrigation",
        moments=list(moments),
        **options,
    )  # type: ignore[arg-type]


def _sequence_items(xml: str) -> list[ET.Element]:
    root = ET.fromstring(xml.split("\n", 2)[2])  # noqa: S314 - our own output
    return root.findall("./project/children/sequence/media/video/track/clipitem")


# --- golden documents ------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "golden", "prefix"),
    [
        (WINDOWS, "selects_mixed_rates_windows.xml", "file://localhost/Y%3A/Pete%20Dye/"),
        (MAC, "selects_mixed_rates_mac.xml", "file://localhost/Volumes/GELCO/Pete%20Dye/"),
    ],
)
async def test_an_interview_and_drone_clip_of_different_rates(
    db: AsyncSession,  # noqa: F811
    profile: str,
    golden: str,
    prefix: str,
) -> None:
    shoot = await _shoot(db)
    result = await assemble(
        db,
        _request(
            profile,
            # The hit is the second half of the sentence; the select is all of it.
            MomentIn(id=str(shoot.interview), at_s=13.4, kind="speech"),
            # A 19 s shot, cut to 8 s around the matching frame.
            MomentIn(
                id=str(shoot.drone), at_s=30.0, kind="visual", reason="visual: mainline irrigation"
            ),
        ),
    )

    assert [(s.in_s, s.out_s) for s in result.selects] == [(10.0, 16.0), (26.0, 34.0)]
    assert result.selects[0].reason == "The mainline was the first thing we replaced this fall."
    assert result.selects[0].name == "Pete Dye 2025-10-28 – The mainline was the first thing…"
    assert result.unmapped == [] and result.offline == []
    doc = ET.fromstring(result.xml.split("\n", 2)[2])  # noqa: S314 - our own output
    urls = [el.text for el in doc.iter("pathurl")]
    assert urls and all(url.startswith(prefix) for url in urls)
    assert_golden(golden, result.xml)


async def test_handles_stop_at_the_start_and_end_of_the_clip(db: AsyncSession) -> None:  # noqa: F811
    shoot = await _shoot(db)
    result = await assemble(
        db,
        _request(
            MAC,
            MomentIn(id=str(shoot.drone), in_s=0.2, out_s=3.0, reason="opening reveal"),
            # The last scene runs 40-60 s; 8 s around 58 s slides back to 52-60.
            MomentIn(id=str(shoot.drone), at_s=58.0, kind="visual", reason="visual: green"),
        ),
    )

    items = _sequence_items(result.xml)
    first, last = items[0], items[1]
    assert first.findtext("in") == "0"  # 0.2 s - 1 s handle, clamped
    end_of_clip = int(60.0 * 24000 / 1001)  # 1438 frames at 23.976
    assert int(last.findtext("out") or 0) == end_of_clip
    assert int(last.findtext("in") or 0) == int(51.0 * 24000 / 1001)  # 52 s - 1 s handle
    assert_golden("selects_handles_clamped.xml", result.xml)


async def test_overlapping_moments_in_one_clip_become_one_select(db: AsyncSession) -> None:  # noqa: F811
    shoot = await _shoot(db)
    result = await assemble(
        db,
        _request(
            WINDOWS,
            MomentIn(id=str(shoot.interview), at_s=30.0, kind="speech"),
            MomentIn(id=str(shoot.interview), at_s=33.5, kind="speech"),
        ),
    )

    assert len(result.selects) == 1
    assert (result.selects[0].in_s, result.selects[0].out_s) == (30.0, 36.0)
    assert result.selects[0].reason == (
        "Every head on the course is new. / Coverage is better than it has ever been."
    )
    assert len(_sequence_items(result.xml)) == 1
    assert_golden("selects_overlap_merged.xml", result.xml)


async def test_a_photo_is_held_for_still_s(db: AsyncSession) -> None:  # noqa: F811
    shoot = await _shoot(db)
    result = await assemble(
        db, _request(WINDOWS, MomentIn(id=str(shoot.photo), reason="tag: masterpiece"), still_s=5.0)
    )

    item = _sequence_items(result.xml)[0]
    assert int(item.findtext("end") or 0) - int(item.findtext("start") or 0) == 125  # 5 s at 25
    assert result.selects[0].name.startswith("Pete Dye 2026 – tag: masterpiece")


# --- what never appears --------------------------------------------------------------


async def test_a_linked_proxy_never_appears(db: AsyncSession) -> None:  # noqa: F811
    shoot = await _shoot(db)

    chosen = await assemble(db, _request(WINDOWS, MomentIn(id=str(shoot.proxy), at_s=13.4)))
    searched = await assemble(db, SelectsRequest(title="4513", profile=WINDOWS, query="MVI_4513"))

    for result in (chosen, searched):
        assert "_Proxy" not in result.xml
        assert [s.filename for s in result.selects] == ["MVI_4513.MP4"]
    # Chosen through its proxy, the moment is still found in the original's words.
    assert chosen.selects[0].reason.startswith("The mainline was")


async def test_offline_and_unmapped_files_are_named(db: AsyncSession) -> None:  # noqa: F811
    shoot = await _shoot(db)
    drone = await db.get(Asset, shoot.drone)
    assert drone is not None
    drone.availability = "missing"
    await db.commit()

    result = await assemble(
        db,
        _request(
            "",
            MomentIn(id=str(shoot.interview), at_s=30.0),
            MomentIn(id=str(shoot.drone), at_s=30.0),
        ),
    )
    assert result.offline == ["DJI_0042.MP4"]
    assert result.unmapped == ["MVI_4513.MP4"]
    assert "import offline" in result.note and "not on the share" in result.note


async def test_nothing_to_select_says_why(db: AsyncSession) -> None:  # noqa: F811
    await _shoot(db)
    with pytest.raises(SelectsError):
        await assemble(db, SelectsRequest(title="x"))
    with pytest.raises(SelectsError):
        await assemble(db, SelectsRequest(title="x", moments=[MomentIn(id="not-an-id")]))


async def test_shooting_order_runs_by_capture_time(db: AsyncSession) -> None:  # noqa: F811
    shoot = await _shoot(db)
    moments = (
        MomentIn(id=str(shoot.photo), reason="photo"),
        MomentIn(id=str(shoot.interview), at_s=10.0),
        MomentIn(id=str(shoot.drone), at_s=30.0),
    )
    ranked = await assemble(db, _request(WINDOWS, *moments))
    dated = await assemble(db, _request(WINDOWS, *moments, order="captured"))

    assert [s.filename for s in ranked.selects] == ["IMG_0007.JPG", "MVI_4513.MP4", "DJI_0042.MP4"]
    # 15:00 drone, 16:02 interview, then the photo with only a year to go on.
    assert [s.filename for s in dated.selects] == ["DJI_0042.MP4", "MVI_4513.MP4", "IMG_0007.JPG"]


# --- the arithmetic ------------------------------------------------------------------


def _segs(*rows: tuple[float, float, str]) -> list[nle.Segment]:
    return [nle.Segment(a, b, t) for a, b, t in rows]


def test_a_soundbite_is_completed_to_its_sentence() -> None:
    segments = _segs(
        (0, 2, "So the"), (2.2, 5, "mainline went in"), (5.1, 7, "in October."), (7.2, 9, "Then")
    )
    assert nle.speech_span(segments, 1) == (0, 7, "So the mainline went in in October.")


def test_a_long_breath_is_capped() -> None:
    segments = _segs((0, 9, "and the"), (9.1, 18, "pipe was"), (18.1, 27, "everywhere."))
    start, end, _ = nle.speech_span(segments, 0, max_s=20.0)
    assert (start, end) == (0, 18)


def test_a_pause_ends_the_soundbite() -> None:
    segments = _segs((0, 2, "Right"), (6, 8, "the other thing"))
    assert nle.speech_span(segments, 0)[:2] == (0, 2)


def test_without_scene_detection_a_picture_is_three_seconds_either_side() -> None:
    assert nle.picture_span(10.0, [], False, 60.0) == (7.0, 13.0)
    assert nle.picture_span(1.0, [], False, 60.0) == (0.0, 4.0)


def test_a_short_scene_is_taken_whole() -> None:
    assert nle.picture_span(22.0, [(0, 21), (21, 25), (25, 60)], True, 60.0) == (21, 25)


def test_newer_shoots_take_the_date_from_the_camera() -> None:
    name = nle.select_name(
        "Pete Dye/2026/Drone/DJI_0101.MP4", datetime(2026, 4, 2, 9, tzinfo=UTC), "visual: bunker"
    )
    assert name == "Pete Dye 2026-04-02 – visual: bunker"


# --- the endpoints ---------------------------------------------------------------------


def test_the_web_and_the_panel_build_the_same_selects(client, app) -> None:  # type: ignore[no-untyped-def]
    """Send to Premiere on the search page, and the panel's route to the
    same builder, which needs the token's `export` scope."""
    from test_security_review_2026_11 import _in_app, _signed_in

    from framefound.auth import panel_tokens
    from framefound.db.models import User

    _signed_in(client)
    shoot: Shoot = _in_app(client, app, _shoot)
    body = {
        "title": "Irrigation",
        "profile": WINDOWS,
        "query": "mainline irrigation",
        "moments": [{"id": str(shoot.interview), "at_s": 13.4, "kind": "speech"}],
    }
    web = client.post("/api/v1/selects", json=body)
    assert web.status_code == 200, web.text
    assert web.json()["filename"] == "Irrigation selects.xml"

    async def mint(session, scopes):  # type: ignore[no-untyped-def]
        from sqlalchemy import select

        user = (await session.execute(select(User))).scalars().first()
        minted = await panel_tokens.mint(session, user=user, name="Premiere", scopes=scopes)
        await session.commit()
        return minted.plaintext

    reader = _in_app(client, app, lambda session: mint(session, ["read"]))
    exporter = _in_app(client, app, lambda session: mint(session, ["read", "export"]))
    client.cookies.clear()
    refused = client.post(
        "/api/v1/panel/export/selects", json=body, headers={"Authorization": f"Bearer {reader}"}
    )
    panel = client.post(
        "/api/v1/panel/export/selects", json=body, headers={"Authorization": f"Bearer {exporter}"}
    )
    assert refused.status_code == 403
    assert panel.status_code == 200, panel.text
    assert panel.json()["xml"] == web.json()["xml"]


def test_the_subclip_probe_fixture_is_well_formed() -> None:
    found = [
        folder / "fixtures" / "premiere" / "subclip-probe.xml"
        for folder in Path(__file__).resolve().parents
        if (folder / "fixtures" / "premiere" / "subclip-probe.xml").is_file()
    ]
    if not found:
        pytest.skip("run from a checkout: fixtures/ is outside apps/server")
    root = ET.parse(found[0]).getroot()  # noqa: S314 - our own fixture
    names = [clip.findtext("name") for clip in root.iter("clip")]
    assert names == ["C master MVI_4513", "A plain in-out 10-20s", "B subclip of C 10-20s"]


def test_a_long_search_still_makes_a_reel() -> None:
    """The search page titles the reel with the query, and a search may be
    200 characters where a title may be 120. The title is shortened at a word,
    never refused (Codex review, PR 41)."""
    query = "golfer lines up a long putt on the eighteenth green at sunset " * 3
    request = SelectsRequest(title=query.strip(), query=query.strip()[:200])
    assert len(request.title) <= TITLE_MAX
    assert request.title.endswith("…")
    assert query.startswith(request.title.removesuffix("…")), "cut at a word, not mid-word"
    assert short_title("  Ledge   Rock  ") == "Ledge Rock"
    assert short_title("x" * 300) == "x" * (TITLE_MAX - 1) + "…"
