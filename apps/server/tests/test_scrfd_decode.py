"""Decoding SCRFD's output.

Written after the first version silently found zero faces in an entire
library. det_10g emits nine tensors in three groups; the original code assumed
six and computed the group size as `len(outputs) // 2` = 4, so it read bounding
boxes out of the keypoint tensors. Everything ran, nothing crashed, and no face
was ever detected.

The shapes below are the real ones, read off the model on the deployment:

    INPUTS : [('input.1', [1, 3, '?', '?'])]
    OUTPUTS: [('448', [12800, 1]), ('471', [3200, 1]), ('494', [800, 1]),
              ('451', [12800, 4]), ('474', [3200, 4]), ('497', [800, 4]),
              ('454', [12800, 10]), ('477', [3200, 10]), ('500', [800, 10])]
"""

import numpy as np
import pytest

from framefound.ai.faces import DETECTOR_SIZE, MIN_DETECTION_SCORE, _decode_scrfd

# 640/8=80 -> 80*80*2 anchors, and so on down the strides.
COUNTS = (12800, 3200, 800)


def _outputs(hot_index: int | None = None, hot_at: int = 0, score: float = 0.9) -> list[np.ndarray]:
    """Nine tensors shaped like the real model's, optionally with one hit."""
    scores = [np.zeros((n, 1), dtype="float32") for n in COUNTS]
    boxes = [np.full((n, 4), 2.0, dtype="float32") for n in COUNTS]
    # Keypoints: present, and must never be mistaken for boxes.
    kps = [np.full((n, 10), 99.0, dtype="float32") for n in COUNTS]
    if hot_index is not None:
        scores[hot_index][hot_at] = score
    return [*scores, *boxes, *kps]


def test_a_confident_anchor_produces_a_box() -> None:
    boxes = _decode_scrfd(_outputs(hot_index=0, hot_at=0), scale=1.0)
    assert len(boxes) == 1
    assert boxes[0][4] == pytest.approx(0.9)


def test_boxes_come_from_the_box_tensors_not_the_keypoints() -> None:
    """The original bug. Keypoints are filled with 99.0 here, so a box built
    from them would be enormous and obviously wrong."""
    outputs = _outputs(hot_index=0, hot_at=0)
    x1, y1, x2, y2, _ = _decode_scrfd(outputs, scale=1.0)[0]
    width, height = x2 - x1, y2 - y1
    # deltas of 2.0 at stride 8 => 16px each side => a 32px box.
    assert width == pytest.approx(32.0)
    assert height == pytest.approx(32.0)


def test_nothing_below_the_score_floor_is_returned() -> None:
    quiet = _outputs(hot_index=0, hot_at=0, score=MIN_DETECTION_SCORE - 0.01)
    assert _decode_scrfd(quiet, scale=1.0) == []


def test_an_empty_frame_yields_nothing() -> None:
    assert _decode_scrfd(_outputs(), scale=1.0) == []


@pytest.mark.parametrize("stride_index", [0, 1, 2])
def test_every_stride_is_decoded(stride_index: int) -> None:
    """All three strides must work. The original code broke out of the loop
    early when its group size was wrong, so the smaller strides — which is
    where larger faces are found — were never read at all."""
    boxes = _decode_scrfd(_outputs(hot_index=stride_index, hot_at=0), scale=1.0)
    assert len(boxes) == 1


def test_larger_strides_give_larger_boxes() -> None:
    """A detection at stride 32 covers four times the area of one at stride 8,
    which is how the same delta describes a face at different distances."""
    small = _decode_scrfd(_outputs(hot_index=0), scale=1.0)[0]
    large = _decode_scrfd(_outputs(hot_index=2), scale=1.0)[0]
    assert (large[2] - large[0]) == pytest.approx(4 * (small[2] - small[0]))


def test_coordinates_are_scaled_back_to_the_original_image() -> None:
    """Detection runs on a letterboxed 640px canvas; the boxes have to come
    back in the coordinate space of the photo the operator actually has."""
    full = _decode_scrfd(_outputs(hot_index=0), scale=1.0)[0]
    halved = _decode_scrfd(_outputs(hot_index=0), scale=0.5)[0]
    assert (halved[2] - halved[0]) == pytest.approx(2 * (full[2] - full[0]))


def test_overlapping_detections_collapse_to_one() -> None:
    """Every stride fires on the same face. Without suppression one person
    becomes three the moment clustering runs."""
    outputs = _outputs(hot_index=0, hot_at=0)
    # A second anchor in the same cell, describing the same face.
    outputs[0][1] = 0.85
    assert len(_decode_scrfd(outputs, scale=1.0)) == 1


def test_distinct_faces_are_both_kept() -> None:
    outputs = _outputs(hot_index=0, hot_at=0)
    outputs[0][5000] = 0.8  # a long way across the grid
    assert len(_decode_scrfd(outputs, scale=1.0)) == 2


def test_a_short_output_list_is_refused_rather_than_misread() -> None:
    """If a future model emits a different shape, return nothing instead of
    inventing boxes — silently finding no faces is bad, silently finding wrong
    ones is worse."""
    assert _decode_scrfd([np.zeros((10, 1), dtype="float32")], scale=1.0) == []


def test_the_grid_matches_the_detector_input_size() -> None:
    # Guards the constant: 640/8 = 80, 80*80*2 = 12800.
    assert COUNTS[0] == (DETECTOR_SIZE // 8) ** 2 * 2
    assert COUNTS[1] == (DETECTOR_SIZE // 16) ** 2 * 2
    assert COUNTS[2] == (DETECTOR_SIZE // 32) ** 2 * 2
