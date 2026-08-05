# Real-estate editing & listing export — feasibility and plan

Drafted 2026-08-04. The ask: an Imagen.ai / Fotello-style flow inside
FrameFound — color correction, sky replacement, object removal, and an export
that groups and renames a selection so it uploads in logical listing order.
Native-feeling, external AI only where it earns its place.

**Verdict: buildable on the current stack.** The two hardest-sounding pieces
are the cheapest: listing-order export is nearly free because every image
already has a CLIP embedding, and non-destructive editing is the only design
the read-only-originals rule ever permitted. The genuinely hard piece is
inpainting speed on the Westmere Xeons (E5620, SSE2-only, no AVX) — the same
CPUs already run CLIP/SCRFD/ArcFace via onnxruntime's CPUExecutionProvider,
so new ONNX models follow a proven path, but object removal will be a queued
batch step, not a live brush, until a GPU exists.

## What each capability maps to

| Ask | Approach | Runs where | Feel |
|---|---|---|---|
| Group + rename for listings | CLIP zero-shot room labels over **existing embeddings** + order template | SQL + one text-embed, instant | Native |
| Color correction | Non-destructive edit recipes; Pillow/libvips render | API + `media` queue | Native, interactive |
| Sky replacement | Segmentation ONNX → mask → composite from a sky library + color match | `vision` queue | Native, seconds-to-minutes |
| Object removal | Brush mask in UI → LaMa inpainting (ONNX) | `vision` queue | Queued on CPU; live with a GPU |
| Smart naming / QA (optional) | Claude API vision as a sealed, off-by-default provider | External | Optional polish |

### Where Claude API honestly fits — and where it does not

Claude does not output edited pixels; it is not the sky-replacer or the
inpainter. What it is genuinely better at than local CLIP:

- Fine room distinctions ("primary bedroom" vs "bedroom", half vs full bath)
- Listing-ready captions and filename text
- A final QA pass: flag blurry, tilted, or badly exposed frames in a set

Pattern: a provider exactly like geocoding — optional, key sealed with
Fernet, presence-only in the settings API, **off by default**, and the local
CLIP path always works without it. Cost at listing scale is pennies.

### The pixel models (all open source, all ONNX-able)

- **Inpainting: LaMa** (Apache-2.0; big-lama ONNX exports exist). IOPaint
  wraps it in a ready editor, but embedding the ONNX in `worker-ai` matches
  how CLIP/InsightFace already ship — no new service.
- **Sky segmentation:** a compact scene-segmentation model (u2net-style sky
  matting or DeepLabV3/ADE20K "sky" class) exported to ONNX. Deterministic
  composite + horizon/feather controls + luminance/temperature transfer to
  relight the foreground. This is what the commercial tools do; no
  generative model required, which keeps results predictable — the property
  must still look like the property.
- **Sky library:** ships empty. Operator adds their own skies (or CC0 sets).
  Licensing stays clean and the skies match local weather/light.
- **Color:** Pillow is already in the stack; auto-levels/auto-WB plus
  slider adjustments (exposure, contrast, temp, tint, vibrance, shadows,
  highlights) cover the Fotello baseline. `pyvips` is the upgrade path if
  Pillow renders feel slow at preview size. HDR bracket merge via `enfuse`
  is a later add for window-pull work.

## Design commitments

1. **Originals are never touched.** An edit is a recipe (JSON of operations,
   including masks and chosen sky) stored per asset with history. Previews
   render at ~1600px for interactivity; export applies the recipe at full
   resolution. Undo is recipe history, not file juggling — this is also why
   it will feel like Lightroom rather than a webform.
2. **Confirm before it counts, again.** Room labels are suggestions until
   the operator confirms or drags into order; nothing is renamed on a guess.
   Same philosophy that made faces trustworthy.
3. **Nothing leaves the machine by default.** Claude provider opt-in per
   export ("Use Claude to name this set"), never ambient.

## Phases

### Phase 0 — feasibility spike ✅ MEASURED 2026-08-04
Run inside `worker-ai` on the production VM (E5620, SSE2-only, 4 threads),
real frames from the catalogue:

| Model | Source | Size | Time | Verdict |
|---|---|---|---|---|
| SegFormer-b0 ADE20K (sky seg) | `lquint/segformer-b0-finetuned-ade-512-512-onnx` | 15 MB | **0.77 s**/image @512 | CPU-viable now |
| LaMa fp32 (inpainting) | `Carve/LaMa-ONNX` | 208 MB | **18.3 s**/512px tile | Queued batch on CPU; live with a GPU |

Both execute correctly through the existing onnxruntime
`CPUExecutionProvider` — no AVX faults, and the inpainted regions carry
plausible statistics (mean ≈ 105–124, σ ≈ 21–48), not a broken graph's
flat fill. The LaMa graph takes fixed 512×512 inputs, so Phase 4 uses the
crop-around-mask-and-blend-back strategy at ~20–40 s per removal region.
Sky fractions on interior test frames came back 0–1.4%, which is exactly
right. **Conclusion: Phase 3 ships on current hardware; Phase 4 ships as a
queued batch step and goes near-live when the GPU arrives.**

### Phase 1 — Listing export ✅ SHIPPED 2026-08-04
- **Listings** (migration 0016): `listings` + `listing_items`, a join table
  because label and position are per-item editable state.
- **Room classification** (`ai/rooms.py`): 15-room taxonomy in canonical
  walk-through order, 2 averaged prompts per room, zero-shot against the
  frame embeddings already in pgvector. `MIN_ROOM_SCORE 0.15` keeps "no
  idea" honest instead of defaulting to the nearest label. Suggestions
  stay dashed until the operator picks — same contract as tags and faces.
- **API** (`api/v1/listings.py`): create/add/remove, label override,
  canonical arrange, partial-safe reorder (ids omitted from a reorder keep
  their relative order), export queue + zip download. Deleting a listing
  is admin-only and writes an `audit_log` row — the gap noted against
  `delete_slideshow` was not repeated.
- **Export task** (`export_listing_zip`, media queue): EXIF-orientation
  fix, ICC→sRGB flatten, LANCZOS resize to the requested edge (default
  3840, q85), `01_front_exterior.jpg …` numbering that closes ranks over
  unreadable files and names them in `export_error` instead of shipping a
  gallery with a hole in it. Videos stay out of the photo zip.
- **UI**: /listings + drag-to-reorder detail page with per-photo room
  dropdowns, the export number badged on each tile, and an add-photos
  search that reuses catalogue search (visual + filename hits).
- 9 tests; the export tests build real JPEGs and read the zip back.

### Phase 2 — Non-destructive color editor ✅ SHIPPED 2026-08-04
- **`asset_edits`** (migration 0017): append-only recipe versions per
  photograph. Undo is the previous row, revert deletes rows; no pixels are
  stored and no original is written — the media mounts are read-only, so
  this was the only possible design as well as the right one.
- **Engine** (`media/develop.py`): exposure (EV), contrast, temperature,
  tint, shadows/highlights (luminance-masked gains so a shadow lift cannot
  bleach a sky), vibrance (chroma-weighted), saturation, and bounded
  auto-levels. Pure Pillow+numpy, monotonic and clipped — no slider
  position produces garbage, only a bad-looking photograph. Recipes are
  clamped at the boundary like probe fields: stored JSON is never trusted
  into arithmetic raw.
- **One engine, two callers.** The preview endpoint and the export task run
  the same `apply_recipe`; there is deliberately no client-side
  approximation of the maths, because two implementations of "+0.4
  contrast" will disagree and the operator would correct toward a preview
  the zip contradicts. Adjustments are per-pixel and scale-free, so both
  apply after downscaling — same look, fraction of the pixels.
- **API** (`/develop`): GET state, POST preview (JPEG, no-store), PUT save
  (identical recipe = no new version), DELETE revert, and
  `/develop/listing/{id}/apply` — one recipe across every photo in a
  listing, each as its own version so single photos can still diverge.
- **UI**: `/edit/{assetId}` — sliders + auto toggle, debounced server
  preview with a race-guard ticket, double-click-to-zero, prev/next through
  the listing in gallery order, "Apply to whole listing". Listing tiles
  badge edited photos.
- Found and fixed in passing: `embed_text` on a server without the ai
  extra raised raw `ModuleNotFoundError` (`_tok` lacked the conversion
  `_session` has), so creating a listing 500'd instead of degrading to
  unlabelled items.
- 14 engine/API tests; the export test proves the zip is one stop brighter.

### Phase 3 — Sky replacement ✅ SHIPPED 2026-08-04
- **Segmentation** (`ai/skyseg.py`): SegFormer-b0/ADE20K through the same
  AVX-free onnxruntime path as CLIP; 15 MB, 0.77 s/image measured. Runs
  at 512 and the mask upsamples — the full-res horizon comes from
  feathering, which is how the commercial tools do it too.
- **Compositor** (`media/sky.py`): cover-fit with vertical overscan so
  `shift` places the sky, Gaussian-feathered join scaled to image height
  (preview and export look alike), and **relight** — bounded channel
  gains pulling the foreground toward the sky's tone, because a dusk sky
  over a noon-lit house is the tell that ruins every amateur sky swap.
  Deliberately non-generative: the property must still be the property.
- **Sky library**: operator photographs in `data/skies/`, uploaded from
  the editor (raw-body PUT, bytes verified as an image before touching
  disk). FrameFound ships no skies and fetches none.
- **One recipe system**: the sky choice lives in the develop recipe, so
  versioning, batch apply and export support came free. Sky names are
  clamped to a safe character set at the same boundary as the sliders — a
  stored recipe is never a path. Below 2% sky fraction compositing is a
  silent no-op, which is what makes batch-applying a sky across a listing
  safe for the hallway photos; the editor also reads `/sky-info` and says
  "looks like an interior" instead of offering the picker.
- **Degradation**: a deleted sky file or a server without the ONNX
  runtime renders colour-only rather than failing the export.
- 9 new tests: composite/relight/no-op behaviour with hand-made masks,
  traversal refusal, garbage-upload refusal, degraded export.

### Phase 4 — Object removal ✅ SHIPPED 2026-08-05
- **Brush in the editor**: paint over the object, press Remove. The mask
  travels as a PNG the operator actually drew, and `mask_meta` keeps it —
  a record of exactly which pixels are invented, which a tool that edits
  real-estate photographs owes its operator. A mask covering more than
  half the frame is refused: this removes objects, it does not repaint
  scenes.
- **Crop-around-the-mask** (`ai/inpaint.py`): square context crop
  (60% margin, 64 px floor) → 512 → LaMa → paste back *through the
  feathered mask*, so unmasked pixels survive byte-for-byte rather than
  being softened by the 512 round-trip. Tested at the pixel.
- **Queued, honestly**: ~20–40 s per region on the Xeons, on the media
  queue (6 CPU / 8 GB, models volume mounted). The editor polls and says
  "about a minute on this hardware"; a GPU later makes it near-live with
  no redesign.
- **Results are a versioned chain** (migration 0018): each removal runs
  on the previous result; the newest becomes the *base image* every
  recipe (sky, geometry, colour) renders on, in both preview and export.
  Undo deletes the newest version's file — only the newest, because
  pulling a middle link out would misdescribe every file after it. The
  original stays untouched on its read-only mount.
- One at a time per photograph (409 otherwise) — rounds chain, so
  parallel rounds would race for the same base.

### Phase 5 — Claude provider (small, optional)
Room labels/captions/filenames/QA behind the sealed-key provider pattern.

## Integration reviews (2026-08-05, operator-requested)

**Cloudinary** (cloudinary.com/pricing): a media-CDN with transform credits
($99/mo Plus = 225 credits; 1 credit ≈ 1,000 transformations or 1 GB).
Its colour correction is generic auto-enhance — roughly the tier of our
local auto-levels, with none of the real-estate specials (window pull,
perspective, sky), and it requires hosting the photos in their cloud.
**Verdict: wrong tool for this; not adopted.**

**Imagen AI API** (api-docs.imagen-ai.com, real-estate guide): purpose-built
and directly relevant. Upload originals → full-resolution edited JPEGs
back, with HDR bracket merge, perspective correction, window pull and sky
replacement as flags; three built-in real-estate presets (Elegant / Modern /
Natural Home) or a **Personal AI Profile trained on the operator's own past
edits** — which closes the "learned style" gap this document previously
called unclosable. Timing: a 42-image bracketed project runs 30–40 minutes.
Trades: full photographs leave the machine, per-photo credits, and the edit
is theirs rather than an inspectable recipe. **Verdict: worth adding as a
selectable premium backend for final MLS delivery, behind the same
sealed-key + explicit-button pattern as the Claude picker. Not yet built.**

The resulting three-tier picture:

| Tier | Cost | What leaves | Quality ceiling |
|---|---|---|---|
| Local engine + presets | free | nothing | good corrected |
| Claude recipe-picker | ~fractions of a cent/photo | 768px preview | near-MLS, inspectable |
| Imagen API (proposed) | per-photo credits | full originals | MLS-final incl. HDR + trained style |

## Parity study — published IntelAuctions listings vs the engine (2026-08-05)

Method: eleven published photographs from intelauctions.com (the Delightful
Denver Dwelling gallery) paired with their NAS originals by filename — the
site keeps original names, so pairing is exact — then measured three ways:
original, our engine, published. Statistics on brightness, red-blue cast and
mean chroma; side-by-side strips for the eyes.

Results, averaged: brightness original 99 → published 113; the engine lands
at **113.0** (dead on). Chroma original 11.3 → published 20.0 (they nearly
double it); the engine reaches 16.7 at vibrance 0.42. Residual cast delta
+7 on golden-hour aerials, where the published grade keeps warmth but tames
it — per-photo territory, exactly what the AI picker exists for. The tuned
values are recorded as `develop.LISTING_PRESET`.

**The visible difference on overcast shots is not colour at all: it is sky
replacement.** The published version of `dji_20260707000320_0016` has a blue
sky composited over a flat grey one. Which settles the workflow: the
operator picks the folder, picks a sky (or none), presses Auto-edit, and
tweaks what comes back.

That workflow shipped the same day: `POST /listings/{id}/ai-edit` takes an
optional `sky_name`; with an Anthropic key it runs the recipe-picker,
without one it applies `LISTING_PRESET` entirely locally — the button works
on day one, and upgrades itself when a key appears. The chosen sky is
composited only where segmentation finds ≥4% sky, which is what makes one
choice safe across a whole shoot: interiors pass through untouched.

## Field-test round 2 (2026-08-05, operator testing live)

Seven findings from real use, six addressed the same session:

- **Per-photo progress.** Auto-edit tiles now show a spinner that flips to
  "✓ edited" as each photograph lands, with an n-of-m counter on the button
  — the Fotello affordance the flow was missing.
- **Farm & rural categories**: Barn, Shed, Pasture/field, Hunting land
  (deer stands, food plots, blinds), Outdoor (other), and Rec room join the
  taxonomy — 21 rooms now, tuned for the Lancaster market.
- **Sky matte rebuilt for trees.** Segmentation now yields a soft
  probability matte (the model's own uncertainty at foliage is exactly the
  softness a matte wants), eroded before feathering so sky never bleeds
  into leaf edges, then luminance-keyed: pixels much darker than the sky's
  own brightness are branches, and they survive the swap. Relight also
  gained a global component — a third of the ground correction applied to
  the whole frame, because a real scene is lit by its sky and that whisper
  is what makes a composite read as one photograph.
- **Listing controls moved to a right rail**: Auto-edit (sky choice +
  progress), Rooms & order (arrange + re-suggest), Curate, Export (size and
  quality selects at last exposed), Delete. The grid is the work surface;
  the rail is what happens to the whole shoot.
- **Curation.** "Suggest removals" finds near-duplicate groups via the CLIP
  embeddings already stored (≥0.92 cosine = interchangeable), keeps the
  sharpest frame per group (mean |Laplacian| at 384px), and flags markedly
  soft outliers — with a coverage guard: a room never loses its last
  photograph, because a blurry photo of the only barn is still the only
  barn. Suggestions cap at 40% of the listing and nothing is removed until
  the operator says so.
- **Bracket merge acknowledged, not yet built**: the operator spotted
  frames that should have been exposure-merged. That is the enfuse work
  already on the plan; it moves up the queue.

## Quality review — will this edit like Fotello? (2026-08-04)

Asked directly, so answered directly. Fotello and Imagen.ai are a human+AI
pipeline trained on millions of real-estate edits; what FrameFound has is a
correct but *global* editor plus scene-aware sky replacement. Where each
Fotello behaviour stands:

| Fotello does | FrameFound today | Gap |
|---|---|---|
| Global colour/exposure correction | ✅ 8 sliders + bounded auto-levels | At parity for competent captures |
| Batch consistency across a shoot | ✅ Apply-to-listing | At parity |
| Sky replacement with relighting | ✅ Phase 3 | Close; theirs handles reflections/glass better |
| **Window pull** (bright windows, dark interiors) | ✅ single-frame local tonemap slider | Bracket fusion (`enfuse`) remains the ceiling for sensor-clipped panes |
| **Vertical correction** (keystone) | ✅ Straighten + Verticals sliders | Auto-detection of the correction amount would be the refinement |
| Learned personal style | ❌ | Their moat: trained on your past edits. Presets-per-operator is the honest approximation |
| Declutter / object removal | ✅ brush + queued LaMa | ~1 min/region on CPU; near-live with a GPU |
| Grass greening, fire-in-fireplace, TV inserts | ❌ | Greening is an easy HSL band op; the inserts are compositing work not planned |
| Human QC pass | ❌ by design | The operator *is* the QC; optional Claude flag-pass (Phase 5) is the assist |

**Honest summary:** for a well-shot exterior, FrameFound after Phase 3 gets
you an edit a listing can use — corrected, consistent, sky replaced, named
and ordered — with zero per-photo fees and nothing leaving the machine. For
interiors, the window-pull gap is the visible difference from Fotello
output, and vertical correction is the most conspicuous missing control.
The two highest-value additions, both ✅ SHIPPED 2026-08-05:

1. **Geometry sliders** — *Straighten* (±5°, rotate-and-crop to the largest
   same-aspect inscribed rectangle, so no black corners ever) and
   *Verticals* (projective keystone correction, up to an 18% top/bottom
   inset at full strength). Output size never changes, which keeps masks,
   previews and exports indifferent to geometry. Applied after the sky
   composite so the segmentation mask stays a pure function of the original
   pixels and the preview's mask cache stays valid.
2. **Window pull (single-frame)** — local tone compression driven by
   *blurred* luminance: a bright window darkens as a region while the
   detail inside it keeps its own contrast, which is what separates it from
   a plain highlights pull. It reveals whatever the file still holds; a
   sensor-clipped pane has nothing left, and no slider can honestly invent
   it — full bracket fusion (`enfuse` on ±EV sequences) remains the
   ceiling and stays on the list below.

What will *not* close: the learned style. That requires their training
corpus. The counterweight is that every FrameFound edit is inspectable,
repeatable, versioned, and free.

## Sequencing note

Phase 1 alone delivers the workflow pain-killer (grouping, ordering,
renaming) with no new models and no external calls — it should ship first
and would be useful the same week. The editor phases then layer onto assets
that already flow through listings. The GPU upgrade already on the Later
list is what turns Phase 4 from batch into live.
