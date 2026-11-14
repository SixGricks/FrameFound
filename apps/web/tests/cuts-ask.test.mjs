// Run with `npm test` (Node's own test runner; nothing to install).
//
// "Ask Claude to review" (src/lib/cuts.ts, the last part): a cut's page hands
// the chat panel a request to review a version, as an attachment on a new
// conversation. The promise that matters is that NOTHING is sent by asking —
// a message to Claude costs money and carries the footage's file names — and
// that what is sent on Send is exactly what was typed and what was attached.
// The rest are the ways that could go wrong quietly: the attachment sent
// twice, left for the next person, or 14,000 characters of shot lines put
// "back" in the message box after a refusal.
//
// The chat store is the real one (lib/chatSession.ts) over a pretend server,
// so what is tested is the attachment against the store the app uses.

import assert from "node:assert/strict";
import { test as nodeTest } from "node:test";

import "./ts-imports.mjs";

import {
  FOLD_OVER_CHARS,
  REVIEW_MS,
  REVIEW_WAIT_MS,
  arrivedSentence,
  arrivedSinceReview,
  arrivedVersions,
  attachedMessage,
  createChatAsk,
  enterInBox,
  foldMessage,
  reviewEvery,
  sendingLine,
  withRequest,
} from "../src/lib/cuts.ts";

// A store that loses track of a send shows up as a promise that never
// settles; nothing here takes real time.
const test = (name, fn) => nodeTest(name, { timeout: 5000 }, fn);

const { createChatStore } = await import("../src/lib/chatSession.ts");

const refuse = (status, message) => Object.assign(new Error(message), { status });
const settle = () => new Promise((resolve) => setImmediate(resolve));

const summary = (id, more = {}) => ({
  id,
  title: "",
  model: "claude-opus-5-5",
  pictures: true,
  state: "idle",
  spent_usd: 0,
  created_at: "2026-11-13T10:00:00+00:00",
  updated_at: "2026-11-13T10:00:00+00:00",
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
const STATUS = {
  available: true,
  configured: true,
  enabled: true,
  problem: null,
  can_chat: true,
  model: "claude-opus-5-5",
  models: ["claude-opus-5-5"],
  budget_usd: 5,
  spent_this_month_usd: 1.2,
};
const HERE = { path: "/cuts/a7e0", title: "Pete Dye — 30 second teaser" };

/** A pretend server and a signed-in store, with the attachment beside it. */
async function world() {
  const w = { calls: [], streams: [], stored: {}, failing: {} };
  const called = (name, ...args) => {
    w.calls.push([name, ...args]);
    if (w.failing[name]) throw w.failing[name];
  };
  w.store = createChatStore({
    status: async () => {
      called("status");
      return STATUS;
    },
    conversations: async () => {
      called("conversations");
      return [];
    },
    create: async (pictures) => {
      called("create", pictures);
      w.stored.C = conversation("C", { pictures });
      return w.stored.C;
    },
    read: async (id) => {
      called("read", id);
      if (!w.stored[id]) throw refuse(404, "No such conversation");
      return w.stored[id];
    },
    setPictures: async (id, pictures) => summary(id, { pictures }),
    remove: async () => undefined,
    stop: async (id) => summary(id),
    send: (id, body, onEvent, signal) =>
      new Promise((resolve, reject) => {
        called("send", id, body);
        const one = { id, body, emit: (...events) => events.forEach(onEvent), end: resolve, fail: reject };
        signal.addEventListener("abort", () =>
          reject(Object.assign(new Error("This operation was aborted"), { name: "AbortError" })),
        );
        w.streams.push(one);
      }),
    answer: () => new Promise(() => undefined),
    follow: () => new Promise(() => undefined),
    context: () => HERE,
    wait: () => new Promise(() => undefined),
    recall: () => null,
    remember: () => undefined,
  });
  w.ask = createChatAsk(w.store);
  w.store.signIn("u1");
  await settle();
  w.named = (name) => w.calls.filter(([called]) => called === name);
  /** The server takes the message that is on its way and answers it. */
  w.answer = async () => {
    const stream = w.streams.at(-1);
    stream.emit(
      { event: "start", index: 0, data: { conversation_id: "C", model: "claude-opus-5-5", pictures: true, message_id: "M1", seq: 1 } },
      { event: "text", index: 1, data: { delta: "Shot 4 runs long." } },
      { event: "done", index: 2, data: { reason: "end", message: null } },
    );
    await settle();
  };
  return w;
}

const REQUEST = {
  label: "Local draft, version 1 — 9 shots, and the 62 it was chosen from",
  text: "Please review this rough cut and improve it if you can.\n\nCut: \"Pete Dye\", version 1. Cut id: a7e0c2d4.",
};

// --- asking ----------------------------------------------------------------------

test("asking opens the panel on a new conversation with the attachment, and sends nothing", async () => {
  const w = await world();
  let told = 0;
  w.ask.subscribe(() => {
    told += 1;
  });

  assert.equal(w.ask.ask({ attachment: REQUEST }), true);
  await settle();

  const now = w.store.get();
  assert.equal(now.open, true);
  assert.equal(now.view.id, null, "a new conversation, not yet made on the server");
  assert.deepEqual(now.view.messages, []);
  assert.equal(now.draft, "");
  assert.deepEqual(w.ask.get(), REQUEST);
  assert.equal(w.ask.asked(), 1);
  assert.ok(told >= 1);
  // Opening the panel asks whether Claude can be used and for the list.
  // Nothing was created and nothing was sent.
  assert.deepEqual(w.calls.map(([name]) => name).sort(), ["conversations", "status"]);
  assert.deepEqual(w.streams, []);
});

test("asking leaves an open conversation for a new one", async () => {
  const w = await world();
  w.stored.A = conversation("A", { title: "Find the silo" });
  await w.store.openConversation("A");
  assert.equal(w.store.get().view.id, "A");

  w.ask.ask({ attachment: REQUEST });

  assert.equal(w.store.get().view.id, null);
  assert.equal(w.named("send").length, 0);
});

test("what was typed and not sent stays in the box, whatever a page asks", async () => {
  const w = await world();
  w.store.setDraft("half a thought");

  w.ask.ask({ attachment: REQUEST });
  assert.equal(w.store.get().draft, "half a thought");

  // A page that brings its own words ("Ask Claude instead") writes them
  // after the reader's, never over them: the box is kept while the panel is
  // closed, so what is in it may have been typed an hour ago and forgotten.
  const request = "Please make a rough cut from the folder 2026/Pete Dye.";
  w.ask.ask({ text: request });
  assert.equal(w.store.get().draft, `half a thought\n\n${request}`);
  assert.equal(w.ask.get(), null, "the earlier attachment does not ride along with another request");
  assert.equal(w.ask.asked(), 2);

  // The button pressed again does not write the request a second time.
  w.ask.ask({ text: request });
  assert.equal(w.store.get().draft, `half a thought\n\n${request}`);
  assert.deepEqual(w.streams, []);
});

test("a page's words go into an empty box as they are", async () => {
  const w = await world();
  const request = "Please make a rough cut from the folder 2026/Pete Dye.";
  w.ask.ask({ text: request });
  assert.equal(w.store.get().draft, request);

  assert.equal(withRequest("", request), request);
  assert.equal(withRequest("  \n", request), request, "a box of blanks is an empty box");
  assert.equal(withRequest("mine \n", request), `mine\n\n${request}`);
  assert.equal(withRequest(request, request), request);
  assert.equal(withRequest("mine", "  "), "mine");
});

// --- the keyboard in the message box ------------------------------------------------------

test("a held Enter never sends: only a key that was pressed does", () => {
  const key = (more = {}) => ({ key: "Enter", shiftKey: false, repeat: false, composing: false, ...more });
  assert.equal(enterInBox(key()), "send");
  // "Ask Claude to review" pressed with Enter and held: the keyboard lands in
  // the box, where an attachment alone is enough to send. The repeats of that
  // one press must not send it (nor add lines to the note).
  assert.equal(enterInBox(key({ repeat: true })), "swallow");
  assert.equal(enterInBox(key({ shiftKey: true })), "leave", "Shift+Enter is a new line");
  assert.equal(enterInBox(key({ shiftKey: true, repeat: true })), "leave", "and held, more new lines");
  assert.equal(enterInBox(key({ composing: true })), "leave", "the input method's Enter");
  assert.equal(enterInBox(key({ key: "a", repeat: true })), "leave");
});

test("nobody signed in: asking does nothing at all", async () => {
  const w = await world();
  w.store.signOut();
  w.calls.length = 0;

  assert.equal(w.ask.ask({ attachment: REQUEST }), false);
  await settle();

  assert.equal(w.ask.get(), null);
  assert.equal(w.ask.asked(), 0);
  assert.equal(w.store.get().open, false);
  assert.deepEqual(w.calls, []);
});

test("an attachment with nothing in it is not an attachment", async () => {
  const w = await world();
  w.ask.ask({ attachment: { label: "Empty", text: "  \n" } });
  assert.equal(w.ask.get(), null);
});

test("the attachment can be taken away, and then nothing of it is sent", async () => {
  const w = await world();
  w.ask.ask({ attachment: REQUEST });
  w.ask.drop();
  assert.equal(w.ask.get(), null);

  const sent = w.ask.send("What is on this page?");
  await settle();
  await w.answer();

  assert.equal(await sent, true);
  assert.equal(w.named("send")[0][2].text, "What is on this page?");
});

// --- sending ---------------------------------------------------------------------

test("Send gives Claude what was typed, a blank line, then the attachment — once", async () => {
  const w = await world();
  w.ask.ask({ attachment: REQUEST });
  w.store.setDraft("  Tighten the opening.  ");

  const sent = w.ask.send(w.store.get().draft);
  await settle();

  // On its way: off the box and off the composer, on screen as sent.
  assert.equal(w.ask.get(), null);
  assert.equal(w.store.get().draft, "");
  const whole = `Tighten the opening.\n\n${REQUEST.text}`;
  assert.deepEqual(w.named("create"), [["create", true]], "pictures on, as for any new conversation");
  assert.deepEqual(w.named("send"), [["send", "C", { text: whole, context: HERE }]]);
  assert.equal(w.store.get().view.messages[0].text, whole);

  await w.answer();
  assert.equal(await sent, true);
  assert.equal(w.ask.get(), null);
  assert.equal(w.store.get().draft, "");

  // The next message is an ordinary one.
  const next = w.ask.send("And the ending?");
  await settle();
  assert.equal(w.named("send")[1][2].text, "And the ending?");
  await w.answer();
  await next;
});

test("with nothing typed the attachment goes by itself", async () => {
  const w = await world();
  w.ask.ask({ attachment: REQUEST });

  const sent = w.ask.send("");
  await settle();
  assert.equal(w.named("send")[0][2].text, REQUEST.text);
  await w.answer();
  assert.equal(await sent, true);
});

test("leaving the page out of the message still works with something attached", async () => {
  const w = await world();
  w.ask.ask({ attachment: REQUEST });

  const sent = w.ask.send("Shorter, please.", { context: null });
  await settle();
  assert.equal(w.named("send")[0][2].context, null);
  await w.answer();
  await sent;
});

test("with nothing attached, sending is the chat store's own send", async () => {
  const w = await world();
  const sent = w.ask.send("Find the silo");
  await settle();
  assert.deepEqual(w.named("send"), [["send", "C", { text: "Find the silo", context: HERE }]]);
  await w.answer();
  assert.equal(await sent, true);
  // And an empty box sends nothing, as before.
  assert.equal(await w.ask.send("   "), false);
  assert.equal(w.named("send").length, 1);
});

test("a message the server refuses comes back as it was: the words in the box, the attachment above it", async () => {
  const w = await world();
  w.ask.ask({ attachment: REQUEST });
  w.store.setDraft("Tighten the opening.");

  const sent = w.ask.send(w.store.get().draft);
  await settle();
  w.streams.at(-1).fail(refuse(400, "That message is too long."));
  await settle();

  assert.equal(await sent, false);
  assert.equal(w.store.get().draft, "Tighten the opening.");
  assert.deepEqual(w.ask.get(), REQUEST);
  assert.deepEqual(w.store.get().problem, { where: "send", message: "That message is too long." });
  assert.deepEqual(w.store.get().view.messages, [], "the unsent message is off the screen");

  // And it can be sent again, whole.
  const again = w.ask.send(w.store.get().draft);
  await settle();
  assert.equal(w.named("send").at(-1)[2].text, `Tighten the opening.\n\n${REQUEST.text}`);
  await w.answer();
  assert.equal(await again, true);
  assert.equal(w.ask.get(), null);
});

test("a refusal with nothing typed leaves an empty box, not the attachment's text in it", async () => {
  const w = await world();
  w.ask.ask({ attachment: REQUEST });
  w.failing.create = refuse(503, "The Claude packages are not installed on this server.");

  assert.equal(await w.ask.send(""), false);

  assert.equal(w.store.get().draft, "");
  assert.deepEqual(w.ask.get(), REQUEST);
  assert.equal(w.named("send").length, 0);
});

test("pressing Send where Claude is still answering loses neither the words nor the attachment", async () => {
  const w = await world();
  w.stored.C = conversation("C", { state: "running", run: { active: true, events: 1, seq: 1 } });
  w.ask.ask({ attachment: REQUEST });
  // The reader goes to an earlier conversation, where a turn is running.
  await w.store.openConversation("C");
  await settle();
  assert.equal(w.store.get().view.running, true);

  assert.equal(await w.ask.send("Tighten the opening."), false);

  assert.equal(w.store.get().draft, "Tighten the opening.");
  assert.deepEqual(w.ask.get(), REQUEST);
  assert.equal(w.named("send").length, 0);
});

test("a message that arrived though its answer was lost is not offered to be sent again", async () => {
  const w = await world();
  w.ask.ask({ attachment: REQUEST });
  w.store.setDraft("Tighten the opening.");

  const sent = w.ask.send(w.store.get().draft);
  await settle();
  const whole = `Tighten the opening.\n\n${REQUEST.text}`;
  // The connection drops before a word comes back, but the server has it.
  w.stored.C = conversation("C", {
    messages: [
      {
        id: "M1",
        seq: 1,
        role: "user",
        text: whole,
        context: HERE,
        tools: [],
        stop_reason: null,
        cost_usd: null,
        created_at: "2026-11-13T10:00:00+00:00",
      },
    ],
  });
  w.streams.at(-1).fail(new TypeError("Failed to fetch"));
  await settle();

  assert.equal(await sent, true);
  assert.equal(w.store.get().draft, "");
  assert.equal(w.ask.get(), null);
  assert.equal(w.store.get().view.messages[0].text, whole);
  assert.equal(w.named("send").length, 1);
});

test("one person's attachment is not there for the next person on the tab", async () => {
  const w = await world();
  w.ask.ask({ attachment: REQUEST });

  w.store.signOut();
  assert.equal(w.ask.get(), null);

  w.store.signIn("u2");
  await settle();
  assert.equal(w.ask.get(), null);
  assert.equal(w.named("send").length, 0);
});

// --- the words around it ------------------------------------------------------------

test("the message is the typed words, a blank line, and the attachment", () => {
  assert.equal(attachedMessage(" Hello \n", " Cut: one. \n"), "Hello\n\nCut: one.");
  assert.equal(attachedMessage("   ", "Cut: one."), "Cut: one.");
  assert.equal(attachedMessage("Hello", "  "), "Hello");
});

test("the line under an attachment says where it goes and what pays for it", () => {
  assert.equal(
    sendingLine(true, "$1.20", "$5.00"),
    "Pressing Send gives this to Claude (Anthropic), with any stills Claude asks to look at. " +
      "It is paid for from the Claude budget: $1.20 of $5.00 used this month.",
  );
  // The pictures switch is the panel's promise about what leaves; the line must not contradict it.
  assert.match(sendingLine(false, "$1.20", "$5.00"), /Pictures are switched off, so no stills go with it\./);
  assert.doesNotMatch(sendingLine(false, "$1.20", "$5.00"), /with any stills/);
  // Before the month's figures are known, none are made up.
  assert.match(sendingLine(true, null, null), /It is paid for from the Claude budget\.$/);
});

test("a long message of your own is shown folded, with nothing dropped", () => {
  assert.equal(foldMessage("Find the silo"), null);
  assert.equal(foldMessage("x".repeat(FOLD_OVER_CHARS)), null);

  const lines = Array.from({ length: 200 }, (_, n) => `${n + 1} 3f2a9c1e DJI_${String(n).padStart(4, "0")} 3.0-7.5s`);
  const text = `Tighten the opening.\n\nPlease review this rough cut.\n\n${lines.join("\n")}`;
  const folded = foldMessage(text);
  assert.ok(folded.head.startsWith("Tighten the opening.\n\nPlease review this rough cut."));
  assert.ok(folded.head.length <= 480);
  assert.ok(folded.head.endsWith("s"), "cut at the end of a line, not in the middle of one");
  // Head and rest are the message: only the line break between them is taken.
  assert.equal(`${folded.head}\n${folded.rest}`, text);

  // One unbroken run still folds, where it falls.
  const run = foldMessage("y".repeat(3000));
  assert.equal(run.head.length + run.rest.length, 3000);
});

// --- waiting for the version Claude may build ------------------------------------------

test("after a review is opened the cut is read every ten seconds, for half an hour", () => {
  const opened = 1_000_000;
  assert.equal(reviewEvery(30_000, opened, opened + 5_000), REVIEW_MS);
  assert.equal(reviewEvery(30_000, opened, opened + REVIEW_WAIT_MS - 1), REVIEW_MS);
  assert.equal(reviewEvery(30_000, opened, opened + REVIEW_WAIT_MS), 30_000, "given up after half an hour");
  // A draft under way is read faster still; no review, no change.
  assert.equal(reviewEvery(3_000, opened, opened + 5_000), 3_000);
  assert.equal(reviewEvery(30_000, null, opened), 30_000);
  // A page that has stopped reading (the cut is gone) is not started again.
  assert.equal(reviewEvery(null, opened, opened + 5_000), null);
  // A clock set back is not a reason to read fast for ever.
  assert.equal(reviewEvery(30_000, opened, opened - 60_000), 30_000);
});

test("a version that appears while the page is open is said once, by who made it", () => {
  const one = { id: "a", version: 1, source: "local" };
  const two = { id: "b", version: 2, source: "mcp" };
  assert.deepEqual(arrivedVersions({ versions: [one] }, { versions: [one, two] }), [two]);
  assert.deepEqual(arrivedVersions({ versions: [one, two] }, { versions: [one, two] }), []);
  // The first reading has nothing to compare with: nothing "arrived".
  assert.deepEqual(arrivedVersions(null, { versions: [one, two] }), []);
  // A version deleted is not one arriving.
  assert.deepEqual(arrivedVersions({ versions: [one, two] }, { versions: [two] }), []);

  assert.equal(arrivedSentence(two), "Claude built version 2.");
  assert.equal(arrivedSentence({ version: 3, source: "local" }), "Version 3 is ready.");
  assert.equal(arrivedSentence({ version: 4, source: "import" }), "Version 4 was imported.");
  assert.equal(arrivedSentence({ version: 5, source: "other" }), "Version 5 was added.");
});

test("a version built while the reader was on another page is said when they come back", () => {
  const one = { id: "a", version: 1, source: "local" };
  const two = { id: "b", version: 2, source: "mcp" };
  // The page that opens on coming back has no earlier reading of its own:
  // what the cut had when the review was opened is what it tells by.
  const watch = { at: 1_000_000, versions: ["a"] };
  assert.deepEqual(arrivedSinceReview(watch, { versions: [one, two] }), [two]);
  // Said once: the watch then holds what that reading had.
  assert.deepEqual(arrivedSinceReview({ ...watch, versions: ["a", "b"] }, { versions: [one, two] }), []);
  // No review was opened for this cut: a first reading announces nothing.
  assert.deepEqual(arrivedSinceReview(undefined, { versions: [one, two] }), []);
  assert.deepEqual(arrivedSinceReview(null, { versions: [one, two] }), []);
  // A version deleted meanwhile is not one arriving.
  assert.deepEqual(arrivedSinceReview({ ...watch, versions: ["a", "gone"] }, { versions: [one] }), []);
});
