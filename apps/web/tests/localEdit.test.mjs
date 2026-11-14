// Run with `npm test` (Node's own test runner; nothing to install).
//
// The listing's "What the local model suggests" (components/LocalEditPanel.tsx):
// the ticks as reads arrive, which photographs a local Apply is editing, and
// the words round asking and applying. The helpers live in src/lib/clipLooks.ts
// with the rest of the listing's words.

import assert from "node:assert/strict";
import { test } from "node:test";

import * as clipLooks from "../src/lib/clipLooks.ts";

const {
  APPLY_WAITS_FOR_ASK,
  APPLY_WAITS_FOR_RUN,
  appliedTargets,
  applyingLine,
  askFocus,
  askHoldsList,
  dismissConfirm,
  dismissedOf,
  ASK_EXPLAINED,
  ASK_FINISHED_LINE,
  ASK_OR_LINE,
  ASK_STOPPED_LINE,
  LISTING_DEMO_LINE,
  askEnded,
  NO_TICKS,
  PANEL_TITLE,
  SKY_WHERE,
  askedLine,
  dismissedLine,
  editingLabel,
  jobLine,
  madeAtShown,
  mergeTicks,
  proposalPicture,
  runTargets,
  skyChoiceLine,
} = clipLooks;

function row(item_id, extra = {}) {
  return { item_id, large: false, state: "proposed", made_at: "2026-11-13T10:00:00Z", ...extra };
}

// ---- the ticks -----------------------------------------------------------------------

test("a list drawn for the first time starts ticked, less the large ones", () => {
  const ticks = mergeTicks(NO_TICKS, [row("a"), row("b", { large: true }), row("c")]);
  assert.deepEqual(ticks.ticked, ["a", "c"]);
});

test("what the operator ticked or unticked survives the next read", () => {
  const first = mergeTicks(NO_TICKS, [row("a"), row("b", { large: true })]);
  // The large one looked at and ticked; the mild one unticked.
  const mine = { ...first, ticked: ["b"] };
  assert.deepEqual(mergeTicks(mine, [row("a"), row("b", { large: true })]).ticked, ["b"]);
});

test("a suggestion that arrives while the model works starts ticked, unless it is large", () => {
  const first = mergeTicks(NO_TICKS, [row("a")]);
  const mine = { ...first, ticked: [] };
  const next = mergeTicks(mine, [row("a"), row("b"), row("c", { large: true })]);
  assert.deepEqual(next.ticked, ["b"]);
});

test("a suggestion asked for again is decided afresh: a large new one is unticked", () => {
  const first = mergeTicks(NO_TICKS, [row("a")]);
  assert.deepEqual(first.ticked, ["a"]);
  const redone = mergeTicks(first, [row("a", { large: true, made_at: "2026-11-13T11:00:00Z" })]);
  assert.deepEqual(redone.ticked, []);
});

test("applied and dismissed suggestions leave the ticks, and come back fresh if proposed again", () => {
  const first = mergeTicks(NO_TICKS, [row("a"), row("b")]);
  const after = mergeTicks(first, [row("a", { state: "applied" }), row("b", { state: "dismissed" })]);
  assert.deepEqual(after, { ticked: [], seen: [] });
  const again = mergeTicks({ ...after, ticked: [] }, [row("a", { made_at: "2026-11-13T12:00:00Z" })]);
  assert.deepEqual(again.ticked, ["a"]);
});

test("the ticks come in the list's order", () => {
  const first = mergeTicks(NO_TICKS, [row("a"), row("b"), row("c")]);
  const shuffled = { ...first, ticked: ["c", "a"] };
  assert.deepEqual(mergeTicks(shuffled, [row("a"), row("b"), row("c")]).ticked, ["a", "c"]);
});

// ---- a local run in the gallery ------------------------------------------------------

test("the other modes edit every photograph of the listing", () => {
  assert.deepEqual(runTargets("ai", ["p1", "p2"], "2026-11-13T10:00:00Z", null), ["p1", "p2"]);
  assert.deepEqual(runTargets("preset", ["p1"], null, null), ["p1"]);
});

test("a local run edits what this tab ticked, only for that same run", () => {
  const remembered = { startedAt: "2026-11-13T10:00:00Z", assetIds: ["p2", "gone"] };
  assert.deepEqual(runTargets("local", ["p1", "p2"], "2026-11-13T10:00:00Z", remembered), ["p2"]);
  // Another run (another tab pressed Apply later): not this tab's ticks.
  assert.equal(runTargets("local", ["p1", "p2"], "2026-11-13T11:00:00Z", remembered), null);
  // Nothing remembered: not known here, never "every photograph".
  assert.equal(runTargets("local", ["p1", "p2"], "2026-11-13T10:00:00Z", null), null);
});

test("a local Apply remembers its ticks only when the server queued every one of them", () => {
  assert.deepEqual(appliedTargets(["p1", "p2", "p3"], 3), ["p1", "p2", "p3"]);
  // Another tab applied or dismissed two since this list was read: the
  // server queued 1 of the 3, and which one is not known here — no
  // "editing…" pill on the two it will never edit, no "Editing 1/3…".
  assert.equal(appliedTargets(["p1", "p2", "p3"], 1), null);
  assert.equal(
    runTargets("local", ["p1", "p2", "p3"], "2026-11-13T10:00:00Z", null),
    null,
  );
});

test("Apply tells the server when each ticked suggestion it shows was made, exactly as read", () => {
  // A suggestion made again between the read and the press is then left
  // waiting by the server, not applied unseen.
  assert.deepEqual(
    madeAtShown([
      row("a"),
      row("b", { made_at: "2026-11-13T11:30:05Z" }),
      // No time on the row (an older server): applied unchecked, as before.
      row("c", { made_at: null }),
    ]),
    { a: "2026-11-13T10:00:00Z", b: "2026-11-13T11:30:05Z" },
  );
  assert.deepEqual(madeAtShown([]), {});
});

test("while the live run is this listing's own Apply, the list says it is applying, not waiting", () => {
  assert.equal(
    applyingLine(1, 3),
    "Applying the suggestions: 1 of 3 done. Each photograph's tile says “✓ edited” when it is done, and its row then leaves this list.",
  );
  assert.match(applyingLine(2, null), /^Applying the suggestions: 2 done so far\./);
  assert.doesNotMatch(applyingLine(0, 3), /waits/);
  assert.match(APPLY_WAITS_FOR_RUN, /Apply waits for it/);
});

test("the Auto-edit button counts against what the run covers, or what is done when that is not known", () => {
  assert.equal(editingLabel(12, 34), "Editing 12/34…");
  assert.equal(editingLabel(3, null), "Editing… 3 done");
});

// ---- the words -----------------------------------------------------------------------

test("the sky Apply would use is named, or how to pick one is said", () => {
  assert.equal(skyChoiceLine("Blue 02"), "Sky: Blue 02");
  assert.equal(skyChoiceLine(""), "Sky: none chosen (pick one under Auto-edit to replace skies)");
  assert.deepEqual(
    SKY_WHERE.map((choice) => choice.value),
    ["suggested", "all"],
  );
});

test("asking and dismissing say what they did", () => {
  assert.equal(askedLine(12), "The local model will look at 12 photographs. Nothing changes until you choose.");
  assert.equal(askedLine(1), "The local model will look at 1 photograph. Nothing changes until you choose.");
  assert.equal(dismissedLine(1), "1 suggestion dismissed.");
  assert.equal(dismissedLine(3), "3 suggestions dismissed.");
});

test("Dismiss asks first, naming how many it puts aside, ticked or not", () => {
  assert.equal(
    dismissConfirm(14),
    "Dismiss all 14 suggestions, ticked or not? Asking the local model again makes them anew.",
  );
  assert.equal(dismissConfirm(1), "Dismiss the 1 suggestion? Asking the local model again makes it anew.");
});

test("Dismiss counts what the server dismissed of the rows sent, not what was on screen", () => {
  const answer = [
    row("a", { state: "dismissed" }),
    row("b", { state: "dismissed" }),
    // Applied by another tab before this one's Dismiss reached the server.
    row("c", { state: "applied" }),
    // Arrived after the list was read: not sent, so left waiting.
    row("d", { state: "proposed" }),
    row("e", { state: "dismissed" }),
  ];
  assert.equal(dismissedOf(["a", "b", "c"], answer), 2);
  assert.equal(dismissedLine(dismissedOf(["a", "b", "c"], answer)), "2 suggestions dismissed.");
  assert.equal(dismissedOf([], answer), 0);
});

test("Apply and Dismiss wait while the model is about to look or looking, and say why", () => {
  // Queued or running: a suggestion asked for again could replace a ticked
  // one before Apply's run reads it.
  assert.equal(askHoldsList(job("queued")), true);
  assert.equal(askHoldsList(job("running")), true);
  // Waiting for the night window: Apply's run is long done before it starts.
  assert.equal(askHoldsList(job("waiting")), false);
  assert.equal(askHoldsList(job("failed")), false);
  assert.equal(askHoldsList(null), false);
  assert.match(APPLY_WAITS_FOR_ASK, /Apply and Dismiss wait while the local model looks/);
});

// ---- the rail's keyboard focus -------------------------------------------------------

test("a request already under way when the page opens never takes focus", () => {
  // The first answer is the baseline: a request waiting for the night window
  // must not put Cancel under the keyboard, where Space would press it.
  let step = askFocus(null, true, 0);
  assert.equal(step.focus, null);
  // It ends on the server later, nobody having pressed anything: still nothing.
  step = askFocus(step.seen, false, 0);
  assert.equal(step.focus, null);
  // One started from another tab: nothing either.
  step = askFocus(step.seen, true, 0);
  assert.equal(step.focus, null);
});

test("after a press in the rail, focus goes to what is there to press, and follows that request to its end", () => {
  let step = askFocus(null, false, 0);
  // Ask pressed: the request is under way when the press settles.
  step = askFocus(step.seen, true, 1);
  assert.equal(step.focus, "cancel");
  // A read with nothing changed moves nothing.
  step = askFocus(step.seen, true, 1);
  assert.equal(step.focus, null);
  // The request ends: Cancel goes, focus to Ask.
  step = askFocus(step.seen, false, 1);
  assert.equal(step.focus, "ask");
  // A later request started elsewhere is not this press's: nothing.
  step = askFocus(step.seen, true, 1);
  assert.equal(step.focus, null);
  // A refused press settles with nothing under way: Ask again.
  step = askFocus(askFocus(null, false, 0).seen, false, 1);
  assert.equal(step.focus, "ask");
});

test("a suggestion asked for again gets a new picture address; the first keeps the server's", () => {
  const url = "/api/v1/listings/l/local-edit/i/preview.jpg?straighten=true";
  assert.equal(
    proposalPicture(url, "2026-11-13T10:00:00Z"),
    `${url}&v=2026-11-13T10%3A00%3A00Z`,
  );
  assert.notEqual(proposalPicture(url, "2026-11-13T10:00:00Z"), proposalPicture(url, "2026-11-13T11:00:00Z"));
  assert.equal(proposalPicture(url, null), url);
  assert.equal(proposalPicture("/x.jpg", "t"), "/x.jpg?v=t");
});

function job(state, extra = {}) {
  return { state, message: "", not_before: null, done: 0, total: 3, cancelling: false, ...extra };
}

test("the end of a request seen moving is said; a failure is left to the request's own line", () => {
  assert.equal(askEnded(job("running"), null), ASK_FINISHED_LINE);
  assert.equal(askEnded(job("queued"), job("cancelled")), ASK_STOPPED_LINE);
  // Seen being stopped, then gone: stopped, not finished.
  assert.equal(askEnded(job("running", { cancelling: true }), null), ASK_STOPPED_LINE);
  assert.equal(askEnded(job("running"), job("failed", { message: "The model stopped answering." })), null);
  // Still on its way, or never seen moving: nothing.
  assert.equal(askEnded(job("queued"), job("running")), null);
  assert.equal(askEnded(null, null), null);
  assert.equal(askEnded(job("failed"), null), null);
});

test("a listing's request that has not named its first photograph says photographs, not a still", () => {
  assert.equal(jobLine(job("running", { total: 14 })), "Looking at the photographs…");
  assert.equal(jobLine(job("running", { total: 14, done: 1, message: "IMG_2542.jpg" })), "Looking at IMG_2542.jpg, 2 of 14…");
  assert.equal(jobLine(job("running", { total: 1 })), "Looking at the still…");
});

test("no sentence of the listing's panel grades a photograph", () => {
  const said = [
    LISTING_DEMO_LINE,
    ASK_STOPPED_LINE,
    ASK_FINISHED_LINE,
    PANEL_TITLE,
    ASK_OR_LINE,
    ASK_EXPLAINED,
    ...SKY_WHERE.map((choice) => choice.label),
    skyChoiceLine("Blue 02"),
    skyChoiceLine(null),
    askedLine(40),
    dismissedLine(3),
    dismissConfirm(3),
    APPLY_WAITS_FOR_ASK,
    APPLY_WAITS_FOR_RUN,
    applyingLine(1, 3),
    applyingLine(1, null),
    editingLabel(1, 2),
    editingLabel(1, null),
  ];
  for (const sentence of said) {
    assert.doesNotMatch(sentence, /ΔE|delta|score|rank|best|better|worse|%/i, sentence);
  }
});
