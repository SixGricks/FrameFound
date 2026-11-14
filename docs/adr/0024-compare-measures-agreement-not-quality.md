# ADR-0024: Compare measures agreement with decisions already made, not quality

- Status: Accepted
- Date: 2026-11-13

## Context

Before any local model is allowed to look at the catalogue (phase L3), the
plan asks for "a measured answer to *which model looks*, or a measured *none
is good enough yet*" ([local-models.md](../local-models.md#comparing-models)).
Compare is the tool that answers it: installed models are asked the Looker's
question about the same footage and their answers are set side by side.

Three things make "measured" harder than it sounds.

- **There is no answer key.** The catalogue holds two kinds of human
  judgement about footage: tags a person said yes or no to, and the shots
  that ended up in a finished cut. Both are sparse, both were shaped by
  something else (most tag rulings were prompted by the CLIP learner; a shot
  was used because that day's edit needed it), and neither says how good a
  caption or a score is.
- **The project has been burned by a number before.** A colour-difference
  figure was once read as a quality grade for photo edits, and it was not one
  (it measures colour only). A single "accuracy" for a tagging model would
  invite the same mistake with less excuse.
- **The truth moves.** From L3 on, a local model's own suggestions land in
  the review queue. A tag the owner confirms after a model proposed it would,
  if read later as truth, count as that model "agreeing with the catalogue" —
  a second opinion confirming itself, the thing `ai/tag_review.py` exists to
  prevent.

## Decision

**Compare reports counts of agreement with decisions already made, each with
what it is a count of, and never a combined score, a ranking or a
highlighted winner. The owner records the decision; the page does not make
it.**

- **Two agreements, both counts.** Tags, per piece of footage and by slug: of
  the tags you said yes to, how many the model proposed; of those you said
  no to, how many it proposed anyway; how many it proposed that nobody has
  ruled on, and how many of those are words the catalogue does not have —
  each at three confidence floors (everything, 0.6, 0.85), because where the
  Looker's admission floors should sit is itself one of the things measured.
  Scores, over pairs of one used and one unused shot: higher, lower, the same,
  and how many different scores the model gave at all. Ties are counted as
  ties; "guessing gets about half" is said beside them; fewer than five used
  shots is called a hint. No percentage, no correlation coefficient, no
  precision or recall: "22 of 31" carries its own denominator.
- **Over the same items.** The agreements are counted only over the items
  every finished model answered, and the page says how many that is. A model
  that failed on eight items is not graded on an easier set. How often each
  model kept to the form asked for, needed a second try or failed, and why,
  is reported separately — it is a result too.
- **The sample and its truth are frozen** when the comparison is made: the
  pictures each model is shown (copied under `<data_dir>/compare/<id>/`), the
  sentence of measured facts, the vocabulary, the tag rulings and which shots
  were used, as of then. A model added months later faces exactly what the
  first one faced. Newer decisions are used by making a new comparison on the
  same items ("Compare again on the same sample"), never by refreshing an old
  one.
- **No model is told the answer.** Not the file name, the folder, the tags on
  the item, whether the shot was used, or the catalogue's own CLIP guess at
  the subjects. A test reads what every model was sent.
- **Nothing a model says in a comparison reaches the catalogue.** No tag goes
  to review, no score or caption lands on a shot. Sending tags to review is
  L3's feature, behind L3's admission policy.
- **The decision is the owner's and is a record.** "This model looks", or
  "none is good enough yet", with a note, who and when, stored on the
  comparison. Saving it changes no role; giving the chosen model the Looker
  role is a separate button that calls the AI models page's own role change.
- **The question has a version** (`look-1`, `looking/prompt.py`), shared with
  L3 so that what Compare measured is what L3 runs. A comparison made under
  an older version stays readable and says so, and cannot be added to.

## Alternatives considered

- **A single agreement score per model, and the columns sorted by it.**
  Easy to read and wrong to read: tag rulings are few and lopsided, and
  "used in a cut" depends on the edit as much as the shot. A sorted column
  would be read as "the best model", which nothing here measures.
- **Precision and recall as percentages.** A percentage of four rejections
  reads as more than it is, and hides how small the evidence is.
- **A rank correlation between scores and use.** A quality number by
  another name, and silent about a model that scores everything 70; pair
  counts show those ties as ties.
- **Re-reading the truth whenever a comparison is opened.** Always current,
  and from L3 on it grades a model against its own confirmed suggestions.
- **Letting Compare send a model's tags to the review queue.** Sixty items of
  an unjudged model's tags is the flood the Tags page is protected from, and
  it would poison the next comparison's truth.
- **Comparing against Claude in the same table.** It costs money per item
  and sends pictures out; Claude's suggestions in the catalogue are not
  truth either.

## Consequences

- The owner reads counts and answers, and decides. The page cannot pick a
  model, and is not meant to.
- The figures are only as good as the rulings and the cut history behind
  them. If a sample has few decided tags or few used shots, the page says so,
  and the first thing Compare teaches may be to rule on more tags (and, for
  the score figure, to import finished timelines, which today is a command:
  `python -m framefound.ops.shots import-cut`).
- Each comparison keeps its own copies of the pictures, about 7 MB per sixty
  shots on the Mac's own disk, freed when it is deleted.
- L3's admission floors for the local Looker are to be chosen from the
  three-floor tag figures of the comparison the owner decided on; L6's gate
  ("each goes through Compare") asks `comparisons.last_finished_for`.
- Changing the question (the instructions, the facts sentence, the schema or
  the vocabulary size) is a new prompt version, and comparisons are run again
  — which takes minutes.
- Nothing here has run on the Mac with a real model. What the counts look
  like for real models on real footage (Brookside) is the open result, not a
  property of this design.
