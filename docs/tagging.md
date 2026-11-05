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

**The words.** `"a photo of a power broom"` can be encoded directly, so a
brand-new tag works before anything has been tagged. For common subjects it
works well.

**The examples.** The mean of the frame vectors you actually tagged is what the
tag means *in your library* — which is what matters when the subject is a
specific piece of turf equipment rather than a dictionary word.

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
percentile of everything just scored. That is regime-independent by
construction and expresses the real intent: *suggest what stands out from the
library*, not what merely scores highly.

On top of that, two rules from your own judgements:

- **Never below** the weakest example you accepted, minus a small margin — a
  tag must match its own training data.
- **Always above** the closest suggestion you rejected, plus a margin — so a
  known-wrong answer cannot come back.

Whichever is stricter wins, capped at 0.98 so a tag whose examples are all
near-identical does not freeze at exactly what it was taught.

Every bar carries a sentence explaining itself, shown on the Tags page. If a
suggestion looks strange, that sentence tells you why it qualified.

### What each answer does

| You do | Stored as | Effect |
|---|---|---|
| Add a tag | `manual` | Positive example. Overrides any past rejection. |
| Accept a suggestion | `confirmed` | Positive example. |
| Reject a suggestion | `rejected` | Negative example; raises the bar above it. |
| Remove a tag | `rejected` | Same — **not** a deletion. |

Removing is stored as a rejection on purpose. Delete the row and the same
suggestion returns on the next run, which is the difference between a system
that learns and one that nags.

---

## Measured behaviour

From **one** tagged DJI aerial photo, on the production library:

- 9,425 frames scored in about 4 seconds
- bar self-calibrated to 0.479
- 63 assets above it, 60 offered (the per-run cap)
- **52 of 60 were DJI drone files**; the top 11 all were

That is roughly 87% precision on a verifiable proxy from a single example. The
remaining 8 are the interesting ones — some will be genuine aerials from other
cameras, which is exactly the point, and some will be wrong. You decide, and
the bar tightens.

---

## Notes and limits

- **Assets need embeddings.** A tag can only match media that has been through
  visual indexing. Newly scanned files will not be suggested until then; use
  **Re-learn** on the Tags page once they are.
- **60 suggestions per run** keeps the review queue workable. Answering some
  and re-learning continues from where it stopped.
- **Video is scored per frame**, so a subject appearing in one shot of a long
  clip still surfaces the clip.
- **Tag names are matched loosely.** "Power Broom", "power broom" and
  "POWER BROOM" are one tag; the first spelling you used is kept for display.
- Suggestions run automatically after learning. Set
  `FRAMEFOUND_TAG_AUTOSUGGEST=false` to require pressing Re-learn instead.

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
with Claude** on the Tags page sends a batch of untagged pictures (newest
first, those with a thumbnail and no tag or suggestion of any kind) to the
Claude API, or turn on the nightly sweep to do it on its own. Each answer
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
| Tasks | `framefound/processing/tag_tasks.py` |
| Claude reviewer | `framefound/ai/tag_review.py`, `framefound/processing/tag_review_tasks.py` |
| Spend ledger and cap | `framefound/ai/spend.py` |
| API | `/api/v1/tags`, `/api/v1/tags/assets/{id}`, `/api/v1/tags/review/claude` |
| Tables | `tags`, `asset_tags` (`origin`: clip / claude / mcp), `ai_usage` |
| UI | `components/TagEditor.tsx`, `app/tags/page.tsx`, `components/AiEditCard.tsx` |

Related: [location.md](location.md) uses the same embeddings to infer where
media was shot.
