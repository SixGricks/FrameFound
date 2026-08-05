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


def cosine(a: list[float], b: list[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b, strict=False)))


def group_duplicates(embeddings: dict[str, list[float]]) -> list[list[str]]:
    """Connected groups of near-identical frames, greedily built. n is a
    listing (≤500), so the quadratic pass is nothing."""
    ids = list(embeddings)
    parent = {i: i for i in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for index, a in enumerate(ids):
        for b in ids[index + 1 :]:
            if cosine(embeddings[a], embeddings[b]) >= DUPLICATE_SIMILARITY:
                parent[find(a)] = find(b)

    groups: dict[str, list[str]] = {}
    for item_id in ids:
        groups.setdefault(find(item_id), []).append(item_id)
    return [members for members in groups.values() if len(members) > 1]


def suggest_removals(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Given [{id, room, sharpness, embedding}], the removals a listing can
    afford. Coverage guard: a room never loses its last photograph."""
    by_id = {item["id"]: item for item in items}
    room_counts: dict[str, int] = {}
    for item in items:
        room_counts[item["room"]] = room_counts.get(item["room"], 0) + 1

    suggestions: list[dict[str, Any]] = []
    claimed: set[str] = set()

    def room_can_spare(item_id: str) -> bool:
        room = by_id[item_id]["room"]
        remaining = room_counts.get(room, 0)
        losing = sum(1 for s in suggestions if by_id[s["id"]]["room"] == room)
        return (remaining - losing) > 1

    embeddings = {i["id"]: i["embedding"] for i in items if i.get("embedding")}
    for group in group_duplicates(embeddings):
        keep = max(group, key=lambda i: by_id[i]["sharpness"])
        for member in group:
            if member == keep or member in claimed:
                continue
            if not room_can_spare(member):
                continue
            claimed.add(member)
            suggestions.append(
                {
                    "id": member,
                    "reason": "near-duplicate, softer than the kept frame",
                    "keep_instead": keep,
                }
            )

    sharp_values = sorted(i["sharpness"] for i in items)
    if sharp_values:
        median = sharp_values[len(sharp_values) // 2]
        for item in items:
            if item["id"] in claimed:
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

    limit = max(1, int(len(items) * MAX_SUGGEST_FRACTION))
    return suggestions[:limit]
