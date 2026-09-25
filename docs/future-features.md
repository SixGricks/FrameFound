# Future features — proposed plan

## 2026-09-24 — Where FrameFound can replace paid services

Asked: make FrameFound more useful across the whole business workflow, and
reduce reliance on paid services. The basis is measured, not assumed: the
Intel library holds **53 shoots in 2025 (8,779 photos) and 53 so far in 2026
(11,132 photos)**, about 50 videos a year, and the workflow around them runs
through Fotello, Canva, an AI video editor, Google Drive, Todoist and
Make.com, with Adobe CC, FreshBooks, M365, WordPress.com and Granola beside
it. This ranks by *fit*: what FrameFound can actually take over. Rank by
money once the real bills are beside it, because nothing here knows what each
one costs.

| Service | Its job today | Fit | What FrameFound does about it |
|---|---|---|---|
| **Fotello** | Per-photo MLS editing of each "Fotello Batch" | **Replace** | Auto-edit (Claude judges a 768px preview, the local engine renders at full resolution) already existed; as of today it also names every photo and exports the batch package (below). What's left is proving the quality. |
| **AI video editor** | Transcripts, b-roll logs, selects | **Mostly replace** | Whisper transcripts, frame search and FCP7 export to Premiere already exist. Missing: a b-roll log document, and selects exported as Premiere markers. |
| **Canva** | Brochures, mailers, the LF and Obertaul ads | **Feed, then partly replace** | Keep Canva for designed pieces. FrameFound supplies the inputs: Photo Index, captions, ordered photos. The two fixed-layout weekly ads could later render server-side from one template. |
| **Make.com / Todoist triggers** | Kick off ads and brochures | **Replace one class** | "A new shoot folder appeared" belongs to the scanner, which already sees it. Outbound webhooks on *listing exported* would let the existing scenarios trigger from FrameFound instead of watching Drive. Not a general automation platform. |
| **Google Drive** | Handing photos to Canva, clients and agents | **Keep as the channel** | FrameFound writes into it (delivery, next). Client share links would need public access switched on, which is off today by design. That is a security decision for you, not a default. |
| **Adobe CC** | Lightroom, Premiere | **Reduce Lightroom use** | MLS editing no longer needs Lightroom. Premiere stays, and with it the subscription. |
| **WordPress.com** | Website | **Later** | The new captions double as image alt text, so a "publish gallery" step would carry SEO for free. |
| FreshBooks, M365, Granola | Invoicing, mail, meeting notes | **Not a fit** | FrameFound should not grow into these. |

### Shipped today — the Fotello-style batch package

The photo-organizer skill did the pre-Fotello work by hand: rename each
select `01-front-exterior-brick-colonial-130-davis-rd-auction.jpg`, write
"Photo Index.md", make contact sheets. The listing export now produces all of
it:

- **Named in the same API call as the edit.** The auto-edit tool also returns
  a caption ("what it shows") and a file-name slug. **Name photos with AI**
  does naming only (a smaller call), for shoots still edited elsewhere. Names
  follow the room-label contract: the AI suggests, typing confirms, and
  confirmed names are never overwritten.
- **SEO file names**: `NN-{slug}-{suffix}.jpg`. The suffix is typed per listing
  (address and sale type) or derived from its name, minus the shoot-folder
  date. The name switches to three digits past 99 photos, so it still sorts.
  "Simple" names (`01_kitchen.jpg`) remain an option.
- **`_index/`** in the zip holds `Photo Index.md` (listing notes first, then #
  | Filename | What it shows | Original), the same table as CSV, and numbered
  contact sheets drawn from the rendered export. It sits in a folder so that
  selecting every top-level JPEG for an MLS upload never picks up a sheet.
- A **Names & index** view of the listing edits all of it in one table. Any
  rename marks an existing zip out of date.

### The plan, in order

1. **Fotello bake-off — about one day, decides the largest replaceable
   spend.** 130 Davis Rd is the one shoot with both sides catalogued: 72 photos
   sent to Fotello and 70 returned edited. Auto-edit the same 72 and show the
   pairs side by side; you judge. Log the API's reported token usage per run,
   so the cost per listing is measured rather than estimated. If the results
   hold, stop sending batches. If they don't, the pairs show exactly where
   they fall short, which is the brief for the next engine change.
2. **Deliver to Drive** (already next on the roadmap). The package lands in
   the property's Drive folder, and the brochure skill reads `Photo Index.md`
   from there instead of from a hand-built one. This turns two overlapping
   tools into a chain.
3. **Draft listings from new shoot folders.** When the scanner finds a new
   dated folder with photos, it creates a draft listing: rooms labelled,
   walk-through ordered, removals suggested, photos named if a key is set.
   Opening it becomes review instead of setup. This saves your time rather
   than a subscription, and it is the largest daily saving on this list.
4. **Push alerts** (already on the roadmap), including "draft listing ready".
5. **Listing reels.** The slideshow renderer, set to 9:16: ordered listing
   photos plus the shoot's drone clips, titled with the address. That covers
   the simple social video now made in the paid editor.
6. **B-roll logs and selects as Premiere markers.** Transcripts and frame
   search exist; the missing pieces are a per-shoot log document and marker
   export (`fcp7.Marker` is already written).
7. **Server-rendered weekly ads.** The LF and Obertaul ads have a fixed size
   and layout each week, which suits a template render (HTML to PDF/JPEG)
   triggered by the listing rather than by Todoist.
8. **Client share links.** Only after a decision to open remote access.

**Not doing:** rebuilding Make.com, a design editor to rival Canva, or
anything in invoicing, mail or meeting notes. Each would be a second product
with no connection to the photographs.

---

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

### 9. Close the real frame gap — done 2026-09-24
The "1,554 assets invisible to visual search" headline decomposed on
inspection: 1,080 are audio (no frames by design), 407 are BRAW (GPU-gated).
The true gap is ~67 images/videos that should have frames and do not. Requeue
them; then the number on the dashboard means what it says.

*Resolved by the system review:* the gap was 591, not ~67 — 152 photos lost a
race between sampling and their own thumbnail, and BRAW now takes one frame
from its SDK-decoded poster. A maintenance sweep re-queues anything left
without thumbnail, frames or vectors (runs once the NAS shares are back).

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

## Proposed vertical — real-estate editing & listing export

Scoped separately in **[real-estate-editing.md](real-estate-editing.md)**
(2026-08-04): Fotello/Imagen-style color correction, sky replacement and
object removal as non-destructive edit recipes, plus a listing export that
groups, orders and renames a selection using room labels computed zero-shot
from the CLIP embeddings the catalogue already stores. Phase 1 (listing
export) needs no new AI at all; inpainting speed on the AVX-less Xeons is
the one open feasibility question, spiked first.

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
  the current default. *2026-09-24: back to 31.9 GB. `manage.sh prune` exists
  now and `manage.sh doctor` reports it; the schedule is still the
  operator's to set up.*
- `delete_slideshow` still writes no audit row (noted 2026-07-31).
- DJ Grick's threshold sits at 0.325 — near the 0.30 floor, driven by a
  weakest-confirmed of 0.345 with zero rejections. Not wrong, but his
  discovery sweeps run at the widest allowed bar; the first rejection he
  records will snap it tighter.
