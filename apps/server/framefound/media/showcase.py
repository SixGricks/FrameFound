"""Finding the showcase photographs: the best finished work, one per place.

Asked for by GELCO (golf course construction) for a calendar: fourteen
masterpiece-quality landscape photographs, each from a different course,
showing the finished product rather than the work. Nothing here is
specific to golf — the subject is a parameter — because "the best of our
work, one per project" is the same question for a portfolio, a website or
an award entry.

How a photograph is judged, all from what the catalogue already stores:

- **Quality and finish** — CLIP zero-shot, the CLIP-IQA idea: how much
  closer a photograph sits to "an award-winning photograph" than to "an
  ordinary documentation photo", and to "a pristine finished golf course"
  than to "a construction site with excavators and bare soil". Margins, not
  probabilities: a softmax saturates at the top, exactly where the ranking
  has to discriminate (a first trial scored twenty photos 0.98–1.00).
- **Fitness for print** — megapixels, orientation and aspect (a 360°
  "tiny planet" is not a calendar page), and brightness from the thumbnail
  (a dark or fogged frame reads as muddy in print).
- **Sky** — a bonus for a frame that shows one, measured in the thumbnail's
  pixels. The best pages usually have a sky, not always, so it is never a
  gate. (A learned aesthetic model — LAION's, on the same CLIP embeddings —
  was tried and rejected: its twenty favourites were mostly the crew.)
- **The work, not the finish** — faces (people in frame are usually a crew)
  and folder names that say so ("irrigation", "drainage", "sod").

Places are the top-level folders, merged when two names are one course
("Forest gate" / "Forest Gate NJ") by name or by GPS. Near-duplicate frames
collapse to the best, and each place offers alternates so a choice is a
choice.
"""

import re
from dataclasses import dataclass, field
from typing import Any

QUALITY_POSITIVE = [
    "a breathtaking professional photograph",
    "an award-winning landscape photograph",
    "a sharp, well-lit, beautiful photo with vivid colour",
]
QUALITY_NEGATIVE = [
    "a dull amateur snapshot",
    "an ordinary documentation photo",
    "a blurry, dark, poorly lit photo",
    "a dreary grey overcast day",
]


# People and machines in frame. On a showcase page they are the crew, a
# client or a golfer, not the work — and the face detector misses a back or
# a figure at distance (a first run offered a man on his phone walking
# through a bunker). Measured against the finished subject itself, not "an
# empty landscape", which a course seen from a drone never looks like.
def clutter_prompts(subject: str) -> dict[str, list[str]]:
    subject = " ".join(subject.split()) or "project"
    empty = [
        f"an empty {subject} with nobody on it",
        f"a pristine {subject} landscape with no vehicles",
        f"a quiet {subject} with no people or machines",
    ]
    return {
        "people_pos": [
            f"people playing or walking on a {subject}",
            "a group of people standing on the grass",
            "a man posing for a photo",
            "a worker on a job site",
        ],
        "people_neg": empty,
        "equipment_pos": [
            f"a tractor on a {subject}",
            "a red utility vehicle and a trailer",
            "construction equipment on the grass",
            "a pickup truck parked on the grass",
            "workers with shovels and a wheelbarrow",
        ],
        "equipment_neg": empty,
    }


def finish_prompts(subject: str) -> tuple[list[str], list[str]]:
    """What "finished" and "being worked on" look like for this subject."""
    subject = " ".join(subject.split()) or "project"
    positive = [
        f"a pristine finished {subject}",
        f"an aerial view of a beautiful {subject}",
        f"a manicured {subject} in perfect condition",
    ]
    negative = [
        f"{subject} construction with excavators and dirt",
        "a construction site with heavy machinery and bare soil",
        "workers digging a muddy trench",
        "a bulldozer on bare earth",
        "piles of sand and gravel at a work site",
        # Seen from a drone, a crew is tractors and a truck on the grass.
        "tractors, utility vehicles and a truck parked on the grass",
        "maintenance equipment and tools spread out on a lawn",
    ]
    return positive, negative


# Folder words that mean the photos document work in progress.
WORK_WORDS = re.compile(
    r"construct|progress|irrigat|drain|trench|excavat|sod\b|seeding|grading|demo\b|"
    r"install|dig|before|during|work",
    re.IGNORECASE,
)
# A place name's noise: punctuation, state suffixes, one common misspelling.
_PLACE_NOISE = re.compile(r"[^a-z0-9]+")
_STATE_SUFFIX = re.compile(r"\s+(nj|pa|ny|de|md|va|ct)$", re.IGNORECASE)
SAME_PLACE_KM = 2.0
NEAR_DUPLICATE = 0.94
MAX_ASPECT = 1.85
FINISH_FLOOR_PERCENTILE = 75.0
# A photograph closer to "people on the course" / "a tractor on the course"
# than to the empty course by more than these is left out (people only when
# they are not allowed). Set against GELCO's shortlist by eye: above them
# nearly every frame had a crew, golfers or machines in it.
PEOPLE_MARGIN = -0.0115
EQUIPMENT_MARGIN = -0.004


@dataclass
class Photo:
    asset_id: str
    relative_path: str
    filename: str
    width: int
    height: int
    embedding: list[float]
    gps: tuple[float, float] | None = None
    faces: int = 0
    captured_at: str | None = None
    thumbnail: str | None = None  # data-dir path, for the brightness check


@dataclass
class Pick:
    photo: Photo
    score: float
    parts: dict[str, float] = field(default_factory=dict)


@dataclass
class Place:
    key: str
    label: str
    picks: list[Pick]


def place_of(relative_path: str) -> tuple[str, str]:
    """(key, label) from the top-level folder."""
    label = relative_path.split("/", 1)[0].strip() if "/" in relative_path else "(library root)"
    key = _PLACE_NOISE.sub("", _STATE_SUFFIX.sub("", label.lower()))
    return key.replace("colony", "colonie"), label


def _distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    import math

    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, h)))


# Folders that collect copies from many places — "Social Images", "Photo
# Exports", "2024 images from Brian". Their names say nothing about where a
# photograph was taken (GELCO's LuLu/Social images/NORTHFORK holds North
# Fork's best shot), so a photo in one is placed by GPS or not at all.
GENERIC_FOLDER = re.compile(r"social|export|images from|post images|misc|unsorted|download", re.I)


def is_generic(relative_path: str) -> bool:
    return any(GENERIC_FOLDER.search(part) for part in relative_path.split("/")[:-1])


def assign_places(photos: list[Photo]) -> dict[str, str | None]:
    """photo asset_id -> place key (None = cannot tell where it was taken).

    A place is a top-level project folder. Its centre is the median GPS of
    its own (non-generic) photographs; folders whose centres lie within
    SAME_PLACE_KM are one place ("Forest gate" / "Forest Gate NJ"). A
    photograph with GPS belongs to the nearest centre within that distance;
    one without GPS belongs to its folder — unless the folder is a
    collection of copies, in which case its place is unknown.
    """
    import statistics

    points: dict[str, list[tuple[float, float]]] = {}
    for photo in photos:
        if photo.gps and not is_generic(photo.relative_path):
            points.setdefault(place_of(photo.relative_path)[0], []).append(photo.gps)
    centres = {
        key: (statistics.median(p[0] for p in pts), statistics.median(p[1] for p in pts))
        for key, pts in points.items()
    }
    alias: dict[str, str] = {}
    ordered = sorted(centres)
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if b not in alias and _distance_km(centres[a], centres[b]) < SAME_PLACE_KM:
                alias[b] = alias.get(a, a)

    def canonical(key: str) -> str:
        return alias.get(key, key)

    assigned: dict[str, str | None] = {}
    for photo in photos:
        folder_key = place_of(photo.relative_path)[0]
        generic = is_generic(photo.relative_path)
        if photo.gps and centres:
            nearest, distance = min(
                ((key, _distance_km(photo.gps, centre)) for key, centre in centres.items()),
                key=lambda kv: kv[1],
            )
            if distance < SAME_PLACE_KM:
                assigned[photo.asset_id] = canonical(nearest)
                continue
        assigned[photo.asset_id] = None if generic else canonical(folder_key)
    return assigned


def technical(photo: Photo, orientation: str, min_megapixels: float) -> tuple[float, str]:
    """A multiplier in [0, 1] for print fitness, and the reason when it is 0."""
    if not photo.width or not photo.height:
        return 0.0, "size unknown"
    megapixels = photo.width * photo.height / 1e6
    if megapixels < min_megapixels:
        return 0.0, f"{megapixels:.0f} MP is under {min_megapixels:.0f}"
    long_side, short_side = max(photo.width, photo.height), min(photo.width, photo.height)
    aspect = long_side / short_side
    if orientation == "landscape" and photo.width <= photo.height:
        return 0.0, "portrait"
    if orientation == "portrait" and photo.height <= photo.width:
        return 0.0, "landscape"
    if aspect > MAX_ASPECT:
        # A 2:1 spherical panorama ("tiny planet") is not a calendar page;
        # 16:9 is the widest ordinary frame.
        return 0.0, "panorama"
    return 1.0, ""


# Sky. GELCO's best photographs usually show some — a horizon, clouds, a
# sunset over the course — but not always: a straight-down drone shot of a
# green can be the best of a course. So sky earns a bonus and is never
# required. It is measured in pixels, not words: CLIP knows a frame with no
# sky at all, but scores a band of sky above the trees much like none.
SKY_WEIGHT = 0.25
SKY_NONE = 0.03  # under this share of the frame: none, or a blurred backdrop
SKY_FULL = 0.12  # a proper sky; more earns no more
SKY_TOO_MUCH = 0.65  # a picture of the sky, or a blank white frame
# Brightness and sky need pixels, so thumbnails are read for each place's
# leaders only — enough of them that the sky bonus can reorder the top.
SHORTLIST_PER_ALTERNATE = 6


def sky_fraction(image: Any) -> float:
    """The share of the frame that is sky: cells joined to the top edge that
    are blue or pale grey (cloud, overcast) and smooth."""
    import numpy as np
    from PIL import Image

    small = image.convert("RGB").resize((96, 64), Image.Resampling.BOX)
    rgb = np.asarray(small, dtype=np.float32) / 255.0
    red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    value = rgb.max(axis=2)
    saturation = (value - rgb.min(axis=2)) / np.maximum(value, 1e-6)
    colour = ((blue >= red + 0.03) & (blue >= green - 0.03) & (value > 0.35)) | (
        (saturation < 0.16) & (value > 0.55)
    )
    texture = np.abs(np.diff(value, axis=0, prepend=value[:1])) + np.abs(
        np.diff(value, axis=1, prepend=value[:, :1])
    )
    sky = colour & (texture < 0.07)
    # A cloud's edge is texture for one cell; let the run down from the top
    # cross it rather than stop there.
    below = np.vstack([sky[1:], np.zeros((1, sky.shape[1]), dtype=bool)])
    sky = colour & (sky | below)
    return float(np.cumprod(sky, axis=0).mean())


def sky_presence(fraction: float) -> float:
    """0..1: how fully a frame shows a sky, for the bonus."""
    if fraction >= SKY_TOO_MUCH:
        return 0.0
    return min(1.0, max(0.0, (fraction - SKY_NONE) / (SKY_FULL - SKY_NONE)))


def look(thumbnail_path: Any) -> tuple[float, float]:
    """(exposure factor, sky fraction) from one read of a thumbnail;
    (1.0, 0.0) when it cannot be read."""
    import numpy as np
    from PIL import Image

    try:
        with Image.open(thumbnail_path) as img:
            rgb = img.convert("RGB")
    except OSError:
        return 1.0, 0.0
    luma = np.asarray(rgb.convert("L"), dtype=np.float32) / 255.0
    return exposure_factor(luma), sky_fraction(rgb)


def exposure_factor(luma: Any) -> float:
    """1.0 for a well-exposed frame, less for a dark or washed-out one —
    judged from the luminance spread (0..1 values)."""
    import numpy as np

    mean = float(luma.mean())
    spread = float(np.percentile(luma, 95) - np.percentile(luma, 5))
    factor = 1.0
    if mean < 0.30:
        factor *= max(0.4, mean / 0.30)
    if mean > 0.75:
        factor *= max(0.5, 1.0 - (mean - 0.75) * 2)
    if spread < 0.35:  # fog, haze, flat grey
        factor *= max(0.5, spread / 0.35)
    return factor


def rank(
    photos: list[Photo],
    text_vectors: dict[str, Any],
    *,
    count: int = 14,
    alternates: int = 3,
    orientation: str = "landscape",
    min_megapixels: float = 12.0,
    thumbnail_root: Any = None,
    allow_people: bool = False,
) -> tuple[list[Place], int]:
    """The showcase: up to `count` places, best first, each with up to
    `alternates` distinct picks. Returns (places, photos considered).

    `text_vectors` holds the prompt embeddings under "quality_pos",
    "quality_neg", "finish_pos", "finish_neg" (and optionally "avoid").
    """
    import numpy as np

    usable = [p for p in photos if p.embedding]
    if not usable:
        return [], 0
    matrix = np.asarray([p.embedding for p in usable], dtype=np.float64)

    def margin(pos_key: str, neg_key: str) -> Any:
        pos = np.asarray(text_vectors[pos_key], dtype=np.float64)
        neg = np.asarray(text_vectors[neg_key], dtype=np.float64)
        return (matrix @ pos.T).max(axis=1) - (matrix @ neg.T).max(axis=1)

    quality = margin("quality_pos", "quality_neg")
    finish = margin("finish_pos", "finish_neg")

    def optional(pos_key: str, neg_key: str) -> Any:
        if len(text_vectors.get(pos_key, [])) == 0:
            return np.full(len(usable), -1.0)
        return margin(pos_key, neg_key)

    people = optional("people_pos", "people_neg")
    equipment = optional("equipment_pos", "equipment_neg")

    # Standardise each signal so neither dominates by scale, then combine.
    def z(values: Any) -> Any:
        spread = float(values.std()) or 1.0
        return (values - float(values.mean())) / spread

    # Finish weighs more than beauty, and is also a floor: a photograph must
    # look more finished than FINISH_FLOOR_PERCENTILE of the library to be
    # offered at all. A construction company's average photo *is* the work,
    # so "above average" was too low a bar — a first run offered a stunning
    # stone ruin with an excavator parked behind it.
    finish_floor = float(np.percentile(finish, FINISH_FLOOR_PERCENTILE))
    base = 0.4 * z(quality) + 0.6 * z(finish)
    if len(text_vectors.get("avoid", [])) > 0:
        avoid = (matrix @ np.asarray(text_vectors["avoid"], dtype=np.float64).T).max(axis=1)
        base = base - 0.5 * z(avoid)
    places = assign_places(usable)
    # Each place is named after the folder most of its own photographs use.
    label_votes: dict[str, dict[str, int]] = {}
    for photo in usable:
        key = places[photo.asset_id]
        if key and not is_generic(photo.relative_path):
            label = place_of(photo.relative_path)[1]
            votes = label_votes.setdefault(key, {})
            votes[label] = votes.get(label, 0) + 1
    scored: list[Pick] = []
    for index, photo in enumerate(usable):
        if places[photo.asset_id] is None:
            continue  # a copy in a collection folder, with nothing to say where
        if finish[index] <= max(finish_floor, 0.0):
            continue  # looks more like the work than the finished product
        if not allow_people and (photo.faces or people[index] > PEOPLE_MARGIN):
            continue  # someone in frame
        if equipment[index] > EQUIPMENT_MARGIN:
            continue  # machines on the grass: the work, not the finish
        factor, _why = technical(photo, orientation, min_megapixels)
        if factor == 0.0:
            continue
        penalty = 1.0
        if WORK_WORDS.search(photo.relative_path.rsplit("/", 1)[0]):
            penalty *= 0.7
        value = float(base[index])
        # Penalties shrink a good score toward zero and push a bad one lower.
        value = value * penalty if value > 0 else value / penalty
        scored.append(
            Pick(
                photo,
                value,
                {
                    "quality": round(float(quality[index]), 4),
                    "finished": round(float(finish[index]), 4),
                    "people": round(float(people[index]), 4),
                    "equipment": round(float(equipment[index]), 4),
                    "penalty": round(penalty, 2),
                },
            )
        )

    by_place: dict[str, list[Pick]] = {}
    for pick in sorted(scored, key=lambda p: -p.score):
        by_place.setdefault(places[pick.photo.asset_id] or "", []).append(pick)

    shortlists = {
        key: picks[: alternates * SHORTLIST_PER_ALTERNATE] for key, picks in by_place.items()
    }
    if thumbnail_root is not None:
        from concurrent.futures import ThreadPoolExecutor

        readable = [p for s in shortlists.values() for p in s if p.photo.thumbnail]
        # Decoding releases the GIL: a few threads read hundreds of
        # thumbnails in a few seconds rather than twenty.
        with ThreadPoolExecutor(max_workers=8) as pool:
            looks = list(pool.map(lambda p: look(thumbnail_root / p.photo.thumbnail), readable))
        for pick, (exposure, sky) in zip(readable, looks, strict=True):
            pick.parts["exposure"] = round(exposure, 2)
            pick.parts["sky"] = round(sky, 3)
            pick.score += SKY_WEIGHT * sky_presence(sky)
            pick.score = pick.score * exposure if pick.score > 0 else pick.score / exposure
        for shortlist in shortlists.values():
            shortlist.sort(key=lambda p: -p.score)

    # Best place first. A frame already offered — near-identical to one in
    # this place or in a better-ranked one (the same drone shot filed under
    # two courses) — is never offered again.
    results: list[Place] = []
    offered: list[Any] = []
    for key in sorted(shortlists, key=lambda k: -shortlists[k][0].score):
        distinct: list[Pick] = []
        for pick in shortlists[key]:
            vector = np.asarray(pick.photo.embedding)
            if any(float(vector @ other) >= NEAR_DUPLICATE for other in offered):
                continue
            distinct.append(pick)
            offered.append(vector)
            if len(distinct) == alternates:
                break
        if not distinct:
            continue
        votes = label_votes.get(key, {})
        label = max(votes, key=lambda name: votes[name]) if votes else key
        results.append(Place(key, label, distinct))
        if len(results) == count:
            break
    return results, len(usable)
