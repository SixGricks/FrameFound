# Local AI models on the Mac mini — plan

Status (2026-11-13): every phase of this plan is built and tested, on the
server and in the web app. One line per phase is under [Phases](#phases).

**Nothing here has run with a real model, and none of it has run on a
Mac.** Every answer in the tests comes from a stand-in. So how well any of
these models tags, scores, grades or cuts this footage, how long it takes,
and how much memory it really uses are all still unknown, and "built"
below never means more than written and tested. That is said here once and
holds for every section.

How to use what is built: the AI models page in
[mac.md](mac.md#local-ai-models); [Comparing models](#comparing-models),
[Rough cuts from the local Editor](#rough-cuts-from-the-local-editor) and
[Reviewing a folder on request](#reviewing-a-folder-on-request) below;
[video-looks.md](video-looks.md) for colour. This is the design the
roadmap's "Local AI models" phases refer to. It depended on the native Mac
install being up and the catalogue migrated: that was done on 2026-11-13
([mac.md](mac.md)), on the release before this one, so the Mac gets these
phases with its first `framefound update`, which on that Mac is the
one-time steps in [mac.md](mac.md#updating).

Two things that came up with it are planned here too, because they share
its pieces: a **chat panel with Claude** in the web UI, and keeping **bulk
storage on the NAS**.

## What it is for

Open-weight models running on the Mac mini, doing three jobs on **video and
photographs** at no per-use cost:

| Job | What the model does | Where the result goes |
|---|---|---|
| **Tagging** | Looks at each shot or photograph and proposes tags, in the catalogue's own words where one fits | The existing tag review queue, as suggestions — never as facts |
| **Grading: scoring** | Rates each shot or photograph for use (focus, exposure, framing, "is something in the way") with a one-line reason and a caption | On the item, so the best takes sort first in search, in `find_shots` and when culling a shoot |
| **Grading: colour** | Suggests a look as slider values | The existing colour engine renders it; for video, exported to Premiere as a LUT |
| **Basic assembly** | Drafts a rough cut from a brief and the shot list | A Premiere sequence, through the same path Claude's cuts take |

## Decisions (2026-11-12)

1. **Grading means both** scoring and suggesting a colour grade.
2. **Local drafts, Claude optional.** The local model builds the first rough
   cut; Claude reviews or improves it on request, under the monthly budget
   that already exists (`ai/spend.py`).
3. **No Chinese models.** That is the point of "American-based": the
   approved makers start as US organisations, and a model built from a
   Chinese lab's model is refused however it is packaged. Where the
   *software that runs* a model comes from is not restricted (so mlxcel,
   from a South Korean company, is acceptable on that count).
4. **Models are added from the UI from approved publishers** — any Hugging
   Face model whose publisher is on a list the operator controls.
5. **No PR and no console** to add a model, switch one, or update the
   software that runs them.
6. **Gated downloads are a last resort.** A model that needs a Hugging Face
   account and a licence click-through (Cosmos Reason 2) is supported but
   not planned around.
7. **Overnight is 21:30–05:30.** Models too large to share memory with
   Adobe run only then.
8. **A folder can be reviewed on request** from the UI, for older footage
   that was never looked at.
9. **Photographs as well as video.**
10. **Bulk storage goes on the NAS** wherever it can.
11. **A chat panel with Claude** in the web UI, **shown pictures by
    default**.
12. **The bulk store is a `Framefound` folder on the NAS's Photos drive**
    (`/Volumes/Photos/Framefound` on the mini, if the drive mounts under
    that name).
13. **Memory budgets start at 8 GB by day and 20 GB overnight.**
14. **The mini's own disk is not the constraint** — assume it has room.
    Bulk still goes to the NAS; memory, not disk, limits which models can
    hold a role.

## The five candidates

Checked 2026-11-12. "Ready on the mini" means a build exists today for a
runtime that runs on Apple silicon without executing code from the model's
repository. Sizes marked ≈ are third-party or estimated figures.

| Model | Maker / licence | Takes | 4-bit size | Ready on the mini? |
|---|---|---|---|---|
| **Gemma 4 E4B** | Google / Apache 2.0 on Google's card¹ | text, image, audio (video is claimed by some community cards, not by Google's) | ≈ 5 GB file, ≈ 6 GB in memory | **Yes** — `mlx-community/gemma-4-e4b-it-4bit` on MLX-VLM |
| **Gemma 4 12B** ("unified") | Google / Apache 2.0¹ | text, image, audio, video in one decoder; 256K context | ≈ 6.7 GB | **Text: yes. Pictures: patchy.** Some MLX builds are text-only; picture and audio support is still landing in MLX-VLM, and one build needs MLX-LM from source |
| **Molmo2-O-7B** | Ai2 / Apache 2.0 (per a model-zoo listing) | image, video; pointing and tracking | — | **Not yet.** No MLX or GGUF build found; the published route runs code from the model's repository (`trust_remote_code`), which this plan does not allow |
| **Nemotron 3 Nano Omni 30B-A3B** | NVIDIA / NVIDIA Open Model Agreement | text, image, video, audio | ≈ 17–18 GB (estimate from 31B parameters) | **Not yet, and overnight only.** No MLX build found; third-party GGUF builds exist |
| **Cosmos Reason 2 8B** | NVIDIA / NVIDIA Open Model License (**gated**) | text + video; built for robotics and physical reasoning | ≈ 18 GB unquantised | **Not on MLX, and gated.** Only the 2B has an MLX build; the 8B has a community GGUF for llama.cpp |

¹ Some community conversions carry the older Gemma Terms of Use on their
own cards. The manager shows the licence of the exact build being added.

What this means:

- **Start with Gemma 4 E4B for looking** (tags, scores, captions, colour
  numbers). It is the only candidate that runs today, and at about 6 GB it
  can work while Premiere is open.
- **Start with Gemma 4 12B for assembly.** Drafting a cut is reasoning over
  a text list of shots, which the text-only MLX builds already do.
- **The other three need a second runtime** (llama.cpp, for GGUF builds) or
  an MLX port that does not exist yet. They are comparison candidates once
  that runtime is in, not day-one choices.
- **Cosmos Reason 2 goes to the back of the queue.** It is gated, and it
  was built to judge physical situations for robots, not footage for an
  edit. It is tried only if the others leave a gap.
- **mlxcel** is a Rust server for MLX models with an OpenAI-compatible API,
  from Lablup (South Korea). Acceptable under decision 3. Its licence is
  described as open source in one place and proprietary in another, and
  whether it runs the unified 12B is unconfirmed; both are checked before
  it is offered as a runtime. The plan starts on Apple's MLX via MLX-VLM.

### Three corrections found while designing the build (2026-11-12)

The table above is what was believed when the plan was agreed. Reading each
model's own files for the design turned up three facts that change it. The
build follows the corrected facts; none of the decisions above was changed
to make a model fit.

- **Cosmos Reason 2 (8B) is refused, token or no token.** Its card declares
  that it was built from a model published by a lab on the blocked list, so
  decision 3 rules it out however it is packaged. It is no longer a
  candidate, and the manager says why if it is searched for.
- **Gemma 4 12B does not fit the daytime budget.** The build chosen for the
  Editor role is 8.35 GB of weights, about 10.1 GB in memory, against 8 GB
  by day. Under decision 13 it therefore runs **overnight only**: a rough
  cut asked for at 14:00 starts at 21:30, and the page says so before the
  button is pressed. The two ways out are both on the AI models page:
  raise the daytime budget to about 11 GB, or give the Editor role to a
  smaller model.
- **No Nemotron 3 Nano Omni build fits the 20 GB overnight budget today.**
  The 4-bit MLX build (which now exists, from the default converter) is
  estimated at 22.0 GB in memory and the usual GGUF build at 29.2 GB. It
  can be added and measured, but not given a role, unless the overnight
  budget is raised. The plan's "≈ 17–18 GB" was a guess from the parameter
  count; the files are larger. Even the smallest GGUF builds (2-bit, with
  the part that reads pictures) come to about 22.5 GB, and a build under 4
  bits answers noticeably worse. So the MLX 4-bit build, which needs only
  L1, is the better first try; and whether either program runs this
  model's architecture at all is unknown — llama.cpp's own list of the
  model families it reads pictures for does not include it. Every GGUF
  build of it found comes from a third party, and no GGUF publisher is an
  approved converter by default: one is added by hand, by the owner, once
  he knows who they are.

None of these has been run on the mini. The table says what exists, not how
well any of them tags a drone pass over a golf course — that is what the
comparison (phase L2) is for.

## How it works

### Frames, not video files

No runtime here takes a video file reliably across models. FrameFound
already keeps three stills for every shot window (start, middle, end) and
the measured facts beside them (move, speed, smoothness, light, sun). The
model is shown the stills and told the facts. That is what makes the choice
of model open: any model that reads pictures can do the job, and one that
reads video natively is simply given more frames.

A photograph is the simple case: the model is shown its preview.

### Two roles, one model each

- **Looker** — a model that reads pictures. One call per shot or photograph
  returns tags, a caption, a score with its reason, and colour slider
  values, as structured data that is validated before anything is stored.
- **Editor** — a model that reasons over text. It gets the brief and the
  compact shot lines `find_shots` already produces, and returns a cut.

The operator assigns an installed model to each role in the UI. Changing a
role's model does not redo old work unless asked to.

### Runtimes are managed, like models

The five candidates need at least three different programs to run them, and
a model released next year may need a version of one that does not exist
today. So the program that runs a model is treated as something FrameFound
installs and updates, not as part of FrameFound:

- FrameFound talks to every model through one interface: an
  OpenAI-compatible chat API on loopback.
- Each **runtime** (MLX-VLM first; llama.cpp second) lives in its own
  folder under `~/FrameFound/runtimes/`, with its own Python environment
  where it needs one. Updating a runtime cannot break FrameFound itself,
  and the previous version is kept for one-click rollback.
- A `model-server` service starts the right runtime with the right model
  when there is work, and stops it when idle.

**What updates from the UI:** models, runtime versions, the approved
publisher lists, role assignments. **What still needs a FrameFound
release:** support for a new *kind* of runtime, or a new job for the models
to do. A new model on an existing runtime never does.

### The model manager (a new "AI models" page)

- **Installed:** name, maker, licence, size on disk, memory needed, what it
  takes (pictures, video, audio), which runtime runs it, which role it has.
- **Add:** paste a Hugging Face model name. Before anything downloads, a
  preflight shows the publisher and whether it is approved, the model it
  was built from and who made *that*, the licence, whether it is gated, the
  file types, the size, whether it fits the memory budget, and which
  runtime will run it.
- **Test:** run it on a sample shot and see the answer, the time taken and
  the memory used.
- **Compare:** run installed models on the same fixed sample of shots and
  photographs, side by side ([Comparing models](#comparing-models)).
- **Remove:** frees the disk space.
- **Runtimes:** installed version, latest version, update, roll back.

### Approved publishers: makers, converters, and a blocked list

Most Mac-ready builds are not published by the company that trained the
model; a community group converts them. So there are three lists, all
edited in the UI:

- **Makers** — who trained it. Defaults: `google`, `allenai`, `nvidia`,
  `apple`. The operator may add others.
- **Converters** — who packaged it for the Mac. Default: `mlx-community`.
- **Blocked** — the main Chinese labs' publishers, pre-filled. It wins over
  the other two: a model from a blocked publisher, or built from one, is
  refused even if a maker or converter entry would otherwise let it in.

A model can be added if its publisher is a maker, or if its publisher is a
converter **and** the model it declares it was built from belongs to a
maker. A conversion that does not say what it was built from is refused.

### Safety rules

- **Weights only.** safetensors and GGUF files; pickle files are refused.
- **No code from a model's repository is ever run.** This is why Molmo2-O
  waits for a native build.
- **Every download is pinned** to an exact revision, recorded with the
  model, so "the model" cannot change underneath a result.
- **Gated models** need a Hugging Face token, stored sealed like the other
  secrets. Nothing in the plan requires one.
- **The model server listens on loopback only.**
- **Everything a model says is a suggestion.** Tags go to the review queue,
  cuts are drafts, colour is a proposed recipe. The same rule the Claude
  reviewer follows (`ai/tag_review.py`): a second opinion that could confirm
  itself would not be one.
- **Provenance.** Every tag, score, caption and cut records the model, its
  revision and the prompt version that produced it.

### Sharing the Mac with Adobe

- A **daytime memory budget** (default 8 GB) and an **overnight budget**
  (default 20 GB, **21:30–05:30**). A model larger than the daytime budget
  only runs overnight. Gemma 4 E4B fits the day; Nemotron Omni is overnight
  only. Both budgets and the window are settings on the page.
- One model loaded at a time, unloaded after a few idle minutes.
- A new lowest-priority worker lane (`local-ai`, one job at a time), so
  this work never delays thumbnails, proxies or transcription.
- A pause switch on the page.

## Comparing models

*Built 2026-11-13 (phase L2).* The decision behind it is
[ADR-0024](adr/0024-compare-measures-agreement-not-quality.md).

**What it is for.** Before any model is trusted to look at the catalogue,
Compare asks installed models the Looker's question about the same footage
and sets their answers side by side, beside what the catalogue already knows
about that footage. It answers "which model looks", or "none is good enough
yet" — and the owner gives that answer, not the page.

**Where.** Manage › AI models › Compare on your footage › **Compare models**
(`/models/compare`): the
saved comparisons and **New comparison**. A comparison opens at
`/models/compare/<id>` with three parts: **Figures**, **Answers** and **What
did you decide?** Anyone signed in can read them; only an administrator can
start, change or delete one.

**Making one.**

1. **What to compare on.** A folder (and everything beneath it), a listing,
   or the shots used in finished cuts from cut history (with the other clips
   in the same folders, so there are unused shots to set against them).
   Shots, photographs or both. Ten to two hundred items; sixty to start.
   A shot is shown as three stills (start, middle, end), or one if a model
   cannot take three.
2. **Which models.** One to six installed models that read pictures. A model
   that does not, or whose download did not finish, is shown greyed with the
   reason. One model on its own is allowed: "not good enough yet" can be
   measured on one.
3. The preview says what will be checked against what, and says plainly when
   there is nothing to check against ("Nobody has decided any tags on this
   footage…"). **Start** freezes the sample and queues one turn per model on
   the local-AI lane, one model at a time. A model that only fits the
   overnight budget waits for 21:30 and says so; the others run now.

**The sample is fixed.** When a comparison is made, each item's pictures are
copied (under `<data folder>/compare/<id>/`, about 7 MB per sixty shots), and
the measured facts, the catalogue's words and the decisions already made are
written down. A model added to it next month sees exactly what the first one
saw, even after clips are re-analysed or tags change. To use newer
decisions, **Compare again on the same sample** makes a new comparison on
the same items with everything read as of then.

**What a model is told** is the Looker's question (`looking/prompt.py`,
version `look-1`): the pictures, the measured facts (the camera's move, how
steady, the light) and the 150 most used tags. Never the file name, the
folder, the tags on the item, whether the shot was used, or the catalogue's
own guess at what is in it — each of those is an answer.

**The figures, and what they mean.** Two agreements, each a count with what
it is a count of, for every model that has finished, counted over the items
every finished model answered (the page says how many):

- **Tags**, per clip or photograph: of the tags you said yes to, how many it
  proposed; of the tags you said no to, how many it proposed anyway; and how
  many it proposed that nobody has ruled on (and how many of those are words
  the catalogue does not have yet). Each at three levels of the model's own
  confidence: everything, 0.6 and above, 0.85 and above. A clip is judged on
  the shots of it that are in the sample.
- **Scores**, over pairs of one used and one unused shot: how often the model
  scored the used one higher, lower, or the same — guessing gets about half —
  and how many different scores it gave at all. With fewer than five used
  shots, read it as a hint.

**What they do not mean.** They say how far a model agrees with decisions
you already made. They do not say which model is good: tag rulings are few
and were mostly prompted by the catalogue's own learner, and a shot was used
because that day's edit needed it. No column is ordered, ranked or marked by
a figure. Beside them: how often each model kept to the form asked for,
needed a second try or failed (and why), its pace per shot and per
photograph, its load time, and the most memory it reported, with how that
was measured.

**The answers.** Every item, its frozen pictures (a click opens the footage
at that shot), what was decided about it, whether it was used, and each
model's tags, caption, score and reason, with filters and orders. What a
model wrote is shown as text and nothing else.

**Nothing reaches the catalogue.** No tag goes to the review queue, no score
or caption lands on a shot or a photograph. That is Looking's job (L3,
[Reviewing a folder on request](#reviewing-a-folder-on-request)), behind
its own admission rules.

**Deciding.** Under **What did you decide?**: a model, none of them yet, or
not decided, with a note in your own words. Saving changes nothing else.
**Make it the Looker** beside a chosen model gives it the role through the
AI models page's own role change.

**Changing a saved one.** **Add a model** runs a newcomer on the very same
sample (how a model added later "goes through Compare"). **Cancel**, **Carry
on** (after a cancel, a restart or a stop), **Try the failed ones again**,
**Take it out of this comparison**, **Rename** and **Delete** (which frees
its pictures). A comparison made with an earlier version of the question can
be read but not added to.

**On the container server** the page is one card saying models run only on
a Mac; saved comparisons can still be read, and every change is refused.

**In demo mode** (`FRAMEFOUND_LOCAL_AI_DEMO=1`, CONTRIBUTING.md) a turn
runs against the demo's made-up model server, and its answers are made up
in the shape of real ones (`looking/demo.py`): a stock caption, a reason
saying no model looked, a score from a hash, tags drawn only from the words
the model was shown — the same for the same model and item every time. They
agree with the catalogue about as often as chance, which is honest. The dev
seed, run with `--compare`, adds a finished comparison of two demo models
whose answers were chosen so its figures are worth reading.

### For developers

The server is `framefound/local_models/{sampling,comparisons,compare_job,
agreement}.py`; the tables are in [data-model.md](data-model.md) (migration
0033). Every route is under `/api/v1/local-models/compare`; reads are open to
anyone signed in (the preview and the two source searches included), writes
are administrator-only, leave an audit row (`local_models.compare_*`) and are
refused with the AI models page's sentence where models cannot run. Errors
are `{"error": {"code", "message"}}`, and `message` is the sentence the page
shows; no body ever gets a 422.

| Route | What it does |
|---|---|
| `GET ""` | The list (newest first, at most 100), the models the builder offers with why one cannot be compared, the limits, the mode |
| `GET /sources/folders?q=&library_id=` | Folders to sample, each counted with everything beneath it |
| `GET /sources/cuts` | Finished cuts from cut history (never a draft) |
| `POST /preview` | What a sample would hold and what it can be checked against. Writes nothing |
| `POST ""` | Start: draw, freeze, one queued turn per model. 503 "The processing queue is unavailable. Nothing was started." leaves nothing behind |
| `GET /{id}` | One comparison with its columns and figures; small, polled |
| `GET /{id}/items` | Every item with each model's answer |
| `GET /{id}/items/{item}/pictures/{n}` | A frozen picture |
| `POST /{id}/models` | Add models to the same sample |
| `POST /{id}/models/{entry}/run` | Carry on; `{"retry_failed": true}` tries the failed ones again |
| `POST /{id}/models/{entry}/cancel` | Cancel a turn |
| `DELETE /{id}/models/{entry}` | Take a column out (its answers go with it) |
| `PUT /{id}` | Rename, or record the decision |
| `DELETE /{id}` | Delete it and its pictures |

A model's turn is one `compare_model` job (priority 5, so a Test goes first
and a turn steps aside between items). Every answer is stored the moment it
arrives, so Cancel, a restart, a step aside and Carry on all resume at "the
items with no answer". `comparisons.last_finished_for(db, model_id)` is when
a model last finished a comparison with an answer — the question L6 asks
before a model is given a role — and `/models/compare?model=<id>` opens the
builder with that model ticked.

## Rough cuts from the local Editor

*Built 2026-11-13 (phase L5). How long a draft takes and how good one is
are both still unknown: no real model has drafted one.*

**What it is for.** You say what the video should be in a sentence, point at
a folder of footage, and the model that holds the **Editor** role on the Mac
drafts a rough cut: which shots, in what order, from where to where in each,
with one line of why for every shot. It costs nothing and nothing leaves the
building. The draft opens in Premiere exactly as one of Claude's cuts does,
because the same code writes both. It is a first pass to react to, not a
decision: Claude is one button away for a second opinion, and every version
is kept.

**Where.** **Cuts** in the top menu (`/cuts`). The page has the form
(**Draft a cut**), then the drafts that are under way or failed, then cut
history, one line per cut: its title, who made it (**Local draft**,
**Claude** or **Imported timeline**), how many versions it has, its shots,
its length and when. Cut history is not new: Claude's cuts and imported
timelines have been recorded all along. This is the first page that shows
it. Anyone signed in can read it; an account that can change things can
draft, download and delete its own; a read-only account sees everything and
has no buttons.

**Asking for a draft.**

1. **What should the video be?** The brief, in your own words: "Thirty
   seconds of smooth aerials at golden hour, opening on the clubhouse."
2. **Footage.** A folder, with everything beneath it. Only folders that
   hold video are offered, each with how many of its clips have been
   analysed into shots. The Editor chooses from analysed shots, so a folder
   with none is refused before anything starts, with a link to Processing.
3. **Length.** 15, 30, 60 or 90 seconds, or any figure from 5 to 600; 30
   to start with. The form's figure is the one that counts, whatever length
   the brief mentions.
4. Under **Shot length and used footage**: the shortest and longest a shot
   may be (1.5 and 8 seconds to start with), and **Leave out shots an
   earlier video already used**.
5. **Title.** Left empty, the cut is named after the brief.

One line above the **Draft** button says which model will draft and when.
With the plan's Editor (Gemma 4 12B, about 10 GB) and the default budgets it
reads "overnight only": **a draft asked for at two in the afternoon is
accepted and waits until 21:30**, and its card says so. That is the memory
budget doing its job, not a fault; the two ways out (a larger daytime
budget, or a smaller Editor) are both on the AI models page, which the line
links to. A waiting draft can be left and come back to, and cancelled.

**What FrameFound does, and what the model does.** FrameFound decides which
shots the Editor is offered: at most 80 analysed shots from the footage, at
most three from any one clip, preferring steadier shots and the ones your
brief's own words point at (a move, a light, a subject). Those go to the
Editor as numbered lines, the same compact lines Claude gets from
`find_shots`, with your brief word for word. The Editor answers once, with
shot numbers and seconds. FrameFound then checks every number against the
footage: a shot that is not on the list is dropped, in and out points are
held inside their shot, a cut longer or shorter than you allowed is brought
into range, a repeat is dropped, and a draft that runs well over the length
loses its last cuts. A draft that comes out short is left short: FrameFound
never adds a shot the model did not choose. **Each of those changes is a
sentence on the draft** ("Changed when it was checked"), so you can see what
the model got wrong.

**When a draft fails**, its card says why in a sentence: the Editor answered
but not in the form asked for (what it wrote instead is kept, under **What
the Editor wrote**), it ran out of room, it chose no shot that could be
used, or the model would not load. From there: **Try again**, **Ask Claude
instead** (opens the chat panel with the brief and the footage written out;
nothing is sent until you press Send) or **Clear**.

**A cut's page** (`/cuts/<id>`; a line of the list opens it):

- **The shots**, in order: each one's picture, clip, in and out, what the
  shot was (move, light) and why it is there. A picture opens the footage
  at that shot's in point.
- On a local draft, **A draft, not a decision.** with the model and build
  that drafted it and your brief, what was changed when it was checked, and
  the shots it was chosen from with the chosen ones marked.
- **Download for Premiere.** Pick this computer's path profile and you get
  the timeline (an FCP7 XML, which DaVinci Resolve also opens) with every
  shot at its in and out point and its reason as a marker. Any cut in
  history can be downloaded again, for any computer.
- **Draft again** (on Claude's cut or an imported timeline: **Draft a new
  version**) asks the Editor for another pass. It becomes the next version
  of the same cut; the one you were looking at stays.
- **Versions** and **Compare.** A cut with more than one version has a
  button for each, and **Compare** puts two side by side with a mark on
  every shot: kept, trimmed, moved, or only in one of them.
- **Ask Claude to review** opens the chat panel on a new conversation with
  this version attached: the brief, the cut shot by shot with the reasons
  given, and the shots it was chosen from. Nothing is sent until you press
  Send, and the panel shows what is attached and that it spends from the
  monthly Claude budget, like any other question. If Claude builds a better
  cut, it is kept as the next version beside the draft
  ([chat.md](chat.md#a-draft-from-the-local-editor)).
- **Delete this version** is for whoever made it, or an administrator, and
  is written to the audit log. The other versions keep their numbers.

**A local draft does not count as "used" until you download it.** Every cut
Claude builds marks its shots as used, so the next video can avoid
repeating them. If drafts did the same, three tries at one folder would
mark half of it used, and the Editor, told to prefer unused shots, would
avoid its own last attempt. So a local draft's shots count only from the
first time its Premiere file is downloaded (the page then shows
**downloaded**). Claude's cuts and imported timelines count at once, as
they always have.

**From the chat panel.** Claude can ask the Editor for a draft itself, when
you ask for "a local draft" or "a free first pass":
[chat.md](chat.md#a-draft-from-the-local-editor).

**On the container server**, where models cannot run, the form's place says
so in one sentence and everything else works: the list, a cut's page, the
download, Compare and Ask Claude to review need no local model.

**In demo mode** (`FRAMEFOUND_LOCAL_AI_DEMO=1`, CONTRIBUTING.md) Draft runs
against the made-up Editor: it takes shots evenly through the list, every
reason reads "Demonstration: taken evenly from the list, not judged.", and
the draft and the cut are marked **demo**. The dev seed adds a cut in two
versions, an imported timeline and a failed draft, so every state of the
page can be looked at.

**Other ways in.** Besides the Cuts page and the chat panel, a draft can
be started from **Draft a cut…** in Search (for the ticked clips) and from
**Draft a cut from this folder** in Browse and on a clip's page. Each opens
the Cuts page's form with that footage already chosen and the keyboard in
the brief. A folder holding more than 20,000 shots is not drafted from:
the Editor would only see part of it, so it is left out of the form's
folders and refused with a sentence saying so.

### For developers

The Editor's prompt, the choice of shots and the check are pure functions in
`framefound/ai/cut_editor.py` (prompt version `editor-1`); the request and
the job are `framefound/nle/drafts.py`; the cut is written by
`framefound/nle/roughcut.py` `build_rough_cut`, which the connector's tool
of that name wraps. The tables are in [data-model.md](data-model.md)
(`cuts`, `cut_pieces`, `cut_drafts`; migration 0036). A draft is one
`draft_cut` job on the local-AI lane at priority 0 (someone is watching),
with one question to the model; "waiting until 21:30", a pause, Cancel and a
worker that died are the job's own states, read through the draft. Every
route is under `/api/v1/cuts`; reads are for anyone signed in, a read-only
account is refused every POST and DELETE, and errors are
`{"error": {"code", "message", "detail"}}` with `message` the sentence the
page shows.

| Route | What it does |
|---|---|
| `GET ""` | Everything the list draws: whether the Editor can draft here and when it would run, the drafts that are not a cut, one line per family of versions |
| `GET /folders?q=` | The form's folder picker: folders holding video, with how many clips are analysed |
| `POST /drafts` | Ask for a draft (202). 400 nothing to draft from, 409 no Editor or models cannot run here, 503 "The processing queue is unavailable. Nothing was started." leaves nothing behind |
| `GET /drafts/{id}` | One draft as the page reads it; polled |
| `POST /drafts/{id}/cancel` | Cancel: at once while waiting, after the answer while the Editor is writing |
| `DELETE /drafts/{id}` | Clear a failed or cancelled draft from the list |
| `GET /{id}` | A cut's whole family (any version's id finds it): pieces, reasons, which build drafted it, the draft it came from and the shots that was chosen from |
| `GET /{id}/pieces/{position}/still` | A piece's picture, from what is already on disk; 404 when there is none |
| `POST /{id}/send` | The Premiere file, rebuilt from history for a path profile; the first download marks a local draft sent |
| `GET /{id}/review-request` | The message that asks Claude to review the cut (at most 18,000 characters, with the cut's full id) |
| `DELETE /{id}` | Delete one version (its maker or an administrator; audit `cuts.deleted`) |

The model's words are untrusted three times over: shown only as text on the
page, escaped as markers in the XML, and handed to Claude as list items
inside a message the operator sends, never as instructions. Nothing in an
answer is used as a path, an id or a command: shot numbers are looked up,
seconds are clamped, reasons are cut to 200 characters. Tests:
`tests/test_cut_editor.py` (the prompt and the check), `test_cut_drafts.py`
(the job), `test_cuts_api.py`, `test_draft_chat.py`, and
`test_cuts_demo.py`, which drafts in demo mode with nothing handed in.

## Reviewing a folder on request

*Built 2026-11-13 (phase L3), server and pages.*

**Where.**

- **Browse › Folders** shows a library folder by folder, each with how many
  of its items have been looked at. **Review with AI**, beside the
  breadcrumb, is for the folder you are in and everything beneath it, or
  for the whole library at its top.
- **Libraries**: the same button on each library's card, for the whole
  library, and the box **Look at new footage with AI**
  ([below](#new-footage-a-switch-per-library)).
- The dialog shows what a review would do before anything starts, then
  **Start now** and **Tonight**. Once started it can be closed: the review
  carries on, and reads the same when the dialog is opened again.
  What is still to look at and what was already looked at are said the
  same way: photographs, and clips with their shots in brackets ("6
  photographs and 2 clips (6 shots)"). A review looks at each shot by
  itself, so its progress counts photographs and shots one by one.
- **Processing › Looking (local AI)** lists the reviews under way and the
  newest that ended, with **Cancel** for an administrator.
- **AI models › What the Looker does** holds the two settings: whether it
  may propose new tags, and one or three pictures a shot.
- **Tags** marks its suggestions "local model"
  ([tagging.md](tagging.md#suggestions-from-a-model-on-this-mac)).
- **A clip's page** ends with **Shots**: one row a shot, with its times,
  how the camera moved, how steady it is and the light, and each row plays
  its shot in the player above. Where a local model can look, or one
  already has, two more columns show its score and what it said about that
  shot ("Not looked at yet." where it has not). A row's picture is the
  nearest of the clip's sampled frames inside the shot, or "no picture"
  when none falls inside it: the server has no address yet for a shot's own
  middle still.
- **An item's page**, photograph or clip, has a card **What the local
  model saw**: the score, the caption and the reason, which model and
  build said it, and "A local model's opinion, not a fact." For a clip it
  is the best shot's, and its times play that shot; a clip with shots
  still to look at says so. Where a model could look and none has, it
  reads "Not looked at yet." with **Review its folder**, which opens the
  folder in Browse; where none can and none has, there is no card.
- **The score where items are listed**: on a tile in Browse, on a hit in
  Search, and on a listing's suggested removals (on the tile and in the
  viewer). Search also shows the caption of an item it found by its
  caption, and **By kind of match** has **AI captions**.

**What it is for.** The model that holds the **Looker** role looks at a
photograph, or at each shot of a clip, and writes three things: tag
suggestions for the review queue, a caption, and a 0–100 use score with a
one-line reason. Each is an opinion, kept with the build and the question
that produced it. None of it is applied by itself: a tag stays a suggestion
until you say yes
([tagging.md](tagging.md#suggestions-from-a-model-on-this-mac)).

**Nothing is swept automatically.** The archive is large and most of it
will never be cut again, so looking happens in two ways only:

- **A folder review, only on request and only by an administrator.** One
  folder and everything beneath it, or a whole library. Nothing else starts
  one: no schedule, no page being opened, no other account. Anyone signed
  in can read the counts, the estimate and the progress; starting,
  cancelling and the two settings are an administrator's, and each is
  written to the audit log.
- **New footage, library by library**, where that library's switch is on
  ([below](#new-footage-a-switch-per-library)). It is off unless an
  administrator turns it on.

**Before it starts** the server counts, from the catalogue alone, what a
review of that folder would do (`GET /looks/preview`): the photographs,
clips and shots not yet looked at; what was looked at already, and how much
of that by another model or question; what has to be made first (clips
with no playable copy, clips whose shots have not been found, files still
being processed); what is left out (sound files, files that are gone); and
about how long it should take.

The estimate is about time and nothing else. It is this model's own pace:
the median of its last 50 timed answers, photographs and shots apart
("About 25 min at this model's measured speed (9 s a photograph, 14 s a
shot, from its last 50 answers)."), or its one Test when it has not looked
at anything yet ("Roughly 25 min, from the one test this model has run."),
or "No estimate yet". It always adds that loading the model and making
playable copies are extra, and it never says how good the answers will be.

**Start now, or Tonight.**

- **Start now** begins at once when the model fits the memory budget in
  force at that hour. A model that fits only overnight is refused by day,
  with the reason: "This model only runs overnight (needs about 10 GB; the
  daytime budget is 8 GB). Choose Tonight."
- **Tonight** waits for the overnight window and runs only inside it,
  however many nights that takes, even for a model that would fit by day.
  When the window closes the review reads "Stopped for the day at 05:30 —
  212 of 505 done. Carries on at 21:30." and it does, by itself. Asked for
  while the window is open, it starts at once.
- **The overnight window** is the one set under **Sharing this Mac** on the
  AI models page (21:30–05:30 unless changed); looking keeps no copy of it.
  If its start and end are the same there is no window, and Tonight is
  refused with a sentence that says so.

A review uses the Looker unless another installed model that could hold the
role is named. Asking again later does only what is new; `redo` also looks
again at what a different build or question looked at.

**Clips are made ready first.** A clip is looked at shot by shot, which
needs its playable copy and its shot analysis. Unless told not to, a review
asks the ordinary pipeline for both, each clip once ("Getting 12 clips
ready (9 to go)."), looks at everything that is ready meanwhile, and looks
at each clip as it becomes ready. Camera raw, a clip whose analysis found
no shots, and a clip that never became ready (nothing made ready for six
hours with the queues empty, or the processing queue down) are judged from
one still each, and the closing sentence counts them. A clip whose shots
are on record but whose shot analysis file is missing or failed is made
ready like any other (analysed again); should it never become ready, it
is still looked at shot by shot from the shots on record, never judged
from one still. While the drive for playable copies is away the review
waits, and says that is what it is waiting for.

**One question an item.** The model is shown pictures already on this
machine (a photograph's preview; a shot's start, middle and end stills, or
its middle alone, which is a setting) and told the measured facts and the
150 most used tags: the question in [Comparing models](#comparing-models),
so what Compare measured is what runs. Never the file name or the folder.
The look, its suggestions and the score on the item are saved together, so
an item is recorded whole or not at all.

**It shares the Mac.** A review is one job on the local-AI lane. Between two
items it steps aside for more urgent work (a Test, a rough cut somebody is
waiting for), and every ten minutes it hands the lane back regardless, so a
job whose time has come is not held up behind a large folder. While local
AI is paused it reads "Paused. It carries on when local AI is resumed."
After a pause, the end of the night or a restart of the worker it is the
same review that carries on, from what still needs looking at.

**Cancel keeps what was looked at.** Cancel stops a review between two
items: "Cancelled. 212 of 505 were looked at; their results are kept."
Playable copies already asked for are still made (they are ordinary
copies, useful anyway), and the sentence says that too. Reviewing the
folder again later picks up where this one stopped.

**When something goes wrong** the review says so in a sentence, and what
was looked at is always kept:

- An item with no picture to show, or one the model fails on, is counted
  as "could not be looked at" and the review goes on. The next review of
  the folder tries it again; this one does not.
- A review never asks the same model about the same item twice. Should an
  answer it was given not count as the item's look, the item is counted as
  "could not be looked at" ("It was asked about once and its answer did
  not count as its look, so it was not asked again.") rather than asked
  again, and the review ends as usual; the answer itself, and any tags it
  suggested, stay. The count it shows never passes its total.
  (A review that looks again with whichever model is the Looker, when the
  role moves to another model partway through, asks the new one about
  everything, as looking again means.)
- Five failures by the model in a row stop the review: "Stopped: the model
  failed on 5 items in a row — …".
- **A model that will not load, or whose files are damaged, stops the
  review at once**: "Stopped: the model could not be used — …". Nothing is
  held against the item it was asked about. (The design counted these
  toward the five. As built one is enough: a model that will not load
  fails every item alike, and five tries at loading it would only waste
  the time.)
- The Looker taken away, removed or no longer approved while a review runs
  ends the review as failed, with the reason.
- A review of a folder that is already under review is refused ("A review
  of this folder is already running."), and so is one with nothing to do
  ("Everything here has been looked at.", or "There is nothing here a
  model can look at." with what is there instead).
- A model whose program is not installed (a llama.cpp build before
  llama.cpp is) cannot be used for a review: the dialog shows it as not
  available, with the sentence ("… is not installed. Install it under
  Software that runs models on the AI models page.") and a link there,
  keeps Start off, and a start asked for anyway is refused with the same
  sentence, as Compare refuses such a model.

**A finished review** keeps one closing sentence: how many were looked at,
how many could not be, how many clips were judged from one still, and how
many suggestions were held back
([tagging.md](tagging.md#suggestions-from-a-model-on-this-mac)).

### New footage: a switch per library

Each library has its own switch, off by default: the box **Look at new
footage with AI** on its card on the Libraries page (`look_at_ingest`).
Only an administrator can change it, and the change is written to the
audit log.

- **It never reaches back.** Turning it on notes the moment, and only what
  is first catalogued after that is looked at. Turning it off and on again
  does not sweep up what arrived in between. Older footage is a folder
  review's job.
- While it is on, each new photograph, and each new clip once its shots are
  described, is looked at by the Looker as it finishes processing. The
  scanner starts anything a lost step missed within five minutes.
- It waits behind a review somebody asked for, and a person's Test goes
  before both.
- It looks only at what processing has made ready. A new clip with no
  playable copy (a library with proxies switched off) is not looked at
  until a folder review makes one.
- An item that fails is tried twice by a given build and question, then
  left for a folder review.
- Turning it on is refused where models cannot run; turning it off always
  works.

**The six-hour hold.** When looking at a library's new footage fails
because the Looker itself cannot be used — it would not load, its files
are damaged, it failed five items in a row, it may no longer run — that
library's new footage is left alone for six hours and then tried once.
Nothing is recorded against a file for such a failure, so without the hold
every file that arrived, and every pass of the scanner, would start
another job that failed the same way. The same hold follows a job whose
worker stopped three times in a row before it had looked at anything. The
library's line then reads as failed, with one sentence, such as "Looking at
new footage stopped because of a problem with the model, and is tried again
in about 6 hours." (the time counts down). The hold ends early when the
Looker role is given to another model, or when the model answers anything
at all (a Test is enough) or is downloaded again. While no model has the
role, or the Looker may not run, no job is made in the first place.

### Where a look is used

- **The item itself.** A photograph carries its look. A clip carries its
  best-scoring shot's, and counts as looked at only once every shot has
  been. A re-analysis that moves a shot's times stops that shot's look
  counting, and the shot is looked at again.
- **The review queue.** Its tags arrive as suggestions marked as the local
  model's ([tagging.md](tagging.md#suggestions-from-a-model-on-this-mac)).
- **Search.** A score moves a result among the things that matched by at
  most ten per cent either way, so the better take of the same subject
  comes first; it is never a reason to match. A caption is a reason, the
  weakest one, labelled "AI caption".
- **Choosing shots.** `find_shots` prints the score on shots a model has
  looked at and can filter or order by it
  ([mcp.md](mcp.md#a-local-models-score-on-shots)).
- **Culling a listing** ([Photographs as well as video](#photographs-as-well-as-video)).
- **Browse.** Sorting by score (`sort=score`: best first, unscored last),
  and how many items in each folder have been looked at.

With no looks in the catalogue, search, `find_shots` and culling answer
exactly as they did before looks existed.

### In demo mode: what "made up" means

With `FRAMEFOUND_LOCAL_AI_DEMO=1` (CONTRIBUTING.md) a review runs for real
— the same job, the same counting, Cancel, Tonight — against the demo's
stand-in model server, and **no model looks at anything**. Each answer is
replaced by a made-up look (`looking/demo.py`):

- a stock caption from a short list ("A drone moves slowly over open ground
  with trees along the edge.");
- the reason "Made up for the demonstration: no model looked at this
  picture.";
- a score between 35 and 95 taken from a hash of the model and the item;
- one to three tags drawn only from the catalogue's own words, never a new
  word (a tag under the admission bar is dropped, as a real model's is).

The same model and item give the same look every time. It is stored like
any look and marked `demo`, and the mark travels with it: on the item's
look, on each shot, on a tag suggestion, on the review, and as ` made-up`
on a `find_shots` line. A demo look says nothing about the footage it is
attached to. A real install cannot be in demo mode: the flag is ignored
wherever `FRAMEFOUND_RUNTIMES_DIR` or `FRAMEFOUND_DOMAIN` is set. The dev
seed adds no looks; it gives the seeded clips what a review needs to look
at them shot by shot.

**On the container server**, where models cannot run, the reads answer
with one sentence and counts of zero, every change is refused with that
sentence, and a library's switch cannot be turned on. The folder level of
a library (`GET /assets/folders`) works everywhere: it does not depend on
local AI.

**From the chat panel.** Claude can start a review too
(`start_folder_review`), where models can run. It goes through the same
service as the route, so it is an administrator's, is refused in the same
words, and shows up as the same review. The panel asks you first, every
time ([chat.md](chat.md#a-review-by-the-local-looker)).

### For developers: the Looking API

The server is `framefound/looking/`: `scope.py` (what is in a folder and
what still needs looking at, counted from the catalogue without touching a
disk), `pictures.py`, `prompt.py` (the question, version `look-1`, and what
an answer may become), `store.py` (the only writer of `looks`),
`estimate.py`, `reviews.py` (who may start what, every refusal and its
sentence, how a review reads) and `runner.py` (the three jobs:
`look_folder`, `look_prepare`, `look_ingest`). The tables are in
[data-model.md](data-model.md) (`look_requests`, `looks`; migration 0034).
A review is one `look_requests` row and one `look_folder` job at priority 5
(plus a `look_prepare` job when clips must be made ready); looking at new
footage is one `look_ingest` job a library at priority 9, made by the hook
at the end of the embedding and shot-description stages
(`processing/tasks.py`) or by the scanner's five-minute sweep.

Every route is under `/api/v1`. Reads are for anyone signed in. Writes are
administrator-only (403 otherwise), are refused with the AI models page's
sentence where models cannot run (409), and leave an audit row
(`looks.review_started`, `looks.review_cancelled`,
`looks.settings_updated`, `library.look_at_ingest`). Errors are
`{"error": {"code", "message", "detail"}}`, and `message` is the sentence
to show. **A malformed id** (in the body, the path or the query) **gets
FastAPI's 422 "Invalid request"**, as on every other router here; the
design asked for a 400.

| Route | What it does |
|---|---|
| `GET /looks/status` | Whether a review can be offered and by whom: `available`, `mode` (ready, demo, unavailable), `reason`, `can_manage`, the `looker` (or why there is none, or why it may not look), the `models` a review could use (each with `program_missing` and `problem` when its program is not installed here), `now` (the overnight window and the pause switch), the two `settings`, and `pending_suggestions`. Never an error; asks no share and no model server |
| `GET /looks/preview?library_id=&folder=&model_id=&redo=` | What a review of that folder would do: `todo` (photos, clips, shots, stills_only), `already` (items, by_another_model), the same two counts by kind as `already_kinds` and `by_another_kinds` (photos, clips, shots; a clip counted once), `first` (what must be made ready, and a drive that is away), `skipped`, `failed_before`, the `model` and why it cannot run now or tonight (`program_missing` when that is its program), the `estimate` with its `words`, `tonight`, `paused`, and `active`, a review of this folder already running. Writes nothing. 404 no such library or model, 400 a folder that is not one |
| `POST /looks/reviews` | Start one (202): `{library_id, folder: "", when: "now" or "tonight", model_id: null, redo: false, prepare: true}`. 400 a bad folder or `when`; 404 no such library or model; 409 with the reason (no Looker, the model may not look, its program is not installed, it only fits overnight, no overnight window, already running, nothing to do); 503 "The processing queue is unavailable. Nothing was started." leaves nothing behind (if the lane had already taken the job, the review stands and the answer is 202) |
| `GET /looks/reviews` | Every active review, then the newest twenty that ended, and one `ingest` line for each library that looks at new footage (`state` idle, waiting, paused, looking or failed; `retry_at` while held). Also writes back a job that ended under a review still marked active |
| `POST /looks/reviews/{id}/cancel` | Cancel (200, the review). 404 "There is no such review.", 409 "It has already finished." |
| `PUT /looks/settings` | `{new_tags?, stills_per_shot?}`: whether the Looker may propose words not yet in the catalogue, and 3 or 1 pictures a shot (400 "Choose 1 or 3 pictures a shot."). Takes effect at the next item |
| `PATCH /libraries/{id}` | `{look_at_ingest}` turns looking at new footage on or off (409 with the sentence when turned on where models cannot run); the answer carries `look_at_ingest` and `look_since` |
| `GET /assets/folders?library_id=&path=` | One level of a library: the folders directly inside `path` (at most 500, then `truncated`), each with its items, photographs, clips, how many are `looked` at and its newest date; `here` (the files directly in `path`), `inside` (everything beneath) and `crumbs` |
| `GET /assets` | Gains `folder` (with `library_id`; `""` is the library's top), `deep` (also the folders inside it) and `sort=score`; each item carries `look_score`, `look_caption`, `looked_at` |
| `GET /assets/{id}` | Gains `look`: the score, caption and reason, the shot's times (or `whole`), `complete`, when, the build that said it and `demo`; null until something has been looked at |
| `GET /assets/{id}/shots` | A clip's shots in order: times, the middle still's moment (`still_ms`), move, direction, smoothness, energy, light, sky, up to five subjects, and each shot's own `look`. `[]` for a photograph or a clip with no shots |
| `GET /tags/{id}/pending` | Each suggestion gains `at_ms` (the moment of the clip it is about), `model` and `demo`; `origin` may be `local` |

A review as the routes return it: `id`, `library_id`, `library_name`,
`folder`, `when`, `redo`, `prepare`, `model_name`, `state` (tonight,
waiting, paused, looking, done, failed, cancelled), `message`,
`not_before`, the figures `done`, `total`, `failed`, `tags_suggested`,
`new_tags`, `held` and `stills_only` (fields of the review itself, not a
nested object), `preparing` (`state`, `total`, `ready`, `gave_up`),
`seconds_left`, `requested_by`, the three times, `can_cancel` (active, and
the reader is an administrator) and `demo`.

Two more things are as built rather than as designed.
**`pending_suggestions` counts every local suggestion still waiting**,
whatever file it is on. The Tags queue lists only catalogued files (a
paired proxy is not one), so the figure can read higher than the queues
add up to. And a review whose model is gone ends its job as failed too,
rather than the job reading done beside a failed review.

The model's words are untrusted, as everywhere here: held to a strict form,
cut to length (a caption to 300 characters, a reason to 200, a tag to 60),
shown only as text, and never used as a path, an id or a command. Tests:
`tests/test_looking_scope.py`, `_pictures`, `_admission`, `_store`,
`_estimate`, `_reviews`, `_runner` (the jobs, through the lane) and `_api`;
`test_search_looks.py`, `test_curate_scores.py`, `test_mcp_looks.py`; and
`test_seed_looking.py`, which reviews a seeded folder in demo mode with
nothing handed in.

## Photographs as well as video

The same Looker runs on photographs:

- **Tags, captions and scores** for every photograph, from its preview.
- **Culling.** `media/curate.py` already suggests near-duplicates and soft
  frames from sharpness and CLIP. The model's score joins those signals —
  it can tell that the sharp frame has a car in front of the house. *As
  built (L3):* where the photographs of a listing have scores, **Suggest
  removals** uses them in two ways, both inside the guards it already had
  (a room never loses its last photograph, and at most 40% of a listing is
  offered). Among near-duplicates it keeps the frame the model scored
  highest instead of the sharpest, but only when every frame of the group
  has a score and the best leads the next by at least 10; the reason then
  reads "near-duplicate; the local model preferred the kept frame". And a
  frame scored under 40 that is also at least 25 below the shoot's median
  is offered as "the local model scored it 31: …", with the model's own
  reason. A shoot the model disliked as a whole loses nothing for it, and
  with no scores the suggestions are what they always were.
- **Listing auto-edit, locally.** `ai_edit_listing` has modes `ai` (Claude
  judges), `preset` and `describe`. A `local` mode has the Looker do what
  Claude does there — straightening, whether the sky is worth replacing,
  the caption and the file name — at no cost and with nothing leaving the
  building. Tone still comes from the learned look, as it does today,
  because that beat every model's judgment when measured.

## A chat panel with Claude

A panel in the web UI, on every page, for talking to Claude about the
catalogue — the connector's abilities without leaving FrameFound.

- **What it can do:** everything the MCP connector's tools already do
  (find and search media, look at a shot or contact sheet, read a
  transcript, find shots, build selects, build a rough cut for Premiere,
  propose and decide tags), run in-process on the server, plus two new
  ones: start a folder review, and ask the local Editor for a draft.
- **It knows where you are:** the folder, clip or selection on screen is
  passed along, so "cut a thirty-second piece from this folder" works.
- **Changes ask first.** Anything that alters the catalogue (deciding
  tags, starting a review) shows a confirm chip in the panel.
- **Cost:** the Claude API, under the existing monthly budget, with each
  conversation's spend shown and the cap enforced before a call. The tool
  list and instructions are cached so a long conversation stays cheap.
- **What leaves the building:** the conversation, the tool results (names,
  tags, transcript excerpts) and any preview Claude asks to look at — the
  same as the connector today. Pictures are on by default; a switch in the
  panel turns them off.
- **Model:** Claude Opus 5.5 by default, chosen from the list the Security
  page already offers; answers stream as they are written.
- **"Ask Claude to review"** on a local draft (decision 2) opens this panel
  with the draft and its shot list attached.
- It does not depend on the local models or on the Mac mini, so it can be
  built at any point and works on the current server too.

## Bulk storage on the NAS

The Mac's own disk is small and shared with Adobe. The install already
keeps proxies somewhere else ([mac.md](mac.md)); this generalises that into
two classes of storage.

| Stays on the Mac (fast) | Goes to the NAS (bulk) |
|---|---|
| The catalogue database — never on a network share | Video proxies |
| Thumbnails, previews, posters, frames, shot stills (read on every page) | Slideshow renders and listing exports |
| Transcripts and the queue | Basemap extracts |
| Models that currently have a role | The model library: every installed model |
| Scratch space | Backup copies |

- **Where:** a `Framefound` folder on the NAS's Photos drive —
  `/Volumes/Photos/Framefound` on the mini. If photographs on that drive
  are also a library, the store's marker keeps the scanner out of the
  folder, as it does for a proxy store today.
- **One NAS folder**, with a subfolder per kind. Each kind can be moved
  somewhere else on its own later (proxies to an external SSD, say) with
  the same copy-check-switch move the proxy store uses today.
- **Models:** the library is on the NAS; giving a model a role copies it to
  the Mac, and taking the role away frees that copy. Loading a 5–18 GB
  model straight off the NAS on every start would take minutes.
- **A Storage page** shows each kind: where it lives, how big it is, whether
  it is connected, and a Move button — no console.
- **The same marker rule as the proxy store.** Each NAS location carries a
  marker; when it is away, only what lives there pauses, and says so.
- Built before the mini is installed and migrated, the files land in the
  right place the first time; built after, it is one move per kind.
- **If the mini is installed before S1 exists**, the install's proxy folder
  is the same place S1 will use, so nothing moves later: create
  `Framefound` on the Photos drive, then install with
  `--proxies "/Volumes/Photos/Framefound/proxies"`.
- **As it happened**, the mini was installed and migrated on 2026-11-13,
  before S1 reached it. So it is the second case: after its first update,
  set the bulk folder on the Storage page, and move each kind that already
  has files on the Mac ([mac.md](mac.md#bulk-storage)).

## What already exists and is reused

- **Tag suggestions and the review queue** — `ai/tag_review.py` defines the
  contract (propose in the catalogue's vocabulary, land as `suggested`).
  The local Looker is a second provider behind it, with no budget to check.
- **"The model judges, the engine renders"** — `ai/recipe_picker.py`. The
  model returns slider values; the local colour engine applies them at full
  resolution. The same split is used for video colour.
- **The learned look** — `media/looks.py`. On photographs, copying how the
  most similar shipped photographs were edited beat every Claude model's
  judgment, locally and for free. So video colour starts by comparing three
  things by eye: the model's sliders, a look learned from clips already
  graded, and no change. The model does not get the job by default.
- **Shot windows, stills and measured facts** — `processing/shots.py`. The
  model adds what measurement cannot see: what the subject is, whether the
  framing works, whether something is in the way.
- **Rough cuts and cut history** — `mcp/video.py` `build_rough_cut` (since
  L5 a wrapper round `nle/roughcut.py`, where the function now lives). The
  local Editor writes through the same function, so a local draft opens in
  Premiere exactly as Claude's do and is recorded the same way.
- **The connector's tools** — `framefound/mcp`. The chat panel calls the
  same code; nothing is reimplemented for it.
- **The Claude budget** — `ai/spend.py`. The chat panel and "Ask Claude to
  review" spend from it and stop at the cap.
- **The proxy store** — `storage/stores.py`. The NAS bulk store is the same
  mechanism with more kinds.

New when this plan was written: any video colour output (there was no LUT
export), a quality score or caption on a shot or photograph, quiet hours, a
Storage page with moves. All four are in the code now: the LUT (L4), the
scores and captions a local model writes (L3's `looks`), quiet hours (L1's
overnight window) and the Storage page (S1).

## Phases

Each phase ends in something usable and is judged before the next starts.
Listed in the order recommended.

Where each stands on 2026-11-13, checked against the code. "Built" means
written and tested; the note at the top of this page says what that
leaves unknown, for all of them at once.

| Phase | Server | Web | Not there yet |
|---|---|---|---|
| **S1** — NAS bulk store | built | built: the Storage page | — |
| **C1** — chat panel with Claude | built | built: **Ask Claude** on every page | a first run against the real Claude API |
| **L1** — model manager, first runtime | built | built: the AI models page | — |
| **L2** — Compare | built | built: AI models › Compare models | the measured answer itself |
| **L3** — Looking | built | built: Folders in Browse, Review with AI, a library's box, reviews on Processing, the Looker's settings, the label on Tags, the shot list on a clip's page, what the local model saw on an item's page, the score in Browse, Search and a listing | — |
| **L4** — Colour | built | built: the Look card on a clip; a listing's local edits | a LUT loaded in Premiere |
| **L5** — Assembly | built | built: the Cuts pages; Draft a cut from Search, a clip's page and a folder in Browse | — |
| **L6** — second runtime (llama.cpp) | built | built: its row and a repository's builds on the AI models page | — |

- **S1 — NAS bulk store.** *Built 2026-11-12.* The two storage classes, the
  Storage page, a move per kind; the install takes one NAS folder instead
  of a proxy folder
  ([storage.md](storage.md#bulk-storage-a-folder-per-kind)). *Best done
  before L0, so the migration puts files where they will live.* (L0 came
  first in the end; see [Bulk storage on the NAS](#bulk-storage-on-the-nas)
  for what that means.)
- **L0 — Prerequisite.** *Done 2026-11-13:* FrameFound is installed on the
  mini and the catalogue is migrated ([mac.md](mac.md)). That Mac runs the
  release before this one; S1, C1 and L1–L6 reach it with its first
  `framefound update`.
- **C1 — Chat panel with Claude.** *Built 2026-11-12: the conversations,
  the connector's tools run in process, streaming turns, ask-first for
  changes, Stop, the budget checked before every request, and the panel on
  every page with the page's context ([chat.md](chat.md)). Not yet run
  against the real Claude API.* Independent of everything else here. Its
  two panel-only tools came with L5 and L3: asking the local Editor for a
  draft (`local_cut_draft`,
  [chat.md](chat.md#a-draft-from-the-local-editor)) and starting a folder
  review (`start_folder_review`,
  [chat.md](chat.md#a-review-by-the-local-looker)).
- **L1 — Model manager and first runtime.** *Built 2026-11-12
  ([how to use it](mac.md#local-ai-models),
  [ADR-0023](adr/0023-managed-model-runtimes.md)).* The "AI models" page, the
  three publisher lists, preflight and download into the NAS library, the
  MLX-VLM runtime with update and rollback, the `model-server` service, the
  `local-ai` lane, memory budgets and the overnight window, `framefound
  doctor` checks. Gemma 4 E4B and Gemma 4 12B installed through the page.
  *Done when:* a model added from the UI answers the Test button, with no
  console step.
- **L2 — Compare on your footage.** *Built 2026-11-13: the sample from a
  folder, a listing or the shots of finished cuts, frozen with its truth;
  one turn per model on the local-AI lane; the two agreements as counts; the
  answers side by side; the owner's decision; demo mode and a seeded
  comparison ([Comparing models](#comparing-models),
  [ADR-0024](adr/0024-compare-measures-agreement-not-quality.md)). The
  measured answer itself is still to come.* The Compare view, run on a
  fixed sample of shots from a job already cut (Brookside): tags against
  tags already decided on that footage, scores against the shots that were
  actually used in the finished cuts, shots per hour, memory. *Done when:*
  there is a measured answer to "which model looks", or a measured "none is
  good enough yet".
- **L3 — Looking.** *Built 2026-11-13. A folder review on
  request (administrators only, Start now or Tonight, clips made ready
  first, Cancel keeps what was looked at), looking at new footage with a
  box per library, what a look writes (suggestions of origin `local`, a
  caption and a score with who said it), the `/looks` API, and its use in
  search, `find_shots` and culling; the pages (Folders in Browse, Review
  with AI, the reviews on Processing, the Looker's settings, the label on
  Tags, the shot list on a clip's page, what the local model saw on an
  item's page, the score in Browse, Search and a listing); Claude's way to
  start a review from the chat panel; demo mode, and seeded clips ready to
  be looked at
  ([Reviewing a folder on request](#reviewing-a-folder-on-request)).* At
  ingest for new footage and photographs, and **Review with AI** on a
  folder for older material. Tags into the review queue, captions and
  scores onto shots and photographs, used by search, `find_shots`, culling
  and the UI.
- **L4 — Colour.** *Built 2026-11-13; no LUT has yet been loaded in
  Premiere. Video: the Look card on a clip's page — the model's suggestion,
  your look and no change side by side, choosing, a `.cube` per clip or a
  folder's as one zip, Suggest one clip or a folder
  ([video-looks.md](video-looks.md)); "your look" for video is learned from
  the clips you choose a look for (8 or more), and until then is your
  photograph look fitted to what a LUT can carry. Photographs: asking (a
  local-AI lane job) and applying (auto-edit mode local, on the exports
  lane) are built; nothing is applied until you tick it and press Apply
  ([ai-editing-models.md](ai-editing-models.md#auto-edits-modes)).* For
  video, a suggested look per clip, previewed on a still and exported to
  Premiere as a `.cube` LUT. For photographs, the `local` mode of listing
  auto-edit. Judged by eye against the learned look and against no change,
  side by side — not by a colour-difference number.
- **L5 — Assembly.** *Built 2026-11-13, but for one way in. The Cuts page
  (cut history, one line per cut), a draft asked for in a sentence from a
  folder, checked shot by shot and written by the function Claude's cuts
  use; versions kept side by side with Compare; Download for Premiere for
  any cut and any computer; Ask Claude to review through the chat panel;
  Claude's own way to ask the Editor (`local_cut_draft`); demo mode and
  seeded cuts
  ([Rough cuts from the local Editor](#rough-cuts-from-the-local-editor)).
  Draft a cut from Search, a clip's page and a folder in Browse.* A local draft rough cut from a brief, and "Ask Claude to
  review" through the chat panel. Both versions kept.
- **L6 — Second runtime.** *Built 2026-11-13: llama.cpp as a second
  program on the page, installed from GitHub's checksummed release archive
  rather than Homebrew, with update, roll back and install by version or
  daily build; the Check lists a GGUF repository's builds with one
  suggested; two builds of a model are two models; a model on llama.cpp
  is asked about once before a role until Compare has seen it; `framefound
  doctor`'s llama.cpp lines; a second demo program and GGUF demo
  repositories ([mac.md](mac.md#local-ai-models),
  [ADR-0023](adr/0023-managed-model-runtimes.md)). Whether macOS runs the
  unpacked program, and every flag it is started with, are first checked on
  the mini.* llama.cpp for GGUF builds, which
  opens the door to overnight Nemotron 3 Nano Omni (see the corrections
  above: no build of it fits the default overnight budget). Cosmos Reason
  2 is refused (its base comes from a blocked publisher). Each goes through
  Compare before it is given a role.

## Risks

- **Quality is unmeasured.** Nothing here has seen this footage. L2 exists
  so that L3–L5 are not built on a model that turns out to tag poorly.
- **MLX support moves under us.** Gemma 4 12B's picture support depends on
  runtime releases outside FrameFound. Managed, updatable runtimes are the
  answer, and also the most novel part of the design.
- **A small model drafting a cut** may be plainly worse than Claude. That is
  why Claude stays one button away, and why both versions are kept.
- **Memory.** 32 GB shared with Premiere leaves room for one small model by
  day. The budgets enforce that rather than trusting it.
- **Licences differ per build**, not just per model. The manager shows the
  licence of the exact repository being added and records it.
- **The NAS becomes load-bearing.** With bulk storage there, a NAS that is
  down pauses proxies, renders, exports and model changes at once. Search,
  thumbnails and anything already loaded keep working.
- **"Not Chinese" is enforced by lists, not by detection.** Hugging Face
  does not state where a publisher is based. The blocked list covers the
  well-known labs; an unfamiliar publisher still has to be added to the
  makers list by hand before anything of theirs can be installed.

## Sources

Checked 2026-11-12; model cards change, so the manager's preflight reads
the live card rather than trusting this table.

- Gemma 4 model card — https://ai.google.dev/gemma/docs/core/model_card_4
- Gemma 4 E4B, MLX 4-bit — https://huggingface.co/mlx-community/gemma-4-e4b-it-4bit
- Gemma 4 12B on MLX — https://huggingface.co/mlx-community/gemma-4-12B-it-OptiQ-4bit ,
  https://huggingface.co/agentmish/gemma-4-12B-it-mlx-8bit ,
  https://mlx-optiq.com/docs/gemma-4
- Molmo 2 — https://allenai.org/molmo , https://huggingface.co/allenai/Molmo2-O-7B ,
  https://docs.voxel51.com/model_zoo/models/allenai_Molmo2_O_7B.html
- Nemotron 3 Nano Omni — https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-BF16 ,
  https://build.nvidia.com/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning/modelcard
- Cosmos Reason 2 — https://huggingface.co/nvidia/Cosmos-Reason2-8B ,
  https://huggingface.co/masato25/Cosmos-Reason2-2B-Arbor-4bit
- MLX-VLM — https://github.com/Blaizzy/mlx-vlm
- mlxcel — https://github.com/lablup/mlxcel ,
  https://www.backend.ai/blog/releases/2026-05-lablup-opensourced-mlxcel
