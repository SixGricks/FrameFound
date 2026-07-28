"""Image thumbnails and previews via Pillow.

EXIF orientation is honored; anything Pillow can't decode (exotic RAW,
corrupt files) raises ThumbnailError and the derivative records the failure —
never trust file contents.
"""

from pathlib import Path


class ThumbnailError(RuntimeError):
    pass


def make_image_derivative(src: Path, dst: Path, max_edge: int) -> tuple[int, int]:
    """Resize down to max_edge (longest side), save as WebP. Returns (w, h)."""
    try:
        from PIL import Image, ImageOps
    except ImportError as err:  # pragma: no cover - media extra always installed
        raise ThumbnailError("Image support is not installed") from err
    try:
        with Image.open(src) as opened:
            img: Image.Image = ImageOps.exif_transpose(opened) or opened
            img.thumbnail((max_edge, max_edge))
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            img.save(dst, "WEBP", quality=82, method=4)
            return img.width, img.height
    except ThumbnailError:
        raise
    except Exception as err:
        raise ThumbnailError("The image could not be decoded") from err
