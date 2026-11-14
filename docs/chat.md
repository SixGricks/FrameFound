# The chat panel — Claude inside FrameFound

A panel in the web UI for talking to Claude about the catalogue: "what have
we got of the barn at dusk", "look at these four and tell me which is
sharpest", "cut me thirty seconds from this folder for Premiere". It can do
what the [MCP connector](mcp.md) lets Claude do from Claude Desktop, without
a token to make, a config file to edit or a second window: FrameFound holds
the conversation, calls the Claude API itself, and runs the connector's
tools on the server as the person who is signed in.

> **Status.** Built 2026-11-12: the server side and the panel. The panel
> opens from **Ask Claude** in the top bar on every page (a drawer beside
> the page on a wide window, a full-screen sheet on a phone), and tells
> Claude which page you are on. Nothing here has yet been run against the
> real Claude API or on a production install: the behaviour below is
> pinned by tests in which Claude is played by a script, and the panel was
> checked in a browser against those scripted answers.

Three rules hold whatever Claude is asked:

- **Nothing writes to an original.** The panel can do what the connector
  can, and the connector cannot reach a file.
- **A change waits for your yes.** Deciding a tag, deciding many, starting
  a paid review, and starting a review of a folder by the local model are
  shown to you first and run only when you agree, one request at a time.
- **The monthly budget is checked before every request to Anthropic**, not
  once per question. A question can take a dozen requests.

---

## What it can do

Everything the connector's tools do: find and search media, look at a
preview, a frame or a contact sheet, read a transcript, list scenes, find
shots, build selects and rough cuts as a timeline for Premiere, read the
review queue, propose tags, record your decisions, start a review. The
full list is in [mcp.md](mcp.md#what-claude-can-do); the panel runs the same code,
so the two never disagree.

It also knows **where you are**. The page you are on, and the folder, item,
tag, search or selection on it, is passed along with what you type, so
"this clip" and "these" mean what is on your screen.

A **timeline** Claude builds arrives as a download in the panel, not as
text in the conversation.

### A draft from the local Editor

Two tools are the panel's alone, and both are offered only where this
install can run local models: `local_cut_draft`, here, and
`start_folder_review`, [below](#a-review-by-the-local-looker). Where it can
run local models (the Mac install; [local-models.md](local-models.md#rough-cuts-from-the-local-editor)),
Claude can ask the **local Editor**, the model given the Editor role on the
AI models page, for a rough cut. It costs nothing: the Editor runs on your
own machine. Claude uses it when you ask for "a local draft" or "a free
first pass", or say to save the Claude budget.

- **Starting one** takes the brief, the footage (a folder name, matched
  anywhere in the path, with a library if the name is not unique; or a list
  of clip ids) and a length in seconds. With no length given, the one the
  brief names is used, else thirty seconds. It is the same request the Cuts
  page's **Draft a cut** form makes, and the draft shows on that page like
  any other.
- **Footage too big for one draft is refused.** A draft reads at most
  20,000 shots. Footage holding more (a client's or a year's folder, say)
  is refused in the Cuts page's own words: the folder holds more shots
  than one draft can read (20,000), so name a narrower folder or choose
  fewer clips. Nothing is started, and Claude can ask again with one
  shoot's folder.
- **Claude waits up to half a minute.** A draft that is done by then comes
  back as numbered lines (clip, in and out, the Editor's reason for each)
  with the cut's id. A slower one, or one that has to wait for the overnight
  window, is answered at once with where it stands and a draft id; Claude
  can read it later with that id, or you can watch it on the Cuts page.
- **It does not ask first.** Asking for a draft changes nothing in the
  catalogue and spends nothing, like building a rough cut.
- **To improve a draft**, Claude builds its own version with
  `build_rough_cut` and the draft's id as `parent`, and it is kept as the
  next version beside the draft ([mcp.md](mcp.md)).

The tool is not offered on an install that cannot run models (the
containers), and it is never part of the external connector.

**Asked from a cut's page.** **Ask Claude to review** on a cut
(Cuts › a cut) opens the panel on a new conversation with a request
attached, written by FrameFound: what the cut is and its id, your brief, the
cut shot by shot with the reasons given, the shots it was chosen from, and
how to keep a better version beside it. The panel shows what is attached and
that sending spends from the monthly budget; **nothing is sent until you
press Send**, and you can add a sentence of your own or take the attachment
away. What is attached then goes to Anthropic as part of your message: file
names, shot descriptions and the local model's reasons, as text. **Ask
Claude instead** on a draft that failed does the same with the brief and
the footage written out as a message for you to send.

### A review by the local Looker

The panel's other tool is `start_folder_review`. Where this install can run
local models, Claude can start what **Review with AI** starts on a folder
in Browse: the **Looker**, the model with that role on the AI models page,
looks at every photograph and shot in one folder (and everything beneath
it), or in a whole library, that has not been looked at yet. It suggests
tags for the review queue and gives each item a caption and a 0–100 use
score ([local-models.md](local-models.md#reviewing-a-folder-on-request)).
It costs nothing and nothing leaves the building; it is slow.

- **It asks first, every time.** A review can be hours of the model's time
  and of making playable copies. What you are shown is one line made from
  what Claude asked for and nothing else: "Review GELCO / Pete Dye with the
  local model, starting now", "Review all of GELCO with the local model,
  tonight", and ", looking again at what another model saw" when Claude
  asked for that. How much there is to look at is not in it: nothing is
  counted until you say yes.
- **What Claude gives it** is the library (by name, capitals aside, or by
  id), the folder as Browse shows it (none for the whole library), now or
  tonight, and whether to look again at what a different model already
  looked at. The review always uses the Looker and always makes clips ready
  first; choosing another model, or not making clips ready, is the
  dialog's.
- **Administrators only**, as on the page. Any other account is refused
  after the yes, in the page's own sentence.
- **It is the same review.** It goes through the code the page's button
  goes through, so it is refused in the same words (no Looker, a model that
  only runs overnight asked for now, a review of that folder already
  running, nothing left to look at, the processing queue down), is written
  to the audit log the same way (`looks.review_started`), and shows on
  Processing, where it can be followed and cancelled. The panel has no tool
  that cancels one. Once it has started, a model that will not load ends
  the review at once ("Stopped: the model could not be used — …" on
  Processing); it is not tried again item by item.
- **What Claude is told back** is what the dialog would have shown: the
  folder and the model, how many photographs, clips and shots there are to
  look at, how many clips have to be made ready first, about how long, and
  that it can be followed on Processing. On a demonstration copy it is also
  told that every caption and score will be made up.

Like the draft tool, it is not offered on an install that cannot run
models, and it is never part of the external connector.

### Closing the tab

**If you close the tab** or the connection drops, the turn carries on: the
tools it had started finish, the answer is stored, and the conversation is
there when you come back. A turn still running can be picked up where it
is. Closing the tab is therefore **not** how to stop an answer; Stop is.

### Stopping an answer

**Stop** ends the answer Claude is working on. What it does:

- **It hangs up on Claude at once.** Whatever Claude was doing (thinking,
  writing, composing a request for a tool), the connection to Anthropic
  is closed that moment, and nothing more is asked. This is the point of
  the button: an answer you no longer want stops costing money.
- **What you had already seen is kept** as Claude's answer, as far as it
  got, so the conversation reads the way it looked when you stopped it.
  If Claude had not written a word yet, nothing is shown, only *Stopped.*
- **You can carry straight on.** Type the next message. Claude is told,
  with that message, that you pressed Stop, wherever in the answer the
  stop landed: that its answer was cut part-way and nothing after the part
  you saw happened, or, if it was waiting on tools at the time, that their
  results arrive because you stopped and not because it should carry on.
  Either way it is told not to go back to the question you dropped unless
  you ask. (The exception is below: answering a request that was left
  waiting for your yes takes the question up again.)

What it does **not** do:

- **It does not interrupt a tool that is already running.** A tool that
  has started is left to finish and its result is kept. Half of a change
  is worse than all of it: recording two hundred tag decisions, or
  starting a paid review, is never cut off in the middle. The turn ends
  when that tool is done, which for most tools is a moment and can be up
  to three minutes. Tools Claude had asked for in the same answer that
  had **not** started do not start, and Claude is told they were not run.
- **It is not a "no".** A request that was waiting for your yes when you
  pressed Stop is still waiting afterwards. Type something else (which
  counts as no, as always, and Claude is told you stopped), or answer it:
  once every waiting request has its yes or no, Claude is handed the
  results and **carries on with the question you stopped**, as it would
  have without the stop. If you had just said yes to several requests and
  stop while the first is being carried out, that one finishes and the
  rest go back to waiting: they are shown to you again, not run later on
  the strength of the earlier yes, and no yes is written to the audit log
  for them.
- **It does not give back what was already spent.** Anthropic bills a
  request from the moment it begins to answer, and the part of the answer
  already read is paid for (see [What it costs](#what-it-costs)).
- **It does not undo anything.** Tools that had finished before you
  pressed Stop stay done.

Pressing Stop when nothing is running, or at the very moment an answer
finishes by itself, does nothing and is not an error.

Each account's conversations are **its own**. Nobody else sees them in
FrameFound, administrators included. (Someone with the database can read
anything in it; this is about the product, not the disk.)

---

## What leaves the building

The same things the connector sends, to the same place: Anthropic's API,
over HTTPS, with the key from the Security page. Nothing else, and nowhere
else.

| Sent | When |
|---|---|
| What you type | Every message |
| The page you are on: its address and title, and the folder, item, tag, search or person it shows; up to 50 selected item ids | With each message, if the page has any |
| What the tools answer: file names, tags, dates, transcript lines, shot descriptions, paths as your workstation sees them | When Claude uses a tool |
| A preview Claude asks to look at: a JPEG no larger than 768 pixels, or a contact sheet of small frames | When Claude uses a picture tool, **unless pictures are off** |
| FrameFound's instructions to Claude and the list of tools | Every request (cached by Anthropic for a few minutes at a time) |

**Never sent:** an original, a proxy, a full-size preview, or anything
Claude did not ask a tool for.

The whole conversation so far is sent again with every request. That is
how the API works: it keeps nothing between requests. FrameFound keeps the
conversation on the server, in the database and under `chat/` in the data
folder (the previews Claude was shown and the timelines it built).
**Deleting a conversation** removes all of that. What Anthropic retains of
a request is set by your agreement with them, not by FrameFound.

### Switching pictures off

Pictures are **on** by default, as they are in the connector. The switch
is per conversation. With it off, a picture tool still runs and **you**
are shown the picture in the panel; Claude is sent its caption (file name,
type, tags) and told a picture was kept back.

The switch decides what is sent **from then on**, the rest of an answer
Claude is in the middle of included: if it has asked for five previews and
you switch pictures off while the second is being made, the first has gone
and the other four stay with you. It does not take back a picture Claude
has already been shown in that conversation: those are part of the
conversation and go with every later request. For a conversation with no
pictures in it, start it with pictures off.

---

## What asks first

| Tool | What you are asked |
|---|---|
| `decide_tag` | Confirm (or reject) the tag *X* on item *Y* |
| `decide_tags` | Tag *X*: confirm it on *n*, reject it on *m* |
| `start_api_review` | Start a paid Claude tag review of *n* items |
| `start_folder_review` | Review *library / folder* (or all of a library) with the local model, starting now or tonight |

These change what the catalogue holds as fact (a decided tag teaches the
learner), spend from the budget, or set the Mac to hours of work. Claude's request is shown with a yes
and a no; nothing happens until you answer. **No** is told to Claude, and
it is asked not to try again. **Typing a new message instead of answering
counts as no.** When one answer from Claude holds several such requests,
each gets its own yes or no.

Every yes and no is written to the audit log with your account
(`chat.tool_approved`, `chat.tool_declined`), a no given by typing instead
included (its row says `how: new message`). A yes is recorded before the
tool is run, so nothing that changes the catalogue or spends money can
happen without its row. If FrameFound is stopped while an approved request
is running, it is not asked about again: Claude is told it was interrupted
and that it is not known whether it took effect, and checks.

Not asked about, on purpose:

- `propose_tags`: a proposal is only a suggestion in the review queue,
  which is where a yes or no is asked anyway.
- `build_rough_cut` and `build_selects`: they write cut history (so the
  next cut can avoid repeating a shot) and hand you a file. They do not
  change the catalogue.
- `local_cut_draft`: a draft from the local Editor costs nothing and is
  only a draft on the Cuts page, which marks no shot as used until you
  download it.

The tools keep their own rules as well. A **read-only account** cannot use
the panel at all (it can read conversations it already had). Starting a
paid review is still **administrators only**, with or without a yes, and
so is starting a review by the local model.

---

## What it costs

The panel uses the Claude API and is paid for by the token, under the same
**monthly budget** as auto-edit, file naming and the tag reviewer
(Security → Claude). There is one budget for all four.

- Before **every** request FrameFound prices what it is about to send and
  compares the month's spend plus that to the cap. Over it, the request is
  not made, the turn ends with a sentence saying so, and nothing is
  charged. A budget of 0 pauses the panel without removing the key.
- After every request the real cost goes on the ledger (purpose `chat`),
  and the panel shows the conversation's total and the month's.
- A request that **fails after Claude has begun to answer** is not free:
  Anthropic has read the whole conversation by then and bills for it. It
  goes on the ledger at what Anthropic said it had counted when it stopped
  (everything read; what had been written by then is not known and is left
  out), and counts toward the cap. A request Anthropic turned away before
  answering (a bad key, a rate limit, an outage) costs nothing and is
  recorded as costing nothing.
- An answer you **stop** is priced by the same rule, because to Anthropic
  it is the same thing: a request that was hung up on. If Claude had begun
  to answer, everything it had read goes on the ledger (marked *stopped by
  the operator*) and counts toward the cap; the words it had written by
  then are billed by Anthropic too, but how many is only reported at the
  end of an answer, so they are left out of FrameFound's figure. Stop
  saves what the rest of the answer, and every request after it, would
  have cost. Stopped before Claude had begun to answer, the request is
  recorded as costing nothing.
- One question may take at most **12 requests**. A question that needs
  more stops there and says so; "carry on" continues it.

Prices, for the default model, **Claude Opus 5.5**: $4 per million tokens
read, $20 per million written (its thinking counts as written), and $0.20
per million for re-reading what Anthropic has cached. FrameFound marks the
instructions, the tool list and the conversation so far for caching, so
after the first request of a conversation almost everything is re-read at
the cached price. The cache lasts five minutes from its last use: come
back after a longer pause and the next request pays once more to read the
conversation in.

As a guide, from the price list rather than from measurement: the
instructions and tools are roughly 7,000 tokens, so the first question in
a conversation costs about five cents and a follow-up within a few minutes
a cent or two. A preview adds a little under a cent the first time Claude
sees it. A long transcript or a hundred search results cost more,
and stay in the conversation; a tool answer over 60,000 characters is cut
and says so. **The panel's own figure after each answer is the real one.**

---

## The model

Security → Claude → **Chat model**. Opus 5.5 unless changed. The choice
applies to conversations started afterwards; a conversation keeps the
model it began on, because what Claude wrote earlier in it cannot be
handed to a different model.

Only models FrameFound has a price for are offered. A model it could not
price would spend outside the budget, so the setting refuses one. Claude
Haiku 4.5 has a price and is refused for a different reason, which the
refusal gives: it takes another kind of request than the panel sends. It
can still be the review model.

---

## What it does not do yet

- **Old conversations are never cleaned up.** There is no retention
  period and nothing that tidies: a conversation, the previews Claude was
  shown in it and the timelines it built (under `chat/` in the data folder)
  stay until you delete that conversation yourself. Deleting it removes its
  rows and its files.
- **A conversation cannot be renamed.** Its title is the first line of the
  first message, cut to 80 characters.
- **The list shows the 50 most recently used** conversations of your
  account and has no further pages. Older ones are still stored; they are
  not listed.

---

## When it does not work

| The panel says | Cause |
|---|---|
| *The chat panel needs an Anthropic API key* | No key on the Security page. An administrator adds one. |
| *Claude is switched off for this server* | The key is there and Claude is disabled on the Security page. |
| *Monthly AI budget reached* | The month's spend plus this request would cross the cap. Raise the budget, or wait for the month to turn. |
| *Claude is still working on the last message* | A turn is running in this conversation, perhaps from another tab. Wait for it, or press Stop. It is **not** said after a restart or an update: the server knows a turn from before it started cannot still be running, and the conversation is free at once. (The one case that still takes up to five minutes is a turn that broke off without the server restarting, for instance because the database could not be reached as it ended.) |
| *Stopped.* | You pressed Stop. What Claude had written is kept; a tool that was running finished; nothing else was done. Carry on with the next message. |
| *Claude declined to answer that* | A refusal. Nothing was changed. Ask differently, or start a new conversation. |
| *Claude's answer reached its length limit* | It was cut off; a tool it was about to use was not run. Ask it to carry on. |
| *Claude used the 12 steps one question may take* | Say "carry on". |
| *Anthropic refused the API key* / *cannot pay for this* | The key is wrong or revoked, or the Anthropic account has no credit. |
| *Anthropic is overloaded* / *limiting how fast this key may ask* | Try again in a minute. |
| *The stored Anthropic API key cannot be read* | The server's secret key has changed since the API key was saved. An administrator enters the API key again on the Security page. |
| *The connection to Anthropic was lost part-way through an answer* | The connection was dropped, or sent nothing at all for five minutes. Ask again. What was read is on the ledger. |
| *Claude stopped answering part-way through and FrameFound gave up waiting* | The connection stayed open and no part of an answer arrived on it for fifteen minutes. Neither this nor the one above is said while Claude is only slow: thinking, or writing out a long timeline or a few hundred tag decisions, can take minutes with nothing to show, and is waited for. What was read is on the ledger. |
| *Claude's answer ended early* / *arrived damaged* | The answer broke off or could not be read. Nothing was changed; ask again. What was read is on the ledger. |
| *FrameFound was stopped while working on this* | The server was restarted (an update does this) while Claude was answering. The answer in progress is lost; anything a tool had finished before then is kept. Ask again: the conversation is free as soon as the server is back, and Claude is told that a tool cut off mid-run may or may not have taken effect, and checks. |
| *This conversation has grown too long* | Start a new one. |
| *This conversation was started with an earlier version of FrameFound's tools…* | Its stored copy of the tool list is gone from the data folder. Start a new one. |
| *The chat panel needs the connector's tools* / *the Anthropic SDK* | The server was installed without the `mcp` or the `chat` extra: `pip install 'framefound[mcp,chat]'` (the Docker image and the Mac install include both). |
| Nothing appears until the whole answer is done | Something between the browser and the API is collecting the stream. FrameFound's own layers do not (tested); a proxy added in front must not buffer `text/event-stream`. |

---

## For developers: the API

All under `/api/v1/chat`, with the browser's session cookie. Errors use the
usual envelope, `{"error": {"code", "message", "detail"}}`. A conversation
that is not the caller's answers **404** on every route. A read-only
account gets **403** on every route that is not a GET.

| | |
|---|---|
| `GET /status` | Can the panel be used: `available`, `configured`, `enabled`, `problem` (a sentence, or null), `can_chat`, `model`, `models`, `budget_usd`, `spent_this_month_usd` |
| `GET /conversations` | The caller's conversations, newest first |
| `POST /conversations` | Start one. Body optional: `{"pictures": true}`. 201 |
| `GET /conversations/{id}` | The conversation for display: messages, tool chips, calls waiting for an answer, state, the turn in progress if any |
| `PATCH /conversations/{id}` | `{"pictures": false}`: the pictures switch |
| `DELETE /conversations/{id}` | 204. 409 while a turn is running |
| `POST /conversations/{id}/messages` | `{"text", "context"?, "pictures"?}` → an event stream |
| `POST /conversations/{id}/confirm` | `{"decisions": [{"tool_use_id", "approve"}]}` → an event stream, the same turn carried on |
| `POST /conversations/{id}/stop` | Stop the running turn. No body. 200 with the conversation's summary (the fields of one entry of `GET /conversations`); the same, and nothing else, when no turn is running |
| `GET /conversations/{id}/events?after=N` | Follow the turn the server has in memory from event `N`. 204 when there is none |
| `GET /conversations/{id}/blobs/{sha}` | A picture from the conversation |
| `GET /conversations/{id}/artifacts/{tool_use_id}` | A timeline a tool built, as a download |

A stream is `text/event-stream`. Each event is `id: <index>`,
`event: <name>`, `data: <one line of JSON>`; lines beginning with `:` are
keep-alives. In the order a turn says them:

| Event | Data |
|---|---|
| `start` | `conversation_id`, `model`, `pictures`, `message_id` (the stored message this turn began with; null when it is a confirm), `seq` |
| `thinking` | `on` |
| `text` | `delta` |
| `tool` | `id`, `name`, `input`, `state`: `preparing` (input null: Claude is still writing the call) then `running` |
| `usage` | After each request that was billed: `cost_usd`, `conversation_usd`, `month_usd`, `budget_usd`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`. A request that failed after Claude began to answer has one too, just before its `error` |
| `tool_result` | `id`, `name`, `state` (`ran`, `failed`, `approved`, `declined`), `ok`, `summary`, `images` (`blob`, `media_type`), `artifact` (`name`) or null |
| `confirm` | `id`, `name`, `input`, `summary`: a call that waits for a yes. `summary` is `Run <name>` when Claude's arguments are not of the kind the tool takes |
| `error` | `kind` (`config`, `budget`, `api`, `internal`), `message` |
| `done` | Always last, and said once. `reason`: `end`, `awaiting_confirmation`, `budget`, `steps`, `refusal`, `max_tokens`, `error`, `stopped`; `message`: the sentence to show, or null (`"Stopped."` for `stopped`, which has no `error` event before it) |

Before the stream starts a request can be refused with 400 (a sentence),
404, 409 (a turn is already running) or 503 (the extras are not
installed). Once it has started the status is 200 and a failure is an
`error` event followed by `done`.

The events of a turn are kept in the API process's memory, which is why
following one needs the API to be a single process (it is: the Docker
image, the compose file and the Mac services each start one uvicorn with
no `--workers`). A finished turn can be replayed for ten minutes; after
that, or once its conversation is deleted, `GET .../events` answers 204.

A tool chip's `state` in `GET /conversations/{id}` is one of `running`
(being run: an at-once call from the moment Claude asks, an approved call
from the moment of the yes), `ran`, `pending` (waiting for a yes or no),
`approved`, `declined`, `failed`. `failed` covers a tool that reported an
error, a call that was interrupted, a call that was never run because
Claude's answer was cut off, and a call that had not started when the
operator pressed Stop; `summary` says which. A chip still `running` in a
conversation that is not running was interrupted.

**Stop** (`POST .../stop`) is a request the turn acts on, not a
cancellation. The route answers after at most two seconds with the
summary as it then stands: `idle` or `awaiting_confirmation` when the turn
has ended (the usual case: a turn waiting on Claude ends in
milliseconds), `running` when a tool is still finishing. The end itself
is the `done` event, `{"reason": "stopped", "message": "Stopped."}`, on
the stream of the send or confirm that started the turn and on every
`GET .../events` following it. Before it come, as usual, the
`tool_result` of a tool that was allowed to finish, a `tool_result` with
state `failed` for each tool of that answer that was not started, a
`confirm` for each call still waiting for a yes, and a `usage` if the
request that was dropped had begun to be answered. A turn started by
`POST .../confirm` says those `confirm` events too, just before `done`:
one for every call still waiting, which includes a call that the same
request answered yes and the stop reached before it was run (it is
`pending` again, and a client that dropped it from its list when it sent
the yes puts it back). A stop that arrives when the answer is already
whole ends the turn the way it was ending (`end`, say), not as `stopped`.

What a stop leaves stored, and so what `GET /conversations/{id}` shows
afterwards (`last_turn.reason` is `stopped`):

- Stopped while Claude was answering: an assistant message with the text
  that had arrived and `stop_reason: "stopped"`. A tool call Claude was
  still writing is not stored, so a chip the live stream showed as
  `preparing` is gone on reload: re-read the conversation after a
  `stopped`. If no text had arrived, no message is shown at all.
- Stopped while Claude was answering about what a tool had found: the
  earlier assistant message with its chips, and after it a second one
  with the text that had arrived and `stop_reason: "stopped"`.
- Stopped while tools were being run: Claude's message with its chips,
  each `ran`, `failed` (not started) or `pending`.

The operator's next message is sent to Claude with a `<stopped>` note in
front of it (after the tools' results, when there are any to send). It is
part of what Claude is sent, never of the message's `text`, so nothing
needs hiding when the conversation is drawn. No note is added when the
turn is carried on by answering the calls that were waiting instead.

After a restart, a conversation whose turn was lost reads `idle` at once,
with `last_turn` `{"reason": "error", "message": "FrameFound was stopped
while working on this. Ask again."}` until its next turn ends.

**A tool only the panel has** is one function and one
`tools.register(tools.ChatTool(...))`; the docstring of
`framefound/chat/tools.py` has a worked example. It is deliberately not
added to the connector. There are two. `local_cut_draft`
(`framefound/nle/draft_chat.py`) is registered once, when the Cuts API is
imported; `start_folder_review` (`framefound/looking/chat_tool.py`,
registered with `confirm=True` and a `describe` for its chip) once, when
the Looking API is imported. Both register only where
`local_models.availability()` says models can run, so the tool list is the
same for every conversation on one install.

## Reference

| | |
|---|---|
| The turn loop | `framefound/chat/engine.py` |
| Tools, run in process | `framefound/chat/tools.py` (over `framefound/mcp`) |
| The panel's own tools | `framefound/nle/draft_chat.py` (`local_cut_draft`), tested in `tests/test_draft_chat.py`; `framefound/looking/chat_tool.py` (`start_folder_review`), tested in `tests/test_looking_chat_tool.py` |
| The one call to Anthropic | `framefound/chat/claude_stream.py` (the official `anthropic` SDK) |
| Storage | `framefound/chat/store.py`; tables `chat_conversations`, `chat_messages`, `chat_tool_calls` (migration 0031); files under `<data>/chat/` |
| Routes | `framefound/api/v1/chat.py` |
| Budget and prices | `framefound/ai/spend.py` |
| Tests | `tests/test_chat.py` (the loop, Claude scripted), `test_chat_api.py` (the routes, streams read piece by piece), `test_chat_stream.py` and `test_chat_wire.py` (the real SDK over a mock transport), `test_chat_without_extras.py` (the API loads and says what is missing with neither extra installed; needs neither) |
