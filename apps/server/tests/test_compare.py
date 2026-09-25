"""The bake-off's measurements: colour distance, pairing renamed finals with
their originals, and the best the sliders can do."""

import random

import numpy as np
from PIL import Image, ImageDraw

from framefound.media import compare
from framefound.media import develop as develop_lib


def _scene(seed: int, size: tuple[int, int] = (240, 160)) -> Image.Image:
    """A made-up photograph: a few rectangles and a gradient, different per seed."""
    rng = random.Random(seed)
    image = Image.new("RGB", size, (rng.randint(60, 160),) * 3)
    draw = ImageDraw.Draw(image)
    for _ in range(7):
        x0, y0 = rng.randint(0, size[0] - 40), rng.randint(0, size[1] - 40)
        colour = tuple(rng.randint(20, 235) for _ in range(3))
        draw.rectangle((x0, y0, x0 + rng.randint(20, 120), y0 + rng.randint(15, 80)), fill=colour)
    return image


def test_lab_anchors() -> None:
    lab = compare.to_lab(np.asarray([[1.0, 1.0, 1.0], [0.0, 0.0, 0.0], [0.5, 0.5, 0.5]]))
    assert abs(lab[0, 0] - 100.0) < 0.1 and abs(lab[0, 1]) < 0.1 and abs(lab[0, 2]) < 0.1
    assert abs(lab[1, 0]) < 0.1
    assert 52.0 < lab[2, 0] < 55.0, "sRGB mid-grey is L* ~53, not 50"


def test_distance_says_which_way_an_edit_is_off() -> None:
    scene = _scene(1)
    assert compare.distance(scene, scene).delta_e < 1e-6
    brighter = develop_lib.apply_recipe(scene, {"exposure": 0.6})
    warmer = develop_lib.apply_recipe(scene, {"temperature": 0.5})
    assert compare.distance(brighter, scene).d_lightness > 3.0
    assert compare.distance(warmer, scene).d_b > 3.0, "warmer reads as + b*"
    assert compare.distance(scene, brighter).d_lightness < -3.0


def test_names_survive_the_round_trip() -> None:
    assert compare.name_key("001_IMG_0802.jpg") == compare.name_key("IMG_0802.JPG")
    assert compare.name_key("sub/DJI_20260617_0040_D.JPG") == "dji_20260617_0040_d"
    seo = "01-front-exterior-brick-colonial-130-davis-rd-auction.jpg"
    assert compare.name_key(seo) == seo.removesuffix(".jpg"), "SEO names pass through"


def test_renamed_finals_match_their_originals_by_content() -> None:
    """The organizer renames before the batch goes out, and the edit changes
    every tone — the match has to see through both."""
    originals = {f"IMG_{n:04d}.JPG": _scene(n) for n in range(8)}
    finals = {
        f"{i + 1:02d}-room-{i}-12-maple-st-auction.jpg": develop_lib.apply_recipe(
            image, {"exposure": 0.5, "shadows": 0.4, "vibrance": 0.3, "contrast": 0.15}
        )
        for i, image in enumerate(originals.values())
    }
    truth = dict(zip(finals, originals, strict=True))
    pairs = compare.match(
        {k: compare.descriptor(v) for k, v in originals.items()},
        {k: compare.descriptor(v) for k, v in finals.items()},
    )
    assert len(pairs) == 8
    for pair in pairs:
        assert pair.method == "content"
        assert truth[pair.reference] == pair.original, f"{pair.reference} paired wrongly"


def test_names_win_before_content_and_nothing_is_used_twice() -> None:
    a, b = _scene(3), _scene(4)
    pairs = compare.match(
        {"IMG_1.JPG": compare.descriptor(a), "IMG_2.JPG": compare.descriptor(b)},
        {"img_1.jpg": compare.descriptor(b), "final.jpg": compare.descriptor(b)},
    )
    by_ref = {p.reference: p for p in pairs}
    assert by_ref["img_1.jpg"].original == "IMG_1.JPG" and by_ref["img_1.jpg"].method == "name"
    assert by_ref["final.jpg"].original == "IMG_2.JPG"


def test_a_different_photograph_is_not_forced_into_a_pair() -> None:
    pairs = compare.match(
        {"IMG_1.JPG": compare.descriptor(_scene(10))},
        {"final.jpg": compare.descriptor(_scene(99))},
    )
    assert pairs == [] or pairs[0].score >= compare.MIN_MATCH


def test_the_fit_finds_the_edit_that_made_the_reference() -> None:
    """Sliders fitted to a reference made by known sliders land close to it:
    the ceiling the bake-off reports is a real ceiling, not a guess."""
    base = _scene(7, (160, 108))
    target = develop_lib.apply_recipe(base, {"exposure": 0.5, "temperature": -0.2})
    start_de = compare.distance(base, target).delta_e
    recipe, fitted_de = compare.fit_recipe(
        base,
        compare.grid_lab(target, portrait=False),
        {},
        develop_lib.apply_recipe,
        develop_lib.RECIPE_FIELDS,
    )
    assert fitted_de < start_de * 0.25, f"{start_de:.1f} -> {fitted_de:.1f}"
    assert recipe.get("exposure", 0.0) > 0.25
