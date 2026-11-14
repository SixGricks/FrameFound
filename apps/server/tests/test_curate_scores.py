"""Culling with a local model's use score (media/curate.py, L3 D17).

The score is an opinion added to the measurements: it may keep the
better-scored of a duplicate group and offer a frame it rated far below
the shoot, always inside the two guards that already hold — a room never
loses its last photograph, and at most 40 % of a listing is offered.
Without scores, every answer is today's.
"""

from typing import Any

from framefound.media import curate


def _vec(*values: float) -> list[float]:
    vector = [0.0] * 512
    for i, value in enumerate(values):
        vector[i] = value
    return vector


SAME = _vec(1.0)
TWIN = _vec(0.999, 0.04)


def _photo(
    item_id: str, room: str, sharpness: float, embedding: list[float] | None = None, **extra: Any
) -> dict[str, Any]:
    return {"id": item_id, "room": room, "sharpness": sharpness, "embedding": embedding, **extra}


def _shoot() -> list[dict[str, Any]]:
    """A kitchen pair, a soft bedroom frame among sharp ones, a lone barn."""
    return [
        _photo("k1", "kitchen", 9.0, SAME),
        _photo("k2", "kitchen", 4.0, TWIN),
        _photo("k3", "kitchen", 8.0),
        _photo("b1", "bedroom", 9.0),
        _photo("b2", "bedroom", 1.0),
        _photo("b3", "bedroom", 8.5),
        _photo("barn", "barn", 0.5),
        _photo("f1", "front", 9.5),
        _photo("f2", "front", 9.0),
        _photo("f3", "front", 8.8),
    ]


def test_culling_is_unchanged_without_scores() -> None:
    """No score key, or every score None (no model has looked): the same
    suggestions, in the same order, with the same words, as before."""
    before = curate.suggest_removals(_shoot())
    unscored = [{**item, "score": None, "score_reason": None} for item in _shoot()]
    assert curate.suggest_removals(unscored) == before
    assert [(s["id"], s["reason"], s["keep_instead"]) for s in before] == [
        ("k2", curate.DUPLICATE_SOFTER, "k1"),
        ("b2", "markedly softer than the rest of the shoot", None),
    ]


def test_the_better_scored_duplicate_is_kept() -> None:
    """The model clearly preferred the softer twin (80 against 50): it stays
    and the sharper one is offered, with words that say whose preference."""
    items = [
        _photo("k1", "kitchen", 9.0, SAME, score=50, score_reason="a door frame cuts the counter"),
        _photo("k2", "kitchen", 6.0, TWIN, score=80, score_reason="the whole island in light"),
        _photo("k3", "kitchen", 8.0, score=70),
    ]
    [suggestion] = curate.suggest_removals(items)
    assert suggestion == {"id": "k1", "reason": curate.DUPLICATE_PREFERRED, "keep_instead": "k2"}


def test_a_small_lead_or_an_unscored_twin_leaves_the_sharpest_kept() -> None:
    """Five points is noise in an uncalibrated model, and a twin nobody
    looked at cannot be compared: sharpness decides, in today's words."""
    close = [
        _photo("k1", "kitchen", 9.0, SAME, score=70),
        _photo("k2", "kitchen", 4.0, TWIN, score=75),
        _photo("k3", "kitchen", 8.0, score=72),
    ]
    half = [
        _photo("k1", "kitchen", 9.0, SAME),
        _photo("k2", "kitchen", 4.0, TWIN, score=95),
        _photo("k3", "kitchen", 8.0, score=72),
    ]
    for items in (close, half):
        [suggestion] = curate.suggest_removals(items)
        assert suggestion == {"id": "k2", "reason": curate.DUPLICATE_SOFTER, "keep_instead": "k1"}


def test_a_frame_the_model_scored_far_below_the_shoot_is_offered_with_its_reason() -> None:
    items = [
        _photo("l1", "living", 9.0, score=82),
        _photo("l2", "living", 8.8, score=78),
        _photo("l3", "living", 8.9, score=20, score_reason="  the TV glare\nfills the frame "),
        _photo("l4", "living", 9.1, score=80),
        _photo("l5", "living", 9.2, score=36),  # under 40, and 42 below the median of 78
    ]
    out = curate.suggest_removals(items)
    by_id = {s["id"]: s for s in out}
    assert by_id["l3"] == {
        "id": "l3",
        "reason": "the local model scored it 20: the TV glare fills the frame",
        "keep_instead": None,
    }
    assert by_id["l5"]["reason"] == "the local model scored it 36"
    assert set(by_id) == {"l3", "l5"}


def test_a_shoot_the_model_disliked_throughout_loses_nothing_for_it() -> None:
    """Every frame under 40, but none far below the shoot's own median."""
    items = [_photo(f"p{i}", "living", 9.0, score=30 + i) for i in range(6)]
    assert curate.suggest_removals(items) == []


def test_a_low_score_never_takes_a_rooms_last_photograph() -> None:
    items = [
        _photo("barn", "barn", 9.0, score=5, score_reason="mostly sky"),
        *(_photo(f"k{i}", "kitchen", 9.0, score=85) for i in range(4)),
    ]
    assert all(s["id"] != "barn" for s in curate.suggest_removals(items)), (
        "a poorly scored photo of the only barn is still the only barn"
    )


def test_the_cap_holds_whatever_the_reason() -> None:
    """Twelve photographs, five scored far down: at most four offered."""
    items = [
        *(_photo(f"good{i}", "front", 9.0, score=90) for i in range(7)),
        *(_photo(f"poor{i}", "front", 9.0, score=10) for i in range(5)),
    ]
    out = curate.suggest_removals(items)
    assert len(out) == int(len(items) * curate.MAX_SUGGEST_FRACTION) == 4
    assert all(s["id"].startswith("poor") for s in out)


def test_a_score_does_not_reclaim_a_frame_already_offered() -> None:
    """A soft frame the model also disliked is offered once, for softness."""
    items = [
        _photo("b1", "bedroom", 9.0, score=80),
        _photo("b2", "bedroom", 1.0, score=10),
        _photo("b3", "bedroom", 8.5, score=82),
        _photo("b4", "bedroom", 8.7, score=79),
    ]
    out = curate.suggest_removals(items)
    assert [(s["id"], s["reason"]) for s in out] == [
        ("b2", "markedly softer than the rest of the shoot")
    ]


def test_a_twin_kept_for_its_score_is_not_then_offered_as_soft() -> None:
    """The model preferred the softer twin (k2, 80 against 50). The kitchen
    has a third photograph, so the room guard would let k2 go too; it must
    not be offered as soft beside a suggestion that says to keep it."""
    items = [
        _photo("k1", "kitchen", 9.0, SAME, score=50),
        _photo("k2", "kitchen", 3.0, TWIN, score=80),
        _photo("k3", "kitchen", 8.0),
        _photo("b1", "bedroom", 9.0),
        _photo("b2", "bedroom", 8.5),
        _photo("f1", "front", 9.0),
    ]
    found = curate.suggest_removals(items)
    assert [(s["id"], s["reason"], s["keep_instead"]) for s in found] == [
        ("k1", curate.DUPLICATE_PREFERRED, "k2"),
    ]


def test_a_twin_kept_for_its_score_is_not_then_offered_for_its_low_score() -> None:
    """Both twins scored low (35 and 10) in a shoot scored about 80: the 35
    is kept over the 10, and is not then offered for scoring 35 — accepting
    both suggestions would lose the kitchen's view."""
    items = [
        _photo("k1", "kitchen", 9.0, SAME, score=35),
        _photo("k2", "kitchen", 9.0, TWIN, score=10),
        _photo("k3", "kitchen", 9.0, score=80),
        _photo("k4", "kitchen", 9.0, score=82),
        _photo("b1", "bedroom", 9.0, score=80),
        _photo("b2", "bedroom", 9.0, score=78),
        _photo("f1", "front", 9.0, score=85),
        _photo("f2", "front", 9.0, score=80),
    ]
    found = curate.suggest_removals(items)
    assert [(s["id"], s["keep_instead"]) for s in found] == [("k2", "k1")]
    assert all(s["id"] != s2["keep_instead"] for s in found for s2 in found)
