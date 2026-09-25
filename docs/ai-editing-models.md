# AI editing: models, measured quality, and cost

2026-09-25. Asked: find newer and better models for the AI edit, including
through Claude's API, and estimate the cost; keep going with the 130 Davis Rd
comparison against Fotello, now across more 2026 shoots.

## How FrameFound's AI edit works (and why that shapes the choice)

A model looks at a 768px preview and returns slider values; FrameFound's
engine renders them at full resolution, locally. The model never produces
pixels, so the photograph cannot gain a window, lose a power line or change a
roofline, and the 6000×4000 original is edited at full size. The question
"which model?" is therefore "which model picks sliders best?", not "which
model makes the prettiest picture?".

## The candidates

**Claude, through the API FrameFound already uses** (prices from
platform.claude.com/docs/en/about-claude/pricing, read 2026-09-25):

| Model | $/M tokens in / out | Notes |
|---|---|---|
| Sonnet 5 (`claude-sonnet-5`, current default) | 2 / 10 | Forced tool call works. |
| Opus 5.5 (`claude-opus-5-5`) | 4 / 20 | Refuses a forced tool call and always thinks; FrameFound now offers the tool with a strict schema at low effort instead. |
| Fable 5.1 (`claude-fable-5-1`) | 10 / 50 | Positioned for long-horizon reasoning; same request shape as Opus. Returned "overloaded" (529) on 8 of 70 calls. |
| Haiku 4.5 | 1 / 5 | Retirement "not sooner than Oct 15, 2026" — not worth adopting. |

**Generative image editors** — Gemini 3.1 Flash Image ($0.034–0.15/image),
Gemini 3 Pro Image, OpenAI gpt-image-2, FLUX.1 Kontext/Fill and FLUX.2 ($0.04–0.08),
Seedream 4.0 ($0.03). None outputs the R8's 6000×4000 frame, and all of them
regenerate content: fine for a disclosed virtual staging, wrong for the base
edit of an MLS photo. Bright MLS (PA/MD/NJ/DE/VA/WV/DC, policy of Feb 28,
2024) requires virtual staging to be disclosed and prohibits adding views
that are not physically possible or removing things outside the owner's
control (power lines, towers, highways). If a generative tool is used at all,
it belongs inside a mask (sky only), blended into the local full-resolution
render. FrameFound's sky compositor already does sky replacement locally,
with a real sky photograph and no generation.

**Real-estate editing services with APIs** (turnkey alternatives to Fotello):
Imagen AI ≈ $0.32/photo; Autoenhance.ai ≈ $0.30–0.58 (unverified);
BoxBrownie $2.00 (24 h). **Fotello** lists $16/listing (Essential, 50 photos)
and $18 (Ultimate, 75), extra photos $0.50 — how that bills at ~4–5 listings
a month is unverified; your invoice is the real number.

**Small local models** that could run on this server's CPU: Illumination
Adaptive Transformer (~90K parameters, Apache-2.0), Image-Adaptive 3D LUT
(<600K, Apache-2.0). Zero-DCE++ is tiny but licensed non-commercial. None is
tested on the AVX-less Xeons yet.

## Cost — measured, not estimated

The bake-off logs the token counts the API returns. On 70 Davis Rd photos,
with FrameFound's instructions cached:

| | $ / photo | 50-photo listing | 3,000 photos / year | with the Batch API (−50%) |
|---|---|---|---|---|
| Sonnet 5 | $0.0069 | $0.35 | **≈ $21** | ≈ $10 |
| Opus 5.5 | $0.0114 | $0.57 | **≈ $34** | ≈ $17 |
| Fable 5.1 | $0.0324 | $1.62 | ≈ $97 | ≈ $49 |
| Naming only (Sonnet, estimated) | ≈ $0.0025 | $0.13 | ≈ $8 | — |
| Imagen AI (list price) | ≈ $0.32 | $16 | ≈ $960 | — |
| Fotello (list price) | — | $16–18 | ≈ $850–950 at 53 listings | — |

Output tokens are two-thirds of the Claude cost (≈520 per photo on Sonnet —
the sliders, the one-line note, the caption and the slug). The server has no
GPU and pays no per-photo fee for anything local: preset, learned look, sky
compositing and rendering cost electricity.

## Measured quality — the seven-shoot bake-off

`python -m framefound.ops.bakeoff` paired every final that shipped with the
original it was made from, 429 photographs:

- **Fotello's edits:** 130 Davis Rd (70) and Skook Portfolio (54).
- **Your own shipped sets:** 325 Cambridge Rd, 111 Water St, 967 Farmdale
  Rd, 475 Cocalico Rd, 678 Susquehanna Trail.

Pairing was by name, and by picture content where the organizer had renamed
the files. Only 3 of 432 finals found no original, and those belong to
another property.

Each candidate edit is measured against the final as mean colour difference
(CIE ΔE), region by region, after lining up the final's framing. 126 of the
429 finals had been re-framed (lens correction, straightening or a crop),
and that is not counted as colour. For scale: under 2 is hard to see side by
side, and over 10 is a different-looking photograph.

| | mean ΔE | closest on | interiors | exteriors | $ / photo |
|---|---|---|---|---|---|
| Untouched original | 15.0 | 28 | 15.1 | 15.0 | — |
| Preset (fixed sliders, after today's white-balance fix) | 12.7 | 23 | 13.8 | 12.1 | 0 |
| Sonnet 5 | 13.3 | 7 | 14.6 | 12.7 | 0.0070 |
| Opus 5.5 | 11.6 | 35 | 12.5 | 11.2 | 0.0109 |
| Fable 5.1 (Davis Rd only) | 11.6 | — | 11.9 | 11.5 | 0.0324 |
| **Learned look** | **9.3** | **226** | **9.6** | **9.2** | **0** |
| Learned look + Opus, averaged | 9.7 | 59 | 10.3 | 9.4 | 0.0109 |
| Best possible sliders (fitted to the answer) | 6.0 | — | 6.9 | 5.6 | — |

**The learned look** is "edit this photo the way the most similar shipped
photos were edited": the sliders fitted to each final, looked up by what the
original looked like. It was scored honestly: each shoot was predicted by a
look learned from the other six only. It covers 68% of the improvement the
sliders can reach; Opus covers 41% and Sonnet 24%. It is closest to what
shipped on more than half the photos, indoors and out, and on Fotello's own
Davis Rd sheets it reproduces the bright neutral interiors and the vivid
lawns. It runs on this machine and costs nothing per photo.

Findings, in order of how much they matter:

1. **Your look, learned, beats every model.** A newer or larger Claude model
   does not close the gap. Fable 5.1 is no better than Opus 5.5 at three
   times the price, and Sonnet 5 lands further from what shipped than the
   free preset, because it brightens interiors and adds contrast.
2. **The model still earns its call for what the look can't do:** naming each
   photo (caption and file-name slug), straightening verticals, and flagging
   a dull sky. Averaging the model's tone into the look's made it worse
   (9.7), so auto-edit now takes the tone from the look and the rest from
   the model.
3. **Sky replacement is Fotello's habit, not yours.** It moved Davis Rd
   closer to Fotello (≈1 ΔE) and moved your own sets further from what you
   shipped, where you kept the real skies. It stays an operator choice per
   listing.
4. **The preset's white balance was broken on exteriors.** It read a lawn as
   a green cast and turned overcast skies purple; fixed today (Davis Rd:
   18.1 → 12.3).
5. **What's left** (9.3 against a slider ceiling of 6.0) is partly per-photo
   judgment, and partly things sliders can't do: Fotello's lens correction
   and crops (29% of finals), its skies, and local fixes. Every shoot that
   ships adds examples; re-running `--learn` after a few more should narrow
   it.

The bake-off itself cost **$9.72** in API calls (Sonnet and Opus on all 429,
Fable on Davis Rd).

## The live test: 901 Smyrna Rd, and what ΔE hides

This listing was not in the training set. By the ΔE measure, auto-edit
(learned look + Sonnet) landed at 8.0 from what went to MLS, and the learned
look alone at 7.7. **By eye, the operator found it much worse than that, and
side-by-sides at full size agree.** ΔE averages colour over coarse blocks. It
cannot see the failures, which are local:

- **Bathroom:** whites blown, tile pattern lost, a glowing halo around the
  door, wood pushed orange.
- **Bedrooms:** grey, dim ceilings and dark corners (uncorrected lens
  vignetting).
- **Exteriors:** over-saturated and contrasty, where the MLS set is bright and
  airy.

The learned look sometimes picks incoherent slider combinations: a −0.77
shadow cut in that bathroom, and +0.53 vibrance on the garage. The underlying
limit is the engine. A dozen global sliders can match average colour but not a
real editor's local tone work.

**Conclusion: FrameFound's own editor is not a Fotello replacement.** ΔE stays
useful only for ranking methods against each other, labelled as colour-only.

A second idea was tried and dropped. A generative model (OpenAI, Gemini, Grok)
would make a small edit, and a guided-filter colour transform would carry it
onto the full-size original. Tested with the MLS finals as the target, the
result was foggy and washed out, with colour bleeding across edges. The finals
are lens-corrected and cropped, so they don't line up pixel for pixel, and
generative outputs wouldn't either.

## What Fotello actually costs, and the alternatives at 750 photos/month

**Fotello Ultimate: $220/month for 10 listings of up to 75 photos**, all
used most months (2–3 shoots a week). That is about **750 photos/month,
≈ $0.29/photo**, renewing Oct 17. Fotello's API is only on its partner plan
(50+ listings a month), so it cannot be automated at this volume.

| Option | $/month at 750 | Fully automatic from FrameFound | Quality evidence | Catch |
|---|---|---|---|---|
| **Fotello (today)** | **$220** | no | the baseline; top score (73) in the one scored comparison¹ | manual upload/download |
| **Autoenhance.ai** | **≈ $232** (500-photo plan $154.99 + $0.31 each over) | **yes**: documented REST API, webhooks, full-res | reviews: strong window pulls and skies, sometimes "a little unnatural"; no head-to-head | unproven on these shoots; previews free, pay per full-size download, so a trial is nearly free |
| Stager AI | ≈ $188, ≈ $235 with skies (separate job) + a subscription | yes | none | API aimed at 1,000+ photos/month |
| Imagen AI | ≈ $240 | only through sales (Business plan) | 42 vs Fotello's 73¹ | weakest in the scored test |
| AutoHDR | $440–500 | no public API | 66¹ | twice the price |
| Claid.ai | ≈ $44 | yes | — | no sky, verticals or window pull |
| Cloudinary (Enhance + Generative Restore, sky via Generative Replace) | ≈ $89–99 (Plus plan: 225 credits; about 150 for Enhance and Restore on 750 photos at 100 transformations each, about 24 for skies at 120, about 15 for storage and delivery) | yes (URL/upload API; 40 MP limit on paid plans) | Enhance is global (exposure, colour, white balance), trained on "vacation or food shots" | no window pull, verticals, local tone work, twilight or staging; the sky is a generative redraw; Restore (denoise, sharpen) is good for drone/high-ISO |
| FrameFound's own editor | ≈ $5 | yes | clearly worse by eye (above) | not a replacement |

¹ WGAN-TV, Sep 2025: one reviewer's scores over 24 photo sets.

**General-purpose image models.** OpenRouter passes through provider prices
plus 5.5% on credit purchases: one account, not better processing.

| Model | $/month at 750 | Largest output (the R8 is 24 MP) |
|---|---|---|
| Gemini 3.1 Flash Image | $57–113 | 17 MP |
| Gemini 3 Pro Image | $90–180 | 17 MP |
| OpenAI gpt-image-2 | ≈ $124 + input | 8.3 MP |
| xAI Grok Imagine | $15–38 | 2K |
| FLUX / Seedream / Qwen | $15–60 | 2–4 MP |

None of them outputs 24 MP. All regenerate the whole frame, so property
details can change, which is a Bright MLS risk. None has a published
comparison with Fotello. They suit disclosed one-offs such as twilight and
virtual staging, not the base edit. Adobe's Lightroom API was retired on
July 31, 2026.

## Recommendation (revised the same day)

1. **Trial Autoenhance on one shoot through its web app**: free previews, side
   by side with Fotello's result for the same shoot, judged by eye.
2. **If it matches**, connect its API to listings: send the selects, get
   full-size edits back named, indexed and packaged. Same cost (≈ $220–232),
   no manual upload or download.
3. **If not, stay on Fotello** and let FrameFound handle everything around
   it: culling, naming, the Photo Index and contact sheets for the upload,
   and the delivery package after.

## Earlier recommendation (superseded)

- **Keep the learned look installed**: done, 429 examples,
  `data/looks/learned.json`. Auto-edit now uses it for tone.
- **Keep Sonnet 5 as the model**: with the look setting the tone, the model
  only names and straightens, so Opus's better tone judgment no longer
  matters. That comes to about $0.007 per photo, or $21 a year at 3,000
  photos. With no learned look, choose Opus 5.5 on the Security page.
- **Decide on Fotello with one live listing.** Auto-edit the next shoot,
  compare it with Fotello's result for that listing, and stop the batches if
  it holds. The Drive folder `Claude outputs/Fotello bake-off 2026-09-25` has
  a side-by-side sheet for every photo here. `130 Davis Rd - learned look vs
  Fotello` is the one to look at first.
- **Next engine work, by measured gap:** lens-profile correction for the R8's
  lenses (the Lensfun database, open source), then a sky suggestion for
  Fotello-style exterior sets.

To refresh after more shoots ship: run a bake-off per new shoot
(`--models configured` or no models at all, which is free), then
`--learn` over all of them.
