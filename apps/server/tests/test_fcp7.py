"""FCP7 XML export.

The failure mode this format punishes is timing. Everything is integer frames
against a declared timebase, and the NTSC rates are not the numbers they look
like — 29.97 is thirty frames in 1001/1000 seconds. Get it wrong and the file
imports cleanly and then drifts, which is worse than failing.
"""

import math
import xml.etree.ElementTree as ET

import pytest

from framefound.nle.fcp7 import (
    Clip,
    Cut,
    Marker,
    build_bin,
    build_sequence,
    effective_rate,
    file_url,
    frames,
    rate_for,
)


def test_pal_and_film_rates_are_not_ntsc() -> None:
    assert rate_for(25.0) == (25, False)
    assert rate_for(24.0) == (24, False)
    assert rate_for(50.0) == (50, False)


@pytest.mark.parametrize(
    ("fps", "expected"),
    [(29.97, (30, True)), (23.976, (24, True)), (59.94, (60, True)), (119.88, (120, True))],
)
def test_ntsc_rates_declare_the_integer_timebase_plus_a_flag(
    fps: float, expected: tuple[int, bool]
) -> None:
    """Writing `timebase 29` produces a file that imports and then drifts."""
    assert rate_for(fps) == expected


def test_a_probed_rate_with_float_noise_still_matches() -> None:
    # ffprobe reports 30000/1001 as 29.969999...
    assert rate_for(29.969999) == (30, True)
    assert rate_for(23.9760004) == (24, True)


def test_a_missing_rate_falls_back_rather_than_dividing_by_zero() -> None:
    assert rate_for(None) == (25, False)
    assert rate_for(0) == (25, False)


def test_frames_use_the_real_rate_not_the_declared_timebase() -> None:
    """The classic NTSC bug: ten seconds of 29.97 material spans 299 whole
    frames, not 300. Converting against the declared timebase of 30 gains a
    frame every ten seconds — nearly two seconds adrift over a half-hour
    auction."""
    assert frames(10.0, 29.97) == 299
    assert frames(10.0, 30.0) == 300


def test_frames_are_exact_at_integer_rates() -> None:
    assert frames(1.0, 25.0) == 25
    assert frames(4.0, 24.0) == 96


def test_a_marker_lands_on_the_frame_that_is_on_screen() -> None:
    # Half a second into 25 fps material is frame 12 — the one showing at that
    # instant. Never 12.5, and floor rather than round so the answer does not
    # depend on Python's half-to-even rule.
    assert frames(0.5, 25.0) == 12
    assert frames(0.999, 25.0) == 24
    assert frames(1.0, 25.0) == 25


def test_no_duration_is_zero_frames_not_an_error() -> None:
    assert frames(None, 25.0) == 0
    assert frames(-3.0, 25.0) == 0


def test_paths_with_spaces_are_quoted() -> None:
    """Real libraries are full of spaces; an unquoted path silently fails to
    resolve on import."""
    url = file_url("/media/gelco/07-07 - 1113 Upper Woods Rd/take 1.mp4")
    assert " " not in url
    assert url.startswith("file://localhost/media/gelco/")
    assert "%20" in url


def test_non_ascii_paths_survive() -> None:
    assert "%C3%A9" in file_url("/media/café/clip.mp4")


def test_windows_drive_paths_become_urls_premiere_can_resolve() -> None:
    """Path profiles hand Windows edit bays `Z:\\...` paths. Read as POSIX
    they were one opaque segment — `file://localhostZ%3A%5CIntel...` — and
    every clip imported offline."""
    url = file_url("Z:\\Intel\\2026\\take 1.mp4")
    assert url == "file://localhost/Z%3A/Intel/2026/take%201.mp4"


def test_unc_paths_carry_their_host() -> None:
    assert file_url("\\\\nas\\intel\\clip.mp4") == "file://nas/intel/clip.mp4"


def test_the_document_has_the_doctype_premiere_requires() -> None:
    xml = build_bin("Results", [Clip(name="a.mp4", path="/media/a.mp4")])
    assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert "<!DOCTYPE xmeml>" in xml


def test_a_bin_holds_every_clip_it_was_given() -> None:
    clips = [Clip(name=f"{i}.mp4", path=f"/media/{i}.mp4") for i in range(4)]
    root = ET.fromstring(build_bin("Power Broom", clips))
    assert root.findtext("bin/name") == "Power Broom"
    assert len(root.findall("bin/children/clip")) == 4


def test_a_clip_carries_its_path_rate_and_dimensions() -> None:
    xml = build_bin(
        "R",
        [
            Clip(
                name="DJI_0001.MP4",
                path="/media/gelco/DJI_0001.MP4",
                duration_s=12.0,
                fps=29.97,
                width=3840,
                height=2160,
            )
        ],
    )
    root = ET.fromstring(xml)
    clip = root.find("bin/children/clip")
    assert clip is not None
    assert clip.findtext("name") == "DJI_0001.MP4"
    assert clip.findtext("duration") == "359"  # 12s at 29.97
    assert clip.findtext("rate/timebase") == "30"
    assert clip.findtext("rate/ntsc") == "TRUE"
    assert clip.findtext("file/pathurl", "").endswith("/media/gelco/DJI_0001.MP4")
    assert clip.findtext("file/media/video/samplecharacteristics/width") == "3840"


def test_a_whole_clip_uses_in_and_out_of_minus_one() -> None:
    root = ET.fromstring(build_bin("R", [Clip(name="a.mp4", path="/media/a.mp4")]))
    clip = root.find("bin/children/clip")
    assert clip is not None
    assert clip.findtext("in") == "-1"
    assert clip.findtext("out") == "-1"


def test_markers_are_placed_at_frame_positions() -> None:
    """A transcript hit at 137.5 s must land on the frame, because on auction
    footage half a second is a different word."""
    xml = build_bin(
        "R",
        [
            Clip(
                name="auction.mp4",
                path="/media/auction.mp4",
                duration_s=600.0,
                fps=29.97,
                markers=[Marker(name="sold", seconds=137.5, comment="going once")],
            )
        ],
    )
    root = ET.fromstring(xml)
    marker = root.find("bin/children/clip/marker")
    assert marker is not None
    assert marker.findtext("name") == "sold"
    assert marker.findtext("comment") == "going once"
    assert marker.findtext("in") == str(frames(137.5, 29.97))
    assert marker.findtext("out") == "-1"


def test_several_markers_all_appear() -> None:
    clip = Clip(
        name="a.mp4",
        path="/media/a.mp4",
        duration_s=60.0,
        fps=25.0,
        markers=[Marker(name=f"hit {i}", seconds=float(i)) for i in range(5)],
    )
    root = ET.fromstring(build_bin("R", [clip]))
    assert len(root.findall("bin/children/clip/marker")) == 5


def test_silent_media_declares_no_audio_track() -> None:
    root = ET.fromstring(build_bin("R", [Clip(name="a.mp4", path="/media/a.mp4", has_audio=False)]))
    assert root.find("bin/children/clip/file/media/audio") is None


def test_an_empty_result_set_still_produces_a_valid_document() -> None:
    # Searching for something with no hits should not produce a broken file.
    root = ET.fromstring(build_bin("Nothing", []))
    assert root.findtext("bin/name") == "Nothing"
    assert root.findall("bin/children/clip") == []


def test_clip_ids_are_unique() -> None:
    clips = [Clip(name="same.mp4", path="/media/same.mp4") for _ in range(3)]
    root = ET.fromstring(build_bin("R", clips))
    ids = [c.get("id") for c in root.findall("bin/children/clip")]
    assert len(set(ids)) == 3


# --- sequences (build_rough_cut) ----------------------------------------------


def _clipitems(xml: str) -> list[ET.Element]:
    root = ET.fromstring(xml)  # noqa: S314 - our own output
    return list(root.iter("clipitem"))


def _spans(item: ET.Element) -> tuple[int, int, int, int]:
    return tuple(int(item.findtext(k) or 0) for k in ("start", "end", "in", "out"))  # type: ignore[return-value]


def _drone(fps: float = 29.97) -> Clip:
    return Clip(name="drone.mp4", path="/media/drone.mp4", duration_s=600, fps=fps)


@pytest.mark.parametrize(
    "cuts",
    [
        # The rough cut the MCP test builds: a quote, a 3 s photo, more drone.
        [
            Cut(_drone(), 12.0, 15.5),
            Cut(Clip(name="barn.jpg", path="/media/barn.jpg", has_audio=False), 0, 3, True),
            Cut(_drone(), 40.0, 46.0),
        ],
        # Short cuts at 25 fps: seven of these ten used to disagree.
        [Cut(_drone(25.0), 10.0 * i, 10.0 * i + 1.03) for i in range(10)],
        [Cut(_drone(23.976), 7.3, 9.1), Cut(_drone(23.976), 20.0, 21.7)],
    ],
    ids=["ntsc-with-a-still", "pal-short-cuts", "film"],
)
def test_every_clipitem_takes_as_much_source_as_timeline(cuts: list[Cut]) -> None:
    """At one rate, a clipitem whose out-in differs from end-start is a speed
    change nobody asked for: Resolve plays it at ~99%, Premiere moves the out
    point. The 29.97 still-then-drone case once gave 180 timeline frames over
    179 of source. Video and both audio tracks share the placement."""
    items = _clipitems(build_sequence("T", cuts))
    assert len(items) > len(cuts)  # audio clipitems are checked too
    for item in items:
        start, end, in_, out = _spans(item)
        assert out - in_ == end - start, item.get("id")
    video = [i for i in items if i.get("id", "").startswith("clipitem-v")]
    for before, after in zip(video, video[1:], strict=False):
        assert _spans(after)[0] == _spans(before)[1], "cuts butt up, no gap or overlap"


def test_placement_still_follows_running_seconds() -> None:
    """Deriving the source span must not bring back the drift that placing
    by summed frame counts had: cut N still starts at frames(sum of lengths)."""
    cuts = [Cut(_drone(25.0), 10.0 * i, 10.0 * i + 1.03) for i in range(10)]
    items = _clipitems(build_sequence("T", cuts))
    starts = [_spans(i)[0] for i in items if i.get("id", "").startswith("clipitem-v")]
    expected, elapsed = [], 0.0
    for cut in cuts:
        expected.append(frames(elapsed, 25.0))
        elapsed += cut.out_s - cut.in_s
    assert starts == expected


def test_a_different_rate_converts_by_the_real_rates() -> None:
    """23.976 drone in a 29.97 sequence: the source span is the timeline span
    scaled by 23.976/29.97, not the cut's length floored separately."""
    interview = Clip(name="int.mov", path="/media/int.mov", duration_s=600, fps=29.97)
    cuts = [Cut(interview, 5.0, 9.0), Cut(_drone(23.976), 7.3, 9.1)]
    items = [i for i in _clipitems(build_sequence("T", cuts)) if i.get("id") == "clipitem-v2"]
    start, end, in_, out = _spans(items[0])
    assert in_ == frames(7.3, 23.976)
    assert out - in_ == round((end - start) * effective_rate(23.976) / effective_rate(29.97))


def test_a_cut_to_the_clips_end_does_not_point_past_the_media() -> None:
    """Rounding the derived span can land one frame beyond the last frame;
    an out point past the media imports as a frame of offline."""
    short = Clip(name="s.mp4", path="/media/s.mp4", duration_s=10.0, fps=29.97)
    cuts = [Cut(short, 0.0, 0.5), Cut(short, 9.5, 10.0)]
    last = frames(10.0, 29.97)
    for item in _clipitems(build_sequence("T", cuts)):
        start, end, in_, out = _spans(item)
        assert out <= last
        assert out - in_ == end - start


def test_frames_a_clamp_trims_are_not_added_to_the_next_cut() -> None:
    """The clamp above pulled a cut's end back but the running clock still
    advanced by the full requested length, so the next cut began earlier
    than the clock said and took the trimmed frames as extra length — and
    since its source span follows its timeline span, extra *source* too.
    Three 23.976 b-roll cuts run to their clips' ends (what build_rough_cut
    does when out_s is omitted) put the closing quote two frames past
    where it was cut: out 6100 for a requested 6098."""
    interview = Clip(name="int.mov", path="/media/int.mov", duration_s=600, fps=29.97)
    broll = [
        Clip(name=f"b{i}.mp4", path=f"/media/b{i}.mp4", duration_s=d, fps=23.976)
        for i, d in enumerate([5.0, 5.21, 5.21])
    ]
    cuts = [
        Cut(interview, 100.0, 104.0),
        *(Cut(b, 1.0, b.duration_s or 0.0) for b in broll),
        Cut(interview, 200.0, 203.5),
    ]
    video = [i for i in _clipitems(build_sequence("T", cuts)) if i.get("id") == "clipitem-v5"]
    start, end, in_, out = _spans(video[0])
    # A cut never takes more timeline than its length rounds up to...
    assert end - start <= math.ceil(3.5 * effective_rate(29.97))
    # ...so the quote ends within a frame of where it was cut (flooring the
    # in point and the span separately can differ from flooring the out by one).
    assert out - frames(203.5, 29.97) <= 1
