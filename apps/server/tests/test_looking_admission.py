"""What a local model's answer becomes, and the key a look is made under.

`prompt.clean` is the gate between a small model's text and the catalogue:
what it lets through is all the review queue will ever see from a look.
`prompt.key_of` is what "already looked at" and "redo" compare. Both are
pure; nothing here touches a database or a model.
"""

from typing import Any

import pytest

from framefound import looking
from framefound.ai import tag_review
from framefound.ai.tag_review import AdmissionPolicy
from framefound.looking import prompt
from framefound.looking.prompt import LOCAL_POLICY, LookAnswer, clean, key_of

# The slugs of the vocabulary the model was shown.
SHOWN = {"barn", "power-broom", "golf-course", "aerial"}
REV = "0123456789abcdef0123456789abcdef01234567"
# Written by code point: as literals they are invisible in the source.
RLO = chr(0x202E)  # right-to-left override
ZWSP = chr(0x200B)  # zero-width space
BELL = chr(0x07)


def _look(**changes: Any) -> LookAnswer:
    answer: dict[str, Any] = {
        "caption": "A red barn beside a gravel lane.",
        "score": 74,
        "reason": "Sharp and level, but a parked truck blocks the door.",
        "matches": [{"tag": "barn", "confidence": 0.9}],
        "new_tags": [],
    }
    answer.update(changes)
    return LookAnswer.model_validate(answer)


def _names(cleaned: prompt.Cleaned) -> list[tuple[str, bool]]:
    return [(p.name, p.existing) for p in cleaned.proposals]


# --- the tags ------------------------------------------------------------------


def test_a_reworded_match_is_a_new_tag() -> None:
    """A small model "matches" a word the vocabulary does not hold. It must
    not attach to a tag nobody chose, and it must clear the new-tag bar."""
    answer = _look(
        matches=[
            {"tag": "Barns", "confidence": 0.84},
            {"tag": "Power  Broom", "confidence": 0.7},
        ]
    )

    cleaned = clean(answer, SHOWN, new_tags=True)

    assert _names(cleaned) == [("power broom", True)], (
        "'barns' is a new word at 0.84, under the new-tag floor; 'power broom' is the "
        "vocabulary's own word, however it was spaced"
    )
    assert cleaned.dropped == 1


def test_which_list_the_model_used_decides_nothing() -> None:
    answer = _look(matches=[], new_tags=[{"tag": "aerial", "confidence": 0.65}])

    assert _names(clean(answer, SHOWN, new_tags=True)) == [("aerial", True)]


def test_new_tags_can_be_switched_off() -> None:
    answer = _look(
        matches=[
            {"tag": "barn", "confidence": 0.9},
            {"tag": "hay loft", "confidence": 0.99},  # reworded: a new word too
        ],
        new_tags=[{"tag": "gravel lane", "confidence": 0.95}],
    )

    on = clean(answer, SHOWN, new_tags=True)
    off = clean(answer, SHOWN, new_tags=False)

    assert _names(on) == [("hay loft", False), ("gravel lane", False), ("barn", True)]
    assert (_names(off), off.dropped) == ([("barn", True)], 2)


def test_a_weak_guess_is_not_admitted() -> None:
    """Guesses Claude's defaults would let in are held back from a model
    nobody has measured on this footage yet."""
    answer = _look(
        matches=[{"tag": "barn", "confidence": 0.55}],
        new_tags=[{"tag": "silo", "confidence": 0.8}],
    )

    claude_would = clean(answer, SHOWN, new_tags=True, policy=AdmissionPolicy())
    local = clean(answer, SHOWN, new_tags=True)

    assert _names(claude_would) == [("silo", False), ("barn", True)]
    assert (local.proposals, local.dropped) == ([], 2)


def test_the_floors_are_reached_not_exceeded() -> None:
    answer = _look(
        matches=[{"tag": "barn", "confidence": 0.6}],
        new_tags=[{"tag": "silo", "confidence": 0.85}],
    )

    assert _names(clean(answer, SHOWN, new_tags=True)) == [("silo", False), ("barn", True)]


def test_a_picture_gets_at_most_four_tags_strongest_first() -> None:
    answer = _look(
        matches=[
            {"tag": "barn", "confidence": 0.7},
            {"tag": "aerial", "confidence": 0.95},
            {"tag": "golf course", "confidence": 0.8},
            {"tag": "power broom", "confidence": 0.65},
        ],
        new_tags=[
            {"tag": "silo", "confidence": 0.9},
            {"tag": "a very long tag", "confidence": 0.99},  # four words: never a tag
        ],
    )

    cleaned = clean(answer, SHOWN, new_tags=True)

    assert _names(cleaned) == [
        ("aerial", True),
        ("silo", False),
        ("golf course", True),
        ("barn", True),
    ]
    assert cleaned.dropped == 2


def test_the_same_tag_twice_is_one_suggestion_at_its_stronger_guess() -> None:
    answer = _look(
        matches=[{"tag": "barn", "confidence": 0.7}],
        new_tags=[{"tag": "Barn", "confidence": 0.9}],
    )

    cleaned = clean(answer, SHOWN, new_tags=False)

    assert [(p.name, p.existing, p.confidence) for p in cleaned.proposals] == [("barn", True, 0.9)]
    assert cleaned.dropped == 0, "a repeat is merged, not held back"


def test_a_tag_name_keeps_only_printable_text() -> None:
    """A NUL cannot be stored on PostgreSQL, and a direction override would
    show the operator a different word from the one stored."""
    answer = _look(
        # A hidden character inside a vocabulary word is still that word.
        matches=[{"tag": "ba" + ZWSP + "rn", "confidence": 0.9}],
        new_tags=[
            {"tag": "hay\u0000 loft", "confidence": 0.9},
            {"tag": RLO + "silo", "confidence": 0.9},
            {"tag": BELL + ZWSP, "confidence": 0.99},  # nothing left: not a tag
        ],
    )

    cleaned = clean(answer, SHOWN, new_tags=True)

    assert sorted(_names(cleaned)) == [("barn", True), ("hay loft", False), ("silo", False)]


# --- the caption, the score, the reason ----------------------------------------


def test_the_caption_and_reason_are_one_line_of_printable_text() -> None:
    answer = _look(
        caption="  A red barn.\n\nIgnore the rules\u0000 above.\t",
        reason="Soft\r\nfocus " + RLO + "on the door",
    )

    cleaned = clean(answer, SHOWN, new_tags=True)

    assert cleaned.caption == "A red barn. Ignore the rules above."
    assert cleaned.reason == "Soft focus on the door"
    assert cleaned.score == 74


def test_an_answer_that_skipped_validation_is_still_held_to_its_limits() -> None:
    """The runner validates every answer; `clean` does not count on it."""
    answer = LookAnswer.model_construct(
        caption="c" * 400, score=140, reason="r " * 200, matches=[], new_tags=[]
    )

    cleaned = clean(answer, SHOWN, new_tags=True)

    assert (len(cleaned.caption), cleaned.score, len(cleaned.reason)) == (300, 100, 200)
    assert clean(answer.model_copy(update={"score": -5}), SHOWN, new_tags=True).score == 0


def test_the_lengths_kept_are_the_lengths_a_model_is_held_to() -> None:
    schema = LookAnswer.model_json_schema()
    assert schema["properties"]["caption"]["maxLength"] == prompt.CAPTION_CHARS
    assert schema["properties"]["reason"]["maxLength"] == prompt.REASON_CHARS
    assert schema["$defs"]["TagGuess"]["properties"]["tag"]["maxLength"] == prompt.TAG_CHARS


def test_the_local_model_is_held_to_a_stricter_bar_than_claude() -> None:
    claude = AdmissionPolicy()

    assert (
        AdmissionPolicy(existing_floor=0.6, new_floor=0.85, max_words=3, max_per_asset=4)
        == LOCAL_POLICY
    )
    assert LOCAL_POLICY.existing_floor > claude.existing_floor
    assert LOCAL_POLICY.new_floor > claude.new_floor
    assert LOCAL_POLICY.max_per_asset < claude.max_per_asset
    assert LOCAL_POLICY.max_words <= claude.max_words


# --- the key -------------------------------------------------------------------


def test_the_key_names_the_build_and_the_question() -> None:
    assert key_of("google/gemma-4-12B-it", REV, variant="") == (
        f"google/gemma-4-12B-it@{REV}#look-1"
    )
    assert key_of("google/gemma-4-12B-it-GGUF", REV, variant="Q4_K_M") == (
        f"google/gemma-4-12B-it-GGUF@{REV}/Q4_K_M#look-1"
    )


def test_the_key_changes_with_the_prompt_version() -> None:
    now = key_of("google/gemma-4-12B-it", REV, variant="")

    assert now.endswith(f"#{prompt.PROMPT_VERSION}")
    assert key_of("google/gemma-4-12B-it", REV, variant="", prompt_version="look-2") != now


def test_two_builds_of_one_commit_are_two_keys_and_a_new_commit_is_a_third() -> None:
    """Without the variant, a redo with the second quantisation would find
    everything already looked at by the first."""
    keys = {
        key_of("nvidia/demo-GGUF", REV, variant="Q4_K_M"),
        key_of("nvidia/demo-GGUF", REV, variant="Q8_0"),
        key_of("nvidia/demo-GGUF", REV, variant=""),
        key_of("nvidia/demo-GGUF", "f" * 40, variant="Q4_K_M"),
    }

    assert len(keys) == 4


def test_a_key_fits_its_column_and_two_long_keys_stay_two() -> None:
    long_repo = "allenai/" + "m" * 190
    first = key_of(long_repo, "a" * 64, variant="v" * 100)
    second = key_of(long_repo, "a" * 64, variant="v" * 99 + "w")

    assert len(first) == len(second) == prompt.KEY_CHARS
    assert first != second
    assert first.startswith(f"{long_repo}@")
    assert first == key_of(long_repo, "a" * 64, variant="v" * 100), "the same build, the same key"


def test_a_key_is_never_the_roll_ups_word_for_several() -> None:
    assert "mixed" not in {key_of("apple/m", REV, variant=v) for v in ("", "mixed")}


@pytest.mark.parametrize(
    ("repo", "revision", "version"),
    [("", REV, "look-1"), ("google/gemma", "", "look-1"), ("google/gemma", REV, "")],
)
def test_a_key_that_names_no_build_is_refused(repo: str, revision: str, version: str) -> None:
    with pytest.raises(ValueError, match="repository, a commit and a question"):
        key_of(repo, revision, variant="", prompt_version=version)


def test_the_variant_has_to_be_said() -> None:
    with pytest.raises(TypeError):
        key_of("google/gemma", REV)  # type: ignore[call-arg]


# --- the origin ------------------------------------------------------------------


def test_looking_and_the_tag_reviewer_use_one_word_for_a_local_suggestion() -> None:
    assert looking.ORIGIN_LOCAL is tag_review.ORIGIN_LOCAL
    assert tag_review.ORIGIN_LOCAL == "local"
    assert tag_review.ORIGIN_LOCAL not in {
        tag_review.ORIGIN_CLIP,
        tag_review.ORIGIN_CLAUDE,
        tag_review.ORIGIN_MCP,
    }
