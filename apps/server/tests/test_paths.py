from pathlib import Path

import pytest

from framefound.scanner.paths import PathValidationError, safe_join, validate_library_root


def test_valid_root_inside_media_root(tmp_path: Path) -> None:
    lib = tmp_path / "libraries" / "photos"
    lib.mkdir(parents=True)
    assert validate_library_root(str(lib), tmp_path) == lib.resolve()


def test_media_root_itself_is_valid(tmp_path: Path) -> None:
    assert validate_library_root(str(tmp_path), tmp_path) == tmp_path.resolve()


def test_relative_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathValidationError, match="absolute"):
        validate_library_root("relative/path", tmp_path)


def test_outside_media_root_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent
    with pytest.raises(PathValidationError, match="media root"):
        validate_library_root(str(outside), tmp_path)


def test_traversal_rejected(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    sneaky = str(lib / ".." / "..")
    with pytest.raises(PathValidationError):
        validate_library_root(sneaky, tmp_path)


def test_nonexistent_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathValidationError, match="not exist"):
        validate_library_root(str(tmp_path / "ghost"), tmp_path)


def test_file_rejected(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(PathValidationError, match="folder"):
        validate_library_root(str(f), tmp_path)


def test_safe_join_normal(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    assert safe_join(tmp_path, "sub/a.jpg") == (tmp_path / "sub" / "a.jpg").resolve()


def test_safe_join_escape_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathValidationError, match="escapes"):
        safe_join(tmp_path, "../outside.jpg")
