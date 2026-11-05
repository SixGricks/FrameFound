"""Learning a tag from a handful of examples.

The operator tags one video "Power Broom". The system should then find the
other power brooms — without a training run, a GPU, or a labelled dataset.

CLIP puts images and text in the same vector space, which gives two
independent sources of evidence about what a tag means:

- **The words.** `embed_text("a photo of power broom")` is a usable guess on
  its own. For common subjects it is a very good one.
- **The examples.** The mean of the assets the operator actually tagged is
  what the tag means *in this library* — which is what matters when the
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
operator called a match. Rejections do not move it; they veto the candidates
that look more like them than like anything accepted (`veto_flags`).
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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
# observed — a high percentile of every asset scored in this run. That is
# regime-independent by construction, self-calibrating as a tag's prototype
# moves from text-heavy to example-heavy, and states the real intent directly:
# suggest what stands out from the library, not what merely scores highly.
BASELINE_PERCENTILE = 99.0

# The percentile is a floor under the bar the examples imply, and at the 99th
# it capped every tag at about 1% of the library: a subject on one asset in
# ten could never be fully found, however many examples said so. A few
# examples are not yet evidence enough to go lower — but five that agree are,
# so from there the examples' own bar may sit as low as the 90th. Still a
# floor, still conservative: what clears it lands in the review queue, where
# a wrong guess costs a click and a missed one costs a client a shot.
RELAXED_BASELINE_PERCENTILE = 90.0
RELAXED_BASELINE_MIN_EXAMPLES = 5

# Used only before any scoring has happened, so the two regimes need separate
# values. Both sit near the top of their measured range.
TEXT_ONLY_FLOOR = 0.26
EXAMPLE_FLOOR = 0.55

# Pulled back from the weakest accepted example so near-misses still surface.
POSITIVE_MARGIN = 0.015

# A tag whose examples are all near-identical would otherwise set a bar so
# high nothing else could ever clear it.
MAX_THRESHOLD = 0.98

# With no accepted example to weigh it against, a rejection can only say "not
# this one" — so it vetoes near-copies of itself (burst frames, re-exports),
# not everything in its neighbourhood. Sits at the bottom of the measured
# near-duplicate band.
REJECTION_ECHO = 0.88

# A video is represented by the mean of its best few frames, not all of them.
# A long auction clip tagged "power broom" is mostly crowd; averaging every
# frame teaches "auction crowd". Three frames is enough to smooth one
# unlucky sample without diluting a subject that is on screen for one shot.
TOP_FRAMES = 3

# Stored with the tag (`tags.threshold_reason`); the column is this wide.
REASON_MAX_LENGTH = 160

# CLIP was trained on caption-like text, and the same subject reads
# differently in different kinds of picture. This library is real-estate
# listings, drone work and farm auctions, so the guess from the words is the
# average over those framings rather than a bet on any one of them.
TEXT_TEMPLATES = (
    "a photo of {}",
    "a real estate photo of {}",
    "an aerial drone photo of {}",
    "a photo of a {} at a farm auction",
)


def _clean(tag: str) -> str:
    return " ".join(tag.strip().lower().split())


def _room_prompts(clean: str) -> list[str]:
    """Paraphrases the room classifier already curated, when the tag names
    one of its rooms ("Barn", "kitchen", "pasture"). Those were written and
    checked for this library; a tag that happens to share the name should
    benefit from them."""
    from framefound.ai.rooms import ROOMS

    slug = slugify(clean)
    wanted = {slug, slug.removesuffix("s")}
    prompts: list[str] = []
    for room in ROOMS:
        names = {slugify(room.key), slugify(room.label)}
        names.update(slugify(part) for part in room.label.split("/"))
        if wanted & names:
            prompts.extend(room.prompts)
    return prompts


def prompt_variants(tag: str) -> list[str]:
    clean = _clean(tag)
    prompts = [template.format(clean) for template in TEXT_TEMPLATES]
    for prompt in _room_prompts(clean):
        if prompt not in prompts:
            prompts.append(prompt)
    return prompts


def text_prototype(tag: str, embed_text: Callable[[str], list[float]]) -> list[float] | None:
    """The words' guess: every prompt variant encoded, averaged, re-normalised.

    `embed_text` is the provider's text encoder reduced to a vector, passed in
    so this stays pure. A prompt that fails to encode is skipped — four out of
    five is still a usable guess; none at all is None.
    """
    vectors: list[list[float]] = []
    for prompt in prompt_variants(tag):
        try:
            vectors.append(embed_text(prompt))
        except Exception:  # noqa: S112 — one bad prompt must not cost the rest
            continue
    return mean_vector(vectors)


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


def unit_rows(vectors: Any) -> Any:
    """Rows of a matrix scaled to unit length (zero rows left alone)."""
    import numpy as np

    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


def top_frame_means(frames: Any, references: Any, k: int = TOP_FRAMES) -> Any:
    """For each reference, the unit mean of the `k` frames most like it.

    `frames` is n×d and `references` T×d, both unit rows; the answer is T×d.
    One matrix product scores every frame against every reference, which is
    what lets a new asset be checked against the whole vocabulary at once.
    The same function represents an asset as an example and as a candidate,
    so the score an example sets the bar with and the score a candidate is
    judged by are on the same scale.
    """
    import numpy as np

    count = frames.shape[0]
    if count <= k:
        means = np.repeat(frames.mean(axis=0, keepdims=True), references.shape[0], axis=0)
    else:
        scores = frames @ references.T  # n×T
        top = np.argpartition(-scores, k - 1, axis=0)[:k]  # k×T frame indices
        means = frames[top].mean(axis=0)  # T×d
    return unit_rows(means)


def representative(frames: list[list[float]], reference: list[float] | None) -> list[float] | None:
    """One vector for an asset: the mean of its `TOP_FRAMES` frames most like
    `reference` (the tag's current prototype, or its words on the first pass).

    A still has one frame and is itself. Without a usable reference the mean
    of every frame is the honest fallback.
    """
    usable = [frame for frame in frames if frame]
    if not usable:
        return None
    width = len(usable[0])
    if any(len(frame) != width for frame in usable):
        return None
    if reference is None or len(reference) != width or len(usable) <= TOP_FRAMES:
        return mean_vector(usable)
    means = top_frame_means(unit_rows(usable), unit_rows(reference))
    return [float(component) for component in means[0]]


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


def baseline_percentile(example_count: int) -> float:
    """Which percentile of the run's scores floors the bar, given how much
    the operator has shown it."""
    if example_count >= RELAXED_BASELINE_MIN_EXAMPLES:
        return RELAXED_BASELINE_PERCENTILE
    return BASELINE_PERCENTILE


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def derive_threshold(
    prototype: list[float],
    positives: list[list[float]],
    negatives: list[list[float]],
    baseline: float | None = None,
) -> Threshold:
    """Where to draw the line for this particular tag.

    `baseline` is the `baseline_percentile` of the scores observed in the run
    being thresholded. Passing it is what makes the bar regime-independent;
    without it the fallback floors apply, which are rough by comparison.

    `negatives` do not move the bar. Raising it above the closest rejection
    let one near-miss lock a tag for good — everything less similar than that
    one mistake, including every true match, fell below it. Rejections veto
    their look-alikes candidate by candidate instead (`veto_flags`).

    Reported with a reason, because an operator who sees an odd suggestion
    deserves to be able to find out why it cleared the bar.
    """
    count = len(positives)
    floor = baseline if baseline is not None else fallback_floor(bool(positives))
    top_share = f"top {100.0 - baseline_percentile(count):g}%"

    if not positives:
        value = floor
        reason = (
            "nothing tagged yet — matching on the tag's name alone"
            if baseline is None
            else f"nothing tagged yet — using the {top_share} of matches"
        )
    else:
        weakest = min(cosine(prototype, vector) for vector in positives)
        candidate = weakest - POSITIVE_MARGIN
        if candidate < floor:
            # The examples are not distinctive enough to beat the library at
            # large. Holding at the baseline is the honest answer — and saying
            # so is more useful than silently suggesting nothing.
            value = floor
            reason = (
                f"{_plural(count, 'example')} so far — held at the {top_share} of matches "
                "until they are more distinctive"
            )
        else:
            value = min(candidate, MAX_THRESHOLD)
            reason = f"just below the weakest of {_plural(count, 'tagged example')}"

    if negatives:
        reason += f"; {_plural(len(negatives), 'rejection')} veto their look-alikes"
    return Threshold(value, reason[:REASON_MAX_LENGTH])


def veto_flags(
    candidates: list[list[float]],
    positives: list[list[float]],
    negatives: list[list[float]],
) -> list[bool]:
    """Which candidates a rejection rules out.

    A candidate is vetoed when its nearest labelled neighbour — among the
    tag's accepted examples and its rejections, by cosine — is a rejection:
    it looks more like something the operator said was wrong than like
    anything they said was right. That is local, so one rejected near-miss
    removes its own neighbourhood without raising the bar for the whole
    library. With nothing accepted to weigh against, only near-copies of a
    rejection are vetoed (`REJECTION_ECHO`). Ties go to the candidate: the
    review queue exists to settle them.
    """
    import numpy as np

    if not candidates:
        return []
    if not negatives:
        return [False] * len(candidates)
    width = len(candidates[0])
    wrong = [vector for vector in negatives if len(vector) == width]
    right = [vector for vector in positives if len(vector) == width]
    if not wrong:
        return [False] * len(candidates)
    pool = unit_rows(candidates)
    nearest_wrong = (pool @ unit_rows(wrong).T).max(axis=1)
    if not right:
        return [bool(score >= REJECTION_ECHO) for score in nearest_wrong]
    nearest_right = (pool @ unit_rows(right).T).max(axis=1)
    return [bool(flag) for flag in np.greater(nearest_wrong, nearest_right)]


def vetoed(
    candidate: list[float], positives: list[list[float]], negatives: list[list[float]]
) -> bool:
    return veto_flags([candidate], positives, negatives)[0]


def percentile(scores: list[float], point: float = BASELINE_PERCENTILE) -> float | None:
    """Nearest-rank percentile. Enough samples or nothing — a percentile of a
    handful of scores describes the handful, not the library."""
    if len(scores) < 100:
        return None
    ordered = sorted(scores)
    index = min(len(ordered) - 1, int(round(point / 100.0 * (len(ordered) - 1))))
    return ordered[index]


# Slugs are for lookup and de-duplication; the display name keeps the
# operator's own capitalisation and spacing. "Power Broom", "power broom" and
# "power-broom" are one tag — and so is whatever Claude proposes.
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return _SLUG_STRIP.sub("-", name.strip().lower()).strip("-")
