"""Slideshow selection.

Selection is the whole feature: the right forty photographs out of four
hundred, nobody left out, nothing shown twice. These tests pin the promises
that a church or a family would actually notice being broken.
"""

import math
from datetime import UTC, datetime, timedelta

from framefound.media.slideshow import (
    Candidate,
    collapse_near_duplicates,
    select,
)
from framefound.media.theming import get_theme, score_against_theme

BASE = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)


def _vec(angle: float) -> list[float]:
    vector = [0.0] * 512
    vector[0], vector[1] = math.cos(angle), math.sin(angle)
    return vector


def _cand(
    name: str,
    *,
    minutes: int = 0,
    angle: float = 0.0,
    theme: float = 0.0,
    sharp: float = 1.0,
    people: list[str] | None = None,
) -> Candidate:
    return Candidate(
        asset_id=name,
        captured_at=BASE + timedelta(minutes=minutes),
        embedding=_vec(angle),
        theme_score=theme,
        sharpness=sharp,
        person_ids=people or [],
    )


def test_a_burst_collapses_to_one_frame() -> None:
    """Three shots in case someone blinked are one moment, not three."""
    burst = [_cand(f"b{i}", minutes=i, angle=0.01 * i) for i in range(3)]
    kept, dropped = collapse_near_duplicates(burst)
    assert len(kept) == 1
    assert dropped == 2


def test_the_sharpest_frame_of_a_burst_survives() -> None:
    burst = [
        _cand("soft", angle=0.0, sharp=0.4),
        _cand("sharp", angle=0.005, sharp=0.95),
    ]
    kept, _ = collapse_near_duplicates(burst)
    assert kept[0].asset_id == "sharp"


def test_the_same_person_an_hour_apart_is_two_moments() -> None:
    """Collapsing on appearance alone would delete half an event."""
    pair = [_cand("morning", minutes=0, angle=0.0), _cand("afternoon", minutes=180, angle=1.2)]
    kept, dropped = collapse_near_duplicates(pair)
    assert len(kept) == 2
    assert dropped == 0


def test_everyone_required_gets_into_the_slideshow() -> None:
    """The reason a family asks for this at all. A purely chronological cut
    misses the quiet child who was only photographed twice."""
    crowd = [_cand(f"c{i}", minutes=i * 5, angle=0.3 * i, sharp=0.9) for i in range(10)]
    rare = _cand("rare", minutes=200, angle=5.0, sharp=0.2, people=["quiet-kid"])
    result = select([*crowd, rare], target_count=4, required_people=["quiet-kid"])

    assert "rare" in [c.asset_id for c in result.chosen]
    assert result.people_covered == ["quiet-kid"]
    assert result.people_missing == []


def test_a_person_with_no_photographs_is_reported_not_hidden() -> None:
    result = select([_cand("a", angle=0.0)], target_count=3, required_people=["never-photographed"])
    assert result.people_missing == ["never-photographed"]


def test_the_output_is_chronological_even_when_chosen_on_merit() -> None:
    """Choose by fit, then tell the day in order. Picking chronologically first
    would mean the theme only ever influenced the tail."""
    candidates = [
        _cand("late", minutes=300, angle=0.0, theme=0.9),
        _cand("early", minutes=10, angle=1.0, theme=0.8),
        _cand("middle", minutes=120, angle=2.0, theme=0.7),
    ]
    result = select(candidates, target_count=3, themed=True)
    assert [c.asset_id for c in result.chosen] == ["early", "middle", "late"]


def test_themed_selection_prefers_photos_that_fit() -> None:
    good = _cand("jungle", minutes=0, angle=0.0, theme=0.62)
    weak = _cand("carpark", minutes=10, angle=1.0, theme=0.04)
    result = select([good, weak], target_count=1, themed=True)
    assert [c.asset_id for c in result.chosen] == ["jungle"]


def test_a_thin_day_still_fills_the_slideshow() -> None:
    """Low-scoring frames are pushed back, not removed. A small event should
    still produce a slideshow rather than four photos."""
    weak = [_cand(f"w{i}", minutes=i * 30, angle=i * 0.8, theme=0.01) for i in range(5)]
    result = select(weak, target_count=5, themed=True)
    assert len(result.chosen) == 5


def test_undated_photos_trail_rather_than_leading() -> None:
    # An unknown date is not the same as the earliest date.
    dated = _cand("dated", minutes=60, angle=0.0)
    undated = Candidate(asset_id="undated", captured_at=None, embedding=_vec(1.5))
    result = select([undated, dated], target_count=2)
    assert [c.asset_id for c in result.chosen] == ["dated", "undated"]


def test_selection_is_bounded_by_the_target() -> None:
    many = [_cand(f"m{i}", minutes=i * 7, angle=i * 0.7) for i in range(30)]
    assert len(select(many, target_count=8).chosen) == 8


def test_selecting_from_nothing_returns_nothing() -> None:
    result = select([], target_count=10, required_people=["someone"])
    assert result.chosen == []
    assert result.people_missing == ["someone"]


def test_the_rainforest_theme_exists_and_carries_prompts() -> None:
    theme = get_theme("rainforest")
    assert theme.label.startswith("Rainforest")
    assert any("waterfall" in p for p in theme.prompts)
    assert theme.negative_prompts, "a theme needs things to push down too"


def test_an_unknown_theme_falls_back_to_plain_rather_than_failing() -> None:
    assert get_theme("no-such-theme").slug == "plain"


def test_the_best_prompt_wins_rather_than_the_average() -> None:
    """A waterfall photo should score on 'a waterfall in a jungle' even though
    it looks nothing like 'children playing in a jungle themed room'.
    Averaging would punish every photo for not matching all of them."""
    frame = _vec(0.0)
    prompts = [_vec(0.0), _vec(1.5)]  # one close, one far
    assert score_against_theme(frame, prompts, []) > 0.9


def test_negatives_nudge_rather_than_veto() -> None:
    frame = _vec(0.0)
    # Equally close to a positive and a negative: still positive overall,
    # because a photo of the car park on the day is still from the day.
    scored = score_against_theme(frame, [_vec(0.0)], [_vec(0.0)])
    assert 0 < scored < 1


def test_a_frame_with_no_embedding_scores_zero_rather_than_crashing() -> None:
    assert score_against_theme(None, [_vec(0.0)], []) == 0.0
