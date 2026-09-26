"""Showcase ranking: finished work over work in progress, one photo per place,
print fitness, no people, no photo offered twice."""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

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


def _scene(sky_rows: int, colour: tuple[int, int, int] = (110, 160, 235)) -> Image.Image:
    """Textured grass under `sky_rows` rows (of 80) of smooth sky."""
    rng = np.random.default_rng(7)
    grass = np.stack(
        [
            rng.integers(30, 110, (80, 120)),
            rng.integers(40, 250, (80, 120)),
            rng.integers(20, 80, (80, 120)),
        ],
        axis=-1,
    ).astype(np.uint8)
    for row in range(sky_rows):
        lift = row * 40 // max(sky_rows, 1)  # a gentle gradient, as a real sky has
        grass[row] = [min(255, c + lift) for c in colour]
    return Image.fromarray(grass)


def test_sky_is_measured_from_the_top_edge() -> None:
    assert abs(showcase.sky_fraction(_scene(24)) - 0.3) < 0.05
    assert abs(showcase.sky_fraction(_scene(24, (200, 202, 205))) - 0.3) < 0.05, "overcast counts"
    assert showcase.sky_fraction(_scene(0)) < 0.01, "a frame of grass has none"
    # A blank white frame is "all sky" and earns nothing for it.
    blank = showcase.sky_fraction(Image.new("RGB", (120, 80), (245, 245, 245)))
    assert blank > 0.9 and showcase.sky_presence(blank) == 0.0
    assert showcase.sky_presence(0.01) == 0.0, "a sliver above the trees is not a sky"
    assert showcase.sky_presence(0.3) == 1.0


def test_sky_breaks_a_tie_but_is_never_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """The best pages usually have a sky — not always."""
    skies = {"horizon.webp": 0.25, "overhead.webp": 0.0, "nadir.webp": 0.0}
    monkeypatch.setattr(showcase, "look", lambda path: (1.0, skies[Path(path).name]))
    photos = _library()
    photos += [
        # The same quality and finish, different frames.
        _photo(
            500,
            "Edgewood/drone/overhead.JPG",
            _unit(1, 0, 1, 0, 0, 1, 0.5, 0),
            thumbnail="overhead.webp",
        ),
        _photo(
            501,
            "Edgewood/drone/horizon.JPG",
            _unit(1, 0, 1, 0, 0, 1, 0, 0.5),
            thumbnail="horizon.webp",
        ),
        _photo(
            502,
            "North Fork/drone/nadir.JPG",
            _unit(1, 0, 1, 0, 0, 1, -0.5, -0.5),
            thumbnail="nadir.webp",
        ),
    ]
    places, _ = showcase.rank(photos, VECTORS, alternates=2, thumbnail_root=Path("/thumbs"))
    leaders = {place.label: place.picks[0] for place in places}
    assert leaders["Edgewood"].photo.asset_id == "a501"
    assert leaders["Edgewood"].parts["sky"] == 0.25
    assert leaders["North Fork"].photo.asset_id == "a502", "no sky, still the best of its place"


def test_machines_on_the_grass_are_left_out() -> None:
    """Seen from a drone, a crew is tractors and a truck on the fairway."""
    vectors = {
        **VECTORS,
        "equipment_pos": [_unit(0, 0, 0, 0, 0, 0, 1)],
        "equipment_neg": [_unit(0, 0, 0, 0, 0, 1)],
    }
    photos = _library()
    photos += [
        _photo(600, "Oak Hill/a.JPG", _unit(1, 0, 1, 0, 0, 1)),
        _photo(601, "Oak Hill/b.JPG", _unit(1, 0, 1, 0, 0, 0.2, 1)),  # tractors on the green
    ]
    places, _ = showcase.rank(photos, vectors, alternates=3)
    offered = {pick.photo.asset_id for place in places for pick in place.picks}
    assert "a600" in offered and "a601" not in offered
