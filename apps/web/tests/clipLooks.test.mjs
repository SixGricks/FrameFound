// Run with `npm test` (Node's own test runner; nothing to install).
//
// Colour looks' words and sums (src/lib/clipLooks.ts): the clip card's three
// tiles, its request to the local model, Adjust's sliders, the folder line,
// and a listing's proposals. Colour is judged by eye, so these also check
// that nothing here prints a difference, a score or a rank.

import assert from "node:assert/strict";
import { test } from "node:test";

import * as clipLooks from "../src/lib/clipLooks.ts";

const {
  ADJUST_WAIT_MS,
  FAST_MS,
  FOLDER_WATCH_MS,
  SLOW_MS,
  NO_SLIDERS,
  SLIDERS,
  applyLabel,
  applyNotice,
  askLabel,
  LUT_IN_PREMIERE,
  adjustChoice,
  adjustStart,
  CANCELLED_LINE,
  NO_LUT_LINE,
  cancelSaid,
  cannotCarry,
  chosenApart,
  chosenLine,
  chosenTile,
  fileNameOf,
  firstTicks,
  flipTo,
  folderAskedLine,
  folderEvery,
  folderLine,
  folderListTitle,
  folderPill,
  folderSuggestLabel,
  folderZipBlocked,
  isActive,
  jobLine,
  learnedFrom,
  localEditLine,
  missingLine,
  momentButtonLabel,
  momentLabel,
  needsLooker,
  needsLut,
  panelShown,
  pollEvery,
  provenanceLine,
  readAgainIn,
  shownMoment,
  skyText,
  sliderRows,
  slidersBody,
  slidersOf,
  sooner,
  straightenText,
  suggestEnded,
  suggestLabel,
  tickRange,
  tileTitle,
  waitingSuggestions,
  watchFolder,
  whiteBalanceLine,
} = clipLooks;

const NOW = Date.parse("2026-11-13T15:00:00Z");

function job(state, extra = {}) {
  return { state, message: "", not_before: null, done: 0, total: 0, cancelling: false, ...extra };
}

// ---- moments -------------------------------------------------------------------------

test("a moment reads as minutes and seconds, with hours only when the clip is that long", () => {
  assert.equal(momentLabel(0), "0:00");
  assert.equal(momentLabel(7000), "0:07");
  assert.equal(momentLabel(760_000), "12:40");
  assert.equal(momentLabel(3_723_000), "1:02:03");
});

test("two stills a fraction of a second apart are never given the same label", () => {
  assert.equal(momentLabel(21_500), "0:21.5");
  assert.notEqual(momentLabel(21_000), momentLabel(21_500));
  // Within a twentieth of a second it is the whole second.
  assert.equal(momentLabel(6_999), "0:07");
});

test("a moment the server could not have meant still prints something", () => {
  assert.equal(momentLabel(-5), "0:00");
  assert.equal(momentLabel(Number.NaN), "0:00");
});

test("a preview is only ever asked for at a moment the clip lists", () => {
  const looks = { stills: [{ ts_ms: 7000 }, { ts_ms: 21_500 }], reference_ts_ms: 7000 };
  assert.equal(shownMoment(looks, 21_500), 21_500);
  // The server answers any other moment with 400; the reference stands in.
  assert.equal(shownMoment(looks, 14_000), 7000);
  assert.equal(shownMoment(looks, null), 7000);
  assert.equal(shownMoment({ stills: [], reference_ts_ms: null }, 7000), null);
});

// ---- a request to the local model ----------------------------------------------------

test("each state of a request reads in words, the server's sentence first", () => {
  assert.equal(jobLine(job("queued")), "Waiting its turn…");
  assert.equal(
    jobLine(job("waiting", { message: "Waiting for the overnight window (starts 21:30)." })),
    "Waiting for the overnight window (starts 21:30).",
  );
  assert.equal(jobLine(job("waiting", { message: "Local AI is paused." })), "Local AI is paused.");
  assert.equal(jobLine(job("waiting")), "Waiting…");
  assert.equal(jobLine(job("running", { total: 1 })), "Looking at the still…");
  assert.equal(
    jobLine(job("failed", { message: "The model looked but did not answer with slider values. Nothing was stored." })),
    "The model looked but did not answer with slider values. Nothing was stored.",
  );
  assert.equal(jobLine(job("failed")), "It did not finish.");
  assert.equal(jobLine(job("cancelled", { message: "Cancelled." })), "Cancelled.");
  assert.equal(jobLine(job("done")), "");
});

test("a listing's request names the photograph it is on and how far it has got", () => {
  const running = job("running", { message: "IMG_0398.jpg", done: 12, total: 40 });
  assert.equal(jobLine(running), "Looking at IMG_0398.jpg, 13 of 40…");
});

test("a request being stopped says so whatever state it is in", () => {
  assert.equal(jobLine(job("running", { cancelling: true })), "Stopping…");
  assert.equal(jobLine(job("waiting", { cancelling: true, message: "Local AI is paused." })), "Stopping…");
  // Once it has ended, the ending is what it says.
  assert.equal(jobLine(job("cancelled", { cancelling: true, message: "Cancelled." })), "Cancelled.");
});

test("only queued, waiting and running requests are on their way", () => {
  for (const state of ["queued", "waiting", "running"]) assert.equal(isActive(job(state)), true, state);
  for (const state of ["done", "failed", "cancelled"]) assert.equal(isActive(job(state)), false, state);
  assert.equal(isActive(null), false);
  assert.equal(isActive(undefined), false);
});

test("the card reads itself every 2.5 s while a request moves and not at all once none is on its way", () => {
  assert.equal(FAST_MS, 2_500);
  assert.equal(pollEvery(job("queued"), NOW), FAST_MS);
  assert.equal(pollEvery(job("running"), NOW), FAST_MS);
  assert.equal(pollEvery(job("waiting", { cancelling: true }), NOW), FAST_MS);
  assert.equal(pollEvery(null, NOW), null);
  assert.equal(pollEvery(job("done"), NOW), null);
  assert.equal(pollEvery(job("failed"), NOW), null);
  assert.equal(pollEvery(job("cancelled"), NOW), null);
});

test("a request waiting for the overnight window is read slowly until its time is near", () => {
  const far = job("waiting", { not_before: "2026-11-13T21:30:00Z" });
  assert.equal(pollEvery(far, NOW), SLOW_MS);
  const near = job("waiting", { not_before: "2026-11-13T15:01:00Z" });
  assert.equal(pollEvery(near, NOW), FAST_MS);
  // A wait with no end given is not watched quickly.
  assert.equal(pollEvery(job("waiting"), NOW), SLOW_MS);
});

test("after a failed read the card always tries again, however quiet it was", () => {
  assert.equal(readAgainIn(null, false, NOW), null);
  assert.equal(readAgainIn(null, true, NOW), SLOW_MS);
  assert.equal(readAgainIn(job("running"), true, NOW), FAST_MS);
  assert.equal(readAgainIn(job("running"), false, NOW), FAST_MS);
});

test("the model's tile offers a first suggestion, then to ask again", () => {
  assert.equal(suggestLabel(false), "Suggest a look");
  assert.equal(suggestLabel(true), "Ask again");
});

test("a refused Suggest points to the AI models page only where giving a model the Looker role would fix it", () => {
  assert.equal(needsLooker({ mode: "ready", ok: false }), true);
  assert.equal(needsLooker({ mode: "demo", ok: false }), true);
  // The container server cannot run a model, whatever that page says.
  assert.equal(needsLooker({ mode: "unavailable", ok: false }), false);
  assert.equal(needsLooker({ mode: "ready", ok: true }), false);
});

function looksWith(jobNow, model) {
  return {
    suggest: { job: jobNow },
    candidates: [
      { source: "model", available: false, version: "0", ...model },
      { source: "learned", available: false, version: "0" },
      { source: "none", available: true, version: "0" },
    ],
  };
}

test("a suggestion arriving while the card watched is announced; a failure is left to the tile", () => {
  const asking = looksWith(job("running"), {});
  assert.equal(
    suggestEnded(asking, looksWith(null, { available: true, version: "m1" })),
    "The local model has suggested a look.",
  );
  // Ask again: a suggestion was there before; the new one has a new version.
  const again = looksWith(job("queued"), { available: true, version: "m1" });
  assert.equal(suggestEnded(again, looksWith(null, { available: true, version: "m2" })), "The local model has suggested a look.");
  // Stopped: the old suggestion stands and nothing new was stored.
  assert.equal(
    suggestEnded(again, looksWith(null, { available: true, version: "m1" })),
    "The request was cancelled. Nothing was stored.",
  );
  assert.equal(suggestEnded(asking, looksWith(job("failed", { message: "It stopped." }), {})), null);
});

test("Ask again that comes back with the very same look is announced as a suggestion, not as cancelled", () => {
  // The version is a sum of the look alone; a model asked again about the
  // same still at temperature 0 answers the same sliders. Only the moment
  // it was stored tells a finished request from a cancelled one.
  const before = { available: true, version: "m1", made_at: "2026-11-13T09:00:00Z" };
  const again = looksWith(job("running"), before);
  assert.equal(
    suggestEnded(again, looksWith(null, { ...before, made_at: "2026-11-13T15:00:04Z" })),
    "The local model has suggested a look.",
  );
  assert.equal(suggestEnded(again, looksWith(null, before)), CANCELLED_LINE, "nothing new was stored");
});

test("cancelling a request that was still waiting is said at once, since no later read shows it", () => {
  // A waiting or queued request ends the moment Cancel is pressed and comes
  // back cancelled; the read after it no longer has a request to compare.
  assert.equal(cancelSaid(job("cancelled")), "The request was cancelled. Nothing was stored.");
  assert.equal(suggestEnded(looksWith(job("cancelled"), {}), looksWith(null, {})), null);
  // One under way comes back still running, being stopped: its end is said
  // by the read that sees it end.
  assert.equal(cancelSaid(job("running", { cancelling: true })), null);
  assert.equal(suggestEnded(looksWith(job("running", { cancelling: true }), {}), looksWith(null, {})), CANCELLED_LINE);
  // One that finished before Cancel reached it says nothing here; the read
  // after it announces the suggestion.
  assert.equal(cancelSaid(job("done")), null);
});

test("nothing is announced about a request the card never saw moving, or one still on its way", () => {
  const arrived = looksWith(null, { available: true, version: "m1" });
  assert.equal(suggestEnded(null, arrived), null, "a page opened after it ended");
  assert.equal(suggestEnded(looksWith(null, {}), arrived), null);
  assert.equal(suggestEnded(looksWith(job("queued"), {}), looksWith(job("running"), {})), null);
});

// ---- the sliders ---------------------------------------------------------------------

test("Adjust offers the eight sliders a LUT carries, in plain words and the server's ranges", () => {
  assert.deepEqual(
    SLIDERS.map((spec) => spec.label),
    ["Exposure", "Contrast", "Temperature", "Tint", "Saturation", "Vibrance", "Darken shadows", "Remove colour cast"],
  );
  const byKey = Object.fromEntries(SLIDERS.map((spec) => [spec.key, spec]));
  assert.deepEqual([byKey.exposure.min, byKey.exposure.max], [-2, 2]);
  // Shadows only darken: a LUT cannot lift them region by region.
  assert.deepEqual([byKey.shadows.min, byKey.shadows.max], [-1, 0]);
  assert.deepEqual([byKey.auto_wb.min, byKey.auto_wb.max], [0, 1]);
  assert.deepEqual(Object.keys(NO_SLIDERS).sort(), SLIDERS.map((spec) => spec.key).sort());
});

test("Adjust starts from a look's own sliders, zero elsewhere and inside the server's ranges", () => {
  const sliders = slidersOf({ exposure: 0.25, auto_wb: 0.6, shadows: 0.3, window_pull: 0.5, contrast: 3 });
  assert.equal(sliders.exposure, 0.25);
  assert.equal(sliders.auto_wb, 0.6);
  assert.equal(sliders.shadows, 0, "a shadow lift is not a slider here");
  assert.equal(sliders.contrast, 1);
  assert.equal(sliders.tint, 0);
  assert.equal("window_pull" in sliders, false);
  assert.deepEqual(slidersOf(null), NO_SLIDERS);
});

test("what is sent for a look of one's own is the sliders that move, held in range", () => {
  assert.deepEqual(slidersBody({ ...NO_SLIDERS, exposure: 0.5, tint: -0.1 }), { exposure: 0.5, tint: -0.1 });
  assert.deepEqual(slidersBody({ exposure: 9, shadows: 0.4, auto_wb: -1 }), { exposure: 2 });
  assert.deepEqual(slidersBody(NO_SLIDERS), {});
});

test("the numbers are shown as the editor names them, exposure in stops and the rest out of 100", () => {
  assert.deepEqual(sliderRows({ auto_wb: 0.6, exposure: 0.25, temperature: 0.08, vibrance: 0.2 }), [
    { key: "exposure", label: "Exposure", value: "+0.25 stops" },
    { key: "temperature", label: "Temperature", value: "+8" },
    { key: "vibrance", label: "Vibrance", value: "+20" },
    { key: "auto_wb", label: "Remove colour cast", value: "+60" },
  ]);
  assert.deepEqual(sliderRows({ exposure: -1, shadows: -0.35 }), [
    { key: "exposure", label: "Exposure", value: "−1 stop" },
    { key: "shadows", label: "Darken shadows", value: "−35" },
  ]);
  assert.deepEqual(sliderRows({}), []);
  assert.deepEqual(sliderRows({ contrast: 0.001, window_pull: 0.4 }), [], "nothing a LUT leaves out, nothing that rounds to 0");
});

test("a LUT is offered only for a look that changes something, as the server makes one", () => {
  // A model that asked only to lift the shadows is stored with nothing a LUT
  // carries; the server answers its .cube with "needs no LUT", so no link.
  assert.equal(needsLut({}), false);
  assert.equal(needsLut(null), false);
  assert.equal(needsLut({ exposure: 0 }), false);
  assert.equal(needsLut({ exposure: 0.25 }), true);
  assert.equal(needsLut({ shadows: -0.35 }), true);
  // The server keeps three decimals; a value too small to print still
  // makes a file, so it is still offered.
  assert.equal(needsLut({ contrast: 0.004 }), true);
  assert.match(NO_LUT_LINE, /no LUT/i);
});

const TILES = [
  { source: "model", available: true, recipe: { exposure: 0.25, auto_wb: 0.6 }, basis: null },
  { source: "learned", available: false, recipe: {}, basis: null },
  { source: "none", available: true, recipe: {}, basis: null },
];

test("Adjust starts from the look last pressed, else the chosen look, else nothing moved", () => {
  const fromModel = adjustStart(TILES, "model", { recipe: { contrast: 0.3 } });
  assert.equal(fromModel.from, "The local model's suggestion");
  assert.deepEqual(fromModel.sliders, { ...NO_SLIDERS, exposure: 0.25, auto_wb: 0.6 });
  // A tile that cannot be shown is not a starting point.
  const fromChosen = adjustStart(TILES, "learned", { recipe: { contrast: 0.3 } });
  assert.equal(fromChosen.from, "The chosen look");
  assert.equal(fromChosen.sliders.contrast, 0.3);
  assert.deepEqual(adjustStart(TILES, null, null), { from: "No change", sliders: NO_SLIDERS });
  assert.deepEqual(adjustStart(TILES, "none", { recipe: { contrast: 0.3 } }).sliders, NO_SLIDERS);
});

test("Use my version stores the sliders that move, and No change when none does", () => {
  assert.deepEqual(adjustChoice({ ...NO_SLIDERS, exposure: 0.5 }), { source: "custom", recipe: { exposure: 0.5 } });
  // A look of one's own that changes nothing would offer a LUT the server refuses.
  assert.deepEqual(adjustChoice(NO_SLIDERS), { source: "none", recipe: {} });
  assert.equal(ADJUST_WAIT_MS, 250);
});

// ---- the three tiles -----------------------------------------------------------------

test("each tile is titled for what it is, and the learned one for where it was learned", () => {
  assert.equal(tileTitle({ source: "model", basis: null }), "The local model's suggestion");
  assert.equal(tileTitle({ source: "learned", basis: "clips" }), "Your look");
  assert.equal(tileTitle({ source: "learned", basis: "photos" }), "Your photograph look");
  assert.equal(tileTitle({ source: "learned", basis: null }), "Your look");
  assert.equal(tileTitle({ source: "none", basis: null }), "No change");
});

test("the learned look says what it was learned from, and nothing while it is not learned yet", () => {
  assert.equal(
    learnedFrom({ source: "learned", basis: "clips", examples: 14, available: true }),
    "learned from the 14 clips you have chosen a look for",
  );
  assert.equal(
    learnedFrom({ source: "learned", basis: "photos", examples: 429, available: true }),
    "from 429 shipped photographs",
  );
  assert.equal(learnedFrom({ source: "learned", basis: "photos", examples: 1, available: true }), "from 1 shipped photograph");
  assert.equal(learnedFrom({ source: "learned", basis: null, examples: 0, available: false }), "");
  assert.equal(learnedFrom({ source: "model", basis: null, examples: 0, available: true }), "");
});

test("the arrows of the flip view go round the looks there are, in the tiles' order", () => {
  const all = ["model", "learned", "none"];
  assert.equal(flipTo(all, "model", 1), "learned");
  assert.equal(flipTo(all, "none", 1), "model");
  assert.equal(flipTo(all, "model", -1), "none");
  // The model has not suggested anything yet: it is skipped.
  assert.equal(flipTo(["learned", "none"], "none", 1), "learned");
  assert.equal(flipTo(["learned", "none"], "model", 1), "learned");
  assert.equal(flipTo([], "none", 1), "none");
});

test("what a LUT cannot carry is named, and nothing is said when it carries the whole look", () => {
  assert.equal(
    cannotCarry(["lifting the shadows", "window pull", "local contrast"]),
    "A LUT cannot carry: lifting the shadows, window pull, local contrast.",
  );
  assert.equal(cannotCarry([]), null);
  assert.equal(cannotCarry(null), null);
});

test("a suggestion says which model, build and question made it, from what was recorded only", () => {
  assert.equal(
    provenanceLine({
      model: "mlx-community/gemma-4-e4b-it-4bit",
      model_revision: "475b9088d29754a3379866cf5aeb6b41acd313c2",
      variant: "",
      runtime: "mlx-vlm",
      runtime_version: "0.7.12",
      prompt_version: "colour-1",
    }),
    "mlx-community/gemma-4-e4b-it-4bit · build 475b9088 · mlx-vlm 0.7.12 · prompt colour-1",
  );
  assert.equal(
    provenanceLine({
      model: "google/gemma-4-12B-it",
      model_revision: null,
      variant: "4bit",
      runtime: null,
      runtime_version: null,
      prompt_version: "colour-1",
    }),
    "google/gemma-4-12B-it (4bit) · prompt colour-1",
  );
  assert.equal(provenanceLine(null), "");
});

test("the white balance line names the moment each look measured it, and only when one does", () => {
  const model = { available: true, recipe: { auto_wb: 0.6, exposure: 0.25 }, measured_at_ms: 7000 };
  const learned = { available: true, recipe: { auto_wb: 0.8 }, measured_at_ms: 7000 };
  const none = { available: true, recipe: {}, measured_at_ms: null };
  assert.equal(
    whiteBalanceLine([model, learned, none]),
    "Each look's white balance was measured at 0:07. Look at other moments to see how it holds.",
  );
  // A suggestion made on another still says its own moment.
  assert.equal(
    whiteBalanceLine([{ ...model, measured_at_ms: 21_500 }, learned, none]),
    "The looks' white balance was measured at 0:07 and 0:21.5. Look at other moments to see how it holds.",
  );
  assert.equal(whiteBalanceLine([{ ...model, recipe: { exposure: 0.2 } }, none]), null);
  assert.equal(whiteBalanceLine([{ ...model, available: false }, none]), null);
});

test("a choice says when and by whom it was made", () => {
  assert.equal(
    chosenLine({ chosen_at: "2026-11-12T19:40:02Z", chosen_by: "dj@example.com" }, NOW),
    "Chosen 12 Nov by dj@example.com.",
  );
  assert.equal(chosenLine({ chosen_at: "2025-03-02T12:00:00Z", chosen_by: null }, NOW), "Chosen 2 Mar 2025.");
  assert.equal(chosenLine({ chosen_at: null, chosen_by: null }, NOW), "Chosen.");
});

test("only the tile showing the very look that was chosen says chosen", () => {
  const tiles = [
    { source: "model", available: true, version: "m2" },
    { source: "learned", available: true, version: "l1" },
    { source: "none", available: true, version: "n" },
  ];
  assert.equal(chosenTile(tiles, { source: "model", version: "m2" }), "model");
  assert.equal(chosenTile(tiles, { source: "none", version: "n" }), "none");
  // Asked again since: the model's tile shows a newer look than the one chosen.
  assert.equal(chosenTile(tiles, { source: "model", version: "m1" }), null);
  assert.equal(chosenTile(tiles, { source: "custom", version: "m2" }), null);
  assert.equal(chosenTile([{ ...tiles[0], available: false }, tiles[1], tiles[2]], { source: "model", version: "m2" }), null);
  assert.equal(chosenTile(tiles, null), null);
});

test("a chosen look no tile shows says what it is", () => {
  assert.equal(chosenApart("custom"), "Your own version, set with the sliders.");
  assert.equal(chosenApart("model"), "An earlier suggestion from the local model.");
  assert.equal(chosenApart("learned"), "Your look as it was when it was chosen.");
});

// ---- the folder ----------------------------------------------------------------------

test("the folder line counts the clips by what they have, and the rest have no look suggested yet", () => {
  const folder = { clips: 12, chosen: 3, suggested: 5 };
  assert.equal(
    folderLine(folder),
    "This folder has 12 clips: 3 with a chosen look, 5 with a suggestion waiting for you, 4 with no look suggested yet.",
  );
  assert.equal(folderSuggestLabel(folder), "Suggest looks for the 4 clips that have none");
  assert.equal(
    folderLine({ clips: 1, chosen: 0, suggested: 0 }),
    "This folder has 1 clip: 0 with a chosen look, 0 with a suggestion waiting for you, 1 with no look suggested yet.",
  );
  assert.equal(folderSuggestLabel({ clips: 1, chosen: 0, suggested: 0 }), "Suggest a look for the 1 clip that has none");
  assert.equal(folderSuggestLabel({ clips: 4, chosen: 2, suggested: 2 }), null);
});

test("the colour-look card never says a clip was or was not looked at: that is the review's phrase", () => {
  // Final QA: after Review with AI, Browse said the folder was "all looked
  // at" while this card, on a clip with a model's score, said "2 not looked
  // at" and marked the other clip "not looked at". Same words, two meanings.
  const folder = { clips: 2, chosen: 0, suggested: 0 };
  const said = [
    folderLine(folder),
    folderSuggestLabel(folder),
    folderSuggestLabel({ clips: 1, chosen: 0, suggested: 0 }),
    ...["chosen", "suggested", "none"].map((state) => folderPill(state).label),
  ];
  for (const sentence of said) {
    assert.doesNotMatch(sentence, /looked at/i, sentence);
  }
});

test("the folder's Suggest says what it asked for and what it left alone", () => {
  assert.equal(
    folderAskedLine({ queued: 4, already: 3, no_still: 1 }),
    "Asked for 4 looks. They appear here as they arrive. 3 clips already had a look or one on its way. " +
      "1 clip has no still yet, so nothing could be shown to the model.",
  );
  assert.equal(folderAskedLine({ queued: 1, already: 0, no_still: 0 }), "Asked for 1 look. They appear here as they arrive.");
  assert.equal(
    folderAskedLine({ queued: 0, already: 1, no_still: 0 }),
    "No clip needed asking. 1 clip already had a look or one on its way.",
  );
});

test("a clip of the folder is marked by what it has, and only a choice reads as settled", () => {
  assert.deepEqual(folderPill("chosen"), { tone: "ok", label: "chosen" });
  assert.deepEqual(folderPill("suggested"), { tone: "warn", label: "suggestion waiting" });
  assert.deepEqual(folderPill("none"), { tone: null, label: "no look suggested yet" });
});

test("the folder's zip is held back only where it is certain to be refused; the server decides the rest", () => {
  // `chosen` counts "No change" choices, which the zip leaves out, so the
  // page cannot tell from it whether the zip holds anything or is within the
  // finer size's limit: it asks, and says the server's sentence if refused.
  assert.equal(folderZipBlocked({ chosen: 0 }), "None chosen yet.");
  assert.equal(folderZipBlocked({ chosen: 3 }), null, "maybe all No change: asked, not guessed");
  assert.equal(folderZipBlocked({ chosen: 45 }), null, "maybe 35 that change something: allowed at 65");
});

test("a download is saved under the server's own name, accents and all, and never as a path", () => {
  const both = `attachment; filename="Pete Dye - FrameFound looks.zip"; filename*=UTF-8''Caf%C3%A9%20Dye%20-%20FrameFound%20looks.zip`;
  assert.equal(fileNameOf(both, "x.zip"), "Café Dye - FrameFound looks.zip");
  assert.equal(fileNameOf('attachment; filename="Pete Dye - FrameFound looks.zip"', "x.zip"), "Pete Dye - FrameFound looks.zip");
  assert.equal(fileNameOf("attachment; filename=plain.zip", "x.zip"), "plain.zip");
  assert.equal(fileNameOf(`attachment; filename*=UTF-8''..%2F..%2Fevil.zip`, "x.zip"), "evil.zip");
  assert.equal(fileNameOf(`attachment; filename*=UTF-8''%E0%A4%A`, "x.zip"), "x.zip", "a name that cannot be read");
  assert.equal(fileNameOf(null, "FrameFound looks.zip"), "FrameFound looks.zip");
});

test("the folder's list says how many other clips it holds, and when only some are listed", () => {
  assert.equal(folderListTitle({ clips: 12, others: new Array(11) }), "The other 11 clips in this folder");
  assert.equal(folderListTitle({ clips: 2, others: [{}] }), "The other clip in this folder");
  assert.equal(
    folderListTitle({ clips: 341, others: new Array(200) }),
    "The first 200 of the other 340 clips in this folder, by name",
  );
});

test("after the folder's Suggest the card reads itself slowly until the answers are in, and not for ever", () => {
  const before = { clips: 12, chosen: 3, suggested: 5 };
  const watch = watchFolder(before, 4, NOW);
  assert.deepEqual(watch, { until: NOW + FOLDER_WATCH_MS, left: 0 });
  assert.equal(folderEvery(watch, before, NOW), SLOW_MS);
  assert.equal(folderEvery(watch, { clips: 12, chosen: 3, suggested: 7 }, NOW), SLOW_MS, "two still to come");
  assert.equal(folderEvery(watch, { clips: 12, chosen: 3, suggested: 9 }, NOW), null, "all four answered");
  assert.equal(folderEvery(watch, before, NOW + FOLDER_WATCH_MS), null, "the watch ran out");
  assert.equal(watchFolder(before, 0, NOW), null, "nothing was asked");
  assert.equal(folderEvery(null, before, NOW), null);
});

test("the sooner of two re-reads wins, and never only when both say never", () => {
  assert.equal(sooner(null, null), null);
  assert.equal(sooner(FAST_MS, null), FAST_MS);
  assert.equal(sooner(null, SLOW_MS), SLOW_MS);
  assert.equal(sooner(SLOW_MS, FAST_MS), FAST_MS);
});

// ---- a listing's photographs ---------------------------------------------------------

test("a proposal's framing reads in words, nothing when it leaves the picture alone", () => {
  assert.equal(straightenText(0, 0), "Leave the framing as it is");
  // A rounding left by the server never prints "0.0°".
  assert.equal(straightenText(0.01, 0.001), "Leave the framing as it is");
  assert.equal(straightenText(-1.2, 0), "Rotate 1.2° clockwise");
  assert.equal(straightenText(1.2, 0), "Rotate 1.2° counterclockwise");
  assert.equal(straightenText(-1.2, 0.15), "Rotate 1.2° clockwise, straighten the verticals a little");
  assert.equal(straightenText(0, 0.4), "Straighten the verticals");
  assert.equal(straightenText(4.5, 0.8), "Rotate 4.5° counterclockwise, straighten the verticals a lot");
  assert.equal(straightenText(0, -0.2), "Straighten verticals that spread toward the top a little");
});

test("the sky is said plainly", () => {
  assert.equal(skyText(true), "Sky: would replace it");
  assert.equal(skyText(false), "Sky: fine as it is");
});

const ROWS = [
  { item_id: "a", large: false, state: "proposed" },
  { item_id: "b", large: true, state: "proposed" },
  { item_id: "c", large: false, state: "applied" },
  { item_id: "d", large: false, state: "dismissed" },
  { item_id: "e", large: false, state: "proposed" },
];

test("the list shows only what waits for a decision, and starts with the large ones unticked", () => {
  assert.deepEqual(
    waitingSuggestions(ROWS).map((row) => row.item_id),
    ["a", "b", "e"],
  );
  assert.deepEqual(firstTicks(ROWS), ["a", "e"]);
});

test("a shift-click ticks or unticks every row between, either way round", () => {
  const order = ["a", "b", "e", "f"];
  assert.deepEqual(tickRange(order, ["a"], "a", "e", true), ["a", "b", "e"]);
  assert.deepEqual(tickRange(order, ["a", "b", "e", "f"], "f", "b", false), ["a"]);
  // Nothing clicked before: only the row clicked.
  assert.deepEqual(tickRange(order, [], null, "e", true), ["e"]);
  // The ticks come back in the list's order, whatever order they were made in.
  assert.deepEqual(tickRange(order, ["f"], "a", "a", true), ["a", "f"]);
});

test("the list's line says how many photographs and where the tone comes from", () => {
  assert.equal(
    localEditLine({ proposed: 37, tone: { from: "look", examples: 429 } }),
    "37 photographs · tone from your learned look (429 shipped photographs)",
  );
  assert.equal(
    localEditLine({ proposed: 1, tone: { from: "preset", examples: 0 } }),
    "1 photograph · tone from the listing preset",
  );
});

test("photographs the last run could not do are counted, with the first problem named", () => {
  assert.equal(
    missingLine({ failed: 2, unreadable: 1, first_problem: "IMG_0412.jpg: The model answered, but not in the form asked for." }),
    "3 photographs have no suggestion — first: IMG_0412.jpg: The model answered, but not in the form asked for.",
  );
  assert.equal(missingLine({ failed: 0, unreadable: 1, first_problem: null }), "1 photograph has no suggestion.");
  assert.equal(missingLine({ failed: 0, unreadable: 0, first_problem: null }), null);
  assert.equal(missingLine(null), null);
});

test("the list appears when there is something to decide, the model is at work, or photographs were missed", () => {
  const quiet = { proposed: 0, job: null, last_run: { failed: 0, unreadable: 0, first_problem: null } };
  assert.equal(panelShown(quiet), false);
  assert.equal(panelShown({ ...quiet, proposed: 2 }), true);
  assert.equal(panelShown({ ...quiet, job: job("waiting") }), true);
  assert.equal(panelShown({ ...quiet, job: job("failed") }), false);
  assert.equal(panelShown({ ...quiet, last_run: { failed: 1, unreadable: 0, first_problem: null } }), true);
});

test("the rail's button shows how far the model has got, then offers to ask again", () => {
  assert.equal(askLabel({ job: null, suggestions: [] }), "Ask the local model");
  assert.equal(askLabel({ job: job("running", { done: 12, total: 40 }), suggestions: [] }), "Looking 12/40…");
  assert.equal(askLabel({ job: job("queued"), suggestions: [] }), "Looking…");
  assert.equal(askLabel({ job: job("done"), suggestions: [{}] }), "Ask again");
});

test("Apply says how many it will edit and where the tone comes from, made on this machine", () => {
  assert.equal(applyLabel(34), "Apply to the 34 ticked");
  assert.equal(applyLabel(1), "Apply to the 1 ticked");
  assert.equal(
    applyNotice(34, { from: "look", examples: 429 }),
    "Applying the local model's suggestions to 34 photographs. Tone comes from your learned look " +
      "(429 shipped photographs, on this machine). Nothing left the building.",
  );
  assert.equal(
    applyNotice(1, { from: "preset", examples: 0 }),
    "Applying the local model's suggestions to 1 photograph. Tone comes from the listing preset. Nothing left the building.",
  );
});

// ---- the owner's rule ----------------------------------------------------------------

test("no sentence here grades a look: no colour difference, score, rank or percentage", () => {
  const said = [
    ...Object.values(clipLooks).flatMap((value) =>
      typeof value === "string"
        ? [value]
        : Array.isArray(value)
          ? value.flatMap((entry) => Object.values(entry).filter((part) => typeof part === "string"))
          : value && typeof value === "object"
            ? Object.values(value).filter((part) => typeof part === "string")
            : [],
    ),
    tileTitle({ source: "model", basis: null }),
    learnedFrom({ source: "learned", basis: "photos", examples: 429, available: true }),
    whiteBalanceLine([{ available: true, recipe: { auto_wb: 0.6 }, measured_at_ms: 7000 }]),
    folderLine({ clips: 12, chosen: 3, suggested: 5 }),
    localEditLine({ proposed: 37, tone: { from: "look", examples: 429 } }),
    applyNotice(34, { from: "look", examples: 429 }),
    ...sliderRows({ exposure: 0.25, auto_wb: 0.6 }).map((row) => `${row.label} ${row.value}`),
    ...LUT_IN_PREMIERE,
    chosenApart("custom"),
    chosenApart("model"),
    suggestEnded(looksWith(job("running"), {}), looksWith(null, { available: true, version: "m1" })),
    suggestEnded(looksWith(job("running"), {}), looksWith(null, {})),
    suggestLabel(true),
    adjustStart(TILES, "model", null).from,
    folderZipBlocked({ chosen: 0 }),
    cancelSaid(job("cancelled")),
    momentButtonLabel(7000),
    folderListTitle({ clips: 341, others: new Array(200) }),
  ];
  for (const sentence of said) {
    assert.doesNotMatch(sentence, /ΔE|delta|score|rank|best|better|worse|%/i, sentence);
  }
});
