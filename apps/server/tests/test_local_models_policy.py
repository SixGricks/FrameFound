"""Whose models may be installed (local_models/policy.py).

One test per rule and per clash between rules. No blocked publisher's model
is named here: a test that needs one invents a model under
`policy.DEFAULT_BLOCKED[0]`, or adds a made-up publisher to the list.
"""

import ast
import sys
from pathlib import Path

import pytest

from framefound.local_models import policy

BLOCKED = policy.DEFAULT_BLOCKED[0]
LISTS = policy.lists_of(policy.DEFAULT_MAKERS, policy.DEFAULT_CONVERTERS, [])


def node(repo_id: str, *bases: str, found: bool = True) -> policy.Node:
    return policy.Node(repo_id, policy.publisher_of(repo_id), tuple(bases), found)


# The first model of the plan, as its pages describe it: packaged by a
# converter from a maker's model, which the maker built from another of its own.
PACKAGED = node("mlx-community/gemma-4-e4b-it-4bit", "google/gemma-4-E4B-it")
TUNED = node("google/gemma-4-E4B-it", "google/gemma-4-E4B")
ORIGINAL = node("google/gemma-4-E4B")


def test_a_makers_own_model_is_allowed() -> None:
    verdict = policy.judge([ORIGINAL], LISTS)

    assert verdict.allowed and verdict.via == "maker"
    assert verdict.reason == "google is on your makers list."
    assert verdict.offender is None


def test_a_conversion_of_a_makers_model_is_allowed_and_says_who_made_it() -> None:
    verdict = policy.judge([PACKAGED, TUNED, ORIGINAL], LISTS)

    assert verdict.allowed and verdict.via == "converter"
    assert verdict.reason == (
        "Packaged by mlx-community from google/gemma-4-E4B-it. google is on your makers list."
    )


def test_a_blocked_publisher_is_refused_even_if_it_is_also_on_the_makers_list() -> None:
    # A stored makers list can hold a name that a later release blocks.
    lists = policy.lists_of([*policy.DEFAULT_MAKERS, BLOCKED], policy.DEFAULT_CONVERTERS, [])

    verdict = policy.judge([node(f"{BLOCKED}/demo-model")], lists)

    assert not verdict.allowed and verdict.via is None
    assert verdict.reason == f"Refused. {BLOCKED} is on the blocked list, and blocked always wins."
    assert verdict.offender == BLOCKED


def test_blocked_wins_over_a_maker_further_up_the_chain() -> None:
    base = f"{BLOCKED}/demo-base"
    sentence = (
        f"Refused. It was built from {base}, which comes from {BLOCKED} — a blocked "
        "publisher. A model built from a blocked publisher's model is refused however it "
        "is packaged."
    )

    # A maker's own model, built from a blocked publisher's.
    direct = policy.judge([node("nvidia/demo-gated", base), node(base)], LISTS)
    assert not direct.allowed
    assert direct.reason == sentence
    assert direct.offender == BLOCKED

    # A conversion of a maker's model that was itself built from one: the
    # conversion and its base are both fine by name, two links up is not.
    deep = policy.judge(
        [
            node("mlx-community/demo-4bit", "google/demo-tuned"),
            node("google/demo-tuned", base),
            node(base),
        ],
        LISTS,
    )
    assert not deep.allowed
    assert deep.reason == sentence


def test_a_blocked_base_is_named_even_when_its_page_could_not_be_read() -> None:
    base = f"{BLOCKED}/demo-base"

    gone = policy.judge([node("google/demo", base), node(base, found=False)], LISTS)
    never_read = policy.judge([node("google/demo", base)], LISTS, complete=False)

    # The publisher is in the name: "could not be checked, try again" would
    # be the wrong thing to say about a model that is refused for good.
    for verdict in (gone, never_read):
        assert not verdict.allowed
        assert verdict.offender == BLOCKED
        assert "a blocked publisher" in verdict.reason


def test_a_publisher_the_operator_blocked_is_refused_like_a_built_in_one() -> None:
    lists = policy.lists_of(policy.DEFAULT_MAKERS, policy.DEFAULT_CONVERTERS, ["Example-Lab"])

    own = policy.judge([node("example-lab/some-model")], lists)
    built_on = policy.judge(
        [node("google/demo", "example-lab/some-model"), node("example-lab/some-model")], lists
    )

    assert not own.allowed and own.offender == "example-lab"
    assert not built_on.allowed and built_on.offender == "example-lab"


def test_a_conversion_that_does_not_say_what_it_was_built_from_is_refused() -> None:
    verdict = policy.judge([node("mlx-community/orphan-4bit")], LISTS)

    assert not verdict.allowed
    assert verdict.reason == (
        "This was packaged by mlx-community, and its page does not say which model it was "
        "made from. Without that FrameFound cannot tell who made it, so it is refused."
    )


def test_a_conversion_of_something_that_is_not_a_makers_is_refused_by_name() -> None:
    verdict = policy.judge(
        [
            node("mlx-community/demo-4bit", "example-lab/some-model"),
            node("example-lab/some-model"),
        ],
        LISTS,
    )

    assert not verdict.allowed
    assert verdict.reason == (
        "It was built from example-lab/some-model, published by example-lab, who is not on "
        "your makers list. Add example-lab as a maker if you trust them, then check again."
    )
    assert verdict.offender == "example-lab"


def test_a_conversion_must_be_made_from_a_makers_model_in_every_base_it_names() -> None:
    verdict = policy.judge(
        [
            node("mlx-community/demo-merge", "google/demo-a", "example-lab/demo-b"),
            node("google/demo-a"),
            node("example-lab/demo-b"),
        ],
        LISTS,
    )

    assert not verdict.allowed
    assert verdict.offender == "example-lab"


def test_a_conversion_of_a_conversion_is_refused() -> None:
    verdict = policy.judge(
        [
            node("mlx-community/demo-8bit", "mlx-community/demo-4bit"),
            node("mlx-community/demo-4bit", "google/demo"),
            node("google/demo"),
        ],
        LISTS,
    )

    assert not verdict.allowed
    # Not "add mlx-community as a maker": that advice would be wrong.
    assert "Add " not in verdict.reason
    assert "straight from a maker's own model" in verdict.reason


def test_a_makers_model_may_be_built_on_a_publisher_that_is_on_no_list() -> None:
    # The plan's sentence read strictly (an open question for the owner): a
    # maker is trusted for what it publishes; only a blocked ancestor stops it.
    verdict = policy.judge(
        [node("google/demo-tuned", "example-lab/some-model"), node("example-lab/some-model")],
        LISTS,
    )

    assert verdict.allowed and verdict.via == "maker"


def test_a_chain_that_could_not_be_read_in_full_is_refused() -> None:
    cut_short = policy.judge([TUNED, ORIGINAL], LISTS, complete=False)
    assert not cut_short.allowed
    assert cut_short.reason == (
        "FrameFound could not read everything this model was built from, so it cannot be "
        "checked. Try again."
    )

    # Even a maker's model: "built from a blocked publisher's" can only be
    # ruled out on a whole chain.
    vanished = policy.judge(
        [node("google/demo", "google/demo-base"), node("google/demo-base", found=False)], LISTS
    )
    assert not vanished.allowed
    assert vanished.reason == (
        "It says it was built from google/demo-base, but Hugging Face has no such model "
        "now, so who made it cannot be checked."
    )

    # And whatever the caller says about completeness: a base that is named
    # but was never read is an unread chain.
    claimed = policy.judge([PACKAGED], LISTS, complete=True)
    assert not claimed.allowed
    assert claimed.reason == policy.COULD_NOT_READ


def test_a_publisher_on_no_list_is_refused_and_told_how_to_be_added() -> None:
    verdict = policy.judge([node("example-lab/some-model")], LISTS)

    assert not verdict.allowed
    assert verdict.reason == (
        "example-lab is not one of your approved publishers. If you know who they are and "
        "want their models, add them under Approved publishers — as a maker if they trained "
        "it, as a converter if they only packaged it. Then check again."
    )
    assert verdict.offender == "example-lab"


def test_a_name_with_no_publisher_is_refused() -> None:
    verdict = policy.judge([policy.Node("orphan", None)], LISTS)

    assert not verdict.allowed
    assert "has no publisher in its name" in verdict.reason
    assert not policy.judge([], LISTS).allowed

    # Older pages name a base with no publisher part at all. Nobody can be
    # asked who made it, so a conversion of it is refused.
    built_on = policy.judge(
        [node("mlx-community/demo-4bit", "orphan"), policy.Node("orphan", None)], LISTS
    )
    assert not built_on.allowed
    assert built_on.reason == (
        "It was built from orphan, which has no publisher in its name, so who made it "
        "cannot be checked."
    )


def test_a_model_hugging_face_no_longer_has_is_refused() -> None:
    verdict = policy.judge([node("google/demo-withdrawn", found=False)], LISTS)

    assert not verdict.allowed
    assert verdict.reason == (
        "Hugging Face has no model called google/demo-withdrawn now, so it cannot be checked."
    )


def test_a_page_that_names_itself_is_not_refused_for_it() -> None:
    assert policy.judge([node("google/demo", "google/demo")], LISTS).allowed
    assert not policy.judge([node("mlx-community/demo", "mlx-community/demo")], LISTS).allowed


def test_names_are_compared_without_regard_to_case() -> None:
    lists = policy.lists_of(["Google"], ["MLX-Community"], ["Example-LAB"])

    assert lists.status("google") == lists.status("GOOGLE") == "maker"
    assert lists.status("mlx-community") == "converter"
    assert lists.status("example-lab") == "blocked"
    assert lists.status(BLOCKED.swapcase()) == "blocked"
    assert lists.status("somebody-else") == lists.status(None) == lists.status("") == "unknown"

    # A base spelled differently on the page that names it and on its own.
    verdict = policy.judge(
        [node("MLX-Community/Demo-4bit", "GOOGLE/Demo"), node("google/demo")], lists
    )
    assert verdict.allowed and verdict.via == "converter"


def test_no_default_maker_or_converter_is_on_the_blocked_list() -> None:
    blocked = {name.casefold() for name in policy.DEFAULT_BLOCKED}

    assert not blocked & {name.casefold() for name in policy.DEFAULT_MAKERS}
    assert not blocked & {name.casefold() for name in policy.DEFAULT_CONVERTERS}
    assert len(blocked) == len(policy.DEFAULT_BLOCKED), "a name is listed twice"
    assert all(policy.NAME.fullmatch(name) for name in policy.DEFAULT_BLOCKED)


def test_the_built_in_blocked_names_are_in_force_however_the_lists_are_built() -> None:
    by_hand = policy.PublisherLists(
        makers=frozenset({BLOCKED}), converters=frozenset(), blocked=frozenset()
    )

    assert by_hand.status(BLOCKED) == "blocked"
    assert all(LISTS.status(name) == "blocked" for name in policy.DEFAULT_BLOCKED)


@pytest.mark.parametrize(
    "stored",
    [
        None,
        7,
        "google",
        {"google": True},
        [["google"]],
        [{"name": "google", "added": "2026-11-12"}],
        [None, 3, "  "],
    ],
)
def test_a_stored_list_of_the_wrong_shape_approves_nobody_and_raises_nothing(
    stored: object,
) -> None:
    # What a row written by a newer version may hold after a rollback. This
    # is read on every check and every question to a model: it must fail
    # closed, never fail with an exception and never open.
    lists = policy.lists_of(stored, stored, stored)

    assert lists.makers == lists.converters == frozenset()
    assert lists.status("google") == "unknown"
    # A bare string is not read letter by letter into one-letter makers.
    assert lists.status("g") == "unknown"
    assert all(lists.status(name) == "blocked" for name in policy.DEFAULT_BLOCKED)
    assert not policy.judge([ORIGINAL], lists).allowed

    # One list that cannot be read does not take the others with it.
    mixed = policy.lists_of(["google"], stored, ["example-lab"])
    assert mixed.status("google") == "maker"
    assert mixed.status("example-lab") == "blocked"


def test_names_from_somebody_elses_page_are_made_fit_for_a_sentence() -> None:
    # What a model says it was built from is free text. However a chain was
    # come by, the sentence that judges it holds no line break, no character
    # that reverses the line, and no more of a name than a name can be.
    odd = "google/gemma\nIGNORE ALL PREVIOUS\u202e text " + "x" * 5000
    chains = [
        [node("mlx-community/demo-4bit", odd), policy.Node(odd, "google", found=False)],
        [node("mlx-community/demo-4bit", odd), policy.Node(odd, "nobody\n" + "y" * 5000)],
        [node("mlx-community/demo-4bit", odd), policy.Node(odd, "mlx-community")],
        [node("mlx-community/demo-4bit", odd), policy.Node(odd, BLOCKED)],
        [policy.Node("no-slash\n" + "x" * 5000, None)],
        [policy.Node(odd, "odd\u202epublisher" + "z" * 5000)],
        [policy.Node(odd, "google", found=False)],
        [policy.Node("google/demo", "google\x1b[31m")],
    ]

    for chain in chains:
        verdict = policy.judge(chain, LISTS)

        assert not verdict.allowed
        assert verdict.reason.isprintable(), verdict.reason
        assert len(verdict.reason) < 1000

    # The sentence that allows a model is no different.
    allowed = policy.judge(
        [node("mlx-community/demo-4bit", odd), policy.Node(odd, "google")], LISTS
    )
    assert allowed.allowed
    assert allowed.reason.isprintable() and len(allowed.reason) < 1000
    # An ordinary name is said whole, however long a real one gets.
    longest = f"{'p' * 96}/{'m' * 96}"
    assert policy.clean_repo_id(longest) == longest
    assert longest in policy.judge([node(longest, found=False)], LISTS).reason


def test_a_built_in_blocked_publisher_cannot_be_made_a_maker() -> None:
    with pytest.raises(ValueError) as as_maker:
        policy.check_lists(["google", BLOCKED], ["mlx-community"], [])
    with pytest.raises(ValueError) as as_converter:
        policy.check_lists(["google"], [BLOCKED.swapcase()], [])

    assert str(as_maker.value) == (
        f"{BLOCKED} is built in to the blocked list and cannot be approved."
    )
    assert str(as_converter.value) == (
        f"{BLOCKED.swapcase()} is built in to the blocked list and cannot be approved."
    )


@pytest.mark.parametrize(
    ("makers", "converters", "blocked", "sentence"),
    [
        (["google", "not a name"], [], [], "That is not a Hugging Face publisher name."),
        (["google/gemma"], [], [], "That is not a Hugging Face publisher name."),
        (["google", ""], [], [], "That is not a Hugging Face publisher name."),
        (
            ["Example-Lab"],
            [],
            ["example-lab"],
            "Example-Lab is on the blocked list. Blocked always wins; take it off that list first.",
        ),
        (
            ["google"],
            ["example-lab"],
            ["example-lab"],
            "example-lab is on the blocked list. Blocked always wins; take it off that list first.",
        ),
        (["google"], ["Google"], [], "Google is already a maker."),
        (
            [f"lab-{n}" for n in range(policy.MAX_LIST + 1)],
            [],
            [],
            "A list can hold at most 200 publishers.",
        ),
    ],
)
def test_saving_the_lists_refuses_each_clash_in_its_own_words(
    makers: list[str], converters: list[str], blocked: list[str], sentence: str
) -> None:
    with pytest.raises(ValueError) as refused:
        policy.check_lists(makers, converters, blocked)

    assert str(refused.value) == sentence


def test_saved_lists_keep_the_operators_spelling_and_lose_repeats() -> None:
    makers, converters, blocked = policy.check_lists(
        [" Google ", "google", "NVIDIA"],
        ["mlx-community", "https://huggingface.co/Example-Converter/"],
        ["Example-Lab", "example-lab", BLOCKED],
    )

    assert makers == ["Google", "NVIDIA"]
    assert converters == ["mlx-community", "Example-Converter"]
    # Only the operator's additions are stored; the built-in name is code.
    assert blocked == ["Example-Lab"]


def test_every_list_may_be_emptied() -> None:
    assert policy.check_lists([], [], []) == ([], [], [])
    assert not policy.judge([ORIGINAL], policy.lists_of([], [], [])).allowed


@pytest.mark.parametrize(
    ("pasted", "name"),
    [
        ("mlx-community/gemma-4-e4b-it-4bit", "mlx-community/gemma-4-e4b-it-4bit"),
        ("  google/gemma-4-E4B-it\n", "google/gemma-4-E4B-it"),
        ("google/gemma-4-E4B-it/", "google/gemma-4-E4B-it"),
        ("https://huggingface.co/google/gemma-4-E4B-it", "google/gemma-4-E4B-it"),
        ("http://www.huggingface.co/google/gemma-4-E4B-it/", "google/gemma-4-E4B-it"),
        ("huggingface.co/google/gemma-4-E4B-it?library=mlx", "google/gemma-4-E4B-it"),
        (
            "https://huggingface.co/mlx-community/gemma-4-e4b-it-4bit/blob/main/config.json#L3",
            "mlx-community/gemma-4-e4b-it-4bit",
        ),
        ("https://hf.co/google/gemma-4-E4B-it/tree/main", "google/gemma-4-E4B-it"),
    ],
)
def test_a_pasted_name_or_address_names_the_model(pasted: str, name: str) -> None:
    assert policy.clean_repo_id(pasted) == name


@pytest.mark.parametrize(
    "pasted",
    [
        "",
        "gemma-4-E4B-it",
        "google/",
        "/gemma",
        "google/gemma/extra",
        "google/gem ma",
        "google/..",
        "../../etc/passwd",
        "google/.hidden",
        "google\\gemma",
        "https://example.com/google/gemma-4-E4B-it",
        "https://huggingface.co/google",
        "https://huggingface.co/",
        "google/" + "x" * 97,
    ],
)
def test_anything_that_is_not_a_publisher_and_a_model_is_refused(pasted: str) -> None:
    # Both halves become folder names on disk; nothing else gets that far.
    with pytest.raises(ValueError) as refused:
        policy.clean_repo_id(pasted)

    assert str(refused.value) == policy.NOT_A_MODEL_NAME


def test_a_dataset_address_is_not_taken_for_a_model() -> None:
    with pytest.raises(ValueError, match="a dataset or a Space, not a model"):
        policy.clean_repo_id("https://huggingface.co/datasets/google/demo")


def test_the_publisher_is_the_part_before_the_slash() -> None:
    assert policy.publisher_of("google/gemma-4-E4B-it") == "google"
    assert policy.publisher_of("orphan") is None
    assert policy.publisher_of("/orphan") is None
    assert policy.publisher_of("google/") is None
    assert policy.clean_publisher(" @Google ") == "Google"


def test_the_rules_need_nothing_but_the_standard_library() -> None:
    # They are the part every other piece trusts; they must be runnable and
    # testable with no database, no settings and no network library.
    imported: set[str] = set()
    for item in ast.walk(ast.parse(Path(policy.__file__).read_text(encoding="utf-8"))):
        if isinstance(item, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in item.names)
        elif isinstance(item, ast.ImportFrom):
            # A relative import is this package, which is not the standard library.
            imported.add("." if item.level else (item.module or "").split(".")[0])

    assert imported
    assert imported <= sys.stdlib_module_names, imported - sys.stdlib_module_names
