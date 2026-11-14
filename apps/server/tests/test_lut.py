"""A look written as a `.cube` file is the colour engine, and says what it is not.

The promise of `media/lut.py` is that the table is the engine's own output,
not a second copy of the look kept in step with it. So the tests that matter
lay a written file back over a picture and compare with what the engine
renders on that picture: byte for byte where the picture's colours sit on
the table's points, within a stated tolerance where a reader has to fill in
between them.

Nothing here stands in for Premiere. `parse_cube` and `apply` are "a reader
that keeps the specification"; that Premiere is one is unproven until it is
tried on the Mac.

The differences measured in these tests are 8-bit levels between two
renders of the same look. They never leave this file: a look is judged by
eye, and no number here grades one.
"""

import json
import random
from typing import Any

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from framefound.media import develop, lut

TYPICAL = {
    "exposure": 0.3, "contrast": 0.15, "temperature": 0.1,
    "tint": -0.05, "vibrance": 0.3, "saturation": 0.1,
}  # fmt: skip
STRONG = {
    "exposure": 0.8, "contrast": 0.4, "temperature": 0.3,
    "vibrance": 0.6, "saturation": 0.3, "shadows": -0.4,
}  # fmt: skip
# Everything a LUT carries, as far as each slider goes.
STOPS = {
    "exposure": 2.0, "contrast": 1.0, "temperature": 1.0, "tint": -1.0,
    "vibrance": 1.0, "saturation": 1.0, "shadows": -1.0,
}  # fmt: skip

# A table with this many points a side has them on whole 8-bit codes (0, 15,
# 30 ... 255), so a picture can be made of nothing but its points. The sizes
# on offer, 33 and 65, put theirs between codes.
ON_CODES = 18


# ------------------------------------------------------------------ pictures


def _room() -> Image.Image:
    """A warm room: a lit wall with a cast, a blown window, a dark sofa, a
    floor. The wall is what auto white balance reads."""
    w, h = 192, 128
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    wall = 0.35 + 0.45 * (xx / w)
    arr = np.stack([wall * 1.00, wall * 0.90, wall * 0.74], axis=-1)
    floor = np.stack([0.42 + 0.1 * xx / w, 0.27 + 0.06 * xx / w, 0.12 + 0.03 * xx / w], axis=-1)
    arr[h * 2 // 3 :] = floor[h * 2 // 3 :]
    arr[h // 6 : h // 2, w // 2 : w * 5 // 6] = (0.99, 0.98, 0.96)
    arr[h // 2 : h * 3 // 4, w // 10 : w * 4 // 10] = (0.09, 0.07, 0.06)
    arr += 0.02 * np.sin(xx * 1.7)[..., None] * np.cos(yy * 0.9)[..., None]
    return Image.fromarray((np.clip(arr, 0.0, 1.0) * 255.0 + 0.5).astype("uint8"))


def _garden() -> Image.Image:
    """Outdoors: a sky fading to the horizon, brick in sun and shade, a lawn,
    a white door and its shadow. Strong colours, smoothly shaded, so most
    pixels fall between a table's points."""
    w, h = 210, 150
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    across, down = xx / w, yy / h
    arr = np.empty((h, w, 3))
    fade = np.clip(down * 3.0, 0.0, 1.0)
    sky = np.stack([0.25 + 0.55 * fade, 0.45 + 0.45 * fade, 0.92 - 0.05 * fade], axis=-1)
    sun = (0.50 + 0.50 * across)[..., None]
    brick = sun * (0.74, 0.26, 0.16)
    lawn = np.stack([0.14 + 0.22 * across, 0.42 + 0.40 * down, 0.10 + 0.05 * across], axis=-1)
    arr[: h // 3] = sky[: h // 3]
    arr[h // 3 : h * 2 // 3] = brick[h // 3 : h * 2 // 3]
    arr[h * 2 // 3 :] = lawn[h * 2 // 3 :]
    arr[h * 2 // 5 : h * 2 // 3, w // 3 : w // 2] = (0.97, 0.97, 0.95)
    arr[h * 2 // 3 : h * 3 // 4, w // 3 : w * 3 // 5] = (0.03, 0.04, 0.05)
    arr += 0.03 * np.sin(xx * 1.3)[..., None] * np.cos(yy * 0.7)[..., None]
    return Image.fromarray((np.clip(arr, 0.0, 1.0) * 255.0 + 0.5).astype("uint8"))


def _swatches() -> Image.Image:
    """Rectangles of any colour at all, blurred into each other: colours a
    real scene would not hold, and every mixture on the way between them."""
    rng = random.Random(11)
    image = Image.new("RGB", (240, 160), (rng.randint(60, 160),) * 3)
    draw = ImageDraw.Draw(image)
    for _ in range(14):
        x0, y0 = rng.randint(0, 200), rng.randint(0, 120)
        colour = tuple(rng.randint(5, 250) for _ in range(3))
        draw.rectangle((x0, y0, x0 + rng.randint(20, 120), y0 + rng.randint(15, 80)), fill=colour)
    return image.filter(ImageFilter.GaussianBlur(5))


SCENES = {"room": _room(), "garden": _garden(), "swatches": _swatches()}


def _every_colour(n: int = ON_CODES) -> Image.Image:
    """Each colour of an n-point table, shuffled into a picture 192 wide
    (a few colours come round twice to fill the last row)."""
    codes = (np.arange(n) * (255 // (n - 1))).astype(np.uint8)
    blue, green, red = np.meshgrid(codes, codes, codes, indexing="ij")
    colours = np.stack([red, green, blue], axis=-1).reshape(-1, 3)
    shuffled = colours[np.random.RandomState(5).permutation(len(colours))]
    rows = -(-len(shuffled) // 192)
    return Image.fromarray(np.resize(shuffled, (rows, 192, 3)))


def _room_on_the_points(n: int = ON_CODES) -> Image.Image:
    """The room with every pixel moved to the nearest point of an n-point
    table, above a chart of all the table's colours. There is still a wall
    with a cast for white balance to read, every point of the table is in
    the picture, and nothing in it lies between points. The room is there
    three times over so the chart's pure blacks and whites stay too few to
    be what auto-levels finds."""
    step = 255 // (n - 1)
    room = (np.round(np.asarray(SCENES["room"]).astype(np.float64) / step) * step).astype("uint8")
    return Image.fromarray(np.vstack([room, room, room, np.asarray(_every_colour(n))]))


def _floats(image: Image.Image) -> Any:
    return np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0


def _levels_apart(one: Image.Image, other: Image.Image) -> tuple[float, float, int]:
    """Mean, 99th percentile and largest difference, in 8-bit levels."""
    apart = np.abs(np.asarray(one).astype(np.int16) - np.asarray(other).astype(np.int16))
    return float(apart.mean()), float(np.percentile(apart, 99)), int(apart.max())


def _file_for(
    recipe: dict[str, Any], still: Image.Image, size: int = lut.DEFAULT_SIZE
) -> tuple[bytes, dict[str, Any]]:
    """A look as the bytes somebody downloads, measured on `still`."""
    carried, left_out = lut.lut_recipe(recipe)
    assert not left_out
    measured = develop.measure(still, carried)
    table = lut.build(carried, size=size, measured=measured)
    return lut.to_cube(table, title="a test look").encode("ascii"), measured


def _through_the_file(
    recipe: dict[str, Any], picture: Image.Image, size: int = lut.DEFAULT_SIZE
) -> Image.Image:
    """The whole journey: built, written out, read back the way a stranger
    to this code would read it, and laid over the picture."""
    data, _measured = _file_for(recipe, picture, size)
    return lut.apply(picture, lut.parse_cube(data))


def _data_lines(text: str) -> list[str]:
    return [line for line in text.split("\n") if line and line[0] in "0123456789-."]


# ------------------------------------------------- the table is the engine


def _looks_on_the_points() -> list[tuple[str, dict[str, Any], Image.Image]]:
    every, room = _every_colour(), _room_on_the_points()
    return [
        ("a typical grade", TYPICAL, every),
        ("a strong grade", STRONG, every),
        ("every carried slider at its stop", STOPS, every),
        ("white balance and a typical grade", {**TYPICAL, "auto_wb": 0.8}, room),
        (
            "levels, white balance and a strong grade",
            {**STRONG, "auto": True, "auto_wb": 1.0},
            room,
        ),
    ]


# The two exact tests below compare a colour graded inside a table with the
# same colour graded inside a picture, with no tolerance. That is only fair
# to ask because every step of the engine a LUT carries works one colour at
# a time in plain arithmetic. Its brightness step used to be a matrix
# product, which NumPy hands to the machine's BLAS, and a BLAS may answer
# differently by where a colour sits; `develop._brightness` says what
# replaced it. This is what a failure by a hair should send the reader to.
IF_BY_A_LAST_PLACE = (
    "If only a handful of values differ, and by one level or one last place, something in the "
    "engine has stopped treating a colour the same wherever it sits (a matrix product handed to "
    "BLAS does that): see develop._brightness, and "
    "test_develop_split.py::test_a_colour_is_graded_the_same_wherever_it_sits."
)


def test_on_its_points_the_table_renders_the_engines_picture_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim the whole module rests on, checked where it can be checked
    without a tolerance: a picture made only of the table's own points, read
    through the table, is `develop.apply_recipe`'s picture. Every byte.

    Temperature moves red and blue opposite ways in all of these, so a table
    written or read with the two swapped could not pass."""
    monkeypatch.setattr(lut, "SIZES", (ON_CODES, *lut.SIZES))
    for name, recipe, picture in _looks_on_the_points():
        measured = develop.measure(picture, recipe)
        table = lut.build(recipe, size=ON_CODES, measured=measured)
        engine = np.asarray(develop.apply_recipe(picture, recipe))
        through = np.asarray(lut.apply(picture, table))
        assert not np.array_equal(engine, np.asarray(picture)), f"{name} changed nothing"
        apart = np.abs(through.astype(np.int16) - engine.astype(np.int16))
        assert not apart.any(), (
            f"{name}: the table is not the engine. {int((apart > 0).sum())} of {apart.size} "
            f"values differ, by {int(apart.max())} at most. {IF_BY_A_LAST_PLACE}"
        )
        if "auto_wb" in recipe:
            assert measured["wb_gains"] != [1.0, 1.0, 1.0], "the room has a cast to remove"
        if "auto" in recipe:
            assert measured["levels"] != [[0.0, 1.0]] * 3, "and blacks to stretch down"


def test_the_written_file_laid_over_a_picture_is_the_engine_to_the_sixth_decimal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same, through the bytes somebody downloads. The file keeps six
    decimals, so the only values allowed to differ are ones the engine left
    within that rounding of the edge between two 8-bit levels, and those by
    one level. Anything else differing is a table that is not the engine."""
    monkeypatch.setattr(lut, "SIZES", (ON_CODES, *lut.SIZES))
    rounding = 255.0 * 10.0**-lut.DECIMALS
    for name, recipe, picture in _looks_on_the_points():
        data, measured = _file_for(recipe, picture, ON_CODES)
        written = np.asarray(lut.apply(picture, lut.parse_cube(data))).astype(np.int16)
        engine = np.asarray(develop.apply_recipe(picture, recipe)).astype(np.int16)

        exact = develop.grade(_floats(picture), develop.clean_recipe(recipe), measured=measured)
        scaled = exact.astype(np.float64) * 255.0 + 0.5
        on_an_edge = np.abs(scaled - np.round(scaled)) < rounding
        differs = written != engine
        assert not (differs & ~on_an_edge).any(), f"{name}: the file is not the engine"
        assert int(np.abs(written - engine).max()) <= 1, name
        assert differs.mean() < 0.001, f"{name}: {int(differs.sum())} values differ"


@pytest.mark.parametrize("size", lut.SIZES)
def test_the_table_is_the_engine_at_every_point_in_the_files_order(size: int) -> None:
    """At the sizes on offer the points fall between 8-bit codes, so this is
    checked in floats: the colours of the grid, listed here the way the file
    lists them (red fastest) and graded as a one-row PICTURE, are the table.
    The white balance and levels are a still's, handed over frozen."""
    recipe = {**TYPICAL, "shadows": -0.4, "auto": True, "auto_wb": 0.8}
    reading = json.loads(json.dumps(develop.measure(SCENES["room"], recipe)))
    index = np.arange(size**3)
    listed = np.stack([index % size, index // size % size, index // size**2], axis=-1)
    row = (listed.astype(np.float32) / np.float32(size - 1))[None]

    as_a_picture = develop.grade(row, develop.clean_recipe(recipe), measured=reading)
    table = lut.build(recipe, size=size, measured=reading)
    assert table.shape == (size, size, size, 3) and table.dtype == np.float32
    differ = table.reshape(-1, 3) != as_a_picture[0]
    assert not differ.any(), (
        f"the table is not the engine at {int(differ.sum())} of {differ.size} values. "
        f"{IF_BY_A_LAST_PLACE}"
    )


def test_an_empty_look_is_the_identity_table() -> None:
    """No sliders: every point is its own colour, and a picture read through
    the table comes back untouched - every 8-bit code, at both sizes."""
    ramp = np.arange(256)
    codes = np.stack([ramp, ramp[::-1], (ramp * 7) % 256], axis=-1).astype(np.uint8)[None]
    for size in lut.SIZES:
        table = lut.build({}, size=size)
        assert np.array_equal(table, lut.identity(size))
        assert table[0, 0, 0].tolist() == [0.0, 0.0, 0.0]
        assert table[-1, -1, -1].tolist() == [1.0, 1.0, 1.0]
        assert table[2, 1, 0].tolist() == pytest.approx([0.0, 1 / (size - 1), 2 / (size - 1)])
        for picture in (Image.fromarray(codes), SCENES["garden"]):
            assert np.array_equal(np.asarray(lut.apply(picture, table)), np.asarray(picture))
    sliders_at_rest = {"exposure": 0.0, "auto": False, "auto_wb": 0}
    assert np.array_equal(lut.build(sliders_at_rest), lut.identity())


# ------------------------------------------- between the points: how close


def test_a_typical_grade_through_the_lut_matches_the_engine() -> None:
    """Between its points a 33-point table is read by interpolation. For an
    ordinary look that lands within half a level on average and four levels
    for all but one pixel in a hundred (measured: about 0.1 and 2)."""
    for name, picture in SCENES.items():
        engine = develop.apply_recipe(picture, TYPICAL)
        mean, most, worst = _levels_apart(_through_the_file(TYPICAL, picture), engine)
        assert mean <= 0.5 and most <= 4 and worst <= 8, f"{name}: {mean:.2f} / {most} / {worst}"
        # And it is the look, not the picture it started from.
        assert _levels_apart(picture, engine)[0] > 10


def test_the_finer_table_is_no_worse() -> None:
    """65 points is offered for looks that clip hard. If it were ever
    further from the engine than 33 there would be no reason to offer it."""
    for recipe in (TYPICAL, STRONG):
        # Nothing in these looks is measured, so one file serves every scene.
        tables = [lut.parse_cube(_file_for(recipe, SCENES["room"], size)[0]) for size in (33, 65)]
        for name, picture in SCENES.items():
            engine = develop.apply_recipe(picture, recipe)
            coarse = _levels_apart(lut.apply(picture, tables[0]), engine)
            fine = _levels_apart(lut.apply(picture, tables[1]), engine)
            assert fine[0] <= coarse[0] and fine[1] <= coarse[1] and fine[2] <= coarse[2], name
            assert fine[0] < coarse[0] * 0.6, f"{name}: {coarse[0]:.3f} -> {fine[0]:.3f}"


def test_white_balance_frozen_on_a_still_matches_the_engine_on_that_still() -> None:
    """Auto white balance reads a whole picture; the table gets the three
    numbers it read off one still. On that still the file and the engine
    then agree - and a file without the correction plainly does not."""
    room = SCENES["room"]
    recipe = {"auto_wb": 1.0, "exposure": 0.3, "vibrance": 0.3}
    engine = develop.apply_recipe(room, recipe)
    data, measured = _file_for(recipe, room)
    red, _green, blue = measured["wb_gains"]
    assert red < 0.97 and blue > 1.03, "the warm wall is pulled back toward neutral"

    mean, most, _worst = _levels_apart(lut.apply(room, lut.parse_cube(data)), engine)
    assert mean <= 0.5 and most <= 4, f"{mean:.2f} / {most}"

    uncorrected = lut.build(recipe, measured={"wb_gains": [1.0, 1.0, 1.0]})
    assert _levels_apart(lut.apply(room, uncorrected), engine)[0] > 3


def test_a_frozen_white_balance_does_not_follow_the_light() -> None:
    """The limit the docstring states, pinned so nobody "fixes" it by
    measuring the table: the correction belongs to the still it was read
    from. On a frame of the same room under cooler light the file still
    applies the warm room's numbers - which is what the engine does when
    handed that reading, and not what it does measuring the frame afresh."""
    warm = SCENES["room"]
    cool = Image.fromarray(
        np.clip(np.asarray(warm) * np.asarray((0.84, 1.0, 1.2)), 0, 255).astype("uint8")
    )
    recipe = {"auto_wb": 1.0, "exposure": 0.3}
    data, reading = _file_for(recipe, warm)
    through = lut.apply(cool, lut.parse_cube(data))

    handed_the_reading = develop.grade(_floats(cool), recipe, measured=reading)
    as_the_file_has_it = Image.fromarray((handed_the_reading * 255.0 + 0.5).astype("uint8"))
    assert _levels_apart(through, as_the_file_has_it)[0] <= 0.5
    assert _levels_apart(through, develop.apply_recipe(cool, recipe))[0] > 5


def test_between_points_a_colour_is_mixed_from_four_corners_not_eight() -> None:
    """Tetrahedral interpolation, which the specification asks a reader to
    use, and which these tables are judged with. A table that is black
    everywhere but its white corner tells the two apart: mixed from the four
    corners of a tetrahedron a colour gets its smallest channel; mixed from
    all eight (trilinear) it would get the product of the three."""
    table = np.zeros((2, 2, 2, 3), dtype=np.float32)
    table[1, 1, 1] = 1.0
    picture = Image.fromarray(np.asarray([[(200, 100, 50), (30, 240, 90)]], dtype=np.uint8))
    assert np.asarray(lut.apply(picture, table)).tolist() == [[[50, 50, 50], [30, 30, 30]]]

    # Only the red corner lit: what is left is how far red leads the others.
    table = np.zeros((2, 2, 2, 3), dtype=np.float32)
    table[0, 0, 1] = 1.0
    assert np.asarray(lut.apply(picture, table)).tolist() == [[[100, 100, 100], [0, 0, 0]]]


def test_reading_a_picture_through_a_table_keeps_its_shape() -> None:
    table = lut.build(TYPICAL)
    tall = SCENES["garden"].resize((40, 90))
    assert lut.apply(tall, table).size == (40, 90)
    assert lut.apply(tall, table).mode == "RGB"
    grey = tall.convert("L")
    assert np.array_equal(
        np.asarray(lut.apply(grey, table)), np.asarray(lut.apply(grey.convert("RGB"), table))
    )
    assert np.array_equal(
        np.asarray(lut.apply(tall, table.tolist())), np.asarray(lut.apply(tall, table))
    )


def test_a_large_frame_is_read_in_pieces_and_comes_out_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full-size frame goes through the table a band of pixels at a time
    to keep the working room small. Where one band ends and the next begins
    must not show."""
    table = lut.build(STRONG)
    picture = SCENES["garden"]
    whole = np.asarray(lut.apply(picture, table))
    monkeypatch.setattr(lut, "_CHUNK", 997)
    assert picture.width * picture.height > 30 * 997
    assert np.array_equal(np.asarray(lut.apply(picture, table)), whole)


def test_a_table_from_elsewhere_that_leaves_zero_to_one_is_clipped_when_read() -> None:
    """The format allows any value; FrameFound's own tables never use that,
    but a table read from a file might. Out of range becomes black or white,
    not a number that wraps round to some other colour."""
    stretched = lut.identity(5) * 3.0 - 1.0
    picture = Image.fromarray(np.asarray([[(0, 64, 128), (191, 255, 100)]], dtype=np.uint8))
    assert np.asarray(lut.apply(picture, stretched)).tolist() == [
        [[0, 0, 129], [255, 255, 45]],
    ]


@pytest.mark.parametrize(
    "not_a_table",
    [
        np.zeros((33, 33, 3), dtype=np.float32),
        np.zeros((4, 4, 5, 3), dtype=np.float32),
        np.zeros((4, 4, 4, 4), dtype=np.float32),
        np.zeros((1, 1, 1, 3), dtype=np.float32),
        np.full((2, 2, 2, 3), np.nan, dtype=np.float32),
    ],
    ids=["a picture", "not a cube", "four channels", "one point", "not numbers"],
)
def test_something_that_is_not_a_table_is_refused(not_a_table: Any) -> None:
    with pytest.raises(ValueError, match="table"):
        lut.apply(SCENES["room"], not_a_table)
    with pytest.raises(ValueError, match="table"):
        lut.to_cube(not_a_table, title="x")


# ------------------------------------------------ what a LUT cannot carry


def test_what_a_lut_cannot_carry_is_named_not_dropped() -> None:
    """The listing preset leans on shadow lift, window pull and local
    contrast. A LUT keeps the rest and SAYS what went: a look that quietly
    lost them is a darker picture than the one promised."""
    carried, left_out = lut.lut_recipe(develop.LISTING_PRESET)
    assert carried == {"exposure": 0.22, "vibrance": 0.42, "auto_wb": 1.0}
    assert left_out == ["shadows", "window_pull", "local_contrast"]
    assert lut.left_out_words(left_out) == ["lifting the shadows", "window pull", "local contrast"]

    # Darkening the shadows is per-pixel and stays; lifting them reads the room.
    assert lut.lut_recipe({"shadows": -0.4}) == ({"shadows": -0.4}, [])
    assert lut.lut_recipe({"shadows": 0.4}) == ({}, ["shadows"])

    everything = {
        **{key: high for key, (_low, high) in develop.RECIPE_FIELDS.items()},
        "auto": True,
        "sky": {"name": "dusk.jpg"},
    }
    carried, left_out = lut.lut_recipe(everything)
    assert left_out == list(lut.LEFT_OUT_WORDS), "named in the order the page lists them"
    assert set(carried) == {*develop.POINTWISE_KEYS, *develop.MEASURED_KEYS}
    assert develop.reads_neighbours(carried) == []


def test_narrowing_cleans_the_recipe_and_leaves_the_callers_alone() -> None:
    stored = {"exposure": 9.0, "vibrance": "lots", "made_up": 1.0, "highlights": -0.2, "tint": 0}
    before = dict(stored)
    assert lut.lut_recipe(stored) == ({"exposure": 2.0}, ["highlights"])
    assert stored == before


def test_every_slider_the_engine_has_is_either_carried_or_has_words() -> None:
    """A slider added to the engine later must be decided about here. Until
    somebody does, it is left out (and named by its key) rather than pushed
    through a table it may be wrong for."""
    for key, (low, high) in develop.RECIPE_FIELDS.items():
        for value in (low, high):
            if value == 0.0:
                continue
            carried, left_out = lut.lut_recipe({key: value})
            assert (key in carried) != (key in left_out), key
            assert all(name in lut.LEFT_OUT_WORDS for name in left_out), key
    assert lut.left_out_words(["sky", "dehaze_strength"]) == ["sky replacement", "dehaze strength"]


def test_a_table_is_never_built_from_what_it_cannot_carry() -> None:
    """`build` does not narrow for the caller: narrowing has to be said to
    the operator, so it must be a step somebody took on purpose."""
    with pytest.raises(ValueError, match="shadows, window_pull, local_contrast"):
        lut.build(develop.LISTING_PRESET, measured={"wb_gains": [1.0, 1.0, 1.0]})
    with pytest.raises(ValueError, match="rotate"):
        lut.build({**TYPICAL, "rotate": 1.5})
    with pytest.raises(ValueError, match="sky"):
        lut.build({**TYPICAL, "sky": {"name": "dusk.jpg"}})


def test_a_table_is_never_built_from_a_white_balance_nobody_measured() -> None:
    """Measured on the grid itself, "white balance" is the balance of a cube
    of every colour: a table that loads anywhere and is wrong."""
    with pytest.raises(ValueError, match="auto_wb"):
        lut.build({"auto_wb": 0.6, "exposure": 0.2})
    with pytest.raises(ValueError, match="auto"):
        lut.build({"auto": True}, measured={"wb_gains": [1.0, 1.0, 1.0]})
    with pytest.raises(ValueError, match="measured"):
        lut.build({"auto_wb": 0.6}, measured={"wb_gains": [1.0, 1.0]})
    assert lut.build({"auto_wb": 0.6}, measured={"wb_gains": [0.9, 1.0, 1.1]}).shape[0] == 33


@pytest.mark.parametrize("size", [17, 32, 0, 257, 33.0, "33", True, None])
def test_only_the_sizes_on_offer_are_built(size: Any) -> None:
    with pytest.raises(ValueError, match="33 or 65"):
        lut.build(TYPICAL, size=size)


def test_a_table_is_built_from_the_recipe_as_the_engine_would_clean_it() -> None:
    """Sliders come back out of a JSON column. A value past its stop is the
    stop, as it is for a photograph, not a table of something stronger."""
    stored = {"exposure": 9.0, "vibrance": "lots", "made_up": 1.0}
    assert np.array_equal(lut.build(stored), lut.build({"exposure": 2.0}))
    assert not np.array_equal(lut.build(stored), lut.identity())


def test_the_table_never_leaves_zero_to_one() -> None:
    """Every slider at its stop, both ways. The engine clips, the table is
    the engine, and the file has no minus sign and nothing above one in it -
    so every reader agrees about it, whatever it does with values outside."""
    for recipe in (STOPS, {key: -value for key, value in STOPS.items() if key != "shadows"}):
        for size in lut.SIZES:
            table = lut.build(recipe, size=size)
            assert float(table.min()) >= 0.0 and float(table.max()) <= 1.0
        rows = _data_lines(lut.to_cube(lut.build(recipe), title="stops"))
        assert len(rows) == 33**3
        assert not any("-" in row for row in rows)
        assert all(0.0 <= float(value) <= 1.0 for row in rows for value in row.split())

    # A clip can leave a minus zero behind. "-0.000000" is a legal number
    # and an alarming thing to find in a file, so it is written as zero.
    minus_zero = lut.identity(2) * np.float32(-0.0)
    assert np.signbit(minus_zero).all()
    assert set(_data_lines(lut.to_cube(minus_zero, title="zero"))) == {"0.000000 0.000000 0.000000"}


# ------------------------------------------------------------------ the file


def test_red_changes_fastest_in_the_file() -> None:
    """Backwards, red and blue are swapped and the file still "looks like a
    LUT". The second line of the table differs from the first in red only;
    green first moves a row of points later, blue a whole plane later."""
    n = lut.DEFAULT_SIZE
    rows = _data_lines(lut.to_cube(lut.identity(n), title="identity"))
    step = f"{1 / (n - 1):.6f}"
    assert rows[0] == "0.000000 0.000000 0.000000"
    assert rows[1] == f"{step} 0.000000 0.000000"
    assert rows[n] == f"0.000000 {step} 0.000000"
    assert rows[n * n] == f"0.000000 0.000000 {step}"
    assert rows[-1] == "1.000000 1.000000 1.000000"


# Annex A.4 of Adobe's Cube LUT Specification 1.0, as printed (with the line
# ends it says to convert to newlines): the function {r, (3 * g + b) / 4, b}.
THE_SPECIFICATIONS_OWN_EXAMPLE = (
    "# Created Tue 19 Mar 2013\n"
    "LUT_3D_SIZE 2\n"
    "0 0 0\n1 0 0\n0 .75 0\n1 .75 0\n0 .25 1\n1 .25 1\n0 1 1\n1 1 1\n"
)


def test_the_specifications_own_example_reads_and_writes_as_it_prints_it() -> None:
    """An anchor outside this code for which way round the table goes."""
    points = lut.identity(2)
    red, green, blue = points[..., 0], points[..., 1], points[..., 2]
    table = np.stack([red, (3 * green + blue) / 4, blue], axis=-1)

    assert np.array_equal(lut.parse_cube(THE_SPECIFICATIONS_OWN_EXAMPLE), table)
    written = [
        [float(value) for value in row.split()] for row in _data_lines(lut.to_cube(table, title=""))
    ]
    printed = [
        [float(value) for value in row.split()]
        for row in _data_lines(THE_SPECIFICATIONS_OWN_EXAMPLE)
    ]
    assert written == printed


def test_keywords_come_before_the_table_and_once_each() -> None:
    """ "All keywords shall appear before any table data" and "each keyword
    shall appear at most once". In the order of the specification's own
    writer: the title, the comments, the domain, the size, the table."""
    text = lut.to_cube(lut.build(TYPICAL), title="DJI_0639 - model", comments=["one", "two"])
    assert text.endswith("\n") and not text.endswith("\n\n")
    lines = text[:-1].split("\n")
    assert lines[:6] == [
        'TITLE "DJI_0639 - model"',
        "# one",
        "# two",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
        "LUT_3D_SIZE 33",
    ]
    assert lines[6:] == _data_lines(text) and len(lines) == 6 + 33**3
    for keyword in ("TITLE", "DOMAIN_MIN", "DOMAIN_MAX", "LUT_3D_SIZE"):
        assert sum(line.startswith(keyword) for line in lines) == 1
    assert all(len(row.split()) == 3 for row in lines[6:])

    untitled = lut.to_cube(lut.identity(2), title="  ")
    assert untitled.startswith("DOMAIN_MIN"), "no title is no TITLE line, not an empty one"


def test_the_file_is_plain_ascii_with_unix_newlines_and_short_lines() -> None:
    """The rules that fail quietly: a carriage return makes it "not a valid
    cube file", a byte outside plain ASCII is not a text character, and a
    line may be 250 bytes at most. A clip called anything, and a comment of
    any length, must still come out as a file that reads back."""
    long_word = "x" * 400
    many_words = " ".join(f"word{n}" for n in range(120))
    table = lut.build(TYPICAL)
    text = lut.to_cube(
        table,
        title='Ch\u00e2teau "d\u2019\u00e9t\u00e9" \u2014 DJI_0639.MP4 ' + "t" * 300,
        comments=[
            "Look for Ch\u00e2teau d\u2019\u00e9t\u00e9 \U0001f3a5.MP4 \u2013 4 \u00d7 3",
            long_word,
            many_words,
            'first\r\nLUT_3D_SIZE 2\n0 0 0\tTITLE "mine"',
            "",
        ],
    )
    data = text.encode("ascii")
    assert b"\r" not in data
    assert set(data) <= {0x0A, *range(0x20, 0x7F)}
    lines = text.split("\n")
    assert max(len(line) for line in lines) <= 250 == lut.MAX_LINE_BYTES

    assert lines[0].startswith("TITLE \"Chateau 'd'ete' - DJI_0639.MP4 ttt") and lines[0].endswith(
        '"'
    )
    assert lines[0].count('"') == 2, "a quote inside the title would end it early"
    assert lines[1] == "# Look for Chateau d'ete ?.MP4 - 4 x 3"
    comments = [line for line in lines if line.startswith("#")]
    assert "".join(line[2:] for line in comments[1:3]) == long_word, "cut, nothing lost"
    assert " ".join(line[2:] for line in comments[3:-2]) == many_words, "wrapped at spaces"
    # A line break inside a comment cannot start a line of its own: the
    # keyword it tried to smuggle in is still inside the comment.
    assert comments[-2] == '# first  LUT_3D_SIZE 2 0 0 0 TITLE "mine"'
    assert comments[-1] == "#"
    assert sum(line.startswith("LUT_3D_SIZE") for line in lines) == 1
    assert np.allclose(lut.parse_cube(data), table, atol=1e-6)


def test_a_file_reads_back_as_the_table_it_was_written_from() -> None:
    """To the sixth decimal, which is all the file keeps - and written out
    again the read-back table is the same file, so nothing drifts."""
    for size, recipe in ((33, STRONG), (65, TYPICAL)):
        table = lut.build(recipe, size=size)
        text = lut.to_cube(table, title="round trip", comments=["a comment"])
        back = lut.parse_cube(text)
        assert back.shape == table.shape and back.dtype == np.float32
        assert float(np.abs(back - table).max()) <= 0.5 * 10.0**-lut.DECIMALS + 1e-7
        assert lut.to_cube(back, title="round trip", comments=["a comment"]) == text
        assert np.array_equal(lut.parse_cube(text.encode("ascii")), back), "bytes or text alike"


def test_a_reader_takes_what_the_specification_allows() -> None:
    """Comments anywhere, blank lines, tabs and spaces round a line, numbers
    with exponents or without a leading zero, and no title or domain."""
    text = (
        "# made elsewhere\n\n"
        "LUT_3D_SIZE\t2\n"
        "DOMAIN_MAX 1 1.0 1e0\n"
        "0 0 0\n"
        "  1.0 0 0\t\n"
        "# halfway\n"
        "0 1. 0\n1 1 0\n\n0 0 1\n1E0 0 1\n0 1 1\n+1 1 .1e1\n"
    )
    assert np.array_equal(lut.parse_cube(text), lut.identity(2))


def _two_point_file(**changes: str) -> str:
    parts = {
        "title": 'TITLE "t"\n',
        "domain": "DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n",
        "size": "LUT_3D_SIZE 2\n",
        "table": "0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n",
        "after": "",
    }
    return "".join({**parts, **changes}.values())


@pytest.mark.parametrize(
    ("text", "says"),
    [
        (_two_point_file().replace("\n", "\r\n"), "carriage return"),
        (_two_point_file(title='TITLE "\u00e9t\u00e9"\n'), "line 1: .*plain ASCII"),
        (_two_point_file(title="# " + "x" * 249 + "\n"), "line 1: longer than 250"),
        (_two_point_file(after='TITLE "late"\n'), "TITLE comes after the table"),
        (_two_point_file(size="LUT_3D_SIZE 2\nLUT_3D_SIZE 2\n"), "LUT_3D_SIZE appears twice"),
        (_two_point_file(title='TITLE "a" "b"\n'), "title"),
        (_two_point_file(title="TITLE unquoted\n"), "title"),
        (_two_point_file(domain="DOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 2 3\n"), "domain 0 to 1"),
        (_two_point_file(domain="DOMAIN_MIN 0 0\n"), "domain 0 to 1"),
        (_two_point_file(size="LUT_1D_SIZE 2\n"), "one-dimensional"),
        (_two_point_file(size="LUT_3D_SIZE 1\n"), "2 to 256 points"),
        (_two_point_file(size="LUT_3D_SIZE 300\n"), "2 to 256 points"),
        (_two_point_file(size="LUT_3D_SIZE two\n"), "2 to 256 points"),
        (_two_point_file(size=""), "no LUT_3D_SIZE"),
        (_two_point_file(size="LUT_3D_SIZE 3\n"), "8 lines; 3 points a side needs 27"),
        (_two_point_file(after="1 1 1\n"), "9 lines; 2 points a side needs 8"),
        (_two_point_file(after="1 1\n"), "three numbers"),
        (_two_point_file(after="1 1 1 1\n"), "three numbers"),
        (_two_point_file(after="0.5 0x1 1_0\n"), "three numbers"),
        (_two_point_file(size="LUT_3D_SIZE 2\nLUT_IN_VIDEO_RANGE\n"), "not a keyword"),
        (
            _two_point_file(table="0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1e39\n"),
            "too large",
        ),
        (
            _two_point_file(table="0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\nnan 1 1\n"),
            "line 12: a line of the table is three numbers",
        ),
    ],
    ids=lambda value: None if "\n" in value else value,
)
def test_a_file_that_breaks_a_rule_is_refused_with_the_rule(text: str, says: str) -> None:
    """The reader here stands in for a strict one, so the round trip means
    something: a file it accepts keeps the specification's rules."""
    with pytest.raises(ValueError, match=says):
        lut.parse_cube(text)
    assert lut.parse_cube(_two_point_file()).shape == (2, 2, 2, 3)


def test_bytes_that_are_not_ascii_are_not_a_cube_file() -> None:
    with pytest.raises(ValueError, match="plain ASCII"):
        lut.parse_cube(_two_point_file(title='TITLE "\u00e9t\u00e9"\n').encode("utf-8"))


# ------------------------------------------------- what the file says of itself


def test_the_comments_say_what_the_file_is_and_what_it_assumes() -> None:
    """Opened in a text editor a year on, the file explains itself: the
    clip, the look, what it was made for, the sliders, where the white
    balance was measured and what it came to, what was left out, and the
    model. Word for word, because the page and the docs say the same."""
    block = lut.comment_block(
        clip="DJI_0639.MP4",
        look="the local model's suggestion",
        size=33,
        recipe={"vibrance": 0.2, "exposure": 0.25, "auto_wb": 0.6, "temperature": 0.08},
        measured={"wb_gains": [1.062, 1.0, 0.921]},
        measured_at_ms=7000,
        provenance={
            "model": "mlx-community/gemma-4-e4b-it-4bit",
            "model_revision": "475b9088d29754a3379866cf5aeb6b41acd313c2",
            "runtime": "mlx-vlm",
            "runtime_version": "0.7.12",
            "prompt_version": "colour-1",
        },
    )
    assert block == [
        "FrameFound look for DJI_0639.MP4 - the local model's suggestion",
        "The table is FrameFound's colour engine at each of 33 x 33 x 33 points.",
        "Made for ordinary (Rec. 709) video as it looks in FrameFound's preview.",
        "Not for log or HDR footage unless Premiere shows it the same way.",
        "Values 0 to 1 in, 0 to 1 out.",
        'Sliders: {"auto_wb": 0.6, "exposure": 0.25, "temperature": 0.08, "vibrance": 0.2}',
        "White balance measured on the still at 0:07.000 - gains 1.062000 1.000000 0.921000",
        "A LUT cannot carry, so left out: (nothing)",
        "Model: mlx-community/gemma-4-e4b-it-4bit @ 475b9088 - mlx-vlm 0.7.12 - prompt colour-1",
    ]
    assert " ".join(block[2:4]) == lut.ASSUMES, "the page's sentence and the file's are one"

    text = lut.to_cube(lut.identity(2), title="DJI_0639 - FrameFound model", comments=block)
    assert text.split("\n")[1:10] == [f"# {line}" for line in block]


def test_the_comments_follow_the_look_they_describe() -> None:
    """What was left out is named in words; levels are recorded like white
    balance; a look nobody measured or no model made has no line claiming
    otherwise; and a moment past the hour reads as one."""
    learned = lut.comment_block(
        clip="A001.mov",
        look="your photograph look",
        size=65,
        recipe={"auto": True, "exposure": 0.31},
        measured={"levels": [[0.02, 0.97], [0.0, 1.0], [0.01, 0.9]]},
        measured_at_ms=3_723_456,
        left_out=["shadows", "window_pull", "local_contrast"],
    )
    assert "The table is FrameFound's colour engine at each of 65 x 65 x 65 points." in learned
    assert (
        "Levels measured on the still at 1:02:03.456 - red 0.020000 to 0.970000, "
        "green 0.000000 to 1.000000, blue 0.010000 to 0.900000"
    ) in learned
    assert learned[-1] == (
        "A LUT cannot carry, so left out: lifting the shadows, window pull, local contrast"
    )
    assert not any(line.startswith(("Model:", "White balance")) for line in learned)

    by_hand = lut.comment_block(
        clip="A001.mov",
        look="your own",
        size=33,
        recipe={"auto_wb": 0.5},
        measured={"wb_gains": [0.95, 1.0, 1.06]},
        provenance={"runtime": "mlx-vlm"},
    )
    assert "White balance measured on a still - gains 0.950000 1.000000 1.060000" in by_hand
    assert by_hand[-1] == "Model: mlx-vlm"
    # Gains kept for a look whose white balance has since been turned off
    # are not what the table used, so the file does not quote them.
    turned_off = lut.comment_block(
        clip="A001.mov", look="x", size=33, recipe={}, measured={"wb_gains": [0.95, 1.0, 1.06]}
    )
    assert not any("White balance" in line for line in turned_off)


def test_a_made_up_suggestion_says_so_on_its_first_line() -> None:
    """Demo mode invents sliders. A file made from them must not be
    mistaken for a model's judgement by whoever finds it in a project."""
    made_up = lut.comment_block(
        clip="DJI_0639.MP4", look="the local model's suggestion", size=33, recipe=TYPICAL, demo=True
    )
    assert made_up[0] == "DEMONSTRATION - these sliders were made up, not suggested by a model"
    text = lut.to_cube(lut.build(TYPICAL), title="DJI_0639 - FrameFound model", comments=made_up)
    first_comment = next(line for line in text.split("\n") if line.startswith("#"))
    assert first_comment == f"# {made_up[0]}"

    real = lut.comment_block(
        clip="DJI_0639.MP4", look="the local model's suggestion", size=33, recipe=TYPICAL
    )
    assert real == made_up[1:] and "DEMONSTRATION" not in " ".join(real)


def test_a_long_look_is_wrapped_in_the_file_not_cut() -> None:
    """Sliders with long decimals run past 250 bytes as one line of JSON."""
    recipe = {key: 0.123456789012345678 * (1 + n) for n, key in enumerate(develop.POINTWISE_KEYS)}
    recipe.update({"shadows": -0.3333333333333333, "auto_wb": 0.6666666666666666})
    padded = {**recipe, **{f"note_{n}": "kept as written by an earlier version" for n in range(4)}}
    block = lut.comment_block(clip="A.mov", look="x", size=33, recipe=padded)
    sliders = next(line for line in block if line.startswith("Sliders: "))
    assert len(sliders) > 250

    text = lut.to_cube(lut.identity(2), title="t", comments=block)
    assert max(len(line) for line in text.split("\n")) <= 250
    comments = " ".join(line[2:] for line in text.split("\n") if line.startswith("# "))
    start = comments.index("Sliders: ") + len("Sliders: ")
    end = comments.index(" A LUT cannot carry")
    assert json.loads(comments[start:end]) == padded


# ------------------------------------------------------------------ the name

# A clip named in Greek ("video") with a film-camera emoji: nothing in it is
# a character the file's name may hold.
NOT_LATIN = "".join(map(chr, (0x3B2, 0x3AF, 0x3BD, 0x3C4, 0x3B5, 0x3BF, 0x20, 0x1F3A5))) + ".mov"


def test_the_files_name_is_the_clips_and_safe_anywhere() -> None:
    assert lut.cube_filename("DJI_0639.MP4", "model") == "DJI_0639 - FrameFound model.cube"
    assert lut.cube_filename("2026/Brookside/DJI_0639.MP4", "chosen") == (
        "DJI_0639 - FrameFound chosen.cube"
    )
    assert lut.cube_filename("C:\\clips\\take 2 (final).mov", "learned") == (
        "take 2 (final) - FrameFound learned.cube"
    )
    assert lut.cube_filename("\u00c9t\u00e9 \u00e0 la plage.mov", "model") == (
        "Ete a la plage - FrameFound model.cube"
    )
    assert lut.cube_filename("", "") == "clip - FrameFound look.cube"
    assert lut.clip_stem("....") == "clip"
    assert lut.clip_stem("x" * 300 + ".mp4") == "x" * 100
    # A name with no letter the file's character set has is "clip", not a
    # row of question marks turned into underscores.
    assert lut.clip_stem(NOT_LATIN) == "clip"
    assert lut.clip_stem("_DSC0042.MOV") == "_DSC0042"

    for hostile in (
        "../../etc/passwd",
        'a"b;c:d*e?.mp4',
        "con\r\nContent-Type: text/html.mp4",
        NOT_LATIN,
        ".hidden",
    ):
        for source in ("model", "../x", 'a"b\n'):
            name = lut.cube_filename(hostile, source)
            assert name.isascii() and name.endswith(".cube") and " - FrameFound " in name
            assert not set(name) & set('/\\:*?"<>|;\r\n\t'), name
            assert not name.startswith((".", " ")), name
