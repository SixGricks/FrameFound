// Run with `npm test`.
//
// The viewer keeps one player while the item on screen stays the same.
// Asking it for another moment of that clip has to move the player there;
// the caption said it had, and the clip played on from the first moment.
// (This replaces the People page's own FollowMoment, which did the same for
// faces only.)

import assert from "node:assert/strict";
import { test } from "node:test";

import { seekOnChange } from "../src/lib/seek.ts";

const at = (seconds, key) => ({ at: seconds, key });

test("another moment in the same clip moves the clip there", () => {
  assert.equal(seekOnChange(at(12), at(40)), 40);
  assert.equal(seekOnChange(at(40, "b"), at(12, "a")), 12);
});

test("a second face seen at the same moment brings the clip back to it", () => {
  // Two detections in one frame: the reader may have scrubbed away since.
  assert.equal(seekOnChange(at(12, "a"), at(12, "b")), 12);
});

test("the same moment drawn again does not jump the clip back", () => {
  // A redraw after an answer was saved, or development mode running the
  // effect twice: the reader may be watching on past the moment.
  assert.equal(seekOnChange(at(12, "a"), at(12, "a")), null);
  assert.equal(seekOnChange(at(12), at(12)), null);
});

test("a moment that is not given, or is not a time, moves nothing", () => {
  assert.equal(seekOnChange(at(12, "a"), at(undefined, "b")), null);
  assert.equal(seekOnChange(at(12), at(-1)), null);
  assert.equal(seekOnChange(at(12), at(Number.NaN)), null);
});

test("the start of the clip is a moment like any other", () => {
  assert.equal(seekOnChange(at(12), at(0)), 0);
});
