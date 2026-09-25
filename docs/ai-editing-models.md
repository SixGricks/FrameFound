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

## Recommendation

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
