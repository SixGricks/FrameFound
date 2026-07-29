"""The low-space guard: previews must never fill the disk out from under
the database."""

from pathlib import Path

import pytest

from framefound.config import get_settings
from framefound.processing.derivatives import OutOfSpace, ensure_space, free_gb


def test_free_gb_reports_a_number(tmp_path: Path) -> None:
    value = free_gb(tmp_path)
    assert value is not None and value > 0


def test_free_gb_on_missing_path_is_none() -> None:
    assert free_gb(Path("/definitely/not/a/real/path/xyz")) is None


def test_ensure_space_passes_with_headroom(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FRAMEFOUND_MIN_FREE_GB", "0.001")
    get_settings.cache_clear()
    try:
        ensure_space(tmp_path)  # must not raise
    finally:
        get_settings.cache_clear()


def test_ensure_space_raises_when_below_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A floor larger than any real disk forces the guard to trip.
    monkeypatch.setenv("FRAMEFOUND_MIN_FREE_GB", "999999")
    get_settings.cache_clear()
    try:
        with pytest.raises(OutOfSpace, match="free for previews"):
            ensure_space(tmp_path)
    finally:
        get_settings.cache_clear()


def test_guard_message_is_user_readable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FRAMEFOUND_MIN_FREE_GB", "999999")
    get_settings.cache_clear()
    try:
        with pytest.raises(OutOfSpace) as err:
            ensure_space(tmp_path)
        message = str(err.value)
        assert "GB free" in message and "pauses below" in message
        assert "Traceback" not in message
    finally:
        get_settings.cache_clear()
