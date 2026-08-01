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


# --- what external tools actually emit ------------------------------------
#
# 872 metadata extractions failed on the reference deployment with
# "invalid input for query argument $16: 'undef' (must be real number, not
# str)". ExifTool writes the literal string `undef` for a tag it could not
# read, even under -n, and it went straight into a FLOAT column. The 230
# assets involved then sat in `processing` for three days looking like work in
# flight.


def test_exiftools_undef_never_reaches_a_float_column() -> None:
    """The exact production failure, as a unit."""
    from framefound.processing.probe import coerce_fields

    assert "aperture_f" not in coerce_fields({"aperture_f": "undef"})


@pytest.mark.parametrize("junk", ["undef", "", "  ", "n/a", None, "not a number"])
def test_unusable_numbers_are_dropped_rather_than_defaulted(junk: object) -> None:
    """Dropped, not zeroed. A missing aperture is honest; an aperture of 0.0 is
    a lie that will be believed by everything downstream."""
    from framefound.processing.probe import coerce_fields

    assert coerce_fields({"aperture_f": junk}) == {}


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan", float("inf"), float("nan")])
def test_infinities_and_nan_are_refused(bad: object) -> None:
    """Postgres will accept these in a float column, and then every consumer
    has to cope with a duration that is not a number."""
    from framefound.processing.probe import coerce_fields

    assert coerce_fields({"duration_s": bad}) == {}


def test_numbers_arriving_as_strings_are_kept() -> None:
    """ffprobe returns everything as a string; refusing those would throw away
    the metadata this whole stage exists to collect."""
    from framefound.processing.probe import coerce_fields

    out = coerce_fields({"duration_s": "12.5", "width": "1920", "bitrate": "48000000"})
    assert out == {"duration_s": 12.5, "width": 1920, "bitrate": 48000000}


def test_a_float_valued_integer_field_is_truncated_not_rejected() -> None:
    """ffprobe reports some integer fields as floats."""
    from framefound.processing.probe import coerce_fields

    assert coerce_fields({"width": 1920.0})["width"] == 1920


def test_booleans_are_not_numbers() -> None:
    """True would otherwise become 1 and silently pass as a real measurement."""
    from framefound.processing.probe import coerce_fields

    assert coerce_fields({"iso": True}) == {}


def test_string_sentinels_do_not_become_camera_names() -> None:
    from framefound.processing.probe import coerce_fields

    assert coerce_fields({"camera_make": "undef", "camera_model": "  "}) == {}


def test_unmapped_fields_pass_through_untouched() -> None:
    """captured_at is a datetime and has no coercion; it must survive."""
    from datetime import UTC, datetime

    from framefound.processing.probe import coerce_fields

    when = datetime(2026, 7, 31, tzinfo=UTC)
    assert coerce_fields({"captured_at": when})["captured_at"] == when
