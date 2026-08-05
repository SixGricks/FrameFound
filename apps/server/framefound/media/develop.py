"""The develop engine: apply a slider recipe to a photograph.

Everything here is per-pixel float32 arithmetic on a numpy array — no model,
no network, and deliberately no dependency beyond the Pillow and numpy the
media extra already carries. The operations are the basic vocabulary every
photo tool shares (exposure, contrast, white balance, shadow/highlight,
vibrance), implemented as monotonic, clipped curves so no slider position can
produce garbage, only a bad-looking photograph.

One engine serves both the interactive preview and the export, which is the
property that matters: what the operator saw is what the zip contains. The
adjustments are per-pixel and scale-free, so applying them after downscaling
(cheap) renders the same image as before (expensive), with the one honest
exception of auto-levels' percentiles, which differ immeasurably.
"""

import re
from collections.abc import Callable
from typing import Any

from PIL import Image

# Rec. 709 luma weights — the standard answer to "how bright is this pixel".
_LUMA = (0.2126, 0.7152, 0.0722)

# Slider ranges, matched by the API's validation and the UI's slider bounds.
# exposure is in EV stops; everything else is -1..1 (the UI shows -100..100).
RECIPE_FIELDS = {
    "exposure": (-2.0, 2.0),
    "contrast": (-1.0, 1.0),
    "temperature": (-1.0, 1.0),
    "tint": (-1.0, 1.0),
    "shadows": (-1.0, 1.0),
    "highlights": (-1.0, 1.0),
    "vibrance": (-1.0, 1.0),
    "saturation": (-1.0, 1.0),
    # Geometry. rotate is degrees of straightening; keystone corrects
    # converging verticals (positive = the camera was tilted up, the usual
    # real-estate case). Both cost edge pixels, never black corners.
    "rotate": (-5.0, 5.0),
    "keystone": (-1.0, 1.0),
    # Single-frame window pull: local tone compression driven by blurred
    # luminance, so a bright window darkens as a region while its own
    # detail keeps its contrast.
    "window_pull": (0.0, 1.0),
}


# The sky entry's numeric fields and their bounds; `name` is a filename in
# the operator's sky library, allowed only a conservative character set so a
# stored recipe can never be a path traversal.
SKY_FIELDS = {"feather": (0.0, 0.2), "shift": (-0.5, 0.5), "relight": (0.0, 1.0)}
SKY_DEFAULTS = {"feather": 0.02, "shift": 0.0, "relight": 0.4}
_SKY_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,118}$")


def clean_recipe(raw: dict[str, Any]) -> dict[str, Any]:
    """Clamp every known field into range and drop everything else.

    Recipes come back out of a JSON column and go into arithmetic; this is
    the boundary where "whatever was stored" becomes "numbers the maths can
    trust", the same lesson the probe module learned from ExifTool.
    """
    out: dict[str, Any] = {}
    for key, (low, high) in RECIPE_FIELDS.items():
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = max(low, min(high, float(value)))
        if number != 0.0:
            out[key] = number
    if raw.get("auto") is True:
        out["auto"] = True
    sky = raw.get("sky")
    if isinstance(sky, dict):
        name = sky.get("name")
        if isinstance(name, str) and _SKY_NAME_OK.match(name) and ".." not in name:
            cleaned_sky: dict[str, Any] = {"name": name}
            for key, (low, high) in SKY_FIELDS.items():
                value = sky.get(key, SKY_DEFAULTS[key])
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    value = SKY_DEFAULTS[key]
                cleaned_sky[key] = max(low, min(high, float(value)))
            out["sky"] = cleaned_sky
    return out


def is_identity(recipe: dict[str, Any]) -> bool:
    return not clean_recipe(recipe)


def render(
    image: Image.Image,
    recipe: dict[str, Any],
    *,
    load_sky: Callable[[str], Image.Image | None] | None = None,
    mask_for: Callable[[Image.Image], Any] | None = None,
) -> Image.Image:
    """The full recipe: sky replacement first, then the colour sliders.

    Sky before colour, so the sliders grade the composited photograph — the
    operator is correcting the image they will export, not the one they
    started from. The two callables are injected because segmentation needs
    the ONNX runtime and the sky library lives on disk; the colour maths
    needs neither, and tests exercise compositing with hand-made masks.

    A recipe that names a sky renders without one when either callable is
    missing or the sky file is gone — degraded output over a failed export,
    with the caller told nothing because there is nothing it could do.
    """
    cleaned = clean_recipe(recipe)
    sky = cleaned.get("sky")
    if sky and load_sky is not None and mask_for is not None:
        sky_image = load_sky(sky["name"])
        if sky_image is not None:
            from framefound.media.sky import composite_sky

            mask = mask_for(image)
            if mask is not None:
                image = composite_sky(
                    image,
                    mask,
                    sky_image,
                    feather=sky["feather"],
                    shift=sky["shift"],
                    relight=sky["relight"],
                )
    # Geometry after the sky so the segmentation mask stays a function of
    # the original pixels alone (which is what lets the preview cache it),
    # and a composited sky simply warps along with everything else.
    image = apply_geometry(image, cleaned)
    return apply_recipe(image, cleaned)


def apply_geometry(image: Image.Image, recipe: dict[str, Any]) -> Image.Image:
    """Straighten, then correct verticals. Output size equals input size —
    both operations sample from inside the frame and pay in edge pixels,
    never in black corners, and a stable size is what keeps the rest of the
    pipeline (masks, previews, exports) indifferent to geometry."""
    recipe = clean_recipe(recipe)
    rotate, keystone = recipe.get("rotate"), recipe.get("keystone")
    if rotate:
        image = _rotate_crop(image, rotate)
    if keystone:
        image = _keystone(image, keystone)
    return image


def _rotate_crop(image: Image.Image, degrees: float) -> Image.Image:
    """Rotate and crop back to the largest same-aspect rectangle.

    A rotated crop of size (s·w, s·h) fits in the original frame exactly
    when its bounding box does, which gives the scale in closed form.
    """
    import math

    w, h = image.size
    a = math.radians(abs(degrees))
    sin_a, cos_a = math.sin(a), math.cos(a)
    s = min(w / (w * cos_a + h * sin_a), h / (w * sin_a + h * cos_a))
    rotated = image.rotate(degrees, Image.Resampling.BICUBIC, expand=True)
    rw, rh = rotated.size
    cw, ch = round(w * s), round(h * s)
    left, top = (rw - cw) // 2, (rh - ch) // 2
    return rotated.crop((left, top, left + cw, top + ch)).resize((w, h), Image.Resampling.LANCZOS)


def _keystone(image: Image.Image, amount: float) -> Image.Image:
    """Vertical perspective correction.

    Positive `amount` fixes verticals converging toward the top (camera
    tilted up): the output's top edge samples from an inset span of the
    source, which stretches the top back out. Negative fixes the opposite.
    At full strength the inset is 18% per side — beyond that a photograph
    stops believing itself.
    """
    import numpy as np

    w, h = image.size
    inset = abs(amount) * 0.18 * w
    if amount > 0:
        source = [(inset, 0.0), (w - inset, 0.0), (float(w), float(h)), (0.0, float(h))]
    else:
        source = [(0.0, 0.0), (float(w), 0.0), (w - inset, float(h)), (inset, float(h))]
    dest = [(0.0, 0.0), (float(w), 0.0), (float(w), float(h)), (0.0, float(h))]

    # Pillow's PERSPECTIVE coefficients map output coordinates to input
    # coordinates; solve the 8-parameter projective system for dest→source.
    rows, rhs = [], []
    for (dx, dy), (sx, sy) in zip(dest, source, strict=True):
        rows.append([dx, dy, 1, 0, 0, 0, -sx * dx, -sx * dy])
        rows.append([0, 0, 0, dx, dy, 1, -sy * dx, -sy * dy])
        rhs.extend([sx, sy])
    coeffs = np.linalg.solve(np.asarray(rows, dtype=np.float64), np.asarray(rhs, dtype=np.float64))
    return image.transform(
        (w, h), Image.Transform.PERSPECTIVE, tuple(coeffs), Image.Resampling.BICUBIC
    )


def apply_recipe(image: Image.Image, recipe: dict[str, Any]) -> Image.Image:
    """Render a recipe onto an image. The input image is not modified."""
    recipe = clean_recipe(recipe)
    if not recipe:
        return image

    import numpy as np

    arr = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0

    # Auto first: it establishes a sane base the manual sliders then shape,
    # so toggling it does not invert the meaning of the other adjustments.
    if recipe.get("auto"):
        arr = _auto_levels(arr, np)

    if ev := recipe.get("exposure"):
        arr *= 2.0**ev

    # White balance as channel gains. Positive temperature warms (red up,
    # blue down); positive tint shifts magenta (green down), matching the
    # convention every photo tool uses.
    if temp := recipe.get("temperature"):
        arr[..., 0] *= 1.0 + 0.25 * temp
        arr[..., 2] *= 1.0 - 0.25 * temp
    if tint := recipe.get("tint"):
        arr[..., 1] *= 1.0 - 0.20 * tint

    if contrast := recipe.get("contrast"):
        arr = (arr - 0.5) * (1.0 + contrast) + 0.5

    # Shadow/highlight as luminance-masked gains: the mask keeps a shadow
    # lift from bleaching a sky and a highlight recovery from crushing a
    # dark hallway. Multiplicative, so the curve stays monotonic.
    shadows, highlights = recipe.get("shadows"), recipe.get("highlights")
    if shadows or highlights:
        luma = arr @ np.asarray(_LUMA, dtype=np.float32)
        luma = np.clip(luma, 0.0, 1.0)
        if shadows:
            mask = (1.0 - luma) ** 2
            arr *= 1.0 + (0.7 * shadows) * mask[..., None]
        if highlights:
            mask = luma**2
            arr *= 1.0 + (0.5 * highlights) * mask[..., None]

    # Window pull: the gain is driven by *blurred* luminance, so a bright
    # window darkens as a region while the detail inside it keeps its own
    # contrast — which is what separates this from just pulling highlights,
    # and is the single-frame approximation of what bracket fusion does.
    # It reveals whatever the file still holds; a sensor-clipped pane has
    # nothing left to reveal, and no slider can honestly invent it.
    if pull := recipe.get("window_pull"):
        from PIL import ImageFilter

        luma = np.clip(arr @ np.asarray(_LUMA, dtype=np.float32), 0.0, 1.0)
        blur_img = Image.fromarray((luma * 255.0).astype("uint8"), "L")
        radius = max(2.0, arr.shape[0] / 24.0)
        blurred = (
            np.asarray(blur_img.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32) / 255.0
        )
        excess = np.clip((blurred - 0.5) * 2.0, 0.0, 1.0)
        gain = 1.0 - (0.75 * pull) * excess**1.3
        arr *= gain[..., None]

    vibrance, saturation = recipe.get("vibrance"), recipe.get("saturation")
    if vibrance or saturation:
        luma = (arr @ np.asarray(_LUMA, dtype=np.float32))[..., None]
        if saturation:
            arr = luma + (arr - luma) * (1.0 + saturation)
        if vibrance:
            # Chroma-weighted: muted pixels move most, already-vivid ones
            # barely at all — which is what keeps a vibrance push from
            # turning a red front door radioactive.
            chroma = arr.max(axis=-1) - arr.min(axis=-1)
            factor = 1.0 + vibrance * np.clip(1.0 - 2.0 * chroma, 0.0, 1.0)
            arr = luma + (arr - luma) * factor[..., None]

    arr = np.clip(arr, 0.0, 1.0)
    return Image.fromarray((arr * 255.0 + 0.5).astype("uint8"), "RGB")


def _auto_levels(arr: Any, np: Any) -> Any:
    """Gentle per-channel stretch: put the 0.5th percentile near black and
    the 99.5th near white, per channel, which both opens up a flat exposure
    and pulls out most colour casts. Gains are bounded so a photograph that
    is genuinely all one tone (a wall, a sky) is nudged, not shredded."""
    out = arr.copy()
    for channel in range(3):
        low, high = np.percentile(out[..., channel], (0.5, 99.5))
        low = min(float(low), 0.25)
        high = max(float(high), 0.75)
        if high - low > 1e-3:
            out[..., channel] = (out[..., channel] - low) / (high - low)
    return out
