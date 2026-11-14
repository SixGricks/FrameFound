"""Looks for a video clip that need no model (framefound/media/clip_look.py).

A clip is offered up to three looks on one still: a model's suggestion, the
learned look, and no change. What is pinned here is everything about them
that is decided without a model or a database: what a look may hold, how a
model's answer is bounded and cleaned, which still a look is judged on, how
a look reads in words, and where "the learned look" comes from when the
only thing ever learned was learned from photographs.

Nothing here asks a model. The colour differences measured in two tests are
between two renders of the same look, to show one is nearer its target than
the other; they stay in this file. A look is judged by eye, and nothing the
module returns grades one.
"""

import dataclasses
import hashlib
import json
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from framefound.media import clip_look, compare, develop, looks, lut

SERVER = Path(__file__).resolve().parents[1]

# The look shipped photographs teach: most of it is what a LUT cannot carry.
PHOTO_LOOK = dict(develop.LISTING_PRESET)


# ------------------------------------------------------------------ pictures


def _room(light: float = 0.55, cast: tuple[float, float, float] = (1.0, 0.9, 0.74)) -> Image.Image:
    """A still the size the shot analysis saves: a wall with a cast, a bright
    window, a dark sofa, a floor. `light` is how well the room is lit."""
    w, h = 480, 270
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    wall = light * (0.45 + 0.55 * xx / w)
    arr = np.stack([wall * cast[0], wall * cast[1], wall * cast[2]], axis=-1)
    floor = light * np.stack([0.5 + 0.1 * xx / w, 0.33 + 0.06 * xx / w, 0.16 + 0 * xx], axis=-1)
    arr[h * 2 // 3 :] = floor[h * 2 // 3 :]
    arr[h // 6 : h // 2, w // 2 : w * 5 // 6] = (0.97, 0.96, 0.93)
    arr[h // 2 : h * 3 // 4, w // 10 : w * 4 // 10] = (0.07, 0.06, 0.05)
    arr += 0.015 * np.sin(xx * 1.7)[..., None] * np.cos(yy * 0.9)[..., None]
    return Image.fromarray((np.clip(arr, 0.0, 1.0) * 255.0 + 0.5).astype("uint8"))


def _course(seed: int) -> Image.Image:
    """A bright drone still: sky, fairway, a bunker. `seed` moves the horizon
    and the greens a little, so no two are the same picture."""
    rng = random.Random(seed)
    w, h = 480, 270
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    horizon = h * rng.uniform(0.28, 0.4)
    arr = np.empty((h, w, 3))
    sky = np.stack([0.55 + 0.2 * yy / h, 0.7 + 0.15 * yy / h, 0.95 + 0 * yy], axis=-1)
    green = rng.uniform(0.5, 0.65)
    grass = np.stack([0.25 + 0.1 * xx / w, green + 0.1 * yy / h, 0.2 + 0 * xx], axis=-1)
    arr[:] = np.where((yy < horizon)[..., None], sky, grass)
    arr[int(h * 0.7) : int(h * 0.8), w // 3 : w // 2] = (0.9, 0.85, 0.7)
    return Image.fromarray((np.clip(arr, 0.0, 1.0) * 255.0 + 0.5).astype("uint8"))


def _dim(seed: int) -> Image.Image:
    rng = random.Random(seed)
    return _room(light=rng.uniform(0.3, 0.4))


def _photo_look(recipe: dict[str, float], n: int = 12) -> compare.LearnedLook:
    """A photograph look that edits everything it has seen the same way."""
    feats = [compare.features(_room(light=0.3 + 0.04 * i)) for i in range(n)]
    return compare.LearnedLook.fit(feats, [recipe] * n)


def _answer(**sliders: Any) -> clip_look.ColourSuggestion:
    said: dict[str, Any] = {
        "exposure": 0.0, "contrast": 0.0, "temperature": 0.0, "tint": 0.0,
        "saturation": 0.0, "vibrance": 0.0, "darken_shadows": 0.0,
        "white_balance": 0.0, "reason": "Nothing to change.",
    }  # fmt: skip
    return clip_look.ColourSuggestion(**{**said, **sliders})


def _apart(one: Image.Image, other: Image.Image) -> float:
    """How far two renders of the same still are from each other. A yardstick
    for these tests only."""
    return compare.distance(one, other).delta_e


# ------------------------------------------------- what a model may suggest


def test_a_suggestion_is_narrowed_to_what_a_lut_carries_and_small_values_go() -> None:
    recipe, reason = clip_look.clean_suggestion(
        _answer(
            exposure=0.25,
            temperature=0.0812,
            tint=0.019,
            contrast=-0.02,
            vibrance=0.2,
            darken_shadows=0.3,
            white_balance=0.6,
            reason="  Overcast and slightly blue;\n lifted and warmed.  ",
        )
    )

    # Darkening the shadows is stored the way the engine means it; a tint
    # of 0.019 is noise and goes, a contrast of exactly the band stays.
    assert recipe == {
        "exposure": 0.25,
        "contrast": -0.02,
        "temperature": 0.081,
        "shadows": -0.3,
        "vibrance": 0.2,
        "auto_wb": 0.6,
    }
    assert reason == "Overcast and slightly blue; lifted and warmed."
    # Everything in it is something a LUT carries whole...
    assert lut.lut_recipe(recipe) == (recipe, [])
    assert set(recipe) <= set(clip_look.SLIDERS)
    # ...so a table can be built from it and what was measured on the still.
    still = _room()
    table = lut.build(recipe, measured=develop.measure(still, recipe))
    assert table.shape == (33, 33, 33, 3)


def test_an_answer_of_all_zeros_is_the_clip_as_it_was_shot() -> None:
    recipe, reason = clip_look.clean_suggestion(_answer(reason="It already looks right."))

    assert recipe == {}
    assert reason == "It already looks right."
    assert clip_look.in_words(recipe) == "The clip as it was shot."


def test_a_long_reason_is_cut_not_refused() -> None:
    wordy = "warmer " * 42  # 294 characters: inside what the form allows
    _recipe, reason = clip_look.clean_suggestion(_answer(reason=wordy))

    assert len(reason) <= 200 and reason.startswith("warmer warmer")


def test_an_answer_nobody_checked_is_still_held_to_the_bounds() -> None:
    """The form is checked where the model is asked. Cleaning does not lean
    on that: whatever it is handed, what it stores is within what a model may
    suggest."""
    unchecked = clip_look.ColourSuggestion.model_construct(
        exposure=1.9,
        contrast=-0.9,
        temperature=float("inf"),
        tint=float("nan"),
        saturation=0.2,
        vibrance=0.0,
        darken_shadows=5.0,
        white_balance=-3.0,
        reason="Pushed hard.",
    )

    recipe, _reason = clip_look.clean_suggestion(unchecked)

    assert recipe == {"exposure": 1.0, "contrast": -0.5, "saturation": 0.2, "shadows": -0.6}
    for slider, value in recipe.items():
        low, high = clip_look.LIMITS[slider]
        assert low <= value <= high


def test_a_model_cannot_answer_outside_the_bounds_it_was_given() -> None:
    for wrong in (
        {"exposure": 1.2},
        {"contrast": -0.6},
        {"darken_shadows": -0.1},  # lifting the shadows is not on offer
        {"darken_shadows": 0.7},
        {"white_balance": 1.5},
        {"temperature": float("nan")},
        {"reason": "x" * 301},
        {"highlights": -0.3},  # a slider a LUT cannot carry is not in the form
    ):
        with pytest.raises(ValidationError):
            _answer(**wrong)
    with pytest.raises(ValidationError):
        clip_look.ColourSuggestion.model_validate({"exposure": 0.2})  # the rest is missing

    # The form a runtime is handed says the same, slider for slider: where
    # the runtime keeps a model to its schema, it cannot go further than this.
    schema = clip_look.ColourSuggestion.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    asked_as = {"shadows": "darken_shadows", "auto_wb": "white_balance"}
    for slider, (low, high) in clip_look.LIMITS.items():
        field = schema["properties"][asked_as.get(slider, slider)]
        if slider == "shadows":  # asked as "how much darker", stored negative
            low, high = -high, -low
        assert (field["minimum"], field["maximum"]) == (low, high), slider
    assert schema["properties"]["reason"]["maxLength"] == 300


def test_a_model_is_given_half_the_engines_range_and_never_a_lift() -> None:
    assert set(clip_look.LIMITS) == set(clip_look.SLIDERS)
    for slider, (low, high) in clip_look.LIMITS.items():
        engine_low, engine_high = develop.RECIPE_FIELDS[slider]
        assert engine_low <= low <= 0.0 <= high <= engine_high, slider
    assert clip_look.LIMITS["shadows"][1] == 0.0
    assert clip_look.LIMITS["exposure"] == (-1.0, 1.0)


def test_the_question_names_every_bound_and_writes_minus_as_json_does() -> None:
    text = clip_look.INSTRUCTIONS

    for said in (
        "exposure: -1 to 1 stops",
        "saturation, vibrance: -0.5 to 0.5",
        "darken_shadows: 0 to 0.6",
        "white_balance: 0 to 1",
        "all zeros",
        "one JSON object and nothing else",
    ):
        assert said in text, said
    # A typographic minus in the question comes back in the answer, and
    # then the answer is not JSON.
    assert "\N{MINUS SIGN}" not in text
    for field in clip_look.ColourSuggestion.model_fields:
        assert field in text, field

    assert clip_look.prompt("") == "This is one still from the clip. Suggest a look."
    asked = clip_look.prompt("Measured: sky overcast.")
    assert asked == "This is one still from the clip. Measured: sky overcast. Suggest a look."


def test_changing_the_question_means_a_new_version() -> None:
    """A stored suggestion names the question it answered. If the words, the
    facts or the form change and the version does not, every suggestion made
    before the change says it answered a question it never saw."""
    # The form as its fields and their bounds: the part of the schema that is
    # the question, without the wrapping a newer pydantic may word differently.
    form = {
        name: [spec.get("minimum"), spec.get("maximum"), spec.get("maxLength")]
        for name, spec in clip_look.ColourSuggestion.model_json_schema()["properties"].items()
    }
    look = {"brightness": 0.5, "saturation": 0.2, "warmth": -0.1, "contrast": 0.1, "sky": "mixed"}
    question = json.dumps(
        [
            clip_look.INSTRUCTIONS,
            clip_look.prompt(clip_look.facts_line(look, "low sun", "pan")),
            form,
        ],
        sort_keys=True,
    )
    fingerprint = hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]

    assert (clip_look.PROMPT_VERSION, fingerprint) == ("colour-1", QUESTION_FINGERPRINT), (
        "The question changed: give PROMPT_VERSION a new name, then put the "
        f"new fingerprint here ({fingerprint})."
    )


QUESTION_FINGERPRINT = "38a05d19ef32b9c7"


def test_the_facts_are_what_was_measured_and_nothing_when_nothing_was() -> None:
    look = {
        "brightness": 0.412,
        "saturation": 0.22,
        "warmth": -0.05,
        "contrast": 0.18,
        "sun": 0.2,
        "sky": "overcast",
        "located_by": "folder",
    }

    assert clip_look.facts_line(look, "golden hour", "orbit") == (
        "Measured: brightness 0.41, saturation 0.22, warmth -0.05, contrast 0.18; "
        "sky overcast; light golden hour; move orbit."
    )
    # What is missing is left out, not reported as missing.
    assert clip_look.facts_line({"sky": "direct"}, None, "static") == (
        "Measured: sky direct; move static."
    )
    assert clip_look.facts_line({"warmth": -0.001}, None, None) == "Measured: warmth 0.00."
    assert clip_look.facts_line(None, None, None) == ""
    assert clip_look.facts_line({}, "", "") == ""
    # Numbers that are not numbers do not reach a model.
    assert clip_look.facts_line({"brightness": float("nan"), "contrast": "high"}, None, None) == ""


# ------------------------------------------------------- what a look may hold


def test_a_look_a_person_set_holds_only_what_a_lut_carries() -> None:
    look = clip_look.clean_look(
        {
            "exposure": 3.0,  # beyond the engine: held at its stop
            "contrast": 0.12345,
            "shadows": 0.4,  # lifting reads the room; a look only darkens
            "highlights": -0.3,
            "window_pull": 0.5,
            "rotate": 1.0,
            "auto": True,
            "auto_wb": 0.5,
            "vibrance": 0,
            "tint": float("nan"),
            "temperature": "warm",
            "sky": {"name": "blue.jpg"},
        }
    )

    assert look == {"exposure": 2.0, "contrast": 0.123, "auto_wb": 0.5}
    assert lut.lut_recipe(look) == (look, [])
    # Darkening the shadows is a LUT's to carry, and a person may go as far
    # as the engine does: further than a model is allowed.
    assert clip_look.clean_look({"shadows": -0.9, "exposure": -1.5}) == {
        "exposure": -1.5,
        "shadows": -0.9,
    }
    assert clip_look.clean_look({}) == {}


def test_the_bounds_of_a_fit_are_the_engines_with_shadows_only_darkened() -> None:
    assert [key for key, _step in clip_look.LUT_STEPS] != []
    assert {key for key, _step in clip_look.LUT_STEPS} == set(clip_look.SLIDERS)
    assert set(clip_look.LUT_BOUNDS) == set(clip_look.SLIDERS)
    assert clip_look.LUT_BOUNDS["shadows"] == (-1.0, 0.0)
    for key in clip_look.SLIDERS:
        if key != "shadows":
            assert clip_look.LUT_BOUNDS[key] == develop.RECIPE_FIELDS[key]
    # Every slider a fit may move is one a LUT carries at both ends of its range.
    for key, (low, high) in clip_look.LUT_BOUNDS.items():
        for value in (low, high):
            if value:
                assert lut.lut_recipe({key: value}) == ({key: value}, [])


# ------------------------------------------------------------- a look in words


def test_a_look_reads_as_a_sentence_about_the_picture() -> None:
    assert (
        clip_look.in_words({"auto_wb": 0.6, "exposure": 0.25, "temperature": 0.08, "vibrance": 0.2})
        == "A little brighter and warmer, more colour, white balance evened out."
    )
    assert (
        clip_look.in_words({"auto_wb": 0.8, "exposure": 0.31, "vibrance": 0.38})
        == "Brighter, more colour, white balance evened out."
    )
    assert clip_look.in_words({}) == "The clip as it was shot."
    assert clip_look.in_words({"exposure": 0.0, "rubbish": 4}) == "The clip as it was shot."
    assert clip_look.in_words(
        {"exposure": -1.4, "temperature": -0.6, "shadows": -0.2, "auto_wb": 0.3}
    ) == (
        "Much darker and cooler, a little darker in the shadows, white balance partly evened out."
    )
    assert (
        clip_look.in_words({"contrast": -0.2, "tint": 0.2, "saturation": -0.3})
        == "More magenta, flatter, less colour."
    )
    assert (
        clip_look.in_words({"exposure": 0.1, "temperature": 0.1, "tint": -0.1})
        == "A little brighter, warmer and greener."
    )
    # Saturation and vibrance are both "how much colour" and are said once.
    assert clip_look.in_words({"saturation": 0.1, "vibrance": 0.1}) == "More colour."
    assert clip_look.in_words({"saturation": 0.3, "vibrance": -0.3}) == "Colour rebalanced."
    # Too small to see is not described as if it could be seen.
    assert clip_look.in_words({"exposure": 0.03}) == "Barely changed from how it was shot."


def test_the_words_for_a_look_contain_no_number() -> None:
    rng = random.Random(4)
    seen = set()
    for _ in range(400):
        recipe = {
            key: round(rng.uniform(low, high), 3)
            for key, (low, high) in clip_look.LUT_BOUNDS.items()
            if rng.random() < 0.5
        }
        sentence = clip_look.in_words(recipe)
        seen.add(sentence)
        assert not re.search(r"\d", sentence), (recipe, sentence)
        assert "%" not in sentence
        assert sentence[0].isupper() and sentence.endswith("."), sentence
        if develop.clean_recipe(recipe):
            assert sentence != "The clip as it was shot."
    assert len(seen) > 100  # not one sentence for everything


def test_a_looks_name_changes_when_the_look_does() -> None:
    still = _room()
    recipe = {"exposure": 0.25, "auto_wb": 0.6}
    measured = develop.measure(still, recipe)
    name = clip_look.version_of(recipe, measured)

    assert re.fullmatch(r"[0-9a-f]{8}", name)
    # The same look is the same name, however its keys happen to be ordered,
    # and after a trip through a JSON column.
    assert clip_look.version_of({"auto_wb": 0.6, "exposure": 0.25}, measured) == name
    assert (
        clip_look.version_of(json.loads(json.dumps(recipe)), json.loads(json.dumps(measured)))
        == name
    )
    # A different slider, or the same sliders measured on another still, is
    # a different picture and must not be served from the browser's cache.
    assert clip_look.version_of({**recipe, "exposure": 0.26}, measured) != name
    other = develop.measure(_room(cast=(0.8, 0.9, 1.0)), recipe)
    assert other != measured
    assert clip_look.version_of(recipe, other) != name
    assert clip_look.version_of({}, {}) == "0"


# ---------------------------------------------------------------- which still


def test_the_reference_is_the_middle_of_the_longest_shot() -> None:
    from framefound.processing import shots

    windows = [(2000, 14000), (14000, 21000), (21000, 40500), (41000, 44000)]

    assert clip_look.reference_moment(windows, [0, 5000, 30000], 45000) == 30750
    # It is a moment the shot analysis saved a still at, so the picture is
    # already on disk and no grab is needed to judge a look.
    assert shots.window_moments(21000, 40500)[1] == 30750
    # Of two shots the same length, the earlier one.
    assert clip_look.reference_moment([(20000, 30000), (0, 10000)], [], None) == 5000
    # A window with no length is not a shot.
    assert clip_look.reference_moment([(5000, 5000), (9000, 8000), (100, 2100)], [], None) == 1100
    for start, end in windows:
        assert (start + end) // 2 == shots.window_moments(start, end)[1]


def test_a_clip_without_shots_uses_its_sampled_frames() -> None:
    frames = [0, 5000, 10000, 15000, 20000, 25000, 60000]

    # The frame nearest the middle of the clip; of two as near, the earlier.
    assert clip_look.reference_moment([], frames, 40000) == 20000
    assert clip_look.reference_moment([], frames, 35000) == 15000
    # A clip whose length nobody recorded: the middle of what was sampled.
    assert clip_look.reference_moment([], frames, None) == 25000
    assert clip_look.reference_moment([], frames, 0) == 25000
    assert clip_look.reference_moment([], [7000], None) == 7000
    assert clip_look.reference_moment([], [], 40000) is None

    assert clip_look.moments([], [10000, 0, 5000, 5000]) == [
        (0, "frame"),
        (5000, "frame"),
        (10000, "frame"),
    ]
    assert clip_look.moments([], []) == []
    # A window with no length is not a shot and has no still: the clip is
    # treated as one without shots, not pointed at a picture that is not there.
    assert clip_look.reference_moment([(5000, 5000)], [7000], None) == 7000
    assert clip_look.moments([(5000, 5000), (9000, 8000)], [7000]) == [(7000, "frame")]


def test_every_shot_can_be_looked_at_and_many_are_thinned_evenly() -> None:
    windows = [(i * 10000, i * 10000 + 8000) for i in range(30)]

    few = clip_look.moments(windows[:3], [1, 2, 3])
    assert few == [(4000, "shot"), (14000, "shot"), (24000, "shot")]

    shown = clip_look.moments(windows, [])
    assert len(shown) == 12
    assert [ts for ts, _source in shown] == sorted(ts for ts, _source in shown)
    # Spread across the clip, not the first twelve.
    assert shown[0][0] == 4000 and shown[-1][0] > 250000
    assert {source for _ts, source in shown} == {"shot"}

    # The moment the white balance was measured on is always among them.
    reference = 4000 + 10000 * 13
    assert reference not in [ts for ts, _source in shown]
    kept = clip_look.moments(windows, [], keep=reference)
    assert len(kept) == 12 and (reference, "shot") in kept
    assert [ts for ts, _source in kept] == sorted(ts for ts, _source in kept)
    # A moment that is not one of the clip's is not smuggled in.
    assert clip_look.moments(windows, [], keep=123) == shown
    assert len(clip_look.moments(windows, [], limit=5)) == 5


# ------------------------------------------------------- the learned look


def test_the_photograph_look_is_refitted_not_just_stripped() -> None:
    still = _room(light=0.34)
    look = _photo_look(PHOTO_LOOK)

    candidate = clip_look.learned_from_photos(look, still)

    whole = looks.predict(look, still)
    stripped, gone = lut.lut_recipe(whole)
    assert gone == ["shadows", "window_pull", "local_contrast"]
    # What could not be carried is named...
    assert candidate.left_out == ("shadows", "window_pull", "local_contrast")
    assert candidate.note == clip_look.FROM_PHOTOS_NARROWED
    assert (candidate.source, candidate.available) == ("learned", True)
    assert (candidate.basis, candidate.examples) == ("photos", 12)
    # ...and what is offered is something a LUT carries whole.
    assert lut.lut_recipe(candidate.recipe) == (candidate.recipe, [])
    assert candidate.measured == develop.measure(still, candidate.recipe)

    # The look as the whole engine renders it is what the operator knows as
    # "the learned look". With the sliders a LUT cannot carry simply taken
    # out, the picture is darker than that; fitted again, it is nearer.
    intended = develop.apply_recipe(still, whole)
    just_stripped = develop.apply_recipe(still, stripped)
    table = lut.build(candidate.recipe, measured=candidate.measured)
    through_the_lut = lut.apply(still, table)
    darker, meant = (float(np.asarray(p.convert("L")).mean()) for p in (just_stripped, intended))
    assert darker < meant
    assert _apart(through_the_lut, intended) < 0.8 * _apart(just_stripped, intended)
    assert candidate.recipe != clip_look.clean_look(stripped)
    # A fit ends a hair off zero on sliders it did not need; those are swept out.
    assert all(abs(value) >= clip_look.DEAD_BAND for value in candidate.recipe.values())

    # The same holds outdoors, where the look's window pull is what goes.
    outdoors = _course(2)
    offered = clip_look.learned_from_photos(look, outdoors)
    whole = looks.predict(look, outdoors)
    intended = develop.apply_recipe(outdoors, whole)
    just_stripped = develop.apply_recipe(outdoors, lut.lut_recipe(whole)[0])
    through_the_lut = lut.apply(outdoors, lut.build(offered.recipe, measured=offered.measured))
    assert _apart(through_the_lut, intended) < 0.8 * _apart(just_stripped, intended)


def test_the_fit_may_only_move_what_a_lut_carries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fitting with every slider and stripping afterwards would be the
    stripped look again by a longer road. The fit is given only the sliders a
    LUT carries, so what it finds is what is offered."""
    fits: list[dict[str, Any]] = []
    real_fit = compare.fit_recipe

    def watched(base: Any, target: Any, start: Any, render: Any, bounds: Any, **more: Any) -> Any:
        found = real_fit(base, target, start, render, bounds, **more)
        fits.append({"start": dict(start), "bounds": dict(bounds), **more, "found": found[0]})
        return found

    monkeypatch.setattr(compare, "fit_recipe", watched)
    candidate = clip_look.learned_from_photos(_photo_look(PHOTO_LOOK), _room(light=0.34))

    [fit] = fits
    assert set(fit["bounds"]) == set(clip_look.SLIDERS)
    assert fit["bounds"]["shadows"][1] == 0.0
    assert {key for key, _step in fit["steps"]} <= set(clip_look.SLIDERS)
    # It starts from what was left of the look, not from nothing.
    assert fit["start"] == {"exposure": 0.22, "vibrance": 0.42, "auto_wb": 1.0}
    # Nothing the fit found is thrown away afterwards but the crumbs.
    settled = {k: v for k, v in fit["found"].items() if abs(v) >= clip_look.DEAD_BAND}
    assert candidate.recipe == settled


def test_a_photograph_look_a_lut_carries_whole_is_used_as_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_fit(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a look a LUT carries whole needs no fitting")

    monkeypatch.setattr(compare, "fit_recipe", no_fit)
    still = _room()
    look = _photo_look({"exposure": 0.4, "vibrance": 0.3, "auto_wb": 0.5, "keystone": 0.2})

    candidate = clip_look.learned_from_photos(look, still)

    # Nothing to fit: the look's own numbers, and the plainer note.
    assert candidate.recipe == {"exposure": 0.4, "vibrance": 0.3, "auto_wb": 0.5}
    assert candidate.left_out == ()
    assert candidate.note == clip_look.FROM_PHOTOS
    assert set(candidate.measured) == {"wb_gains"}


def test_fitting_a_look_does_not_touch_the_still_it_was_fitted_on() -> None:
    still = _room(light=0.34)
    before = still.tobytes()

    clip_look.learned_from_photos(_photo_look(PHOTO_LOOK), still)

    assert still.size == (480, 270) and still.tobytes() == before


def _chosen(stills: list[Image.Image], look: dict[str, float]) -> list[Any]:
    return [(clip_look.features_of(still), look) for still in stills]


def test_fewer_than_eight_chosen_clips_teach_nothing() -> None:
    still = _dim(100)
    seven = _chosen([_dim(i) for i in range(7)], {"exposure": 0.4})

    assert clip_look.learned_from_clips(seven, still) is None
    assert clip_look.learned_from_clips([], still) is None

    nothing = clip_look.learned(still, clips=seven)
    assert (nothing.source, nothing.available, nothing.recipe) == ("learned", False, {})
    assert nothing.note == clip_look.NO_LEARNED_LOOK
    assert "first 8 clips" in clip_look.NO_LEARNED_LOOK
    assert clip_look.MIN_CLIP_EXAMPLES == 8

    eighth = _chosen([_dim(7)], {"exposure": 0.4})
    assert clip_look.learned_from_clips(seven + eighth, still) is not None


def test_the_video_look_predicts_from_chosen_clips() -> None:
    brightened = {"exposure": 0.4, "temperature": 0.1, "auto_wb": 0.5}
    examples = _chosen([_dim(i) for i in range(8)], brightened)
    # Bright drone stills the operator chose to leave alone: that is a
    # choice too, and it teaches "clips like this are right as shot".
    examples += _chosen([_course(i) for i in range(8)], {})

    dim = clip_look.learned_from_clips(examples, _dim(50))
    bright = clip_look.learned_from_clips(examples, _course(50))

    assert dim is not None and bright is not None
    assert dim.recipe == brightened
    assert (dim.source, dim.available, dim.basis, dim.examples) == ("learned", True, "clips", 16)
    assert dim.left_out == ()
    assert dim.note == "From the 16 clips you have chosen a look for."
    # The white balance is measured on THIS still, not carried from another.
    assert dim.measured == develop.measure(_dim(50), brightened)
    assert bright.recipe == {} and bright.measured == {}
    assert clip_look.in_words(bright.recipe) == "The clip as it was shot."

    # A still between the two kinds gets a mixture, and the crumbs a
    # mixture leaves of sliders one neighbour used are swept out.
    mostly_as_shot = _chosen([_dim(i) for i in range(8)], {})
    mostly_as_shot[0] = (mostly_as_shot[0][0], {"exposure": 0.3, "tint": 0.02})
    mixed = clip_look.learned_from_clips(mostly_as_shot, _dim(0))
    assert mixed is not None
    assert all(abs(value) >= clip_look.DEAD_BAND for value in mixed.recipe.values())
    assert "tint" not in mixed.recipe


def test_what_a_chosen_look_cannot_hold_is_not_learned_from_it() -> None:
    """A row edited by hand, or written before a rule changed, must not put a
    slider into the learned look that no LUT can carry."""
    examples = _chosen(
        [_dim(i) for i in range(8)],
        {"exposure": 0.3, "shadows": 0.5, "window_pull": 0.4, "rotate": 2.0},
    )

    candidate = clip_look.learned_from_clips(examples, _dim(9))

    assert candidate is not None and candidate.recipe == {"exposure": 0.3}
    assert lut.lut_recipe(candidate.recipe) == (candidate.recipe, [])

    # Nor may it cancel what the sound rows say. Half of these lift the
    # shadows, which no look can; the other half darken them. The lift is not
    # averaged in against the darkening: it counts as nothing said.
    still = _dim(3)
    same = clip_look.features_of(still)
    both = [(same, {"shadows": 0.6} if i % 2 else {"shadows": -0.4}) for i in range(8)]
    darkened = clip_look.learned_from_clips(both, still)
    assert darkened is not None and darkened.recipe["shadows"] <= -0.15


def test_features_from_another_feature_set_teach_nothing() -> None:
    good = _chosen([_dim(i) for i in range(8)], {"exposure": 0.4})
    stale = [(features[:-2], look) for features, look in good]
    broken = [([float("nan")] * len(compare.FEATURE_NAMES), {"exposure": 0.4}), (None, {})]

    assert clip_look.learned_from_clips(stale + broken, _dim(9)) is None
    # They are passed over, not allowed to spoil the ones that are sound.
    learned = clip_look.learned_from_clips(stale + broken + good, _dim(9))
    assert learned is not None and learned.examples == 8
    assert learned.recipe == {"exposure": 0.4}

    features = clip_look.features_of(_dim(1))
    assert len(features) == len(compare.FEATURE_NAMES)
    assert all(type(value) is float for value in features)
    assert json.loads(json.dumps(features)) == features


def test_the_learned_look_comes_from_clips_once_there_are_enough() -> None:
    still = _dim(60)
    photos = _photo_look({"exposure": 0.2, "vibrance": 0.3})
    eight = _chosen([_dim(i) for i in range(8)], {"contrast": 0.2})

    # Until the operator has chosen enough, the photograph look stands in.
    early = clip_look.learned(still, clips=eight[:7], photos=photos)
    assert (early.basis, early.recipe) == ("photos", {"exposure": 0.2, "vibrance": 0.3})
    # From the eighth on it is the operator's own choices for video.
    later = clip_look.learned(still, clips=eight, photos=photos)
    assert (later.basis, later.recipe) == ("clips", {"contrast": 0.2})
    assert clip_look.learned(still, clips=eight).basis == "clips"
    assert clip_look.learned(still).available is False


def test_no_change_is_always_on_offer_and_changes_nothing() -> None:
    none = clip_look.no_change()

    assert (none.source, none.available, none.recipe, none.measured) == ("none", True, {}, {})
    assert (none.left_out, none.note, none.basis, none.examples) == ((), None, None, 0)
    assert np.array_equal(lut.build(none.recipe), lut.identity())

    missing = clip_look.unavailable("model", "No look has been suggested for this clip yet.")
    assert (missing.source, missing.available, missing.recipe) == ("model", False, {})
    assert missing.note == "No look has been suggested for this clip yet."


def test_every_look_on_offer_builds_the_table_it_would_be_exported_as() -> None:
    still = _room(light=0.4)
    photos = _photo_look(PHOTO_LOOK)
    clips = _chosen([_dim(i) for i in range(8)], {"exposure": 0.3, "auto_wb": 0.7, "shadows": -0.2})
    suggested, _reason = clip_look.clean_suggestion(_answer(exposure=0.3, white_balance=0.8))

    for recipe, measured in (
        (clip_look.no_change().recipe, clip_look.no_change().measured),
        (clip_look.learned(still, photos=photos).recipe, None),
        (clip_look.learned(still, clips=clips).recipe, None),
        (suggested, None),
    ):
        if measured is None:
            measured = develop.measure(still, recipe)
        # Through JSON, as a stored look comes back.
        recipe, measured = json.loads(json.dumps(recipe)), json.loads(json.dumps(measured))
        table = lut.build(recipe, measured=measured)
        shown = lut.apply(still, table)
        assert shown.size == still.size
        # On the still it was measured on, the table is the engine's picture
        # to within what a 33-point table gives.
        engine = np.asarray(develop.apply_recipe(still, recipe), dtype=np.int16)
        assert float(np.abs(np.asarray(shown, dtype=np.int16) - engine).mean()) < 0.5


def test_nothing_a_look_carries_is_a_grade() -> None:
    """A colour difference steers the fit and is thrown away there. No
    field of a candidate holds one, under any name."""
    names = {field.name for field in dataclasses.fields(clip_look.Candidate)}

    assert names == {
        "source", "available", "recipe", "measured", "left_out", "note", "basis", "examples",
    }  # fmt: skip
    for word in ("delta", "score", "distance", "loss", "rank"):
        assert not any(word in name for name in names)
    candidate = clip_look.learned_from_photos(_photo_look(PHOTO_LOOK), _room(light=0.34))
    assert set(candidate.recipe) <= set(clip_look.SLIDERS)
    assert set(candidate.measured) <= {"levels", "wb_gains"}


# --------------------------------------------------------- where it may be used


def test_looks_load_without_the_connector_or_a_model_runtime() -> None:
    """The clip API and a job on the local-AI lane both import this. It must
    bring in neither the optional `mcp` package nor the local-model code,
    which only the Mac install can run. Asked of a fresh interpreter,
    because this one may hold either from another test file."""
    code = (
        "import sys\n"
        "import framefound.media.clip_look\n"
        "held = sorted(m for m in sys.modules if m.split('.')[0] == 'mcp'"
        " or m.startswith(('framefound.mcp', 'framefound.local_models')))\n"
        "print(','.join(held))\n"
    )
    # A fixed command line of our own, run with this interpreter.
    done = subprocess.run(  # noqa: S603 - no outside input in the arguments
        [sys.executable, "-c", code], cwd=SERVER, capture_output=True, text=True, check=False
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""
