"""Room recognition for real-estate listings, zero-shot from CLIP.

No model runs against the photographs here. Every indexed image already has a
CLIP embedding in pgvector, and CLIP's text encoder lives in the same space —
so "which room is this" is a dot product between vectors the catalogue
already paid for and a handful of text vectors computed once per process.
Classifying a fifty-photo shoot costs microseconds, which is what lets the
labels appear instantly when photos are added to a listing.

Labels are suggestions until the operator confirms or overrides — the same
contract tags and faces honour. The canonical order below is the sequence a
listing gallery conventionally walks: kerb appeal first, then through the
front door, public rooms before private ones, outside last, plans at the end.
"""

from dataclasses import dataclass

from framefound.ai.embeddings import get_embedding_provider


@dataclass(frozen=True)
class Room:
    key: str  # stored in ListingItem.room, used in export filenames
    label: str  # shown in the UI
    prompts: tuple[str, ...]  # text prompts averaged into one vector


# In canonical listing order. Multiple prompts per room because CLIP responds
# to phrasing; averaging a few paraphrases is steadier than betting on one.
ROOMS: tuple[Room, ...] = (
    Room(
        "front_exterior",
        "Front exterior",
        (
            "the front exterior of a house seen from the street",
            "a real estate photo of the front of a home with its lawn",
        ),
    ),
    Room(
        "aerial",
        "Aerial",
        (
            "an aerial drone photo of a house and its property",
            "a bird's eye view of a residential property",
        ),
    ),
    Room(
        "entryway",
        "Entryway",
        ("the entryway or foyer inside a home", "a front hallway with a staircase"),
    ),
    Room(
        "living_room",
        "Living room",
        (
            "a living room with sofas and a television",
            "a family room with couches and a fireplace",
        ),
    ),
    Room(
        "dining_room",
        "Dining room",
        ("a dining room with a table and chairs",),
    ),
    Room(
        "kitchen",
        "Kitchen",
        (
            "a kitchen with cabinets, countertops and appliances",
            "a real estate photo of a kitchen island",
        ),
    ),
    Room(
        "bedroom",
        "Bedroom",
        ("a bedroom with a bed", "a real estate photo of a bedroom"),
    ),
    Room(
        "bathroom",
        "Bathroom",
        (
            "a bathroom with a sink and toilet",
            "a bathroom with a shower or bathtub",
        ),
    ),
    Room(
        "office",
        "Office",
        ("a home office with a desk",),
    ),
    Room(
        "laundry",
        "Laundry",
        ("a laundry room with a washer and dryer",),
    ),
    Room(
        "basement",
        "Basement",
        ("an unfinished basement", "a finished basement recreation room"),
    ),
    Room(
        "garage",
        "Garage",
        ("the inside of a garage", "a garage door on a house"),
    ),
    Room(
        "backyard",
        "Backyard",
        (
            "the backyard of a house with a lawn",
            "a back deck or patio behind a home",
        ),
    ),
    Room(
        "pool",
        "Pool",
        ("a swimming pool at a house",),
    ),
    Room(
        "floor_plan",
        "Floor plan",
        ("a floor plan drawing of a house", "an architectural floor plan diagram"),
    ),
)

ROOM_ORDER: dict[str, int] = {room.key: index for index, room in enumerate(ROOMS)}
ROOM_LABELS: dict[str, str] = {room.key: room.label for room in ROOMS}

# Below this similarity the best guess is noise, not a suggestion. CLIP
# zero-shot scores against averaged prompts typically land 0.2-0.35 for a
# true match; 0.15 keeps "no idea" honest instead of defaulting everything
# to whichever label sits closest in a sparse region.
MIN_ROOM_SCORE = 0.15

_vectors_cache: list[list[float]] | None = None


def _mean_unit(vectors: list[list[float]]) -> list[float]:
    summed = [sum(parts) for parts in zip(*vectors, strict=True)]
    norm = sum(v * v for v in summed) ** 0.5
    return [v / norm for v in summed] if norm else summed


def room_vectors() -> list[list[float]]:
    """One unit vector per room, in ROOMS order. Computed once per process.

    Raises EmbeddingUnavailable where the CLIP runtime is not installed;
    callers degrade to unlabelled items rather than failing the request.
    """
    global _vectors_cache
    if _vectors_cache is None:
        provider = get_embedding_provider()
        _vectors_cache = [
            _mean_unit([provider.embed_text(p).vector for p in room.prompts]) for room in ROOMS
        ]
    return _vectors_cache


def classify(embedding: list[float], vectors: list[list[float]]) -> tuple[str, float]:
    """Best room for one image embedding: ("kitchen", 0.31), or ("", score)
    when nothing clears the floor."""
    best_key, best_score = "", 0.0
    for room, vector in zip(ROOMS, vectors, strict=True):
        score = sum(a * b for a, b in zip(embedding, vector, strict=False))
        if score > best_score:
            best_key, best_score = room.key, score
    if best_score < MIN_ROOM_SCORE:
        return "", best_score
    return best_key, best_score


def canonical_sort_key(room: str, score: float | None) -> tuple[int, float]:
    """Unlabelled items sort last; within a room, strongest match first."""
    return (ROOM_ORDER.get(room, len(ROOMS)), -(score or 0.0))
