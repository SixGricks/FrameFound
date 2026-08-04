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

### Phase 2 — Non-destructive color editor (medium)
Edit-recipe table + preview render endpoint + slider panel + auto button.
**Batch apply** a recipe across a selection — the Imagen.ai move that makes
30 photos of one property consistent in one gesture. Export honors recipes.

### Phase 3 — Sky replacement (medium)
Mask from segmentation, composite from the sky library, foreground color
match, feather/horizon controls in the editor. Runs on `vision` queue;
seconds-to-a-minute per image on current CPUs is acceptable here.

### Phase 4 — Object removal (medium, pace set by Phase 0)
Brush/lasso mask in the editor → queued LaMa inpaint → result appears as
the next recipe version. On current CPUs this is "mark five photos, get
them back in ten minutes" — honest and still far faster than Photoshop
round-trips. A GPU later makes it near-live without any redesign.

### Phase 5 — Claude provider (small, optional)
Room labels/captions/filenames/QA behind the sealed-key provider pattern.

## Sequencing note

Phase 1 alone delivers the workflow pain-killer (grouping, ordering,
renaming) with no new models and no external calls — it should ship first
and would be useful the same week. The editor phases then layer onto assets
that already flow through listings. The GPU upgrade already on the Later
list is what turns Phase 4 from batch into live.
