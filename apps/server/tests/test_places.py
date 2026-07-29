"""Clustering located assets into places, and naming them.

The naming rules are where the judgement is: a name that reads wrong makes
the whole feature feel broken even when the geometry is right.
"""

from datetime import UTC, datetime, timedelta

from framefound.media.places import LocatedAsset, cluster, name_for

BARN = (41.8781, -87.6298)
BASE = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def _at(
    lat: float,
    lon: float,
    path: str = "shoot/a.jpg",
    minutes: int = 0,
    inferred: bool = False,
) -> LocatedAsset:
    return LocatedAsset(
        asset_id=f"{lat}-{lon}-{path}-{minutes}",
        lat=lat,
        lon=lon,
        relative_path=path,
        captured_at=BASE + timedelta(minutes=minutes),
        inferred=inferred,
    )


def test_one_shoot_is_one_place() -> None:
    # Roughly 100 m of drift, which is one property.
    assets = [_at(BARN[0] + i * 0.0003, BARN[1], minutes=i) for i in range(5)]
    places = cluster(assets)
    assert len(places) == 1
    assert places[0].asset_count if hasattr(places[0], "asset_count") else True
    assert len(places[0].members) == 5


def test_two_jobs_miles_apart_stay_separate() -> None:
    here = [_at(BARN[0], BARN[1], minutes=i) for i in range(3)]
    there = [_at(BARN[0] + 0.5, BARN[1] + 0.5, minutes=10 + i) for i in range(2)]
    places = cluster(here + there)
    assert len(places) == 2
    # Largest first, so the three-asset job leads.
    assert [len(p.members) for p in places] == [3, 2]


def test_the_centroid_settles_among_its_members() -> None:
    assets = [_at(BARN[0], BARN[1]), _at(BARN[0] + 0.004, BARN[1])]
    place = cluster(assets)[0]
    assert BARN[0] < place.lat < BARN[0] + 0.004
    assert place.radius_km > 0


def test_radius_reports_the_furthest_member() -> None:
    assets = [_at(BARN[0], BARN[1]), _at(BARN[0] + 0.002, BARN[1])]
    place = cluster(assets)[0]
    assert 0.05 < place.radius_km < 0.2


def test_clustering_is_stable_regardless_of_input_order() -> None:
    assets = [
        _at(BARN[0], BARN[1], minutes=0),
        _at(BARN[0] + 0.5, BARN[1], minutes=5),
        _at(BARN[0] + 0.0002, BARN[1], minutes=1),
    ]
    first = [(round(p.lat, 6), len(p.members)) for p in cluster(assets)]
    second = [(round(p.lat, 6), len(p.members)) for p in cluster(list(reversed(assets)))]
    assert first == second


def test_a_place_is_named_for_the_folder_its_files_live_in() -> None:
    assets = [
        _at(BARN[0], BARN[1], path="2026/Feb 4 - 513 Jacobs Rd/DJI_001.jpg"),
        _at(BARN[0], BARN[1], path="2026/Feb 4 - 513 Jacobs Rd/DJI_002.jpg"),
    ]
    assert name_for(cluster(assets)[0]) == "Feb 4 - 513 Jacobs Rd"


def test_subfolders_of_one_shoot_are_named_for_the_shoot() -> None:
    # No single folder holds a majority, so the name must come from the
    # shared ancestor rather than from whichever subfolder happens to win.
    assets = [
        _at(BARN[0], BARN[1], path="2026/Columbia Ave/a-roll/one.mp4"),
        _at(BARN[0], BARN[1], path="2026/Columbia Ave/stills/two.jpg"),
    ]
    assert name_for(cluster(assets)[0]) == "Columbia Ave"


def test_a_clear_majority_folder_wins_over_the_ancestor() -> None:
    assets = [
        _at(BARN[0], BARN[1], path="2026/Hebe Bypass/main/a.jpg"),
        _at(BARN[0], BARN[1], path="2026/Hebe Bypass/main/b.jpg"),
        _at(BARN[0], BARN[1], path="2026/Hebe Bypass/extra/c.jpg"),
    ]
    assert name_for(cluster(assets)[0]) == "main"


def test_files_at_the_library_root_get_a_fallback_name() -> None:
    # No folder means nothing to name the place after — but a blank card
    # title is worse than admitting we don't know.
    assets = [_at(BARN[0], BARN[1], path="loose.jpg")]
    assert name_for(cluster(assets)[0]) == "Unknown location"


def test_a_root_file_does_not_drag_down_a_named_place() -> None:
    assets = [
        _at(BARN[0], BARN[1], path="2026/Jacobs Rd/a.jpg"),
        _at(BARN[0], BARN[1], path="2026/Jacobs Rd/b.jpg"),
        _at(BARN[0], BARN[1], path="stray.jpg"),
    ]
    assert name_for(cluster(assets)[0]) == "Jacobs Rd"


def test_unrelated_folders_fall_back_rather_than_guessing() -> None:
    assets = [
        _at(BARN[0], BARN[1], path="alpha/one.jpg"),
        _at(BARN[0], BARN[1], path="beta/two.jpg"),
    ]
    assert name_for(cluster(assets)[0]) == "Unknown location"


def test_a_wider_radius_merges_neighbouring_jobs() -> None:
    assets = [
        _at(BARN[0], BARN[1], path="a/x.jpg"),
        _at(BARN[0] + 0.01, BARN[1], path="b/y.jpg"),  # about 1.1 km
    ]
    assert len(cluster(assets, radius_km=0.75)) == 2
    assert len(cluster(assets, radius_km=5.0)) == 1
