"""A colour look for a video clip: what one may hold, and the looks that
need no model.

A clip's look is a handful of slider values that leave FrameFound as a LUT
(`media/lut.py`). A clip is offered up to three, side by side on the same
still: what a local model suggests, the learned look, and no change. This
module is everything about those three that needs neither a model nor a
database:

- **What a look may hold** (`SLIDERS`): the eight sliders a LUT carries.
  Lifting the shadows, window pull and the rest read the pixels round a
  pixel, which a table of colours cannot do, so a clip look never holds
  them. `clean_look` is the boundary for values a person set,
  `clean_suggestion` for values a model answered with.
- **The question for a model** (`INSTRUCTIONS`, `prompt`, `facts_line`) and
  the form its answer must take (`ColourSuggestion`). Nothing here asks it:
  that is a job on the local-AI lane. A suggestion is bounded to about half
  of what the engine allows, small values are dropped, and it is only ever
  a suggestion: nothing in this file chooses a look for anybody.
- **The learned look, for video** (`learned`). There are no graded clips to
  learn from, so it learns from the looks the operator has chosen on other
  clips, once there are enough. Until then it is the look learned from
  shipped photographs, fitted again to what a LUT can carry: with the
  sliders a LUT cannot carry simply taken out, what is left of that look is
  a darker picture than it intends.
- **Which still a look is judged on** (`reference_moment`, `moments`). A
  white balance is measured once, on one still, so every look of a clip is
  measured on the same one.
- **A look in words** (`in_words`), with no number in it.

No number that grades a look leaves this module. Fitting works by a colour
difference, as it always has; here that number is the fit's own business
and is thrown away. A look is judged by eye, on the still.

Imports nothing from `framefound.mcp` or `framefound.local_models`.
"""

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from framefound.media import compare, develop, looks, lut

# Goes into the record of every suggestion. Bump it on ANY change to
# INSTRUCTIONS, prompt(), facts_line() or ColourSuggestion: a stored
# suggestion says which question it answered.
PROMPT_VERSION = "colour-1"
# Chosen looks on OTHER clips before the learned look comes from them.
MIN_CLIP_EXAMPLES = 8

# What a look may hold: every slider a LUT carries (lut.lut_recipe), with
# shadows only ever darkened.
SLIDERS: tuple[str, ...] = (
    "exposure",
    "contrast",
    "temperature",
    "tint",
    "saturation",
    "vibrance",
    "shadows",
    "auto_wb",
)
# What a MODEL may suggest: about half the engine's range. A small model that
# misjudges a still then costs a look that is a bit off, not a wrecked one.
LIMITS: dict[str, tuple[float, float]] = {
    "exposure": (-1.0, 1.0),
    "contrast": (-0.5, 0.5),
    "temperature": (-0.5, 0.5),
    "tint": (-0.5, 0.5),
    "saturation": (-0.5, 0.5),
    "vibrance": (-0.5, 0.5),
    "shadows": (-0.6, 0.0),
    "auto_wb": (0.0, 1.0),
}
# A value nearer nothing than this is nothing: not worth a line in a LUT's
# header, and a model's 0.01 is noise, not a decision.
DEAD_BAND = 0.02

# The sliders a fit may move when it is fitting a look a LUT can carry, with
# each one's opening step (compare.fit_recipe(steps=...)), and how far each
# may go: the engine's own range, shadows never above zero.
LUT_STEPS: tuple[tuple[str, float], ...] = (
    ("exposure", 0.4),
    ("auto_wb", 0.25),
    ("temperature", 0.1),
    ("tint", 0.1),
    ("contrast", 0.1),
    ("shadows", 0.2),
    ("vibrance", 0.2),
    ("saturation", 0.1),
)
LUT_BOUNDS: dict[str, tuple[float, float]] = {
    key: (develop.RECIPE_FIELDS[key][0], min(develop.RECIPE_FIELDS[key][1], 0.0))
    if key == "shadows"
    else develop.RECIPE_FIELDS[key]
    for key in SLIDERS
}

NO_LEARNED_LOOK = (
    "No learned look yet. It comes from the photographs you have shipped, "
    f"or from the first {MIN_CLIP_EXAMPLES} clips you choose a look for."
)
FROM_PHOTOS = "From your photograph look."
FROM_PHOTOS_NARROWED = (
    "From your photograph look. A LUT cannot carry all of it; this is the closest it gets."
)
AS_SHOT = "The clip as it was shot."


# --- the question, and the form of its answer ----------------------------------


class ColourSuggestion(BaseModel):
    """What a model is asked to answer with. The bounds are `LIMITS`; a
    runtime that keeps to a schema cannot answer outside them, and one that
    does not is caught when the answer is checked against this."""

    model_config = ConfigDict(extra="forbid")

    exposure: float = Field(ge=-1.0, le=1.0)
    contrast: float = Field(ge=-0.5, le=0.5)
    temperature: float = Field(ge=-0.5, le=0.5)
    tint: float = Field(ge=-0.5, le=0.5)
    saturation: float = Field(ge=-0.5, le=0.5)
    vibrance: float = Field(ge=-0.5, le=0.5)
    # Asked the way a person would say it; stored as shadows = -value.
    darken_shadows: float = Field(ge=0.0, le=0.6)
    # Stored as auto_wb.
    white_balance: float = Field(ge=0.0, le=1.0)
    # Generous, so a wordy model is cut rather than refused; 200 are kept.
    reason: str = Field(max_length=300)


# Every minus below is the plain keyboard one. The answer is JSON, JSON's
# minus is that one, and a model shown a typographic minus may write it back.
INSTRUCTIONS = (
    "You grade video for a small production company: honest, natural colour \N{EM DASH} "
    "neutral whites, skies that are not blown out, skin and grass that look "
    "real. You are shown one still from a clip and told what was measured. "
    "Suggest slider values for a gentle grade. Every value is 0 when nothing "
    "should change. exposure: -1 to 1 stops. contrast, temperature (positive "
    "is warmer), tint (positive is more magenta), saturation, vibrance: "
    "-0.5 to 0.5. darken_shadows: 0 to 0.6. white_balance: 0 to 1, how much of "
    "a colour cast to remove. If the still already looks right, answer with "
    "all zeros. In `reason`, say in one short sentence what you changed and "
    "why. Answer with one JSON object and nothing else."
)


def prompt(facts: str) -> str:
    """The question that goes with the still. `facts` is `facts_line(...)`,
    or nothing when nothing was measured."""
    told = " ".join(facts.split())
    return "This is one still from the clip." + (f" {told}" if told else "") + " Suggest a look."


def _fact_word(value: Any) -> str:
    return " ".join(str(value).split())[:40]


def facts_line(look: Mapping[str, Any] | None, light: str | None, move: str | None) -> str:
    """What was measured about the shot the still is from, as one sentence:
    "Measured: brightness 0.41, saturation 0.22, warmth -0.05, contrast 0.18;
    sky overcast; light golden hour; move orbit."

    `look`, `light` and `move` are a shot window's. Whatever is missing is
    left out, and with nothing at all the answer is empty: a model is not
    told that nothing was measured."""
    parts: list[str] = []
    look = look or {}
    numbers = []
    for name in ("brightness", "saturation", "warmth", "contrast"):
        value = look.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(value):
            # "+ 0.0" so that a hair under zero is not printed as "-0.00".
            numbers.append(f"{name} {round(float(value), 2) + 0.0:.2f}")
    if numbers:
        parts.append(", ".join(numbers))
    for name, value in (("sky", look.get("sky")), ("light", light), ("move", move)):
        if isinstance(value, str) and (word := _fact_word(value)):
            parts.append(f"{name} {word}")
    return f"Measured: {'; '.join(parts)}." if parts else ""


def clean_suggestion(answer: ColourSuggestion) -> tuple[dict[str, float], str]:
    """A model's answer as (the look it suggests, its one line of reason).

    Held to `LIMITS` whatever the runtime let through, values inside the dead
    band dropped, then through the engine's own cleaning and the one function
    that says what a LUT carries. An answer of all zeros is the empty look:
    "the clip is right as it was shot" is a suggestion too."""
    said = {
        "exposure": answer.exposure,
        "contrast": answer.contrast,
        "temperature": answer.temperature,
        "tint": answer.tint,
        "saturation": answer.saturation,
        "vibrance": answer.vibrance,
        "shadows": -answer.darken_shadows,
        "auto_wb": answer.white_balance,
    }
    kept: dict[str, float] = {}
    for key, value in said.items():
        if not math.isfinite(value):
            continue
        low, high = LIMITS[key]
        value = round(min(high, max(low, float(value))), 3)
        if abs(value) >= DEAD_BAND:
            kept[key] = value
    carried, _left_out = lut.lut_recipe(kept)
    recipe = {key: float(carried[key]) for key in SLIDERS if key in carried}
    return recipe, " ".join(str(answer.reason).split())[:200]


def clean_look(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Sliders a PERSON set (or a row read back from the database), as a look:
    only `SLIDERS`, each within the engine's own range, shadows never above
    zero, three decimals. Anything else is dropped, so what comes back is
    always something `lut.lut_recipe` carries whole."""
    picked = {}
    for key in SLIDERS:
        value = raw.get(key)
        # Not-a-number would come out of the engine's clamp as the slider's
        # far end; here it is simply not a value.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(value):
            picked[key] = value
    look: dict[str, Any] = {}
    for key, value in develop.clean_recipe(picked).items():
        value = round(float(value), 3)
        if key == "shadows":
            value = min(value, 0.0)
        if value:
            look[key] = value
    return look


# --- a look in words -----------------------------------------------------------

# Per slider: the word when it is raised, the word when it is lowered, and
# from where it is worth "a little", is said plainly, and is "much". Listed
# in the order the sentence says them.
_WORDS: tuple[tuple[str, str, str, float, float, float], ...] = (
    ("exposure", "brighter", "darker", 0.05, 0.3, 1.0),
    ("temperature", "warmer", "cooler", 0.03, 0.15, 0.5),
    ("tint", "more magenta", "greener", 0.03, 0.15, 0.5),
    ("contrast", "more contrast", "flatter", 0.03, 0.15, 0.5),
    ("shadows", "lighter in the shadows", "darker in the shadows", 0.05, 0.3, 0.7),
    # Saturation and vibrance together: both are "how much colour".
    ("colour", "more colour", "less colour", 0.03, 0.15, 0.6),
)
_WB_SAID_FROM = 0.05
_WB_WHOLE_FROM = 0.5


def in_words(recipe: Mapping[str, Any]) -> str:
    """A look as a sentence a person can check against the picture: "A little
    brighter and warmer, more colour, white balance evened out." The empty
    look is "The clip as it was shot."

    It never prints a number: how strong a look is, is for the eye, and a
    figure beside a picture gets read as a grade."""
    cleaned = develop.clean_recipe(dict(recipe))
    if not cleaned:
        return AS_SHOT
    amounts = {key: float(value) for key, value in cleaned.items() if key != "sky"}
    amounts["colour"] = amounts.get("saturation", 0.0) + amounts.get("vibrance", 0.0)
    said: list[tuple[str, str]] = []  # (how much, what)
    for key, raised, lowered, little, plain, much in _WORDS:
        amount = amounts.get(key, 0.0)
        size = abs(amount)
        if size <= little:
            # One up and one down that cancel is still something done.
            if (
                key == "colour"
                and min(abs(amounts.get("saturation", 0.0)), abs(amounts.get("vibrance", 0.0)))
                > little
            ):
                said.append(("", "colour rebalanced"))
            continue
        degree = "much" if size > much else "" if size > plain else "a little"
        said.append((degree, raised if amount > 0 else lowered))
    strength = amounts.get("auto_wb", 0.0)
    if strength > _WB_SAID_FROM:
        whole = strength >= _WB_WHOLE_FROM
        said.append(
            ("", "white balance evened out" if whole else "white balance partly evened out")
        )
    if cleaned.get("auto"):
        said.append(("", "levels stretched"))
    if not said:
        return "Barely changed from how it was shot."

    # "a little brighter, a little warmer" reads better said once.
    phrases: list[str] = []
    index = 0
    while index < len(said):
        degree, what = said[index]
        run = [what]
        while degree and index + 1 < len(said) and said[index + 1][0] == degree:
            index += 1
            run.append(said[index][1])
        joined = run[0] if len(run) == 1 else f"{', '.join(run[:-1])} and {run[-1]}"
        phrases.append(f"{degree} {joined}" if degree else joined)
        index += 1
    sentence = ", ".join(phrases)
    return sentence[0].upper() + sentence[1:] + "."


def version_of(recipe: Mapping[str, Any], measured: Mapping[str, Any]) -> str:
    """A short name for exactly this look, for a preview's address: a look
    that changed is then a different picture to the browser, not the old one
    from its cache. Eight hex digits of a hash; "0" for the empty look."""
    if not recipe:
        return "0"
    text = json.dumps(
        {"recipe": dict(recipe), "measured": dict(measured)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


# --- which still --------------------------------------------------------------


def _middles(windows: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """(length, middle) of each window that has any length. The middle is
    where the shot analysis saved the window's middle still
    (`processing.shots.window_moments`), so it is a picture already on disk."""
    return [
        (int(end) - int(start), (int(start) + int(end)) // 2)
        for start, end in windows
        if int(end) > int(start) >= 0
    ]


def reference_moment(
    windows: Sequence[tuple[int, int]], frames: Sequence[int], duration_ms: int | None
) -> int | None:
    """The one moment of a clip its looks are measured on and a model is
    shown: the middle of its longest shot (the earliest, of equals). A clip
    with no shots uses the sampled frame nearest its middle. None when it has
    neither.

    `windows` are (start_ms, end_ms); `frames` the sampled frames' moments."""
    middles = _middles(windows)
    if middles:
        longest = max(length for length, _middle in middles)
        return min(middle for length, middle in middles if length == longest)
    sampled = sorted({int(ts) for ts in frames if int(ts) >= 0})
    if not sampled:
        return None
    centre = duration_ms / 2 if duration_ms and duration_ms > 0 else (sampled[0] + sampled[-1]) / 2
    return min(sampled, key=lambda ts: (abs(ts - centre), ts))


def moments(
    windows: Sequence[tuple[int, int]],
    frames: Sequence[int],
    limit: int = 12,
    *,
    keep: int | None = None,
) -> list[tuple[int, str]]:
    """The moments of a clip a look can be looked at on, in order, each with
    where its picture comes from: every shot's middle still ("shot"), or for
    a clip with no shots its sampled frames ("frame").

    More than `limit` are thinned evenly across the clip rather than cut off
    at the front. `keep` is a moment that must survive the thinning: the
    reference, which is the one the white balance was measured on."""
    middles = sorted({middle for _length, middle in _middles(windows)})
    source = "shot" if middles else "frame"
    ordered = middles or sorted({int(ts) for ts in frames if int(ts) >= 0})
    limit = max(1, limit)
    if len(ordered) > limit:
        stride = len(ordered) / limit
        picked = [ordered[int(i * stride)] for i in range(limit)]
        if keep is not None and keep in ordered and keep not in picked:
            # In place of its nearest neighbour, which keeps them in order.
            nearest = min(range(limit), key=lambda i: abs(picked[i] - keep))
            picked[nearest] = keep
        ordered = picked
    return [(ts, source) for ts in ordered]


# --- the looks that need no model ----------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """One of the looks a clip is offered."""

    source: Literal["model", "learned", "none"]
    available: bool
    # Cleaned, and only what a LUT carries. Empty is "as it was shot".
    recipe: dict[str, Any]
    # develop.measure() of the recipe on the still: what the LUT freezes.
    measured: dict[str, Any]
    # Keys of sliders the look wanted and a LUT cannot carry; words for them
    # are lut.left_out_words().
    left_out: tuple[str, ...] = ()
    # The sentence to show when it is not available; otherwise where the look
    # comes from, with its caveat.
    note: str | None = None
    basis: Literal["clips", "photos"] | None = None
    # How many chosen clips, or shipped photographs, it was learned from.
    examples: int = 0


def no_change() -> Candidate:
    """The clip as it was shot. Always on offer."""
    return Candidate("none", True, {}, {})


def unavailable(source: Literal["model", "learned"], note: str) -> Candidate:
    """A look that cannot be offered, with the sentence that says why."""
    return Candidate(source, False, {}, {}, note=note)


def features_of(still: Image.Image) -> list[float]:
    """What a still looks like at a glance (`compare.features`), as plain
    numbers for a JSON column: what the video look learns "clips like this
    one" from."""
    return [float(value) for value in compare.features(still)]


def _settled(sliders: Mapping[str, Any]) -> dict[str, Any]:
    """Sliders that were worked out rather than set, as a look: cleaned, and
    without the crumbs working them out leaves. An average of neighbours
    carries a hundredth of a slider one of them used, and a fit ends a hair
    off zero; neither is a decision anyone made."""
    return {key: value for key, value in clean_look(sliders).items() if abs(value) >= DEAD_BAND}


def learned_from_photos(look: compare.LearnedLook, still: Image.Image) -> Candidate:
    """The look learned from shipped photographs, as near as a LUT gets to it
    on this still.

    That look leans on lifting the shadows, window pull and local contrast.
    Take those out and what is left, exposure and vibrance, is a darker
    picture than the look means. So when anything has to go, the whole look
    is first rendered on the still as the target, and the sliders a LUT does
    carry are fitted to that target. What could not be carried is named in
    `left_out`, never dropped in silence."""
    full = looks.predict(look, still)
    carried, left_out = lut.lut_recipe(full)
    if left_out:
        # Fitted small: every adjustment is scale-free, so this predicts the
        # full-size still at a fraction of the pixels.
        small = still.convert("RGB")
        small.thumbnail((compare.FIT_EDGE, compare.FIT_EDGE))
        target = compare.grid_lab(develop.apply_recipe(small, full))
        # The second answer is the fit's colour difference. It is how the fit
        # steers and it stops here: nobody is shown it.
        carried, _loss = compare.fit_recipe(
            small, target, carried, develop.apply_recipe, LUT_BOUNDS, steps=LUT_STEPS
        )
    recipe = _settled(carried)
    return Candidate(
        "learned",
        True,
        recipe,
        develop.measure(still, recipe),
        tuple(left_out),
        FROM_PHOTOS_NARROWED if left_out else FROM_PHOTOS,
        "photos",
        len(look.recipes),
    )


def _usable(features: Any) -> bool:
    """Stored features come back out of a JSON column, and may have been
    measured with a feature set that has since changed."""
    try:
        values = [float(value) for value in features]
    except (TypeError, ValueError):
        return False
    return len(values) == len(compare.FEATURE_NAMES) and all(math.isfinite(v) for v in values)


def learned_from_clips(
    examples: Sequence[tuple[Sequence[float], Mapping[str, float]]], still: Image.Image
) -> Candidate | None:
    """The look learned from the looks chosen for OTHER clips, for this
    still: the same nearest-examples average the photograph look is, over
    (features of the still a look was chosen on, the look). None while there
    are fewer than `MIN_CLIP_EXAMPLES`.

    A clip is never its own example; leaving it out is the caller's job, as
    is passing only chosen looks. "No change" is a choice and counts: it
    says clips like that one are right as shot."""
    usable = [(features, look) for features, look in examples if _usable(features)]
    if len(usable) < MIN_CLIP_EXAMPLES:
        return None

    import numpy as np

    learned_look = compare.LearnedLook.fit(
        [np.asarray(features, dtype=np.float64) for features, _look in usable],
        [clean_look(look) for _features, look in usable],
        k=min(7, len(usable)),
    )
    recipe = _settled(learned_look.predict(compare.features(still)))
    count = len(usable)
    return Candidate(
        "learned",
        True,
        recipe,
        develop.measure(still, recipe),
        (),
        f"From the {count} clips you have chosen a look for.",
        "clips",
        count,
    )


def learned(
    still: Image.Image,
    *,
    clips: Sequence[tuple[Sequence[float], Mapping[str, float]]] = (),
    photos: compare.LearnedLook | None = None,
) -> Candidate:
    """The learned look for this still: from the operator's chosen clips once
    there are enough, until then from the photograph look when one is
    installed, and with neither a candidate that says so."""
    from_clips = learned_from_clips(clips, still)
    if from_clips is not None:
        return from_clips
    if photos is not None:
        return learned_from_photos(photos, still)
    return unavailable("learned", NO_LEARNED_LOOK)
