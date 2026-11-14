// Run with `npm test` (Node's own test runner; nothing to install).
//
// One comparison's words and sums (src/lib/compare.ts, the second half):
// how often the page reads itself, each column's Status, the figures'
// sentences and the decision. Every figure is a count of agreement with
// what a person already decided; these tests also check that no sentence
// grades a model or sets one before another.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ALL_USED,
  COMPARE_FAST_MS,
  COMPARE_SLOW_MS,
  FEW_SCORES,
  NO_SCORE_OVERLAP,
  NO_SCORE_TRUTH,
  NO_TAG_OVERLAP,
  NO_TAG_TRUTH,
  PHOTOS_ONLY,
  addChoices,
  againTicks,
  buildLine,
  choiceOf,
  comparisonEvery,
  comparisonLine,
  dayClock,
  decidedLine,
  earlierQuestion,
  endedColumns,
  fitsPill,
  formSentence,
  lookerOffer,
  lookerQuestion,
  memorySentence,
  overNote,
  paceSentence,
  pollEvery,
  removeQuestion,
  roleWords,
  scoreSentences,
  secondsWords,
  statusOf,
  tagSentences,
  timeSentences,
  verdictChange,
  verdictWords,
} from "../src/lib/compare.ts";

const NOW = Date.parse("2026-11-14T20:00:00Z");

const pace = (over = {}) => ({ answered: 0, median_s: null, slowest_s: null, per_hour: null, ...over });

function entry(id, over = {}) {
  return {
    id,
    model_id: `m-${id}`,
    installed: true,
    name: `model-${id}`,
    repo_id: `mlx-community/model-${id}`,
    revision_short: "475b908",
    variant: "",
    runtime: "mlx_vlm",
    runtime_label: "MLX-VLM",
    runtime_version: "0.7.12",
    demo: false,
    state: "done",
    message: "",
    detail: null,
    not_before: null,
    cancelling: false,
    done: 60,
    failed: 0,
    total: 60,
    load_seconds: null,
    ran_from: null,
    memory_gb: null,
    memory_source: null,
    fits: "day",
    roles: [],
    measures: {
      form: { asked: 60, answered: 60, second_tries: 0, failed: {} },
      shots: pace(),
      photos: pace(),
      tags: null,
      scores: null,
    },
    can_cancel: false,
    can_carry_on: false,
    can_retry_failed: false,
    can_remove: true,
    started_at: null,
    finished_at: "2026-11-14T19:20:00Z",
    added_by: null,
    ...over,
  };
}

function comparison(over = {}) {
  return {
    id: "c1",
    name: "Brookside",
    source_kind: "cuts",
    source_label: "Shots used in Brookside Quick 30 - Finished Work - Rev 2",
    what: "shots",
    size: 60,
    stills: 3,
    seed: 7,
    prompt_version: "look-1",
    current: true,
    truth_at: "2026-11-14T19:02:00Z",
    created_at: "2026-11-14T19:02:00Z",
    created_by: "dj@example.com",
    counts: { photos: 0, shots: 60, clips: 14, used: 12, unused: 48, footage_with_tags: 9, yes: 31, no: 4 },
    counted_cuts: [],
    left_out: [],
    vocabulary_size: 150,
    over: 52,
    answers_stored: 120,
    entries: [entry("a"), entry("b")],
    verdict: { verdict: "", entry_id: null, note: "", at: null, by: null },
    can_add_models: true,
    mode: "demo",
    note: null,
    can_manage: true,
    ...over,
  };
}

const tags = (floor, over = {}) => ({
  floor,
  footage: 9,
  yes_total: 31,
  yes_found: 22,
  no_total: 4,
  no_repeated: 1,
  proposed: 140,
  unjudged: 117,
  unjudged_new: 35,
  ...over,
});

const scores = (over = {}) => ({
  used: 11,
  unused: 41,
  pairs: 451,
  higher: 290,
  lower: 118,
  same: 43,
  scored: 52,
  distinct: 40,
  few: false,
  ...over,
});

// ---- reading it again ---------------------------------------------------------------

test("a comparison is read quickly while a column moves, slowly while one waits, then not at all", () => {
  assert.equal(pollEvery(comparison({ entries: [entry("a", { state: "running" })] }), NOW), COMPARE_FAST_MS);
  assert.equal(pollEvery(comparison({ entries: [entry("a"), entry("b", { state: "queued" })] }), NOW), COMPARE_FAST_MS);
  const later = new Date(NOW + 3 * 3600_000).toISOString();
  assert.equal(
    pollEvery(comparison({ entries: [entry("a", { state: "waiting", not_before: later })] }), NOW),
    COMPARE_SLOW_MS,
  );
  assert.equal(pollEvery(comparison({ entries: [entry("a"), entry("b", { state: "stopped" })] }), NOW), null);
  assert.equal(pollEvery(comparison({ mode: "unavailable", entries: [entry("a", { state: "running" })] }), NOW), null);
});

test("a wait about to end and a Cancel being noticed are watched quickly", () => {
  const soon = new Date(NOW + 60_000).toISOString();
  assert.equal(pollEvery(comparison({ entries: [entry("a", { state: "waiting", not_before: soon })] }), NOW), COMPARE_FAST_MS);
  assert.equal(pollEvery(comparison({ entries: [entry("a", { state: "stopped", cancelling: true })] }), NOW), COMPARE_FAST_MS);
});

test("a failed first read is tried again slowly; nothing is read before a first answer otherwise", () => {
  assert.equal(comparisonEvery(null, true), COMPARE_SLOW_MS);
  assert.equal(comparisonEvery(null, false), null);
  assert.equal(comparisonEvery(comparison({ entries: [entry("a", { state: "running" })] }), false), COMPARE_FAST_MS);
});

test("a failed read after an idle one still comes back: the action before it may have started a column", () => {
  const idle = comparison({ entries: [entry("a", { state: "done" }), entry("b", { state: "stopped" })] });
  assert.equal(comparisonEvery(idle, false), null);
  assert.equal(comparisonEvery(idle, true), COMPARE_SLOW_MS);
  // A run that was moving keeps its quick pace through a failed read.
  assert.equal(comparisonEvery(comparison({ entries: [entry("a", { state: "running" })] }), true), COMPARE_FAST_MS);
});

// ---- the header ---------------------------------------------------------------------

test("the line under the name says what was sampled, from where, when it was fixed and how", () => {
  const line = comparisonLine(comparison(), NOW);
  assert.match(line, /^60 shots from 14 clips · Shots used in Brookside Quick 30 - Finished Work - Rev 2 · fixed 14 Nov \d\d:02 · three stills a shot · question look-1$/);
  const photos = comparisonLine(
    comparison({ counts: { ...comparison().counts, shots: 0, clips: 0, photos: 14 }, stills: 1, source_label: "130 Davis Rd" }),
    NOW,
  );
  assert.match(photos, /^14 photographs · 130 Davis Rd · fixed .* · question look-1$/);
  assert.doesNotMatch(photos, /still/);
  assert.match(comparisonLine(comparison({ stills: 1 }), NOW), /one still a shot/);
});

test("an earlier question is named, and said to be readable but not added to", () => {
  assert.equal(earlierQuestion("look-0"), "Made with an earlier version of the question (look-0). It can be read but not added to.");
});

test("a column's head names its build and the software that ran it, and the roles it holds", () => {
  assert.equal(buildLine(entry("a")), "build 475b908 · MLX-VLM 0.7.12");
  assert.equal(buildLine(entry("a", { runtime_label: null, runtime_version: null })), "build 475b908");
  assert.deepEqual(roleWords(["looker", "editor"]), ["Looker", "Editor"]);
  assert.deepEqual(roleWords(null), []);
});

// ---- Status -------------------------------------------------------------------------

test("a running column shows how far it has got as a bar, with any failures", () => {
  const status = statusOf(entry("a", { state: "running", done: 31, failed: 3, total: 60 }), NOW);
  assert.equal(status.text, "34 of 60 · 3 failed");
  assert.deepEqual(status.progress, { value: 34, max: 60, percent: 56 });
  assert.equal(statusOf(entry("a", { state: "running", done: 34, failed: 0 }), NOW).text, "34 of 60");
});

test("a Cancel being noticed reads Stopping, and the bar stays until it stops", () => {
  const status = statusOf(entry("a", { state: "running", cancelling: true, done: 10 }), NOW);
  assert.equal(status.text, "Stopping…");
  assert.ok(status.progress);
});

test("the server's sentence is shown as it comes, with the time when it gave one", () => {
  const overnight = "Waiting for the overnight window (starts 21:30): it needs about 10 GB and the daytime budget is 8 GB.";
  const waiting = statusOf(
    entry("a", { state: "waiting", message: overnight, not_before: "2026-11-14T21:30:00Z", done: 0 }),
    NOW,
  );
  assert.equal(waiting.text, overnight);
  assert.equal(waiting.tone, "warn");
  const down = statusOf(
    entry("a", { state: "waiting", message: "The model server is not answering.", not_before: "2026-11-14T20:05:00Z" }),
    NOW,
  );
  assert.match(down.text, /^The model server is not answering\. It looks again at \d\d:05\.$/);
  const paused = statusOf(entry("a", { state: "waiting", message: "Local AI is paused." }), NOW);
  assert.equal(paused.text, "Local AI is paused.");
});

test("a stopped or failed column keeps the server's sentence and says how far it got", () => {
  const restarted = "It stopped without finishing: the worker running it was restarted. What it had answered is kept.";
  const stopped = statusOf(entry("a", { state: "stopped", message: restarted, done: 20, failed: 1 }), NOW);
  assert.equal(stopped.text, restarted);
  assert.equal(stopped.sofar, "21 of 60 asked so far.");
  const failed = statusOf(entry("a", { state: "failed", message: "It would not load.", done: 0 }), NOW);
  assert.equal(failed.text, "It would not load.");
  assert.equal(failed.tone, "bad");
  assert.equal(failed.sofar, null);
  assert.equal(statusOf(entry("a", { state: "queued", message: "Waiting its turn.", done: 0 }), NOW).text, "Waiting its turn.");
});

test("a finished column says when it finished", () => {
  assert.match(statusOf(entry("a"), NOW).text, /^Finished 14 Nov \d\d:20\.$/);
  assert.equal(statusOf(entry("a", { finished_at: null }), NOW).text, "Finished.");
});

test("a column that ends while the page is open is said once, by name", () => {
  const before = [entry("a", { state: "running" }), entry("b", { state: "queued" }), entry("c", { state: "done" })];
  const after = [entry("a", { state: "done" }), entry("b", { state: "running" }), entry("c", { state: "done" })];
  assert.equal(endedColumns(before, after), "model-a has finished.");
  assert.equal(endedColumns(after, after), null);
  assert.equal(endedColumns([entry("a", { state: "running" })], [entry("a", { state: "failed" })]), "model-a has stopped.");
});

// ---- the figures --------------------------------------------------------------------

test("the form row counts answers, second tries and failures by what went wrong", () => {
  assert.equal(
    formSentence({ asked: 60, answered: 57, second_tries: 9, failed: { timeout: 1, bad_answer: 2 } }),
    "57 of 60. 9 needed a second try. 3 failed: 2 did not answer in the form asked, 1 took too long.",
  );
  assert.equal(formSentence({ asked: 60, answered: 60, second_tries: 0, failed: {} }), "60 of 60.");
  assert.equal(
    formSentence({ asked: 60, answered: 58, second_tries: 0, failed: { no_picture: 2 } }),
    "58 of 60. 2 had no picture to look at.",
  );
  assert.equal(formSentence({ asked: 60, answered: 0, second_tries: 0, failed: {} }), "Nothing answered yet.");
  assert.match(formSentence({ asked: 60, answered: 1, second_tries: 0, failed: { strange: 1 } }), /1 failed \(strange\)/);
});

test("tag figures say what was found, what was repeated and what nobody ruled on, at each floor", () => {
  const said = tagSentences([tags(0.85, { yes_found: 9 }), tags(0), tags(0.6, { yes_found: 18 })], { done: true, truth: true });
  assert.deepEqual(said.lines, [
    "Found 22 of the 31 tags you had said yes to.",
    "Suggested 1 of the 4 you had said no to.",
    "117 more that nobody has ruled on — 35 of them words the catalogue does not have.",
  ]);
  assert.deepEqual(
    said.surer.map((floor) => [floor.label, floor.lines[0]]),
    [
      ["At least 60% sure", "Found 18 of the 31 tags you had said yes to."],
      ["At least 85% sure", "Found 9 of the 31 tags you had said yes to."],
    ],
  );
  const bare = tagSentences([tags(0, { unjudged: 0, unjudged_new: 0, no_total: 0, no_repeated: 0 })], { done: true, truth: true });
  assert.deepEqual(bare.lines.slice(1), ["You had said no to no tags here.", "Nothing more that nobody has ruled on."]);
});

test("tag figures wait for the column to finish, and say why when there is nothing to check against", () => {
  assert.equal(tagSentences([tags(0)], { done: false, truth: true }), null);
  assert.deepEqual(tagSentences(null, { done: true, truth: false }).lines, [NO_TAG_TRUTH]);
  assert.deepEqual(tagSentences(null, { done: true, truth: true }).lines, [NO_TAG_OVERLAP]);
});

test("score figures set each used shot against each unused one, and say when they are weak", () => {
  const lines = scoreSentences(scores(), { done: true, what: "shots", used: 12, unused: 48 });
  assert.deepEqual(lines, [
    "You used 11 of these 52 shots. Setting each used shot against each unused one (451 pairs), it scored the used shot higher in 290, lower in 118, the same in 43. Guessing would get about half.",
  ]);
  assert.match(
    scoreSentences(scores({ distinct: 3 }), { done: true, what: "shots", used: 12, unused: 48 })[1],
    /^It only gave 3 different scores\.$/,
  );
  assert.equal(FEW_SCORES, 5);
  assert.match(
    scoreSentences(scores({ distinct: 1 }), { done: true, what: "shots", used: 12, unused: 48 })[1],
    /every shot the same score, so its scores say nothing here/,
  );
  assert.equal(
    scoreSentences(scores({ used: 3, few: true }), { done: true, what: "shots", used: 3, unused: 48 }).at(-1),
    "Only 3 used shots are in the sample: read this as a hint.",
  );
});

test("score figures say why there is nothing to check, a listing included", () => {
  const done = { done: true, what: "shots", used: 0, unused: 60 };
  assert.equal(scoreSentences(scores(), { ...done, done: false }), null);
  assert.deepEqual(scoreSentences(null, done), [NO_SCORE_TRUTH]);
  assert.deepEqual(scoreSentences(null, { ...done, used: 60, unused: 0 }), [ALL_USED]);
  assert.deepEqual(scoreSentences(null, { ...done, used: 12, unused: 48 }), [NO_SCORE_OVERLAP]);
  assert.deepEqual(scoreSentences(null, { ...done, what: "photos" }), [PHOTOS_ONLY]);
});

test("time is the typical answer, the slowest, about how many an hour, and the load", () => {
  assert.equal(
    paceSentence(pace({ answered: 52, median_s: 4.2, slowest_s: 11, per_hour: 820 }), "shot"),
    "4.2 s a shot, typically; the slowest took 11 s. About 820 shots an hour.",
  );
  assert.equal(paceSentence(pace(), "shot"), null);
  assert.deepEqual(
    timeSentences(
      { load_seconds: 14, ran_from: "library" },
      {
        shots: pace({ answered: 52, median_s: 4.2, slowest_s: 11, per_hour: 820 }),
        photos: pace({ answered: 8, median_s: 2, slowest_s: 3.4, per_hour: 1700 }),
      },
    ),
    [
      "4.2 s a shot, typically; the slowest took 11 s. About 820 shots an hour.",
      "2.0 s a photograph, typically; the slowest took 3.4 s. About 1,700 photographs an hour.",
      "It took 14 s to load from the model library.",
    ],
  );
  assert.deepEqual(timeSentences({ load_seconds: null, ran_from: null }, { shots: pace(), photos: pace() }), ["Not timed yet."]);
  assert.equal(secondsWords(125), "2 min 5 s");
});

test("memory says how it was measured, and the pill says when it may run", () => {
  assert.equal(memorySentence({ memory_gb: 6.1, memory_source: "runtime" }), "6.1 GB at most, as the software reports it.");
  assert.equal(memorySentence({ memory_gb: 6, memory_source: "process" }), "6 GB at most, the size of its process.");
  assert.equal(memorySentence({ memory_gb: 6.14, memory_source: null }), "6.1 GB at most, as measured.");
  assert.equal(memorySentence({ memory_gb: null, memory_source: null }), "Not measured yet.");
  assert.equal(fitsPill("night").label, "overnight only");
  assert.equal(fitsPill(null), null);
});

test("the note under the figures says what they are counted over and what they do not say", () => {
  const two = overNote(comparison());
  assert.equal(two.length, 1);
  assert.match(two[0], /^The tag and score figures use the 52 items every finished model answered/);
  assert.match(two[0], /They do not say which is good — look at the answers\.$/);
  const one = overNote(comparison({ entries: [entry("a"), entry("b", { state: "running" })] }));
  assert.match(one[0], /use the 52 items it answered/);
  assert.equal(one[1], "Figures for a model appear when it finishes.");
  assert.deepEqual(overNote(comparison({ entries: [entry("a", { state: "queued" })] })), [
    "Figures for a model appear when it finishes.",
  ]);
});

// ---- add, again, remove, delete ------------------------------------------------------

const choice = (id, over = {}) => ({ id, name: `model-${id}`, can_compare: true, reason: null, seconds_hint: null, ...over });

test("Add a model offers every model not in it yet, a text-only one and a seventh with why not", () => {
  const offered = addChoices(
    [choice("m-a"), choice("m-c"), choice("m-t", { can_compare: false, reason: "It does not read pictures." })],
    [entry("a"), entry("b")],
    6,
  );
  assert.deepEqual(
    offered.map((one) => [one.model.id, one.blocked]),
    [
      ["m-c", null],
      ["m-t", "It does not read pictures."],
    ],
  );
  const full = addChoices([choice("m-z")], ["a", "b", "c", "d", "e", "f"].map((id) => entry(id)), 6);
  assert.equal(full[0].blocked, "A comparison holds at most six models.");
});

test("Compare again ticks the models compared here that can still be compared", () => {
  assert.deepEqual(
    againTicks([choice("m-a"), choice("m-b", { can_compare: false, reason: "x" }), choice("m-c")], [entry("a"), entry("b")], 6),
    ["m-a"],
  );
});

test("taking a column out says the decision that chose it is cleared", () => {
  const chosen = { verdict: "chosen", entry_id: "a" };
  assert.match(removeQuestion(entry("a"), chosen), /The decision that chose it is cleared; its note is kept\./);
  assert.doesNotMatch(removeQuestion(entry("b"), chosen), /decision/);
});

// ---- the decision -------------------------------------------------------------------

test("Save sends only what changed, and a chosen model with its column", () => {
  const saved = { verdict: "", entry_id: null, note: "" };
  assert.equal(verdictChange("open", "", saved), null);
  assert.deepEqual(verdictChange("entry:a", "", saved), { verdict: "chosen", entry_id: "a" });
  assert.deepEqual(verdictChange("none", "too slow", saved), { verdict: "none", entry_id: null, note: "too slow" });
  const chosen = { verdict: "chosen", entry_id: "a", note: "steady" };
  assert.equal(choiceOf(chosen), "entry:a");
  assert.deepEqual(verdictChange("entry:a", "steadier", chosen), { note: "steadier" });
  assert.deepEqual(verdictChange("open", "steady", chosen), { verdict: "", entry_id: null });
});

test("the decision reads as what the owner decided, even after its column was taken out", () => {
  const base = comparison();
  assert.equal(verdictWords({ ...base, verdict: { ...base.verdict, verdict: "chosen", entry_id: "a" } }), "model-a looks");
  assert.match(verdictWords({ ...base, verdict: { ...base.verdict, verdict: "chosen", entry_id: "gone" } }), /taken out/);
  assert.equal(verdictWords({ ...base, verdict: { ...base.verdict, verdict: "none" } }), "None is good enough yet");
  assert.equal(verdictWords(base), "Not decided yet");
  assert.equal(
    decidedLine({ verdict: "none", entry_id: null, note: "", at: "2026-11-14T19:30:00Z", by: "dj@example.com" }, NOW),
    "Decided 14 Nov by dj@example.com.",
  );
  assert.equal(decidedLine(base.verdict, NOW), null);
});

test("Make it the Looker is offered only for a saved choice that is installed and not the Looker", () => {
  const chose = (over, verdict = { verdict: "chosen", entry_id: "a", note: "", at: null, by: null }) =>
    lookerOffer({ verdict, entries: [entry("a", over), entry("b")] });
  assert.equal(chose({})?.id, "a");
  assert.equal(chose({ roles: ["looker"] }), null);
  assert.equal(chose({ installed: false, model_id: null, roles: null }), null);
  assert.equal(chose({}, { verdict: "none", entry_id: null, note: "", at: null, by: null }), null);
  assert.equal(lookerQuestion("model-a", "model-b"), "Make model-a the Looker instead of model-b?");
  assert.equal(lookerQuestion("model-a", null), "Make model-a the Looker?");
});

test("dates read on this browser's clock, the year only when it is another", () => {
  assert.match(dayClock("2026-11-14T19:02:00Z", NOW), /^14 Nov \d\d:02$/);
  assert.match(dayClock("2025-03-01T12:00:00Z", NOW), /^1 Mar 2025 \d\d:00$/);
  assert.equal(dayClock(null, NOW), "");
});

// ---- no figure grades or orders a model ----------------------------------------------

test("no sentence about a column grades or ranks a model", () => {
  const words = [
    ...tagSentences([tags(0), tags(0.6), tags(0.85)], { done: true, truth: true }).lines,
    ...scoreSentences(scores({ few: true, used: 3, distinct: 2 }), { done: true, what: "shots", used: 3, unused: 9 }),
    formSentence({ asked: 60, answered: 57, second_tries: 9, failed: { timeout: 1, bad_answer: 2 } }),
    ...timeSentences({ load_seconds: 14, ran_from: null }, { shots: pace({ answered: 5, median_s: 3, slowest_s: 9, per_hour: 900 }), photos: pace() }),
    memorySentence({ memory_gb: 6, memory_source: "runtime" }),
    ...overNote(comparison()),
    statusOf(entry("a"), NOW).text,
  ].join(" ");
  assert.doesNotMatch(words, /\b(best|better|worse|worst|winner|rank|ranked|grade|accuracy|accurate)\b/i);
  // A percentage appears only as a confidence floor ("at least 60% sure"),
  // never as a share of right answers.
  const floors = tagSentences([tags(0), tags(0.6)], { done: true, truth: true }).surer.map((floor) => floor.label);
  assert.doesNotMatch(words, /%/);
  assert.ok(floors.every((label) => /^At least \d+% sure$/.test(label)));
});
