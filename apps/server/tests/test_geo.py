"""Distance maths and the two-signal location inference."""

from datetime import UTC, datetime, timedelta

from framefound.media.geo import (
    LocationCandidate,
    best_candidate,
    bounding_box,
    confidence_for,
    haversine_km,
)

LANCASTER = (40.0379, -76.3055)
PHILADELPHIA = (39.9526, -75.1652)
NOON = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def candidate(minutes: float, similarity: float) -> LocationCandidate:
    return LocationCandidate(
        asset_id="a",
        lat=LANCASTER[0],
        lon=LANCASTER[1],
        captured_at=NOON + timedelta(minutes=minutes),
        similarity=similarity,
    )


def test_haversine_matches_known_distance() -> None:
    km = haversine_km(*LANCASTER, *PHILADELPHIA)
    assert 95 < km < 110  # Lancaster to Philadelphia is ~100 km


def test_haversine_zero_for_same_point() -> None:
    assert haversine_km(*LANCASTER, *LANCASTER) == 0.0


def test_bounding_box_contains_the_circle() -> None:
    min_lat, max_lat, min_lon, max_lon = bounding_box(*LANCASTER, radius_km=10)
    assert min_lat < LANCASTER[0] < max_lat
    assert min_lon < LANCASTER[1] < max_lon
    # A point 9 km due north is inside the circle, so must be inside the box.
    assert min_lat <= LANCASTER[0] + 9 / 111.32 <= max_lat


def test_close_in_time_and_visually_similar_is_confident() -> None:
    assert confidence_for(time_gap_s=120, similarity=0.9) > 0.8


def test_similar_but_hours_apart_is_rejected() -> None:
    # Same subject, different day's shoot — must not borrow the location.
    assert confidence_for(time_gap_s=5 * 3600, similarity=0.95) == 0.0


def test_simultaneous_but_visually_unrelated_is_rejected() -> None:
    # Two crews working at once in different places.
    assert confidence_for(time_gap_s=30, similarity=0.2) == 0.0


def test_best_candidate_prefers_the_strongest_signal() -> None:
    weak = LocationCandidate("weak", 1.0, 1.0, NOON + timedelta(minutes=90), 0.6)
    strong = LocationCandidate("strong", 2.0, 2.0, NOON + timedelta(minutes=2), 0.95)
    result = best_candidate(NOON, [weak, strong])
    assert result is not None
    assert result[0].asset_id == "strong"


def test_best_candidate_returns_none_when_nothing_convinces() -> None:
    assert best_candidate(NOON, [candidate(minutes=300, similarity=0.9)]) is None
    assert best_candidate(NOON, []) is None


def test_confidence_is_bounded() -> None:
    assert 0.0 <= confidence_for(0, 1.0) <= 1.0
