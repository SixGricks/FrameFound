"""Reciprocal Rank Fusion (ADR-0011).

Three retrieval strategies answer every query — spoken words, filenames, and
visual similarity — and their scores are not comparable: a BM25 rank, a
substring hit, and a cosine distance live on different scales. Normalising
them against each other means inventing a conversion nobody can defend.

RRF sidesteps that by using only *position*: an item scoring 1/(k + rank) in
each list it appears in, summed. An asset that ranks moderately in two
strategies beats one that tops a single list, which is the behaviour we want
when someone searches "auction" and a clip both shows a gavel and mentions
bidding. k=60 is the value from the original Cormack et al. paper; it damps
the influence of the very top ranks so a single confident-but-wrong retriever
cannot dominate.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

RRF_K = 60


@dataclass
class FusedHit:
    key: str
    score: float = 0.0
    # Which strategies matched, in the order they contributed — this is what
    # the UI shows as "why this matched".
    reasons: list[str] = field(default_factory=list)


def fuse(
    ranked_lists: dict[str, Iterable[str]], weights: dict[str, float] | None = None
) -> list[FusedHit]:
    """Combine ranked key lists into one ordered result.

    `ranked_lists` maps a strategy name to its keys in rank order. Weights let
    an operator favour a strategy later without changing the algorithm.
    """
    weights = weights or {}
    fused: dict[str, FusedHit] = {}
    for strategy, keys in ranked_lists.items():
        weight = weights.get(strategy, 1.0)
        for rank, key in enumerate(keys):
            hit = fused.setdefault(key, FusedHit(key=key))
            hit.score += weight / (RRF_K + rank + 1)
            if strategy not in hit.reasons:
                hit.reasons.append(strategy)
    return sorted(fused.values(), key=lambda h: (-h.score, h.key))
