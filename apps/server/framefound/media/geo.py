"""Geographic helpers and location inference.

Drones stamp GPS into every frame; the DSLR or phone shooting the same job
usually does not. But those cameras were at the same place at the same time,
often pointed at the same thing — so a located asset can lend its position to
an unlocated neighbour.

The inference deliberately requires *two* agreeing signals:

1. **Time proximity** — shots minutes apart are almost certainly the same
   location; shots hours apart are not.
2. **Visual similarity** — CLIP vectors confirm the cameras were actually
   looking at the same scene, not merely running on the same afternoon.

Requiring both is what stops a morning shoot in one county from tagging an
afternoon shoot in another. Inferred positions are always recorded as
inferred, with a confidence score, and never overwrite real EXIF data.
"""

import math
from dataclasses import dataclass
from datetime import datetime

EARTH_RADIUS_KM = 6371.0

# An inferred location is only offered when both signals agree.
MAX_TIME_GAP_S = 2 * 3600.0
MIN_VISUAL_SIMILARITY = 0.55
MIN_CONFIDENCE = 0.35


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def bounding_box(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    """(min_lat, max_lat, min_lon, max_lon) for a cheap indexed pre-filter.

    Coarse by design: the box is a superset of the circle, so an exact
    haversine check still runs on the survivors. Longitude degrees shrink
    toward the poles, hence the cosine term.
    """
    lat_delta = radius_km / 111.32
    cos_lat = max(math.cos(math.radians(lat)), 1e-6)
    lon_delta = radius_km / (111.32 * cos_lat)
    return (lat - lat_delta, lat + lat_delta, lon - lon_delta, lon + lon_delta)


@dataclass(frozen=True)
class LocationCandidate:
    """A located asset offered as the source of an inferred position."""

    asset_id: str
    lat: float
    lon: float
    captured_at: datetime
    similarity: float  # cosine similarity of the two assets' frames


def confidence_for(time_gap_s: float, similarity: float) -> float:
    """Blend the two signals into one score in [0, 1].

    Time decays linearly across the window; similarity is rescaled from its
    threshold so a borderline visual match cannot alone produce a confident
    answer. The product means BOTH must hold up — which is the whole point.
    """
    if time_gap_s > MAX_TIME_GAP_S or similarity < MIN_VISUAL_SIMILARITY:
        return 0.0
    time_score = 1.0 - (time_gap_s / MAX_TIME_GAP_S)
    visual_score = (similarity - MIN_VISUAL_SIMILARITY) / (1.0 - MIN_VISUAL_SIMILARITY)
    return round(time_score * (0.4 + 0.6 * visual_score), 4)


def best_candidate(
    subject_captured_at: datetime, candidates: list[LocationCandidate]
) -> tuple[LocationCandidate, float] | None:
    """Pick the strongest location source, or None if none is convincing."""
    best: tuple[LocationCandidate, float] | None = None
    for candidate in candidates:
        gap = abs((subject_captured_at - candidate.captured_at).total_seconds())
        score = confidence_for(gap, candidate.similarity)
        if score >= MIN_CONFIDENCE and (best is None or score > best[1]):
            best = (candidate, score)
    return best
