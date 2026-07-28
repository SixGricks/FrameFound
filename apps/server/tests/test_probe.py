"""Probe helper tests (pure parsing; binary-dependent paths run on the VM)."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from framefound.processing.probe import _fps_from_ratio, _parse_capture_datetime, probe_pillow


def test_fps_ratio_parsing() -> None:
    assert _fps_from_ratio("30000/1001") == 29.97
    assert _fps_from_ratio("25/1") == 25.0
    assert _fps_from_ratio("0/0") is None
    assert _fps_from_ratio("garbage") is None


def test_exif_datetime_parsing() -> None:
    parsed = _parse_capture_datetime("2026:07:12 10:30:00")
    assert parsed == datetime(2026, 7, 12, 10, 30, tzinfo=UTC)
    assert _parse_capture_datetime("2026:07:12 10:30:00-04:00") is not None
    assert _parse_capture_datetime("not a date") is None


def test_probe_pillow_reads_dimensions(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    img_path = tmp_path / "img.png"
    pil.new("RGB", (320, 240)).save(img_path)
    assert probe_pillow(img_path) == {"width": 320, "height": 240}


def test_probe_pillow_tolerates_garbage(tmp_path: Path) -> None:
    pytest.importorskip("PIL.Image")
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"this is not an image")
    assert probe_pillow(bad) == {}
