"""Curation: which photographs a listing can afford to lose.

Two kinds of removal candidate, both suggestions and nothing more:

- **Near-duplicates.** Shoots produce five frames of the same kitchen; CLIP
  embeddings the catalogue already stores make the grouping a dot product.
  Within a group, the sharpest frame stays and the rest are offered up.
- **Soft outliers.** Frames markedly blurrier than the listing's norm — but
  only when their room stays represented. A blurry photo of the only barn
  is still the only barn, and coverage beats polish.

Sharpness is mean absolute Laplacian on a downscaled luma — the standard
"is this in focus" number, no model required.

A local model's use score (`assets.look_score`, docs/local-models.md L3),
when every photograph it would decide between has one, may do two things
more, both inside the same guards: keep the better-scored of a duplicate
group, and offer a frame it scored far below the rest of the shoot, with
its reason. Without scores nothing here changes: the score is an opinion
added to the measurements, never a replacement for them.
"""

from typing import Any

# Two frames of one subject: embeddings this similar are the same shot in
# CLIP's eyes. Chosen above the tag/face thresholds because near-duplicate
# means *interchangeable*, not merely related.
DUPLICATE_SIMILARITY = 0.92
# A frame this far below the listing's median sharpness is soft. Ratio, not
# z-score: shoots are small samples and one tack-sharp aerial should not
# make everything else look blurry by comparison.
SOFTNESS_RATIO = 0.45
# Never offer to remove more than this fraction of a listing.
MAX_SUGGEST_FRACTION = 0.4
# The local model's preference decides a duplicate group only when its best
# frame beats every other by this much: a few points either way is noise in
# an uncalibrated small model, and sharpness is a measurement.
SCORE_LEAD = 10
# A frame is offered for its score alone only when it is both low in itself
# and well below the shoot's own median — a shoot the model disliked as a
# whole loses nothing for it.
LOW_SCORE = 40
LOW_SCORE_GAP = 25

DUPLICATE_SOFTER = "near-duplicate, softer than the kept frame"
DUPLICATE_PREFERRED = "near-duplicate; the local model preferred the kept frame"


def sharpness(image: Any) -> float:
    """Mean absolute Laplacian of the luma at analysis size."""
    import numpy as np

    gray = np.asarray(image.convert("L"), dtype=np.float32)
    lap = (
        -4.0 * gray[1:-1, 1:-1]
        + gray[:-2, 1:-1]
        + gray[2:, 1:-1]
        + gray[1:-1, :-2]
        + gray[1:-1, 2:]
    )
    return float(np.abs(lap).mean())


def group_duplicates(embeddings: dict[str, list[float]]) -> list[list[str]]:
    """Connected groups of near-identical frames.

    Every pair is compared, in one matrix product. As a Python loop over pairs
    this was 125,000 dot products of 512 floats for a 500-photo listing — tens
    of seconds on these CPUs, and the API served nothing else meanwhile.
    """
    import numpy as np

    ids = list(embeddings)
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    dims = len(embeddings[ids[0]]) if ids else 0
    comparable = [i for i in ids if len(embeddings[i]) == dims and dims]
    if len(comparable) > 1:
        matrix = np.asarray([embeddings[i] for i in comparable], dtype=np.float32)
        similar = np.triu(matrix @ matrix.T >= DUPLICATE_SIMILARITY, k=1)
        for a, b in np.argwhere(similar):
            parent[find(comparable[a])] = find(comparable[b])

    groups: dict[str, list[str]] = {}
    for item_id in ids:
        groups.setdefault(find(item_id), []).append(item_id)
    return [members for members in groups.values() if len(members) > 1]


def _score(item: dict[str, Any]) -> int | None:
    value = item.get("score")
    return None if value is None else int(value)


def _scored_keep(group: list[str], by_id: dict[str, dict[str, Any]]) -> str | None:
    """The member the local model clearly preferred, or None to fall back on
    sharpness: some member has no score, or the best does not lead the rest
    by SCORE_LEAD."""
    scores = [_score(by_id[member]) for member in group]
    if any(score is None for score in scores):
        return None
    ranked = sorted(
        ((score, member) for score, member in zip(scores, group, strict=True) if score is not None),
        reverse=True,
    )
    (best, keep), (second, _) = ranked[0], ranked[1]
    return keep if best - second >= SCORE_LEAD else None


def suggest_removals(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Given [{id, room, sharpness, embedding}, plus optional score (0-100)
    and score_reason}], the removals a listing can afford. Coverage guard: a
    room never loses its last photograph, and at most MAX_SUGGEST_FRACTION
    of the listing is offered, whatever the reason."""
    by_id = {item["id"]: item for item in items}
    room_counts: dict[str, int] = {}
    for item in items:
        room_counts[item["room"]] = room_counts.get(item["room"], 0) + 1

    suggestions: list[dict[str, Any]] = []
    claimed: set[str] = set()
    # Frames a duplicate suggestion says to keep instead. They are never
    # offered themselves: "remove k2, keep k1" beside "remove k1" would lose
    # the view to anyone who accepted both. A keep chosen by score can be
    # the softer or a low-scored frame, so the later passes must skip it.
    kept: set[str] = set()

    def room_can_spare(item_id: str) -> bool:
        room = by_id[item_id]["room"]
        remaining = room_counts.get(room, 0)
        losing = sum(1 for s in suggestions if by_id[s["id"]]["room"] == room)
        return (remaining - losing) > 1

    embeddings = {i["id"]: i["embedding"] for i in items if i.get("embedding")}
    for group in group_duplicates(embeddings):
        preferred = _scored_keep(group, by_id)
        keep = preferred or max(group, key=lambda i: by_id[i]["sharpness"])
        reason = DUPLICATE_PREFERRED if preferred else DUPLICATE_SOFTER
        for member in group:
            if member == keep or member in claimed:
                continue
            if not room_can_spare(member):
                continue
            claimed.add(member)
            kept.add(keep)
            suggestions.append({"id": member, "reason": reason, "keep_instead": keep})

    sharp_values = sorted(i["sharpness"] for i in items)
    if sharp_values:
        median = sharp_values[len(sharp_values) // 2]
        for item in items:
            if item["id"] in claimed or item["id"] in kept:
                continue
            if median > 0 and item["sharpness"] < median * SOFTNESS_RATIO:
                if not room_can_spare(item["id"]):
                    continue
                claimed.add(item["id"])
                suggestions.append(
                    {
                        "id": item["id"],
                        "reason": "markedly softer than the rest of the shoot",
                        "keep_instead": None,
                    }
                )

    scores = sorted(score for item in items if (score := _score(item)) is not None)
    if scores:
        median_score = scores[len(scores) // 2]
        for item in items:
            score = _score(item)
            if item["id"] in claimed or item["id"] in kept or score is None:
                continue
            if score < LOW_SCORE and score <= median_score - LOW_SCORE_GAP:
                if not room_can_spare(item["id"]):
                    continue
                claimed.add(item["id"])
                # The model's reason is its own words: shown as text, never
                # as markup (docs/local-models.md, P13).
                why = " ".join(str(item.get("score_reason") or "").split())
                reason = f"the local model scored it {score}" + (f": {why}" if why else "")
                suggestions.append({"id": item["id"], "reason": reason, "keep_instead": None})

    limit = max(1, int(len(items) * MAX_SUGGEST_FRACTION))
    return suggestions[:limit]
