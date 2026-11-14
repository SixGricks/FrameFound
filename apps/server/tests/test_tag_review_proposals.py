"""`proposals_from_answer`: what an answer's tags become, whoever answered.

It was the second half of `parse_answer` and is now a function of its own,
so that a model on this Mac goes through the same cleaning as Claude does.
tests/test_tag_review.py still covers it through `parse_answer`, unedited;
what is proved here is the half that is new — that it needs no Anthropic
envelope, and that an answer which is not the shape asked for is skipped
rather than trusted, since from now on the answers come from small models.
"""

import json
from typing import Any

import pytest

from framefound.ai import tag_review
from framefound.ai.tag_review import TagProposal, proposals_from_answer
from framefound.looking.prompt import LookAnswer

SLUGS = {"power-broom", "barn", "golf-course"}


def _enveloped(answer: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "tool_use", "name": "review_tags", "input": answer}]}


def test_the_claude_route_and_the_bare_answer_end_in_the_same_proposals() -> None:
    answer = {
        "matches": [
            {"tag": "  Power  Broom ", "confidence": 0.9},
            {"tag": "barn", "confidence": 2},
        ],
        "new_tags": [
            {"tag": "power-broom", "confidence": 0.4, "why": "dup"},
            {"tag": "Horse Barn", "confidence": 0.81, "why": "a red barn with stalls"},
            {"tag": "!!!", "confidence": 0.9, "why": "nothing usable"},
        ],
        "description": "A  barn\nand a broom",
    }

    bare = proposals_from_answer(answer, SLUGS)

    assert bare == tag_review.parse_answer(_enveloped(answer), SLUGS)
    proposals, description = bare
    assert description == "A barn and a broom"
    assert proposals == [
        TagProposal(name="barn", confidence=1.0, existing=True),
        TagProposal(name="power broom", confidence=0.9, existing=True),
        TagProposal(
            name="horse barn", confidence=0.81, existing=False, why="a red barn with stalls"
        ),
    ]


def test_a_look_goes_through_as_it_is_given() -> None:
    """The Looker's answer has a caption, not a description; its tags are
    cleaned exactly as Claude's are and the description is simply empty."""
    look = LookAnswer.model_validate_json(
        json.dumps(
            {
                "caption": "A fairway seen from above.",
                "score": 82,
                "reason": "Steady and well lit.",
                "matches": [{"tag": "Golf Course", "confidence": 0.93}],
                "new_tags": [{"tag": "sand trap", "confidence": 0.7}],
            }
        )
    )

    proposals, description = proposals_from_answer(look.model_dump(), SLUGS)

    assert description == ""
    assert proposals == [
        TagProposal(name="golf course", confidence=0.93, existing=True),
        TagProposal(name="sand trap", confidence=0.7, existing=False),
    ]


def test_which_list_a_tag_came_in_decides_nothing() -> None:
    """A model's word that a tag "is in the vocabulary" is not evidence. A
    reworded match is a new word, and a "new" tag the catalogue already has
    is not new — so nothing attaches to a tag it does not actually name."""
    proposals, _ = proposals_from_answer(
        {
            "matches": [{"tag": "power brooms", "confidence": 0.9}],
            "new_tags": [{"tag": "Barn", "confidence": 0.6}],
        },
        SLUGS,
    )
    assert {p.name: p.existing for p in proposals} == {"power brooms": False, "barn": True}


@pytest.mark.parametrize(
    "answer",
    [
        None,
        "matches: barn",
        ["barn"],
        42,
        {},
        {"matches": None, "new_tags": None},
        {"matches": "barn", "new_tags": {"tag": "barn", "confidence": 1}},
        {"matches": ["barn", 7, None, ["barn", 0.9]]},
        {"matches": [{"confidence": 0.9}, {"tag": "", "confidence": 0.9}]},
        {"matches": [{"tag": "!!! ???", "confidence": 0.9}]},
        # A tag that is not text: str() would have made these "none",
        # "['barn']", "{'x': 'silo'}" and "7" — new words in the review queue.
        {"matches": [{"tag": None, "confidence": 0.9}]},
        {"matches": [{"tag": ["barn"], "confidence": 0.9}]},
        {"new_tags": [{"tag": {"x": "silo"}, "confidence": 0.9, "why": ""}]},
        {"new_tags": [{"tag": {"name": "silo"}, "confidence": 0.95, "why": "tall"}]},
        {"new_tags": [{"tag": 7, "confidence": 0.9, "why": "a number"}]},
        {"new_tags": [{"tag": True, "confidence": 0.9, "why": "a yes"}]},
    ],
)
def test_an_answer_that_is_not_the_shape_asked_for_proposes_nothing(answer: Any) -> None:
    assert proposals_from_answer(answer, SLUGS)[0] == []


@pytest.mark.parametrize(
    ("said", "kept"),
    [
        ("0.75", 0.75),
        (7, 1.0),
        (-3, 0.0),
        (True, 1.0),
        ("very", 0.0),
        (None, 0.0),
        ([0.9], 0.0),
        ({"value": 0.9}, 0.0),
        (float("nan"), 0.0),
        (float("inf"), 1.0),
        # JSON carries whole numbers of any length; this one is too long to be
        # a float, and used to stop the whole answer being read.
        pytest.param(10**400, 0.0, id="a 400-digit number"),
    ],
)
def test_a_confidence_is_held_between_nought_and_one_whatever_was_said(
    said: Any, kept: float
) -> None:
    proposals, _ = proposals_from_answer({"matches": [{"tag": "barn", "confidence": said}]}, SLUGS)
    assert proposals == [TagProposal(name="barn", confidence=kept, existing=True)]


def test_a_name_a_reason_and_a_description_are_cut_to_length_and_kept_on_one_line() -> None:
    proposals, description = proposals_from_answer(
        {
            "matches": [],
            "new_tags": [
                {"tag": "SILO\n" + "very " * 40, "confidence": 0.9, "why": "tall\n\n" + "w" * 400}
            ],
            "description": "line one\nline two " + "d" * 400,
        },
        SLUGS,
    )
    (silo,) = proposals
    assert silo.name.startswith("silo very very") and len(silo.name) <= tag_review.MAX_TAG_CHARS
    assert silo.why.startswith("tall www") and len(silo.why) == 200
    assert description.startswith("line one line two ddd") and len(description) == 200
    assert "\n" not in silo.name + silo.why + description


@pytest.mark.parametrize("said", [None, 7, ["tall"], {"text": "tall"}, True])
def test_a_reason_or_a_description_that_is_not_text_is_left_empty(said: Any) -> None:
    """The tag is good, so it is kept; what came with it in place of words is
    not turned into words ("None", "['tall']")."""
    proposals, description = proposals_from_answer(
        {"new_tags": [{"tag": "silo", "confidence": 0.9, "why": said}], "description": said},
        SLUGS,
    )
    assert proposals == [TagProposal(name="silo", confidence=0.9, existing=False, why="")]
    assert description == ""


def test_a_tag_said_twice_is_kept_once_at_its_stronger_confidence() -> None:
    proposals, _ = proposals_from_answer(
        {
            "matches": [{"tag": "barn", "confidence": 0.4}, {"tag": "Barn ", "confidence": 0.8}],
            "new_tags": [{"tag": "BARN", "confidence": 0.6, "why": "again"}],
        },
        SLUGS,
    )
    assert proposals == [TagProposal(name="barn", confidence=0.8, existing=True)]
