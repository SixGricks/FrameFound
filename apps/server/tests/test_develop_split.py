"""The colour engine split into measuring and grading — and nothing else changed.

`develop.apply_recipe` used to be one function that measured a photograph
(auto-levels, auto white balance) and graded it in the same breath. A LUT
needs the grading without the measuring: the table is the engine run on a
lattice of colours, with the white balance measured once on a still. So the
engine is now `measure` + `grade`, and `apply_recipe` is the two together.

Two things are pinned here. That the split changed no pixel: the engine as
it stood before is kept below, verbatim, and every render is compared with
it byte for byte. And that the new seam is safe to stand on: frozen
measurements reproduce the engine exactly, and a lattice is refused every
step that would treat it as a photograph.
"""

import json
import random
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw

from framefound.media import compare, develop

# ------------------------------------------------- the engine before the split
#
# develop.apply_recipe, _neutralise and _auto_levels as they were on
# 2026-11-12, before measure/grade existed. Kept only as the yardstick for
# test_the_split_engine_renders_exactly_what_the_engine_before_it_did: when
# the engine is next changed ON PURPOSE (a constant retuned, a slider added),
# make the same change here or retire that one test. test_develop.py is what
# pins how the engine should behave; this pins that a refactor moved nothing.
#
# It still takes brightness as a matrix product (`arr @ luma`), as the engine
# did. The engine now writes that out (develop._brightness says why), and the
# two are the same floats on OpenBLAS, which is why this copy could stay
# verbatim. A machine whose BLAS rounds the product differently (Apple's
# Accelerate was never tried) would fail the test by one level on a handful
# of values. That would be this old copy's doing and not the engine's: the
# engine's floats do not depend on the machine any more. Retire the test
# there.


def _engine_before(image: Image.Image, recipe: dict[str, Any]) -> Image.Image:
    recipe = develop.clean_recipe(recipe)
    if not recipe:
        return image

    arr = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0

    if recipe.get("auto"):
        arr = _auto_levels_before(arr)

    if wb := recipe.get("auto_wb"):
        arr = _neutralise_before(arr, wb)

    if ev := recipe.get("exposure"):
        arr *= 2.0**ev

    if temp := recipe.get("temperature"):
        arr[..., 0] *= 1.0 + 0.25 * temp
        arr[..., 2] *= 1.0 - 0.25 * temp
    if tint := recipe.get("tint"):
        arr[..., 1] *= 1.0 - 0.20 * tint

    if contrast := recipe.get("contrast"):
        arr = (arr - 0.5) * (1.0 + contrast) + 0.5

    shadows, highlights = recipe.get("shadows"), recipe.get("highlights")
    pull = recipe.get("window_pull")
    if shadows or highlights or pull:
        luma = np.clip(arr @ np.asarray(develop._LUMA, dtype=np.float32), 1e-4, 1.0)
        illum = np.clip(develop._illumination(luma, np), 1e-4, 1.0)
        if shadows and shadows > 0:
            deficit = np.clip(0.55 - illum, 0.0, 0.55) / 0.55
            arr *= (1.0 + (0.85 * shadows) * deficit)[..., None]
        elif shadows:
            mask = (1.0 - luma) ** 2
            arr *= 1.0 + (0.7 * shadows) * mask[..., None]
        if highlights:
            mask = illum**2
            arr *= 1.0 + (0.5 * highlights) * mask[..., None]
        if pull:
            excess = np.clip((illum - 0.55) * 2.2, 0.0, 1.0)
            gain = 1.0 - (0.75 * pull) * excess**1.2
            arr *= gain[..., None]

    if punch := recipe.get("local_contrast"):
        from PIL import ImageFilter

        luma = np.clip(arr @ np.asarray(develop._LUMA, dtype=np.float32), 1e-4, 1.0)
        blur_img = Image.fromarray((luma * 255.0).astype("uint8"), "L")
        radius = max(4.0, arr.shape[0] / 16.0)
        blurred = (
            np.asarray(blur_img.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32) / 255.0
        )
        lifted = np.clip(luma + (0.55 * punch) * (luma - blurred), 1e-4, 1.0)
        arr *= (lifted / luma)[..., None]

    vibrance, saturation = recipe.get("vibrance"), recipe.get("saturation")
    if vibrance or saturation:
        luma = (arr @ np.asarray(develop._LUMA, dtype=np.float32))[..., None]
        if saturation:
            arr = luma + (arr - luma) * (1.0 + saturation)
        if vibrance:
            chroma = arr.max(axis=-1) - arr.min(axis=-1)
            factor = 1.0 + vibrance * np.clip(1.0 - 2.0 * chroma, 0.0, 1.0)
            arr = luma + (arr - luma) * factor[..., None]

    arr = np.clip(arr, 0.0, 1.0)
    return Image.fromarray((arr * 255.0 + 0.5).astype("uint8"), "RGB")


def _neutralise_before(arr: Any, strength: float) -> Any:
    luma = arr @ np.asarray(develop._LUMA, dtype=np.float32)
    low, high = np.percentile(luma, (40.0, 95.0))
    brightest = arr.max(axis=-1)
    saturation = (brightest - arr.min(axis=-1)) / np.maximum(brightest, 1e-4)
    band = (luma >= low) & (luma <= high) & (saturation <= develop.NEUTRAL_MAX_SATURATION)
    if band.sum() < develop.NEUTRAL_MIN_FRACTION * luma.size:
        return arr
    means = arr[band].reshape(-1, 3).mean(axis=0)
    target = float(means.mean())
    if target <= 1e-4:
        return arr
    gains = np.clip(target / np.maximum(means, 1e-4), 0.7, 1.4)
    applied = 1.0 + (gains - 1.0) * strength
    return arr * applied[None, None, :]


def _auto_levels_before(arr: Any) -> Any:
    out = arr.copy()
    for channel in range(3):
        low, high = np.percentile(out[..., channel], (0.5, 99.5))
        low = min(float(low), 0.25)
        high = max(float(high), 0.75)
        if high - low > 1e-3:
            out[..., channel] = (out[..., channel] - low) / (high - low)
    return out


# ------------------------------------------------------------------ scenes


def _rectangles() -> Image.Image:
    """A few coloured rectangles on grey: plenty that is neutral, no cast."""
    rng = random.Random(7)
    image = Image.new("RGB", (240, 160), (rng.randint(60, 160),) * 3)
    draw = ImageDraw.Draw(image)
    for _ in range(7):
        x0, y0 = rng.randint(0, 200), rng.randint(0, 120)
        colour = tuple(rng.randint(20, 235) for _ in range(3))
        draw.rectangle((x0, y0, x0 + rng.randint(20, 120), y0 + rng.randint(15, 80)), fill=colour)
    return image


def _interior() -> Image.Image:
    """A warm room: a lit wall with a cast, a blown window, a dark sofa, a floor."""
    w, h = 192, 128
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    wall = 0.35 + 0.45 * (xx / w)
    arr = np.stack([wall * 1.00, wall * 0.90, wall * 0.74], axis=-1)
    floor = np.stack([0.42 + 0.1 * xx / w, 0.27 + 0.06 * xx / w, 0.12 + 0.03 * xx / w], axis=-1)
    arr[h * 2 // 3 :] = floor[h * 2 // 3 :]
    arr[h // 6 : h // 2, w // 2 : w * 5 // 6] = (0.99, 0.98, 0.96)
    arr[h // 2 : h * 3 // 4, w // 10 : w * 4 // 10] = (0.09, 0.07, 0.06)
    # Texture, so the percentiles the engine measures fall between pixels
    # rather than on a flat plateau. A fixed table, not a random generator:
    # the picture is the same on every machine.
    arr += 0.02 * np.sin(xx * 1.7)[..., None] * np.cos(yy * 0.9)[..., None]
    return Image.fromarray((np.clip(arr, 0.0, 1.0) * 255.0 + 0.5).astype("uint8"), "RGB")


def _exterior() -> Image.Image:
    """Portrait, and nothing neutral in it: blue sky, red brick, lawn."""
    w, h = 96, 144
    _yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    shade = (0.8 + 0.2 * (xx / w))[..., None]
    arr = np.empty((h, w, 3))
    arr[: h // 3] = (shade * (0.18, 0.42, 0.92))[: h // 3]
    arr[h // 3 : h * 2 // 3] = (shade * (0.70, 0.20, 0.14))[h // 3 : h * 2 // 3]
    arr[h * 2 // 3 :] = (shade * (0.16, 0.62, 0.12))[h * 2 // 3 :]
    return Image.fromarray((arr * 255.0 + 0.5).astype("uint8"), "RGB")


SCENES = {"rectangles": _rectangles(), "interior": _interior(), "exterior": _exterior()}

TYPICAL = {
    "exposure": 0.3, "contrast": 0.15, "temperature": 0.1,
    "tint": -0.05, "vibrance": 0.3, "saturation": 0.1,
}  # fmt: skip
STOPS_HIGH = {key: high for key, (_low, high) in develop.RECIPE_FIELDS.items()}
STOPS_LOW = {key: low for key, (low, _high) in develop.RECIPE_FIELDS.items()}


def _recipes() -> dict[str, dict[str, Any]]:
    """Every slider alone in both directions, the two measuring steps alone
    and together, the listing preset, and every slider at its stop."""
    out: dict[str, dict[str, Any]] = {}
    for key, (low, high) in develop.RECIPE_FIELDS.items():
        out[f"{key} up"] = {key: high * 0.5}
        if low < 0:
            out[f"{key} down"] = {key: low * 0.5}
    out["auto"] = {"auto": True}
    out["auto_wb"] = {"auto_wb": 0.6}
    out["auto and auto_wb"] = {"auto": True, "auto_wb": 1.0}
    out["the listing preset"] = dict(develop.LISTING_PRESET)
    out["the listing preset after auto"] = {**develop.LISTING_PRESET, "auto": True}
    out["a typical grade"] = dict(TYPICAL)
    out["a typical grade on a neutral base"] = {**TYPICAL, "auto_wb": 1.0}
    out["every slider at its top stop"] = dict(STOPS_HIGH)
    out["every slider at its bottom stop"] = dict(STOPS_LOW)
    out["the top stops after auto"] = {**STOPS_HIGH, "auto": True}
    out["the bottom stops after auto"] = {**STOPS_LOW, "auto": True}
    out["a bit of everything"] = {
        "auto": True, "auto_wb": 0.8, "exposure": -0.4, "contrast": 0.25, "temperature": -0.2,
        "tint": 0.15, "shadows": 0.5, "highlights": -0.4, "window_pull": 0.6,
        "local_contrast": 0.5, "vibrance": -0.3, "saturation": 0.2,
    }  # fmt: skip
    return out


def _floats(image: Image.Image) -> Any:
    return np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0


def _bytes(graded: Any) -> Any:
    """A graded array as the 8-bit picture `apply_recipe` makes of it."""
    return (graded * 255.0 + 0.5).astype("uint8")


def _lattice(n: int = 5) -> Any:
    """An n x n x n table of colours, the shape a LUT is built on."""
    axis = np.linspace(0.0, 1.0, n, dtype=np.float32)
    blue, green, red = np.meshgrid(axis, axis, axis, indexing="ij")
    return np.stack([red, green, blue], axis=-1)


# ------------------------------------------------------ nothing has changed


def test_the_split_engine_renders_exactly_what_the_engine_before_it_did() -> None:
    """Every pixel of every render, on three drawn scenes. Not "close": the
    promise of this step is that no photograph already exported would come
    out differently if it were exported again."""
    for scene_name, image in SCENES.items():
        for recipe_name, recipe in _recipes().items():
            before = np.asarray(_engine_before(image, recipe))
            after = np.asarray(develop.apply_recipe(image, recipe))
            apart = np.abs(before.astype(np.int16) - after.astype(np.int16))
            assert not apart.any(), (
                f"{recipe_name} on the {scene_name} scene moved: {int((apart > 0).sum())} of "
                f"{apart.size} values, by {int(apart.max())} at most. (One level on a handful, "
                "on a machine this has not run on: read the note above _engine_before.)"
            )


def test_an_identity_recipe_still_returns_the_same_image_object() -> None:
    image = SCENES["rectangles"]
    assert develop.apply_recipe(image, {}) is image
    assert develop.apply_recipe(image, {"exposure": 0, "auto_wb": 0.0, "auto": False}) is image
    assert develop.measure(image, {"exposure": 0.4, "shadows": 0.3}) == {}, "nothing to measure"


# ------------------------------------------------------------ the new seam


def test_grading_a_picture_array_is_what_apply_recipe_returns() -> None:
    """`grade` is the engine; `apply_recipe` only cleans, converts and
    rounds. So a caller holding floats (the LUT builder) gets the engine's
    own answer, and its array is left as it was."""
    for scene_name, image in SCENES.items():
        arr = _floats(image)
        untouched = arr.copy()
        for recipe_name, recipe in _recipes().items():
            graded = develop.grade(arr, develop.clean_recipe(recipe))
            assert graded.dtype == np.float32 and graded.shape == arr.shape
            assert float(graded.min()) >= 0.0 and float(graded.max()) <= 1.0
            rendered = np.asarray(develop.apply_recipe(image, recipe))
            assert np.array_equal(_bytes(graded), rendered), f"{recipe_name} on {scene_name}"
        assert np.array_equal(arr, untouched), "grade worked on the caller's own array"


def test_frozen_measurements_reproduce_the_engine_on_the_same_picture() -> None:
    """Measure once, store the reading as JSON, grade with it later: the
    same floats as letting the engine measure for itself. This is what lets
    a table built from a stored reading be the look the operator judged."""
    for name in ("auto", "auto_wb", "auto and auto_wb", "the listing preset after auto"):
        recipe = develop.clean_recipe(_recipes()[name])
        for scene_name, image in SCENES.items():
            stored = json.loads(json.dumps(develop.measure(image, recipe)))
            arr = _floats(image)
            fresh = develop.grade(arr, recipe)
            frozen = develop.grade(arr, recipe, measured=stored)
            assert np.array_equal(fresh, frozen), f"{name} on the {scene_name} scene"


def test_a_frozen_reading_is_applied_to_whatever_it_is_handed() -> None:
    """The reading belongs to the picture it was taken from. On anything
    else the engine must apply it as given and measure nothing of its own —
    a flat grey card has no cast, and still gets the room's correction."""
    reading = develop.measure(SCENES["interior"], {"auto_wb": 1.0})
    red, green, blue = reading["wb_gains"]
    assert red < 1.0 < blue, "the warm wall is pulled back toward neutral"
    assert all(type(gain) is float for gain in reading["wb_gains"]), "plain floats for JSON"

    card = np.full((4, 4, 3), 0.5, dtype=np.float32)
    assert np.array_equal(develop.grade(card, {"auto_wb": 1.0}), card), "by itself: no cast"
    corrected = develop.grade(card, {"auto_wb": 1.0}, measured=reading)
    assert corrected[0, 0].tolist() == pytest.approx([0.5 * red, 0.5 * green, 0.5 * blue])

    levels = develop.measure(SCENES["interior"], {"auto": True})["levels"]
    assert len(levels) == 3 and all(low < high for low, high in levels)
    stretched = develop.grade(card, {"auto": True}, measured={"levels": levels})
    low, high = levels[0]
    assert stretched[0, 0, 0] == pytest.approx((0.5 - low) / (high - low))


def test_a_reading_the_measurement_lacks_is_taken_from_the_picture() -> None:
    """Levels frozen, white balance not: a picture still renders, with the
    missing half measured on itself as the engine always did."""
    image = SCENES["interior"]
    recipe = {"auto": True, "auto_wb": 0.8}
    levels_only = {"levels": develop.measure(image, recipe)["levels"]}
    arr = _floats(image)
    assert np.array_equal(
        develop.grade(arr, recipe, measured=levels_only), develop.grade(arr, recipe)
    )


def test_a_picture_with_nothing_neutral_measures_no_correction() -> None:
    """Sky, brick and lawn: there is no grey wall to read a cast off, so the
    reading says "leave it" in a form that multiplies out exactly."""
    assert develop.measure(SCENES["exterior"], {"auto_wb": 1.0}) == {"wb_gains": [1.0, 1.0, 1.0]}
    black = Image.new("RGB", (16, 16), (0, 0, 0))
    assert develop.measure(black, {"auto_wb": 1.0}) == {"wb_gains": [1.0, 1.0, 1.0]}


def test_what_reads_its_neighbours_is_named_in_the_engines_order() -> None:
    assert develop.reads_neighbours(develop.clean_recipe(develop.LISTING_PRESET)) == [
        "shadows",
        "window_pull",
        "local_contrast",
    ]
    assert develop.reads_neighbours(develop.clean_recipe(STOPS_HIGH)) == [
        "shadows",
        "highlights",
        "window_pull",
        "local_contrast",
    ]
    assert develop.reads_neighbours({"shadows": -0.4, **TYPICAL}) == [], (
        "darkening the shadows is per-pixel; only lifting them reads the room"
    )


# ------------------------------------------------- a lattice is not a picture

# The exact comparisons here, and the byte-for-byte ones in test_lut.py, set
# a colour graded in a table against the same colour graded in a picture and
# allow no difference at all. What a failure by a hair means, for whoever
# meets one on a machine these have not run on:
BY_A_LAST_PLACE = (
    "If the two differ only in the last place of a float, a step of the engine has stopped "
    "working one colour at a time. A matrix product does that: NumPy hands it to the machine's "
    "BLAS, which may round a colour differently by where it sits. See develop._brightness."
)


def test_a_colour_is_graded_the_same_wherever_it_sits() -> None:
    """A table of colours, a row, a column one colour wide, and the same
    colours shuffled: each colour comes out the very same float.

    The engine's brightness used to be `arr @ luma`. NumPy hands that to
    BLAS, and here OpenBLAS gave a picture one pixel wide a different last
    place for a quarter of its values; on a BLAS nobody had tried (the Mac's)
    the table and the photograph could have parted the same way, and the
    tests that say "the table is the engine" would have failed with nothing
    in the tree to say why. The column is what catches it on this machine."""
    colours = np.random.RandomState(9).random_sample((4096, 3)).astype(np.float32)
    # Past 1.0 too: exposure and contrast push values there before the
    # brightness is read.
    colours[::7] *= 3.0
    as_a_row = develop._brightness(colours.reshape(1, -1, 3), np)
    assert as_a_row.dtype == np.float32 and as_a_row.shape == (1, 4096)
    for shape in ((16, 16, 16, 3), (64, 64, 3), (2048, 2, 3), (4096, 1, 3)):
        placed = develop._brightness(colours.reshape(shape), np)
        assert placed.shape == shape[:-1]
        assert np.array_equal(placed.reshape(-1), as_a_row[0]), f"{shape}: {BY_A_LAST_PLACE}"

    # And through every step that reads it: saturation, vibrance, darkened
    # shadows.
    recipe = {**TYPICAL, "shadows": -0.4}
    graded = develop.grade(colours.reshape(1, -1, 3), recipe, pointwise=True)
    for shape in ((16, 16, 16, 3), (4096, 1, 3)):
        placed = develop.grade(colours.reshape(shape), recipe, pointwise=True)
        assert np.array_equal(placed.reshape(-1, 3), graded[0]), f"{shape}: {BY_A_LAST_PLACE}"


def test_a_lattice_is_graded_colour_by_colour_whatever_its_shape() -> None:
    """The sliders a LUT carries give each colour one answer, wherever it
    sits and whatever is beside it: a 5x5x5 table, the same colours in a row,
    and the same colours shuffled all come out colour for colour alike."""
    recipe = {**TYPICAL, "shadows": -0.4}
    assert set(develop.POINTWISE_KEYS) <= set(recipe)
    lattice = _lattice()
    table = develop.grade(lattice, recipe, pointwise=True)
    assert table.shape == lattice.shape and table.dtype == np.float32

    row = lattice.reshape(1, -1, 3)
    as_a_picture = develop.grade(row, recipe)
    assert np.array_equal(table.reshape(-1, 3), as_a_picture.reshape(-1, 3)), BY_A_LAST_PLACE

    order = np.random.RandomState(3).permutation(row.shape[1])
    shuffled = develop.grade(row[:, order], recipe, pointwise=True)
    assert np.array_equal(shuffled, as_a_picture[:, order]), BY_A_LAST_PLACE


@pytest.mark.parametrize(
    "recipe",
    [{"shadows": 0.3}, {"highlights": -0.3}, {"window_pull": 0.3}, {"local_contrast": 0.3}],
    ids=lambda recipe: next(iter(recipe)),
)
def test_a_lattice_is_refused_sliders_that_read_their_neighbours(recipe: dict[str, float]) -> None:
    """Pushed through, the engine would box-filter the table as if it were a
    room. The table that came out would load in Premiere and be wrong."""
    (key,) = recipe
    with pytest.raises(ValueError, match=key):
        develop.grade(_lattice(), {**TYPICAL, **recipe}, pointwise=True)


def test_a_lattice_is_refused_white_balance_nobody_measured() -> None:
    """Measured on the lattice itself, "white balance" is the balance of a
    cube of every colour. It must come from a still, or not at all."""
    lattice = _lattice()
    with pytest.raises(ValueError, match="auto_wb"):
        develop.grade(lattice, {"auto_wb": 0.6}, pointwise=True)
    with pytest.raises(ValueError, match="auto_wb"):
        develop.grade(lattice, {"auto_wb": 0.6}, measured={}, pointwise=True)
    reading = develop.measure(SCENES["interior"], {"auto": True, "auto_wb": 0.6})
    with pytest.raises(ValueError, match="auto"):
        develop.grade(
            lattice,
            {"auto": True, "auto_wb": 0.6},
            measured={"wb_gains": reading["wb_gains"]},
            pointwise=True,
        )

    table = develop.grade(lattice, {"auto": True, "auto_wb": 0.6}, measured=reading, pointwise=True)
    assert table.shape == lattice.shape
    # The same reading on the picture it came from, and on the table: one
    # correction. A pixel of the picture and the table's entry for that
    # colour agree, which is all a LUT is.
    white = develop.grade(np.ones((1, 1, 3), dtype=np.float32), {"auto_wb": 0.6}, measured=reading)
    alone = develop.grade(lattice, {"auto_wb": 0.6}, measured=reading, pointwise=True)
    assert np.array_equal(alone[-1, -1, -1], white[0, 0])


@pytest.mark.parametrize(
    "measured",
    [
        {"wb_gains": [1.0, 1.0]},
        {"wb_gains": ["warm", 1.0, 1.0]},
        {"wb_gains": [1.0, float("nan"), 1.0]},
        {"wb_gains": [1.0, 0.0, 1.0]},
        {"wb_gains": 1.1},
        {"levels": [[0.0, 1.0], [0.0, 1.0]]},
        {"levels": [[0.5, 0.5], [0.0, 1.0], [0.0, 1.0]]},
        {"levels": [0.0, 1.0, 0.5]},
    ],
    ids=str,
)
def test_a_stored_reading_of_the_wrong_shape_is_refused_not_rendered(
    measured: dict[str, Any],
) -> None:
    """A reading comes back out of a JSON column. One that is not three
    channels of usable numbers must stop the render in words, not tint it."""
    with pytest.raises(ValueError, match="measured"):
        develop.grade(_lattice(), {"auto": True, "auto_wb": 1.0}, measured=measured)


# ----------------------------------------------- fitting with fewer sliders


def _fit_case() -> tuple[Image.Image, Any, float]:
    base = SCENES["rectangles"].resize((120, 80), Image.Resampling.BOX)
    target = develop.apply_recipe(base, {"exposure": 0.5, "shadows": 0.6, "vibrance": 0.3})
    return base, compare.grid_lab(target, portrait=False), compare.distance(base, target).delta_e


def test_fitting_only_some_sliders_needs_bounds_only_for_those() -> None:
    """The nearest a LUT gets to a look that leans on shadow lift: fit with
    the sliders a LUT carries and nothing else. The fit used to walk every
    slider and look each one's bounds up, so a short list of bounds was a
    KeyError."""
    base, target_lab, start = _fit_case()
    recipe, fitted = compare.fit_recipe(
        base,
        target_lab,
        {"exposure": 0.1, "auto_wb": 0.5},
        develop.apply_recipe,
        {"exposure": (-2.0, 2.0), "vibrance": (-1.0, 1.0)},
        steps=(("exposure", 0.4), ("vibrance", 0.2)),
    )
    assert set(recipe) <= {"exposure", "vibrance"}, "a start value with no bounds is dropped"
    assert recipe["exposure"] > 0.25
    assert fitted < start * 0.6, f"{start:.1f} -> {fitted:.1f}"


def test_a_slider_the_fit_may_not_move_stays_where_it_started() -> None:
    base, target_lab, _start = _fit_case()
    recipe, _fitted = compare.fit_recipe(
        base,
        target_lab,
        {"temperature": 0.2},
        develop.apply_recipe,
        develop.RECIPE_FIELDS,
        steps=(("exposure", 0.4),),
    )
    assert recipe["temperature"] == 0.2
    assert set(recipe) <= {"temperature", "exposure"}


def test_the_fit_moves_every_colour_slider_unless_told_otherwise() -> None:
    """The bake-off's ceiling fit passes no `steps`: it must still be the
    whole list, so its figures mean what they meant."""
    base, target_lab, _start = _fit_case()
    args = (base, target_lab, {}, develop.apply_recipe, develop.RECIPE_FIELDS)
    assert compare.fit_recipe(*args, max_rounds=3) == compare.fit_recipe(
        *args, max_rounds=3, steps=compare.FIT_STEPS
    )
    assert {key for key, _step in compare.FIT_STEPS} >= {"shadows", "window_pull", "auto_wb"}
