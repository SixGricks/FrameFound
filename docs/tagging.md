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

**Answer them in bulk.** On the Tags page, press **Review** beside a tag; the
page scrolls to its queue. The queue is strongest first, so there is usually
one line between right and wrong — draw it, then answer everything above it
at once:

- **Click a picture to look at it.** It opens over the queue: a clip plays, a
  photograph is shown large. Nothing is lost by looking — the queue and the
  selection are exactly as you left them when you close it. A clip is
  suggested for its three best frames, which the small picture may not
  show, so play it before you answer.
- **Tick the box** in a picture's corner to add or remove one; **↑ down to
  here** on a tile selects it and every stronger one above it;
  **shift-click** on a picture or a box selects a range; **Select all** takes
  the whole queue.
- The bar at the foot of the screen answers the selection: **Yes**, **No**,
  or **Yes to these, no to the other N** — the whole queue in one go, the
  selected tiles yes and every other tile *shown* no. **Y** and **N** on the
  keyboard do the same; **Esc** clears the selection. The keys work straight
  after a tick — there is no need to click anywhere else first — and a key
  held down counts once.

One request answers them all and the tag re-learns once. Only suggestions
still waiting change: anything already answered elsewhere — on the asset
page, in another tab, by Claude in chat — is left exactly as it was. Claude
can do the same through the MCP connector (`decide_tags`), when you tell it
your answers.

**Or one at a time, in the viewer.** With a picture open, **←** and **→**
step through the queue. **Y** and **N** answer the one on screen and move to
the next; **Space** adds it to the selection instead, to be answered with the
rest; **Esc** closes the viewer and leaves the selection alone. Every key has
a button beside it, and **Open full page** opens the asset — transcript,
scenes, details — in a new tab, so the queue stays where it is. While the
player itself has focus the arrows scrub and Space pauses, as in any player.
A clip with no playable copy (a library with proxies turned off, or the drive
that holds them away) shows its poster frame and says which.

**The same viewer is on the other grids**, with the same keys, and none of
them loses a selection or an open panel by looking:

- **People.** The magnifier on a face opens the photograph or clip it was
  found in, with the face in question shown beside the buttons. **Y** and
  **N** answer that one face; **Space** adds it to the selection. The answer
  bar is pinned to the foot of the screen there too, one row for "To review"
  and one for "Found elsewhere". With the viewer closed, **Y**, **N** and
  **Esc** act on one grid only — the one you selected in last, which the bar
  marks — so a selection waiting in the other grid is never answered by a
  second press; its row keeps its own buttons. The keys are off while the
  name field is open, because a name is made of letters.
- **Listings.** Any picture in the gallery opens — a video plays. In
  "Suggested removals" **Y** removes and **N** keeps, and one button shows
  the photograph that would be kept instead. In "Add photos" the picture
  looks and the tick chooses; **Space** chooses from the viewer.
- **Slideshows.** A proposed photograph opens large and can be removed from
  there.

In the table, a tag's name and its **Tagged** count open Browse filtered to
that tag — the way to see what a round of answers just tagged. While a queue
is open they open in a new tab, so the queue and whatever is ticked in it
stay as they are. **Re-learn**
reports in its row: that it has started, what it found when it finishes, or
why it could not start.

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

## Suggestions from a model on this Mac

A model running on the Mac mini itself (the **Looker**, chosen on the **AI
models** page — [local-models.md](local-models.md)) can look at photographs
and at each shot of a clip and propose tags, the way Claude does, at no
cost and with nothing leaving the building.

*Where this stands (2026-11-13):* built and tested, and not yet run on a
Mac or with a real model. Two things set the Looker going: a review of a
folder, which only an administrator can start and only by asking
(**Review with AI**, on a folder in Browse or on a library), and a
library's own box on the Libraries page for looking at what arrives
([local-models.md](local-models.md#reviewing-a-folder-on-request)). On the
Tags page a card, **Suggestions from the local model**, says how many are
waiting, and each one reads "suggested by local model".

**They are suggestions like any other.** Each lands in the review queue on
the Tags page with the origin `local`, beside the learner's and Claude's,
and becomes an example only when you say yes. A no is kept as a rejection
and never offered again, and a tag you have already answered on a picture
is left alone. `review_queue(origin="local")` lists them for Claude
([mcp.md](mcp.md)).

**A stricter bar than Claude's.** A review is thousands of pictures nobody
is watching, so a local model's answer passes its own admission policy: a
word from your vocabulary needs 60% confidence, a brand-new word 85%, three
words at most, four tags per picture. It is `LOCAL_POLICY` in
`framefound/looking/prompt.py`, written to be changed like Claude's. A
setting, `new_tags`, stops it proposing new words at all: an administrator
changes it under **What the Looker does** on the AI models page ("Let it
propose new tags").

**One suggestion per tag per clip.** A clip is looked at shot by shot, so
the same tag may be said of several shots. That is one suggestion, and it
follows the strongest guess: its confidence, the look that made it, and the
moment it was seen at.

**The moment a suggestion was seen at.** A suggestion about a clip keeps
the middle of the shot the model was looking at (`at_ms`, in milliseconds
from the start of the clip). A photograph's has none. It is kept so the
clip can be opened where the model saw the thing, not at its first frame:
a power broom that is on screen for one shot of a long clip is found at
once. The queue's API hands it over with each suggestion, beside the name
of the model that made it and whether the demonstration made it up
(`at_ms`, `model`, `demo` on `GET /api/v1/tags/{id}/pending`). On the Tags
page such a suggestion reads "suggested by local model · at 00:08", with
"(made up)" when the demonstration invented it, and the viewer starts the
clip at that moment.

**What the model said is kept.** Every suggestion points at the look that
made it: which shot, which model build and question, the caption and score,
and what became of each tag the model proposed. Removing a model never
removes its suggestions or what it said.

**Two limits hold suggestions back rather than bury the queue.**

- **A full queue.** Once a tag has 200 local suggestions waiting — as many
  as the Tags page loads for one tag — further ones for that tag are held
  back. Answer some and later looks fill the room.
- **The new-tag allowance.** One review may add 30 words the catalogue did
  not have, and so may one run of looking at a library's new footage.
  After that its new words are held back; words already in the vocabulary
  are not affected. A word a review has added is the catalogue's from then
  on, so a second shot proposing it needs only the lower bar.

A held suggestion is not written and is not offered later by itself. The
look keeps what the model proposed and that it was held, the review counts
it, and its closing sentence says so: "41 suggestions were held back: its
tag already had a full queue on the Tags page, or this review had proposed
its 30 new tags." Only a suggestion that would add a row is ever held: one
that meets, or strengthens, a suggestion already on the same clip adds
nothing to any queue. Holding is not rejecting: nothing is stored against
the picture. A local model's suggestions count toward the 60 a tag may have
waiting, so while its queue is full the learner adds nothing to it either;
answer the queue down and the learner tops it up again from the strongest
matches, held pictures included if they match.

**It does not take a picture out of Claude's pool.** A picture only a local
model has guessed at still counts as untagged for **Review with Claude**:
Claude is the better second opinion, and an archive the Mac has been
through should still be one Claude can review. Once you answer a local
guess it is your opinion like any other, and the picture leaves the pool.

---

## Reference

| | |
|---|---|
| Learning maths | `framefound/ai/tagging.py` |
| Tasks | `framefound/processing/tag_tasks.py` (`suggest_tags_for_asset` runs from the embedding stage; `suggest_all_tags` is the nightly sweep) |
| Claude reviewer | `framefound/ai/tag_review.py`, `framefound/processing/tag_review_tasks.py` |
| Spend ledger and cap | `framefound/ai/spend.py` |
| A local model's suggestions | `framefound/looking/prompt.py` (`LOCAL_POLICY`, `clean`), `framefound/looking/store.py` (`suggest`, the two limits), `framefound/looking/__init__.py` (the numbers: 200, 30) |
| API | `/api/v1/tags`, `/api/v1/tags/assets/{id}`, `/api/v1/tags/review/claude`; for a local model's reviews, `/api/v1/looks` ([local-models.md](local-models.md#for-developers-the-looking-api)) |
| Tables | `tags`, `asset_tags` (`origin`: clip / claude / mcp / local; `look_id`, `at_ms` for local), `looks`, `ai_usage` |
| UI | `components/TagEditor.tsx`, `app/tags/page.tsx`, `components/MediaPreview.tsx` (the viewer), `components/AiEditCard.tsx` |

Related: [location.md](location.md) uses the same embeddings to infer where
media was shot.
