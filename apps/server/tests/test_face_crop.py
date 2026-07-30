"""Cropping a face out of the frame it was found in.

This exists because the crop was wrong in two independent ways and both looked
plausible on screen. It was cut from the *asset's* thumbnail rather than the
frame the face was detected in — and 161 of 307 faces on the reference
deployment came from frames partway through a video, so most of the grid showed
unrelated content. Underneath that, `object-fit: cover` had already cropped the
image before any box maths applied.

Neither was a detector problem: those faces averaged 0.73 confidence. The tests
here pin the geometry against images whose contents are known, so a crop that
lands in the wrong place fails rather than merely looking odd.
"""

import io

import pytest

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402


def _frame(width: int, height: int, marker: tuple[int, int, int, int]) -> Image.Image:
    """A black frame with one white rectangle at `marker` (x1, y1, x2, y2)."""
    image = Image.new("RGB", (width, height), (0, 0, 0))
    for x in range(marker[0], marker[2]):
        for y in range(marker[1], marker[3]):
            image.putpixel((x, y), (255, 255, 255))
    return image


def _crop_like_the_api(
    image: Image.Image,
    box: tuple[float, float, float, float],
    size: int = 64,
    padding: float = 0.35,
) -> Image.Image:
    """The same geometry as `people.face_crop`, kept in step by the tests below."""
    box_x, box_y, box_w, box_h = box
    width, height = image.size
    cx = (box_x + box_w / 2) * width
    cy = (box_y + box_h / 2) * height
    half = max(box_w * width, box_h * height) * (1 + padding * 2) / 2
    return image.crop(
        (
            int(max(0, cx - half)),
            int(max(0, cy - half)),
            int(min(width, cx + half)),
            int(min(height, cy + half)),
        )
    ).resize((size, size), Image.Resampling.LANCZOS)


def _brightness(image: Image.Image) -> float:
    pixels = list(image.convert("L").getdata())
    return sum(pixels) / len(pixels)


# A correct crop is never all marker: 0.35 padding on each side makes the
# window 1.7x the box, so the marker covers 1/1.7**2 — about 35% — of it, or
# roughly 88 mean brightness against a black frame. A miss reads near zero, so
# the two are far apart and the threshold does not need to be delicate.
HIT = 60
MISS = 20


def test_the_crop_lands_on_the_marked_region() -> None:
    """A white square in an otherwise black frame must dominate the crop.
    Off by anything and the crop is mostly black."""
    image = _frame(400, 300, (170, 120, 230, 180))
    crop = _crop_like_the_api(image, (0.425, 0.4, 0.15, 0.2))
    assert _brightness(crop) > HIT, "the crop missed the marked region"


def test_a_crop_from_the_wrong_image_is_detectably_wrong() -> None:
    """The regression that started this: cropping the right box out of the
    wrong picture. The test is that the two disagree — if a change ever makes
    them agree, the crop has stopped reading the frame."""
    box = (0.425, 0.4, 0.15, 0.2)
    right = _frame(400, 300, (170, 120, 230, 180))
    wrong = _frame(400, 300, (10, 10, 70, 70))
    assert _brightness(_crop_like_the_api(right, box)) > HIT
    assert _brightness(_crop_like_the_api(wrong, box)) < MISS


def test_the_crop_is_square_regardless_of_the_frames_shape() -> None:
    """Normalised coordinates are measured against differing width and height,
    so a box that looks square in the numbers is not square in pixels. Taking
    one side from the larger edge is what stops faces coming out stretched."""
    wide = _frame(1600, 400, (700, 150, 900, 250))
    crop = _crop_like_the_api(wide, (0.4375, 0.375, 0.125, 0.25), size=64)
    assert crop.size == (64, 64)


def test_a_face_at_the_edge_does_not_wrap_or_fail() -> None:
    """Padding pushes the window past the frame on a face near the border."""
    image = _frame(200, 200, (0, 0, 40, 40))
    crop = _crop_like_the_api(image, (0.0, 0.0, 0.2, 0.2))
    assert crop.size[0] > 0 and crop.size[1] > 0
    # Clamping at the border means more of the window falls outside the marker
    # than it would in the middle of the frame, so this reads lower than HIT
    # while still being unmistakably on target rather than a miss.
    assert _brightness(crop) > 40, "the crop drifted off the marked corner"


def test_padding_includes_context_around_the_box() -> None:
    """Detectors crop tight to the features and a portrait with no forehead is
    hard to recognise, so the window is deliberately larger than the box."""
    image = _frame(400, 400, (180, 180, 220, 220))
    tight = _crop_like_the_api(image, (0.45, 0.45, 0.1, 0.1), padding=0.0)
    padded = _crop_like_the_api(image, (0.45, 0.45, 0.1, 0.1), padding=0.35)
    # More surrounding black means a wider window on the same marker.
    assert _brightness(padded) < _brightness(tight)


def test_the_result_is_a_readable_jpeg() -> None:
    image = _frame(400, 300, (20, 20, 80, 80))
    buffer = io.BytesIO()
    _crop_like_the_api(image, (0.05, 0.067, 0.15, 0.2)).save(buffer, format="JPEG", quality=85)
    buffer.seek(0)
    with Image.open(buffer) as reopened:
        assert reopened.format == "JPEG"
        assert reopened.size == (64, 64)


# --- the size gate --------------------------------------------------------


def test_a_face_too_small_to_embed_is_rejected() -> None:
    """ArcFace upscales its input to 112x112. The smallest boxes accepted on
    the reference deployment were 0.013 x 0.038 of the frame — about seven
    pixels across on a 512px frame thumbnail — so the embedding was almost
    entirely interpolation, and it polluted every cluster it landed in."""
    from framefound.ai.faces import MIN_FACE_PIXELS

    box_w, box_h = 0.013 * 512, 0.038 * 512  # the real case, in pixels
    assert max(box_w, box_h) < MIN_FACE_PIXELS


def test_an_ordinary_portrait_still_passes_both_gates() -> None:
    """The gate must not be so eager that it throws away real faces."""
    from framefound.ai.faces import MIN_FACE_FRACTION, MIN_FACE_PIXELS

    width = height = 512
    box_w = box_h = 0.25 * 512  # a face filling a quarter of the frame
    assert max(box_w, box_h) >= MIN_FACE_PIXELS
    assert (box_w / width) * (box_h / height) >= MIN_FACE_FRACTION**2
