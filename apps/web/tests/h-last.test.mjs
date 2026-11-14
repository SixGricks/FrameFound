// Run with `npm test`.
//
// Final QA, the last items (batch H): Review with AI kept an old redo
// sentence after its review had ended; a model whose program is not
// installed could be chosen for a review that then failed at once; a clip
// that cannot play here still had its shot buttons announced as "Play
// shot N", and its Look card spoke of a player that was not there; and a
// review row stored by an older build read "20 of 8".
//
// These pages are React with no DOM to render them in here, so the words and
// the dialog's memory are pinned through the helpers they share, and the
// sources are held to them.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { FLIP_NOTE, flipNote, MOMENTS_LINE, momentsLine } from "../src/lib/clipLooks.ts";
import { INSTALL_PROGRAM } from "../src/lib/localModelsCompare.ts";
import {
  NO_REDO_MEMORY,
  PROGRAM_NOT_INSTALLED,
  modelChoiceText,
  programMissingText,
  progressText,
  redoShown,
  redoText,
  rememberRedo,
  reviewLine,
  shotButtonWords,
  startBlock,
} from "../src/lib/looks.ts";

const read = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");

// ---- Review with AI: the redo box after a review ------------------------------------------

const preview = (byAnother, kinds) => ({
  already: { by_another_model: byAnother },
  by_another_kinds: kinds,
});
const SIX = { photos: 6, clips: 2, shots: 6 };
const NONE = { photos: 0, clips: 0, shots: 0 };

test("the redo box keeps its count while ticked, and believes the server again once a review has ended", () => {
  // Opened: 6 photographs and 2 clips were looked at by another model.
  let memory = rememberRedo(false, NO_REDO_MEMORY, preview(12, SIX));
  assert.deepEqual(memory, { items: 12, kinds: SIX });
  // Ticked: the preview now counts them as "to look at", the box still says how many.
  memory = rememberRedo(true, memory, preview(0, NONE));
  const ticked = redoShown(true, memory, { items: 0, kinds: NONE });
  assert.match(redoText(ticked.items, ticked.kinds), /^Also look again at the 6 photographs and 2 clips/);

  // The review ran and ended; "See what is left here" forgets and unticks.
  memory = rememberRedo(false, NO_REDO_MEMORY, preview(0, NONE));
  const after = redoShown(false, memory, { items: 0, kinds: NONE });
  assert.equal(redoText(after.items, after.kinds), "Nothing here was looked at by another model.");
  // What a fresh dialog says too.
  const fresh = rememberRedo(false, NO_REDO_MEMORY, preview(0, NONE));
  assert.deepEqual(fresh, memory);

  // Without forgetting, the old sentence outlived the review: what was fixed.
  const stale = redoShown(true, { items: 12, kinds: SIX }, { items: 0, kinds: NONE });
  assert.match(redoText(stale.items, stale.kinds), /^Also look again/);
});

test('"See what is left here" and a review no longer listed go back to the folder with redo unticked', () => {
  const dialog = read("components/ReviewWithAi.tsx");
  const back = dialog.slice(dialog.indexOf("const backToFolder = () => {"));
  const body = back.slice(0, back.indexOf("};"));
  assert.match(body, /others\.current = NO_REDO_MEMORY;/);
  assert.match(body, /setRedo\(false\);/);
  assert.match(body, /setReview\(null\);/);
  const button = dialog.slice(dialog.indexOf("See what is left here") - 400, dialog.indexOf("See what is left here"));
  assert.match(button, /backToFolder\(\);/);
  assert.match(dialog, /others\.current = rememberRedo\(redo, others\.current, found\);/);
  assert.match(dialog, /const another = redoShown\(redo, others\.current, counted\);/);
});

// ---- Review with AI: a model whose program is not installed ---------------------------------

const SENTENCE =
  "Demo llama.cpp (not a real program) is not installed. Install it under Software that runs models on the AI models page.";

test("a model whose program is not installed is offered as not available, with the server's sentence", () => {
  const choice = { name: "demo-vision-gguf", memory_gb: 4.2, fits: "day", program_missing: true };
  assert.equal(modelChoiceText(choice), `demo-vision-gguf — ${PROGRAM_NOT_INSTALLED}`);
  assert.equal(
    modelChoiceText({ ...choice, program_missing: false }),
    "demo-vision-gguf — about 4.2 GB, fits by day",
  );
  // An older server sends no such field: as before.
  assert.equal(modelChoiceText({ name: "a", memory_gb: 5, fits: "night" }), "a — about 5 GB, overnight only");

  assert.equal(programMissingText({ problem: SENTENCE, program_missing: true }), SENTENCE);
  assert.equal(programMissingText({ problem: "It does not read pictures.", program_missing: false }), "");
  assert.equal(programMissingText({ problem: null }), "");
  assert.equal(programMissingText(null), "");
});

test("Start stays off for the Looker whose program is missing, and the dialog links to where it is installed", () => {
  // The server sends the sentence as the model's why_not_now (and tonight_problem).
  const looker = {
    todo: { photos: 3, clips: 0, shots: 0, stills_only: 0 },
    first: { need_copy: 0, need_shots: 0, copy_away: 0 },
    already: { items: 0 },
    model: { why_not_now: SENTENCE },
  };
  assert.equal(startBlock(looker), SENTENCE);

  assert.deepEqual(INSTALL_PROGRAM, {
    href: "/models#software",
    label: "AI models › Software that runs models",
  });
  const dialog = read("components/ReviewWithAi.tsx");
  assert.match(dialog, /const programWhy = programMissingText\(preview\.model\);/);
  assert.match(dialog, /<Link href=\{INSTALL_PROGRAM\.href\}>\{INSTALL_PROGRAM\.label\}<\/Link>/);
  // Said once, beside the choice; the buttons are described by it.
  assert.match(dialog, /startWhy !== programWhy \? startWhy : null/);
  assert.match(dialog, /aria-describedby=\{startWhyRef\}/);
});

// ---- a clip that cannot play here -------------------------------------------------------

test("a shot's button promises to play only where the clip can play", () => {
  assert.deepEqual(shotButtonWords("shot 2", "00:16", "00:33", true), {
    label: "Play shot 2, 00:16 to 00:33",
    title: "Play this shot",
  });
  assert.deepEqual(shotButtonWords("shot 2", "00:16", "00:33", false), {
    label: "Shot 2, 00:16 to 00:33 — this clip cannot play here",
    title: "This clip cannot play here. Click to see why.",
  });
  assert.equal(
    shotButtonWords("the best shot", "00:16", "00:33", false).label,
    "The best shot, 00:16 to 00:33 — this clip cannot play here",
  );

  const list = read("components/ShotList.tsx");
  assert.ok(!list.includes("`Play shot ${"), "the shot list writes its own label again");
  assert.match(list, /aria-label=\{words\.label\}/);
  const page = read("app/assets/[id]/page.tsx");
  assert.match(page, /playable=\{!cannotPlayNow\}/);
  assert.match(page, /aria-label=\{bestShot\?\.label\}/);
  // Pressing one still brings the reason into view where the player was.
  assert.match(page, /const stage = videoRef\.current \?\? stageRef\.current;/);
});

test("the Look card speaks of the player only when there is one", () => {
  assert.equal(momentsLine(true), MOMENTS_LINE);
  assert.match(MOMENTS_LINE, /\(the player stays where it is\)/);
  assert.equal(momentsLine(false), "Compare the looks at another moment:");
  const page = read("app/assets/[id]/page.tsx");
  assert.match(page, /<ClipLookCard key=\{asset\.id\} assetId=\{asset\.id\} hasPlayer=\{!cannotPlayNow\} \/>/);
  assert.match(read("components/ClipLookCard.tsx"), /\{momentsLine\(hasPlayer\)\}/);
  // The flip view's note, too: no "player above" to judge sharpness in.
  assert.equal(flipNote(true), FLIP_NOTE);
  assert.match(FLIP_NOTE, /sharpness in the player above/);
  assert.equal(flipNote(false), "Stills are 480 pixels wide; judge colour here.");
  assert.match(read("components/ClipLookCard.tsx"), /\{flipNote\(hasPlayer\)\}/);
});

// ---- a count past its total ---------------------------------------------------------------

test('a review counted past its total says the count alone, never "20 of 8"', () => {
  const old = { done: 20, total: 8, stills_only: 14, seconds_left: null };
  assert.equal(progressText(old), "20 photographs, shots and clips");
  assert.equal(
    reviewLine({
      ...old,
      state: "cancelled",
      message: "Cancelled. 20 of 8 were looked at; their results are kept.",
    }),
    "Cancelled. 20 photographs, shots and clips were looked at; their results are kept.",
  );
  assert.equal(
    reviewLine({ ...old, state: "done", message: "Finished: 1,020 of 8 were looked at." }),
    "Finished: 1,020 photographs, shots and clips were looked at.",
  );
  // Within its total, as before.
  const now = { done: 12, total: 12, stills_only: 0, seconds_left: null };
  assert.equal(progressText(now), "12 of 12 photographs and shots");
  assert.equal(
    reviewLine({ ...now, state: "done", message: "Finished: 12 of 12 were looked at." }),
    "Finished: 12 of 12 photographs and shots were looked at.",
  );
  assert.equal(progressText({ done: 1200, total: 5050 }), "1,200 of 5,050 photographs and shots");
});
