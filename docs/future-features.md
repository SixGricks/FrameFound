# Future features — proposed plan

Drafted 2026-08-04 from a live system review, not from a wishlist. Each item
says why it earns a place, grounded in what the catalogue measures today:
25,349 assets · 8,121 faces · 5 named people · 2,230 unnamed clusters ·
6,988 located assets · 9 tags · 0 slideshows rendered.

The strongest signal in the data: **the People loop is the part being used.**
Stef Grick went 95 → 835 confirmed and DJ Grick 0 → 498 in three days of
operator time. Features that ride that momentum are worth more than features
that open a new front.

## Now — finish what has momentum

### 1. Background discovery sweeps
"Find more of them" works but is a button. Run it for every named person on
the scanner's maintenance tick, so a new scan quietly produces suggestions and
the People page says "12 found" the next morning. All the pieces exist
(`/discover`, the scheduler, `suggestion_count` on the person card); the work
is wiring and a per-person cool-down. Confirm-before-it-counts is untouched —
sweeps only ever *offer*.

### 2. A person's photographs, not just their faces
The person page shows face crops; the thing an operator actually wants next is
the *photographs* — a "See their photos" view and a `person:` filter in
Browse. Faces already join to assets, so this is a query and a page, and it is
the moment the review work starts paying rent: the reason to name people is to
find pictures of them.

### 3. Person-aware slideshows
"Slideshow of Stef" — selection filtered by confirmed faces before the styling
presets apply. The render pipeline is built and measured but has rendered
nothing; a person is the most natural first selection criterion this archive
has, and it makes the first real slideshow (still the top of "Next up") more
likely to happen than a generic one.

### 4. Re-detect the sub-40px legacy faces
Detection now refuses boxes under 40px because those embeddings are
interpolation, not identity — but the old ones are still in the database,
polluting clusters and discovery sweeps alike. The frames backlog has drained;
this is now unblocked. One task, queued low.

## Next — search gets smarter

### 5. Offline reverse geocoding (GeoNames)
Already designed in the roadmap (option 1 of the geocoding note): load
`cities500` (~10 MB) into the Postgres that already exists, nearest-neighbour
in SQL, no new service. 6,988 assets have coordinates but no place *names* —
this turns them into "photos in Lancaster" searches and gives Places captions,
all offline.

### 6. Combined filters in Browse
Person × tag × place × date-range, composable in the URL. Each exists or is
arriving separately; the archive becomes genuinely navigable when they stack —
"Stef, at the lake, 2019" is the query a family catalogue exists to answer.

### 7. "On this day"
A dashboard strip resurfacing photographs from this week in prior years.
Nearly free (`captured_at` is indexed) and it is the feature that brings an
operator back daily, which in turn feeds the review loops. Blocked only by the
59 impossible capture dates being *fixable* — see item 8.

### 8. Capture-date repair queue
59 dates are impossible (12 future, 47 pre-1990). A small review page listing
them with candidate corrections — file mtime, folder name, median of siblings
in the same folder — and the operator picks. Same philosophy as faces: the
system proposes, a human confirms, nothing is silently rewritten.

### 9. Close the real frame gap
The "1,554 assets invisible to visual search" headline decomposed on
inspection: 1,080 are audio (no frames by design), 407 are BRAW (GPU-gated).
The true gap is ~67 images/videos that should have frames and do not. Requeue
them; then the number on the dashboard means what it says.

### 10. Transcription coverage audit
116 of 3,052 videos have transcripts. Much of the footage is b-roll where VAD
correctly finds no speech, but "correctly skipped" and "never attempted" are
indistinguishable today. Record the skip verdict per asset so coverage is a
fact rather than a hope, then requeue whatever was never tried.

## Later — bigger bets, in rough order

- **GPU upgrade unlocks** — the 38 BRAW proxies waiting on it, plus larger
  Whisper models and faster embedding. The single hardware change with the
  longest feature tail.
- **Marker export from transcript hits** (M9 remainder) — a search hit lands
  on the frame in Premiere, not the file. `fcp7.Marker` is already written.
- **Mobile review** — face confirmation and date repair are swipe-shaped
  work; the web app on a phone is most of the way there. Worth a viewport
  pass before any native ambition.
- **Co-appearance** — "people photographed together", one SQL join over
  confirmed faces. Cheap, and it surfaces the family structure the archive
  actually documents.
- **Second suggestion slot per face** — only if the one-slot limitation
  starts colliding in practice; the migration is trivial but not yet earned.
- **Face vector index** — HNSW on `faces.embedding` when the candidate pool
  nears six figures; at 7,582 a scan is sub-second.

## Explicitly not planned

- Generative motion/AI styling on photographs (decided against, 2026-07-30).
- Cloud face models or external identity lookup — every name stays
  operator-typed, nothing leaves the machine.
- Street-level geocoding (Nominatim/Photon) — more service than "where was
  this" needs.

## Ops debt recorded while reviewing

- Docker build cache had grown to **70 GB** and put root at 80%; pruned to
  keep 8 GB, root now 25%. The build host needs `docker builder prune`
  on a schedule or a disk gauge on the dashboard — silent growth to full is
  the current default.
- `delete_slideshow` still writes no audit row (noted 2026-07-31).
- DJ Grick's threshold sits at 0.325 — near the 0.30 floor, driven by a
  weakest-confirmed of 0.345 with zero rejections. Not wrong, but his
  discovery sweeps run at the widest allowed bar; the first rejection he
  records will snap it tighter.
