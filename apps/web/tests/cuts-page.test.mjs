// Run with `npm test` (Node's own test runner; nothing to install).
//
// One cut's page (src/lib/cuts.ts, the second half): which version an
// address names, what a version and a piece say, which shots the viewer
// steps to, what a download says, and what Draft again and Delete do. A
// piece's note and a shot's words come from a model or a file name; these
// check they are only ever joined into text.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  afterDelete,
  canLook,
  deleteQuestion,
  draftBody,
  formFromVersion,
  goneSentence,
  madeLine,
  momentHref,
  noteLead,
  otherVersions,
  pieceDetail,
  pieceHeading,
  pieceSteps,
  selectionOf,
  sendLines,
  sentElsewhere,
  shotWords,
  shownVersion,
  spanText,
  totalLine,
  versionButton,
  versionHref,
  versionLine,
} from "../src/lib/cuts.ts";

const LIB = "8137ff04-7f27-4433-9646-4e7e8c4e3e67";
const V1 = "11111111-1111-4111-8111-111111111111";
const V2 = "22222222-2222-4222-8222-222222222222";
const V3 = "33333333-3333-4333-8333-333333333333";

function madeFrom(overrides = {}) {
  return {
    brief: "Thirty seconds of smooth aerials at golden hour.",
    scope: {
      kind: "folder",
      library_id: LIB,
      folder: "2026/Pete Dye",
      contains: "",
      asset_ids: [],
      label: "GELCO › 2026/Pete Dye",
    },
    target_s: 30,
    min_shot_s: 1.5,
    max_shot_s: 8,
    shots_offered: 62,
    shots_in_scope: 480,
    seconds: 48,
    ...overrides,
  };
}

function version(overrides = {}) {
  return {
    id: V1,
    version: 1,
    source: "local",
    label: "Local draft",
    title: "Pete Dye — 30 second teaser",
    duration_s: 31.5,
    piece_count: 9,
    provenance: {
      model_name: "gemma-4-12B-it-OptiQ-4bit",
      revision_short: "de9e183",
      runtime: "mlx-vlm",
      runtime_version: "0.7.12",
      prompt_version: "editor-1",
    },
    draft: madeFrom(),
    ...overrides,
  };
}

function piece(position, overrides = {}) {
  return {
    position,
    asset_id: `a${position}`,
    clip: `DJI_06${30 + position}`,
    available: true,
    in_s: 3,
    out_s: 7.5,
    shot: { move: "orbit", direction: "right", light: "golden hour", sky: "clear", subjects: ["green", "bunker"] },
    ...overrides,
  };
}

// ---- which version -------------------------------------------------------------------

test("the address names a version by its number, and the number outlives a deletion", () => {
  const versions = [
    { id: V1, version: 1 },
    { id: V3, version: 3 },
  ];
  // ?v= wins over the id in the address: the buttons only change ?v=.
  assert.equal(shownVersion(versions, V1, "3")?.id, V3);
  // No ?v=: the version whose id was opened.
  assert.equal(shownVersion(versions, V3, null)?.id, V3);
  // Version 2 was deleted: a link to it falls back to the id opened, not to
  // "the second in the list" (which is version 3).
  assert.equal(shownVersion(versions, V1, "2")?.id, V1);
  // Anything that is not a number is ignored rather than read as one.
  for (const junk of ["", "two", "1e0", "-1", "1.0", " ", "9999999"]) {
    assert.equal(shownVersion(versions, V1, junk)?.id, V1, JSON.stringify(junk));
  }
  // The id in the address is gone from the answer (it cannot be, but the
  // page must still show something): the newest.
  assert.equal(shownVersion(versions, "gone", null)?.id, V3);
  assert.equal(shownVersion([], V1, "1"), null);
});

test("a version's address keeps the id that was opened and only changes ?v=", () => {
  assert.equal(versionHref(V1, { version: 2 }), `/cuts/${V1}?v=2`);
});

test("a version says its place, who made it, and how long it is", () => {
  const two = [{ version: 1 }, { version: 2 }];
  assert.equal(
    versionLine(version({ version: 2, source: "mcp", label: "Claude", piece_count: 8, duration_s: 29.8 }), two),
    "version 2 of 2 · Claude · 8 shots · 29.8 s",
  );
  assert.equal(versionLine(version({ piece_count: 1, duration_s: 4 }), [{ version: 1 }]), "version 1 of 1 · Local draft · 1 shot · 4 s");
  // After version 2 was deleted, "version 3 of 2" would be nonsense.
  assert.equal(
    versionLine(version({ version: 3 }), [{ version: 1 }, { version: 3 }]),
    "version 3 · 2 versions kept · Local draft · 9 shots · 31.5 s",
  );
  assert.equal(versionButton({ version: 2, source: "mcp", label: "Claude" }), "2 · Claude");
  // An install whose server sends no label still names the maker.
  assert.equal(versionButton({ version: 1, source: "import", label: "" }), "1 · Imported timeline");
});

// ---- who drafted it ------------------------------------------------------------------

test("a local draft names the build that drafted it, how long it took and from how much", () => {
  assert.equal(
    madeLine(version()),
    "gemma-4-12B-it-OptiQ-4bit (build de9e183, on MLX-VLM 0.7.12, prompt editor-1) drafted this " +
      "in 48 s from 62 of the 480 shots in GELCO › 2026/Pete Dye.",
  );
  // Every shot in the folder was offered: no "of the".
  assert.match(madeLine(version({ draft: madeFrom({ shots_in_scope: 62 }) })), /from 62 shots in GELCO/);
  // Clips ticked in Search read as a thing, not a place.
  assert.match(
    madeLine(
      version({
        draft: madeFrom({ scope: { ...madeFrom().scope, kind: "selection", label: "6 clips ticked in Search" } }),
      }),
    ),
    /in the 6 clips ticked in Search\.$/,
  );
});

test("what the server did not record about a draft is left out, never guessed", () => {
  // The demonstration is no program: it is not named as one.
  assert.equal(
    madeLine(
      version({
        provenance: { model_name: "demo-editor", revision_short: null, runtime: "demo", runtime_version: "1", prompt_version: null },
        draft: madeFrom({ seconds: null, shots_offered: 0 }),
      }),
    ),
    "demo-editor drafted this.",
  );
  assert.equal(madeLine(version({ provenance: null, draft: null })), "A local model drafted this.");
  // llama.cpp by its own name; a program this page has not heard of, as sent.
  assert.match(
    madeLine(version({ provenance: { ...version().provenance, runtime: "llama-cpp", runtime_version: null } })),
    /on llama\.cpp, prompt/,
  );
  assert.match(madeLine(version({ provenance: { ...version().provenance, runtime: "other" } })), /on other 0\.7\.12/);
  // Claude's cut and an imported timeline were not drafted by a local model.
  assert.equal(madeLine(version({ source: "mcp" })), null);
  assert.equal(madeLine(version({ source: "import" })), null);
});

// ---- a piece -------------------------------------------------------------------------

test("a shot is worded as the shot lines word it", () => {
  assert.equal(spanText(3, 7.5), "3.0–7.5 s");
  assert.equal(shotWords(piece(1).shot), "orbit right · golden hour · green, bunker");
  // A static shot has no direction worth saying; the sky only when overcast.
  assert.equal(
    shotWords({ move: "static", direction: "left", light: null, sky: "overcast", subjects: [] }),
    "static · overcast",
  );
  assert.equal(shotWords({ move: null, direction: "left", light: null, sky: null, subjects: [] }), "");
  assert.equal(shotWords(null), "");
  assert.equal(pieceDetail(piece(1)), "3.0–7.5 s · orbit right · golden hour · green, bunker");
  // An imported timeline's piece has no snapshot: the span alone.
  assert.equal(pieceDetail(piece(1, { shot: null })), "3.0–7.5 s");
  assert.equal(pieceHeading(piece(3), 3, 9), "Shot 3 of 9 — DJI_0633");
});

test("a piece's link opens the clip's page at its in point", () => {
  // Whole seconds: the clip's page reads `t` as a number of seconds.
  assert.equal(momentHref("abc", 7.9), "/assets/abc?t=7");
  assert.equal(momentHref("abc", 0.4), "/assets/abc");
  assert.equal(momentHref("abc", 0), "/assets/abc");
});

test("only a deleted clip cannot be opened; one off the share still can", () => {
  const offline = piece(2, { available: false });
  const deleted = piece(3, { asset_id: null, available: false });
  assert.equal(canLook(piece(1)), true);
  assert.equal(canLook(offline), true);
  assert.equal(canLook(deleted), false);
  assert.equal(goneSentence(piece(1)), null);
  assert.equal(goneSentence(offline), "This clip is not on the share right now.");
  assert.equal(goneSentence(deleted), "This clip has been deleted from the catalogue.");
});

test("the viewer steps through the cut in order and passes over a deleted clip", () => {
  const pieces = [
    piece(1),
    piece(2, { asset_id: null }),
    piece(3),
    piece(4, { available: false }),
    piece(5, { asset_id: null }),
  ];
  const first = pieceSteps(pieces, 1);
  assert.deepEqual([first.at, first.count, first.prev, first.next.position], [1, 5, null, 3]);
  const third = pieceSteps(pieces, 3);
  assert.deepEqual([third.at, third.prev.position, third.next.position], [3, 1, 4]);
  // The last piece that can be opened has nowhere further to go.
  const fourth = pieceSteps(pieces, 4);
  assert.deepEqual([fourth.at, fourth.prev.position, fourth.next], [4, 3, null]);
  // Positions are the server's and need not start at 1 or be unbroken.
  const gapped = [piece(10), piece(30)];
  assert.deepEqual([pieceSteps(gapped, 30).at, pieceSteps(gapped, 30).prev.position], [2, 10]);
  // The piece left the version while the viewer was open.
  assert.equal(pieceSteps(pieces, 99), null);
});

test("a note is marked with whose words it is", () => {
  assert.equal(noteLead("local"), "Editor:");
  assert.equal(noteLead("mcp"), "Claude:");
  assert.equal(noteLead("import"), "Note:");
});

test("the length is set against what was asked for, and a short draft says why nothing was added", () => {
  assert.equal(totalLine(31.5, 30), "31.5 s of the 30 s asked for.");
  assert.equal(
    totalLine(26, 30),
    "26 s in all. It is 4 s short of the 30 s asked for. FrameFound never adds shots the Editor did not choose.",
  );
  // Claude's cut and an imported timeline were asked for no length.
  assert.equal(totalLine(29.8, null), "29.8 s in all.");
  assert.equal(totalLine(29.8, undefined), "29.8 s in all.");
});

// ---- the Premiere file ---------------------------------------------------------------

test("a download says what the server said, first and unchanged", () => {
  const sent = {
    filename: "Pete Dye - 30 second teaser.xml",
    note: "9 cuts, 31.5s. 2 file(s) have no path for profile 'DJ PC'; they use the server's path and will import offline until relinked.",
    missing: [],
    offline: [],
  };
  assert.deepEqual(sendLines(sent, false), [
    "Pete Dye - 30 second teaser.xml: 9 cuts, 31.5s. 2 file(s) have no path for profile 'DJ PC'; " +
      "they use the server's path and will import offline until relinked. In Premiere, File → Import.",
  ]);
});

test("a download names the clips left out and the clips that are away", () => {
  const lines = sendLines(
    {
      filename: "t.xml",
      note: "7 cuts, 24.0s.",
      missing: ["DJI_0644.MP4"],
      offline: ["DJI_0650.MP4", "DJI_0651.MP4"],
    },
    false,
  );
  assert.equal(lines.length, 3);
  assert.equal(lines[1], "1 clip has been deleted from the catalogue and was left out: DJI_0644.MP4.");
  assert.equal(
    lines[2],
    "2 clips are not on the share right now: DJI_0650.MP4, DJI_0651.MP4. Premiere shows them offline until the share is back.",
  );
  // A long list is cut short and says how many more.
  const many = sendLines(
    { filename: "t.xml", note: "1 cuts, 2.0s.", missing: ["a", "b", "c", "d", "e", "f", "g"], offline: ["z"] },
    false,
  );
  assert.equal(many[1], "7 clips have been deleted from the catalogue and were left out: a, b, c, d, e and 2 more.");
  assert.match(many[2], /^1 clip is not on the share right now: z\. Premiere shows it offline/);
});

test("only the first download of a local draft says its shots now count as used", () => {
  const sent = { filename: "t.xml", note: "9 cuts, 31.5s.", missing: [], offline: [] };
  assert.equal(sendLines(sent, true).length, 2);
  assert.match(sendLines(sent, true)[1], /marked as used, so the next draft can avoid them/);
  assert.equal(sendLines(sent, false).length, 1);
});

// ---- drafting again, and deleting ----------------------------------------------------

test("Draft again asks for the next version of the same cut, from the same footage", () => {
  const { form, selection } = formFromVersion(version({ id: V2 }));
  assert.equal(selection, null);
  assert.equal(form.revises, V2);
  assert.equal(form.brief, "Thirty seconds of smooth aerials at golden hour.");
  assert.deepEqual(form.folder, { library_id: LIB, path: "2026/Pete Dye" });
  assert.deepEqual([form.lengthText, form.shortestText, form.longestText], ["30", "1.5", "8"]);
  // What the form then sends is a request the server takes: the version it
  // revises goes with it, so the result is the next version here.
  const made = draftBody(form);
  assert.ok("body" in made);
  assert.equal(made.body.revises, V2);
  assert.deepEqual(made.body.scope, { kind: "folder", library_id: LIB, folder: "2026/Pete Dye" });
});

test("Draft again on a cut made from ticked clips keeps those clips", () => {
  const ids = ["c1", "c2", "c3"];
  const scope = { kind: "selection", library_id: null, folder: "", contains: "", asset_ids: ids, label: "3 clips ticked in Search" };
  const { form, selection } = formFromVersion(version({ draft: madeFrom({ scope }) }));
  assert.equal(form.folder, null);
  assert.deepEqual(selection, { assetIds: ids, label: "3 clips ticked in Search" });
  // The form holds them too, so it sends them with no page handing them over.
  assert.deepEqual(form.assetIds, ids);
  assert.equal(form.footageLabel, "3 clips ticked in Search");
  assert.deepEqual(draftBody({ ...form, brief: "Again." }).body.scope, { kind: "selection", asset_ids: ids });
  // A copy: the page's answer is not handed to the form to change.
  assert.notEqual(selection.assetIds, ids);
  assert.deepEqual(selectionOf(scope), { assetIds: ids, label: "3 clips ticked in Search" });
  assert.equal(selectionOf(madeFrom().scope), null);
  assert.equal(selectionOf({ ...scope, asset_ids: [] }), null);
});

test("a version no local model drafted opens the form with its title and nothing made up", () => {
  const { form, selection } = formFromVersion(version({ id: V3, source: "mcp", draft: null, provenance: null }));
  assert.equal(selection, null);
  assert.equal(form.revises, V3);
  assert.equal(form.title, "Pete Dye — 30 second teaser");
  assert.equal(form.brief, "");
  assert.equal(form.folder, null);
  // The chat's "folders matching" names no one folder, and is asked for
  // again as it was (it used to come back as an empty footage field).
  const matching = formFromVersion(
    version({ draft: madeFrom({ scope: { ...madeFrom().scope, folder: "", contains: "Pete" } }) }),
  );
  assert.equal(matching.form.folder, null);
  assert.equal(matching.selection, null);
  assert.deepEqual(matching.form.matching, { library_id: LIB, contains: "Pete" });
  assert.deepEqual(draftBody(matching.form).body.scope, { kind: "folder", contains: "Pete", library_id: LIB });
});

test("Delete says which version goes and what stays", () => {
  assert.equal(
    deleteQuestion("Pete Dye — 30 second teaser", 2, 2),
    "Delete version 2 of “Pete Dye — 30 second teaser”? The other version stays.",
  );
  assert.match(deleteQuestion("T", 1, 4), /The other 3 versions stay\.$/);
  // The last version: the cut itself goes, and the question says so.
  assert.match(deleteQuestion("T", 1, 1), /^Delete “T”\? It is the only version, so the cut leaves the list\./);
});

test("after a delete the page shows the newest version left, or goes back to the list", () => {
  const versions = [{ id: V1 }, { id: V2 }, { id: V3 }];
  assert.equal(afterDelete(versions, V3)?.id, V2);
  assert.equal(afterDelete(versions, V1)?.id, V3);
  assert.equal(afterDelete([{ id: V1 }], V1), null);
});

test("a version deleted somewhere else leaves the others to look at, newest first", () => {
  // The address names one version; when that one is gone the cut is not.
  const versions = [{ id: V1 }, { id: V2 }, { id: V3 }];
  assert.deepEqual(otherVersions(versions, V3), [V2, V1]);
  assert.deepEqual(otherVersions(versions, V1), [V3, V2]);
  // An id the page never knew of: every version it did know is worth trying.
  assert.deepEqual(otherVersions(versions, "gone"), [V3, V2, V1]);
  // The only version: nothing else to try, the cut is gone.
  assert.deepEqual(otherVersions([{ id: V1 }], V1), []);
});

test("a download that finishes under another version says whose file it is", () => {
  // Versions share a title, so the file's name does not tell them apart, and
  // "its shots are marked as used" must not be read as the version on screen.
  const lines = sentElsewhere({ filename: "Pete Dye.xml" }, "version 1");
  assert.deepEqual(lines, [
    "Pete Dye.xml was downloaded. It is version 1, which was on screen when Download was pressed — not the version shown now.",
  ]);
  assert.ok(lines.every((line) => !/marked as used/.test(line)));
});
