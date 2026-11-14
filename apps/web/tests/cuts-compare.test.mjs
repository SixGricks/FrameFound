// Run with `npm test` (Node's own test runner; nothing to install).
//
// Compare on a cut's page (src/lib/cuts.ts): which shots of one version are
// in the other, which were trimmed or moved, the sentence that counts them,
// and which two versions an address puts side by side. The pills are what
// the reader judges a second pass by, so a shot called "kept" that was cut
// shorter, or one counted twice, is the kind of mistake these are for.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  compareAfterShow,
  compareDefault,
  compareHref,
  compareVersions,
  comparedWith,
  markPills,
  trimNote,
} from "../src/lib/cuts.ts";

let made = 0;
/** A piece: `piece("A", 3, 7.5)` is clip A from 3.0 to 7.5 s. */
const piece = (clip, in_s, out_s, more = {}) => ({
  position: made++,
  asset_id: `id-${clip}`,
  clip,
  in_s,
  out_s,
  ...more,
});
const version = (number, pieces, more = {}) => ({
  version: number,
  duration_s: pieces.reduce((sum, each) => sum + (each.out_s - each.in_s), 0),
  pieces,
  ...more,
});
/** The marks of one side, in the order of its pieces: "kept", "trimmed+moved", "only". */
const marks = (side, pieces) =>
  pieces.map((each) => {
    const mark = side.get(each.position);
    return mark.moved ? `${mark.change}+moved` : mark.change;
  });

test("a second version is counted against the first: kept, trimmed, dropped and added", () => {
  const first = version(1, [
    piece("A", 3, 7.5),
    piece("B", 0, 4),
    piece("C", 10, 14),
    piece("D", 2, 6),
    piece("E", 0, 3),
    piece("F", 5, 9),
    piece("G", 1, 4),
    piece("H", 0, 5),
    piece("I", 8, 12),
  ]);
  const second = version(
    2,
    [
      piece("A", 3, 7.5),
      piece("B", 0, 4),
      piece("C", 10, 13), // an out point pulled in
      piece("D", 2, 6),
      piece("X", 0, 4),
      piece("E", 0.5, 3), // an in point pushed out
      piece("F", 5, 9),
      piece("Y", 2, 5),
    ],
    { duration_s: 29.8 },
  );
  const compared = compareVersions({ ...first, duration_s: 31.5 }, second);

  assert.equal(
    compared.sentence,
    "Version 2 keeps 6 of version 1's 9 shots (2 trimmed), drops 3 and adds 2. 31.5 s → 29.8 s.",
  );
  assert.deepEqual(
    [compared.kept, compared.trimmed, compared.dropped, compared.added, compared.moved],
    [4, 2, 3, 2, 0],
  );
  assert.deepEqual(marks(compared.older, first.pieces), [
    "kept",
    "kept",
    "trimmed",
    "kept",
    "trimmed",
    "kept",
    "only",
    "only",
    "only",
  ]);
  assert.deepEqual(marks(compared.newer, second.pieces), [
    "kept",
    "kept",
    "trimmed",
    "kept",
    "only",
    "trimmed",
    "kept",
    "only",
  ]);
  // A trimmed shot knows what it was, and where its partner stands.
  const trimmed = compared.newer.get(second.pieces[2].position);
  assert.deepEqual(trimmed.other, { number: 3, in_s: 10, out_s: 14 });
  assert.deepEqual(compared.older.get(first.pieces[4].position).other, { number: 6, in_s: 0.5, out_s: 3 });
});

test("in and out within a tenth of a second are the same cut; further apart is a trim", () => {
  const first = version(1, [piece("A", 3, 7.5), piece("B", 1, 5)]);
  const second = version(2, [piece("A", 3.1, 7.4), piece("B", 1.2, 5)]);
  const compared = compareVersions(first, second);
  assert.deepEqual(marks(compared.newer, second.pieces), ["kept", "trimmed"]);
});

test("two pieces from different stretches of one clip are not the same shot", () => {
  // The same file, opposite ends: nothing of the first is in the second.
  const first = version(1, [piece("A", 0, 4)]);
  const second = version(2, [piece("A", 30, 34)]);
  const compared = compareVersions(first, second);
  assert.deepEqual(marks(compared.older, first.pieces), ["only"]);
  assert.deepEqual(marks(compared.newer, second.pieces), ["only"]);
  assert.equal(
    compared.sentence,
    "Version 2 keeps none of version 1's 1 shot: it drops it and adds 1. 4 s → 4 s.",
  );
  // Sharing less than half of the shorter piece is still not the same shot…
  const barely = compareVersions(version(1, [piece("A", 0, 4)]), version(2, [piece("A", 3, 7)]));
  assert.equal(barely.kept + barely.trimmed, 0);
  // …and half of it is.
  const half = compareVersions(version(1, [piece("A", 0, 4)]), version(2, [piece("A", 2, 6)]));
  assert.equal(half.trimmed, 1);
});

test("a piece is matched once: two uses of a clip do not both claim the other version's one", () => {
  const first = version(1, [piece("A", 0, 4), piece("A", 1, 5)]);
  const second = version(2, [piece("A", 0.5, 4.5)]);
  const compared = compareVersions(first, second);
  assert.equal(compared.trimmed, 1);
  assert.equal(compared.dropped, 1);
  assert.deepEqual(marks(compared.older, first.pieces), ["trimmed", "only"]);

  // And the other way round: the newer version uses the clip twice, the
  // older once. One of the two is the old shot; the other is new.
  const once = version(1, [piece("A", 0, 4)]);
  const twice = version(2, [piece("A", 0, 4), piece("A", 0.5, 4.5)]);
  const again = compareVersions(once, twice);
  assert.deepEqual(marks(again.newer, twice.pieces), ["kept", "only"]);
  assert.deepEqual([again.kept, again.trimmed, again.added, again.dropped], [1, 0, 1, 0]);
});

test("the exact stretch is matched before a nearby one, whatever order they come in", () => {
  // The first piece overlaps the newer one, but the second IS the newer one.
  const first = version(1, [piece("A", 2, 6), piece("A", 3, 7)]);
  const second = version(2, [piece("A", 3, 7)]);
  const compared = compareVersions(first, second);
  assert.deepEqual(marks(compared.older, first.pieces), ["only", "kept"]);
  assert.equal(compared.kept, 1);
  assert.equal(compared.trimmed, 0);
});

test("a shot put somewhere else is marked moved, and only that one", () => {
  const first = version(1, [piece("A", 0, 4), piece("B", 0, 4), piece("C", 0, 4), piece("D", 0, 4)]);
  // D brought to the front; A, B and C are where they were relative to each other.
  const second = version(2, [piece("D", 0, 4), piece("A", 0, 4), piece("B", 0, 4), piece("C", 0, 4)]);
  const compared = compareVersions(first, second);
  assert.deepEqual(marks(compared.newer, second.pieces), ["kept+moved", "kept", "kept", "kept"]);
  assert.deepEqual(marks(compared.older, first.pieces), ["kept", "kept", "kept", "kept+moved"]);
  assert.equal(
    compared.sentence,
    "Version 2 keeps all 4 of version 1's shots (1 moved). 16 s → 16 s.",
  );
});

test("a new shot at the front does not make every shot after it look moved", () => {
  const first = version(1, [piece("A", 0, 4), piece("B", 0, 4), piece("C", 0, 4)]);
  const second = version(2, [piece("X", 0, 2), piece("A", 0, 4), piece("B", 0, 4), piece("C", 0, 4)]);
  const compared = compareVersions(first, second);
  assert.equal(compared.moved, 0);
  assert.deepEqual(marks(compared.newer, second.pieces), ["only", "kept", "kept", "kept"]);
  assert.equal(compared.sentence, "Version 2 keeps all 3 of version 1's shots and adds 1. 12 s → 14 s.");
});

test("a clip deleted from the catalogue is matched by its name, and never with one that is still there", () => {
  const gone = (clip, in_s, out_s) => piece(clip, in_s, out_s, { asset_id: null });
  const first = version(1, [gone("DJI_0644", 0, 4), piece("DJI_0650", 0, 4)]);
  const second = version(2, [gone("DJI_0644", 0, 4), gone("DJI_0650", 0, 4)]);
  const compared = compareVersions(first, second);
  assert.deepEqual(marks(compared.newer, second.pieces), ["kept", "only"]);
  assert.deepEqual(marks(compared.older, first.pieces), ["kept", "only"]);
});

test("the sentence says so when nothing changed, when nothing was kept, and when there is nothing", () => {
  const same = [piece("A", 0, 4), piece("B", 0, 4)];
  assert.equal(
    compareVersions(version(1, same), version(3, same.map((each) => ({ ...each })))).sentence,
    "Version 3 has the same 2 shots as version 1, in the same order. 8 s → 8 s.",
  );
  assert.equal(
    compareVersions(version(1, [piece("A", 0, 4), piece("B", 0, 4)]), version(2, [piece("C", 0, 3)])).sentence,
    "Version 2 keeps none of version 1's 2 shots: it drops all 2 and adds 1. 8 s → 3 s.",
  );
  assert.equal(
    compareVersions(version(1, [piece("A", 0, 4), piece("B", 0, 4)]), version(2, [piece("A", 0, 4)])).sentence,
    "Version 2 keeps 1 of version 1's 2 shots and drops 1. 8 s → 4 s.",
  );
  assert.equal(
    compareVersions(version(1, [piece("A", 0, 4)]), version(2, [piece("A", 1, 4)])).sentence,
    "Version 2 keeps version 1's one shot (1 trimmed). 4 s → 3 s.",
  );
  assert.equal(compareVersions(version(1, []), version(2, [])).sentence, "Neither version has any shots.");
  assert.equal(
    compareVersions(version(1, []), version(2, [piece("A", 0, 4)])).sentence,
    "Version 2 has 1 shot; version 1 has none. 0 s → 4 s.",
  );
});

test("a row's pills say what became of the shot, and a trimmed one what it was", () => {
  const first = version(1, [piece("A", 3, 7.5), piece("B", 0, 4), piece("C", 0, 4), piece("D", 0, 4)]);
  // A trimmed where it stood; D brought forward past B; C gone; Z new.
  const second = version(2, [piece("A", 3, 6), piece("D", 0, 4), piece("B", 0, 4), piece("Z", 0, 2)]);
  const compared = compareVersions(first, second);
  const of = (side, list, at) => side.get(list[at].position);
  const words = (mark, otherVersion) => markPills(mark, otherVersion).map((pill) => [pill.text, pill.tone]);

  const trimmed = of(compared.newer, second.pieces, 0);
  assert.deepEqual(words(trimmed, 1), [["trimmed", "warn"]]);
  assert.equal(trimNote(trimmed, "newer", 1), "was 3.0–7.5 s");
  assert.equal(trimNote(of(compared.older, first.pieces, 0), "older", 2), "3.0–6.0 s in version 2");

  // Two shots that changed places: one of the pair is called moved, on both sides.
  const moved = [1, 2].map((at) => of(compared.newer, second.pieces, at)).filter((mark) => mark.moved);
  assert.equal(moved.length, 1);
  assert.equal(compared.moved, 1);
  assert.deepEqual(words(moved[0], 1), [
    ["kept", "ok"],
    ["moved", null],
  ]);
  assert.match(markPills(moved[0], 1)[1].title, /shot [24] of version 1/);
  assert.equal(trimNote(moved[0], "newer", 1), null);
  assert.equal([...compared.older.values()].filter((mark) => mark.moved).length, 1);

  const only = of(compared.newer, second.pieces, 3);
  assert.deepEqual(markPills(only, 1), [
    { text: "only here", tone: null, title: "Version 1 has no shot like this one." },
  ]);
  assert.equal(trimNote(only, "newer", 1), null);
  assert.deepEqual(words(of(compared.older, first.pieces, 2), 2), [["only here", null]]);
});

test("the address says which two versions are side by side, by their numbers", () => {
  const versions = [
    { id: "a", version: 1, parent_id: null },
    { id: "b", version: 2, parent_id: "a" },
    { id: "d", version: 4, parent_id: "gone" },
  ];
  const [one, two, four] = versions;
  assert.equal(compareHref("a", two, one), "/cuts/a?v=2&with=1");
  assert.equal(comparedWith(versions, two, "1"), one);
  assert.equal(comparedWith(versions, two, " 4 "), four);
  // No comparison asked for, one with itself, a number that is gone, and nonsense.
  assert.equal(comparedWith(versions, two, null), null);
  assert.equal(comparedWith(versions, two, "2"), null);
  assert.equal(comparedWith(versions, two, "3"), null);
  assert.equal(comparedWith(versions, two, "1; drop"), null);
});

test("Compare starts with the version this one revises, else its neighbour", () => {
  const versions = [
    { id: "a", version: 1, parent_id: null },
    { id: "b", version: 2, parent_id: "a" },
    { id: "c", version: 3, parent_id: "a" },
    { id: "d", version: 4, parent_id: "gone" },
  ];
  const [one, two, three, four] = versions;
  assert.equal(compareDefault(versions, three), one); // its parent, not version 2
  assert.equal(compareDefault(versions, four), three); // the parent was deleted
  assert.equal(compareDefault(versions, one), two); // the first: the one after
  assert.equal(compareDefault([one], one), null);
  assert.equal(compareDefault(versions, two), one);
});

test("choosing a version while comparing keeps a comparison, swapping when it is the other one", () => {
  const one = { version: 1 };
  const two = { version: 2 };
  const three = { version: 3 };
  assert.deepEqual(compareAfterShow(two, one, three), { shown: three, other: one });
  assert.deepEqual(compareAfterShow(two, one, one), { shown: one, other: two });
});
