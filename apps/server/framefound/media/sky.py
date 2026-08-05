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

    # Build the matte in three steps, all aimed at trees:
    #
    # 1. Erode before feathering. A matte that reaches the last classified
    #    pixel bleeds sky colour into leaf edges; pulling it in first means
    #    the feather blends *inward* from safely-sky territory.
    # 2. Feather, scaled with the image so preview and export look alike.
    # 3. Luminance keying: within the matte, pixels much darker than the
    #    sky's own brightness are branches and twigs, not sky — segmentation
    #    at 512 cannot resolve them, but their darkness gives them away.
    #    Suppressing the matte there keeps the tree's silhouette crisp over
    #    the new sky instead of haloed.
    fg = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    bg = np.asarray(sky, dtype=np.float32) / 255.0

    mask_img = Image.fromarray((np.clip(mask, 0.0, 1.0) * 255.0).astype("uint8"), "L")
    erode = max(3, (round(0.004 * height) * 2) + 1)  # odd kernel, ~0.4% of height
    mask_img = mask_img.filter(ImageFilter.MinFilter(erode))
    radius = max(1.0, feather * height)
    mask_img = mask_img.filter(ImageFilter.GaussianBlur(radius))
    soft = np.asarray(mask_img, dtype=np.float32) / 255.0

    luma = fg @ np.asarray([0.2126, 0.7152, 0.0722], dtype=np.float32)
    core = soft > 0.7
    sky_luma = float(np.median(luma[core])) if core.any() else 0.8
    # 1.0 at sky brightness, fading to 0 for pixels ~0.35 darker.
    darkness_key = np.clip((luma - (sky_luma - 0.35)) / 0.25, 0.0, 1.0)
    soft = (soft * darkness_key)[..., None]

    if relight > 0.0:
        fg = _relight(fg, bg, soft, relight, np)

    out = fg * (1.0 - soft) + bg * soft
    out = np.clip(out, 0.0, 1.0)
    return Image.fromarray((out * 255.0 + 0.5).astype("uint8"), "RGB")


def _relight(fg: Any, bg: Any, soft: Any, strength: float, np: Any) -> Any:
    """Make the whole photograph agree with its new sky.

    Two parts. The ground gets bounded channel gains toward the sky's mean
    tone — a dusk sky over a noon-lit house is the tell that ruins every
    amateur swap. Then the *entire* frame gets the same correction at a
    third of the strength: a real scene is lit by its sky, so every surface
    carries a trace of its colour, and that global whisper is what makes a
    composite read as one photograph instead of two.
    """
    sky_mean = bg.reshape(-1, 3).mean(axis=0)
    tone = sky_mean / max(float(sky_mean.mean()), 1e-4)  # colour, not brightness
    # At most ±12% per channel at full strength on the ground...
    gains = 1.0 + (np.clip(tone, 0.7, 1.3) - 1.0) * 0.4 * strength
    ground = soft[..., 0] < 0.5
    out = fg.copy()
    if ground.any():
        out[ground] = fg[ground] * gains[None, :]
    # ...and a global third of that everywhere, so the harmonisation has no
    # visible seam of its own at the matte boundary.
    global_gains = 1.0 + (gains - 1.0) * (1.0 / 3.0)
    return out * global_gains[None, None, :]
