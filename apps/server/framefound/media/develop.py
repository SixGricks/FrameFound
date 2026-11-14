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

The colour maths is `grade`; `measure` is what two of its steps (auto-levels,
auto white balance) first read off a whole picture. `apply_recipe` is the two
together on a photograph. They are kept apart so that something that is not
a photograph — a table of colours on its way to becoming a LUT — goes
through the very same arithmetic, with the readings taken from a still.
"""

import contextlib
import io
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

# Rec. 709 luma weights — the standard answer to "how bright is this pixel".
_LUMA = (0.2126, 0.7152, 0.0722)


def _brightness(arr: Any, np: Any) -> Any:
    """How bright each colour of a float32 (..., 3) array is: float32 (...).

    Written out channel by channel, not as `arr @ _LUMA`. NumPy hands that
    product to the machine's BLAS, and what a BLAS returns for one colour may
    depend on where the colour sits in the array: OpenBLAS already gave a
    picture exactly one pixel wide a different last place of the float than
    the same colours in a wider one, and Apple's Accelerate (NumPy's BLAS on
    a Mac) was never tried. A LUT is this engine run on a lattice of colours
    and is promised to be the engine's own answer for each of them, so a
    colour must get the same float in a table as in a photograph. Three
    multiplies and two adds, element by element, cannot depend on position,
    and are the same floats on every machine.

    Same floats as the product it replaced wherever that was checked:
    OpenBLAS's Nehalem, Sandy Bridge, Haswell and Zen kernels, 58 million
    values each, on arrays two or more colours wide.
    """
    red, green, blue = np.asarray(_LUMA, dtype=np.float32)
    # Added in place: on a 45 MP export each of these is 180 MB, and one at a
    # time is enough.
    luma = arr[..., 0] * red
    luma += arr[..., 1] * green
    luma += arr[..., 2] * blue
    return luma


def open_for_render(
    path: Path, already_normalized: bool = False, max_edge: int | None = None
) -> Image.Image:
    """The pixels every render path starts from: upright and in sRGB.

    One function because the paths used to disagree. Export flattened
    embedded profiles to sRGB while the editor preview skipped it, so an
    AdobeRGB photograph looked duller on screen than in the zip — the
    operator added vibrance to compensate and the export overshot, which is
    the "what you saw is what ships" promise broken in the most expensive
    direction. Auto-edit judged the unconverted preview too.

    `already_normalized` is for object-removal results, which were made
    upright and sRGB when their chain started; doing either again is wrong.

    `max_edge` asks for a preview no longer than that on its long side. The
    JPEG decoder then produces a quarter of the pixels directly, and the
    profile transform — per-pixel and scale-free, the module docstring's own
    argument — runs on the small image instead of a 45 MP one.
    Returns a loaded RGB copy, independent of the file.
    """
    from PIL import ImageCms, ImageOps

    from framefound.media.pixels import to_rgb8

    with Image.open(path) as img:
        if max_edge is not None:
            img.draft("RGB", (max_edge, max_edge))
        if already_normalized:
            image = to_rgb8(img)
            if max_edge is not None:
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            return image
        image = ImageOps.exif_transpose(img) or img
        if max_edge is not None:
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        icc = image.info.get("icc_profile")
        if icc:
            # A malformed profile is the camera's problem; the photograph
            # still renders, just unconverted.
            with contextlib.suppress(Exception):
                converted = ImageCms.profileToProfile(
                    image,
                    ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                    ImageCms.createProfile("sRGB"),
                    outputMode="RGB",
                )
                if converted is not None:
                    image = converted
        return to_rgb8(image)


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
    # Measured white-balance neutralisation, by strength. Unlike the
    # temperature/tint sliders (a fixed shove), this measures the cast in
    # the photograph's own likely-neutral surfaces and removes that much of
    # it — the single biggest difference between a camera JPEG interior and
    # an MLS final.
    "auto_wb": (0.0, 1.0),
    # Large-radius local contrast: the "even but punchy" light of an edited
    # interior. Midtone separation without moving global exposure.
    "local_contrast": (0.0, 1.0),
}


# The sky entry's numeric fields and their bounds; `name` is a filename in
# the operator's sky library, allowed only a conservative character set so a
# stored recipe can never be a path traversal.
SKY_FIELDS = {"feather": (0.0, 0.2), "shift": (-0.5, 0.5), "relight": (0.0, 1.0)}
SKY_DEFAULTS = {"feather": 0.02, "shift": 0.0, "relight": 0.4}
# The one rule for a sky's name, shared with the upload endpoint so a sky
# that can be added can also be chosen.
SKY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,118}$")
_SKY_NAME_OK = SKY_NAME_RE


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


# The sliders that need nothing but the pixel itself, and the two steps that
# first read something off the whole picture. A colour table (a LUT) can carry
# the first group as it stands and the second once measured on a still.
# shadows is per-pixel when negative and reads the illumination map when positive.
POINTWISE_KEYS: tuple[str, ...] = (
    "exposure",
    "temperature",
    "tint",
    "contrast",
    "saturation",
    "vibrance",
)
MEASURED_KEYS: tuple[str, ...] = ("auto", "auto_wb")
# Where each measured step's reading is kept in the dict `measure` returns.
_MEASURED_AS = {"auto": "levels", "auto_wb": "wb_gains"}


def reads_neighbours(recipe: dict[str, Any]) -> list[str]:
    """The keys of a cleaned recipe that need more than the pixel itself, in
    the engine's order: shadows (when > 0), highlights, window_pull,
    local_contrast."""
    keys = []
    if (recipe.get("shadows") or 0.0) > 0:
        keys.append("shadows")
    keys.extend(key for key in ("highlights", "window_pull", "local_contrast") if recipe.get(key))
    return keys


def measure(image: Image.Image, recipe: dict[str, Any]) -> dict[str, Any]:
    """What the data-dependent steps read off this picture, frozen.

    `{"levels": [[low, high], [low, high], [low, high]]}` when the recipe has
    `auto`; `{"wb_gains": [r, g, b]}` when it has `auto_wb` — the gains as
    applied (strength folded in), measured AFTER auto-levels, as the engine
    applies them; `[1, 1, 1]` when the engine would leave the picture alone.
    Empty when the recipe has neither.

    Handing the result to `grade` on the same picture renders exactly what
    measuring afresh would. Handing it to `grade` on anything else is the
    point: a colour table, or another frame of the same clip, gets the
    correction this picture called for. Plain floats: it goes into JSON.
    """
    recipe = clean_recipe(recipe)
    measured: dict[str, Any] = {}
    if not (recipe.get("auto") or recipe.get("auto_wb")):
        return measured

    import numpy as np

    arr = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    if recipe.get("auto"):
        measured["levels"] = _levels(arr, np)
        _apply_levels(arr, measured["levels"])
    if wb := recipe.get("auto_wb"):
        measured["wb_gains"] = _wb_gains(arr, wb, np)
    return measured


def grade(
    arr: Any,
    recipe: dict[str, Any],
    *,
    measured: dict[str, Any] | None = None,
    pointwise: bool = False,
) -> Any:
    """The colour maths. float32 (..., 3) in 0..1 in; clipped float32 out;
    `arr` is not modified. `recipe` must already be cleaned.

    `measured=None` measures `arr` itself (a picture: what `apply_recipe`
    does). With `measured` (from `measure`), auto-levels and auto white
    balance apply what was read off that other picture instead; a reading it
    lacks is still taken from `arr`.

    `pointwise=True` is for anything that is not a picture (a lattice of
    colours on its way to becoming a LUT). It raises ValueError if the recipe
    has a key from `reads_neighbours()`, or a MEASURED key with no entry in
    `measured`: a lattice pushed through those steps would be white-balanced
    and box-filtered as if it were a photograph, and the table that comes out
    looks plausible and is wrong.
    """
    frozen = _frozen(measured)
    if pointwise:
        if spatial := reads_neighbours(recipe):
            raise ValueError(
                f"{', '.join(spatial)} read the pixels around each pixel, which only a picture has"
            )
        unmeasured = [
            key for key in MEASURED_KEYS if recipe.get(key) and _MEASURED_AS[key] not in frozen
        ]
        if unmeasured:
            raise ValueError(
                f"{', '.join(unmeasured)} must be measured on a picture first (develop.measure)"
            )

    import numpy as np

    return _grade(np.array(arr, dtype=np.float32), recipe, frozen, np)


def apply_recipe(image: Image.Image, recipe: dict[str, Any]) -> Image.Image:
    """Render a recipe onto an image. The input image is not modified."""
    recipe = clean_recipe(recipe)
    if not recipe:
        return image

    import numpy as np

    # Straight to the working copy: `grade` would copy a second time, which
    # on a 45 MP export is half a gigabyte held for nothing.
    arr = _grade(np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0, recipe, {}, np)
    return Image.fromarray((arr * 255.0 + 0.5).astype("uint8"), "RGB")


def _frozen(measured: dict[str, Any] | None) -> dict[str, Any]:
    """`measured` as numbers the maths can trust, or ValueError.

    It comes back out of a JSON column like a recipe does, and a reading of
    the wrong shape would otherwise surface as a numpy broadcasting error or,
    worse, as a tint nobody chose.
    """
    import math

    if not measured:
        return {}
    levels, gains = measured.get("levels"), measured.get("wb_gains")
    out: dict[str, Any] = {}
    try:
        # Python floats on purpose: numpy treats them as the array's own
        # precision, which is what keeps a frozen reading bit-for-bit equal
        # to a fresh one.
        if levels is not None:
            out["levels"] = [[float(low), float(high)] for low, high in levels]
        if gains is not None:
            out["wb_gains"] = [float(gain) for gain in gains]
    except (TypeError, ValueError) as err:
        raise ValueError("measured levels and wb_gains must be numbers") from err
    pairs: list[list[float]] = out.get("levels", [])
    scales: list[float] = out.get("wb_gains", [])
    numbers = [*scales, *(value for pair in pairs for value in pair)]
    if (
        any(len(reading) != 3 for reading in out.values())
        or not all(math.isfinite(value) for value in numbers)
        or any(high <= low for low, high in pairs)
        or any(gain <= 0 for gain in scales)
    ):
        raise ValueError("measured levels and wb_gains must be three channels each, in order")
    return out


def _grade(arr: Any, recipe: dict[str, Any], frozen: dict[str, Any], np: Any) -> Any:
    """`grade` on an array the caller owns: it is worked on in place."""
    # Auto first: it establishes a sane base the manual sliders then shape,
    # so toggling it does not invert the meaning of the other adjustments.
    if recipe.get("auto"):
        _apply_levels(arr, frozen.get("levels") or _levels(arr, np))

    # Auto white balance before the manual temperature/tint, so those stay
    # a creative nudge on top of a neutral base rather than fighting a cast.
    if wb := recipe.get("auto_wb"):
        arr *= np.asarray(frozen.get("wb_gains") or _wb_gains(arr, wb, np), dtype=np.float32)

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

    # Shadow lift and window pull both read the room's ILLUMINATION - the
    # edge-aware large-scale lighting map - rather than raw or blurred
    # luminance. That distinction is the whole game for interiors: a
    # Gaussian mask darkens "near the window" (the smudge), an edge-aware
    # map darkens "the window", and lifts "the dim corner" without lifting
    # the dark leather sofa that is dark because it is leather.
    shadows, highlights = recipe.get("shadows"), recipe.get("highlights")
    pull = recipe.get("window_pull")
    if shadows or highlights or pull:
        luma = np.clip(_brightness(arr, np), 1e-4, 1.0)
        # Darkening the shadows (negative) is per-pixel and never reads the
        # map, so it is not built for it: that is what lets a lattice, which
        # has no rows and columns to filter over, take that slider.
        illum: Any = None
        if (shadows and shadows > 0) or highlights or pull:
            illum = np.clip(_illumination(luma, np), 1e-4, 1.0)
        if shadows and shadows > 0:
            # Even the lighting: raise regions the ROOM lights dimly toward
            # a mid target, leaving intrinsically dark objects their depth
            # (their edges are preserved in the map, so they do not read as
            # dim lighting).
            deficit = np.clip(0.55 - illum, 0.0, 0.55) / 0.55
            arr *= (1.0 + (0.85 * shadows) * deficit)[..., None]
        elif shadows:
            mask = (1.0 - luma) ** 2
            arr *= 1.0 + (0.7 * shadows) * mask[..., None]
        if highlights:
            mask = illum**2
            arr *= 1.0 + (0.5 * highlights) * mask[..., None]
        if pull:
            # The window darkens as a pane-bounded region; the wall beside
            # it, on the other side of an edge the guide preserved, does
            # not move. Detail inside the pane keeps its own contrast
            # because the gain is per-region, not per-pixel.
            excess = np.clip((illum - 0.55) * 2.2, 0.0, 1.0)
            gain = 1.0 - (0.75 * pull) * excess**1.2
            arr *= gain[..., None]

    if punch := recipe.get("local_contrast"):
        from PIL import ImageFilter

        luma = np.clip(_brightness(arr, np), 1e-4, 1.0)
        blur_img = Image.fromarray((luma * 255.0).astype("uint8"), "L")
        radius = max(4.0, arr.shape[0] / 16.0)
        blurred = (
            np.asarray(blur_img.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32) / 255.0
        )
        # Unsharp on luminance at a large radius: push each region away from
        # its neighbourhood average, then carry the colour along by ratio so
        # hues do not shift.
        lifted = np.clip(luma + (0.55 * punch) * (luma - blurred), 1e-4, 1.0)
        arr *= (lifted / luma)[..., None]

    vibrance, saturation = recipe.get("vibrance"), recipe.get("saturation")
    if vibrance or saturation:
        luma = (_brightness(arr, np))[..., None]
        if saturation:
            arr = luma + (arr - luma) * (1.0 + saturation)
        if vibrance:
            # Chroma-weighted: muted pixels move most, already-vivid ones
            # barely at all — which is what keeps a vibrance push from
            # turning a red front door radioactive.
            chroma = arr.max(axis=-1) - arr.min(axis=-1)
            factor = 1.0 + vibrance * np.clip(1.0 - 2.0 * chroma, 0.0, 1.0)
            arr = luma + (arr - luma) * factor[..., None]

    return np.clip(arr, 0.0, 1.0)


# A warm camera-JPEG interior wall sits near 0.3 saturation; grass, red brick
# and blue sky sit above 0.45. The cast is measured on what is below.
NEUTRAL_MAX_SATURATION = 0.42
# Fewer candidate neutrals than this share of the frame: no correction.
NEUTRAL_MIN_FRACTION = 0.02


def _wb_gains(arr: Any, strength: float, np: Any) -> list[float]:
    """The channel gains that remove the measured colour cast, by strength.
    `[1.0, 1.0, 1.0]` when there is nothing to correct; multiply to apply.

    The cast is measured on the mid-to-bright band (40th–95th luminance
    percentile) — in an interior that band is walls and ceilings, the
    surfaces that are usually *meant* to be neutral. Gray-world over the
    whole frame would try to neutralise the oak floor; this does not. Gains
    are bounded so a photograph that is legitimately one colour (a sunset,
    a red barn wall) is nudged, never bleached.

    Only surfaces that could plausibly *be* neutral vote: a pixel more than
    NEUTRAL_MAX_SATURATION saturated is lawn, brick or blue sky, not a grey
    wall with a cast. Without that, a lawn-heavy exterior — lawn fills the
    mid-bright band — read as a green cast, and its correction turned the
    overcast sky purple and the house magenta (found by the Fotello
    bake-off, Sep 2026). A photograph with almost nothing neutral in it is
    left as it is: there is nothing to measure a cast against.
    """
    luma = _brightness(arr, np)
    low, high = np.percentile(luma, (40.0, 95.0))
    brightest = arr.max(axis=-1)
    saturation = (brightest - arr.min(axis=-1)) / np.maximum(brightest, 1e-4)
    band = (luma >= low) & (luma <= high) & (saturation <= NEUTRAL_MAX_SATURATION)
    if band.sum() < NEUTRAL_MIN_FRACTION * luma.size:
        return [1.0, 1.0, 1.0]
    means = arr[band].reshape(-1, 3).mean(axis=0)
    target = float(means.mean())
    if target <= 1e-4:
        return [1.0, 1.0, 1.0]
    gains = np.clip(target / np.maximum(means, 1e-4), 0.7, 1.4)
    applied = 1.0 + (gains - 1.0) * strength
    # float32 values as Python floats: exact, so the same three numbers come
    # back whether they went through JSON or straight into the multiply.
    return [float(gain) for gain in applied]


def _levels(arr: Any, np: Any) -> list[list[float]]:
    """Where auto-levels puts black and white, per channel: `[low, high]`.

    Gentle per-channel stretch: put the 0.5th percentile near black and
    the 99.5th near white, per channel, which both opens up a flat exposure
    and pulls out most colour casts. Gains are bounded so a photograph that
    is genuinely all one tone (a wall, a sky) is nudged, not shredded. A
    channel left alone reads `[0.0, 1.0]`, which divides out exactly."""
    levels: list[list[float]] = []
    for channel in range(3):
        low, high = np.percentile(arr[..., channel], (0.5, 99.5))
        low = min(float(low), 0.25)
        high = max(float(high), 0.75)
        levels.append([low, high] if high - low > 1e-3 else [0.0, 1.0])
    return levels


def _apply_levels(arr: Any, levels: list[list[float]]) -> None:
    """Stretch each channel of `arr` between its two levels, in place."""
    for channel, (low, high) in enumerate(levels):
        arr[..., channel] = (arr[..., channel] - low) / (high - low)


# The listing preset: the deterministic auto-edit used when no AI key is
# configured, and the base the AI picker's judgment refines. Tuned against
# published IntelAuctions listings vs their NAS originals (2026-08-05,
# eleven pairs): brightness landed within half a point of the published
# average, saturation within ~15%, with the remaining visible difference
# being sky replacement - which is a separate, operator-chosen step.
LISTING_PRESET: dict[str, Any] = {
    "auto_wb": 1.0,
    "exposure": 0.22,
    "shadows": 0.30,
    "window_pull": 0.30,
    "local_contrast": 0.30,
    "vibrance": 0.42,
}


def _box_mean(arr: Any, radius: int) -> Any:
    """Mean over a (2r+1) box via integral image - O(N) regardless of radius,
    which is what makes a large-radius illumination map affordable per
    preview keystroke."""
    import numpy as np

    height, width = arr.shape
    padded = np.pad(arr, radius + 1, mode="edge")
    integral = padded.cumsum(axis=0).cumsum(axis=1)
    size = 2 * radius + 1
    total = (
        integral[size:, size:]
        - integral[:-size, size:]
        - integral[size:, :-size]
        + integral[:-size, :-size]
    )
    return (total / (size * size))[:height, :width]


def _illumination(luma: Any, np: Any) -> Any:
    """The room's lighting, without its contents: a self-guided filter.

    A Gaussian blur of luminance bleeds across edges - darken "the bright
    region" and the wall beside the window darkens too, which is exactly
    the smudge the operator called out. The guided filter's per-window
    linear model (a*I + b) keeps strong edges in the guide, so the
    illumination map follows the window frame instead of blurring across
    it. Self-guided (guide = luma itself), radius ~1/8 of height,
    eps tuned so window frames and doorways count as edges but wall
    texture does not.
    """
    radius = max(8, luma.shape[0] // 8)
    eps = 0.02
    mean_i = _box_mean(luma, radius)
    var_i = _box_mean(luma * luma, radius) - mean_i * mean_i
    a = var_i / (var_i + eps)
    b = (1.0 - a) * mean_i
    return _box_mean(a, radius) * luma + _box_mean(b, radius)
