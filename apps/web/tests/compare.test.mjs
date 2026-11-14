// Run with `npm test` (Node's own test runner; nothing to install).
//
// Compare's words and sums (src/lib/compare.ts): the builder's request, the
// preview card, the list's pills. The figures are counts of agreement with
// what a person decided, never a grade, so these also check that nothing
// here puts one model before another.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  LIST_FAST_MS,
  LIST_SLOW_MS,
  NO_CUTS,
  PER_CLIP,
  SIZE_PROBLEM,
  bodyKey,
  cutLine,
  cutsByClient,
  decisionText,
  defaultWhat,
  firstTicks,
  folderCounts,
  folderPlace,
  listEvery,
  listingLine,
  liveTicks,
  minutes,
  modelInAddress,
  paceHint,
  previewLines,
  sampleBody,
  sampleMissing,
  sampleText,
  shortDay,
  sizeLine,
  sizeOf,
  startBlocker,
  statePill,
  summaryLine,
  tickBlocker,
  whatChoices,
} from "../src/lib/compare.ts";

const LIMITS = { min_size: 10, max_size: 200, max_models: 6 };
const NOV_13 = Date.parse("2026-11-13T12:00:00Z");

const BLANK = {
  kind: null,
  cutIds: [],
  neighbours: true,
  folder: null,
  what: null,
  folderCutIds: [],
  untickedCutIds: [],
  listingId: null,
  sizeText: "60",
  stills: 3,
};

function model(id, extra = {}) {
  return { id, can_compare: true, reason: null, seconds_hint: null, ...extra };
}

function preview(extra = {}) {
  return {
    ok: true,
    label: "Shots used in Brookside Quick 30 - Finished Work - Rev 2",
    what: "shots",
    size: 60,
    stills: 3,
    items: 60,
    photos: 0,
    shots: 60,
    clips: 14,
    pool: { photos: 0, shots: 212, clips: 31 },
    truth: {
      tags: { footage: 9, yes: 31, no: 4 },
      used: { used: 12, unused: 48, cuts: [{ title: "Brookside Quick 30 - Finished Work - Rev 2" }] },
    },
    left_out: [{ why: "no_shots", count: 7, sentence: "7 clips have no shot analysis yet, so they have no shots to show." }],
    notes: [PER_CLIP],
    ...extra,
  };
}

// ---- the list ---------------------------------------------------------------

test("the list is read often only while a comparison runs, and never where models cannot run", () => {
  assert.equal(listEvery({ mode: "ready", comparisons: [{ state: "finished" }, { state: "running" }] }, false), LIST_FAST_MS);
  assert.equal(listEvery({ mode: "demo", comparisons: [{ state: "waiting" }] }, false), LIST_SLOW_MS);
  assert.equal(listEvery({ mode: "unavailable", comparisons: [{ state: "running" }] }, false), null);
  // Nothing read yet: a failure tries again slowly; a first read that has
  // not answered arms nothing.
  assert.equal(listEvery(null, true), LIST_SLOW_MS);
  assert.equal(listEvery(null, false), null);
});

test("a waiting comparison is not promised for tonight: it may be paused", () => {
  assert.deepEqual(statePill("waiting"), { tone: "warn", label: "waiting" });
  assert.deepEqual(statePill("running"), { tone: "warn", label: "running" });
  assert.deepEqual(statePill("finished"), { tone: "ok", label: "finished" });
  assert.deepEqual(statePill("stopped"), { tone: "bad", label: "stopped" });
  assert.equal(statePill("empty").label, "no models");
});

test("the decision is said as what the owner decided, and nothing when nothing was", () => {
  assert.equal(decisionText({ verdict: "chosen", verdict_model: "gemma-4-e4b-it-4bit" }), "gemma-4-e4b-it-4bit looks");
  assert.equal(decisionText({ verdict: "none", verdict_model: null }), "none good enough yet");
  assert.equal(decisionText({ verdict: "", verdict_model: null }), null);
});

test("a comparison's sample is counted in what it holds", () => {
  assert.equal(sampleText({ shots: 60, photos: 0 }), "60 shots");
  assert.equal(sampleText({ shots: 0, photos: 1 }), "1 photograph");
  assert.equal(sampleText({ shots: 30, photos: 30 }), "30 shots and 30 photographs");
  assert.equal(sampleText({ shots: 0, photos: 0, size: 12 }), "12 items");
  assert.equal(
    summaryLine({ shots: 60, photos: 0, size: 60, models: [{}, {}], state: "finished", verdict: "", verdict_model: null }),
    "60 shots · 2 models",
  );
});

test("a day this year leaves the year out; another year says it", () => {
  assert.equal(shortDay("2026-11-09T12:10:00Z", NOV_13), "9 Nov");
  assert.equal(shortDay("2025-11-09T12:10:00Z", NOV_13), "9 Nov 2025");
  assert.equal(shortDay(null, NOV_13), "");
  assert.equal(shortDay("not a date", NOV_13), "");
});

// ---- 1 · What to compare on ---------------------------------------------------

test("a cut says who made it, when and how many pieces it has", () => {
  const cut = {
    id: "c1",
    title: "Brookside Quick 30 - Finished Work - Rev 2",
    client: "GELCO",
    source: "import",
    created_at: "2026-11-09T12:10:00Z",
    pieces: 11,
  };
  assert.deepEqual(cutLine(cut, NOV_13), {
    title: "Brookside Quick 30 - Finished Work - Rev 2",
    rest: "imported timeline · 9 Nov · 11 pieces",
  });
  assert.equal(cutLine({ ...cut, source: "mcp", pieces: 1 }, NOV_13).rest, "built with Claude · 9 Nov · 1 piece");
});

test("cuts are grouped by who they were for, filtered by title, the unnamed last", () => {
  const cuts = [
    { id: "1", title: "Rev 2", client: "GELCO", source: "import", created_at: null, pieces: 3 },
    { id: "2", title: "Sizzle", client: "", source: "mcp", created_at: null, pieces: 3 },
    { id: "3", title: "Rev 1", client: "GELCO", source: "import", created_at: null, pieces: 3 },
    { id: "4", title: "Open house", client: "Davis", source: "mcp", created_at: null, pieces: 3 },
  ];
  assert.deepEqual(
    cutsByClient(cuts, "").map((group) => [group.client, group.cuts.map((cut) => cut.id)]),
    [
      ["GELCO", ["1", "3"]],
      ["Davis", ["4"]],
      ["No client", ["2"]],
    ],
  );
  assert.deepEqual(
    cutsByClient(cuts, "  REV ").map((group) => [group.client, group.cuts.map((cut) => cut.id)]),
    [["GELCO", ["1", "3"]]],
  );
  assert.deepEqual(cutsByClient(cuts, "nothing like it"), []);
  assert.match(NO_CUTS, /imported/);
});

test("a folder reads as its library and path, the root as the whole library", () => {
  assert.equal(
    folderPlace({ library_name: "GELCO", path: "Brookside Country Club/2026/2026-11-03" }),
    "GELCO › Brookside Country Club/2026/2026-11-03",
  );
  assert.equal(folderPlace({ library_name: "GELCO", path: "" }), "GELCO › the whole library");
  assert.equal(
    folderCounts({ library_name: "GELCO", path: "x", photos: 42, clips: 18, clips_with_shots: 16 }),
    "42 photographs · 18 clips (16 with shots)",
  );
  assert.equal(
    folderCounts({ library_name: "GELCO", path: "x", photos: 1, clips: 0, clips_with_shots: 0 }),
    "1 photograph · 0 clips",
  );
});

test("a side of a folder with nothing to show cannot be chosen, and says why", () => {
  const both = { library_name: "L", path: "p", photos: 4, clips: 3, clips_with_shots: 2 };
  assert.ok(whatChoices(both).every((choice) => choice.blocked === null));
  assert.equal(defaultWhat(both), "both");

  const unanalysed = { ...both, clips_with_shots: 0 };
  const choices = Object.fromEntries(whatChoices(unanalysed).map((choice) => [choice.what, choice.blocked]));
  assert.equal(choices.shots, "None of its clips has shot analysis yet.");
  assert.equal(choices.photos, null);
  assert.equal(choices.both, "None of its clips has shot analysis yet.");
  assert.equal(defaultWhat(unanalysed), "photos");

  const clipsOnly = { ...both, photos: 0 };
  assert.equal(defaultWhat(clipsOnly), "shots");
  assert.equal(defaultWhat({ ...both, photos: 0, clips: 0, clips_with_shots: 0 }), null);
});

test("a listing is named with how many photographs it holds", () => {
  assert.equal(listingLine({ name: "130 Davis Rd Auction", item_count: 14 }), "130 Davis Rd Auction — 14 photographs");
});

// ---- the request --------------------------------------------------------------

test("the size is a whole number between the limits, or nothing", () => {
  assert.equal(sizeOf("60", LIMITS), 60);
  assert.equal(sizeOf(" 10 ", LIMITS), 10);
  assert.equal(sizeOf("200", LIMITS), 200);
  for (const bad of ["9", "201", "", "6.5", "-20", "sixty", "1e2"]) assert.equal(sizeOf(bad, LIMITS), null, bad);
  assert.equal(SIZE_PROBLEM(LIMITS), "A sample is between 10 and 200 items.");
});

test("nothing is asked until a sample has been described, and the page says what is missing", () => {
  assert.equal(sampleMissing(BLANK, LIMITS), "Choose what to compare on.");
  assert.equal(sampleMissing({ ...BLANK, kind: "cuts" }, LIMITS), "Tick at least one cut.");
  assert.equal(sampleMissing({ ...BLANK, kind: "folder" }, LIMITS), "Find a folder and choose it.");
  assert.equal(sampleMissing({ ...BLANK, kind: "listing" }, LIMITS), "Choose a listing.");
  assert.equal(sampleMissing({ ...BLANK, kind: "listing", listingId: "l1", sizeText: "5" }, LIMITS), SIZE_PROBLEM(LIMITS));
  assert.equal(sampleBody(BLANK, LIMITS), null);
  assert.equal(bodyKey(null), "");
});

test("cuts look at shots and a listing at photographs, so their what is the server's", () => {
  assert.deepEqual(sampleBody({ ...BLANK, kind: "cuts", cutIds: ["c1", "c2"], neighbours: false, stills: 1 }, LIMITS), {
    source: { kind: "cuts", cut_ids: ["c1", "c2"], neighbours: false },
    what: null,
    truth_cut_ids: null,
    size: 60,
    stills: 1,
  });
  assert.deepEqual(sampleBody({ ...BLANK, kind: "listing", listingId: "l1", sizeText: "14" }, LIMITS), {
    source: { kind: "listing", listing_id: "l1" },
    what: null,
    truth_cut_ids: null,
    size: 14,
    stills: 3,
  });
});

test("a folder counts every cut that touches it until one is unticked", () => {
  const folder = {
    ...BLANK,
    kind: "folder",
    folder: { library_id: "lib", path: "Brookside Country Club/2026" },
    what: "both",
    folderCutIds: ["a", "b"],
  };
  const all = sampleBody(folder, LIMITS);
  assert.deepEqual(all.source, { kind: "folder", library_id: "lib", folder: "Brookside Country Club/2026" });
  assert.equal(all.what, "both");
  // null: every finished cut that touches the folder, including one recorded since.
  assert.equal(all.truth_cut_ids, null);
  assert.deepEqual(sampleBody({ ...folder, untickedCutIds: ["a"] }, LIMITS).truth_cut_ids, ["b"]);
  // Every one unticked: no cut counts, which is not the same as null.
  assert.deepEqual(sampleBody({ ...folder, untickedCutIds: ["a", "b"] }, LIMITS).truth_cut_ids, []);
  assert.notEqual(bodyKey(all), bodyKey(sampleBody({ ...folder, untickedCutIds: ["a"] }, LIMITS)));
  // A folder with nothing on either side has nothing to ask.
  assert.equal(sampleMissing({ ...folder, what: null }, LIMITS), "That folder has nothing to compare on.");
});

// ---- 2 · How many ---------------------------------------------------------------

test("minutes are rounded up and never zero", () => {
  assert.equal(minutes(4.2, 60), 5);
  assert.equal(minutes(1, 1), 1);
  assert.equal(minutes(60, 60), 60);
});

test("how long a run may take is a hint from earlier runs, never a ranking", () => {
  assert.equal(paceHint([], 60), "The first run will show how long a model takes.");
  assert.equal(paceHint([model("a")], 60), "The first run will show how long a model takes.");
  assert.equal(
    paceHint([model("a", { seconds_hint: 4.2 })], 60),
    "About 5 minutes a model, going by how long it took before.",
  );
  // A range, low to high, whatever order the models are ticked in.
  const slowFirst = paceHint([model("a", { seconds_hint: 30 }), model("b", { seconds_hint: 4.2 })], 60);
  const fastFirst = paceHint([model("b", { seconds_hint: 4.2 }), model("a", { seconds_hint: 30 })], 60);
  assert.equal(slowFirst, "About 5 to 30 minutes a model, going by how long each took before.");
  assert.equal(slowFirst, fastFirst);
  assert.match(
    paceHint([model("a", { seconds_hint: 4.2 }), model("b")], 60),
    /Not every ticked model has been timed yet\.$/,
  );
  assert.equal(paceHint([model("a", { seconds_hint: 120 })], 200), "About 7 hours a model, going by how long it took before.");
});

// ---- 3 · Which models -------------------------------------------------------------

test("every model that can be compared is ticked to start, up to six", () => {
  const models = [
    model("a"),
    model("b", { can_compare: false, reason: "It does not read pictures." }),
    ...["c", "d", "e", "f", "g", "h"].map((id) => model(id)),
  ];
  assert.deepEqual(firstTicks(models, 6, null), ["a", "c", "d", "e", "f", "g"]);
});

test("a model named in the address is the only one ticked, and only if it can be compared", () => {
  const models = [model("a"), model("b"), model("t", { can_compare: false, reason: "It does not read pictures." })];
  assert.deepEqual(firstTicks(models, 6, "b"), ["b"]);
  assert.deepEqual(firstTicks(models, 6, "t"), []);
  assert.deepEqual(firstTicks(models, 6, "gone"), []);
  assert.equal(modelInAddress("?model=5f0c&x=1"), "5f0c");
  assert.equal(modelInAddress("?model="), null);
  assert.equal(modelInAddress(""), null);
});

test("a model that can no longer be compared drops out of the ticks", () => {
  const models = [model("a"), model("b", { can_compare: false, reason: "It is still downloading." })];
  assert.deepEqual(liveTicks(models, ["a", "b", "removed"]), ["a"]);
});

test("a text-only model cannot be ticked and says why; neither can a seventh", () => {
  const textOnly = model("t", { can_compare: false, reason: "It does not read pictures." });
  assert.equal(tickBlocker(textOnly, [], 6), "It does not read pictures.");
  const six = ["a", "b", "c", "d", "e", "f"];
  assert.equal(tickBlocker(model("g"), six, 6), "A comparison holds at most six models.");
  // A ticked model can always be unticked.
  assert.equal(tickBlocker(model("a"), six, 6), null);
  assert.equal(tickBlocker(model("g"), ["a"], 6), null);
});

// ---- 4 · What you will get -----------------------------------------------------------

test("the preview says what there is to check against, in the server's counts", () => {
  const lines = previewLines(preview());
  assert.deepEqual(
    lines.map((line) => [line.tone, line.lead ?? null, line.text]),
    [
      ["plain", null, "About 60 shots from 14 clips (of 212 shots in 31 clips)."],
      [
        "plain",
        "Scores",
        "can be checked: 12 of them were used in Brookside Quick 30 - Finished Work - Rev 2, 48 were not.",
      ],
      [
        "plain",
        "Tags",
        `can be checked on 9 clips: you said yes to 31 tags on them and no to 4. ${PER_CLIP}`,
      ],
      ["plain", null, "Left out: 7 clips have no shot analysis yet, so they have no shots to show."],
    ],
  );
});

test("a missing truth is said in amber, not hidden", () => {
  const noTruth = preview({
    truth: { tags: { footage: 0, yes: 0, no: 0 }, used: { used: 0, unused: 60, cuts: [] } },
    left_out: [],
    notes: [
      "Nobody has decided any tags on this footage, so there will be nothing to check tags against.",
      "None of these shots was used in a recorded cut, so there will be nothing to check scores against.",
      PER_CLIP,
    ],
  });
  const lines = previewLines(noTruth);
  assert.equal(lines.some((line) => line.lead), false, "no Scores or Tags line without a truth");
  assert.deepEqual(
    lines.filter((line) => line.tone === "warn").map((line) => line.text),
    noTruth.notes.slice(0, 2),
  );
  // With no tags line to hang it on, the per-clip note is plain, not a warning.
  assert.deepEqual(lines.at(-1), { tone: "plain", text: PER_CLIP });
});

test("a few used shots are a hint, and say so in amber", () => {
  const few = previewLines(
    preview({
      truth: { tags: { footage: 0, yes: 0, no: 0 }, used: { used: 1, unused: 59, cuts: [{ title: "A" }, { title: "B" }] } },
      notes: ["Only 1 of these shots was used: read the score figures as a hint.", PER_CLIP],
      left_out: [],
    }),
  );
  assert.equal(few[1].text, "can be checked: 1 of them was used in A and B, 59 were not.");
  assert.ok(few.some((line) => line.tone === "warn" && line.text.startsWith("Only 1 of these shots")));
});

test("photographs are counted as photographs, both kinds side by side", () => {
  assert.equal(
    sizeLine(preview({ what: "photos", items: 14, shots: 0, photos: 14, clips: 0, pool: { photos: 14, shots: 0, clips: 0 } })),
    "About 14 photographs (of 14 photographs).",
  );
  assert.equal(
    sizeLine(preview({ what: "both", items: 60, shots: 30, photos: 30, clips: 8, pool: { photos: 120, shots: 212, clips: 31 } })),
    "About 30 shots from 8 clips and 30 photographs (of 212 shots in 31 clips and 120 photographs).",
  );
  const photoTags = previewLines(
    preview({
      what: "photos",
      shots: 0,
      photos: 14,
      clips: 0,
      truth: { tags: { footage: 1, yes: 2, no: 0 }, used: { used: 0, unused: 0, cuts: [] } },
      notes: [],
      left_out: [],
    }),
  );
  assert.equal(photoTags.find((line) => line.lead === "Tags").text, "can be checked on 1 photograph: you said yes to 2 tags on it and no to 0.");
});

test("an empty sample shows the server's sentence and nothing else", () => {
  const sentence = "There is nothing to compare on here: it holds no shots.";
  assert.deepEqual(previewLines(preview({ ok: false, notes: [sentence] })), [{ tone: "warn", text: sentence }]);
});

// ---- Start ------------------------------------------------------------------------

test("Start waits for the preview of exactly what it would send, and for a model", () => {
  const ready = { missing: null, key: "k", preview: { key: "k", ok: true }, previewFailed: false, ticked: 2 };
  assert.equal(startBlocker(ready), null);
  assert.equal(startBlocker({ ...ready, missing: "Tick at least one cut." }), "Tick at least one cut.");
  assert.equal(startBlocker({ ...ready, preview: { key: "older", ok: true } }), "Counting…");
  assert.equal(startBlocker({ ...ready, preview: null }), "Counting…");
  assert.match(startBlocker({ ...ready, preview: null, previewFailed: true }), /could not be counted/);
  assert.equal(startBlocker({ ...ready, preview: { key: "k", ok: false } }), "There is nothing to compare on here.");
  assert.equal(startBlocker({ ...ready, ticked: 0 }), "Tick at least one model.");
});

test("no helper's words grade, rank or score a model", () => {
  const words = [
    paceHint([model("a", { seconds_hint: 9 }), model("b", { seconds_hint: 3 })], 60),
    ...previewLines(preview()).map((line) => line.text),
    statePill("finished").label,
  ].join(" ");
  assert.doesNotMatch(words, /\b(best|better|worse|worst|winner|rank|grade)\b|%/i);
});
