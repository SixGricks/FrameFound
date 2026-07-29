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


def test_oversized_images_never_reach_pillow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the size threshold: a file too large to decode must
    not be handed to Pillow, because that is what killed the worker."""
    from framefound.processing import thumbnails

    src = tmp_path / "huge.tif"
    src.write_bytes(b"\x00" * 16)
    monkeypatch.setattr(thumbnails, "LARGE_IMAGE_BYTES", 8)

    opened: list[Path] = []
    monkeypatch.setattr(thumbnails, "_encode_webp", lambda s, d, e: (opened.append(s), (4, 4))[1])
    scaled: list[Path] = []

    def fake_downscale(source: Path, dest: Path, max_edge: int) -> None:
        scaled.append(source)
        dest.write_bytes(b"jpeg")

    monkeypatch.setattr("framefound.processing.ffmpeg.downscale_still", fake_downscale)

    assert make_image_derivative(src, tmp_path / "out.webp", max_edge=512) == (4, 4)
    assert scaled == [src], "FFmpeg should have handled the oversized source"
    assert opened and opened[0] != src, "Pillow must only see the scaled intermediate"


def test_ffmpeg_failure_surfaces_as_thumbnail_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from framefound.processing import thumbnails
    from framefound.processing.ffmpeg import FfmpegError

    src = tmp_path / "huge.tif"
    src.write_bytes(b"\x00" * 16)
    monkeypatch.setattr(thumbnails, "LARGE_IMAGE_BYTES", 8)

    def boom(source: Path, dest: Path, max_edge: int) -> None:
        raise FfmpegError("The image could not be decoded")

    monkeypatch.setattr("framefound.processing.ffmpeg.downscale_still", boom)

    with pytest.raises(ThumbnailError):
        make_image_derivative(src, tmp_path / "out.webp", max_edge=512)


def test_still_timeout_scales_with_file_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flat two-minute timeout is what failed the 1 GB TIFFs: at the
    5.2 MB/s measured over SMB, reading one takes over three minutes.

    The assumed throughput is varied rather than the file size, so the test
    reads a real file and never has to fake `stat` — which pathlib itself
    calls, and which broke on a different Python than the one it was written
    on.
    """
    from framefound.processing import ffmpeg

    captured: list[int] = []
    src = tmp_path / "big.tif"
    src.write_bytes(b"\x00" * 4096)

    def fake_run(argv: list[str], timeout_s: int) -> None:
        captured.append(timeout_s)
        Path(argv[-1]).write_bytes(b"jpeg")

    monkeypatch.setattr(ffmpeg, "_run", fake_run)

    # Fast storage: reading the file is free, so the flat floor applies.
    monkeypatch.setattr(ffmpeg, "SLOW_STORAGE_BPS", 1024 * 1024 * 1024)
    ffmpeg.downscale_still(src, tmp_path / "out.jpg", 512)
    assert captured[-1] == ffmpeg.POSTER_TIMEOUT_S

    # Storage 128x slower makes the same file cost 32 seconds of reading.
    monkeypatch.setattr(ffmpeg, "SLOW_STORAGE_BPS", 128)
    ffmpeg.downscale_still(src, tmp_path / "out.jpg", 512)
    assert captured[-1] == ffmpeg.POSTER_TIMEOUT_S + 32

    # However slow the storage is claimed to be, the wait stays bounded.
    monkeypatch.setattr(ffmpeg, "SLOW_STORAGE_BPS", 1)
    ffmpeg.downscale_still(src, tmp_path / "out.jpg", 512)
    assert captured[-1] == ffmpeg.MAX_STILL_TIMEOUT_S


def test_a_signal_kill_is_reported_as_running_out_of_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found in deployment on 1-2 GB TIFF panoramas: ffmpeg is SIGKILLed by
    the OOM killer, so it exits negative with nothing on stderr. Calling that
    "could not be processed" sends the operator looking for a corrupt file."""
    import subprocess

    from framefound.processing import ffmpeg

    def killed(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, returncode=-9, stdout=b"", stderr=b"")

    monkeypatch.setattr(ffmpeg.subprocess, "run", killed)
    monkeypatch.setattr(ffmpeg.shutil, "which", lambda _name: "/usr/bin/ffmpeg")

    with pytest.raises(ffmpeg.FfmpegError, match="memory"):
        ffmpeg.downscale_still(tmp_path / "huge.tif", tmp_path / "out.jpg", 512)
