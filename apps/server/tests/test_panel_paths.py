"""Translating catalogue paths for a workstation.

This is the whole reason a panel is worth building. The catalogue stores
`/media/gelco/...`; a Windows edit bay sees `Z:\\`. Get it wrong and Premiere
imports a project full of offline media — which looks like a broken server, a
missing drive, or a corrupt project, and almost never like a path bug.

`PathMapping` has held these profiles since Milestone 2 and nothing consumed
them until now.
"""

import pytest

from framefound.api.v1.panel import _translate


def test_a_posix_share_becomes_a_windows_drive() -> None:
    assert _translate("/media/gelco/2026/a001.mp4", "/media/gelco", "Z:\\") == "Z:\\2026\\a001.mp4"


def test_separators_are_converted_for_windows() -> None:
    """A profile pointing at `Z:\\Intel` and a path full of forward slashes is
    a combination Premiere accepts and then fails to resolve — a much more
    confusing failure than a wrong drive letter."""
    out = _translate("/media/gelco/2026/june/a001.mp4", "/media/gelco", "Z:\\Intel")
    assert out == "Z:\\Intel\\2026\\june\\a001.mp4"
    assert "/" not in out


def test_a_unc_path_is_treated_as_windows() -> None:
    out = _translate("/media/gelco/a.mp4", "/media/gelco", "\\\\nas\\media")
    assert out == "\\\\nas\\media\\a.mp4"


def test_a_mac_mount_keeps_forward_slashes() -> None:
    assert (
        _translate("/media/gelco/2026/a001.mp4", "/media/gelco", "/Volumes/GELCO")
        == "/Volumes/GELCO/2026/a001.mp4"
    )


def test_a_trailing_separator_on_the_profile_does_not_double_up() -> None:
    """Operators type `Z:\\` and `Z:` and `/Volumes/GELCO/` interchangeably."""
    assert _translate("/media/g/a.mp4", "/media/g", "Z:\\") == "Z:\\a.mp4"
    assert _translate("/media/g/a.mp4", "/media/g", "/mnt/g/") == "/mnt/g/a.mp4"


def test_a_path_outside_the_root_is_left_alone() -> None:
    """Rewriting a path that does not start with the library root would invent
    a location. Better to hand back something obviously wrong than something
    plausibly wrong."""
    assert _translate("/somewhere/else/a.mp4", "/media/gelco", "Z:\\") == "/somewhere/else/a.mp4"


def test_nested_directories_survive() -> None:
    out = _translate("/media/g/2026/06/29/card1/a.mp4", "/media/g", "Z:\\")
    assert out == "Z:\\2026\\06\\29\\card1\\a.mp4"


@pytest.mark.parametrize("prefix", ["Z:\\", "Z:/", "C:\\Media"])
def test_drive_letters_are_recognised_however_they_are_written(prefix: str) -> None:
    out = _translate("/media/g/a/b.mp4", "/media/g", prefix)
    assert "/" not in out.replace(prefix, ""), f"{prefix} produced {out}"


def test_the_filename_is_never_lost() -> None:
    """The one property that must hold for every profile and every platform."""
    for prefix in ("Z:\\", "/Volumes/GELCO", "\\\\nas\\media", "/mnt/x/"):
        assert _translate("/media/g/deep/a001.mp4", "/media/g", prefix).endswith("a001.mp4")
