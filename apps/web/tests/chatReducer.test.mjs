// Run with `npm test` (Node's own test runner; nothing to install).
//
// The chat panel draws one thing from two sources: the conversation as the
// server stored it, and the events of a turn as it happens. These pin that
// the two agree — a turn read live, a turn replayed after a reload and the
// stored conversation afterwards must all draw the same — using the worked
// example the server team wrote down (briefs/C1-api-surface.md), and then
// the ways a turn goes wrong.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  emptyView,
  expecting,
  reduce,
  viewOf,
  withSent,
  withoutSent,
} from "../src/lib/chatReducer.ts";

// --- the worked example ------------------------------------------------------

const ASK = "Confirm the tag “Silo” on item 1704c870";
const WRITE_INPUT = { id: "1704c870", tag: "Silo", accept: true };
const USAGE = {
  cost_usd: 0.0014,
  conversation_usd: 0.0014,
  month_usd: 0.0028,
  budget_usd: 5.0,
  input_tokens: 100,
  output_tokens: 50,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
};

/** Number a list of [name, data] pairs the way one turn's stream does. */
const turn = (...pairs) => pairs.map(([event, data], index) => ({ event, index, data }));

const START_SEND = {
  conversation_id: "C",
  model: "claude-opus-5-5",
  pictures: true,
  message_id: "M1",
  seq: 1,
};

const SEND = turn(
  ["start", START_SEND],
  ["thinking", { on: true }],
  ["thinking", { on: false }],
  ["text", { delta: "I'll confirm " }],
  ["text", { delta: "that." }],
  ["tool", { id: "toolu_read", name: "list_tags", input: null, state: "preparing" }],
  ["tool", { id: "toolu_write", name: "decide_tag", input: null, state: "preparing" }],
  ["usage", USAGE],
  ["tool", { id: "toolu_read", name: "list_tags", input: {}, state: "running" }],
  [
    "tool_result",
    {
      id: "toolu_read",
      name: "list_tags",
      state: "ran",
      ok: true,
      summary: "12 tags",
      images: [],
      artifact: null,
    },
  ],
  ["confirm", { id: "toolu_write", name: "decide_tag", input: WRITE_INPUT, summary: ASK }],
  ["done", { reason: "awaiting_confirmation", message: null }],
);

const START_CONFIRM = { ...START_SEND, message_id: null, seq: 2 };

const APPROVE = turn(
  ["start", START_CONFIRM],
  ["tool", { id: "toolu_write", name: "decide_tag", input: WRITE_INPUT, state: "running" }],
  [
    "tool_result",
    {
      id: "toolu_write",
      name: "decide_tag",
      state: "approved",
      ok: true,
      summary: "Confirmed “Silo”.",
      images: [],
      artifact: null,
    },
  ],
  ["text", { delta: "Confirmed." }],
  ["usage", { ...USAGE, conversation_usd: 0.0028 }],
  ["done", { reason: "end", message: null }],
);

const DECLINE = turn(
  ["start", START_CONFIRM],
  [
    "tool_result",
    {
      id: "toolu_write",
      name: "decide_tag",
      state: "declined",
      ok: false,
      summary: "The operator declined this.",
      images: [],
      artifact: null,
    },
  ],
  ["text", { delta: "Left as it was." }],
  ["usage", USAGE],
  ["done", { reason: "end", message: null }],
);

const play = (view, events) => events.reduce(reduce, view);

const TYPED = "The kitchen photo does show a silo";
const CONTEXT = { path: "/tags", tag: "silo" };

/** A stored chip, with the fields that are usually empty filled in. */
const chip = (id, name, state, more = {}) => ({
  id,
  name,
  input: {},
  state,
  needs_confirm: false,
  ok: state === "ran" || state === "approved",
  summary: "",
  ask: null,
  images: [],
  artifact: null,
  ...more,
});

const stored = (more = {}) => ({
  id: "C",
  title: TYPED,
  model: "claude-opus-5-5",
  pictures: true,
  state: "idle",
  spent_usd: 0,
  created_at: "2026-11-12T10:00:00+00:00",
  updated_at: "2026-11-12T10:00:00+00:00",
  input_tokens: 0,
  output_tokens: 0,
  last_turn: null,
  run: null,
  messages: [],
  pending: [],
  ...more,
});

const message = (seq, role, text, more = {}) => ({
  id: `M${seq}`,
  seq,
  role,
  text,
  context: null,
  tools: [],
  stop_reason: null,
  cost_usd: null,
  created_at: "2026-11-12T10:00:00+00:00",
  ...more,
});

/** What GET /conversations/C answers once the first stream has ended. */
const AWAITING = stored({
  state: "awaiting_confirmation",
  spent_usd: 0.0014,
  last_turn: { reason: "awaiting_confirmation", message: null },
  messages: [
    message(1, "user", TYPED, { context: CONTEXT }),
    message(2, "assistant", "I'll confirm that.", {
      stop_reason: "tool_use",
      tools: [
        chip("toolu_read", "list_tags", "ran", { summary: "12 tags" }),
        chip("toolu_write", "decide_tag", "pending", {
          input: WRITE_INPUT,
          needs_confirm: true,
          ask: ASK,
        }),
      ],
    }),
  ],
  pending: [{ id: "toolu_write", name: "decide_tag", input: WRITE_INPUT, summary: ASK }],
});

/** What is on screen, without the keys and ids a live view cannot know. */
const drawn = (view) => ({
  messages: view.messages.map((m) => ({
    role: m.role,
    text: m.text,
    tools: m.tools.map((t) => ({
      id: t.id,
      name: t.name,
      input: t.input,
      state: t.state,
      needs_confirm: t.needs_confirm,
      ok: t.ok,
      summary: t.summary,
      ask: t.ask,
    })),
  })),
  pending: view.pending,
  running: view.running,
  thinking: view.thinking,
  reason: view.ended?.reason ?? null,
});

const sent = () => withSent({ ...emptyView(), id: "C" }, TYPED, CONTEXT);

test("the worked example, read live, draws what the server then has stored", () => {
  const live = play(sent(), SEND);

  assert.deepEqual(drawn(live), drawn(viewOf(AWAITING)));
  assert.equal(live.spent_usd, 0.0014);
  assert.equal(live.events, SEND.length);
  // The message typed here is the stored one now, and kept its key.
  const mine = live.messages[0];
  assert.deepEqual(
    { key: mine.key, id: mine.id, seq: mine.seq, sending: mine.sending, context: mine.context },
    { key: "sent-1", id: "M1", seq: 1, sending: false, context: CONTEXT },
  );
});

test("the turn takes shape event by event", () => {
  let view = sent();
  assert.equal(view.running, true);
  assert.equal(view.messages[0].sending, true);
  assert.equal(view.title, TYPED);

  view = play(view, SEND.slice(0, 2));
  assert.equal(view.thinking, true);
  assert.equal(view.messages.length, 1, "thinking alone writes nothing");

  view = play(view, SEND.slice(2, 5));
  assert.equal(view.thinking, false);
  assert.equal(view.messages[1].text, "I'll confirm that.");

  view = play(view, SEND.slice(5, 7));
  assert.deepEqual(
    view.messages[1].tools.map((t) => [t.id, t.state, t.input]),
    [
      ["toolu_read", "preparing", null],
      ["toolu_write", "preparing", null],
    ],
  );

  view = play(view, SEND.slice(7, 9));
  assert.equal(view.messages[1].tools[0].state, "running");
  assert.deepEqual(view.messages[1].tools[0].input, {});
  assert.equal(view.running, true);
  assert.equal(view.ended, null);
});

test("a yes carries the same turn on: the call runs, and Claude's reply is a new answer", () => {
  const view = play(expecting(play(sent(), SEND)), APPROVE);

  assert.deepEqual(drawn(view).messages, [
    { role: "user", text: TYPED, tools: [] },
    {
      role: "assistant",
      text: "I'll confirm that.",
      tools: [
        { id: "toolu_read", name: "list_tags", input: {}, state: "ran", needs_confirm: false, ok: true, summary: "12 tags", ask: null },
        { id: "toolu_write", name: "decide_tag", input: WRITE_INPUT, state: "approved", needs_confirm: true, ok: true, summary: "Confirmed “Silo”.", ask: ASK },
      ],
    },
    { role: "assistant", text: "Confirmed.", tools: [] },
  ]);
  assert.deepEqual(view.pending, []);
  assert.equal(view.running, false);
  assert.deepEqual(view.ended, { reason: "end", message: null, kind: null });
  assert.equal(view.spent_usd, 0.0028);
});

test("a no is shown as a no, with no call run", () => {
  const view = play(expecting(play(sent(), SEND)), DECLINE);

  const write = view.messages[1].tools[1];
  assert.deepEqual([write.state, write.ok, write.summary], ["declined", false, "The operator declined this."]);
  assert.deepEqual(view.pending, []);
  assert.equal(view.messages.at(-1).text, "Left as it was.");
});

test("answering only some of the waiting calls leaves the rest waiting", () => {
  const two = turn(
    ["start", START_SEND],
    ["tool", { id: "a", name: "decide_tag", input: null, state: "preparing" }],
    ["tool", { id: "b", name: "decide_tag", input: null, state: "preparing" }],
    ["usage", USAGE],
    ["confirm", { id: "a", name: "decide_tag", input: { tag: "Silo" }, summary: "Confirm Silo" }],
    ["confirm", { id: "b", name: "decide_tag", input: { tag: "Barn" }, summary: "Confirm Barn" }],
    ["done", { reason: "awaiting_confirmation", message: null }],
  );
  const one = turn(
    ["start", START_CONFIRM],
    ["tool", { id: "a", name: "decide_tag", input: { tag: "Silo" }, state: "running" }],
    ["tool_result", { id: "a", name: "decide_tag", state: "approved", ok: true, summary: "Done", images: [], artifact: null }],
    ["done", { reason: "awaiting_confirmation", message: null }],
  );

  const view = play(expecting(play(sent(), two)), one);

  assert.deepEqual(view.pending, [
    { id: "b", name: "decide_tag", input: { tag: "Barn" }, summary: "Confirm Barn" },
  ]);
  assert.deepEqual(
    view.messages[1].tools.map((t) => t.state),
    ["approved", "pending"],
  );
});

test("typing a new message instead of answering is a no to what was waiting", () => {
  // The server records that with no event of its own.
  const waiting = play(sent(), SEND);
  const next = turn(
    ["start", { ...START_SEND, message_id: "M3", seq: 3 }],
    ["text", { delta: "Never mind then." }],
    ["usage", USAGE],
    ["done", { reason: "end", message: null }],
  );

  const view = play(withSent(waiting, "Actually, leave it", null), next);

  assert.deepEqual(view.pending, []);
  const write = view.messages[1].tools[1];
  assert.deepEqual([write.state, write.ok], ["declined", false]);
  assert.deepEqual(
    view.messages.map((m) => [m.role, m.text]),
    [
      ["user", TYPED],
      ["assistant", "I'll confirm that."],
      ["user", "Actually, leave it"],
      ["assistant", "Never mind then."],
    ],
  );
});

// --- the reload rule ---------------------------------------------------------

test("a reload mid-turn draws the messages up to where the turn began, then the turn itself", () => {
  // The first stream is half-way: the server has stored Claude's answer
  // (seq 2) and is running a tool. The stored rows and the events say the
  // same things; drawing both would say them twice.
  const midway = stored({
    state: "running",
    last_turn: { reason: "end", message: null },
    run: { active: true, events: 9, seq: 1 },
    messages: [
      message(1, "user", TYPED, { context: CONTEXT }),
      message(2, "assistant", "I'll confirm that.", {
        tools: [
          chip("toolu_read", "list_tags", "running"),
          chip("toolu_write", "decide_tag", "pending", { needs_confirm: true, ask: ASK, input: WRITE_INPUT }),
        ],
      }),
    ],
    pending: [{ id: "toolu_write", name: "decide_tag", input: WRITE_INPUT, summary: ASK }],
  });

  const before = viewOf(midway);
  assert.deepEqual(before.messages.map((m) => m.seq), [1]);
  assert.deepEqual(before.pending, [], "its confirm event brings the waiting call back");
  assert.equal(before.running, true);
  assert.equal(before.stranded, false);
  assert.equal(before.ended, null, "last_turn is the turn before while one runs");
  assert.equal(before.events, 0);

  const after = play(before, SEND);
  assert.deepEqual(drawn(after), drawn(viewOf(AWAITING)));
  assert.equal(after.messages[0].key, "M1");
});

test("a reload before the turn has said where it began trims on its start event", () => {
  const early = stored({
    state: "running",
    run: { active: true, events: 0, seq: null },
    messages: AWAITING.messages,
  });

  const before = viewOf(early);
  assert.deepEqual(before.messages.map((m) => m.seq), [1, 2]);

  const after = play(before, SEND);
  assert.deepEqual(drawn(after), drawn(viewOf(AWAITING)));
});

test("a reload while a yes is being carried out keeps the answer that asked, and its chip moves", () => {
  // The turn began from Claude's answer (seq 2); the approved call's row
  // is `running` from the moment of the yes.
  const midway = stored({
    state: "running",
    run: { active: true, events: 2, seq: 2 },
    messages: [
      AWAITING.messages[0],
      message(2, "assistant", "I'll confirm that.", {
        tools: [
          chip("toolu_read", "list_tags", "ran", { summary: "12 tags" }),
          chip("toolu_write", "decide_tag", "running", { needs_confirm: true, ask: ASK, input: WRITE_INPUT }),
        ],
      }),
      message(4, "assistant", "Confir"),
    ],
  });

  const before = viewOf(midway);
  assert.deepEqual(before.messages.map((m) => m.seq), [1, 2]);
  assert.equal(before.messages[1].tools[1].state, "running", "a live turn's call is really running");

  const after = play(before, APPROVE);
  assert.equal(after.messages[1].tools[1].state, "approved");
  assert.deepEqual(after.messages.map((m) => m.text), [TYPED, "I'll confirm that.", "Confirmed."]);
});

test("a turn picked up a little too early is not drawn twice", () => {
  const half = play(sent(), SEND.slice(0, 6));
  assert.equal(half.events, 6);

  // The connection dropped; the rest is asked for from event 4.
  const view = play(half, SEND.slice(4));

  assert.deepEqual(drawn(view), drawn(play(sent(), SEND)));
  assert.equal(view.messages[1].text, "I'll confirm that.");
  assert.equal(view.messages[1].tools.length, 2);
});

// --- turns that go wrong -------------------------------------------------------

test("a call left running by a turn that is over is shown as interrupted, not as working", () => {
  // An approved call's row is `running` from the yes until it answers; a
  // restart in between leaves it so, and nothing is running it.
  const broken = stored({
    state: "idle",
    last_turn: { reason: "error", message: "FrameFound was stopped while working on this. Ask again." },
    run: { active: false, events: 4, seq: 2 },
    messages: [
      AWAITING.messages[0],
      message(2, "assistant", "I'll confirm that.", {
        tools: [chip("toolu_write", "decide_tag", "running", { needs_confirm: true, ask: ASK })],
      }),
    ],
  });

  const view = viewOf(broken);

  const write = view.messages[1].tools[0];
  assert.deepEqual([write.state, write.ok], ["interrupted", false]);
  assert.deepEqual(view.pending, [], "it is never waiting for a yes again");
  assert.equal(view.running, false);
  assert.equal(view.ended.message, "FrameFound was stopped while working on this. Ask again.");
});

test("a turn the server lost is running with nothing to follow", () => {
  const lost = stored({
    state: "running",
    run: null,
    last_turn: { reason: "end", message: null },
    messages: [
      AWAITING.messages[0],
      message(2, "assistant", "", { tools: [chip("toolu_read", "list_tags", "running")] }),
    ],
  });

  const view = viewOf(lost);

  assert.equal(view.running, true);
  assert.equal(view.stranded, true);
  assert.equal(view.ended, null);
  assert.equal(view.messages[1].tools[0].state, "interrupted");
});

test("a request that fails part-way is billed, then reported: usage comes before error", () => {
  const failed = turn(
    ["start", START_SEND],
    ["text", { delta: "The silo is" }],
    ["tool", { id: "toolu_read", name: "list_tags", input: null, state: "preparing" }],
    ["usage", { ...USAGE, conversation_usd: 0.0009 }],
    ["error", { kind: "api", message: "The connection to Anthropic was lost part-way through an answer." }],
    ["done", { reason: "error", message: "The connection to Anthropic was lost part-way through an answer." }],
  );

  const view = play(sent(), failed);

  assert.equal(view.spent_usd, 0.0009, "what was read is paid for");
  assert.equal(view.running, false);
  assert.deepEqual(view.ended, {
    reason: "error",
    message: "The connection to Anthropic was lost part-way through an answer.",
    kind: "api",
  });
  // What had arrived stays on screen, and the call that was never
  // finished is not left spinning.
  assert.equal(view.messages[1].text, "The silo is");
  assert.equal(view.messages[1].tools[0].state, "interrupted");
});

test("a request turned away before answering has no usage and writes nothing", () => {
  const sentence = "This month's Claude budget of $5.00 has been reached.";
  const refused = turn(
    ["start", START_SEND],
    ["error", { kind: "budget", message: sentence }],
    ["done", { reason: "budget", message: sentence }],
  );

  const view = play(sent(), refused);

  assert.deepEqual(view.messages.map((m) => m.role), ["user"]);
  assert.deepEqual(view.ended, { reason: "budget", message: sentence, kind: "budget" });
  assert.equal(view.spent_usd, 0);
});

test("Stop ends the turn quietly and keeps what was written", () => {
  const stopped = turn(
    ["start", START_SEND],
    ["thinking", { on: true }],
    ["thinking", { on: false }],
    ["text", { delta: "Let me look at" }],
    ["tool", { id: "toolu_read", name: "find_media", input: null, state: "preparing" }],
    ["usage", USAGE],
    ["done", { reason: "stopped", message: "Stopped." }],
  );

  const view = play(sent(), stopped);

  assert.equal(view.running, false);
  assert.equal(view.thinking, false);
  assert.deepEqual(view.ended, { reason: "stopped", message: "Stopped.", kind: null });
  assert.equal(view.messages[1].text, "Let me look at");
  // A call still being written when the answer stopped was never asked
  // for; the server does not keep it, so a reload would not show it.
  assert.deepEqual(view.messages[1].tools, []);
});

test("a stop takes away an answer that was only a call still being written", () => {
  const stopped = turn(
    ["start", START_SEND],
    ["tool", { id: "toolu_read", name: "find_media", input: null, state: "preparing" }],
    ["done", { reason: "stopped", message: "Stopped." }],
  );

  const view = play(sent(), stopped);

  assert.deepEqual(view.messages.map((m) => m.role), ["user"]);
});

test("after a stop, a call that was never started says so even with no `running` before it", () => {
  const stopped = turn(
    ["start", START_SEND],
    ["tool", { id: "a", name: "find_media", input: { q: "silo" }, state: "running" }],
    ["tool", { id: "b", name: "list_tags", input: null, state: "preparing" }],
    ["tool_result", { id: "a", name: "find_media", state: "ran", ok: true, summary: "3 found" }],
    ["tool_result", { id: "b", name: "list_tags", state: "failed", ok: false, summary: "It was not run." }],
    ["done", { reason: "stopped", message: "Stopped." }],
  );

  const view = play(sent(), stopped);

  assert.deepEqual(
    view.messages[1].tools.map((chip) => [chip.id, chip.state, chip.summary]),
    [
      ["a", "ran", "3 found"],
      ["b", "failed", "It was not run."],
    ],
  );
});

test("an error ending still shows a call that was being written as not finished", () => {
  const broken = turn(
    ["start", START_SEND],
    ["tool", { id: "toolu_read", name: "find_media", input: null, state: "preparing" }],
    ["done", { reason: "error", message: "Claude could not be reached." }],
  );

  const view = play(sent(), broken);

  assert.equal(view.messages[1].tools[0].state, "interrupted");
});

test("a stop leaves calls that were waiting for a yes still waiting", () => {
  const stopped = [...SEND.slice(0, -1), { event: "done", index: 11, data: { reason: "stopped", message: "Stopped." } }];

  const view = play(sent(), stopped);

  assert.equal(view.pending.length, 1);
  assert.equal(view.messages[1].tools[1].state, "pending");
});

test("a refused answer's words are taken back and the server's sentence stands", () => {
  const sentence = "Claude declined to answer that.";
  const refusal = turn(
    ["start", START_SEND],
    ["text", { delta: "I can" }],
    ["usage", USAGE],
    ["done", { reason: "refusal", message: sentence }],
  );

  const view = play(sent(), refusal);

  assert.deepEqual(view.messages.map((m) => m.role), ["user"]);
  assert.deepEqual(view.ended, { reason: "refusal", message: sentence, kind: null });
});

test("a refusal after tools ran takes back only the refused answer", () => {
  const refusal = turn(
    ["start", START_SEND],
    ["text", { delta: "Looking." }],
    ["tool", { id: "toolu_read", name: "list_tags", input: null, state: "preparing" }],
    ["usage", USAGE],
    ["tool", { id: "toolu_read", name: "list_tags", input: {}, state: "running" }],
    ["tool_result", { id: "toolu_read", name: "list_tags", state: "ran", ok: true, summary: "12 tags", images: [], artifact: null }],
    ["usage", USAGE],
    ["done", { reason: "refusal", message: "Claude declined to answer that." }],
  );

  const view = play(sent(), refusal);

  assert.deepEqual(view.messages.map((m) => m.text), [TYPED, "Looking."]);
  assert.equal(view.messages[1].tools[0].state, "ran");
});

// --- how answers are put together ---------------------------------------------

test("what Claude says after a tool has answered is a new answer, with its pictures and file on the chip", () => {
  const looked = turn(
    ["start", START_SEND],
    ["text", { delta: "Looking." }],
    ["tool", { id: "t1", name: "view_media", input: null, state: "preparing" }],
    ["usage", USAGE],
    ["tool", { id: "t1", name: "view_media", input: { id: "abc" }, state: "running" }],
    [
      "tool_result",
      {
        id: "t1",
        name: "view_media",
        state: "ran",
        ok: true,
        summary: "1 picture",
        images: [{ blob: "ab12", media_type: "image/jpeg" }],
        artifact: { name: "Ledgerock 60.xml" },
      },
    ],
    ["thinking", { on: true }],
    ["thinking", { on: false }],
    ["text", { delta: "It is a silo." }],
    ["usage", USAGE],
    ["done", { reason: "end", message: null }],
  );

  const view = play(sent(), looked);

  assert.deepEqual(view.messages.map((m) => [m.role, m.text, m.tools.length]), [
    ["user", TYPED, 0],
    ["assistant", "Looking.", 1],
    ["assistant", "It is a silo.", 0],
  ]);
  const seen = view.messages[1].tools[0];
  assert.deepEqual(seen.images, [{ blob: "ab12", media_type: "image/jpeg" }]);
  assert.deepEqual(seen.artifact, { name: "Ledgerock 60.xml" });
  assert.deepEqual(seen.input, { id: "abc" });
  assert.notEqual(view.messages[1].key, view.messages[2].key);
});

test("text either side of a tool call or of more thinking, in one answer, is two paragraphs", () => {
  // The stored answer joins its text blocks with a blank line; the live
  // one must read the same.
  const split = turn(
    ["start", START_SEND],
    ["text", { delta: "First " }],
    ["text", { delta: "part." }],
    ["tool", { id: "t1", name: "list_tags", input: null, state: "preparing" }],
    ["text", { delta: "Second part." }],
    ["thinking", { on: true }],
    ["thinking", { on: false }],
    ["text", { delta: "Third." }],
    ["usage", USAGE],
  );

  const view = play(sent(), split);

  assert.equal(view.messages.length, 2);
  assert.equal(view.messages[1].text, "First part.\n\nSecond part.\n\nThird.");
});

test("an event leaves the messages it did not touch exactly as they were", () => {
  const before = play(sent(), SEND);
  const after = reduce(expecting(before), APPROVE[0]);
  const later = reduce(after, APPROVE[1]);

  assert.equal(later.messages[0], before.messages[0]);
  assert.notEqual(later.messages[1], before.messages[1]);
});

test("an answer to a call nobody saw being asked is shown all the same", () => {
  const view = play(expecting(viewOf(stored())), [
    { event: "tool_result", index: 0, data: { id: "tX", name: "find_media", state: "ran", ok: true, summary: "3 clips", images: [], artifact: null } },
  ]);

  assert.deepEqual(
    view.messages.map((m) => m.tools.map((t) => [t.id, t.state, t.summary])),
    [[["tX", "ran", "3 clips"]]],
  );
});

test("a message the server would not take is taken back, and nothing is left running", () => {
  const waiting = viewOf(AWAITING);

  const view = withoutSent(withSent(waiting, "again", null));

  assert.deepEqual(view.messages, waiting.messages);
  assert.equal(view.running, false);
  assert.equal(view.pending.length, 1, "what was waiting still is: nothing was sent");
});

test("a stored conversation at rest is drawn whole, with how its last turn ended", () => {
  const done = stored({
    spent_usd: 0.0028,
    pictures: false,
    last_turn: { reason: "max_tokens", message: "Claude ran out of room and stopped mid-answer." },
    run: { active: false, events: 6, seq: 2 },
    messages: [...AWAITING.messages, message(4, "assistant", "Confirmed.")],
  });

  const view = viewOf(done);

  assert.deepEqual(view.messages.map((m) => m.seq), [1, 2, 4]);
  assert.equal(view.running, false);
  assert.equal(view.pictures, false);
  assert.equal(view.spent_usd, 0.0028);
  assert.deepEqual(view.ended, {
    reason: "max_tokens",
    message: "Claude ran out of room and stopped mid-answer.",
    kind: null,
  });
  assert.deepEqual(view.messages[0].context, CONTEXT);
});

test("a turn has begun, and is known by where it began, only once it says `start`", () => {
  // Stop is only offered after this, and a dropped turn is only picked up
  // part-way if the server's turn still began where this one did.
  let view = sent();
  assert.deepEqual([view.begun, view.turn], [false, null]);

  view = play(view, SEND.slice(0, 1));
  assert.deepEqual([view.begun, view.turn], [true, 1]);

  const reloaded = viewOf(stored({ state: "running", run: { active: true, events: 3, seq: 2 } }));
  assert.deepEqual([reloaded.begun, reloaded.turn], [true, 2]);
  const asked = expecting(reloaded);
  assert.deepEqual([asked.begun, asked.turn], [false, null], "answering a request starts a new turn");
});
