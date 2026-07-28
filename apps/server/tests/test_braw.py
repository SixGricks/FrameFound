"""BRAW integration units: stream-arg parsing and graceful absence."""

from pathlib import Path

import pytest

from framefound.processing.braw import parse_dimensions, parse_stream_args, probe_braw
from framefound.processing.ffmpeg import FfmpegError

PRINTED = "-f rawvideo -pixel_format rgba -s 3840x2160 -r 60 -i pipe:0"


def test_parse_stream_args_roundtrip() -> None:
    args = parse_stream_args(PRINTED)
    assert args[:2] == ["-f", "rawvideo"]
    assert args[-2:] == ["-i", "pipe:0"]


def test_parse_stream_args_rejects_garbage() -> None:
    with pytest.raises(FfmpegError):
        parse_stream_args("this is not a stream description")


def test_parse_dimensions() -> None:
    fields = parse_dimensions(PRINTED)
    assert fields == {"width": 3840, "height": 2160, "fps": 60.0, "video_codec": "braw"}


def test_parse_dimensions_fractional_rate() -> None:
    assert parse_dimensions("-s 6144x3456 -r 23.976 -i pipe:0")["fps"] == 23.976


def test_probe_braw_empty_without_decoder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FRAMEFOUND_BRAW_DECODER", str(tmp_path / "missing" / "braw-decode"))
    from framefound.config import get_settings

    get_settings.cache_clear()
    try:
        assert probe_braw(tmp_path / "clip.braw") == {}
    finally:
        get_settings.cache_clear()
