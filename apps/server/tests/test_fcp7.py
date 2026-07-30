"""FCP7 XML export.

The failure mode this format punishes is timing. Everything is integer frames
against a declared timebase, and the NTSC rates are not the numbers they look
like — 29.97 is thirty frames in 1001/1000 seconds. Get it wrong and the file
imports cleanly and then drifts, which is worse than failing.
"""

import xml.etree.ElementTree as ET

import pytest

from framefound.nle.fcp7 import Clip, Marker, build_bin, file_url, frames, rate_for


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
