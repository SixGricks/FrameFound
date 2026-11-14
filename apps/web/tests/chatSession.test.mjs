// Run with `npm test` (Node's own test runner; nothing to install).
//
// The chat panel's store: what happens when a message is sent, when the
// server says no, when the connection drops with Claude mid-answer, when
// the page is reloaded, when Stop is pressed. A turn costs money and goes
// on running whether or not anyone is listening, so the cases here are the
// ones where losing track of it would show: an answer drawn twice, a
// message sent twice, a spinner that never stops, one person's conversation
// left for the next.
//
// No network, no clock, no browser: the store is handed all three.

import assert from "node:assert/strict";
import { test as nodeTest } from "node:test";

import "./ts-imports.mjs";

// A store that loses track of a turn shows up here as a promise that never
// settles, and Node's runner waits on one of those for ever. Nothing in
// this file takes real time, so a test still going after a few seconds is a
// failed one, and says which.
const test = (name, fn) => nodeTest(name, { timeout: 5000 }, fn);

const { NO_STOP, RETRY_MS, STOP_GRACE_MS, UNREACHED, WATCH_MS, createChatStore } = await import(
  "../src/lib/chatSession.ts"
);

// --- a server, a clock and a tab, all pretend ------------------------------------

const refuse = (status, message) => Object.assign(new Error(message), { status });
const NO_SUCH = () => refuse(404, "No such conversation");

/** Let everything that is ready to happen, happen. */
const settle = () => new Promise((resolve) => setImmediate(resolve));

const STATUS = {
  available: true,
  configured: true,
  enabled: true,
  problem: null,
  can_chat: true,
  model: "claude-opus-5-5",
  models: ["claude-opus-5-5"],
  budget_usd: 5,
  spent_this_month_usd: 1,
};

const summary = (id, more = {}) => ({
  id,
  title: "",
  model: "claude-opus-5-5",
  pictures: true,
  state: "idle",
  spent_usd: 0,
  created_at: "2026-11-12T10:00:00+00:00",
  updated_at: "2026-11-12T10:00:00+00:00",
  ...more,
});

const conversation = (id, more = {}) => ({
  ...summary(id),
  input_tokens: 0,
  output_tokens: 0,
  last_turn: null,
  run: null,
  messages: [],
  pending: [],
  ...more,
});

const message = (seq, role, text, tools = []) => ({
  id: `M${seq}`,
  seq,
  role,
  text,
  context: null,
  tools,
  stop_reason: null,
  cost_usd: null,
  created_at: "2026-11-12T10:00:00+00:00",
});

function world({ memory = null, context = { path: "/tags", tag: "silo" } } = {}) {
  const w = {
    calls: [],
    streams: [],
    waits: [],
    memory,
    stored: {},
    list: [],
    status: STATUS,
    failing: {},
    // name -> (args) => a promise to wait on before answering, for a
    // request the test wants to keep in flight.
    hold: {},
    stopLeaves: "idle",
  };
  const called = (name, ...args) => {
    w.calls.push([name, ...args]);
    if (w.failing[name]) throw w.failing[name];
  };
  const stream = (kind) => (id, arg, onEvent, signal) =>
    new Promise((resolve, reject) => {
      const one = {
        kind,
        id,
        arg,
        aborted: false,
        emit: (...events) => events.forEach(onEvent),
        end: (status = 200) => resolve(status),
        fail: reject,
      };
      signal.addEventListener("abort", () => {
        one.aborted = true;
        reject(Object.assign(new Error("This operation was aborted"), { name: "AbortError" }));
      });
      w.streams.push(one);
    });
  w.store = createChatStore({
    status: async () => {
      called("status");
      return typeof w.status === "function" ? w.status() : w.status;
    },
    conversations: async () => {
      called("conversations");
      return typeof w.list === "function" ? w.list() : w.list;
    },
    create: async (pictures) => {
      called("create", pictures);
      if (w.hold.create) await w.hold.create(pictures);
      w.stored.C = conversation("C", { pictures });
      return w.stored.C;
    },
    read: async (id) => {
      called("read", id);
      const found = w.stored[id];
      if (!found) throw NO_SUCH();
      return typeof found === "function" ? found() : found;
    },
    setPictures: async (id, pictures) => {
      called("setPictures", id, pictures);
      if (w.hold.setPictures) await w.hold.setPictures(pictures);
      return summary(id, { pictures });
    },
    remove: async (id) => {
      called("remove", id);
    },
    stop: async (id) => {
      called("stop", id);
      // What the route answers after its short wait: usually the turn has
      // ended; `running` when a tool that had started is finishing.
      return summary(id, { state: w.stopLeaves });
    },
    send: stream("send"),
    answer: stream("answer"),
    follow: stream("follow"),
    context: () => context,
    wait: (ms) => new Promise((resolve) => w.waits.push({ ms, resolve })),
    recall: () => w.memory,
    remember: (value) => {
      w.memory = value;
    },
  });
  /** Let the oldest wait run out; returns how long it was for. */
  w.tick = async () => {
    const wait = w.waits.shift();
    assert.ok(wait, "nothing was waiting");
    wait.resolve();
    await settle();
    return wait.ms;
  };
  w.named = (name) => w.calls.filter(([called]) => called === name);
  /** The server still holds the turn begun from message `seq` of "C". */
  w.holding = (seq = 1, more = {}) => {
    w.stored.C = conversation("C", { state: "running", run: { active: true, events: 4, seq }, ...more });
  };
  w.remembered = () => JSON.parse(w.memory);
  return w;
}

/** The panel was left open in this tab: signing in asks for the status and
 *  the list, as opening it would. */
const OPEN = JSON.stringify({ user: "u1", open: true, conversation: null, draft: "" });

async function signedIn(options = {}) {
  const w = world({ memory: OPEN, ...options });
  w.store.signIn("u1");
  await settle();
  return w;
}

const turn = (...pairs) => pairs.map(([event, data], index) => ({ event, index, data }));

const START = { conversation_id: "C", model: "claude-opus-5-5", pictures: true, message_id: "M1", seq: 1 };
const USAGE = { cost_usd: 0.25, conversation_usd: 0.25, month_usd: 1.25, budget_usd: 5 };
const ANSWER = turn(
  ["start", START],
  ["thinking", { on: true }],
  ["thinking", { on: false }],
  ["text", { delta: "It is " }],
  ["text", { delta: "a silo." }],
  ["usage", USAGE],
  ["done", { reason: "end", message: null }],
);

const texts = (w) => w.store.get().view.messages.map((one) => [one.role, one.text]);

// --- signing in ---------------------------------------------------------------

test("nothing is asked of the server until someone is signed in", async () => {
  // The panel is mounted on the sign-in page too. A request from there
  // would be answered 401 and bounce the page to itself.
  const w = world();

  w.store.setOpen(true);
  await w.store.refreshStatus();
  await w.store.refreshList();
  await w.store.openConversation("C");
  w.store.newConversation();
  assert.equal(await w.store.send("hello"), false);
  assert.equal(await w.store.confirm([{ tool_use_id: "t", approve: true }]), false);
  await w.store.stop();
  assert.equal(await w.store.remove("C"), false);
  await w.store.setPictures(false);
  await settle();

  assert.deepEqual(w.calls, []);
  assert.deepEqual(w.streams, []);
  assert.equal(w.memory, null);
});

test("signing in with the panel left open loads the status and the list once, however often it is said", async () => {
  const w = world({ memory: OPEN });
  w.list = [summary("A", { title: "Find the silo" })];
  let told = 0;
  w.store.subscribe(() => {
    told += 1;
  });

  w.store.signIn("u1");
  w.store.signIn("u1");
  await settle();
  w.store.signIn("u1");
  await settle();

  assert.deepEqual(w.calls, [["status"], ["conversations"]]);
  const now = w.store.get();
  assert.equal(now.status, STATUS);
  assert.deepEqual(now.conversations.map((one) => one.title), ["Find the silo"]);
  assert.ok(told >= 1);
  assert.equal(w.store.get(), now, "the same snapshot until something changes");
});

test("a status or list that cannot be read says so without hiding the other", async () => {
  const w = world({ memory: OPEN });
  w.failing.status = new TypeError("Failed to fetch");

  w.store.signIn("u1");
  await settle();

  assert.deepEqual(w.store.get().problem, { where: "status", message: UNREACHED });
  assert.deepEqual(w.store.get().conversations, []);

  delete w.failing.status;
  await w.store.refreshStatus();
  assert.equal(w.store.get().problem, null);
});

test("signing in with the panel closed asks nothing about chat until it is opened", async () => {
  // Every page load signs in. Asking whether chat can be used is not free
  // on the server (the first time, it loads the Claude libraries), and an
  // install where nobody opens the panel must not pay for it on every tab.
  const w = world();

  w.store.signIn("u1");
  await settle();
  assert.deepEqual(w.calls, []);
  assert.equal(w.store.get().status, null);

  w.store.setOpen(true);
  await settle();
  assert.deepEqual(w.calls, [["status"], ["conversations"]]);
});

test("a reload with the panel closed still rejoins the turn that is running", async () => {
  const w = world({ memory: JSON.stringify({ user: "u1", open: false, conversation: "C", draft: "" }) });
  w.holding(1, { messages: [message(1, "user", "Is this a silo?")] });

  w.store.signIn("u1");
  await settle();

  assert.deepEqual(w.calls, [["read", "C"]]);
  assert.deepEqual([w.streams[0].kind, w.streams[0].arg], ["follow", 0]);
});

// --- sending ------------------------------------------------------------------

test("a first message creates the conversation, is drawn at once, and is read to its end", async () => {
  const w = await signedIn();
  w.store.setDraft("  Is this a silo?  ");
  w.list = [summary("C", { title: "Is this a silo?" })];

  const sending = w.store.send(w.store.get().draft);
  await settle();

  // Drawn before the server has said a word, and out of the box.
  let now = w.store.get();
  assert.deepEqual(texts(w), [["user", "Is this a silo?"]]);
  assert.equal(now.view.messages[0].sending, true);
  assert.equal(now.draft, "");
  assert.equal(now.view.running, true);
  assert.equal(now.link, "live");
  assert.deepEqual(w.named("create"), [["create", true]]);
  const [stream] = w.streams;
  assert.deepEqual([stream.kind, stream.id], ["send", "C"]);
  // With where the reader is, as it was when they pressed send.
  assert.deepEqual(stream.arg, { text: "Is this a silo?", context: { path: "/tags", tag: "silo" } });

  stream.emit(...ANSWER.slice(0, 5));
  now = w.store.get();
  assert.deepEqual(texts(w), [
    ["user", "Is this a silo?"],
    ["assistant", "It is a silo."],
  ]);
  assert.equal(now.view.messages[0].sending, false);

  stream.emit(...ANSWER.slice(5));
  assert.equal(await sending, true);

  now = w.store.get();
  assert.equal(now.view.running, false);
  assert.equal(now.link, "idle");
  assert.equal(now.view.spent_usd, 0.25);
  assert.equal(now.status.spent_this_month_usd, 1.25, "the month's total is what the turn said it is");
  // The list has the new conversation, under the title the server gave it.
  assert.deepEqual(now.conversations.map((one) => one.title), ["Is this a silo?"]);
  assert.equal(w.remembered().conversation, "C");
});

test("the end of a turn is its `done`, not the connection closing", async () => {
  // A proxy that holds the connection open must not hold the panel.
  const w = await signedIn();
  const sending = w.store.send("hello");
  await settle();

  w.streams[0].emit(...ANSWER);

  assert.equal(await sending, true);
  assert.equal(w.streams[0].aborted, true);
  assert.equal(w.store.get().link, "idle");
});

test("a message can be sent with no page context, when the reader took it off", async () => {
  const w = await signedIn();

  void w.store.send("hello", { context: null });
  await settle();

  assert.deepEqual(w.streams[0].arg, { text: "hello", context: null });
});

test("a message the server will not take goes back in the box, with the server's reason", async () => {
  const w = await signedIn();
  w.store.setDraft("x".repeat(10));

  const sending = w.store.send(w.store.get().draft);
  await settle();
  w.streams[0].fail(refuse(400, "That message is too long (30,000 characters; the limit is 20,000)."));

  assert.equal(await sending, false);
  const now = w.store.get();
  assert.deepEqual(texts(w), []);
  assert.equal(now.draft, "x".repeat(10));
  assert.equal(now.view.running, false);
  assert.equal(now.link, "idle");
  assert.deepEqual(now.problem, {
    where: "send",
    message: "That message is too long (30,000 characters; the limit is 20,000).",
  });
  assert.equal(w.waits.length, 0, "a refusal is not retried");
});

test("text typed while the refused message was on its way is not overwritten", async () => {
  const w = await signedIn();

  const sending = w.store.send("first thought");
  await settle();
  w.store.setDraft("second thought");
  w.streams[0].fail(refuse(503, "The chat panel is not installed on this server."));
  await sending;

  assert.equal(w.store.get().draft, "second thought");
});

test("a conversation that cannot be created takes the message back too", async () => {
  const w = await signedIn();
  w.failing.create = refuse(403, "This account is read-only");

  assert.equal(await w.store.send("hello"), false);

  assert.deepEqual(w.store.get().problem, { where: "send", message: "This account is read-only" });
  assert.equal(w.store.get().draft, "hello");
  assert.equal(w.store.get().view.id, null);
  assert.deepEqual(w.streams, []);
});

test("nothing is sent while a turn runs, and an empty message is not a message", async () => {
  const w = await signedIn();
  assert.equal(await w.store.send("   "), false);

  void w.store.send("one");
  await settle();
  assert.equal(await w.store.send("two"), false);

  assert.equal(w.streams.length, 1);
  assert.deepEqual(texts(w), [["user", "one"]]);
});

test("told Claude is still working, the store goes and follows that turn", async () => {
  // Another tab started it, or this one lost track of it.
  const w = await signedIn();
  w.stored.A = conversation("A", { messages: [message(1, "user", "earlier")] });
  await w.store.openConversation("A");

  const sending = w.store.send("and another thing");
  await settle();
  w.stored.A = conversation("A", {
    state: "running",
    run: { active: true, events: 3, seq: 3 },
    messages: [message(1, "user", "earlier"), message(3, "user", "from the other tab")],
  });
  w.streams[0].fail(refuse(409, "Claude is still working on the last message."));
  assert.equal(await sending, false);

  const now = w.store.get();
  assert.equal(now.problem.message, "Claude is still working on the last message.");
  assert.equal(now.draft, "and another thing");
  assert.deepEqual(texts(w), [
    ["user", "earlier"],
    ["user", "from the other tab"],
  ]);
  assert.equal(now.view.running, true);
  const follow = w.streams[1];
  assert.deepEqual([follow.kind, follow.id, follow.arg], ["follow", "A", 0]);
});

test("no answer to a send is not taken to mean the message did not arrive", async () => {
  // The request went out and the connection died before the reply. Putting
  // the text back for the reader to send again would send it twice.
  const w = await signedIn();
  w.stored.A = conversation("A", { messages: [message(1, "user", "earlier"), message(2, "assistant", "Yes.")] });
  await w.store.openConversation("A");

  const sending = w.store.send("and another thing");
  await settle();
  w.stored.A = conversation("A", {
    state: "running",
    run: { active: true, events: 1, seq: 3 },
    messages: [...w.stored.A.messages, message(3, "user", "and another thing")],
  });
  w.streams[0].fail(new TypeError("Failed to fetch"));

  assert.equal(await sending, true);
  const now = w.store.get();
  assert.equal(now.draft, "");
  assert.equal(now.problem, null);
  assert.deepEqual(texts(w).at(-1), ["user", "and another thing"]);
  assert.equal(w.streams[1].kind, "follow");
});

test("no answer to a send that truly did not arrive puts the text back and says so", async () => {
  const w = await signedIn();
  w.stored.A = conversation("A", { messages: [message(1, "user", "earlier")] });
  await w.store.openConversation("A");

  const sending = w.store.send("and another thing");
  await settle();
  w.streams[0].fail(new TypeError("Failed to fetch"));

  assert.equal(await sending, false);
  const now = w.store.get();
  assert.equal(now.draft, "and another thing");
  assert.deepEqual(now.problem, { where: "send", message: UNREACHED });
  assert.deepEqual(texts(w), [["user", "earlier"]]);
  assert.equal(now.view.running, false);
});

test("leaving while a first message's conversation is being made puts the message back in the box", async () => {
  const w = await signedIn();
  let made;
  w.hold.create = () => new Promise((resolve) => (made = resolve));
  w.store.setDraft("A long question about the silo");
  const sending = w.store.send(w.store.get().draft);
  await settle();
  assert.equal(w.store.get().draft, "");

  w.store.newConversation();
  made();

  assert.equal(await sending, false);
  const now = w.store.get();
  assert.equal(now.draft, "A long question about the silo", "the words are not lost");
  assert.equal(now.problem.where, "send");
  assert.match(now.problem.message, /not sent/);
  assert.deepEqual(w.streams, [], "nothing is sent into the conversation that was left");
  assert.deepEqual(now.conversations.map((one) => one.id), ["C"], "the conversation made is listed, not hidden");
  assert.equal(now.view.id, null);
});

test("leaving while a message is on its way does not cut it off", async () => {
  // On a slow link to the NAS, "New" pressed a second after Enter used to
  // abort the request carrying the message: neither sent nor on screen.
  const w = await signedIn();
  w.stored.B = conversation("B", { title: "Other" });
  w.store.setDraft("Is this a silo?");
  const sending = w.store.send("Is this a silo?");
  await settle();
  const post = w.streams[0];

  await w.store.openConversation("B");
  assert.equal(post.aborted, false, "the request carrying the message is left to arrive");

  post.emit(ANSWER[0]);
  await settle();
  assert.equal(post.aborted, true, "once the server has it, this tab stops reading it");
  assert.equal(await sending, true);
  const now = w.store.get();
  assert.equal(now.view.id, "B");
  assert.deepEqual(texts(w), []);
  assert.equal(now.draft, "", "it was sent, so it is not offered again");
});

test("a message refused after its conversation was left goes back in the box", async () => {
  const w = await signedIn();
  const sending = w.store.send("Is this a silo?");
  await settle();

  w.store.newConversation();
  w.streams[0].fail(refuse(400, "That message is too long."));

  assert.equal(await sending, false);
  const now = w.store.get();
  assert.equal(now.draft, "Is this a silo?");
  assert.match(now.problem.message, /too long/);
});

// --- a connection that drops -----------------------------------------------------

test("a dropped connection is picked up from the event it had reached, and nothing is drawn twice", async () => {
  const w = await signedIn();
  const sending = w.store.send("Is this a silo?");
  await settle();

  w.streams[0].emit(...ANSWER.slice(0, 4));
  w.streams[0].end();
  await settle();
  w.holding(1);

  // The turn carries on without us. Say so, wait a moment, ask for the rest.
  assert.equal(w.store.get().link, "reconnecting");
  assert.equal(w.store.get().view.running, true);
  assert.equal(await w.tick(), RETRY_MS[0]);
  const follow = w.streams[1];
  assert.deepEqual([follow.kind, follow.id, follow.arg], ["follow", "C", 4]);

  // The server is generous and starts one early.
  follow.emit(...ANSWER.slice(3));
  assert.equal(await sending, true);

  assert.deepEqual(texts(w), [
    ["user", "Is this a silo?"],
    ["assistant", "It is a silo."],
  ]);
  assert.equal(w.store.get().link, "idle");
});

test("a turn picked up after a drop is carried on only if it is still the same turn", async () => {
  // While this tab was away the turn ended, and the next one was started
  // from a phone. Its events from 4 on, added to this half-drawn answer,
  // would be a transcript of nothing that happened.
  const w = await signedIn();
  const sending = w.store.send("Is this a silo?");
  await settle();
  w.streams[0].emit(...ANSWER.slice(0, 4));
  w.streams[0].end();
  await settle();
  w.holding(3, {
    messages: [
      message(1, "user", "Is this a silo?"),
      message(2, "assistant", "It is a silo."),
      message(3, "user", "And the barn?"),
    ],
  });

  await w.tick();

  const follow = w.streams[1];
  assert.deepEqual([follow.kind, follow.arg], ["follow", 0], "the turn now running, from its start");
  assert.deepEqual(texts(w), [
    ["user", "Is this a silo?"],
    ["assistant", "It is a silo."],
    ["user", "And the barn?"],
  ]);
  follow.emit(
    ...turn(
      ["start", { ...START, message_id: "M3", seq: 3 }],
      ["text", { delta: "A barn." }],
      ["usage", USAGE],
      ["done", { reason: "end", message: null }],
    ),
  );
  assert.equal(await sending, true);
  assert.deepEqual(texts(w).at(-1), ["assistant", "A barn."]);
  assert.equal(w.store.get().view.running, false);
});

test("while the server cannot be reached the store keeps trying, less and less often", async () => {
  const w = await signedIn();
  void w.store.send("hello");
  await settle();
  w.streams[0].emit(...ANSWER.slice(0, 2));
  w.streams[0].end();
  await settle();
  w.holding(1);

  const waited = [await w.tick()];
  for (let attempt = 1; attempt <= RETRY_MS.length + 1; attempt++) {
    w.streams.at(-1).fail(attempt % 2 ? new TypeError("Failed to fetch") : refuse(502, "Bad Gateway"));
    await settle();
    assert.equal(w.store.get().link, "reconnecting");
    waited.push(await w.tick());
  }

  assert.deepEqual(waited, [...RETRY_MS, RETRY_MS.at(-1), RETRY_MS.at(-1)]);
  assert.equal(w.store.get().view.running, true);
  assert.ok(w.streams.slice(1).every((one) => one.kind === "follow" && one.arg === 2));
});

test("when the server no longer holds the turn, what it stored is what is drawn", async () => {
  // It ended more than ten minutes ago, or went with a restart.
  const w = await signedIn();
  void w.store.send("Is this a silo?");
  await settle();
  w.streams[0].emit(...ANSWER.slice(0, 4));
  w.streams[0].end();
  await settle();
  w.holding(1);
  await w.tick();

  w.stored.C = conversation("C", {
    last_turn: { reason: "error", message: "FrameFound was stopped while working on this. Ask again." },
    messages: [message(1, "user", "Is this a silo?")],
  });
  w.streams[1].end(204);
  await settle();

  const now = w.store.get();
  assert.deepEqual(texts(w), [["user", "Is this a silo?"]]);
  assert.equal(now.view.running, false);
  assert.equal(now.link, "idle");
  assert.equal(now.view.ended.message, "FrameFound was stopped while working on this. Ask again.");
});

// --- reload, and other conversations ---------------------------------------------

test("a reload comes back to the conversation and joins the turn that is still running", async () => {
  const w = world({
    memory: JSON.stringify({ user: "u1", open: true, conversation: "C", draft: "half a thought" }),
  });
  w.stored.C = conversation("C", {
    state: "running",
    run: { active: true, events: 4, seq: 1 },
    messages: [message(1, "user", "Is this a silo?"), message(2, "assistant", "It is")],
  });

  w.store.signIn("u1");
  assert.equal(w.remembered().conversation, "C", "the tab's memory is not wiped while it reopens");
  await settle();

  let now = w.store.get();
  assert.equal(now.open, true);
  assert.equal(now.draft, "half a thought");
  assert.equal(now.opening, false);
  // Up to where the turn began; the turn's own events say the rest.
  assert.deepEqual(texts(w), [["user", "Is this a silo?"]]);
  const follow = w.streams[0];
  assert.deepEqual([follow.kind, follow.id, follow.arg], ["follow", "C", 0]);

  follow.emit(...ANSWER);
  await settle();

  now = w.store.get();
  assert.deepEqual(texts(w), [
    ["user", "Is this a silo?"],
    ["assistant", "It is a silo."],
  ]);
  assert.equal(now.view.messages[0].key, "M1");
  assert.equal(now.view.running, false);
});

test("what one person left in a tab is not handed to the next", async () => {
  const w = world({
    memory: JSON.stringify({ user: "someone-else", open: true, conversation: "X", draft: "private" }),
  });

  w.store.signIn("u1");
  await settle();

  const now = w.store.get();
  assert.equal(now.draft, "");
  assert.equal(now.open, false);
  assert.equal(now.view.id, null);
  assert.deepEqual(w.named("read"), []);
  assert.equal(w.remembered().user, "u1");
});

test("a remembered conversation that has since been deleted is forgotten, not an error loop", async () => {
  const w = world({ memory: JSON.stringify({ user: "u1", open: true, conversation: "gone", draft: "" }) });
  w.list = [summary("gone"), summary("A")];

  w.store.signIn("u1");
  await settle();

  const now = w.store.get();
  assert.equal(now.view.id, null);
  assert.equal(now.opening, false);
  assert.deepEqual(now.problem, { where: "open", message: "No such conversation" });
  assert.deepEqual(now.conversations.map((one) => one.id), ["A"]);
  assert.equal(w.remembered().conversation, null);
});

test("garbage in the tab's memory is nothing remembered", async () => {
  const w = world({ memory: "{not json" });

  w.store.signIn("u1");
  await settle();

  assert.equal(w.store.get().view.id, null);
  assert.equal(w.store.get().problem, null);
});

test("leaving for another conversation stops listening to the first, and its late events change nothing", async () => {
  const w = await signedIn();
  w.stored.B = conversation("B", { title: "Other", messages: [message(1, "user", "an older question")] });
  void w.store.send("hello");
  await settle();
  const first = w.streams[0];
  first.emit(...ANSWER.slice(0, 4));

  await w.store.openConversation("B");
  first.emit(...ANSWER.slice(4));
  await settle();

  assert.equal(first.aborted, true);
  const now = w.store.get();
  assert.equal(now.view.id, "B");
  assert.deepEqual(texts(w), [["user", "an older question"]]);
  assert.equal(now.view.running, false);
  assert.equal(w.waits.length, 0, "and nobody is trying to reconnect to it");
});

test("an answer for a conversation since left does not replace the one now open", async () => {
  const w = await signedIn();
  let release;
  w.stored.A = () => new Promise((resolve) => (release = () => resolve(conversation("A", { title: "slow" }))));
  w.stored.B = conversation("B", { title: "fast" });

  const slow = w.store.openConversation("A");
  await settle();
  await w.store.openConversation("B");
  release();
  await slow;

  assert.equal(w.store.get().view.title, "fast");
});

test("a turn the server lost is waited out, then drawn as it was stored", async () => {
  // `running` with no turn to follow: it died with a restart. Sending is
  // refused until the server frees it, so keep asking.
  const w = await signedIn();
  w.stored.A = conversation("A", { state: "running", run: null, messages: [message(1, "user", "hello")] });

  await w.store.openConversation("A");

  let now = w.store.get();
  assert.equal(now.view.stranded, true);
  assert.equal(now.link, "watching");
  assert.deepEqual(w.streams, [], "there is nothing to listen to");
  assert.equal(await w.store.send("again"), false);

  assert.equal(await w.tick(), WATCH_MS);
  assert.equal(w.store.get().link, "watching");

  w.stored.A = conversation("A", {
    last_turn: { reason: "error", message: "FrameFound was stopped while working on this. Ask again." },
    messages: [message(1, "user", "hello")],
  });
  await w.tick();

  now = w.store.get();
  assert.equal(now.view.running, false);
  assert.equal(now.link, "idle");
  assert.equal(now.view.ended.reason, "error");
  assert.equal(w.waits.length, 0);
});

// --- yes and no ---------------------------------------------------------------

const ASKING = () =>
  conversation("A", {
    state: "awaiting_confirmation",
    messages: [
      message(1, "user", "It is a silo"),
      message(2, "assistant", "I'll confirm that.", [
        {
          id: "toolu_write",
          name: "decide_tag",
          input: { tag: "Silo" },
          state: "pending",
          needs_confirm: true,
          ok: false,
          summary: "",
          ask: "Confirm the tag “Silo”",
          images: [],
          artifact: null,
        },
      ]),
    ],
    pending: [{ id: "toolu_write", name: "decide_tag", input: { tag: "Silo" }, summary: "Confirm the tag “Silo”" }],
  });

test("a yes is sent and the same turn is read on to its end", async () => {
  const w = await signedIn();
  w.stored.A = ASKING();
  await w.store.openConversation("A");
  assert.equal(w.store.get().view.pending.length, 1);

  const answering = w.store.confirm([{ tool_use_id: "toolu_write", approve: true }]);
  await settle();
  assert.equal(w.store.get().view.running, true);
  const stream = w.streams[0];
  assert.deepEqual([stream.kind, stream.id, stream.arg], [
    "answer",
    "A",
    [{ tool_use_id: "toolu_write", approve: true }],
  ]);

  stream.emit(
    ...turn(
      ["start", { ...START, conversation_id: "A", message_id: null, seq: 2 }],
      ["tool", { id: "toolu_write", name: "decide_tag", input: { tag: "Silo" }, state: "running" }],
      ["tool_result", { id: "toolu_write", name: "decide_tag", state: "approved", ok: true, summary: "Done", images: [], artifact: null }],
      ["text", { delta: "Confirmed." }],
      ["usage", USAGE],
      ["done", { reason: "end", message: null }],
    ),
  );

  assert.equal(await answering, true);
  const now = w.store.get();
  assert.deepEqual(now.view.pending, []);
  assert.equal(now.view.messages[1].tools[0].state, "approved");
  assert.deepEqual(texts(w).at(-1), ["assistant", "Confirmed."]);
});

test("a yes to something no longer waiting says so and shows what is waiting now", async () => {
  // Answered from another tab a moment ago.
  const w = await signedIn();
  w.stored.A = ASKING();
  await w.store.openConversation("A");

  const answering = w.store.confirm([{ tool_use_id: "toolu_write", approve: true }]);
  await settle();
  w.stored.A = conversation("A", {
    messages: [...ASKING().messages, message(4, "assistant", "Confirmed.")],
  });
  w.stored.A.messages[1].tools[0].state = "approved";
  w.streams[0].fail(refuse(400, "That request is no longer waiting for an answer."));

  assert.equal(await answering, false);
  const now = w.store.get();
  assert.deepEqual(now.problem, {
    where: "confirm",
    message: "That request is no longer waiting for an answer.",
  });
  assert.deepEqual(now.view.pending, []);
  assert.equal(now.view.running, false);
  assert.deepEqual(texts(w).at(-1), ["assistant", "Confirmed."]);
});

// --- Stop ---------------------------------------------------------------------

test("Stop asks the server to stop and goes on listening until the turn says it has", async () => {
  // Closing the stream stops nothing and saves nothing.
  const w = await signedIn();
  const sending = w.store.send("hello");
  await settle();
  w.streams[0].emit(...ANSWER.slice(0, 4));

  const stopping = w.store.stop();
  await settle();

  assert.deepEqual(w.named("stop"), [["stop", "C"]]);
  assert.equal(w.store.get().stopping, true);
  assert.equal(w.streams[0].aborted, false);
  assert.equal(w.store.get().view.running, true);
  await w.store.stop();
  assert.equal(w.named("stop").length, 1, "pressing twice asks once");

  // The server keeps a stopped answer as far as it got.
  w.stored.C = conversation("C", {
    last_turn: { reason: "stopped", message: "Stopped." },
    messages: [message(1, "user", "hello"), message(2, "assistant", "It is ")],
  });
  w.streams[0].emit(
    { event: "usage", index: 4, data: USAGE },
    { event: "done", index: 5, data: { reason: "stopped", message: "Stopped." } },
  );
  await sending;
  await settle();

  const now = w.store.get();
  assert.equal(now.stopping, false);
  assert.equal(now.view.running, false);
  assert.deepEqual(now.view.ended, { reason: "stopped", message: "Stopped.", kind: null });
  assert.equal(now.problem, null);
  // Drawn from what was stored, so a reload shows the same words.
  assert.deepEqual(w.named("read"), [["read", "C"]]);
  assert.deepEqual(texts(w).at(-1), ["assistant", "It is "], "what was written stays");
  assert.equal(now.view.messages.at(-1).id, "M2");

  // The wait for the turn's own ending runs out and finds it ended.
  assert.equal(await w.tick(), STOP_GRACE_MS);
  await stopping;
  assert.equal(w.named("read").length, 1, "nothing more to ask");
});

test("Stop before the turn has started asks nothing and cuts nothing", async () => {
  // Until `start` there is nothing on the server to stop: the route says
  // "nothing running", the turn starts a moment later and runs to the end,
  // and the re-read after the grace used to abort the message itself.
  const w = await signedIn();
  const sending = w.store.send("hello");
  await settle();
  assert.equal(w.store.get().view.begun, false);

  await w.store.stop();
  assert.deepEqual(w.named("stop"), []);
  assert.equal(w.store.get().stopping, false);
  assert.equal(w.streams[0].aborted, false);
  assert.deepEqual(w.waits, []);

  w.streams[0].emit(ANSWER[0]);
  assert.equal(w.store.get().view.begun, true, "and once it has, it can be stopped");
  void w.store.stop();
  await settle();
  assert.deepEqual(w.named("stop"), [["stop", "C"]]);

  w.streams[0].emit(...ANSWER.slice(1));
  await sending;
});

test("a Stop the server answers with the turn still running keeps listening until it ends", async () => {
  // A tool that had started is left to finish, for up to three minutes.
  // The answer to Stop is not the end of the turn, and asking for the
  // stored conversation meanwhile would cut the stream and draw the turn
  // afresh as if Stop had never been pressed.
  const w = await signedIn();
  w.stopLeaves = "running";
  const sending = w.store.send("hello");
  await settle();
  w.streams[0].emit(
    ...ANSWER.slice(0, 3),
    { event: "tool", index: 3, data: { id: "t1", name: "find_media", input: { q: "silo" }, state: "running" } },
    { event: "tool", index: 4, data: { id: "t2", name: "list_tags", input: null, state: "preparing" } },
  );

  await w.store.stop();
  await settle();

  let now = w.store.get();
  assert.deepEqual(w.waits, [], "no wait that would end in a re-read");
  assert.deepEqual(w.named("read"), []);
  assert.equal(w.streams[0].aborted, false);
  assert.equal(now.stopping, true, "Stopping… stays up");
  assert.equal(now.view.running, true);

  // The started tool finishes; the one never started is said not to have
  // run, with no `tool {running}` before it.
  w.stored.C = conversation("C", {
    last_turn: { reason: "stopped", message: "Stopped." },
    messages: [
      message(1, "user", "hello"),
      message(2, "assistant", "", [
        { id: "t1", name: "find_media", input: { q: "silo" }, state: "ran", needs_confirm: false, ok: true, summary: "3 found", ask: null, images: [], artifact: null },
      ]),
    ],
  });
  w.streams[0].emit(
    { event: "tool_result", index: 5, data: { id: "t1", name: "find_media", state: "ran", ok: true, summary: "3 found" } },
    {
      event: "tool_result",
      index: 6,
      data: { id: "t2", name: "list_tags", state: "failed", ok: false, summary: "The operator stopped the answer before this was started. It was not run." },
    },
  );
  assert.equal(w.store.get().stopping, true);
  w.streams[0].emit({ event: "done", index: 7, data: { reason: "stopped", message: "Stopped." } });
  await sending;
  await settle();

  now = w.store.get();
  assert.equal(now.stopping, false);
  assert.equal(now.view.running, false);
  assert.equal(now.view.ended.reason, "stopped");
  assert.deepEqual(
    now.view.messages.at(-1).tools.map((chip) => [chip.id, chip.state]),
    [["t1", "ran"]],
  );
});

test("a Stop whose ending never arrives asks what became of the turn", async () => {
  // The stream had already been lost when Stop was pressed.
  const w = await signedIn();
  void w.store.send("hello");
  await settle();
  w.streams[0].emit(...ANSWER.slice(0, 4));

  const stopping = w.store.stop();
  await settle();
  w.stored.C = conversation("C", {
    last_turn: { reason: "stopped", message: "Stopped." },
    messages: [message(1, "user", "hello")],
  });
  await w.tick();
  await stopping;

  const now = w.store.get();
  assert.equal(w.streams[0].aborted, true);
  assert.equal(now.view.running, false);
  assert.equal(now.stopping, false);
  assert.equal(now.view.ended.reason, "stopped");
});

for (const [status, body] of [
  [404, "Not Found"],
  [405, "Method Not Allowed"],
]) {
  test(`on a server with no Stop (${status}) the panel says so, stops listening and waits for the stored answer`, async () => {
    const w = await signedIn();
    void w.store.send("hello");
    await settle();
    w.streams[0].emit(...ANSWER.slice(0, 4));
    w.failing.stop = refuse(status, body);

    await w.store.stop();

    let now = w.store.get();
    assert.deepEqual(now.problem, { where: "stop", message: NO_STOP });
    assert.equal(now.canStop, false);
    assert.equal(now.stopping, false);
    assert.equal(w.streams[0].aborted, true);
    assert.equal(now.link, "watching");
    // It has not stopped, and the panel does not pretend it has.
    assert.equal(now.view.running, true);

    // Still running, and the server could replay it: not listened to again.
    w.stored.C = conversation("C", { state: "running", run: { active: true, events: 6, seq: 1 } });
    await w.tick();
    assert.equal(w.streams.length, 1);
    assert.deepEqual(texts(w).at(-1), ["assistant", "It is "], "what was on screen stays");

    w.stored.C = conversation("C", {
      last_turn: { reason: "end", message: null },
      messages: [message(1, "user", "hello"), message(2, "assistant", "It is a silo.")],
    });
    await w.tick();

    now = w.store.get();
    assert.equal(now.view.running, false);
    assert.equal(now.link, "idle");
    assert.deepEqual(texts(w).at(-1), ["assistant", "It is a silo."]);
  });
}

test("a Stop that is refused for a real reason says the reason and keeps listening", async () => {
  const w = await signedIn();
  void w.store.send("hello");
  await settle();
  w.streams[0].emit(...ANSWER.slice(0, 4));
  w.failing.stop = refuse(403, "This account is read-only");

  await w.store.stop();

  const now = w.store.get();
  assert.deepEqual(now.problem, { where: "stop", message: "This account is read-only" });
  assert.equal(now.canStop, true);
  assert.equal(now.stopping, false);
  assert.equal(w.streams[0].aborted, false);
  assert.equal(now.link, "live");
});

test("Stop with nothing running asks nothing", async () => {
  const w = await signedIn();

  await w.store.stop();

  assert.deepEqual(w.named("stop"), []);
});

// --- the rest of the panel -----------------------------------------------------

test("a conversation Claude is still working in is not deleted, and the reason is the server's", async () => {
  const w = await signedIn();
  w.list = [summary("A"), summary("B")];
  await w.store.refreshList();
  w.failing.remove = refuse(409, "Claude is still working in this conversation. Delete it when it has finished.");

  assert.equal(await w.store.remove("A"), false);

  assert.deepEqual(w.store.get().problem, {
    where: "delete",
    message: "Claude is still working in this conversation. Delete it when it has finished.",
  });
  assert.equal(w.store.get().conversations.length, 2);
});

test("deleting the open conversation leaves a new, empty one and stops listening to it", async () => {
  const w = await signedIn();
  w.list = [summary("A"), summary("B")];
  await w.store.refreshList();
  w.stored.A = conversation("A", { pictures: false, messages: [message(1, "user", "hello")] });
  await w.store.openConversation("A");

  assert.equal(await w.store.remove("A"), true);

  const now = w.store.get();
  assert.deepEqual(now.conversations.map((one) => one.id), ["B"]);
  assert.equal(now.view.id, null);
  assert.deepEqual(now.view.messages, []);
  assert.equal(w.remembered().conversation, null);
  // Deleting another one leaves the open one alone.
  w.stored.B = conversation("B");
  await w.store.openConversation("B");
  assert.equal(await w.store.remove("Z"), true);
  assert.equal(w.store.get().view.id, "B");
});

test("the pictures choice made before the first message is the one the conversation is created with", async () => {
  const w = await signedIn();

  await w.store.setPictures(false);
  assert.deepEqual(w.named("setPictures"), [], "there is nothing on the server to change yet");
  void w.store.send("hello");
  await settle();

  assert.deepEqual(w.named("create"), [["create", false]]);
  assert.equal(w.store.get().view.pictures, false);
  // And a new conversation after it does not quietly switch them back on.
  w.streams[0].emit(...ANSWER.map((event) => (event.event === "start" ? { ...event, data: { ...START, pictures: false } } : event)));
  await settle();
  w.store.newConversation();
  assert.equal(w.store.get().view.pictures, false);
  assert.equal(w.store.get().view.id, null);
});

test("a pictures switch the server did not take goes back to what is true", async () => {
  const w = await signedIn();
  w.stored.A = conversation("A");
  await w.store.openConversation("A");

  await w.store.setPictures(false);
  assert.equal(w.store.get().view.pictures, false);
  assert.deepEqual(w.named("setPictures"), [["setPictures", "A", false]]);

  w.failing.setPictures = refuse(403, "This account is read-only");
  await w.store.setPictures(true);

  assert.equal(w.store.get().view.pictures, false);
  assert.deepEqual(w.store.get().problem, { where: "pictures", message: "This account is read-only" });
});

test("quick changes of the pictures switch go one at a time, and the switch ends on the last", async () => {
  // Sent together, the server could keep "on" while the answer to "off"
  // came back last: the switch would say pictures stay here while they go
  // to Anthropic.
  const w = await signedIn();
  w.stored.A = conversation("A", { pictures: false });
  await w.store.openConversation("A");
  const answers = [];
  w.hold.setPictures = () => new Promise((resolve) => answers.push(resolve));

  const on = w.store.setPictures(true);
  await settle();
  const off = w.store.setPictures(false);
  await settle();
  assert.deepEqual(w.named("setPictures"), [["setPictures", "A", true]], "the second waits for the first");
  assert.equal(w.store.get().view.pictures, false);

  answers[0]();
  await on;
  await settle();
  assert.deepEqual(w.named("setPictures"), [
    ["setPictures", "A", true],
    ["setPictures", "A", false],
  ]);
  assert.equal(w.store.get().view.pictures, false, "the first answer does not flip the switch back");

  answers[1]();
  await off;
  assert.equal(w.store.get().view.pictures, false);

  // Three quick clicks: the middle one is overtaken and never sent.
  w.calls.length = 0;
  const one = w.store.setPictures(true);
  await settle();
  const two = w.store.setPictures(false);
  const three = w.store.setPictures(true);
  await settle();
  answers[2]();
  await settle();
  answers[3]();
  await Promise.all([one, two, three]);
  assert.deepEqual(w.named("setPictures"), [
    ["setPictures", "A", true],
    ["setPictures", "A", true],
  ]);
  assert.equal(w.store.get().view.pictures, true);
});

test("what is typed survives the panel closing, and opening it asks afresh whether chat can be used", async () => {
  const w = await signedIn();
  w.store.setOpen(true);
  w.store.setDraft("half a thought");
  w.store.setOpen(false);
  await settle();
  w.calls.length = 0;

  w.store.setOpen(true);
  await settle();

  assert.equal(w.store.get().draft, "half a thought");
  assert.deepEqual(w.calls, [["status"], ["conversations"]]);
  assert.deepEqual(w.remembered(), { user: "u1", open: true, conversation: null, draft: "half a thought" });
});

test("signing out stops listening and forgets everything, in the tab's memory too", async () => {
  const w = await signedIn();
  w.store.setDraft("private");
  void w.store.send("hello");
  await settle();
  w.streams[0].emit(...ANSWER.slice(0, 4));
  let told = 0;
  w.store.subscribe(() => {
    told += 1;
  });

  w.store.signOut();
  w.streams[0].emit(...ANSWER.slice(4));
  await settle();

  const now = w.store.get();
  assert.equal(w.streams[0].aborted, true);
  assert.equal(now.user, null);
  assert.deepEqual(now.view.messages, []);
  assert.equal(now.draft, "");
  assert.equal(now.conversations, null);
  assert.equal(now.status, null);
  assert.equal(w.memory, null);
  assert.equal(told, 1);
  assert.equal(w.waits.length, 0);
});

test("an answer that arrives after signing out is not kept for whoever signs in next", async () => {
  // The list of one person's conversations, asked for a moment before they
  // signed out on a shared computer.
  const w = world();
  let late = {};
  w.status = () => new Promise((resolve) => (late.status = resolve));
  w.list = () => new Promise((resolve) => (late.list = resolve));
  w.store.signIn("u1");
  w.store.setOpen(true);
  await settle();
  const theirs = late;
  late = {};

  w.store.signOut();
  w.store.signIn("u2");
  w.store.setOpen(true);
  await settle();
  theirs.status({ ...STATUS, can_chat: false });
  theirs.list([summary("A", { title: "Private" })]);
  await settle();

  assert.equal(w.store.get().conversations, null);
  assert.equal(w.store.get().status, null);

  late.status(STATUS);
  late.list([]);
  await settle();
  assert.deepEqual(w.store.get().conversations, []);
  assert.equal(w.store.get().status.can_chat, true);
});
