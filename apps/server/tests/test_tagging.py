"""Tag learning: the maths that turns one example into a working tag.

The behaviours worth pinning are the ones that make the feature feel like it
is learning rather than guessing:

- one example should shift the prototype but not take it over;
- enough examples should override a wrong guess from the words;
- a rejection must raise the bar so the same mistake is not offered twice.
"""

import math

import pytest

from framefound.ai import tagging


def unit(*components: float) -> list[float]:
    """A unit vector in as many dimensions as given, zero-padded to 8."""
    padded = list(components) + [0.0] * (8 - len(components))
    return tagging.normalise(padded)


def rotated(angle: float) -> list[float]:
    """A unit vector `angle` radians from `unit(1)`, so cosine == cos(angle)."""
    return unit(math.cos(angle), math.sin(angle))


def test_normalise_produces_unit_length() -> None:
    assert tagging.cosine(
        tagging.normalise([3.0, 4.0]), tagging.normalise([3.0, 4.0])
    ) == pytest.approx(1.0)


def test_normalise_survives_a_zero_vector() -> None:
    # Can happen if an encoder fails; must not divide by zero.
    assert tagging.normalise([0.0, 0.0]) == [0.0, 0.0]


def test_cosine_of_identical_vectors_is_one() -> None:
    assert tagging.cosine(unit(1), unit(1)) == pytest.approx(1.0)


def test_cosine_of_orthogonal_vectors_is_zero() -> None:
    assert tagging.cosine(unit(1, 0), unit(0, 1)) == pytest.approx(0.0)


def test_cosine_of_mismatched_widths_is_zero_not_an_error() -> None:
    assert tagging.cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


def test_mean_of_one_vector_is_that_vector() -> None:
    assert tagging.mean_vector([unit(1, 1)]) == pytest.approx(unit(1, 1))


def test_mean_sits_between_its_inputs() -> None:
    mean = tagging.mean_vector([unit(1, 0), unit(0, 1)])
    assert mean is not None
    assert tagging.cosine(mean, unit(1, 0)) == pytest.approx(tagging.cosine(mean, unit(0, 1)))


def test_mean_of_nothing_is_none() -> None:
    assert tagging.mean_vector([]) is None


def test_mean_refuses_ragged_input() -> None:
    # A dimension mismatch means something upstream is wrong; averaging it
    # would produce a plausible-looking vector that means nothing.
    assert tagging.mean_vector([[1.0, 0.0], [1.0, 0.0, 0.0]]) is None


def test_the_words_alone_are_used_when_there_are_no_examples() -> None:
    """Zero-shot: a brand-new tag has to work before anything is tagged."""
    text = unit(1, 0)
    assert tagging.blend(text, None, 0) == pytest.approx(text)


def test_the_examples_alone_are_used_when_the_words_cannot_be_encoded() -> None:
    example = unit(0, 1)
    assert tagging.blend(None, example, 3) == pytest.approx(example)


def test_no_evidence_at_all_produces_no_prototype() -> None:
    assert tagging.blend(None, None, 0) is None


def test_one_example_shifts_the_prototype_without_taking_over() -> None:
    """The 'Power Broom' case. CLIP has an opinion about the words; one photo
    should move the prototype toward reality without discarding that prior."""
    text, example = unit(1, 0), unit(0, 1)
    blended = tagging.blend(text, example, 1)
    assert blended is not None
    to_text = tagging.cosine(blended, text)
    to_example = tagging.cosine(blended, example)
    assert to_text > to_example, "one example should not outweigh the words"
    assert to_example > 0.2, "but it must move the prototype meaningfully"


def test_many_examples_override_a_wrong_guess_from_the_words() -> None:
    text, example = unit(1, 0), unit(0, 1)
    blended = tagging.blend(text, example, 40)
    assert blended is not None
    assert tagging.cosine(blended, example) > tagging.cosine(blended, text)


def test_example_weight_grows_monotonically() -> None:
    text, example = unit(1, 0), unit(0, 1)
    scores = []
    for count in (1, 2, 5, 10, 50):
        blended = tagging.blend(text, example, count)
        assert blended is not None
        scores.append(tagging.cosine(blended, example))
    assert scores == sorted(scores)


def test_with_no_examples_the_threshold_is_the_floor() -> None:
    threshold = tagging.derive_threshold(unit(1), [], [])
    assert threshold.value == tagging.SIMILARITY_FLOOR
    assert "floor" in threshold.reason


def test_the_threshold_admits_the_weakest_accepted_example() -> None:
    """Whatever the operator called a match must itself clear the bar —
    otherwise the tag would not match its own training data."""
    prototype = unit(1, 0)
    positives = [rotated(0.0), rotated(0.35)]  # cos(0.35) ~ 0.939
    threshold = tagging.derive_threshold(prototype, positives, [])
    for vector in positives:
        assert tagging.cosine(prototype, vector) >= threshold.value


def test_a_rejection_raises_the_bar_above_itself() -> None:
    """The correction loop. Something the operator said was wrong must not be
    offered again, so the threshold moves above it."""
    prototype = unit(1, 0)
    positives = [rotated(0.0), rotated(0.5)]
    near_miss = rotated(0.45)  # closer than the weakest positive
    threshold = tagging.derive_threshold(prototype, positives, [near_miss])
    assert tagging.cosine(prototype, near_miss) < threshold.value
    assert "rejected" in threshold.reason


def test_a_distant_rejection_does_not_move_the_bar() -> None:
    prototype = unit(1, 0)
    positives = [rotated(0.0), rotated(0.3)]
    without = tagging.derive_threshold(prototype, positives, [])
    with_far = tagging.derive_threshold(prototype, positives, [rotated(1.4)])
    assert with_far.value == without.value


def test_the_threshold_never_drops_below_the_floor() -> None:
    """A sloppy example would otherwise drag the bar into noise, and every
    outdoor photograph would match every tag."""
    prototype = unit(1, 0)
    threshold = tagging.derive_threshold(prototype, [rotated(1.5)], [])
    assert threshold.value == tagging.SIMILARITY_FLOOR


def test_the_threshold_is_capped_so_a_tag_can_still_grow() -> None:
    """Identical examples imply a bar of 1.0, which nothing else could ever
    clear — the tag would be frozen at exactly what it was taught."""
    prototype = unit(1, 0)
    threshold = tagging.derive_threshold(prototype, [unit(1, 0), unit(1, 0)], [])
    assert threshold.value <= 0.98


def test_every_threshold_carries_an_explanation() -> None:
    # An operator who sees an odd suggestion should be able to find out why it
    # cleared the bar.
    for positives, negatives in (([], []), ([rotated(0.2)], []), ([rotated(0.5)], [rotated(0.45)])):
        assert tagging.derive_threshold(unit(1, 0), positives, negatives).reason


def test_prompts_are_caption_shaped() -> None:
    """CLIP was trained on captions; a bare noun performs measurably worse."""
    prompts = tagging.prompt_variants("Power Broom")
    assert "a photo of a power broom" in prompts
    assert "power broom" in prompts


def test_prompt_variants_normalise_case_and_spacing() -> None:
    assert tagging.prompt_variants("  POWER Broom  ") == tagging.prompt_variants("power broom")
