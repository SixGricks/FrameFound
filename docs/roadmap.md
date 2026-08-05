# Roadmap & MVP Backlog

Versioning: SemVer. MVP = end of Milestone 8 ≈ v0.9; v1.0 after beta feedback.
Each milestone maps to a GitHub Milestone; items become issues at milestone start.

## Where things stand — 2026-07-29

Milestones were not completed strictly in order: deployment happened early
(which surfaced eleven real bugs), and backup/restore was pulled forward
because a production install without it is negligent. What follows reflects
reality rather than the original sequence.

### Done and running in production

| Milestone | State | Notes |
|---|---|---|
| **M0** Architecture | ✅ | 10 accepted ADRs, threat model, licence inventory |
| **M1** Foundation | ✅ | auth, migrations, queues, CI/release, health checks |
| **M2** Indexing | ✅ | 15,774 assets across 4 libraries incl. the 18 TB GELCO share |
| **M3** Proxies & previews | ✅ | thumbnails, posters, H.264 proxies, signed URLs |
| **M4** Transcription | ✅ | faster-whisper + VAD, sidecar import; a retry sweep now catches work that fails and is forgotten |
| **M5** Visual search | ✅ | CLIP ViT-B/32 via ONNX, pgvector HNSW, similar-assets |
| **M6** Web UI alpha | ✅ | search, browse, asset detail, places, storage, tags, dashboards |
| **M7** Remote access | ✅ | 2FA, sealed secrets, DDNS, kill switch, sessions, Tailscale enrolment |
| **M8** Hardening | ✅ | backup/restore, image scanning + SBOMs, benchmarks, failure drills (9/9) |
| **M9** Editorial handoff | 🔶 started | panel tokens, `/panel` API, Premiere UXP + Lightroom Lua clients written (untested in-host) |

### Measured state of the production install — 2026-07-29 17:45 UTC

| | |
|---|---|
| Assets indexed | **15,774** across 4 libraries |
| — GELCO (18 TB share) | 6,820, scan complete |
| — Intel 2026 / Promo / Breeze | 8,954 |
| Thumbnails ready | 10,694 and climbing |
| Located — EXIF | 4,510 (up ~600 as GELCO metadata lands) |
| Located — inferred | 264, across 67 places |
| Transcripts | 29, backlog re-queuing after the retry-sweep fix |
| Geocode cache | 0 rows — no Google keys configured, by choice |
| Queues | metadata 5,042 · vision 1,749 · transcribe 18 — all draining |

### In progress

- **GELCO processing** — scan complete at 6,820 assets; metadata, thumbnails
  and embeddings are draining now, with location inference to follow. First
  library at real scale, and the honest benchmark for everything after.
- **Transcript backlog** — the retry sweep is feeding the 40 assets that
  failed on the old permission fault back through, 25 at a time.

### Needs attention

- **RESOLVED: the scanner never called face clustering.** `_cluster_new_faces`
  was defined and had passing tests; `await _cluster_new_faces(db)` appeared
  nowhere in the loop. Two earlier "fixes" — removing a queue-busy guard and
  threading observer setup — were both real improvements and neither was the
  cause, because the code was never reached.

  The insertion had been made with a Python string replace anchored on a block
  that no longer matched the file, and `.replace()` silently does nothing when
  its anchor is absent. Verification then checked the wrong thing: that the
  function existed, not that it was called.

  `tests/test_scanner_wiring.py` now parses the loop and asserts every
  maintenance sweep is named in it, plus the inverse — a `_requeue_*` or
  `_cluster_*` coroutine that nothing calls fails the suite. Checked to fail
  with the call removed.

- **21 damaged files, 127 GB** — found by the QA sweep, not a FrameFound bug.
  All fail with "moov atom not found": recordings interrupted before the camera
  finished writing the container. They will not open in Premiere either. One is
  125 GB (`Brian Job and Promo/2023 11 21 Camera 2`), fifteen are DJI drone
  clips, and four are 24–48 byte stubs. Repair tools sometimes recover this;
  FrameFound now says so instead of reporting a generic failure.

- **80 failed derivatives.** Four are the 1–2 GB TIFF panoramas that exceed
  the worker's 1.27 GB memory limit (waiting on the RAM upgrade, and now
  reported honestly as running out of memory). The rest are mostly DJI MP4
  proxy failures and the BRAW proxies deferred until a GPU exists — worth a
  pass to confirm nothing else is hiding in there.
- **The "5.2 MB/s share" was a single-stream figure, and it was not the
  constraint.** Three parallel readers pull **17 MB/s** from the same NAS.
  Timeouts throughout the codebase are still sized against 5.2 MB/s, which is
  merely conservative rather than wrong, but the belief that frames was
  IO-bound sent tuning in the wrong direction for a while — see the frames
  entry below.

### Hardware upgrade complete — 2026-07-31

6 GB → **58 GB RAM** and a 2.4 TB second disk. Full detail and the measured
numbers in [storage.md](storage.md); the parts that change decisions:

- **Neither disk is faster for random reads** (326 vs 315 IOPS), so Postgres
  stayed on `sda`. The plan had said to move it if the new drive won; it did
  not. `/data` moved to `sdb`, root went 30 GB → 36 GB free.
- **Both disks are ~320 IOPS at ~50 ms** — spinning-rust territory. Worth
  knowing before blaming software for a slow query.
- **Container limits rewritten** against real memory. `worker-vision` now sits
  at 2.2 GB in normal use, which it could never have reached under the old
  1200M ceiling — that limit was silently shaping behaviour, not just guarding
  it.
- **Postgres tuned** for the first time (shared_buffers 128MB → 2GB). pgvector
  HNSW search measures 3.2 ms warm median at a 99.9% buffer cache hit rate.
- **Frames: 60 -> 1,080 jobs/hour, and the diagnosis was wrong twice.**
  Raising concurrency moved the bottleneck off the share and onto CPU, at which
  point the obvious answer ("it's the network") was no longer true — measured
  mid-run, worker-media was pegged at 405% of a 4-core cap with the host 67%
  idle and iowait at 1.1%.

  Two changes, measured separately. First, the scene-detect guard weighed
  duration but not resolution, so a two-minute 4K drone original with no proxy
  got a complete decode over SMB — about three minutes each, to find no cuts at
  all, because an aerial shot is one continuous take. Gating on pixels too took
  it to 330/hour. Then raising the CPU cap 4 -> 6 took it to 1,080/hour, at
  which point the container sits at ~507% of 600% and is no longer capped.

  Backlog went from roughly ten days to about thirteen hours.

**Follow-up found while verifying:** `delete_slideshow` is admin-only and
destructive but writes no `audit_log` entry, unlike session and panel-token
revocation. A deleted slideshow currently leaves no trace of who removed it.

### 2026-07-31 — new library, and the basemap actually draws

**Grick Family Storage** added: the `Grick Family Storage` share on
SixGricksServer (the same NAS as the other shares), mounted read-only at
`/mnt/media/family`, scanned to completion at **9,392 files**. The catalogue is
now **25,349 assets**. Excludes `#recycle`, `@eaDir` and `ha_backup_home` — Synology
plumbing and Home Assistant backups, none of it media. It ships with a path
profile for `W:\`, so the Premiere and Lightroom panels can hand back a path
that opens on that machine.

**The retried derivatives mostly worked.** Of 42 requeued after the memory
upgrade, **40 succeeded** — failures fell 101 → 61. The remaining 61 are the
59 that were never retried (38 BRAW proxies waiting on a GPU, 20 genuinely
damaged files) plus 2 real failures. The theory that most of those were the
memory ceiling rather than bad files held up.

**A downloaded basemap now draws a map.** Reported as "no map on Places
despite a downloaded basemap", and it was not a configuration slip — the
feature was half-built. Downloading an archive and *using* one were two
unconnected settings with nothing linking them, and underneath that FrameFound
served no style JSON at all, so switching the provider by hand would not have
helped either. MapLibre cannot render a `.pmtiles` file without a style.

Tiles are now unpacked server-side and served as ordinary `{z}/{x}/{y}` vector
tiles. The alternative, the `pmtiles://` protocol, needs a second JavaScript
library from a CDN before a single tile can be read — an odd dependency for a
feature whose selling point is working without the internet. "Use this map" on
the Basemaps page wires Places to it in one click.

Labels are deliberately absent: they need font glyphs, which means hosting a
few megabytes of PBFs or fetching them from someone else's server, and the
second would undo the promise. Rivers and roads are what a photograph needs
behind it.

### Geocoding without Google — the options

Asked directly, so recorded here. Reverse geocoding ("which town is this
photograph in") has three self-hosted shapes, and the cheapest is the one that
fits this project:

1. **A local place dataset, no service at all.** GeoNames publishes
   `cities500` (~10 MB) and richer sets up to a few hundred megabytes:
   name, country, admin region, population, coordinates. Loaded into the
   Postgres that already exists, a nearest-neighbour lookup answers town,
   county and state offline, in microseconds, with no new container. This
   covers essentially everything a photo catalogue asks of a geocoder and is
   the **recommended** route.
2. **The basemap already downloaded.** Protomaps v4 carries a `places` layer
   with named settlements, so the Pennsylvania archive on disk *already*
   contains the answer for that region. Neat, and worth considering as a
   refinement, but reading it means querying vector tiles rather than a table.
3. **Nominatim or Photon** — the real OSM geocoders, both self-hostable and
   both a genuine service: Nominatim wants PostGIS and a heavy import, Photon
   wants Elasticsearch. Either would give street-level addresses, which is
   more than "where was this" needs, and both contradict the no-extra-service
   posture that made PMTiles the right basemap choice.

Google stays what it is today: optional, off, and one provider among several.

### 2026-08-01 — the "230 processing" counter, and what it hid

Reported as a hung counter. Nothing was hung: all 230 assets had failed
deterministically, been retried to exhaustion, and had nowhere to go.

**872 of 876 failures were one error.** ExifTool writes the literal string
`undef` for a tag it cannot read — even under `-n` — and `probe.py` copied
every mapped value straight into a `FLOAT` column:

    invalid input for query argument $16: 'undef' (must be real number, not str)

The other four were the same mistake from the opposite side: `fmt["duration"]`
raises `KeyError` on a container without one, and the suppressor there listed
only `TypeError` and `ValueError`, so the entire traceback message was
`'duration'`. Every probed field is now coerced at the boundary, and anything
that will not coerce is **dropped rather than defaulted** — a missing aperture
is honest, an aperture of 0.0 is a lie that gets believed.

**Why it read as progress.** `processing_status` is set on the way in and only
cleared on success, so a failure leaves it at `processing` forever and the word
comes to mean both "in flight" and "died and nobody noticed". Failures now
reach a terminal status the UI already had a label for. Re-queued: **230 → 1**,
and the one is genuinely running.

**1,132 orphaned job rows.** Separately, jobs sat in `running` indefinitely —
699 `index_visual_batch`, 229 `generate_derivatives`, 165 `transcribe_asset`,
some three days old — because the process that would have written an outcome
was gone. A sweep now closes them as **`abandoned`** rather than `failed`:
nobody observed a failure, and recording one would invent a verdict. Registered
in `REQUIRED_SWEEPS`, so the wiring test fails if it is ever defined and not
called.

**The People page was showing the wrong hundred.** It ordered by
`Person.face_count`, which counts *confirmed* faces only, so all 2,205 unnamed
clusters tied at zero and the first hundred came back in planner order. That is
the exact inverse of the triage the page exists for. Now ordered by real group
size.

One incidental trap worth remembering: a comment line beginning `# type:` is a
PEP 484 type comment. mypy parsed the prose after it and reported "Invalid
syntax" on a line that is visibly just a comment, while ruff and CPython both
accepted the file.

### 2026-08-04 — system review

**The build cache filled the root disk to 80%.** 70 GB of Docker build cache
from repeated image builds — the 2.4 TB data disk sat at 1% while root
quietly climbed. Pruned keeping 8 GB of recent layers; root is back to 25%
(73 GB free). The real fix is a scheduled prune or a disk gauge on the
dashboard; silent growth to full is currently the default.

**The People loop is being used, hard.** In three days of operator time:
Stef Grick 95 → 835 confirmed, DJ Grick 0 → 498, five named people total.
The remaining unnamed work is 2,230 clusters, but the leverage is uneven —
the 27 clusters of 20+ faces hold 958 faces, while 1,127 tiny clusters hold
2,661. The People page ordering already surfaces the big ones first.

**"1,554 assets invisible to visual search" was mostly by design.** It
decomposes: 1,080 audio files (frames are not a thing audio has), 407 BRAW
(decode waits on the GPU), and only **~67 real gaps** — 33 jpg, 30 mp4, a few
strays — worth requeuing. The headline number was technically true and
practically misleading.

**All 62 failed derivatives are now attributed**: 38 BRAW proxies gated on
the GPU, 22 damaged-file posters/waveforms, 1 no-frame video, 1 BRAW decode
failure. Nothing else was hiding in there; the "worth a pass" item is closed.

**Transcription coverage is 116 of 3,052 videos** — plausibly correct (b-roll
has no speech to find, and VAD skips it) but unverifiable, because a
correctly-skipped video and a never-attempted one look identical. Worth
recording the skip verdict per asset.

Future work now lives in **[future-features.md](future-features.md)** — a
reviewed plan grounded in these numbers, split Now / Next / Later.

### 2026-08-04 — real-estate vertical: spike measured, listing export shipped

Phases 0–1 of [real-estate-editing.md](real-estate-editing.md) are done.
The spike settled the open hardware question with numbers: sky segmentation
runs at **0.77 s/image** on the Westmere Xeons (CPU-viable today) and LaMa
inpainting at **18.3 s per 512px tile** (a queued batch step until the GPU,
exactly as scoped). Both execute correctly through the same AVX-free
onnxruntime path CLIP uses.

**Listings shipped end-to-end**: migration 0016, a 15-room zero-shot
classifier over embeddings the catalogue already stores, canonical
walk-through arrangement with drag-to-reorder and per-photo overrides, and
an export task that writes `01_front_exterior.jpg …` into a zip — EXIF
orientation fixed, ICC flattened to sRGB, sized to 3840px/q85 by default.
Room labels are suggestions until confirmed, videos stay out of the photo
zip, and an unreadable file is skipped *by name* while the numbering closes
ranks. Deleting a listing audits who did it.

**2026-08-06: parity measured against the operator's own published work.**
Eleven photographs from a live intelauctions.com gallery paired with their
NAS originals by filename. Brightness parity is exact (engine 113.0 vs
published 113.2); chroma reaches 84% of the published push; the visible gap
on overcast shots turned out to be sky replacement, not colour. The tuned
values live as `develop.LISTING_PRESET`, and the auto-edit flow now takes a
sky choice and works with or without an API key — folder in, near-final
photos out, operator tweaks last. Full numbers in real-estate-editing.md.

**2026-08-05, evening: the operator's field test came back.** Three findings
from real use, all addressed the same day: listings now build from **shoot
folders** (search the address, tick the photos, direct children only so an
MLS/ sibling folder cannot double a shoot); the develop engine gained
**auto-WB neutralisation** (measured on the photo's own likely-neutral
surfaces, bounded so a sunset survives) and **large-radius local contrast**;
and **AI auto-edit** landed — Claude vision reads a 768px preview per photo
and returns slider values, the engine renders them locally at full
resolution. Key sealed on the Security page, presence-only in the API,
photos leave only when the button is pressed. Eight licensed Adobe Stock
skies were named and installed into the sky library. Reviewed on request:
Cloudinary (wrong tool), Imagen AI API (strong; proposed as a premium
backend — see real-estate-editing.md). Reference target recorded: the
professionally-edited MLS finals of 5096 Old Philadelphia Pike.

**2026-08-05, later: object removal shipped (Phase 4).** Brush over the
object in the editor, press Remove; LaMa runs crop-around-the-mask at 512
on the media queue (~20-40 s per region, measured), and the result becomes
the photograph's new base — every recipe renders on top, in preview and
export alike. Removals are a versioned chain with newest-only undo, the
painted mask is kept as provenance of exactly which pixels are invented,
and a mask over half the frame is refused: it removes objects, it does not
repaint scenes. The original never changes.

**2026-08-05: the two Fotello gaps named in the quality review closed the
next morning** — *Straighten* and *Verticals* geometry sliders (rotate with
inscribed-crop, projective keystone; no black corners, output size stable)
and a single-frame *Window pull* (blurred-luminance local tone compression:
the window darkens as a region, its internal detail keeps contrast). All
three live in the same recipe, so batch apply and export honoured them from
the first minute. Bracket fusion stays on the list as the ceiling for
sensor-clipped panes. Incidentally verified: the VM rebooted mid-session
and all twelve services returned unattended on their restart policies.

**Phase 3 shipped: sky replacement.** SegFormer segmentation through the
proven AVX-free ONNX path, a deterministic compositor (cover-fit, feathered
join, bounded relight toward the sky's tone), and an operator-supplied sky
library — FrameFound ships no skies and fetches none. The sky choice lives
inside the develop recipe, so versioning, batch apply and export support
came free; under 2% detected sky the composite is a silent no-op, which is
what makes batch-applying one look across a listing safe for the hallway
photos. A quality review against Fotello is recorded in
[real-estate-editing.md](real-estate-editing.md): global correction and sky
are at or near parity for exteriors; the visible gaps are window pull
(needs bracket fusion) and vertical correction (small, next).

**Phase 2 shipped the same day: the develop editor.** Non-destructive
colour correction as append-only recipe versions (migration 0017) — eight
sliders plus bounded auto-levels, rendered by one Pillow/numpy engine that
serves both the interactive preview and the export, so what the operator
saw is what the zip contains. "Apply to whole listing" writes one look
across a shoot in a gesture. Originals are never written; revert deletes
recipes, which were never pixels. In passing this surfaced a real bug:
`embed_text` raised a raw `ModuleNotFoundError` on servers without the ai
extra, so creating a listing would have 500'd instead of degrading.

### Still worth attention

- **59 capture dates are impossible** (12 in the future, 47 before 1990). EXIF
  garbage that will mis-order any chronological slideshow. A repair queue is
  sketched in future-features.md.
- **390 videos still have no duration** after re-extraction.
- **~67 assets genuinely missing frame rows** (see above) — requeue them.
- **DJ Grick's threshold is 0.325**, near the 0.30 floor: weakest confirmed
  0.345, zero rejections. His first recorded rejection will tighten it.

### Next up

1. **Make a slideshow.** The render pipeline is built and measured; the
   Rainforest Falls VBS material is the first real test of whether the
   *selection* picks well, which is the part no test can answer.
2. **Tag the things you care about.** The mechanism is proven — one DJI aerial
   produced 52 correct suggestions out of 60. It gets useful once real tags
   exist.
3. **Inference tuning** (#34) against places you can verify.
4. **Load the panels in their hosts.** The Premiere UXP panel and the
   Lightroom Classic plugin are written and their server side is tested, but
   neither has been run inside its application — see [panels.md](panels.md).
   That first run is the remaining unknown.
5. **M9 remaining**: marker export from transcript hits, so a search result
   lands on the frame rather than the file (`fcp7.Marker` is waiting for it).
6. **Fixture corpus** (#26) — needs real RAW/HEIC samples.
7. **Next 16 upgrade** as its own piece of work (PRs #8/#11 held together).

### Design decisions taken (2026-07-30)

| Question | Decision |
|---|---|
| Slideshow theming | CLIP selection + styling presets + generated title cards. **Not** generative motion on the photos. |
| Face review | Confirm before it counts |
| Render output | VM data directory |
| Slideshow music | Operator supplies licensed tracks |

### In progress — face recognition

**Two bugs found by looking at production rather than trusting green tests:**
`detect_faces` existed, was tested, was deployed, and nothing ever called it —
0 faces after a full pass. Then once wired, the SCRFD decoder read bounding
boxes out of the keypoint tensors and found zero faces in the entire library
without erroring once. Both fixed; the decoder now has 13 tests written
against the model's real output shapes.

**A third, reported by looking at the People page:** the grid was full of
random objects. Not a detector fault — those faces average 0.73 confidence —
but a display one, wrong twice over. Faces are detected in *sampled frames*,
and 161 of 307 came from frames partway through a video, while the tile cropped
from the *asset's* thumbnail: a different picture entirely. Underneath that,
`object-fit: cover` had already cropped the tile before any box maths applied.
Crops now come from the API, cut out of the correct frame. Nothing is stored —
they are computed per request, so the promise that there is no second copy of
everyone's face still holds.

The same look turned up a quality problem: the smallest accepted boxes were
0.013 × 0.038 of the frame, about seven pixels across, and ArcFace upscales its
input to 112×112. Those embeddings were interpolation rather than identity, and
they were polluting clusters. Detection now requires 40px as well as a minimum
frame fraction. **Existing faces below that are still in the database** — a
re-detect would clear them, and is worth doing once the frames backlog drains.


**On by default**, per the operator's decision, with a real off switch. Landed
so far: the `people`/`faces` model, SCRFD + ArcFace via ONNX (the same AVX-free
path as CLIP, so it runs on the Westmere Xeons), greedy clustering, and
per-person thresholds derived from corrections. 16 tests on the grouping logic.

API landed (naming, confirm, reject, merge, forget — 13 tests), and the People
page with it.

**Review at the scale the catalogue actually produces.** The first review UI
asked for one click per face, which is fine for a dozen and absurd for what
production held: Stef Grick alone had 295 unreviewed faces against 95
confirmed. Two things were wrong underneath the clicking.

The queue was *sorted by detection score* — the detector's confidence that the
box contains a face at all, which says nothing about whose face it is. A
ranked-looking grid in effectively random identity order has no boundary to
draw a line at, so there was no prefix worth confirming in bulk and the
operator was forced through it one at a time. Sorting by similarity to the
person gives the grid a single yes/no boundary, and "down to here" turns the
whole queue into one gesture. Confirmation sends the *similarity* rather than
the face ids, so it settles the faces below the fold too — the page holds 600,
the queue may hold more, and a bulk action that silently covered only what was
loaded would be worse than one that refused.

The second gap was that nothing ever went *looking*. Clustering compares a face
only to the group it landed in, so its own near-misses sit in other groups
forever and no amount of confirming reaches them. `POST /people/{id}/discover`
scans every face that is nobody yet — 855 loose plus 6,727 across 2,235 unnamed
clusters — against the person's prototype, which `_relearn` has already
sharpened from each confirmation. Each sweep is better than the last, which is
the learning loop the operator asked for.

**A suggestion deliberately does not move a face.** Reaching across the whole
catalogue means offering faces out of clusters that belong to nobody yet; if
rejecting one left it attached to the person it is *not*, careful review would
destroy data. So the suggestion rides alongside on `faces.suggested_person_id`
and only *accepting* moves anything — and that FK is `SET NULL`, because
`CASCADE` would make "forget this person" delete every face the system had
merely wondered about. Refusals are kept and fed back to `_relearn` as
negatives: a face the search ranked highly and the operator still rejected is
the most informative negative there is, and it is exactly the case a threshold
learned only from cluster members never sees.

**Sorting by a column is only a fix if the column is populated.** It was not.
A face that joined when its *cluster* was named had never been compared to
anybody, so it carried a NULL score — all 295 of Stef Grick's unreviewed faces,
which is to say the entire case this work exists to fix. `_relearn` now
re-scores a person's faces whenever it recomputes their prototype, which is
also the only honest moment to do it: every confirmation moves the prototype,
so a score from three corrections ago is stale. That closed a hazard in the
page too — confirming a prefix sends the boundary face's similarity as a bar,
and an unscored boundary sent 0, which every pending face clears.

Measured after the operator ran it: Stef Grick 95 → 510 confirmed, the 295
queue and 120 discovered faces cleared in a handful of gestures, emptying five
unnamed clusters. 81% of those 510 sit at 0.65+ similarity and only six below
0.50, so the bulk path is not buying speed with accuracy.

Known limitations:
- One suggestion slot per face, so a face refused for one person is not offered
  to another. At 7,582 candidates and 120 per sweep the collision rate is
  negligible; a second slot is not worth a migration until it bites.
- **Brian Ley's grouping contradicts itself** — weakest confirmed face at 0.09
  similarity against rejections up at 0.73, which pins his threshold to the
  0.75 ceiling. Confirmed before any ranking existed. Worth re-reviewing now
  that the queue can be sorted; the ranking makes the bad ones obvious.
- `faces.embedding` has no HNSW index, so a sweep sequentially scans the
  candidate pool. At 7,582 that is sub-second and the filter would defeat the
  index anyway. Revisit if the pool reaches six figures.

Design notes worth keeping:
- **No zero-shot start.** A tag bootstraps from its own words because CLIP
  shares a space with text; "Brian" tells a face model nothing. So a person
  begins as an unnamed cluster and the operator supplies the name.
- **The threshold is a heuristic; the rejection record is the guarantee.** A
  face more similar than the 0.75 ceiling cannot be excluded by threshold
  alone, so rejections are stored per person and enforced explicitly. Two
  siblings are the case that forces this.
- **No face crops are stored.** The frame is already on disk and the box is
  enough to render a thumbnail, so the most sensitive data in the system is
  not duplicated.
- **Nothing leaves the machine.** No pre-trained identity set, no external
  lookup, no network call at inference. Every name was typed by the operator.
- Turning it off stops detection and suggestion but does **not** delete the
  names already given; losing that work on a toggle would be its own bug.

### Recently landed

- **Two bugs the operator found, both real** — the Manage dropdown rendered
  inside `.navlinks`, which scrolls horizontally, so an absolutely-positioned
  panel was clipped to a 40px-tall row and invisible; and `worker-ai` consumed
  `transcribe` *and* `vision` at concurrency 1, so ~6,300 sub-second embedding
  jobs queued behind minutes-long transcriptions and processing looked stalled.
  Vision now has its own worker.

- **QA sweep** — twelve data-integrity checks over the live catalogue; eleven
  clean. The twelfth asked why 72 assets were `ready` with no thumbnail and
  found 21 damaged files (127 GB): recordings interrupted before the camera
  finished writing the container. FrameFound now names that plainly instead of
  reporting a generic failure — 20 of the 21 already updated.
- **Tags are searchable** — the QA sweep found the obvious hole: tagging
  existed but search did not know about it, which made a tag a label rather
  than a search feature. Tag hits now lead the search page (a tag is a human
  judgement; a filename or a CLIP score is a guess), Browse filters by tag from
  the URL, and confirmed tags are kept visually distinct from unreviewed
  suggestions everywhere they appear.
- **Nav split at ten items** — four "find" destinations stay visible; the six
  administrative ones moved behind one Manage menu with click-away, Escape and
  `aria-haspopup`.
- **M9 started** — [ADR-0019](adr/0019-premiere-panel.md): UXP over the
  deprecated CEP, with a browser handoff shipping first because it needs no
  Adobe SDK and serves Resolve and Final Cut too. FCP7 XML export is live.
- **M8 complete** — [benchmarks](benchmarks.md) measured on the real install,
  and `drills.sh` failure drills at 9/9 including a backup verified restorable
  by `pg_restore`.
- **Vector search 27x faster** (75.0 ms → 2.8 ms p50). The benchmark found it
  doing a Sort instead of an HNSW index scan; the cause was stale statistics
  after a bulk embedding run, not a missing index. The scanner now ANALYZEs on
  its maintenance tick.
- **The shell scripts were never executable** from a clone — all four committed
  mode 644, so backup had only ever worked for people invoking `bash manage.sh`.
  Found by drill 4 on its first run.
- **Learning tags** — tag a video "Power Broom" and the system finds the other
  power brooms. CLIP puts words and images in one space, so a new tag works
  zero-shot from its own name, then shifts toward the operator's examples as
  they accumulate. The match bar is derived per tag, not fixed: low enough to
  admit the weakest accepted example, high enough to exclude the closest
  rejected one. Removing a tag is stored as a *rejection*, so a wrong guess is
  never offered twice — every correction tightens the next round.
- **Tailscale enrolment** (#30) — guided setup on the Security page, an
  optional sidecar behind the `tailnet` profile, and a tailnet address that is
  *learned* from a request that actually arrived over it rather than assembled
  from configuration, so it cannot be shown wrong.
- **Storage management from the UI** (ADR-0018) — add and remove media and
  cache drives from a form, via a scoped mount helper that is off unless
  enabled.
- **UI/UX audit fixes** — keyboard focus was invisible app-wide (no
  `:focus-visible` anywhere, and `a { text-decoration: none }`); `--paper-faint`
  measured ~3.3:1 against the background, under WCAG AA for text that carries
  real information; the only layout breakpoint was for asset detail, so the
  8-item nav had no mobile behaviour. All three fixed, plus a skip link, touch
  targets, and `aria-current` on the active nav item.
- **Transcription retry sweep** — 555 jobs had failed on a models-directory
  permission fault, exhausted their Celery retries, and were never looked at
  again; the fault was fixed long before anyone noticed the backlog. The
  scanner now re-queues audio that never got a successful attempt, bounded
  and skipping files that have failed repeatedly.
- **Maps and location documentation** — [maps.md](maps.md) and
  [location.md](location.md), linked from the settings card itself.
- **GELCO library** — the 18 TB share added read-only, with Premiere scratch
  folders (previews, auto-save, captured-and-generated, #recycle) excluded so
  regenerable intermediates never enter the catalogue. Proxies off, matching
  Intel 2026.
- **Google Maps, opt-in** — a real basemap on Places and address lookup for
  clusters the folders cannot name. Two separate keys (browser, referrer-
  restricted; geocoding, server-side and IP-restricted), both sealed at rest
  and configured on the Security page. Off by default: enabling either sends
  data to Google. Geocoding results are cached in the database, and a place
  the folders already name is never looked up.
- **Place detail view** — a place opens as a library-style page with media,
  position-source and sort filters, and paging.
- **Places** (#34): 4,177 located assets clustered into 67 named shoots,
  named from folder structure rather than a gazetteer.
- **`/assets/near` was unreachable** — registered after `/assets/{asset_id}`,
  so FastAPI parsed "near" as a UUID. Never worked until now; no test had
  covered it.
- **Location inference**: 264 positions lent from GPS-bearing cameras to
  cameras that were on the same job. Anchors are EXIF-only, so no inferred
  position ever seeds another.
- **Cross-library move detection** and the watcher departure lane — see
  "Media that moves" below.
- **Duplicate detection** (#24) with on-demand full-BLAKE3 verification.
  Real result: ~3.5 GB reclaimable, 1.2% of the corpus.
- **Trusted-proxy client IP handling** — a client can no longer spoof a LAN
  address past the public-access gate.
- **Supply chain** (#19): both images scanned and SBOM'd on every push;
  releases scan before publishing and pin by digest. Caught a real CVE on
  its first run.
- **Large-image handling**: images over 192 MB scale through FFmpeg rather
  than Pillow, with timeouts derived from file size.

### Deliberately deferred

OIDC/SSO, multi-tenant permissions, mobile
apps, OpenSearch backend, Kubernetes, DaVinci/Lightroom integrations,
generative summaries, cloud storage drivers.

## Storage management from the UI (shipped)

Drives are added and removed from **Storage** in the UI. A media drive is
mounted read-only and can register a library and start a scan in one step; a
cache drive is writable and holds thumbnails and proxies, keeping generated
files off the system disk.

Mounting needs `CAP_SYS_ADMIN`, so it lives in a `mounter` sidecar that holds
that capability and drops every other one — never in the API, which terminates
untrusted requests. It sits behind a compose profile and is **off by default**:

```bash
docker compose --profile storage up -d
```

An install that never adds a drive from the UI never runs a privileged
container at all. Constraints and the reasoning behind each are in
[ADR-0018](adr/0018-mount-helper.md); the short version is cifs/nfs only,
targets confined under `/mnt/media` or `/mnt/cache`, options constructed
rather than accepted, argv with no shell, credentials via a 0600 file, media
always read-only, and validation repeated inside the helper because that is
the side holding the capability.

Mounts made this way are live immediately but do not survive a host reboot.
The UI returns the exact fstab line and says so, rather than silently writing
to the host's `/etc/fstab` — a larger privilege that was deliberately not
taken.

**Still open:** health-aware storage — disconnected-mount alerts, capacity
warnings, and per-library storage attribution on the System page.

## Automated video and slideshows (A/C started)

Design decisions taken with the operator:

| | |
|---|---|
| Theming | CLIP selection + styling presets + generated title cards. **No** generative motion on the photos themselves. |
| Face review | Confirm before it counts |
| Render output | The VM data directory |
| Music | Operator supplies their own licensed tracks |

**Landed so far:** `media/theming.py` (themes as data, not code — a church
running a different VBS next summer adds one without a deploy) and
`media/slideshow.py` (selection: near-duplicate collapsing, face coverage,
themed ordering). 16 tests.

The selection logic is where the value is. A good event slideshow is not a
clever transition, it is the right forty photographs out of four hundred with
nobody left out — so `select()` gives every required person their best frame
*first*, then fills, then re-sorts chronologically. Choosing on merit and
re-sorting afterwards is what lets a themed slideshow still tell the day in
order; picking chronologically first would mean the theme only influenced the
tail.

Low-scoring frames are pushed back rather than removed, so a thin day still
produces a slideshow instead of four photos.

**Now landed: the render itself.** `media/render.py` (filter graphs),
`media/pipeline.py` (orchestration), `render_slideshow` on the `media` queue, a
`slideshows` table (migration 0013), the `/api/v1/slideshows` surface and a
review-then-render UI. 38 render tests plus 6 on the thread cap.

The architecture was chosen by measurement, and the measurement overturned the
obvious design. One `filter_complex` over every still peaks at 792 MB for a
*single* slide against a 1000 MB worker limit, and a chained `xfade` is killed
at **any** length — four segments as readily as twenty-four — because each
later input's decoder buffers until its transition point. Batching the join
would not have helped.

What works instead: render each slide's body and each crossfade as separate
short clips (one and two inputs respectively), then stitch with the concat
demuxer under `-c copy`. Worst piece 302 MB, stitch 62 MB, and neither grows
with the length of the slideshow. Full numbers and the two FFmpeg traps that
cost an afternoon are in [video-generation.md](video-generation.md).

**A real bug fell out of the profiling.** `_with_thread_cap` inserted
`-threads` before `-i`, which configures the *decoder* — `-threads` is a
per-file option, not a global one. Every proxy transcode had been running x264
across all 12 cores with the setting appearing to be in force (810 MB against
470 MB once actually applied). Fixed, with tests pinning both positions.

Renders take about 7 s per photograph on CPU — roughly five minutes for a
forty-photo show — and pick up NVENC automatically once the GPU lands.

**Still to come:** title cards and interstitials, and a sharpness measure
(selection currently ties every candidate, so it falls through to theme score
and capture order).

## Automated video and slideshows — research

Full analysis in [video-generation.md](video-generation.md). The finding worth
recording here: **"AI video from photos" is two products wearing one name**, and
only one of them is dependable.

- **Deterministic renders** (Ken Burns, ordering, transitions, music, text) are
  pure FFmpeg, need no GPU, work on current hardware, and are reliable because
  nothing is invented. This is what an estate agent would actually put a
  client's name on, and it covers both listing videos and event slideshows.
- **Generative image-to-video** (SVD, LTX-Video, Wan 2.1, CogVideoX — all local,
  12–24 GB VRAM) is genuinely impressive and genuinely unsuited to property
  interiors: architecture must stay rigid and generative motion is loose with
  straight lines, invented space beyond the frame is a *disclosure* problem for
  a regulated listing rather than a quality one, and independently generated
  clips do not hold colour continuity across a 90-second edit. Estimated 3 in 10
  interior clips usable; better on exteriors and drone material.

Recommended sequence: build the deterministic renderer first (it is what makes
generative clips usable at all, by giving them somewhere to sit), then add
generative clips as an optional, reviewed, exteriors-only enhancement once the
GPU lands.

Event slideshows are the easiest win and lean on what already exists: capture
time for order, CLIP for collapsing near-duplicates, and **face coverage as a
constraint** so every named person appears — which is exactly what a church or
a family asks for and what a chronological cut misses.

## Maps — native, no extra service

Basemaps are now served by FrameFound itself. PMTiles puts a whole region in
**one file** addressed by HTTP range requests — the same mechanism already
implemented for video scrubbing — so a basemap is a download into the data
directory and an endpoint that serves byte ranges out of it. No tile server, no
extra container, nothing to keep alive on a host that is already
over-committed.

`GET /api/v1/basemaps` lists a short curated catalogue (Pennsylvania ~0.6 GB,
US Northeast ~2 GB, continental US ~12 GB) with what is installed; `POST
/api/v1/basemaps/download` extracts one on the `media` lane, to a `.part` file
renamed on completion so an interrupted transfer never looks like a usable map.
Once the file is there, nothing about the map leaves the network.

**Manage → Basemaps** now drives all of it: download, size, delete. It polls
only while an extraction is in flight and says plainly that `pmtiles` reports
nothing until it finishes, rather than showing a progress bar it would have to
invent. Pennsylvania was measured at 377 MB in 22 seconds.

Self-hosted vector tiles are the **recommended** basemap, and Google is one
option among three rather than the only one. Chosen on Security → Maps &
geocoding:

- **none** — the local scatter. Nothing leaves the machine.
- **maplibre** — MapLibre GL from a style URL you control. Point it at your own
  Protomaps file or OpenMapTiles server and no third party sees anything.
  No API key, no bill. See [maps.md](maps.md#self-hosting-tiles-recommended).
- **google** — Google's tiles, still supported for anyone who would rather not
  run a tile server.

Protomaps is the pragmatic route on this hardware: a regional `.pmtiles` extract
is one file served over range requests, with no tile server process to keep
alive on a host that is already over-committed on memory.

MapLibre is loaded at runtime from a configurable URL rather than bundled, so an
install with no internet can serve the library from its own origin and the map
makes no outbound request at all. That URL is validated to http(s) or an
absolute path, because it goes into a `<script src>`.

## Media that moves (mostly shipped)

A file that reappears at a new path with the same size and partial hash
re-binds to the existing asset, keeping its UUID, transcripts, thumbnails, and
embeddings (ADR-0010).

**Working now:**

- **Across libraries** — the lookup is global, not per-library. A clip dragged
  from `Intel 2026` to `Archive 2026` keeps its derived data and simply
  changes `library_id`. Before re-binding, the old path is stat'd: if the file
  is still there, this is a genuine duplicate, not a move.
- **Whole-folder reorganisation** — the watcher walks a moved directory's
  subtree, since watchdog reports the folder and says nothing about its
  contents. Each file then re-binds through the same content lookup.
- **Departures** — deletes and moves out of a watched tree flag their assets
  `missing` after a 60-second grace period and a confirming stat, instead of
  waiting for the next reconciliation scan. Nothing is deleted from the
  catalogue; a NAS that blinks must not take the catalogue with it.
- **Verification pass** — `POST /api/v1/duplicates/verify` runs full BLAKE3
  hashing on demand to confirm that files really are the same bytes.

**Still open:**

- **Across mounts and drives** — same content on a new NAS or a new share,
  including when a library root itself changes.
- **Re-linking after restore** — after `manage.sh restore` onto new hardware,
  reconcile the catalog against storage by content rather than by path.

Design note: all of this hangs off the existing `partial_hash` /
`content_hash` columns — no schema change was needed, and none is expected for
what remains.

## M0 — Product & architecture definition ✅

- [x] Architecture doc + diagrams · data model · ADR seed set (0001-0005)
- [x] Threat-model outline · license inventory · repo scaffolding · compose definition
- [ ] Backlog converted to GitHub issues; labels + milestones created
- **Definition of done**: docs above merged; `docker compose config` valid; API
  and web skeletons boot with health checks green; CI runs lint+tests on PR;
  a new contributor can go from clone → running skeleton via CONTRIBUTING.md
  in under 30 minutes.

## M1 — Repository & infrastructure foundation (v0.1)

- Compose stack boots end-to-end (pg + redis + api + web + caddy + workers)
- Alembic baseline migration; config system; structured logging
- Local auth: argon2id, sessions, roles, rate limiting, setup token, first-run wizard (admin creation)
- Health endpoints (`/healthz`, `/readyz`) for every service; system-health API
- CI: ruff/mypy/pytest, ESLint/tsc/vitest, docker build, Trivy scan, license scan
- GHCR publishing with version tags; Dependabot config

## M2 — Library indexing (v0.2)

- Library CRUD + path validation against allowlist; path-mapping profiles
- Recursive initial scan (bounded memory, progress, pause/resume, restart-safe)
- File-identity strategy (ADR-0010): size+mtime+partial hash; full BLAKE3 on demand; dedupe
- Stability detection for in-flight copies; watcher (watchdog) + periodic reconciliation
- Missing-mount detection → assets flagged `unmounted`, never deleted
- Metadata extraction: ffprobe, ExifTool, EXIF/GPS, camera fields

## M3 — Proxies & previews (v0.3)

- Image thumbnails + previews; video poster frames; waveforms (audio)
- 1080p H.264 proxies (CPU x264; NVENC when available); HLS or MP4 range streaming
- Derivative tracking, retention, regeneration; processing profiles v1
- Signed short-lived media URLs (ADR-0012); range-request streaming through API

## M4 — Transcription (v0.4)

- Audio extraction; faster-whisper provider (`TranscriptionProvider` interface)
- Language detection, timestamped segments, SRT/VTT generation
- Postgres FTS over transcripts; search API returns asset + timestamp
- Transcript panel in UI; click-to-seek

## M5 — Visual search (v0.5)

- `EmbeddingProvider` interface + OpenCLIP implementation
- Image embeddings; scene detection + duration-aware frame sampling; frame embeddings
- Text→image semantic query; related/similar assets; optional OCR stage
- Hybrid ranking: RRF over exact/FTS/vector (ADR-0011); quoted-phrase exact mode

## M6 — Web UI alpha (v0.6)

- Polished search page (unified box, filters, media-type toggles, match reasons)
- Asset detail: player with match/scene markers, transcript sync, metadata, paths + copy buttons
- Browse (folder tree mirroring NAS), processing dashboard, system health page
- Collections + saved searches; mobile-usable search/preview; attribution screen

## M7 — Remote access (v0.7)

- Remote-access wizard (4 modes); Caddyfile templating; public-HTTPS hardening
- Cloudflare DDNS adapter + sidecar (scoped token, IPv4/IPv6, error surfacing)
- Cloudflare Tunnel instructions/profile; Tailscale docs + detection
- TOTP 2FA, audit log, session management UI, "disable public access" kill switch

## M8 — Beta hardening (v0.8-0.9)

- Large-library benchmarks (target: 100k+ assets; publish measured numbers)
- Failure-mode drills: NAS disconnect mid-scan, worker OOM, power loss
- `manage.sh backup/restore` + documented full VM recovery; update workflow with
  preflight, migration backup, health-gated rollback (ADR-0014)
- Security review vs threat model; FFmpeg sandbox decision; pen-test checklist
- Proxmox deployment guide finalized (GPU passthrough, sizing, mounts)

## M9 — Adobe proof of concept (post-1.0 track)

- UXP vs CEP research (ADR-0015) → auth flow → search panel → proxy preview →
  import via path mapping → transcript→marker experiment

## Deferred beyond 1.0

OIDC/SSO, multi-tenant permissions, mobile
apps, OpenSearch backend, Kubernetes, DaVinci/Lightroom integrations,
generative summaries, cloud storage drivers.

## Hardware path — 2012 Mac Pro (5,1) as the primary host

Decision 2026-07-28: the production host is the owner's 2012 Mac Pro running
Proxmox (2× Westmere Xeon, no AVX; upgradeable RAM and GPU). Strategy: prove
each milestone on this box, upgrading components only when a milestone needs
them. Any GPU purchased carries over to a future host — no stranded spend.

| Phase | Hardware change | Unlocks | Risk |
|---|---|---|---|
| A (now) | none — 4 GB VM | M2 verified at scale; M3 thumbnails + CPU (x264) proxies, slow but correct | low |
| B | +RAM (DDR3 ECC is cheap; target 48–64 GB host → 16–24 GB VM, restore HA to 8 GB) | full-archive scans, real proxy concurrency, M3 done at production scale | low |
| C | +GPU (NVIDIA ≤225 W for the dual 6-pin aux budget; RTX 3060 12 GB is the reference pick) + VT-d passthrough | **NVENC proxy transcoding** (M3 gets fast with zero software fight — NVENC needs no AVX) | low-medium (MP5,1 passthrough is well-trodden but firmware-quirky) |
| D | same GPU, custom AI builds | M4 transcription: faster-whisper/CTranslate2 has runtime CPU dispatch and CUDA execution — expected to work without AVX, must be proven on-box | medium |
| E | same GPU, custom AI builds | M5 visual search: official PyTorch wheels **require AVX and will not load**; path is ONNX-exported CLIP on onnxruntime-gpu or a from-source AVX-free torch build | **high** — timebox it; if it stalls, M5 waits for a CPU-era upgrade and everything else still ships |

Standing caveats: PCIe 2.0 bandwidth (minor for inference), Westmere
single-thread speed (scans/transcodes are parallel, so throughput is fine),
and the AVX ceiling is permanent — a future platform swap is migration-by-
design (`manage.sh backup` → restore; GPU moves over).

## Major technical risks

| Risk | Exposure | Mitigation |
|---|---|---|
| pgvector HNSW performance at millions of frame vectors | search latency promise | benchmark at M5/M8; SearchBackend seam ready for OpenSearch/Qdrant |
| SMB/NFS watcher unreliability | missed/duplicate events | reconciliation scan is the source of truth; watcher is an optimization |
| FFmpeg/codec matrix (HEVC 10-bit, MXF, HEIC, RAW) | proxy failures on pro media | fixture corpus per codec; per-stage degradation, never total failure |
| GPU driver/CUDA/toolkit variance on user hardware | install failures | CPU is the default path; GPU strictly additive via overlay file |
| Whisper accuracy on domain audio (auctioneers!) | search quality | model-size setting per profile; user-corrected transcripts feed back into FTS |
| Adobe UXP API surface for panels still maturing | M9 slip | research-first ADR; core API designed panel-agnostic |
| One-VM resource contention (DB vs FFmpeg vs GPU) | perceived instability | queue segregation, concurrency caps, quiet hours (M2/M8) |
| Redis licensing drift | distribution clarity | pin BSD build or move to Valkey (tracked in licensing.md) |
