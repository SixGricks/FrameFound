# Looks for video clips

Status (2026-11-13): built and tested, **not yet run on a Mac with a real
model, and no file has yet been loaded in Premiere** (see
[Not yet checked](#not-yet-checked)). Part L4 of the
[local AI plan](local-models.md#phases).

A look is a colour grade for one clip: exposure, contrast, temperature,
tint, saturation, vibrance, darker shadows and removing a colour cast — the
same sliders as the photograph editor, and the same engine. FrameFound shows
you a few versions of the clip side by side, you choose one by eye, and you
take it to Premiere as a `.cube` LUT file. Nothing is ever chosen for you,
and nothing about the clip itself changes.

## Where it is

On a video's page, the **Look** card, under the transcript. The player, the
transcript and the tags stay where they were.

## The three looks

The card shows the same moment of the clip three ways, always in this order:

- **The local model's suggestion** — what the Looker (the local model with
  that role on the **AI models** page) suggested for this clip, with its
  one-line reason and which model and question it came from. Empty until
  you press **Suggest a look**.
- **Your look** — learned from you. Once you have chosen a look for 8
  other clips, it is worked out from those choices for clips like this one
  ("learned from the 14 clips you have chosen a look for"). Until then it is
  your photograph look (the one learned from shipped listing photographs),
  fitted to what a LUT can carry ("Your photograph look"). With neither, the
  tile says so.
- **No change** — the clip as it was shot.

Every picture is the still run through the very LUT you would download, so
what you see is what the file does. Under the pictures is a strip of the
clip's moments (the middle of each shot, or sampled frames when the clip has
no shots yet): pick one to see how each look holds elsewhere in the clip.
Press a picture to see it large and flip between the looks with ← and →;
**Back to all three** returns. **Show the numbers** lists a look's sliders.
No look carries a score or a "recommended" mark: judge them by eye.

**Use this look** chooses one. **Adjust before using** opens the eight
sliders, starting from the look you pressed last, with a fourth picture
that follows them; **Use my version** chooses that. **Clear the choice**
takes it back. A choice is stored per clip, with who made it and when; the
model's suggestion stays a suggestion and never replaces a choice.

## Asking the local model

**Suggest a look** (later **Ask again**) asks the Looker about the clip's
reference still — the middle of its longest shot — and what was measured
about that shot. It runs on the Mac install's local-AI lane, like a model's
Test, and the tile says how it is going ("Waiting its turn…", "Waiting for
the overnight window (starts 21:30)…"). **Cancel** stops it. A model that
fits only overnight is not refused: the request waits for the window. If the
model answers with something that is not slider values, nothing is stored
and the tile says so, with **Suggest again**.

What the model may suggest is bounded to about half of each slider's range
and cleaned before it is stored; anything a LUT cannot carry is named, not
used.

Suggestions come only from the Mac install. On the container server the
button is off and says why; the learned look, choosing and the LUT downloads
work there as they are. With no model holding the Looker role, the card says
so and links to the AI models page.

## Taking it to Premiere

**Download LUT (.cube)**, under the chosen look (or on any tile), gives one
file per clip, named after it. Its size select offers **Standard (33)**,
about 1 MB and right for nearly every look, and **Finer (65)**, a 7 MB file
for strong looks. The first lines of the file say which look it is, for
which clip, what it was measured on and, for a model's look, which model
made it.

In Premiere, select the clip, open **Lumetri Color**, and load the file
under **Creative › Look › Browse** (or **Basic Correction › Input LUT ›
Browse**). The card's "Using the LUT in Premiere" says the same.

**A whole folder.** The foot of the card counts the clips in the same
folder — how many have a chosen look, how many a suggestion waiting for you,
how many nobody has looked at — and lists them as links, so the next clip is
one press away. **Suggest looks for the N not looked at** asks the model
about each of those (skipping clips that already have a suggestion, a
choice or a request on its way, or no still yet). **Download this folder's
looks (.zip)** gives one `.cube` per clip with a chosen look, named after
the clip, plus a `READ ME.txt` saying how to load them. A zip holds at most
200 looks, or 40 at the finer size.

## What a look cannot do

A LUT changes colour only, the same way on every frame. It cannot straighten,
lift shadows region by region, pull a window or replace a sky: those stay in
the photograph editor, and the card names whatever a suggested look wanted
that a LUT could not carry ("A LUT cannot carry: lifting the shadows").

It is made for **ordinary (Rec. 709) video as it looks in FrameFound's
preview**. If a clip looks different in Premiere than in FrameFound before
any LUT — log or HDR footage that Premiere converts — the look will land
differently. FrameFound cannot tell which clips those are, so it says this
on the card, in the file and here.

## Not yet checked

- **Loading the file in Premiere on the mini.** The file follows Adobe's
  Cube LUT Specification 1.0, and a reader that follows it too gives
  FrameFound's picture. Where Premiere loads it from (Creative › Look or
  Basic Correction › Input LUT) comes from third-party guides, because
  Adobe's own page could not be read.
- **Keeping looks in Premiere's own menus.** Adobe staff described LUT
  folders under `~/Library/Application Support/Adobe/Common/LUTs/Creative`
  (the Look menu) and `…/Technical` (the Input LUT menu), read when Premiere
  starts, in a 2016 answer; a 2025 reply says they no longer exist on a
  fresh install. Worth trying, not relied on.
- **One reported Premiere bug (April 2025):** browsing for a LUT from the
  Creative section failed for some people; turning off "Auto-Refresh Input
  LUT/Look" in the Lumetri panel menu was the workaround, and Input LUT is
  the other way in.
- How Premiere interpolates the table, and whether its log detection or tone
  mapping changes what a Look receives, were not found.

## Demo mode

On a review copy with `FRAMEFOUND_LOCAL_AI_DEMO=1`
([CONTRIBUTING.md](../CONTRIBUTING.md#looking-at-the-ui-with-a-seeded-catalogue)),
Suggest is answered by the demo's made-up model: the card says so in a banner,
the tile carries a `demo` pill, and a downloaded file says DEMONSTRATION on
its second line. The seed gives the one clip in GELCO's Hudson National
folder a suggestion and the first clip by name in its LedgeRock folder a look
set by hand, so both states show on first load. It does that once: clear the
choice or replace the suggestion and seeding again leaves the clip as you
left it.

## For developers: the API

Router `framefound/api/v1/clip_looks.py`, prefix `/api/v1/clip-looks`. Any
signed-in user; no admin, no audit. Errors are
`{"error": {"code", "message", "detail"}}`, where `message` is the sentence
the page shows. Times are ISO UTC (`…Z`). No answer carries a colour
difference, score or rank. Stored in `clip_looks`
([data-model.md](data-model.md)); the files are built when asked for, never
stored.

| Route | What it does |
|---|---|
| `GET /{asset_id}` | The card: `stills` (each shot's middle, or sampled frames; at most 12), `reference_ts_ms`, `candidates` (always three: `model`, `learned`, `none`), `chosen` or null, `suggest` (`mode` ready \| demo \| unavailable, `ok`, `note`, `job`), `folder` (path, counts, up to 200 `others` with their state), `lut` (`sizes` [33, 65], `default`, `assumes`). `problem` is set, and nothing else is offered, when the clip has no still yet |
| `GET /{asset_id}/preview.jpg?source=model\|learned\|none\|chosen&ts=&size=33\|65` | One look on one still, through the LUT. `ts` must be one of the listed moments (400 otherwise); `none` is the still itself |
| `POST /{asset_id}/preview?ts=&size=` | Sliders set by hand (`exposure` −2..2; `contrast`, `temperature`, `tint`, `saturation`, `vibrance` −1..1; `shadows` −1..0; `auto_wb` 0..1), as a picture |
| `PUT /{asset_id}/choice` | `{source: model\|learned\|none\|custom, recipe}` (recipe only for `custom`): the clip's one choice, copied as offered now. A chosen model look keeps the suggestion's provenance and `demo` flag |
| `DELETE /{asset_id}/choice` | Clears it |
| `GET /{asset_id}/look.cube?source=chosen\|model\|learned&size=` | The `.cube` file (plain ASCII, LF), `<clip> - FrameFound <source>.cube`. "No change" needs no file (400) |
| `GET /folder.zip?asset_id=&size=` | One `.cube` per catalogued clip directly in that clip's folder with a chosen look (a repeated name gets " (2)"), plus `READ ME.txt`. 404 when none is chosen; at most 200, or 40 at size 65 |
| `POST /{asset_id}/suggest` | 202 `{job}`: asks the Looker (a `clip_look` job on the local-AI lane, priority 0). 409 where models cannot run, with no Looker, with no still, or when one is already on its way |
| `POST /{asset_id}/suggest/cancel` | 200 `{job}`; 409 when nothing is waiting |
| `POST /folder/suggest` | `{asset_id}` → 202 `{queued, already, no_still}`: one job (priority 5) per clip in the folder with no suggestion, choice or request yet |

A candidate is `{source, available, note, recipe, in_words, reason,
left_out, measured_at_ms, basis, examples, provenance, made_at, demo,
version}`; `version` goes into a preview URL as `v`, so a changed look is
fetched afresh. A job is `{id, state, message, detail (administrators only),
not_before, done, total, cancelling, can_cancel}`. The worker side is
`framefound/processing/colour_jobs.py`; what a LUT carries is decided in
`framefound/media/lut.py`.
