"""Learning a tag from a handful of examples.

The operator tags one video "Power Broom". The system should then find the
other power brooms — without a training run, a GPU, or a labelled dataset.

CLIP puts images and text in the same vector space, which gives two
independent sources of evidence about what a tag means:

- **The words.** `embed_text("a photo of a power broom")` is a usable guess on
  its own. For common subjects it is a very good one.
- **The examples.** The mean of the frame vectors the operator actually tagged
  is what the tag means *in this library* — which is what matters when the
  subject is a specific piece of turf equipment rather than a dictionary word.

Neither is sufficient alone. Text is generic and may be confidently wrong
about niche vocabulary; CLIP has no idea what a power broom is, but it has a
strong opinion. One or two examples overfit to whatever else was in frame —
tag one photo and you may learn "gravel driveway on an overcast day".

So the prototype is a blend that shifts toward the examples as they
accumulate: a prior that evidence is allowed to overrule.

The threshold is derived, not fixed. A fixed cosine cutoff cannot work across
tags — "sand bunker" and "power broom" occupy differently shaped regions. It
is set from the examples themselves: low enough to admit the weakest thing the
operator called a match, high enough to exclude the strongest thing they said
was not.
"""

import math
from dataclasses import dataclass

# How fast examples take over from the text guess. At n=3 the two carry equal
# weight, which matches the point where a handful of examples starts being
# more trustworthy than a dictionary definition.
EXAMPLE_HALF_WEIGHT = 3.0

# CLIP's modality gap, measured on this library (3,000 frames, ViT-B/32):
#
#   text  vs image:  min 0.119   median 0.189   p95 0.235   max 0.294
#   image vs image:  min 0.270   median 0.458   p95 0.534   near-dupes 0.88+
#
# Text and image embeddings occupy different regions of the space, so a cosine
# score means nothing without knowing which regime produced it. An absolute
# floor cannot work: 0.78 is unreachable for a prototype with any text in it,
# and trivially cleared by one built purely from images.
#
# The bar is therefore taken from the *distribution* of scores actually
# observed — a high percentile of everything scored in this run. That is
# regime-independent by construction, self-calibrating as a tag's prototype
# moves from text-heavy to example-heavy, and states the real intent directly:
# suggest what stands out from the library, not what merely scores highly.
BASELINE_PERCENTILE = 99.0

# Used only before any scoring has happened, so the two regimes need separate
# values. Both sit near the top of their measured range.
TEXT_ONLY_FLOOR = 0.26
EXAMPLE_FLOOR = 0.55

# Pulled back from the weakest accepted example so near-misses still surface;
# pushed above the strongest rejection so a known wrong answer cannot return.
POSITIVE_MARGIN = 0.015
NEGATIVE_MARGIN = 0.01

# CLIP was trained on caption-like text. A bare noun performs measurably worse
# than the same noun in a caption frame.
TEXT_PROMPTS = (
    "a photo of a {tag}",
    "{tag}",
)


def prompt_variants(tag: str) -> list[str]:
    clean = tag.strip().lower()
    return [template.format(tag=clean) for template in TEXT_PROMPTS]


def normalise(vector: list[float]) -> list[float]:
    length = math.sqrt(sum(component * component for component in vector))
    if length == 0.0:
        return vector
    return [component / length for component in vector]


def mean_vector(vectors: list[list[float]]) -> list[float] | None:
    """Centroid of unit vectors, re-normalised.

    Averaging unit vectors then re-normalising is spherical k-means' update
    step, and it is the right operation for cosine similarity — a plain
    average would let a vector with an unusual magnitude dominate, which is
    why everything is normalised on the way in.
    """
    if not vectors:
        return None
    width = len(vectors[0])
    if any(len(v) != width for v in vectors):
        return None
    total = [0.0] * width
    for vector in vectors:
        unit = normalise(vector)
        for index, component in enumerate(unit):
            total[index] += component
    return normalise([component / len(vectors) for component in total])


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b, strict=True)))


def blend(
    text_vector: list[float] | None,
    example_vector: list[float] | None,
    example_count: int,
) -> list[float] | None:
    """Prior from the words, evidence from the examples."""
    if example_vector is None:
        return normalise(text_vector) if text_vector else None
    if text_vector is None:
        return normalise(example_vector)

    example_weight = example_count / (example_count + EXAMPLE_HALF_WEIGHT)
    text_weight = 1.0 - example_weight
    text_unit = normalise(text_vector)
    example_unit = normalise(example_vector)
    return normalise(
        [text_weight * t + example_weight * e for t, e in zip(text_unit, example_unit, strict=True)]
    )


@dataclass(frozen=True)
class Threshold:
    value: float
    reason: str


def fallback_floor(has_examples: bool) -> float:
    """The bar to use when no score distribution is available yet."""
    return EXAMPLE_FLOOR if has_examples else TEXT_ONLY_FLOOR


def derive_threshold(
    prototype: list[float],
    positives: list[list[float]],
    negatives: list[list[float]],
    baseline: float | None = None,
) -> Threshold:
    """Where to draw the line for this particular tag.

    `baseline` is a high percentile of the scores observed in the run being
    thresholded. Passing it is what makes the bar regime-independent; without
    it the fallback floors apply, which are rough by comparison.

    Reported with a reason, because an operator who sees an odd suggestion
    deserves to be able to find out why it cleared the bar.
    """
    count = len(positives)
    floor = baseline if baseline is not None else fallback_floor(bool(positives))

    if not positives:
        return Threshold(
            floor,
            "nothing tagged yet — matching on the tag's name alone"
            if baseline is None
            else "nothing tagged yet — using the top 1% of matches",
        )

    weakest = min(cosine(prototype, vector) for vector in positives)
    candidate = weakest - POSITIVE_MARGIN
    reason = f"just below the weakest of {count} tagged example{'' if count == 1 else 's'}"

    if negatives:
        strongest_wrong = max(cosine(prototype, vector) for vector in negatives)
        if strongest_wrong + NEGATIVE_MARGIN > candidate:
            candidate = strongest_wrong + NEGATIVE_MARGIN
            reason = f"just above the closest of {len(negatives)} rejected suggestions"

    if candidate < floor:
        # The examples are not distinctive enough to beat the library at large.
        # Holding at the baseline is the honest answer — and saying so is more
        # useful than silently suggesting nothing.
        return Threshold(
            floor,
            f"{count} example{'' if count == 1 else 's'} so far — held at the top "
            "1% of matches until they are more distinctive",
        )
    # A tag whose examples are all near-identical would otherwise set a bar so
    # high nothing else could ever clear it.
    return Threshold(min(candidate, 0.98), reason)


def percentile(scores: list[float], point: float = BASELINE_PERCENTILE) -> float | None:
    """Nearest-rank percentile. Enough samples or nothing — a percentile of a
    handful of scores describes the handful, not the library."""
    if len(scores) < 100:
        return None
    ordered = sorted(scores)
    index = min(len(ordered) - 1, int(round(point / 100.0 * (len(ordered) - 1))))
    return ordered[index]
