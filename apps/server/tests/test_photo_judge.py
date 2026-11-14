"""What the local model is asked about a listing photograph, and what its
answer becomes (media/photo_judge.py; design L4 3.4). Pure: no model, no
database."""

import pytest
from pydantic import ValidationError

from framefound.media import develop, looks, photo_judge


def _answer(**said: object) -> photo_judge.PhotoJudgement:
    values: dict[str, object] = {
        "rotate": 0.0,
        "keystone": 0.0,
        "sky_worth_replacing": False,
        "caption": "Front exterior",
        "file_name": "front-exterior",
        "note": "Nothing to change.",
    }
    values.update(said)
    return photo_judge.PhotoJudgement.model_validate(values)


def test_the_question_asks_only_what_claude_decides_today() -> None:
    fields = set(photo_judge.PhotoJudgement.model_json_schema()["properties"])
    assert fields == {"rotate", "keystone", "sky_worth_replacing", "caption", "file_name", "note"}
    # Tone is the learned look's, never a model's.
    assert "Do not judge brightness or colour" in photo_judge.INSTRUCTIONS
    # JSON's minus is the keyboard one; a model shown another may write it back.
    assert photo_judge.INSTRUCTIONS.isascii()
    assert photo_judge.PROMPT_VERSION == "listing-look-1"


def test_an_answer_outside_the_bounds_or_with_more_is_refused() -> None:
    with pytest.raises(ValidationError):
        _answer(rotate=6.0)
    with pytest.raises(ValidationError):
        _answer(keystone=-1.5)
    with pytest.raises(ValidationError):
        _answer(exposure=0.4)
    with pytest.raises(ValidationError):
        _answer(caption="x" * 301)


def test_small_corrections_are_dropped_and_the_rest_kept() -> None:
    judged = photo_judge.clean_judgement(_answer(rotate=0.15, keystone=-0.04))
    assert (judged.rotate, judged.keystone) == (0.0, 0.0)
    judged = photo_judge.clean_judgement(_answer(rotate=-1.234, keystone=0.1504))
    assert (judged.rotate, judged.keystone) == (-1.23, 0.15)
    # Exactly at a dead band's edge is a correction.
    judged = photo_judge.clean_judgement(_answer(rotate=0.2, keystone=-0.05))
    assert (judged.rotate, judged.keystone) == (0.2, -0.05)


def test_the_name_and_caption_are_cleaned_as_claudes_are() -> None:
    judged = photo_judge.clean_judgement(
        _answer(
            caption="  Front exterior,\n straight-on   facade  ",
            file_name="Front Exterior 130 Davis Rd!!",
            note="Levelled   the roofline. " + "x" * 250,
            sky_worth_replacing=True,
        )
    )
    assert judged.caption == "Front exterior, straight-on facade"
    # No numbers: the most likely one is the house number.
    assert judged.slug == "front-exterior-davis-rd"
    assert judged.note.startswith("Levelled the roofline.") and len(judged.note) == 200
    assert judged.sky is True


def test_a_large_correction_is_marked_to_be_looked_at_first() -> None:
    assert not photo_judge.clean_judgement(_answer(rotate=2.9, keystone=0.59)).large
    assert photo_judge.clean_judgement(_answer(rotate=-3.0)).large
    assert photo_judge.clean_judgement(_answer(keystone=0.6)).large
    assert photo_judge.is_large(0.0, -0.75)


def test_without_a_look_the_tone_is_the_preset_and_the_straightening_the_models() -> None:
    recipe = photo_judge.proposed_recipe(None, -1.2, 0.15)
    assert recipe == {**develop.LISTING_PRESET, "rotate": -1.2, "keystone": 0.15}
    # Not cleaned and no sky: the caller adds the sky and cleans.
    assert "sky" not in recipe


def test_with_a_look_the_tone_is_the_looks_and_the_model_only_straightens() -> None:
    tone = {"exposure": 0.33, "vibrance": 0.21}
    recipe = photo_judge.proposed_recipe(tone, 2.0, 0.0)
    assert recipe == {"exposure": 0.33, "vibrance": 0.21, "rotate": 2.0}
    assert recipe == looks.combine(tone, {"rotate": 2.0})


def test_straightening_can_be_left_out() -> None:
    assert photo_judge.proposed_recipe(None, 2.0, 0.3, straighten=False) == dict(
        develop.LISTING_PRESET
    )
    assert photo_judge.proposed_recipe({"exposure": 0.1}, 2.0, 0.3, straighten=False) == {
        "exposure": 0.1
    }
