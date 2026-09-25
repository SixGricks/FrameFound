"""Choosing which photos go into a slideshow, and in what order.

Selection is the whole job. A good event slideshow is not a clever transition —
it is the right forty photographs out of four hundred, with nobody left out and
nothing shown twice. FrameFound already knows enough to do that well:

- **capture time** for a spine that makes narrative sense,
- **CLIP embeddings** for collapsing four shots of the same moment to the best
  one,
- **faces** for making sure each named person actually appears,
- **theme scores** for leaning toward photographs that fit the occasion.

Everything here is deterministic. Given the same library and the same settings
it produces the same list, which is what makes a render repeatable and a
complaint diagnosable.
"""

from dataclasses import dataclass, field
from datetime import datetime

# Two frames this close in CLIP space are the same moment — burst frames, or
# the photographer taking three in case someone blinked.
NEAR_DUPLICATE_SIMILARITY = 0.94
# Below this a theme match is not meaningful, and ordering by it would just be
# ordering by noise.
MIN_THEME_SCORE = 0.18


@dataclass
class Candidate:
    asset_id: str
    captured_at: datetime | None
    embedding: list[float] | None
    theme_score: float = 0.0
    sharpness: float = 1.0
    person_ids: list[str] = field(default_factory=list)


@dataclass
class Selection:
    chosen: list[Candidate]
    dropped_duplicates: int
    people_covered: list[str]
    people_missing: list[str]


def collapse_near_duplicates(
    candidates: list[Candidate], threshold: float = NEAR_DUPLICATE_SIMILARITY
) -> tuple[list[Candidate], int]:
    """Keep the best of each burst.

    Compared against what has already been kept rather than pairwise across
    everything: three shots of the same moment collapse to one, but a person
    photographed twice an hour apart is two moments and stays two.

    Every similarity is computed up front in one matrix product, and the
    greedy pass only reads it. Written as a Python loop it cost n²/2 dot
    products of 512 floats in the interpreter — fine for a few hundred photos,
    hours for the 4,000 a proposal can consider — and it ran on the API's only
    request thread, so pressing Propose froze every page (Sep 2026). A
    matrix-vector product per candidate still took ~20 s on these CPUs; one
    matrix-matrix product lets BLAS use every core and the cache.
    """
    import numpy as np

    dims = next((len(c.embedding) for c in candidates if c.embedding), 0)
    if dims == 0:
        return list(candidates), 0

    n = len(candidates)
    matrix = np.zeros((n, dims), dtype=np.float32)
    usable = np.zeros(n, dtype=bool)
    for i, candidate in enumerate(candidates):
        if candidate.embedding and len(candidate.embedding) == dims:
            matrix[i] = candidate.embedding
            usable[i] = True
    similarity = matrix @ matrix.T

    kept: list[Candidate] = []
    # Which candidate currently holds each kept slot, in slot order.
    holders = np.empty(n, dtype=np.intp)
    dropped = 0
    for i, candidate in enumerate(candidates):
        twin_slot: int | None = None
        # A frame without a (well-formed) vector matches nothing, as before.
        if kept and usable[i]:
            hits = np.flatnonzero(similarity[i, holders[: len(kept)]] >= threshold)
            if hits.size:
                twin_slot = int(hits[0])  # the first kept match, as the loop found it
        if twin_slot is None:
            holders[len(kept)] = i
            kept.append(candidate)
            continue
        dropped += 1
        twin = kept[twin_slot]
        # Prefer the sharper frame; tie-break on theme fit.
        if (candidate.sharpness, candidate.theme_score) > (twin.sharpness, twin.theme_score):
            kept[twin_slot] = candidate
            holders[twin_slot] = i
    return kept, dropped


def select(
    candidates: list[Candidate],
    *,
    target_count: int,
    required_people: list[str] | None = None,
    themed: bool = False,
) -> Selection:
    """Pick the slideshow, then put it back in chronological order.

    Two passes on purpose. Choosing by merit and *then* re-sorting by time is
    what lets a themed slideshow still tell the day in order — picking
    chronologically first would mean the theme only ever influenced the tail.
    """
    required = list(required_people or [])
    survivors, dropped = collapse_near_duplicates(candidates)

    if themed:
        # Frames that clearly do not fit are pushed behind those that do, but
        # not removed: a thin day should still fill a slideshow.
        survivors.sort(
            key=lambda c: (c.theme_score < MIN_THEME_SCORE, -c.theme_score, -c.sharpness)
        )
    else:
        survivors.sort(key=lambda c: (-c.sharpness, -c.theme_score))

    chosen: list[Candidate] = []
    seen_people: set[str] = set()

    # Everyone who must appear gets their best frame first. This is the whole
    # reason a church or a family asks for a slideshow, and a purely
    # chronological or purely themed cut misses people entirely.
    for person in required:
        best = next(
            (c for c in survivors if person in c.person_ids and c not in chosen),
            None,
        )
        if best is not None:
            chosen.append(best)
            seen_people.update(best.person_ids)

    for candidate in survivors:
        if len(chosen) >= target_count:
            break
        if candidate in chosen:
            continue
        chosen.append(candidate)
        seen_people.update(candidate.person_ids)

    # Chronological for the render. Undated frames trail rather than leading,
    # because an unknown date is not the same as the earliest date.
    chosen.sort(key=lambda c: (c.captured_at is None, c.captured_at or datetime.min))

    return Selection(
        chosen=chosen,
        dropped_duplicates=dropped,
        people_covered=sorted(seen_people & set(required)),
        people_missing=sorted(set(required) - seen_people),
    )
