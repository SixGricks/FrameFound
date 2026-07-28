from pathlib import Path

import pytest

from framefound.processing.thumbnails import ThumbnailError, make_image_derivative


def test_thumbnail_resizes_and_converts(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    src = tmp_path / "big.png"
    pil.new("RGB", (4000, 3000), color=(200, 40, 40)).save(src)
    dst = tmp_path / "thumb.webp"
    width, height = make_image_derivative(src, dst, max_edge=512)
    assert (width, height) == (512, 384)
    assert dst.stat().st_size > 0
    with pil.open(dst) as out:
        assert out.format == "WEBP"


def test_small_image_not_upscaled(tmp_path: Path) -> None:
    pil = pytest.importorskip("PIL.Image")
    src = tmp_path / "small.png"
    pil.new("RGB", (100, 80)).save(src)
    dst = tmp_path / "thumb.webp"
    assert make_image_derivative(src, dst, max_edge=512) == (100, 80)


def test_garbage_raises_thumbnail_error(tmp_path: Path) -> None:
    pytest.importorskip("PIL.Image")
    src = tmp_path / "fake.jpg"
    src.write_bytes(b"not an image at all")
    with pytest.raises(ThumbnailError):
        make_image_derivative(src, tmp_path / "out.webp", max_edge=512)
