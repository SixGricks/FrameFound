"""Grouping faces into people, and learning from corrections.

Same shape as `ai/tagging.py`, for the same reasons: nearest-centroid needs no
training, runs on a CPU, and gets better every time the operator corrects it.

The differences from tagging are what matter here:

- **There is no zero-shot start.** A tag can bootstrap from its own words
  because CLIP puts text and images in one space. A face cannot: "Brian" tells
  an embedding model nothing. So a person begins as a cluster of faces that
  look alike, unnamed, and the operator supplies the name afterwards.
- **The threshold is tighter.** A wrong tag is a filing error. A wrong face
  puts one person's photograph in another person's album, which is the kind of
  mistake that makes the whole feature untrustworthy.
- **Rejections are per person, not global.** Two siblings genuinely look
  alike; saying "this is not Brian" must not also mean "this is nobody".
"""

from dataclasses import dataclass, field

# Cosine similarity between ArcFace embeddings of the same person typically
# lands above 0.5; different people below 0.3. 0.42 sits in the gap, chosen
# toward the strict end because a false merge is worse than a missed one.
DEFAULT_THRESHOLD = 0.42
# Never widen past this, whatever the examples suggest. Below it, the model is
# not distinguishing people any more.
MIN_THRESHOLD = 0.30
MAX_THRESHOLD = 0.75
# A cluster this small is usually one bad detection. Kept, but not offered for
# naming until it has corroboration.
MIN_CLUSTER_SIZE = 2


@dataclass
class FaceVector:
    face_id: str
    embedding: list[float]
    asset_id: str


@dataclass
class Cluster:
    centroid: list[float]
    members: list[FaceVector] = field(default_factory=list)

    def add(self, face: FaceVector) -> None:
        count = len(self.members)
        self.centroid = [
            (c * count + v) / (count + 1)
            for c, v in zip(self.centroid, face.embedding, strict=False)
        ]
        self.members.append(face)


def similarity(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity. Both sides are L2-normalised, so this is a dot."""
    if not a or not b:
        return 0.0
    return float(sum(x * y for x, y in zip(a, b, strict=False)))


def cluster_faces(faces: list[FaceVector], threshold: float = DEFAULT_THRESHOLD) -> list[Cluster]:
    """Greedy single-pass grouping, largest clusters first.

    Faces are compared against running centroids rather than every member, so
    this stays linear in the number of clusters. On a library where one person
    appears in hundreds of shots that matters: the alternative is quadratic in
    faces, and there are more faces than assets.
    """
    clusters: list[Cluster] = []
    for face in faces:
        best: Cluster | None = None
        best_score = threshold
        for cluster in clusters:
            score = similarity(cluster.centroid, face.embedding)
            if score >= best_score:
                best, best_score = cluster, score
        if best is None:
            clusters.append(Cluster(centroid=list(face.embedding), members=[face]))
        else:
            best.add(face)
    clusters.sort(key=lambda c: -len(c.members))
    return clusters


def prototype_for(embeddings: list[list[float]]) -> list[float] | None:
    """Mean of a person's confirmed faces, renormalised.

    Renormalising matters: the mean of unit vectors is not itself a unit
    vector, and comparing against a shorter vector would quietly depress every
    similarity score and make the person harder to match over time.
    """
    usable = [e for e in embeddings if e]
    if not usable:
        return None
    dims = len(usable[0])
    total = [0.0] * dims
    for vector in usable:
        for i, value in enumerate(vector):
            total[i] += value
    mean = [v / len(usable) for v in total]
    norm = sum(v * v for v in mean) ** 0.5
    return [v / norm for v in mean] if norm > 0 else mean


def threshold_for(
    prototype: list[float] | None,
    confirmed: list[list[float]],
    rejected: list[list[float]],
) -> float:
    """Derive the match bar for one person from their own corrections.

    Low enough to admit the least similar face the operator confirmed, high
    enough to exclude the most similar one they rejected. When those two
    conflict — which happens with siblings — the rejection wins, because
    offering a wrong person is the failure that loses trust.
    """
    if prototype is None:
        return DEFAULT_THRESHOLD

    floor = min((similarity(prototype, e) for e in confirmed if e), default=None)
    ceiling = max((similarity(prototype, e) for e in rejected if e), default=None)

    if floor is None and ceiling is None:
        return DEFAULT_THRESHOLD
    if ceiling is None:
        # Sit just under the weakest accepted face so it still matches.
        return _clamp((floor or DEFAULT_THRESHOLD) - 0.02)
    if floor is None:
        return _clamp(ceiling + 0.02)
    if floor > ceiling:
        # A clean gap: sit in the middle of it.
        return _clamp((floor + ceiling) / 2)
    # Overlapping. Exclude the rejection and accept that the weakest confirmed
    # face may need confirming again.
    return _clamp(ceiling + 0.02)


def _clamp(value: float) -> float:
    return max(MIN_THRESHOLD, min(MAX_THRESHOLD, round(value, 4)))
