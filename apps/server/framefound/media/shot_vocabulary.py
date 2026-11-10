"""What a shot shows and how it is framed: CLIP prompts, kept as data.

Descriptors, not tags. A tag is the operator's word for something they will
search by, and earns its place through the review queue; these are scored
descriptions of every window — "aerial, oblique, wide; green, bunker" —
that find_shots filters on and nobody is asked to confirm. They never enter
the tag review queue.

**Vocabularies are data.** The built-in one is golf-course construction,
for GELCO. Another client's words go in a JSON file in the data directory,
`<data_dir>/vocabularies/<name>.json`, and apply on the next descriptor
pass (`python -m framefound.ops.shots describe`) — no deploy:

    {"label": "Barn restoration",
     "subjects": {"timber frame": ["exposed timber framing", "a barn's beams"],
                  "slate roof": ["a slate roof being laid"]}}

Every window is scored against every subject of every vocabulary; a subject
is one more row in one matrix product, so there is no need to say which
vocabulary a library uses.

Framing is three questions with fixed answers (aerial or ground; top-down,
oblique or near the horizon; wide, medium or close), each a small set of
prompts. A camera that is a drone counts for "aerial" too: CLIP can mistake
a high ground shot for one, the camera's maker cannot.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger()


@dataclass(frozen=True)
class Vocabulary:
    slug: str
    label: str
    # Subject -> CLIP prompts. Several short captions per subject beat one
    # long one, for the reason media/theming.py gives.
    subjects: dict[str, list[str]] = field(default_factory=dict)


FRAMING: dict[str, dict[str, list[str]]] = {
    "view": {
        "aerial": [
            "an aerial photo taken from a drone",
            "a drone shot looking down at the ground from high above",
            "a bird's eye view of a landscape",
        ],
        "ground": [
            "a photo taken standing on the ground",
            "a ground level photo of a landscape",
            "a handheld photo at eye level",
        ],
    },
    "angle": {
        "top-down": [
            "a top-down aerial photo looking straight down",
            "an overhead view directly from above, no horizon",
            "a straight down drone shot like a map",
        ],
        "oblique": [
            "an oblique aerial photo looking down at an angle",
            "a high angle view of a landscape",
            "a drone shot angled down toward the ground with the horizon at the top",
        ],
        "horizon": [
            "a low view toward the horizon with lots of sky",
            "a landscape photo with the horizon across the middle",
            "a view looking out level at the skyline",
        ],
    },
    "size": {
        "wide": [
            "a wide establishing shot of a large area",
            "a wide landscape view",
            "a panoramic view of the whole site",
        ],
        "medium": [
            "a medium shot of a work area",
            "a mid-range view of a few objects",
        ],
        "close": [
            "a close-up photo of an object",
            "a detailed close-up shot",
            "a close view filling the frame with one thing",
        ],
    },
}

BUILT_IN: dict[str, Vocabulary] = {
    "golf-construction": Vocabulary(
        slug="golf-construction",
        label="Golf course construction (GELCO)",
        subjects={
            "bunker": ["a sand bunker on a golf course", "a golf course sand trap"],
            "green": ["a golf green with a flag", "a putting green on a golf course"],
            "fairway": ["a golf course fairway", "mown fairway grass on a golf course"],
            "tee box": ["a golf tee box", "the teeing ground of a golf hole"],
            "clubhouse": ["a golf clubhouse building", "a country club clubhouse"],
            "pump station": [
                "an irrigation pump station building",
                "pumps and pipes inside a pump house",
            ],
            "trench": ["a trench dug in the ground", "an open trench for pipe on a golf course"],
            "mainline pipe": [
                "large irrigation pipe laid in a trench",
                "lengths of PVC mainline pipe",
            ],
            "irrigation head": [
                "a golf course sprinkler head",
                "an irrigation sprinkler spraying water on grass",
            ],
            "sod": ["rolls of sod being laid", "freshly laid sod strips"],
            "cart path": ["a golf cart path", "a paved path winding across a golf course"],
            "pond": ["a pond on a golf course", "a water hazard lake beside a fairway"],
            "machine operating": [
                "an excavator digging",
                "construction machinery working on a site",
                "a tractor or skid steer moving earth",
            ],
            "crew working": [
                "construction workers working on site",
                "a crew of workers installing pipe",
            ],
            "finished hole": [
                "a finished golf hole with green and fairway",
                "a manicured golf hole ready for play",
            ],
            "before/after": [
                "bare dirt where grass will be planted",
                "a golf course under construction with exposed soil",
            ],
        },
    ),
}

VOCABULARY_DIR = "vocabularies"


def load_vocabularies(data_dir: Path | None) -> dict[str, Vocabulary]:
    """The built-in vocabularies plus any JSON ones in the data directory.
    A file that does not parse is skipped with a warning rather than
    stopping the descriptor pass."""
    found = dict(BUILT_IN)
    if data_dir is None:
        return found
    folder = data_dir / VOCABULARY_DIR
    if not folder.is_dir():
        return found
    for path in sorted(folder.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            subjects = {
                str(name).strip().lower(): [str(p) for p in prompts if str(p).strip()]
                for name, prompts in dict(raw.get("subjects", {})).items()
                if str(name).strip()
            }
            found[path.stem] = Vocabulary(
                slug=path.stem,
                label=str(raw.get("label") or path.stem),
                subjects={k: v for k, v in subjects.items() if v},
            )
        except (OSError, ValueError, TypeError, AttributeError) as err:
            log.warning("vocabulary.unreadable", file=path.name, reason=str(err)[:120])
    return found


def _best(vector: list[float], prompts: list[list[float]]) -> float:
    return max((sum(a * b for a, b in zip(vector, p, strict=False)) for p in prompts), default=0.0)


# CLIP's own logit scale: cosine similarities that differ by 0.01 are a
# real preference at this temperature, which a plain argmax would not say.
LOGIT_SCALE = 100.0
MAX_SUBJECTS = 4
SUBJECT_Z = 1.0


def describe_framing(
    vector: list[float], prompts: dict[str, dict[str, list[list[float]]]], is_drone: bool = False
) -> dict[str, object]:
    """Each framing question answered with its likeliest label and how
    sure CLIP is (a softmax over the group's labels)."""
    import math

    out: dict[str, object] = {}
    for question, labels in prompts.items():
        sims = {label: _best(vector, vecs) for label, vecs in labels.items() if vecs}
        if not sims:
            continue
        top = max(sims.values())
        weights = {k: math.exp(LOGIT_SCALE * (v - top)) for k, v in sims.items()}
        total = sum(weights.values())
        probs = {k: w / total for k, w in weights.items()}
        if question == "view" and is_drone and "aerial" in probs:
            # The camera's maker outranks CLIP's guess: a drone is aerial.
            probs = {k: (0.95 if k == "aerial" else 0.05 / max(1, len(probs) - 1)) for k in probs}
        label = max(probs, key=lambda k: probs[k])
        out[question] = label
        out[f"{question}_conf"] = round(probs[label], 3)
    return out


def describe_subjects(
    vector: list[float], subjects: dict[str, list[list[float]]]
) -> dict[str, float]:
    """The subjects a window stands out for: those scoring a standard
    deviation above the window's own average across the whole subject
    list, at most four, with that margin (z) as the score. Relative on
    purpose — CLIP's raw similarities drift with the picture's overall
    look, and a misty morning scores lower on everything."""
    sims = {name: _best(vector, vecs) for name, vecs in subjects.items() if vecs}
    if len(sims) < 3:
        return {}
    values = list(sims.values())
    mean = sum(values) / len(values)
    spread = (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5 or 1e-9
    ranked = sorted(sims.items(), key=lambda kv: kv[1], reverse=True)
    out: dict[str, float] = {}
    for name, sim in ranked[:MAX_SUBJECTS]:
        z = (sim - mean) / spread
        if z >= SUBJECT_Z:
            out[name] = round(z, 2)
    return out


def all_subjects(vocabularies: dict[str, Vocabulary]) -> dict[str, list[str]]:
    """Every subject across vocabularies; a name in two pools its prompts."""
    merged: dict[str, list[str]] = {}
    for vocabulary in vocabularies.values():
        for name, prompts in vocabulary.subjects.items():
            merged.setdefault(name, [])
            merged[name].extend(p for p in prompts if p not in merged[name])
    return merged
