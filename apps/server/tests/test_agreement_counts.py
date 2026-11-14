"""The two counts Compare shows: a model's tags against the tags a person
decided, and its scores against the shots an editor used.

Pure arithmetic on tables small enough to check by hand — every expected
number below can be counted off the rows above it. What is being guarded
is as much what the counts are *not*: not per shot, not over different
items for different models, not a percentage, not a grade.
"""

import ast
import dataclasses
import random
import re
import sys
import typing
from pathlib import Path

from framefound.local_models import agreement
from framefound.local_models.agreement import ItemAnswer, ItemTruth


def _truth(
    item: str,
    footage: str,
    *,
    yes: tuple[str, ...] = (),
    no: tuple[str, ...] = (),
    used: bool | None = None,
    kind: str = "shot",
) -> ItemTruth:
    return ItemTruth(
        item=item, footage=footage, kind=kind, yes=frozenset(yes), no=frozenset(no), used=used
    )


def _said(
    item: str,
    *tags: tuple[str, float, bool],
    score: int | None = 70,
    seconds: float | None = 5.0,
    attempts: int = 1,
) -> ItemAnswer:
    return ItemAnswer(
        item=item,
        ok=True,
        tags=tags,
        score=score,
        seconds=seconds,
        attempts=attempts,
        problem=None,
    )


def _failed(item: str, problem: str | None, *, seconds: float | None = None) -> ItemAnswer:
    return ItemAnswer(
        item=item, ok=False, tags=(), score=None, seconds=seconds, attempts=2, problem=problem
    )


def _by_item(*answers: ItemAnswer) -> dict[str, ItemAnswer]:
    return {answer.item: answer for answer in answers}


# --- tags ----------------------------------------------------------------------

# One clip, three of its shots in the sample. A person said yes to "drone"
# and "barn" on the clip and no to "pond".
CLIP = [
    _truth("a1", "clip-a", yes=("drone", "barn"), no=("pond",)),
    _truth("a2", "clip-a", yes=("drone", "barn"), no=("pond",)),
    _truth("a3", "clip-a", yes=("drone", "barn"), no=("pond",)),
]


def test_tags_are_counted_per_clip_not_per_shot() -> None:
    """A tag belongs to the clip. "drone" on all three shots is one tag found,
    not three; "barn" seen in one shot only is the barn found."""
    answers = _by_item(
        _said("a1", ("drone", 0.9, True)),
        _said("a2", ("drone", 0.8, True), ("barn", 0.7, True)),
        _said("a3", ("drone", 0.9, True)),
    )

    counted = agreement.tag_agreement(CLIP, answers, over={"a1", "a2", "a3"})

    assert counted == agreement.TagAgreement(
        floor=0.0,
        footage=1,
        yes_total=2,
        yes_found=2,
        no_total=1,
        no_repeated=0,
        proposed=2,
        unjudged=0,
        unjudged_new=0,
    )


def test_a_clip_is_judged_only_on_the_shots_of_it_that_are_counted() -> None:
    answers = _by_item(
        _said("a1", ("drone", 0.9, True)),
        _said("a2", ("barn", 0.7, True)),
        _said("a3", ("pond", 0.9, True)),
    )
    counted = agreement.tag_agreement(CLIP, answers, over={"a1"})
    assert counted is not None
    assert (counted.yes_found, counted.no_repeated, counted.proposed) == (1, 0, 1)


def test_a_tag_said_no_to_and_proposed_anyway_is_counted_as_repeated() -> None:
    answers = _by_item(_said("a1", ("pond", 0.9, True), ("drone", 0.9, True)))
    counted = agreement.tag_agreement(CLIP, answers, over={"a1", "a2", "a3"})
    assert counted is not None
    assert (counted.no_total, counted.no_repeated, counted.yes_found) == (1, 1, 1)


def test_when_two_shots_of_one_clip_disagree_about_a_tag_the_yes_stands() -> None:
    """One ruling per tag per clip is what is expected, but two rows read at
    different moments can disagree. Counted as both, "barn" would be found
    and repeated at once, and the parts would add up to more than the
    whole beside them."""
    truth = [
        _truth("b1", "clip-b", yes=("barn",), no=("pond",)),
        _truth("b2", "clip-b", yes=("drone",), no=("barn",)),
    ]
    answers = _by_item(_said("b1", ("barn", 0.9, True)), _said("b2", ("pond", 0.9, True)))

    counted = agreement.tag_agreement(truth, answers, over={"b1", "b2"})

    assert counted is not None
    assert (counted.yes_total, counted.yes_found) == (2, 1)
    assert (counted.no_total, counted.no_repeated) == (1, 1)
    assert counted.proposed == 2 == counted.yes_found + counted.no_repeated + counted.unjudged


def test_a_tag_nobody_ruled_on_is_neither_right_nor_wrong() -> None:
    """ "silo" is the catalogue's word and "grain leg" is not; nobody has said
    yes or no to either on this clip, so neither is a hit or a miss."""
    answers = _by_item(
        _said("a1", ("drone", 0.9, True), ("silo", 0.8, True), ("grain-leg", 0.8, False)),
        _said("a2", ("grain-leg", 0.6, False)),
    )

    counted = agreement.tag_agreement(CLIP, answers, over={"a1", "a2", "a3"})

    assert counted is not None
    assert (counted.yes_found, counted.no_repeated) == (1, 0)
    assert (counted.proposed, counted.unjudged, counted.unjudged_new) == (3, 2, 1)


def test_a_floor_drops_the_unsure_proposals() -> None:
    answers = _by_item(
        _said(
            "a1",
            ("drone", 0.95, True),
            ("barn", 0.6, True),
            ("pond", 0.59, True),
            ("silo", 0.85, True),
            ("hay-bale", 0.3, False),
        )
    )
    over = {"a1", "a2", "a3"}

    everything, sure, very_sure = (
        agreement.tag_agreement(CLIP, answers, over=over, floor=floor) for floor in agreement.FLOORS
    )

    assert agreement.FLOORS == (0.0, 0.6, 0.85)
    assert everything is not None and sure is not None and very_sure is not None
    assert [
        (c.floor, c.yes_found, c.no_repeated, c.proposed, c.unjudged, c.unjudged_new)
        for c in (everything, sure, very_sure)
    ] == [
        (0.0, 2, 1, 5, 2, 1),
        (0.6, 2, 0, 3, 1, 0),  # exactly at the floor is kept
        (0.85, 1, 0, 2, 1, 0),
    ]
    # What there was to find does not move with the floor.
    assert {(c.footage, c.yes_total, c.no_total) for c in (everything, sure, very_sure)} == {
        (1, 2, 1)
    }


def test_footage_with_nothing_decided_is_not_counted() -> None:
    """Nobody ruled on anything on clip B, so there is nothing there to agree
    with: its proposals are not "unjudged", they are simply not counted."""
    truth = [*CLIP, _truth("b1", "clip-b"), _truth("p1", "photo-1", kind="photo", yes=("barn",))]
    answers = _by_item(
        _said("a1", ("drone", 0.9, True)),
        _said("b1", ("drone", 0.9, True), ("tractor", 0.9, True)),
        _said("p1", ("barn", 0.9, True), ("tractor", 0.9, True)),
    )

    counted = agreement.tag_agreement(truth, answers, over={"a1", "b1", "p1"})

    assert counted is not None
    assert counted.footage == 2
    assert (counted.yes_total, counted.yes_found) == (3, 2)
    assert (counted.proposed, counted.unjudged) == (3, 1)

    assert agreement.tag_agreement(truth, answers, over={"b1"}) is None
    assert agreement.tag_agreement(truth, answers, over=set()) is None


def test_an_item_that_failed_or_was_never_asked_proposes_nothing() -> None:
    answers = _by_item(_said("a1", ("drone", 0.9, True)), _failed("a2", "bad_answer"))
    counted = agreement.tag_agreement(CLIP, answers, over={"a1", "a2", "a3", "not-in-the-sample"})
    assert counted is not None
    assert (counted.footage, counted.yes_total, counted.yes_found, counted.proposed) == (1, 2, 1, 1)


# --- scores --------------------------------------------------------------------

# Two shots an editor used and three left out.
SHOTS = [
    _truth("u1", "clip-a", used=True),
    _truth("u2", "clip-b", used=True),
    _truth("n1", "clip-a", used=False),
    _truth("n2", "clip-b", used=False),
    _truth("n3", "clip-c", used=False),
]
ALL_SHOTS = {"u1", "u2", "n1", "n2", "n3"}


def _scores(**scores: int | None) -> dict[str, ItemAnswer]:
    return _by_item(*(_said(item, score=score) for item, score in scores.items()))


def test_every_used_shot_is_set_against_every_unused_one() -> None:
    """u1=90 beats 80, 60 and 40. u2=60 loses to 80, ties 60, beats 40."""
    counted = agreement.score_agreement(
        SHOTS, _scores(u1=90, u2=60, n1=80, n2=60, n3=40), over=ALL_SHOTS
    )

    assert counted == agreement.ScoreAgreement(
        used=2, unused=3, pairs=6, higher=4, lower=1, same=1, scored=5, distinct=4, few=True
    )


def test_equal_scores_are_counted_as_the_same_not_as_a_win() -> None:
    counted = agreement.score_agreement(
        SHOTS, _scores(u1=70, u2=70, n1=70, n2=71, n3=69), over=ALL_SHOTS
    )
    assert counted is not None
    assert (counted.pairs, counted.higher, counted.lower, counted.same) == (6, 2, 2, 2)


def test_a_model_that_scores_everything_alike_has_one_distinct_score() -> None:
    """A small model may give every shot 70. That is a result, and it must
    read as one: every pair the same, one score given."""
    counted = agreement.score_agreement(
        SHOTS, _scores(u1=70, u2=70, n1=70, n2=70, n3=70), over=ALL_SHOTS
    )
    assert counted is not None
    assert (counted.higher, counted.lower, counted.same, counted.pairs) == (0, 0, 6, 6)
    assert (counted.scored, counted.distinct) == (5, 1)


def test_no_used_shots_means_no_score_figure() -> None:
    answers = _scores(u1=90, u2=60, n1=80, n2=60, n3=40)

    assert agreement.score_agreement(SHOTS, answers, over={"n1", "n2", "n3"}) is None
    assert agreement.score_agreement(SHOTS, answers, over={"u1", "u2"}) is None
    assert agreement.score_agreement(SHOTS, answers, over=set()) is None
    # The only used shots have no score: there is still no pair.
    assert agreement.score_agreement(SHOTS, _scores(u1=None, n1=80), over=ALL_SHOTS) is None


def test_a_photograph_is_never_set_against_a_shot() -> None:
    """Cut history says which shots were used; it says nothing of a photograph."""
    truth = [*SHOTS, _truth("p1", "photo-1", kind="photo"), _truth("p2", "photo-2", kind="photo")]
    answers = {**_scores(u1=90, u2=60, n1=80, n2=60, n3=40), **_scores(p1=100, p2=0)}

    with_photos = agreement.score_agreement(truth, answers, over={*ALL_SHOTS, "p1", "p2"})

    assert with_photos == agreement.score_agreement(SHOTS, answers, over=ALL_SHOTS)
    assert agreement.score_agreement(truth, answers, over={"p1", "p2"}) is None


def test_a_shot_without_a_score_takes_no_part() -> None:
    answers = {**_scores(u1=90, n1=80, n2=None), **_by_item(_failed("u2", "timeout"))}
    counted = agreement.score_agreement(SHOTS, answers, over=ALL_SHOTS)
    assert counted is not None
    assert (counted.used, counted.unused, counted.pairs, counted.scored) == (1, 1, 1, 2)


def test_fewer_than_five_used_shots_is_marked_as_a_hint() -> None:
    def counted(used: int) -> agreement.ScoreAgreement:
        truth = [_truth(f"u{n}", f"clip-{n}", used=True) for n in range(used)]
        truth.append(_truth("n", "clip-n", used=False))
        answers = _by_item(*(_said(row.item, score=50) for row in truth))
        result = agreement.score_agreement(truth, answers, over={row.item for row in truth})
        assert result is not None
        return result

    assert agreement.FEW_USED == 5
    assert counted(4).few is True
    assert counted(5).few is False


# --- the same items for every model --------------------------------------------


def test_only_items_every_model_answered_are_counted() -> None:
    """The second model failed on n2 — the one unused shot the first model
    scored above a used one. Counted each on its own items, the second model
    would be measured on an easier sample than the first."""
    first = _scores(u1=90, u2=60, n1=50, n2=95, n3=40)
    second = {**_scores(u1=90, u2=60, n1=50, n3=40), **_by_item(_failed("n2", "bad_answer"))}

    over = agreement.common({"first": first, "second": second})

    assert over == {"u1", "u2", "n1", "n3"}
    one = agreement.score_agreement(SHOTS, first, over=over)
    two = agreement.score_agreement(SHOTS, second, over=over)
    assert one == two
    assert one is not None and (one.pairs, one.higher, one.lower) == (4, 4, 0)
    # On everything it answered, the first model reads differently.
    alone = agreement.score_agreement(SHOTS, first, over=ALL_SHOTS)
    assert alone is not None and (alone.pairs, alone.higher, alone.lower) == (6, 4, 2)


def test_an_item_one_model_was_never_asked_is_not_in_common() -> None:
    answers = {
        "first": _scores(u1=1, n1=2, n2=3),
        "second": _scores(u1=1, n1=2),
        "third": {**_scores(u1=1), **_by_item(_failed("n1", "died"))},
    }
    assert agreement.common(answers) == {"u1"}
    assert agreement.common({"first": answers["first"]}) == {"u1", "n1", "n2"}
    assert agreement.common({}) == frozenset()
    assert agreement.common({"first": {}, "second": answers["second"]}) == frozenset()


# --- pace and form -------------------------------------------------------------

KINDS = {"s1": "shot", "s2": "shot", "s3": "shot", "s4": "shot", "p1": "photo", "p2": "photo"}


def test_pace_leaves_out_the_failures_and_the_load() -> None:
    """Three shots answered in 4, 6 and 8 seconds; a fourth hung for five
    minutes and failed. The load is not an item and is not here at all."""
    answers = _by_item(
        _said("s1", seconds=4.0),
        _said("s2", seconds=8.0),
        _said("s3", seconds=6.0),
        _failed("s4", "timeout", seconds=300.0),
        _said("p1", seconds=1.5),
        _said("p2", seconds=2.5),
    )

    assert agreement.pace(answers, KINDS, "shot") == agreement.Pace(
        answered=3, median_s=6.0, slowest_s=8.0, per_hour=600
    )
    # Photographs are one picture, shots three: never one pace for both.
    assert agreement.pace(answers, KINDS, "photo") == agreement.Pace(
        answered=2, median_s=2.0, slowest_s=2.5, per_hour=1800
    )


def test_pace_with_nothing_timed_promises_nothing() -> None:
    nothing = agreement.Pace(answered=0, median_s=None, slowest_s=None, per_hour=None)
    assert agreement.pace({}, KINDS, "shot") == nothing
    assert agreement.pace(_by_item(_failed("s1", "died", seconds=3.0)), KINDS, "shot") == nothing

    untimed = _by_item(_said("s1", seconds=None), _said("s2", seconds=float("nan")))
    assert agreement.pace(untimed, KINDS, "shot") == agreement.Pace(
        answered=2, median_s=None, slowest_s=None, per_hour=None
    )
    instant = _by_item(_said("s1", seconds=0.0))
    assert agreement.pace(instant, KINDS, "shot").per_hour is None


def test_form_says_how_many_were_answered_retried_and_failed_and_why() -> None:
    answers = _by_item(
        _said("s1"),
        _said("s2", attempts=2),
        _said("s3", attempts=2),
        _failed("s4", "bad_answer"),
        _failed("p1", "bad_answer"),
        _failed("p2", "timeout"),
        _failed("p3", None),
    )

    assert agreement.form(answers, asked=10) == agreement.Form(
        asked=10,
        answered=3,
        second_tries=2,
        failed={"bad_answer": 2, "timeout": 1, "unknown": 1},
    )
    assert agreement.form({}, asked=60) == agreement.Form(
        asked=60, answered=0, second_tries=0, failed={}
    )


def test_spread_is_how_far_apart_the_models_scored_one_item() -> None:
    assert agreement.spread([70, 85, 40]) == 45
    assert agreement.spread([70, None, 70]) == 0
    # One score differs from nothing; that is not "they agree".
    assert agreement.spread([70, None]) is None
    assert agreement.spread([None, None]) is None
    assert agreement.spread([]) is None


# --- what the counts are not ---------------------------------------------------


def test_the_parts_of_each_count_always_add_up() -> None:
    """Whatever the answers, a count is shown beside what it is a count of —
    so the parts have to sum to the whole, on any data."""
    rng = random.Random(20261112)
    words = [f"tag-{n}" for n in range(12)]
    for _round in range(200):
        truth, answers = [], {}
        for clip in range(rng.randint(1, 6)):
            ruled = rng.sample(words, rng.randint(0, 6))
            for shot in range(rng.randint(1, 4)):
                # Each shot reads its own rulings, so the shots of one clip
                # sometimes disagree about a tag — the case the sums must
                # survive as well.
                ruled = rng.sample(ruled, len(ruled))
                split = rng.randint(0, len(ruled))
                item = f"c{clip}s{shot}"
                truth.append(
                    _truth(
                        item,
                        f"clip-{clip}",
                        yes=tuple(ruled[:split]),
                        no=tuple(ruled[split:]),
                        used=rng.choice([True, False, None]),
                    )
                )
                if rng.random() < 0.15:
                    answers[item] = _failed(item, "bad_answer")
                elif rng.random() < 0.9:
                    tags = tuple(
                        (word, round(rng.random(), 2), rng.random() < 0.7)
                        for word in rng.sample(words, rng.randint(0, 6))
                    )
                    answers[item] = _said(item, *tags, score=rng.choice([None, 40, 70, 70, 90]))
        over = {row.item for row in truth if rng.random() < 0.8}

        for floor in agreement.FLOORS:
            tags = agreement.tag_agreement(truth, answers, over=over, floor=floor)
            if tags is None:
                continue
            assert tags.proposed == tags.yes_found + tags.no_repeated + tags.unjudged
            assert 0 <= tags.yes_found <= tags.yes_total
            assert 0 <= tags.no_repeated <= tags.no_total
            assert 0 <= tags.unjudged_new <= tags.unjudged
            assert tags.footage >= 1 and tags.yes_total + tags.no_total >= 1
        scores = agreement.score_agreement(truth, answers, over=over)
        if scores is not None:
            assert scores.pairs == scores.used * scores.unused
            assert scores.pairs == scores.higher + scores.lower + scores.same
            assert scores.scored == scores.used + scores.unused
            assert 1 <= scores.distinct <= scores.scored


def test_an_agreement_is_counts_with_what_they_count_and_never_a_grade() -> None:
    """A figure that looks like a mark is read as one. Nothing in the two
    agreements is a share, a percentage or a rank, so nothing downstream can
    show one without first inventing it."""
    for counts, not_counts in (
        (agreement.TagAgreement, {"floor": float}),
        (agreement.ScoreAgreement, {"few": bool}),
    ):
        # get_type_hints, not field.type: with postponed annotations a field's
        # type is the string "int", and the check would fail on a count.
        hints = typing.get_type_hints(counts)
        for field in dataclasses.fields(counts):
            assert hints[field.name] is not_counts.get(field.name, int), field.name

    graded = {"percent", "percentage", "pct", "share", "fraction", "proportion", "out_of"}
    graded |= {"rate", "ratio", "accuracy", "precision", "recall", "f1", "rank", "ranking"}
    graded |= {"grade", "mark", "best", "winner", "quality", "correlation"}
    for name in _names_agreement_defines():
        # Whole words, split on "_" and at a capital: "separate" is not a
        # rate, and "found_share" or "TagShare" is a share.
        words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower().split("_")
        words += [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]
        said = {word for word in words if word in graded or word.removesuffix("s") in graded}
        assert not said, f"{name}: {sorted(said)}"


def _names_agreement_defines() -> list[str]:
    """Every name the module itself defines — functions, classes, constants,
    and the fields and methods of its classes — read from its source, so
    what it merely imports (`median`) is not held to the rule and nothing it
    adds can slip past by being a function rather than a field."""
    tree = ast.parse(Path(agreement.__file__).read_text(encoding="utf-8"))
    names: list[str] = []

    def collect(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
                if isinstance(node, ast.ClassDef):
                    collect(node.body)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.append(node.target.id)
            elif isinstance(node, ast.Assign):
                names.extend(t.id for t in node.targets if isinstance(t, ast.Name))

    collect(tree.body)
    assert {"TagAgreement", "yes_found", "FLOORS", "spread"} <= set(names), "the walk found it all"
    return names


def test_the_counting_needs_nothing_but_the_standard_library() -> None:
    """It is arithmetic. It must stay importable, and checkable, with no
    database, no model manager and nothing installed."""
    tree = ast.parse(Path(agreement.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import reaches into the package"
            imported.add((node.module or "").split(".")[0])
    assert imported <= sys.stdlib_module_names, imported - sys.stdlib_module_names
