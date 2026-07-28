"""Subtitle parsing/import — real-world files are messy, so tolerance matters."""

from pathlib import Path

from framefound.media.subtitles import (
    find_sidecar,
    import_sidecar,
    parse_subtitles,
)

SRT = """1
00:00:01,000 --> 00:00:04,500
Welcome to the auction preview.

2
00:00:04,500 --> 00:00:09,800
The <i>starting bid</i> will be announced on site.
"""

VTT = """WEBVTT

NOTE this block should be ignored

cue-1
00:01.000 --> 00:04.500
Short form timestamps without hours.
"""


def test_parse_srt_with_tags_and_numbering() -> None:
    segments = parse_subtitles(SRT)
    assert len(segments) == 2
    assert segments[0].start_s == 1.0 and segments[0].end_s == 4.5
    assert segments[1].text == "The starting bid will be announced on site."


def test_parse_vtt_short_timestamps_and_notes() -> None:
    segments = parse_subtitles(VTT)
    assert len(segments) == 1
    assert segments[0].start_s == 1.0
    assert segments[0].text == "Short form timestamps without hours."


def test_parse_tolerates_crlf_and_bom() -> None:
    messy = "﻿" + SRT.replace("\n", "\r\n")
    assert len(parse_subtitles(messy)) == 2


def test_parse_skips_malformed_cues() -> None:
    bad = "1\n00:00:05,000 --> 00:00:01,000\nBackwards timing.\n\n2\nno timing here\n"
    assert parse_subtitles(bad) == []


def test_multiline_cue_joined() -> None:
    text = "1\n00:00:00,000 --> 00:00:02,000\nFirst line\nsecond line\n"
    assert parse_subtitles(text)[0].text == "First line second line"


def test_find_sidecar_variants(tmp_path: Path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    assert find_sidecar(media) is None

    srt = tmp_path / "clip.srt"
    srt.write_text(SRT, encoding="utf-8")
    assert find_sidecar(media) == srt


def test_find_sidecar_double_extension(tmp_path: Path) -> None:
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    doubled = tmp_path / "clip.mp4.srt"
    doubled.write_text(SRT, encoding="utf-8")
    assert find_sidecar(media) == doubled


def test_import_sidecar_builds_result(tmp_path: Path) -> None:
    srt = tmp_path / "clip.srt"
    srt.write_text(SRT, encoding="utf-8")
    result = import_sidecar(srt)
    assert result is not None
    assert result.model_name == "sidecar/srt"
    assert result.duration_s == 9.8
    assert len(result.segments) == 2


def test_import_empty_sidecar_returns_none(tmp_path: Path) -> None:
    empty = tmp_path / "clip.srt"
    empty.write_text("\n\n", encoding="utf-8")
    assert import_sidecar(empty) is None
