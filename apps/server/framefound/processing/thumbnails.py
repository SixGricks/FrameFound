"""Image thumbnails and previews.

EXIF orientation is honored; anything that cannot be decoded raises
ThumbnailError and the derivative records the failure — never trust file
contents.

Pillow is the default: it handles orientation, odd modes, and colour profiles
well. But it decodes the entire raster into memory before anything can be
resized, and `exif_transpose` copies it again — so a 2 GB 16-bit TIFF export
needs several gigabytes of RAM to produce a 512-pixel thumbnail, and takes the
worker down with it (found in deployment on drone TIFF exports). Above a size
threshold FFmpeg scales during decode instead, and Pillow only ever sees the
small result.
"""

import tempfile
from pathlib import Path


class ThumbnailError(RuntimeError):
    pass


# Above this, Pillow's decode-then-copy is the wrong tool. Set well below the
# smallest worker memory limit: decoded size is a multiple of file size, and
# the resize needs headroom on top of that.
LARGE_IMAGE_BYTES = 192 * 1024 * 1024

# Pillow's own decompression-bomb ceiling is ~178 Mpx. Drone panoramas already
# reach 50 Mpx and stitched work goes higher, so the limit is raised — but not
# removed, because the guard is still worth having against hostile input.
MAX_PIXELS = 500_000_000


def _encode_webp(src: Path, dst: Path, max_edge: int) -> tuple[int, int]:
    """Pillow path. Safe only when the source fits in memory."""
    from PIL import Image, ImageOps

    with Image.open(src) as opened:
        # For JPEG this decodes at a reduced size directly — a large saving on
        # 8000-pixel camera originals. A no-op for formats that can't do it.
        opened.draft("RGB", (max_edge, max_edge))
        img: Image.Image = ImageOps.exif_transpose(opened) or opened
        img.thumbnail((max_edge, max_edge))
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        img.save(dst, "WEBP", quality=82, method=4)
        return img.width, img.height


def _via_ffmpeg(src: Path, dst: Path, max_edge: int) -> tuple[int, int]:
    """Downscale with FFmpeg first, then encode the small result with Pillow.

    Two steps rather than letting FFmpeg write WebP directly, because its
    .webp output goes through libwebp_anim and fails on some high-resolution
    sources. This keeps one WebP encoder for every derivative.
    """
    from framefound.processing.ffmpeg import FfmpegError, downscale_still

    with tempfile.TemporaryDirectory() as tmp:
        intermediate = Path(tmp) / "scaled.jpg"
        try:
            downscale_still(src, intermediate, max_edge)
        except FfmpegError as err:
            raise ThumbnailError(str(err)) from err
        return _encode_webp(intermediate, dst, max_edge)


def make_image_derivative(src: Path, dst: Path, max_edge: int) -> tuple[int, int]:
    """Resize down to max_edge (longest side), save as WebP. Returns (w, h)."""
    try:
        from PIL import Image
    except ImportError as err:  # pragma: no cover - media extra always installed
        raise ThumbnailError("Image support is not installed") from err
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS

    try:
        oversized = src.stat().st_size > LARGE_IMAGE_BYTES
    except OSError as err:
        raise ThumbnailError("The image could not be read") from err

    if oversized:
        return _via_ffmpeg(src, dst, max_edge)
    try:
        return _encode_webp(src, dst, max_edge)
    except Exception:
        # Pillow refuses plenty of real files — exotic RAW, unusual TIFF
        # layouts, decompression-bomb trips. FFmpeg reads many of them, so a
        # second attempt beats an immediate failure.
        return _via_ffmpeg(src, dst, max_edge)
