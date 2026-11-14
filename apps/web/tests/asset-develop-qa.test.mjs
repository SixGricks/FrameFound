// Run with `npm test`.
//
// Final QA, the clip page and Develop: a clip with no playable copy says so
// on its own page in the in-page viewer's words, and the "Remove objects"
// instruction names the real button and goes when marking is cancelled.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { NOT_PLAYABLE, cannotPlayText, missingFor } from "../src/app/assets/[id]/playable.ts";
import { MARKING_NOTICE, REMOVE_MARKED, noticeAfterMarking } from "../src/app/edit/[assetId]/marking.ts";

// ---- a clip that will not play ---------------------------------------------------------

test("a clip page says why its clip will not play, by what the server says of the copy", () => {
  assert.equal(cannotPlayText(404), "No playable copy of this clip has been made yet.");
  assert.equal(
    cannotPlayText(503),
    "The drive that holds the playable copies is not connected, so this clip cannot be played right now.",
  );
  assert.equal(cannotPlayText(200), "This browser could not play the clip.");
  // Signed out, offline, a server error: nothing specific is claimed.
  assert.equal(cannotPlayText(0), NOT_PLAYABLE);
  assert.equal(cannotPlayText(401), NOT_PLAYABLE);
  assert.equal(cannotPlayText(500), NOT_PLAYABLE);
});

test("the clip page and Browse's viewer say the same thing about the same clip", () => {
  // QA found Browse saying "No playable copy…" and the clip's own page
  // saying nothing. The viewer keeps its sentences to itself, so hold this
  // page's to the viewer's source.
  const viewer = readFileSync(new URL("../src/components/MediaPreview.tsx", import.meta.url), "utf8");
  for (const status of [404, 503, 200, 0]) {
    const sentence = cannotPlayText(status);
    assert.ok(viewer.includes(sentence), `the viewer no longer says: ${sentence}`);
  }
});

test("one clip's reason never shows under the next clip's player", () => {
  const missing = { of: "clip-a", text: cannotPlayText(404) };
  assert.equal(missingFor(missing, "clip-a"), "No playable copy of this clip has been made yet.");
  assert.equal(missingFor(missing, "clip-b"), null);
  assert.equal(missingFor(null, "clip-a"), null);
});

// ---- marking objects for removal -------------------------------------------------------

test("the marking instruction names the button that is there", () => {
  assert.equal(MARKING_NOTICE, "Paint over what should disappear, then press Remove marked.");
  assert.ok(MARKING_NOTICE.includes(REMOVE_MARKED));
  assert.equal(noticeAfterMarking(true, null), MARKING_NOTICE);
  assert.equal(noticeAfterMarking(true, "Saved. The export will use this look."), MARKING_NOTICE);
});

test("cancelling marking takes its instruction away and leaves anything said since", () => {
  assert.equal(noticeAfterMarking(false, MARKING_NOTICE), null);
  assert.equal(noticeAfterMarking(false, "Added sky.jpg to the sky library."), "Added sky.jpg to the sky library.");
  assert.equal(noticeAfterMarking(false, null), null);
});
