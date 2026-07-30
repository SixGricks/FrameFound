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

# A suggestion is never offered below this, whatever the examples imply. Two
# unrelated photographs of outdoor equipment sit around 0.7 in CLIP space, so
# anything under this is noise regardless of what the arithmetic says.
SIMILARITY_FLOOR = 0.78

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


def derive_threshold(
    prototype: list[float],
    positives: list[list[float]],
    negatives: list[list[float]],
) -> Threshold:
    """Where to draw the line for this particular tag.

    Reported with a reason, because an operator who sees an odd suggestion
    deserves to be able to find out why it cleared the bar.
    """
    floor = Threshold(SIMILARITY_FLOOR, "no examples yet — using the default floor")
    if not positives:
        return floor

    positive_scores = [cosine(prototype, vector) for vector in positives]
    weakest = min(positive_scores)
    candidate = weakest - POSITIVE_MARGIN
    reason = f"just below the weakest of {len(positives)} accepted examples"

    if negatives:
        strongest_wrong = max(cosine(prototype, vector) for vector in negatives)
        if strongest_wrong + NEGATIVE_MARGIN > candidate:
            candidate = strongest_wrong + NEGATIVE_MARGIN
            reason = f"just above the closest of {len(negatives)} rejected suggestions"

    if candidate < SIMILARITY_FLOOR:
        return floor
    # A tag whose examples are all near-identical would otherwise set a bar so
    # high nothing else could ever clear it.
    return Threshold(min(candidate, 0.98), reason)
