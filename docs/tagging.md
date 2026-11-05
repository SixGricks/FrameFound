# Tags that learn

Tag one video "Power Broom" and FrameFound goes looking for the other power
brooms. There is no training run, no GPU requirement, and no labelled dataset —
the machinery is already there from visual search.

---

## Using it

**Add a tag.** Open any asset. The Tags panel is at the top of the sidebar.
Type a name and press Add.

**FrameFound looks for more.** Within a minute or two the tag appears on other
assets as a **suggestion** — outlined rather than solid, with Yes and No.
It keeps looking: every new file is checked against every tag you have taught
as soon as it is indexed, so next week's shoot arrives already suggested.

**Answer the suggestions.** Every answer teaches. Yes makes that asset a new
example. No records a rejection, and the same wrong guess is never offered
again. The **Tags** page has a review queue if you would rather work through
them in bulk than one asset at a time.

**Correcting it is the feature**, not a workaround. The match gets sharper each
time you answer.

---

## How it works

CLIP — the model already used for visual search — puts images and text in the
same vector space. That gives two independent sources of evidence about what a
tag means.

**The words.** `"a photo of power broom"` can be encoded directly, so a
brand-new tag works before anything has been tagged. For common subjects it
works well. The guess is an average over the kinds of picture this library
actually holds — *a photo of*, *a real estate photo of*, *an aerial drone photo
of*, *a photo of a … at a farm auction* — rather than a bet on one phrasing.
A tag that shares a name with a room the listing tools recognise ("Barn",
"Kitchen", "Pasture") also borrows that room's hand-written descriptions.

**The examples.** The mean of the assets you actually tagged is what the tag
means *in your library* — which is what matters when the subject is a specific
piece of turf equipment rather than a dictionary word. A video counts as its
**three frames most like the tag**, not the whole clip: a long auction video
tagged "power broom" is mostly crowd, and averaging every frame taught "auction
crowd". The same three-best-frames rule scores the videos being considered, so
an example and a candidate are measured the same way.

Neither is enough alone:

- CLIP has no idea what a power broom is, but it holds a confident opinion.
- One or two examples overfit to whatever else was in frame. Tag a single photo
  and you may teach it "gravel driveway on an overcast day".

So the prototype is a **blend that shifts toward your examples as they
accumulate** — a prior your evidence is allowed to overrule. At three examples
the two carry equal weight; beyond that your examples dominate.

### The match bar

A single similarity cutoff cannot work, and the reason is worth stating plainly
because it caught this implementation out. Measured on this library
(ViT-B/32, 3,000 frames):

| Comparison | min | median | p95 | max |
|---|---|---|---|---|
| text vs image | 0.119 | 0.189 | 0.235 | 0.294 |
| image vs image | 0.270 | 0.458 | 0.534 | 0.97 (near-duplicates) |

CLIP's text and image embeddings sit in **different regions of the space**.
A cosine score means nothing without knowing which regime produced it — and a
tag's prototype starts text-heavy and becomes image-heavy, so it crosses
between them as it learns. The first implementation used a fixed 0.78 floor
calibrated on image-vs-image; it scored 9,425 frames and matched nothing,
because 0.78 is unreachable for anything with text in it.

The bar is now taken from **the distribution of scores in each run** — a high
percentile of every asset just scored. That is regime-independent by
construction and expresses the real intent: *suggest what stands out from the
library*, not what merely scores highly.

The bar itself is set **just below the weakest example you accepted** — a tag
must match its own training data — and the percentile is a floor under it.
With fewer than five examples the floor is the top 1% of the library. That
turned out to be a ceiling as well: a subject on one asset in ten could never
be fully found, however many examples said so. From **five examples** the
floor drops to the top 10%, because five that agree are evidence enough for
their own bar to be trusted further down. It stays a floor, and what clears it
still lands in the review queue — a wrong guess costs you a click, a missed one
costs a client a shot. The bar is capped at 0.98 so a tag whose examples are
all near-identical does not freeze at exactly what it was taught.

**Rejections veto, one candidate at a time.** The first version raised the
whole bar above the closest thing you had ever rejected. One near-miss could
lock a tag: everything less similar than that single mistake — every true
match included — fell below it. Now a candidate is dropped only when the
nearest thing you have judged, accepted or rejected, is a rejection: it looks
more like something you said was wrong than like anything you said was right.
A rejection clears its own neighbourhood and leaves the rest of the library
alone. Before you have accepted anything there is nothing to weigh it against,
so it vetoes only near-copies of itself.

Every bar carries a sentence explaining itself, shown on the Tags page. If a
suggestion looks strange, that sentence tells you why it qualified.

### What each answer does

| You do | Stored as | Effect |
|---|---|---|
| Add a tag | `manual` | Positive example. Overrides any past rejection. |
| Accept a suggestion | `confirmed` | Positive example. |
| Reject a suggestion | `rejected` | Negative example; vetoes its look-alikes. |
| Remove a tag | `rejected` | Same — **not** a deletion. |

Removing is stored as a rejection on purpose. Delete the row and the same
suggestion returns on the next run, which is the difference between a system
that learns and one that nags.

---

## Measured behaviour

From **one** tagged DJI aerial photo, on the production library:

- 9,425 frames scored in about 4 seconds
- bar self-calibrated to 0.479
- 63 assets above it, 60 offered (the cap on suggestions waiting per tag)
- **52 of 60 were DJI drone files**; the top 11 all were

That is roughly 87% precision on a verifiable proxy from a single example. The
remaining 8 are the interesting ones — some will be genuine aerials from other
cameras, which is exactly the point, and some will be wrong. You decide, and
the bar tightens.

---

## Notes and limits

- **Assets need embeddings.** A tag can only match media that has been through
  visual indexing. The moment a file is indexed it is scored against every
  tag you have taught, using each tag's bar from its last full run; nothing
  you have already judged on that file is touched. When you edit a tag, its
  new shape and the bar measured for it are saved together once the library
  has been scored against it; until then new files are judged by the
  previous pair, never by a mix of the two. A **nightly sweep** (02:40
  Eastern) re-runs every tag over the whole library as a safety net, and
  refreshes each bar as the library grows.
- **At most 60 suggestions per tag wait for your answer.** A run — yours,
  or the nightly sweep — only tops a tag back up to 60, so a queue nobody is
  working through stops growing instead of gaining 60 a night. Every answer
  frees a slot, and the next run fills it with the strongest match not yet
  offered. New files have headroom to 120 per tag, so a backlog does not
  stop next week's shoot being suggested; past that, new matches wait for
  the sweep like everything else. Claude's suggestions count towards the
  same 60.
- **Video is scored on its three best frames**, so a subject on screen for a
  few shots of a long clip still surfaces the clip.
- **Tag names are matched loosely.** "Power Broom", "power broom" and
  "POWER BROOM" are one tag; the first spelling you used is kept for display.
- Suggestions run automatically after learning, on new media, and nightly.
  Set `FRAMEFOUND_TAG_AUTOSUGGEST=false` to stop the new-media check and the
  nightly sweep.

### Where it will struggle

- **Fine distinctions between similar machines.** CLIP sees shapes and context;
  two brands of the same implement may be indistinguishable to it. More
  examples and more rejections help, but there is a ceiling.
- **Subjects that are small in frame.** A power broom filling the shot is far
  easier than one parked in the background.
- **Abstract tags** ("good take", "client approved") have no visual signature.
  They still work as manual labels; they will not spread usefully.

---

## A second opinion from Claude

The learner spreads a tag you chose. It cannot propose "horse barn" about a
picture nobody has tagged, and it cannot read the badge on a machine. Claude
can look at a thumbnail and say what it is — in the words your vocabulary
already uses where one fits, and in new words where none does.

Both routes below end in the same place: a **suggestion** in the review queue
above, marked with who made it, that becomes an example only when you say
yes. A second opinion that could confirm itself would not be a second opinion.

**In conversation**, through the MCP connector ([mcp.md](mcp.md)): "look at
the untagged pictures from Tuesday and tell me what they are." Claude views
each thumbnail, proposes tags, and records your yes or no when you give it.
Costs nothing beyond your Claude subscription.

**On the server**, with the Anthropic key on the Security page: **Review
with Claude** on the Tags page sends a batch of untagged pictures to the
Claude API, or turn on the nightly sweep to do it on its own. "Untagged"
means newest first, with a thumbnail, no tag or answer of yours, no
suggestion from Claude, and not already reviewed by the API. The learner's
own guesses do not count: new files are scored against your tags the moment
they are indexed, and if a guess were enough to skip a picture, most of a
new shoot would never reach Claude — the one reviewer that can offer a word
you have not taught yet. Each answer
passes an **admission policy** before it reaches you — by default a
vocabulary match needs 50% confidence, a brand-new tag 75%, three words at
most, six per picture. That policy is `admit()` in `framefound/ai/tag_review.py`,
written to be changed: it is the one judgement in the reviewer that is about
*your* library.

### The budget

Everything FrameFound asks the Claude API — the reviewer, auto-edit, naming —
goes into one ledger with the token counts the API reported, and the
**monthly budget** on the Security page is enforced from it: before every
call, the software checks that this month's spend plus the next call's
estimate stays under the cap, and a run that would cross it stops and says
so. The page shows what has been spent. Zero pauses all calls without
removing the key.

What it costs, from the price sheet (2026-11): the reviewer's default model
(Haiku 5.5) is about $0.0004 a picture, so $5 a month covers roughly ten
thousand pictures; Sonnet 5.5 about $0.008; Opus 5.5 about $0.015. The
rules and vocabulary are cached across a run, so the repeats bill cheaper.

### Where it fits and where it does not

- It reads thumbnails (512px). A tractor filling the frame is easy; a model
  number on a badge is not.
- Video is judged from its poster frame, not every shot.
- It never names people. Faces are the People page's job.
- The learner still does the spreading: once you confirm Claude's "horse
  barn" on three pictures, the match takes it from there, free.

---

## Reference

| | |
|---|---|
| Learning maths | `framefound/ai/tagging.py` |
| Tasks | `framefound/processing/tag_tasks.py` (`suggest_tags_for_asset` runs from the embedding stage; `suggest_all_tags` is the nightly sweep) |
| Claude reviewer | `framefound/ai/tag_review.py`, `framefound/processing/tag_review_tasks.py` |
| Spend ledger and cap | `framefound/ai/spend.py` |
| API | `/api/v1/tags`, `/api/v1/tags/assets/{id}`, `/api/v1/tags/review/claude` |
| Tables | `tags`, `asset_tags` (`origin`: clip / claude / mcp), `ai_usage` |
| UI | `components/TagEditor.tsx`, `app/tags/page.tsx`, `components/AiEditCard.tsx` |

Related: [location.md](location.md) uses the same embeddings to infer where
media was shot.
