"""Grouping located assets into places.

Coordinates in a database are not something anyone searches by. A shoot is a
place — "the Jacobs Rd job" — and the catalogue should be able to answer that
without the operator knowing a latitude.

Clustering is greedy and single-pass: an asset joins the first cluster whose
centroid is within the radius, otherwise it starts one. Proper agglomerative
clustering would produce marginally tidier boundaries, but shoots are already
well separated in practice — a job site is hundreds of metres across and the
next one is miles away — and a stable, explainable grouping is worth more here
than an optimal one.

Naming avoids reverse geocoding on purpose. An offline gazetteer would say
"Chicago, Illinois" for every one of these; the operator's own folder names
say "Feb 4 - 513 Jacobs Rd". The directory that most of a cluster's files sit
in is both more specific and already correct.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from framefound.media.geo import haversine_km

# A job site spans a few hundred metres; the next one is usually miles away.
# Wide enough to hold one property including drone orbits, tight enough that
# two shoots on the same street stay separate.
DEFAULT_RADIUS_KM = 0.75


@dataclass
class LocatedAsset:
    """Only what clustering needs, so the DB layer stays out of this module."""

    asset_id: str
    lat: float
    lon: float
    relative_path: str
    captured_at: datetime | None
    inferred: bool


@dataclass
class Place:
    lat: float
    lon: float
    members: list[LocatedAsset] = field(default_factory=list)

    def add(self, asset: LocatedAsset) -> None:
        """Add a member and move the centroid to the running mean.

        Recomputing incrementally keeps the pass single — and means a cluster
        drifts toward where its members actually are rather than staying
        pinned to whichever asset happened to arrive first.
        """
        count = len(self.members)
        self.lat = (self.lat * count + asset.lat) / (count + 1)
        self.lon = (self.lon * count + asset.lon) / (count + 1)
        self.members.append(asset)

    @property
    def radius_km(self) -> float:
        """Distance from the centroid to the furthest member."""
        return max(
            (haversine_km(self.lat, self.lon, m.lat, m.lon) for m in self.members),
            default=0.0,
        )


def _folder_of(relative_path: str) -> str:
    head, _, tail = relative_path.rpartition("/")
    return head if tail else relative_path


def name_for(place: Place) -> str:
    """The deepest folder shared by most of the cluster's files.

    Falls back through the ancestry when members are split across
    subdirectories — an `a-roll` and a `stills` folder under one shoot should
    be named for the shoot, not for whichever subfolder is larger.
    """
    # Files sitting at the library root have no folder to be named after, so
    # they drop out rather than contributing an empty name.
    folders = [f for f in (_folder_of(m.relative_path) for m in place.members) if f]
    if not folders:
        return "Unknown location"

    common = Counter(folders).most_common(1)[0]
    if common[1] * 2 > len(folders):  # a clear majority sit together
        return common[0].rpartition("/")[2] or common[0]

    # No majority: walk up to the deepest ancestor that covers everyone.
    segments = [f.split("/") for f in folders]
    shared: list[str] = []
    for parts in zip(*segments, strict=False):
        if len(set(parts)) != 1:
            break
        shared.append(parts[0])
    return shared[-1] if shared else "Unknown location"


def cluster(assets: list[LocatedAsset], radius_km: float = DEFAULT_RADIUS_KM) -> list[Place]:
    """Group assets into places, largest first.

    Sorted by capture time before clustering so the grouping is deterministic
    for a given library rather than dependent on database row order.
    """
    places: list[Place] = []
    ordered = sorted(assets, key=lambda a: (a.captured_at is None, a.captured_at or datetime.min))
    for asset in ordered:
        for place in places:
            if haversine_km(place.lat, place.lon, asset.lat, asset.lon) <= radius_km:
                place.add(asset)
                break
        else:
            places.append(Place(lat=asset.lat, lon=asset.lon, members=[asset]))
    places.sort(key=lambda p: -len(p.members))
    return places
