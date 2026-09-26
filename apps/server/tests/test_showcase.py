"""Showcase ranking: finished work over work in progress, one photo per place,
print fitness, no people, no photo offered twice."""

import numpy as np

from framefound.media import showcase


def _unit(*weights: float) -> list[float]:
    vector = np.zeros(8)
    vector[: len(weights)] = weights
    return (vector / np.linalg.norm(vector)).tolist()


# Axes: 0 = beautiful, 1 = ugly, 2 = finished, 3 = construction, 4 = person, 5 = empty
VECTORS = {
    "quality_pos": [_unit(1)],
    "quality_neg": [_unit(0, 1)],
    "finish_pos": [_unit(0, 0, 1)],
    "finish_neg": [_unit(0, 0, 0, 1)],
    "people_pos": [_unit(0, 0, 0, 0, 1)],
    "people_neg": [_unit(0, 0, 0, 0, 0, 1)],
}


def _photo(n: int, path: str, embedding: list[float], **kw: object) -> showcase.Photo:
    defaults: dict = {"width": 6000, "height": 4000}
    defaults.update(kw)
    return showcase.Photo(
        asset_id=f"a{n}",
        relative_path=path,
        filename=path.rsplit("/", 1)[-1],
        embedding=embedding,
        **defaults,
    )


def _library() -> list[showcase.Photo]:
    """Four courses. Filler photos (construction) make up most of each, as
    in a construction company's library, so a finished shot is rare."""
    photos = []
    n = 0
    for course in ("Edgewood", "North Fork", "Pete Dye", "Oak Hill"):
        for _ in range(6):
            n += 1
            photos.append(_photo(n, f"{course}/work/IMG_{n}.JPG", _unit(0.2, 0.5, 0.1, 1, 0, 1)))
    return photos


def test_the_finished_masterpiece_leads_and_each_place_appears_once() -> None:
    photos = _library()
    photos.append(_photo(100, "Edgewood/2023 drone/DJI_1.JPG", _unit(1, 0, 1, 0, 0, 1)))
    photos.append(_photo(101, "Edgewood/2023 drone/DJI_2.JPG", _unit(0.9, 0.1, 0.9, 0.1, 0, 1)))
    photos.append(_photo(102, "North Fork/drone/DJI_9.JPG", _unit(0.8, 0.1, 0.9, 0.1, 0, 1)))
    places, considered = showcase.rank(photos, VECTORS, count=10, alternates=2)
    assert considered == len(photos)
    keys = [p.key for p in places]
    assert len(keys) == len(set(keys)), "one entry per place"
    assert places[0].label == "Edgewood"
    assert places[0].picks[0].photo.asset_id == "a100"
    offered = [pick.photo.asset_id for place in places for pick in place.picks]
    assert not any(a.startswith("a") and int(a[1:]) < 100 for a in offered), (
        "construction photos are below the finish floor"
    )


def test_print_fitness_filters_small_portrait_and_panoramas() -> None:
    photos = _library()
    good = _unit(1, 0, 1, 0, 0, 1)
    photos += [
        _photo(200, "Oak Hill/a.JPG", good, width=2000, height=1500),  # 3 MP
        _photo(201, "Oak Hill/b.JPG", good, width=4000, height=6000),  # portrait
        _photo(202, "Oak Hill/c.JPG", good, width=12000, height=6000),  # 2:1 sphere
    ]
    places, _ = showcase.rank(photos, VECTORS, min_megapixels=12)
    offered = {pick.photo.asset_id for place in places for pick in place.picks}
    assert offered.isdisjoint({"a200", "a201", "a202"})
    places, _ = showcase.rank(photos, VECTORS, orientation="portrait", min_megapixels=12)
    assert "a201" in {pick.photo.asset_id for place in places for pick in place.picks}


def test_people_are_left_out_unless_allowed() -> None:
    photos = _library()
    posed = _unit(1, 0, 1, 0, 1, 0)  # finished and lovely, with someone in it
    photos.append(_photo(300, "Pete Dye/x.JPG", posed))
    photos.append(_photo(301, "Pete Dye/y.JPG", _unit(1, 0, 1, 0, 0, 1), faces=2))
    places, _ = showcase.rank(photos, VECTORS)
    offered = {pick.photo.asset_id for place in places for pick in place.picks}
    assert offered.isdisjoint({"a300", "a301"})
    places, _ = showcase.rank(photos, VECTORS, allow_people=True)
    offered = {pick.photo.asset_id for place in places for pick in place.picks}
    assert {"a300", "a301"} <= offered


def test_a_copy_in_a_social_folder_is_placed_by_gps_and_never_offered_twice() -> None:
    """GELCO files other courses' best shots under LuLu/Social images/…:
    the folder says nothing about where the photo was taken."""
    shot = _unit(1, 0, 1, 0, 0, 1)
    north_fork = (40.95, -72.55)
    photos = _library()
    photos += [
        _photo(400, "North Fork/2023 drone/DJI_16.JPG", shot, gps=north_fork),
        _photo(401, "LuLu/Social images/NORTHFORK/DJI_16.jpg", shot, gps=north_fork),
        _photo(402, "LuLu/Social images/nogps.jpg", _unit(1, 0, 1, 0, 0, 1)),
    ]
    places, _ = showcase.rank(photos, VECTORS, alternates=3)
    where = {pick.photo.asset_id: place.label for place in places for pick in place.picks}
    assert where.get("a400") == "North Fork" or where.get("a401") == "North Fork"
    assert not ("a400" in where and "a401" in where), "the same frame is offered once"
    assert "a402" not in where, "a copy with no GPS in a collection folder has no place"


def test_place_names_merge_on_noise_and_gps() -> None:
    assert showcase.place_of("Forest Gate NJ/x.jpg")[0] == showcase.place_of("Forest gate/y.jpg")[0]
    assert showcase.place_of("Town Of Colony/a.jpg")[0] == showcase.place_of("Town of Colonie/b")[0]
    assert showcase.is_generic("LuLu/ Social images/NORTHFORK/DJI.jpg")
    assert not showcase.is_generic("LuLu/2023 11 09/DJI.jpg")
