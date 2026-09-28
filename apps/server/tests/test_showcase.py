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


def test_options_stop_at_the_floor_but_a_place_always_keeps_its_best(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twenty options for a course with four good photographs would be
    sixteen of the work; a thin place stays short instead."""
    photos = _library()
    # Four distinct finished frames of one course (cosines well under the
    # near-duplicate line), of differing quality.
    for n, (q, a, b) in enumerate([(1.0, 0.8, 0), (0.9, 0, 0.8), (0.8, -0.8, 0), (0.7, 0, -0.8)]):
        photos.append(_photo(700 + n, f"Edgewood/drone/{n}.JPG", _unit(q, 0, 1, 0, 0, 1, a, b)))

    def options() -> list[showcase.Pick]:
        places, _ = showcase.rank(photos, VECTORS, alternates=20)
        return next(p.picks for p in places if p.label == "Edgewood")

    monkeypatch.setattr(showcase, "OPTION_FLOOR", -99.0)
    everything = options()
    assert len(everything) == 4
    assert [p.score for p in everything] == sorted((p.score for p in everything), reverse=True)

    monkeypatch.setattr(showcase, "OPTION_FLOOR", everything[1].score)
    assert [p.photo.asset_id for p in options()] == [p.photo.asset_id for p in everything[:2]]

    monkeypatch.setattr(showcase, "OPTION_FLOOR", everything[0].score + 1)
    assert [p.photo.asset_id for p in options()] == [everything[0].photo.asset_id], (
        "below the floor, a place still shows its best"
    )


def _offered(places: list[showcase.Place]) -> set[str]:
    return {pick.photo.asset_id for place in places for pick in place.picks}


def test_each_kind_looks_for_its_own_photographs() -> None:
    """The calendar brief: mostly the finished work, a few crew shots in a
    scenic setting, one or two of the construction, and a group photo."""
    photos = _library()  # the work: construction at four courses
    photos += [
        _photo(900, "Pete Dye/a.JPG", _unit(1, 0, 1, 0, 0, 1)),  # finished, nobody in it
        _photo(901, "Pete Dye/b.JPG", _unit(1, 0, 1, 0, 1, 0, 0.5)),  # a crew on the course
        _photo(902, "Pete Dye/c.JPG", _unit(0.5, 0, 0.6, 0, 1, 0, 0, 0.5), faces=6),  # the team
    ]
    finished = _offered(showcase.rank(photos, VECTORS, alternates=5)[0])
    assert "a900" in finished and not finished & {"a901", "a902"}

    crew = _offered(showcase.rank(photos, VECTORS, alternates=5, kind="crew")[0])
    assert "a901" in crew and "a900" not in crew, "people, in front of the course"

    work = _offered(showcase.rank(photos, VECTORS, alternates=5, kind="construction")[0])
    assert "a900" not in work and any(int(a[1:]) < 100 for a in work), "the work, not the finish"

    places, _ = showcase.rank(photos, VECTORS, alternates=5, kind="group")
    assert [p.label for p in places] == ["Company photo"], "a group photo is not of a place"
    assert _offered(places) == {"a902"}, "four or more faces"
    assert places[0].picks[0].tags["kind"] == "group"


def test_a_ground_level_view_takes_the_last_place_when_none_made_it() -> None:
    """Drone overheads outscore the ground almost every time; the brief
    wants both."""
    photos = _library()
    for n, (a, b) in enumerate([(0.8, 0), (0, 0.8), (-0.8, 0)]):
        photos.append(
            _photo(
                960 + n, f"Pete Dye/DJI_{n}.JPG", _unit(1, 0, 1, 0, 0, 1, a, b), camera_make="DJI"
            )
        )
    # Ground level, a little less striking than the aerials.
    photos.append(
        _photo(970, "Pete Dye/IMG_1.JPG", _unit(0.8, 0, 0.9, 0, 0, 1, 0, -0.8), camera_make="Canon")
    )
    places, _ = showcase.rank(photos, VECTORS, alternates=3)
    options = next(p.picks for p in places if p.label == "Pete Dye")
    assert [p.tags["source"] for p in options] == ["drone", "drone", "ground"]
    assert options[-1].photo.asset_id == "a970", "the ground view took the last place"

    places, _ = showcase.rank(photos, VECTORS, alternates=4)
    options = next(p.picks for p in places if p.label == "Pete Dye")
    assert [p.tags["source"] for p in options].count("ground") == 1, "no second ground view forced"


def test_only_the_chosen_places_even_when_merged_by_gps() -> None:
    here = (40.9, -73.8)
    photos = _library()
    photos += [
        _photo(950, "Hudson National/a.JPG", _unit(1, 0, 1, 0, 0, 1, 0.5), gps=here),
        _photo(951, "Hudson Natl/b.JPG", _unit(1, 0, 1, 0, 0, 1, 0, 0.5), gps=here),
        _photo(952, "North Fork/c.JPG", _unit(1, 0, 1, 0, 0, 1)),
    ]
    places, _ = showcase.rank(photos, VECTORS, alternates=5, only_places={"hudsonnatl"})
    assert [p.key for p in places] == ["hudsonnational"], "the folder it was merged into"
    assert _offered(places) == {"a950", "a951"}


def test_tags_and_file_names_say_where_when_and_how() -> None:
    assert showcase.season_of("2023-10-14T09:12:00") == "fall"
    assert showcase.season_of("2024-01-02T09:12:00") == "winter"
    assert showcase.season_of(None) == ""

    def shot(make: str, name: str = "IMG_1.JPG") -> showcase.Photo:
        return _photo(1, f"x/{name}", _unit(1), camera_make=make)

    assert showcase.source_of(shot("DJI")) == "drone"
    assert showcase.source_of(shot("Hasselblad")) == "drone", "DJI's Mavic 3 camera"
    assert showcase.source_of(shot("Canon")) == "ground"
    assert showcase.source_of(shot("", "DJI_0241.JPG")) == "drone"

    finished = {"season": "fall", "source": "drone", "kind": "finished"}
    crew = {"season": "summer", "source": "ground", "kind": "crew"}
    assert showcase.file_stem("LedgeRock", 3, finished) == "ledgerock-03-fall-drone"
    assert showcase.file_stem("North Fork Country Club", 12, crew) == (
        "north-fork-country-club-crew-12-summer-ground"
    )
    assert showcase.file_stem("Company photo", 2, {"kind": "group"}) == "company-photo-02"
    assert showcase.file_stem("LedgeRock", 1, {"kind": "finished"}) == "ledgerock-01", "undated"
