"""Sky compositing: put a photographed sky behind a segmented foreground.

Deliberately not generative. The replacement sky is a real photograph the
operator supplied, scaled and placed; the foreground is relit toward the
sky's tone so the light agrees; the join is feathered. Deterministic in,
deterministic out — the property in the picture must still be the property,
which is both the aesthetic bar and, for real-estate use, close to an
ethical one: buyers are owed the house, not a hallucination of it.

Parameters (all live in the develop recipe, so versioning, batch apply and
export come for free):
  feather - edge softness as a fraction of image height
  shift   - vertical placement of the sky, -0.5..0.5 of the overscan
  relight - 0..1 strength of pulling the foreground toward the sky's tone
"""

from typing import Any

# Below this sky fraction, compositing is a silent no-op: the photograph is
# an interior and "replace the sky" has nothing to talk about. Silent matters
# for batch apply — one recipe over a whole listing must not wreck hallways.
MIN_SKY_FRACTION = 0.02

# The sky image is rendered taller than the frame by this factor so `shift`
# has somewhere to move it.
OVERSCAN = 1.5


def composite_sky(
    image: Any,
    mask: Any,
    sky_image: Any,
    feather: float = 0.02,
    shift: float = 0.0,
    relight: float = 0.4,
) -> Any:
    """Blend `sky_image` into `image` where `mask` says sky.

    `mask` is float 0..1 at the image's size. Returns a new image; the
    original is untouched. If there is no meaningful sky, returns the input.
    """
    import numpy as np
    from PIL import Image, ImageFilter

    if float(np.mean(mask)) < MIN_SKY_FRACTION:
        return image

    width, height = image.size

    # Cover-fit the sky with vertical overscan, then crop the window `shift`
    # selects. Skies tolerate stretch far better than buildings do.
    sky_h = round(height * OVERSCAN)
    sky = sky_image.convert("RGB").resize((width, sky_h), Image.Resampling.LANCZOS)
    y0 = round((sky_h - height) * min(1.0, max(0.0, 0.5 + shift)))
    sky = sky.crop((0, y0, width, y0 + height))

    # Feather the mask so the horizon is a blend, not a scissor line. The
    # blur radius scales with the image, which keeps the preview and the
    # full-resolution export looking alike.
    mask_img = Image.fromarray((np.clip(mask, 0.0, 1.0) * 255.0).astype("uint8"), "L")
    radius = max(1.0, feather * height)
    mask_img = mask_img.filter(ImageFilter.GaussianBlur(radius))
    soft = np.asarray(mask_img, dtype=np.float32)[..., None] / 255.0

    fg = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    bg = np.asarray(sky, dtype=np.float32) / 255.0

    if relight > 0.0:
        fg = _relight(fg, bg, soft, relight, np)

    out = fg * (1.0 - soft) + bg * soft
    out = np.clip(out, 0.0, 1.0)
    return Image.fromarray((out * 255.0 + 0.5).astype("uint8"), "RGB")


def _relight(fg: Any, bg: Any, soft: Any, strength: float, np: Any) -> Any:
    """Nudge the foreground toward the sky's colour temperature.

    A dusk sky over a noon-lit house is the tell that ruins every amateur
    sky swap. The correction is bounded channel gains toward the sky's mean
    tone — enough that the light agrees, never enough to repaint the house.
    """
    ground = soft[..., 0] < 0.5
    if not ground.any():
        return fg
    sky_mean = bg.reshape(-1, 3).mean(axis=0)
    tone = sky_mean / max(float(sky_mean.mean()), 1e-4)  # colour, not brightness
    # At most ±12% per channel at full strength.
    gains = 1.0 + (np.clip(tone, 0.7, 1.3) - 1.0) * 0.4 * strength
    out = fg.copy()
    out[ground] = fg[ground] * gains[None, :]
    return out
