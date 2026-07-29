# Location

How FrameFound knows where your media was shot, and what it does with that.

For the optional Google Maps layer on top of this, see [maps.md](maps.md).

---

## Where positions come from

**EXIF.** Drones and phones stamp GPS into every file. This is read during
metadata extraction and stored on the asset as `gps_lat` / `gps_lon` with
`gps_source` left empty, meaning "the camera said so".

**Inference.** Cinema cameras, most DSLRs, and audio recorders record no
position at all — but they were standing next to the drone that did. A shot
with no GPS can borrow a position from one that has it, provided **two
independent signals agree**:

1. **Time** — the two were captured within two hours of each other.
2. **Appearance** — their CLIP embeddings are similar enough to believe the
   cameras were pointed at the same scene.

Requiring both is the whole design. Time alone would tag a morning job in one
county with an afternoon job in another. Appearance alone would tag every
green fairway with the coordinates of every other green fairway.

### Confidence

Both signals feed one score in `[0, 1]`:

```
time_score   = 1 - (gap / 2 hours)
visual_score = (similarity - 0.55) / 0.45
confidence   = time_score × (0.4 + 0.6 × visual_score)
```

Nothing below **0.35** is recorded. Because the two terms multiply, a
borderline visual match cannot be rescued by good timing, or the reverse.

Thresholds live in `framefound/media/geo.py`:

| Constant | Default | Meaning |
|---|---|---|
| `MAX_TIME_GAP_S` | 7200 (2 h) | Beyond this, no relationship is assumed |
| `MIN_VISUAL_SIMILARITY` | 0.55 | Cosine similarity floor |
| `MIN_CONFIDENCE` | 0.35 | Below this, nothing is written |

### Rules that will not be relaxed

- **EXIF is never overwritten.** Inference only fills gaps.
- **An inferred position never anchors another inference.** Anchors are
  EXIF-positioned assets only, so every inferred position is exactly one hop
  from ground truth. Chaining carries no confidence penalty, so without this
  rule a guess built on a guess would score as highly as real data, and drift
  would compound silently on every pass. (This was live briefly: a second run
  filled 18 extra assets purely from the first run's output.)
- **Everything inferred is labelled.** `gps_source = 'inferred'`,
  `gps_confidence` records the score, and `gps_inferred_from` records which
  asset lent the position. The UI badges them, and Places can filter them out
  entirely.

### Performance

Inference is a matrix operation, not a nested loop. Anchors are sorted by
capture time and a bisect selects only the slice inside the two-hour window,
which on real footage is a handful rather than thousands. Similarity across
that slice is one dot product, since embeddings are L2-normalised at write
time.

Measured on the production library: 3,840 anchors against 4,809 candidates in
**under a second**. The naive form — every candidate against every anchor —
was roughly ten billion float operations and never completed.

### Running it

Inference runs as a Celery task per library, on the `vision` queue:

```python
from framefound.processing.tasks import infer_locations
infer_locations.delay(str(library_id))
```

It requires embeddings on both sides, so it should run after visual indexing
has finished. It is idempotent: re-running fills only what is still empty.

---

## Places

Located assets are grouped into the shoots they came from.

Clustering is greedy and single-pass: an asset joins the first cluster whose
centroid is within the radius (default **750 m**), otherwise it starts a new
one, and the centroid moves to the running mean as members arrive. Input is
sorted by capture time first, so the grouping is deterministic rather than
dependent on database row order.

Proper agglomerative clustering would draw marginally tidier boundaries, but
job sites are already well separated — a property is hundreds of metres
across and the next one is miles away — and a stable, explainable grouping is
worth more here than an optimal one.

Clusters are computed **on demand**, not stored. Persisting them would mean
invalidating on every scan and every inference run, and the located set is
small enough that recomputing is cheaper than keeping a cache honest.

### Naming

Places are named from **your folder structure** — see
[maps.md § How naming works](maps.md#how-naming-works) for the full order of
precedence and why folder names beat reverse geocoding.

### Browsing

- **Places** lists every cluster with an asset count, date range, and how many
  of its positions were inferred.
- Clicking a place (card or map marker) opens it as a library view with media
  type, position-source and sort filters, and paging.
- The URL carries the coordinate and radius rather than an index, so links
  survive a re-cluster and can be shared.

---

## Tuning

If inferred positions look wrong, the two dials worth touching are in
`framefound/media/geo.py`:

- **Too many wrong positions** → raise `MIN_VISUAL_SIMILARITY` (0.55 → 0.65)
  or `MIN_CONFIDENCE` (0.35 → 0.5).
- **Too few positions filled** → lower `MIN_CONFIDENCE`, or widen
  `MAX_TIME_GAP_S` if your shoots genuinely run longer than two hours.

After changing a threshold, clear and re-run:

```sql
UPDATE assets SET gps_lat = NULL, gps_lon = NULL, gps_source = NULL,
                  gps_confidence = NULL, gps_inferred_from = NULL
WHERE gps_source = 'inferred';
```

Then re-queue `infer_locations` per library. Only inferred positions are
cleared; EXIF data is untouched.

The honest way to judge a threshold is to open Places, filter to **inferred
only**, and look at a shoot you recognise.

---

## Reference

| | |
|---|---|
| Inference logic | `framefound/media/geo.py` |
| Inference task | `framefound/processing/tasks.py::infer_locations` |
| Clustering | `framefound/media/places.py` |
| API | `GET /api/v1/places`, `GET /api/v1/assets/near` |
| Asset columns | `gps_lat`, `gps_lon`, `gps_source`, `gps_confidence`, `gps_inferred_from` |
