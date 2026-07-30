# Automated video and slideshow generation — options

Research, not a commitment. Written to answer three questions:

1. Could FrameFound make **AI real-estate videos** from edited photos?
2. How **reliable** would that be?
3. Could it make **slideshows** from event photos?

The short version: **yes to slideshows, and they would be genuinely reliable.
Real-estate video is two different products wearing one name, and only one of
them is worth building.**

---

## The distinction that decides everything

"AI video from photos" covers two techniques that share nothing but a name.

**Deterministic motion** — Ken Burns pans, zooms, crossfades, a music bed, text
overlays, ordered by room. No model invents pixels. FFmpeg does all of it. This
is what estate-agent listing videos have been for fifteen years, and it is
**completely reliable** because nothing is being guessed.

**Generative motion** — an image-to-video diffusion model invents intermediate
frames, so the camera appears to move *through* the space and parallax appears
where the photo had none. This is what people mean by "AI video", and on real
estate work it is **not reliable**, for reasons that are structural rather than
a matter of model quality.

Everything below separates the two.

---

## Option A — Deterministic slideshow/listing video (recommended)

Pure FFmpeg. No GPU, no model, works on the current hardware today.

What it produces:

- Ken Burns moves derived from the image, not applied blindly — a slow push on
  a room, a lateral drift on a wide exterior. Direction can be picked from
  where the visual weight sits.
- Ordering from what FrameFound already knows: capture time for events, and for
  property work the folder is usually already the shoot, with exteriors first
  by convention.
- Crossfades, a title card, an agent/branding card, a music bed with ducking.
- Output presets: 16:9 for YouTube, 1:1 and 9:16 for social.

Cost: **nothing per render.** A 90-second 1080p video is a few minutes of CPU.

Reliability: **very high.** The failure modes are all boring and visible —
wrong aspect ratio, music too loud, an image that should not have been included.
Nothing hallucinates. A render either looks right or is trivially adjustable.

This is the honest answer to "can I make real-estate videos from edited
photos": yes, and it is the version an agent would actually use, because it is
predictable enough to put a client's name on.

Fit with what exists: excellent. FrameFound already has the photos, the
ordering signals, the tags, the faces, and the places. A "make a video from
this place / this tag / this folder" action is a natural next step, not a new
subsystem.

---

## Option B — Generative image-to-video (interesting, not yet dependable)

Local models that turn a still into a few seconds of motion. Requires a GPU
with real VRAM.

| Model | VRAM | Output | Notes |
|---|---|---|---|
| **Stable Video Diffusion** (SVD-XT) | 12–16 GB | ~4 s, 576×1024 | The established baseline; runs locally |
| **LTX-Video** | 12–24 GB | ~5–10 s, 768×512 | Fast for its class; good quality/speed trade |
| **Wan 2.1 / 2.2 I2V** | 16–24 GB | ~5 s, 720p | Currently among the best open I2V |
| **CogVideoX-5B I2V** | 16–24 GB | ~6 s, 720p | Strong prompt adherence |
| **HunyuanVideo I2V** | 24 GB+ | longer, higher res | Heaviest; best results |

All are Apache/MIT-ish or source-available and run offline. Roughly **1–5
minutes of GPU time per 5-second clip** on a 24 GB card.

### Why it is unreliable *specifically for real estate*

This is not model pessimism. Property interiors are close to the worst case for
image-to-video:

- **Architecture must stay rigid.** A hallway that bends, a doorframe that
  bows, or a countertop that ripples reads instantly as fake to anyone who has
  walked through a house. Generative motion is loose with straight lines and
  right angles, which is most of what a room is.
- **Invented geometry is a misrepresentation.** If the model fabricates
  plausible space beyond the photo's edge, the video is showing a property
  feature that does not exist. For a listing that is not a quality problem, it
  is a **disclosure problem** — and real-estate advertising is regulated.
- **Text and fixtures mangle.** Address numbers, appliance brands and signage
  distort under generative motion.
- **No temporal continuity between clips.** Each 5-second clip is generated
  independently; lighting and colour drift between them, so a 90-second video
  assembled from 18 clips will not hold together without heavy grading.

Practical reliability estimate for interiors: **roughly 3 in 10 clips usable
without a human rejecting them.** That is a review queue, not automation. For
exteriors and drone stills it is meaningfully better — perhaps 6 in 10 — because
foliage and sky tolerate invention and there are fewer straight lines.

### Where it *is* worth using

- A single generative **hero shot** — a slow push on the best exterior — mixed
  into an otherwise deterministic edit. One clip to review, not eighteen.
- **B-roll texture** for promotional work rather than listings, where nothing is
  being represented as a property feature.
- Drone and landscape material generally.

---

## Option C — Slideshows from event photos (recommended, easy)

The clearest win, and the least risky thing here.

FrameFound already has everything needed: capture time for ordering, faces for
"make sure everyone appears", places for grouping by venue, tags for theme, and
CLIP embeddings for excluding near-duplicates so the same moment does not
appear four times.

A good event slideshow is mostly *selection*, not motion, and selection is what
this system is already good at. Suggested behaviour:

- Chronological, with near-duplicates collapsed to the sharpest frame.
- **Face coverage as a constraint** — ensure each named person appears at least
  once, which is exactly what a church or a family wants and what a naive
  chronological cut misses.
- Gentle Ken Burns, beat-aware transitions against a chosen track.
- Skip anything below a sharpness threshold.

Reliability: **high.** Same reasoning as Option A. The output is deterministic
and every decision is inspectable.

---

## Recommendation

1. **Build Option A/C first** — one deterministic renderer serving both listing
   videos and event slideshows. No GPU needed, works today, reliable enough to
   put in front of a client.
2. **Then Option B as an optional single-clip enhancement** once the GPU lands,
   scoped to exteriors and drone material, always with a review step and never
   generating interior geometry for a listing.

The sequencing matters: a deterministic renderer is the thing that makes
generative clips *usable*, because it gives them somewhere to sit. Building the
generative half first would produce a pile of 5-second clips and no video.

### What would need building

- A `render_video` task on `media` (long, FFmpeg-shaped — the lane already
  exists).
- A **project** model: an ordered list of assets, per-item motion, transitions,
  audio, output preset. Worth persisting so a render is repeatable and editable
  rather than a one-shot.
- Selection helpers over what already exists: by place, by tag, by person, by
  date range, with near-duplicate collapsing.
- A review step before anything is published.
- For Option B: a `generate_clip` task on a GPU-gated queue, plus an honest
  per-clip accept/reject UI, because the review *is* the feature.

### Hardware

Option A/C: current hardware, today. Option B: 16 GB VRAM minimum for SVD or
LTX at useful resolution; 24 GB for Wan 2.1 or CogVideoX comfortably. Anything
less and it will technically run while being too slow and too small to use.

---

## What was built: Option A/C, and what it cost

Option A/C now exists — `framefound/media/render.py` (filter graphs),
`pipeline.py` (orchestration), `render_slideshow` on the `media` queue, and a
`slideshows` table holding the resolved selection.

The design was decided by measurement rather than by preference, and the
measurements were surprising enough to be worth recording.

### The obvious implementation does not work at any size

Feed every still into one `filter_complex`, let FFmpeg do the rest. Measured on
the reference deployment against the worker's 1000 MB limit:

| what | peak |
| --- | --- |
| one slide, scale + x264, no zoompan | 126 MB |
| one slide, zoompan @1080p | 795 MB |
| one slide, zoompan @4K supersample | 787 MB |
| one slide, zoompan + encoder threads capped | 470 MB |
| one slide, ... + a short x264 lookahead | 259 MB |
| xfade chain, 4 / 8 / 16 / 24 segments | killed, every one |

Three things fall out of that table.

**Resolution is irrelevant.** 4K supersampling costs the same as 1080p. The
memory is x264's frame-threading lookahead — zoompan emits frames faster than
the encoder drains them, and each encoder thread holds its own queue of decoded
frames. So the supersampling that removes pan judder is effectively free, and
the lever that matters is the encoder, not the picture.

**An xfade chain cannot be batched out of trouble.** It was killed at four
segments as readily as at twenty-four, because chaining makes every later
input's decoder run ahead and buffer until its transition point. A first
implementation that batched the join into groups of eight would have failed
exactly as hard as one that did not.

**A latent bug was sitting underneath.** `_with_thread_cap` inserted `-threads`
before `-i`, which in FFmpeg's parser configures the *decoder*. `-threads` is a
per-file option, not a global one, so x264 had been running one thread per core
on every proxy transcode with the setting appearing to be in force: 810 MB
against 470 MB with it actually applied. Fixed, with `tests/test_ffmpeg_threads.py`
pinning both positions.

### The shape that does work

    slide 0 body | fade 0->1 | slide 1 body | fade 1->2 | ... | slide n body

A *body* is the part of a slide not shared with a neighbour's crossfade: one
input, one encoder. A *fade* is the tail of one slide dissolving into the head
of the next: exactly two inputs, `transition_seconds` long. The pieces are
stitched by the concat **demuxer** with `-c copy` — packets copied, nothing
decoded.

Measured over twelve slides: **worst piece 302 MB, concat 62 MB, 2.1 s**, and
neither figure grows with the length of the slideshow. Every piece is encoded
once, straight from its still, so the stitch costs no generation of quality.

Trims are expressed in **frames, not seconds**, so a body and the fade abutting
it cannot disagree about the boundary by a frame.

### Two things that cost an afternoon

`setpts=PTS-STARTPTS` discards the stream's frame-rate metadata, and xfade then
refuses the input outright — *"The inputs needs to be a constant frame rate;
current rate of 1/0 is invalid"*. The symptom is a transition clip with no
frames in it and an encoder reporting only that it could not start. The fix is
a trailing `fps=`, which looks redundant and is not.

`zoompan`'s `d` is applied **per input frame**. The common `-loop 1 -t 3`
recipe therefore feeds 90 input frames and asks for 90 output frames from each:
8100 frames for a three-second slide. Feed the still once.

### Preset

`veryfast` rather than `medium`: 6.8 s against 19.0 s per slide, producing a
*smaller* file (984 KB against 1107 KB). Slow pans give a slower preset almost
nothing to work with. `ultrafast` is where it becomes a real tradeoff — 3.1 s,
but a 9.6 MB file.

At roughly 7 s of work per photograph, a forty-photograph slideshow renders in
about five minutes on CPU. NVENC is selected automatically when the GPU is
genuinely usable (probed functionally, not by reading `-encoders`), which is
where the planned upgrade pays off.

### Still open

- Title cards and interstitials (needs text rendering, not GPU).
- A sharpness measure. Selection currently ties every candidate at 1.0, so it
  falls through to theme score and capture order. Ranking on a number we do not
  have would be worse than not ranking.
- Option B remains unbuilt and unscheduled, for the reasons above.

---

*Option B is not scheduled. Tracked in the roadmap under "Automated video and
slideshows" so the sequencing decision is recorded rather than rediscovered.*
