"""Pixel-format helpers shared by everything that opens an image."""

from typing import Any

from PIL import Image

# Pillow's single-channel 16-bit (and 32-bit integer) modes.
_WIDE_GREY = ("I;16", "I;16B", "I;16L", "I;16N", "I")


def to_rgb8(image: Image.Image) -> Image.Image:
    """An 8-bit RGB copy, scaling a 16-bit grey instead of clipping it.

    Pillow's `convert("RGB")` on a 16-bit single-channel image — a 16-bit
    greyscale TIFF or PNG, modes I;16 and I — goes through mode L by
    clipping every value above 255, so the result is a white rectangle
    (review, 2026-09). The high byte is what an 8-bit grey means. 16-bit RGB
    TIFFs are opened by Pillow as 8-bit RGB already and pass straight
    through; everything else converts as before.
    """
    if image.mode in _WIDE_GREY:
        import numpy as np

        data: Any = np.asarray(image, dtype=np.uint32)
        if data.size and int(data.max()) > 255:
            data = data >> 8
        grey = Image.fromarray(np.clip(data, 0, 255).astype(np.uint8), "L")
        return grey.convert("RGB")
    return image.convert("RGB")
