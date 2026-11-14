// Run with `npm test` (Node's own test runner; nothing to install).
//
// The answers of one comparison, item by item (src/lib/compare.ts, the third
// part): when the items are read again, what each choice of Show keeps, the
// orders, the words of an item's and a model's cell, and where ← and → go in
// the viewer. Items may be ordered by a model's score; models never are.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  AS_SHOT,
  DIFFER_POINTS,
  ITEMS_EVERY_MS,
  ITEMS_RETRY_MS,
  NOT_COUNTED,
  SHOW_CHOICES,
  answerFoot,
  assetHref,
  canOpen,
  differs,
  failureSentence,
  goneLine,
  itemsKey,
  itemsReader,
  itemsWait,
  keep,
  lookingAt,
  notCountedLine,
  nothingShown,
  orderChoices,
  orderOf,
  rulingMark,
  scoreLine,
  showCounts,
  sorted,
  spanWords,
  spread,
  sureTitle,
  viewerSteps,
} from "../src/lib/compare.ts";

const ok = (over = {}) => ({
  ok: true,
  problem: null,
  message: "",
  tags: [],
  caption: "A fairway from above.",
  score: 50,
  reason: "Steady.",
  seconds: 4.1,
  attempts: 1,
  raw: null,
  ...over,
});
const failed = (problem, over = {}) => ok({ ok: false, problem, score: null, caption: "", reason: "", ...over });
const tag = (name, ruling = null, over = {}) => ({ name, confidence: 0.9, in_vocabulary: true, ruling, ...over });

function item(id, position, over = {}) {
  return {
    id,
    position,
    kind: "shot",
    asset_id: `asset-${id}`,
    in_catalogue: true,
    filename: `${id}.mp4`,
    start_s: 2,
    end_s: 14,
    truth: { yes: [], no: [] },
    used: false,
    used_in: [],
    counted: true,
    answers: {},
    ...over,
  };
}

const IDS = ["e1", "e2"];

test("the items are read again when any column's counts move, not only the total", () => {
  const before = { answers_stored: 60, entries: [{ id: "e1", state: "running", done: 57, failed: 3 }] };
  // Try the failed ones again: three failures deleted, three answers stored.
  const after = { answers_stored: 60, entries: [{ id: "e1", state: "running", done: 60, failed: 0 }] };
  assert.notEqual(itemsKey(before), itemsKey(after));
  assert.equal(itemsKey(before), itemsKey({ ...before }));
  // A column added (no answers yet) is drawn at once: the key moves.
  const added = { ...before, entries: [...before.entries, { id: "e2", state: "queued", done: 0, failed: 0 }] };
  assert.notEqual(itemsKey(before), itemsKey(added));
});

test("the items are read at most every five seconds", () => {
  assert.equal(itemsWait(0, 1_000), 0);
  assert.equal(itemsWait(10_000, 11_000), ITEMS_EVERY_MS - 1_000);
  assert.equal(itemsWait(10_000, 10_000 + ITEMS_EVERY_MS + 1), 0);
});

// A hand-driven clock and timers for itemsReader, and reads that answer only
// when the test says so.
function harness() {
  let now = 1_000_000;
  let timers = [];
  const reads = [];
  const got = [];
  const failures = [];
  const reader = itemsReader({
    read: () =>
      new Promise((resolve, reject) => {
        reads.push({ resolve, reject });
      }),
    answered: (value) => got.push(value),
    failed: (err) => {
      failures.push(err);
      return err !== "gone";
    },
    later: (run, ms) => {
      const timer = { run, at: now + ms };
      timers.push(timer);
      return timer;
    },
    cancel: (timer) => {
      timers = timers.filter((one) => one !== timer);
    },
    now: () => now,
  });
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  async function pass(ms) {
    now += ms;
    for (;;) {
      const due = timers.filter((one) => one.at <= now);
      if (!due.length) break;
      timers = timers.filter((one) => !due.includes(one));
      for (const one of due) one.run();
      await settle();
    }
  }
  return { reader, reads, got, failures, pass, settle, pending: () => timers.length };
}

test("a slow items read is kept although the answers move every poll while it runs", async () => {
  const h = harness();
  h.reader.moved("k1");
  await h.pass(0);
  assert.equal(h.reads.length, 1);
  // Three comparison polls land while the read is still out; none starts or drops a read.
  for (const key of ["k2", "k3", "k4"]) {
    h.reader.moved(key);
    await h.pass(2_500);
  }
  assert.equal(h.reads.length, 1);
  h.reads[0].resolve("items for k1");
  await h.settle();
  assert.deepEqual(h.got, ["items for k1"]);
  // The answers moved meanwhile, and 7.5 s have passed: the next read starts at once.
  await h.pass(0);
  assert.equal(h.reads.length, 2);
  h.reads[1].resolve("items for k4");
  await h.settle();
  assert.deepEqual(h.got, ["items for k1", "items for k4"]);
  // Nothing moved since: nothing more is read.
  await h.pass(60_000);
  assert.equal(h.reads.length, 2);
});

test("the items are not read again sooner than five seconds, nor when nothing moved", async () => {
  const h = harness();
  h.reader.moved("k1");
  await h.pass(0);
  h.reads[0].resolve("a");
  await h.settle();
  h.reader.moved("k1");
  await h.pass(10_000);
  assert.equal(h.reads.length, 1);
  h.reader.moved("k2");
  await h.pass(0);
  assert.equal(h.reads.length, 2, "more than five seconds since the last read");
  h.reads[1].resolve("b");
  await h.settle();
  h.reader.moved("k3");
  await h.pass(ITEMS_EVERY_MS - 1);
  assert.equal(h.reads.length, 2);
  await h.pass(1);
  assert.equal(h.reads.length, 3);
});

test("a failed items read is tried again after a while, sooner if the answers move; a 404 is not", async () => {
  const h = harness();
  h.reader.moved("k1");
  await h.pass(0);
  h.reads[0].reject(new Error("offline"));
  await h.settle();
  assert.equal(h.failures.length, 1);
  await h.pass(ITEMS_RETRY_MS - 1);
  assert.equal(h.reads.length, 1);
  await h.pass(1);
  assert.equal(h.reads.length, 2, "tried again with nothing moving");
  h.reads[1].reject(new Error("offline"));
  await h.settle();
  h.reader.moved("k2");
  await h.pass(ITEMS_EVERY_MS);
  assert.equal(h.reads.length, 3, "the answers moved: no thirty-second wait");
  h.reads[2].reject("gone");
  await h.settle();
  await h.pass(ITEMS_RETRY_MS * 2);
  assert.equal(h.reads.length, 3);
  assert.equal(h.pending(), 0);
});

test("once stopped, a read that lands is not used and nothing more is read", async () => {
  const h = harness();
  h.reader.moved("k1");
  await h.pass(0);
  h.reader.stop();
  h.reads[0].resolve("the last comparison's items");
  await h.settle();
  h.reader.moved("k2");
  await h.pass(60_000);
  assert.deepEqual(h.got, []);
  assert.equal(h.reads.length, 1);
});

test("spread needs two scores and ignores the missing ones", () => {
  assert.equal(spread([]), null);
  assert.equal(spread([70, null]), null);
  assert.equal(spread([70, null, 40, 55]), 30);
});

test("models differ by scores far apart or by a ruled tag one proposed and another did not", () => {
  const apart = item("a", 1, { answers: { e1: ok({ score: 80 }), e2: ok({ score: 80 - DIFFER_POINTS }) } });
  const close = item("b", 2, { answers: { e1: ok({ score: 80 }), e2: ok({ score: 80 - DIFFER_POINTS + 1 }) } });
  assert.equal(differs(apart, IDS), true);
  assert.equal(differs(close, IDS), false);

  const ruled = item("c", 3, {
    answers: { e1: ok({ tags: [tag("Drone", "yes")] }), e2: ok({ tags: [tag("drone", null)] }) },
  });
  // "Drone" and "drone" are one tag; the second model proposed no ruled tag.
  assert.equal(differs(ruled, IDS), true);
  const same = item("d", 4, {
    answers: { e1: ok({ tags: [tag("Drone", "yes"), tag("pool")] }), e2: ok({ tags: [tag("drone", "yes")] }) },
  });
  // An unruled tag one model gave is not a difference worth showing here.
  assert.equal(differs(same, IDS), false);
});

test("one answer, a failure or a column taken out cannot make a difference", () => {
  const oneOnly = item("a", 1, { answers: { e1: ok({ score: 90 }) } });
  const withFailure = item("b", 2, { answers: { e1: ok({ score: 90 }), e2: failed("bad_answer") } });
  const removedColumn = item("c", 3, { answers: { e1: ok({ score: 90 }), gone: ok({ score: 10 }) } });
  assert.equal(differs(oneOnly, IDS), false);
  assert.equal(differs(withFailure, IDS), false);
  assert.equal(differs(removedColumn, IDS), false);
});

test("each choice of Show keeps what it says, and the counts add up", () => {
  const items = [
    item("used", 1, { used: true, answers: { e1: ok(), e2: ok() } }),
    item("unused", 2, { used: false, truth: { yes: ["drone"], no: [] }, answers: { e1: ok({ score: 90 }), e2: ok({ score: 10 }) } }),
    item("photo", 3, { kind: "photo", used: null, start_s: null, end_s: null, truth: { yes: [], no: ["sunset"] } }),
    item("broken", 4, { answers: { e1: failed("timeout"), e2: ok() } }),
  ];
  const ids = (show) => items.filter((one) => keep(one, show, IDS)).map((one) => one.id);
  assert.deepEqual(ids("all"), ["used", "unused", "photo", "broken"]);
  assert.deepEqual(ids("used"), ["used"]);
  // A photograph is neither used nor unused: cut history knows only shots.
  assert.deepEqual(ids("unused"), ["unused", "broken"]);
  assert.deepEqual(ids("decided"), ["unused", "photo"]);
  assert.deepEqual(ids("differ"), ["unused"]);
  assert.deepEqual(ids("failed"), ["broken"]);
  assert.deepEqual(showCounts(items, IDS), { all: 4, differ: 1, used: 1, unused: 2, decided: 2, failed: 1 });
  assert.equal(SHOW_CHOICES[0].value, "all");
  assert.equal(nothingShown("failed"), "No item fits “Something failed”.");
});

test("ordering by a model's score puts its highest first, unscored after, ties as shot", () => {
  const items = [
    item("c", 3, { answers: { e1: ok({ score: 70 }) } }),
    item("a", 1, { answers: { e1: ok({ score: 40 }) } }),
    item("d", 4, { answers: { e1: failed("bad_answer") } }),
    item("b", 2, { answers: { e1: ok({ score: 70 }) } }),
    item("e", 5, {}),
  ];
  assert.deepEqual(sorted(items, AS_SHOT).map((one) => one.id), ["a", "b", "c", "d", "e"]);
  assert.deepEqual(sorted(items, "entry:e1").map((one) => one.id), ["b", "c", "a", "d", "e"]);
  // The list it was given is left alone.
  assert.deepEqual(items.map((one) => one.id), ["c", "a", "d", "b", "e"]);
});

test("the orders offered are as shot and one per model, and a gone column falls back to as shot", () => {
  const entries = [
    { id: "e1", name: "gemma-4-e4b-it-4bit" },
    { id: "e2", name: "Molmo2-O-7B" },
  ];
  assert.deepEqual(orderChoices(entries), [
    { value: "shot", label: "As shot" },
    { value: "entry:e1", label: "By gemma-4-e4b-it-4bit's score, highest first" },
    { value: "entry:e2", label: "By Molmo2-O-7B's score, highest first" },
  ]);
  assert.equal(orderOf("entry:e2", entries), "entry:e2");
  assert.equal(orderOf("entry:e9", entries), AS_SHOT);
  assert.equal(orderOf("nonsense", entries), AS_SHOT);
});

test("an item's words: its span, its page at the shot's start, and when it has gone", () => {
  assert.equal(spanWords(2, 14), "2.0–14.0 s");
  assert.equal(spanWords(null, null), "");
  assert.equal(assetHref({ asset_id: "x", start_s: 12.7 }), "/assets/x?t=12");
  assert.equal(assetHref({ asset_id: "x", start_s: 0 }), "/assets/x");
  assert.equal(assetHref({ asset_id: null, start_s: 3 }), null);
  assert.equal(goneLine("shot"), "This clip is no longer in the catalogue.");
  assert.equal(goneLine("photo"), "This photograph is no longer in the catalogue.");
  assert.equal(canOpen({ asset_id: "x", in_catalogue: false }), false);
  assert.equal(canOpen({ asset_id: null, in_catalogue: true }), false);
});

test("an item is said to be left out of the figures only once a column has finished", () => {
  const left = { counted: false };
  assert.equal(notCountedLine(left, [{ state: "running" }]), null);
  assert.equal(notCountedLine(left, [{ state: "done" }, { state: "running" }]), NOT_COUNTED);
  assert.equal(notCountedLine({ counted: true }, [{ state: "done" }]), null);
});

test("a model's cell: how sure it was, the marks on ruled tags, the score and the time", () => {
  assert.equal(sureTitle(0.9), "it was 90% sure");
  assert.equal(sureTitle(null), null);
  assert.equal(sureTitle(7), "it was 100% sure");
  assert.deepEqual(rulingMark("yes"), { mark: "✓", words: "you had said yes" });
  assert.deepEqual(rulingMark("no"), { mark: "✗", words: "you had said no" });
  assert.equal(rulingMark(null), null);
  assert.equal(scoreLine({ score: 72, reason: "Steady and well framed." }), "Score 72 — Steady and well framed.");
  assert.equal(scoreLine({ score: 72, reason: " " }), "Score 72");
  assert.equal(scoreLine({ score: null, reason: "No score given." }), "No score given.");
  assert.equal(answerFoot({ seconds: 4.12, attempts: 2 }), "4.1 s · second try");
  assert.equal(answerFoot({ seconds: null, attempts: 3 }), "3 tries");
  assert.equal(answerFoot({ seconds: 14, attempts: 1 }), "14 s");
});

test("a failure says the server's sentence, or the one for its kind", () => {
  assert.equal(failureSentence({ message: "It took too long (300 s).", problem: "timeout" }), "It took too long (300 s).");
  assert.equal(failureSentence({ message: "", problem: "bad_answer" }), "It answered, but not in the form asked for.");
  assert.equal(failureSentence({ message: "", problem: "no_picture" }), "Its picture is no longer there.");
  assert.equal(failureSentence({ message: "", problem: null }), "It did not answer.");
});

test("a running column is looking at its first unanswered item as shot", () => {
  const items = [
    item("c", 3, {}),
    item("a", 1, { answers: { e1: ok() } }),
    item("b", 2, {}),
  ];
  assert.equal(lookingAt(items, { id: "e1", state: "running" }), "b");
  assert.equal(lookingAt(items, { id: "e1", state: "queued" }), null);
  assert.equal(lookingAt([item("a", 1, { answers: { e1: ok() } })], { id: "e1", state: "running" }), null);
});

test("the viewer steps through the rows as shown, past items that cannot be opened", () => {
  const rows = [
    item("a", 1),
    item("gone", 2, { in_catalogue: false }),
    item("b", 3),
    item("c", 4),
  ];
  const middle = viewerSteps(rows, "b");
  assert.equal(middle.position, "3 of 4");
  assert.equal(middle.prev?.id, "a");
  assert.equal(middle.next?.id, "c");
  const first = viewerSteps(rows, "a");
  assert.equal(first.prev, null);
  assert.equal(first.next?.id, "b");
  assert.equal(viewerSteps(rows, "c").next, null);
  // Filtered away while open: no place, no steps; the viewer stays put.
  assert.deepEqual(viewerSteps(rows, "elsewhere"), { position: null, prev: null, next: null });
});

test("no word about an answer grades or ranks a model", () => {
  const words = [
    ...SHOW_CHOICES.map((one) => one.label),
    ...orderChoices([{ id: "e1", name: "model-a" }]).map((one) => one.label),
    NOT_COUNTED,
    failureSentence({ message: "", problem: "died" }),
    scoreLine({ score: 72, reason: "Steady." }),
    nothingShown("differ"),
  ].join(" ");
  assert.doesNotMatch(words, /\b(best|better|worse|worst|winner|rank|ranked|grade|accuracy|accurate|right|wrong)\b/i);
});
