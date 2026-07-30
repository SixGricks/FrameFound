"""Face clustering and per-person thresholds.

The failure that matters here is not a missed face — it is one person's
photograph appearing in another person's album. Every threshold decision below
leans that way on purpose.
"""

import math

from framefound.ai.people import (
    DEFAULT_THRESHOLD,
    MAX_THRESHOLD,
    MIN_THRESHOLD,
    FaceVector,
    cluster_faces,
    prototype_for,
    similarity,
    threshold_for,
)


def _vec(angle: float, dims: int = 512) -> list[float]:
    """A unit vector rotated by `angle`, so cosine against angle=0 is cos(angle)."""
    vector = [0.0] * dims
    vector[0] = math.cos(angle)
    vector[1] = math.sin(angle)
    return vector


def _face(name: str, angle: float) -> FaceVector:
    return FaceVector(face_id=name, embedding=_vec(angle), asset_id=f"asset-{name}")


def test_the_same_person_across_shots_becomes_one_cluster() -> None:
    faces = [_face(f"f{i}", 0.05 * i) for i in range(5)]
    clusters = cluster_faces(faces)
    assert len(clusters) == 1
    assert len(clusters[0].members) == 5


def test_two_different_people_stay_apart() -> None:
    faces = [_face("a1", 0.0), _face("a2", 0.05), _face("b1", 1.4), _face("b2", 1.45)]
    clusters = cluster_faces(faces)
    assert len(clusters) == 2
    assert sorted(len(c.members) for c in clusters) == [2, 2]


def test_clusters_come_back_largest_first() -> None:
    faces = [_face("a1", 0.0), _face("a2", 0.03), _face("a3", 0.06), _face("b1", 1.4)]
    clusters = cluster_faces(faces)
    assert [len(c.members) for c in clusters] == [3, 1]


def test_the_centroid_moves_toward_its_members() -> None:
    faces = [_face("a", 0.0), _face("b", 0.3)]
    cluster = cluster_faces(faces)[0]
    # Between the two, not pinned to whichever arrived first.
    assert similarity(cluster.centroid, _vec(0.15)) > 0.0
    assert similarity(cluster.centroid, _vec(0.15)) > similarity(cluster.centroid, _vec(0.9))


def test_a_prototype_is_renormalised() -> None:
    """The mean of unit vectors is shorter than one. Leaving it that way would
    depress every future similarity and make the person harder to match."""
    proto = prototype_for([_vec(0.0), _vec(0.4)])
    assert proto is not None
    assert math.isclose(sum(v * v for v in proto) ** 0.5, 1.0, abs_tol=1e-6)


def test_a_prototype_of_nothing_is_nothing() -> None:
    assert prototype_for([]) is None
    assert prototype_for([[]]) is None


def test_a_new_person_uses_the_default_bar() -> None:
    assert threshold_for(None, [], []) == DEFAULT_THRESHOLD
    assert threshold_for(_vec(0.0), [], []) == DEFAULT_THRESHOLD


def test_the_bar_drops_to_admit_a_confirmed_face() -> None:
    """A face the operator said yes to must keep matching next time."""
    proto = _vec(0.0)
    weak = _vec(1.0)  # cos(1.0) ~ 0.54
    bar = threshold_for(proto, [weak], [])
    assert bar < similarity(proto, weak)


def test_the_bar_rises_to_exclude_a_rejected_face() -> None:
    proto = _vec(0.0)
    wrong = _vec(1.0)  # cos(1.0) ~ 0.54, inside the clampable range
    bar = threshold_for(proto, [], [wrong])
    assert bar > similarity(proto, wrong)


def test_a_very_similar_rejection_pins_the_bar_at_the_ceiling() -> None:
    """The threshold alone cannot exclude a face more similar than
    MAX_THRESHOLD — clamping stops it. That is deliberate: a bar above 0.75
    would stop matching the person at all. The guarantee that a rejected face
    is never re-offered comes from the explicit rejection record, not from the
    threshold, exactly as it does for tags."""
    proto = _vec(0.0)
    twin = _vec(0.4)  # ~0.92, closer than the ceiling allows
    assert threshold_for(proto, [], [twin]) == MAX_THRESHOLD


def test_a_clean_gap_puts_the_bar_in_the_middle() -> None:
    proto = _vec(0.0)
    confirmed = _vec(0.6)  # ~0.825
    rejected = _vec(1.2)  # ~0.362
    bar = threshold_for(proto, [confirmed], [rejected])
    assert similarity(proto, rejected) < bar < similarity(proto, confirmed)


def test_when_confirmed_and_rejected_overlap_the_rejection_wins() -> None:
    """Siblings. Offering the wrong person is the failure that loses trust, so
    the rejection is honoured even at the cost of re-confirming the other."""
    proto = _vec(0.0)
    confirmed = _vec(1.1)  # ~0.454
    rejected = _vec(0.8)  # ~0.697 — more similar than the confirmed one
    bar = threshold_for(proto, [confirmed], [rejected])
    assert bar > similarity(proto, rejected)


def test_the_bar_is_never_loose_enough_to_stop_distinguishing_people() -> None:
    proto = _vec(0.0)
    # A confirmed face at near-90 degrees would imply a threshold near zero.
    assert threshold_for(proto, [_vec(1.55)], []) >= MIN_THRESHOLD


def test_the_bar_is_never_so_tight_that_nothing_matches() -> None:
    proto = _vec(0.0)
    assert threshold_for(proto, [], [_vec(0.01)]) <= MAX_THRESHOLD


def test_a_stricter_threshold_splits_a_borderline_cluster() -> None:
    faces = [_face("a", 0.0), _face("b", 1.1)]
    assert len(cluster_faces(faces, threshold=0.3)) == 1
    assert len(cluster_faces(faces, threshold=0.7)) == 2


def test_clustering_nothing_returns_nothing() -> None:
    assert cluster_faces([]) == []
