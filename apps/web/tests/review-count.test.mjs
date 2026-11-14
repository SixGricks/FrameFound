// Run with `npm test` (Node's own test runner; nothing to install).
//
// The Review with AI dialog counts one way throughout (src/lib/looks.ts):
// what was already looked at, and what redo adds, are said as "Not looked at
// yet" is, photographs and clips with their shots in brackets. Before, a
// folder of 8 files (6 photographs, 2 clips of 3 shots each) read "Already
// looked at: 12 items." beside a folder list that says "8 items".

import assert from "node:assert/strict";
import { test } from "node:test";

import { alreadyText, kindsText, previewKinds, redoText, todoText } from "../src/lib/looks.ts";

// What GET /looks/preview sends for Pete Dye once all of it was looked at.
const peteDye = () => ({
  todo: { photos: 0, clips: 0, shots: 0, stills_only: 0 },
  already: { items: 12, by_another_model: 0 },
  already_kinds: { photos: 6, clips: 2, shots: 6 },
  by_another_kinds: { photos: 0, clips: 0, shots: 0 },
});

test("a folder of 8 all looked at says its 6 photographs and 2 clips, never 12 items", () => {
  const preview = peteDye();
  const said = alreadyText(preview.already, previewKinds(preview).already);
  assert.equal(said, "6 photographs and 2 clips (6 shots).");
  assert.ok(!said.includes("12"));
  assert.ok(!said.includes("item"));
});

test("already and still to do are said in the same words", () => {
  const before = {
    todo: { photos: 0, clips: 2, shots: 6, stills_only: 0 },
    already: { items: 6, by_another_model: 0 },
    already_kinds: { photos: 6, clips: 0, shots: 0 },
  };
  assert.equal(todoText(before), "2 clips (6 shots).");
  assert.equal(alreadyText(before.already, previewKinds(before).already), "6 photographs.");
});

test("a clip judged from one still is a clip with no shots in the brackets", () => {
  assert.equal(kindsText({ photos: 0, clips: 1, shots: 0 }), "1 clip");
  assert.equal(kindsText({ photos: 1, clips: 3, shots: 4 }), "1 photograph and 3 clips (4 shots)");
  assert.equal(kindsText({ photos: 0, clips: 0, shots: 0 }), "");
});

test("redo names what it adds by kind", () => {
  assert.equal(
    redoText(2, { photos: 0, clips: 1, shots: 2 }),
    "Also look again at the 1 clip (2 shots) a different model, or an older version of the question, already looked at.",
  );
  assert.equal(redoText(0, { photos: 0, clips: 0, shots: 0 }), "Nothing here was looked at by another model.");
});

test("a breakdown that is missing or not whole is not used", () => {
  assert.deepEqual(previewKinds({ already: { items: 3, by_another_model: 0 } }), {
    already: null,
    byAnother: null,
  });
  const half = previewKinds({ already_kinds: { photos: 2, clips: "1" }, by_another_kinds: null });
  assert.deepEqual(half, { already: null, byAnother: null });
  // An all-zero breakdown beside a number says the number, not nothing.
  assert.equal(
    alreadyText({ items: 4 }, { photos: 0, clips: 0, shots: 0 }),
    "4, counting each shot of a clip by itself.",
  );
});
