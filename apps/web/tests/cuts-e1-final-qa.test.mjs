// Run with `npm test` (Node's own test runner; nothing to install).
//
// Final QA of the Cuts pages (batch E1): the "Asked." sentence that stayed
// after its draft had ended, the path-profile list that said "No path
// profiles yet" before the server had answered, and the clips handed over
// from Search, which the form counted but never named or linked.

import assert from "node:assert/strict";
import { test } from "node:test";

import { CLIPS_NAMED, askedStands, clipRows, endedDrafts, profilesWord } from "../src/lib/cuts.ts";

const A = "0b4f3c1e-9a7d-4c2b-8e6f-1a2b3c4d5e6f";
const B = "1c5a4d2f-8b6e-4d3c-9f7a-2b3c4d5e6f70";

test("the Asked sentence stands while its draft is under way and goes once it has ended", () => {
  for (const state of ["queued", "waiting", "running"]) {
    assert.equal(askedStands("d1", [{ id: "d1", state }]), true, state);
  }
  for (const state of ["failed", "cancelled", "done"]) {
    assert.equal(askedStands("d1", [{ id: "d1", state }]), false, state);
  }
  // A finished draft becomes a version and leaves the list.
  assert.equal(askedStands("d1", []), false);
  // Another draft still under way does not keep this one's sentence.
  assert.equal(askedStands("d1", [{ id: "d2", state: "running" }]), false);
});

test("the reading that says a draft ended is the one that takes its Asked sentence away", () => {
  // The two pages say how a draft ended from `endedDrafts`; the sentence that
  // promised it must not outlive that line.
  const before = { drafts: [{ id: "d1", state: "running" }] };
  const after = { drafts: [] };
  assert.deepEqual(endedDrafts(before, after), ["d1"]);
  assert.equal(askedStands("d1", before.drafts), true);
  assert.equal(askedStands("d1", after.drafts), false);
});

test("the path-profile list says it is reading until the server answers, and why it is empty after", () => {
  assert.equal(profilesWord("reading", 0), "Reading path profiles…");
  assert.equal(profilesWord("reading", 2), "Reading path profiles…");
  assert.equal(profilesWord("read", 0), "No path profiles yet");
  assert.equal(profilesWord("read", 2), null);
  // Not answered at all is not "none yet": the file then has the server's paths.
  assert.equal(profilesWord("failed", 0), "Path profiles could not be read");
});

test("handed-over clips are each linked to their own page, named once read", () => {
  const { rows, more } = clipRows([A, B], new Map([[A, "DJI_0265_D.mp4"]]));
  assert.equal(more, null);
  assert.deepEqual(
    rows.map((row) => [row.href, row.label, row.named]),
    [
      [`/assets/${A}`, "DJI_0265_D.mp4", true],
      // Not read yet: still a way to the clip, and nothing made up for its name.
      [`/assets/${B}`, "Reading its name…", false],
    ],
  );
});

test("a clip whose name cannot be read is still listed and still opens", () => {
  const { rows } = clipRows([A], new Map([[A, null]]));
  assert.equal(rows.length, 1);
  assert.equal(rows[0].href, `/assets/${A}`);
  assert.equal(rows[0].label, "A clip whose name could not be read");
  assert.equal(rows[0].named, false);
});

test("a long selection names the first clips and counts the rest", () => {
  const ids = Array.from({ length: CLIPS_NAMED + 7 }, (_, n) => `${String(n).padStart(8, "0")}-0000-4000-8000-000000000000`);
  const { rows, more } = clipRows(ids, new Map());
  assert.equal(rows.length, CLIPS_NAMED);
  assert.deepEqual(
    rows.map((row) => row.id),
    ids.slice(0, CLIPS_NAMED),
  );
  assert.equal(more, "…and 7 more");
  assert.equal(clipRows(ids.slice(0, CLIPS_NAMED), new Map()).more, null);
});
