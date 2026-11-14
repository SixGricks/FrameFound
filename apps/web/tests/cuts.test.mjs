// Run with `npm test` (Node's own test runner; nothing to install).
//
// The Cuts pages' words and sums (src/lib/cuts.ts): what a draft says, when
// the list is read again, which drafts ended between two readings, the line
// about who will draft, the folder picker's notes and the request the form
// sends. Every sentence about a draft's state is the server's; these check
// that the page only words it and never makes one up.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CUTS_FAST_MS,
  CUTS_FIRST,
  CUTS_MOST,
  CUTS_SLOW_MS,
  NO_SHOTS_FOLDER,
  REVISED_GONE,
  STOPPING,
  askClaudeText,
  askedLine,
  blankForm,
  clockOf,
  cutsShown,
  draftBody,
  draftSentence,
  editorLine,
  endedDrafts,
  endedSentence,
  folderFromKey,
  folderHint,
  folderKey,
  folderLabel,
  folderReadiness,
  footageWords,
  formFromDraft,
  joinPages,
  laterPages,
  lengthNote,
  moreCuts,
  offerFurther,
  offerLine,
  pollEvery,
  readAgainIn,
  refusalLink,
  revisedIsGone,
  revisesLine,
  secondsText,
  sourceLabel,
} from "../src/lib/cuts.ts";

const LIB = "8137ff04-7f27-4433-9646-4e7e8c4e3e67";

function draft(overrides = {}) {
  return {
    id: "d1",
    state: "queued",
    stopping: false,
    message: "",
    title: "Pete Dye — 30 second teaser",
    brief: "Thirty seconds of smooth aerials at golden hour.",
    scope: {
      kind: "folder",
      library_id: LIB,
      folder: "Pete Dye",
      contains: "",
      asset_ids: [],
      label: "GELCO › Pete Dye",
    },
    target_s: 30,
    min_shot_s: 1.5,
    max_shot_s: 8,
    exclude_used: false,
    shots_offered: 0,
    shots_in_scope: 0,
    clips: 0,
    seconds: null,
    revises_cut_id: null,
    ...overrides,
  };
}

function editor(overrides = {}) {
  return {
    mode: "ready",
    note: null,
    model_name: "gemma-4-12B-it-OptiQ-4bit",
    runs: "now",
    starts_at: null,
    paused: false,
    why_not: null,
    ...overrides,
  };
}

test("seconds are written plainly, a tenth at most, and nothing is a dash", () => {
  assert.equal(secondsText(30), "30 s");
  assert.equal(secondsText(31.5), "31.5 s");
  assert.equal(secondsText(29.84), "29.8 s");
  assert.equal(secondsText(7.999), "8 s");
  assert.equal(secondsText(null), "—");
  assert.equal(secondsText(Number.NaN), "—");
});

test("who made a cut is named as the page names it", () => {
  assert.equal(sourceLabel("local"), "Local draft");
  assert.equal(sourceLabel("mcp"), "Claude");
  assert.equal(sourceLabel("import"), "Imported timeline");
});

test("a draft shorter than asked says by how much and that nothing was added", () => {
  assert.equal(
    lengthNote(24, 30),
    "It is 6 s short of the 30 s asked for. FrameFound never adds shots the Editor did not choose.",
  );
  assert.equal(lengthNote(29.9, 30), null, "a tenth short is not short");
  assert.equal(lengthNote(31.5, 30), null);
  assert.equal(lengthNote(10, null), null);
});

test("the list is read quickly while a draft moves and slowly while it only waits", () => {
  assert.equal(pollEvery({ drafts: [] }), CUTS_SLOW_MS);
  assert.equal(pollEvery({ drafts: [draft({ state: "queued" })] }), CUTS_FAST_MS);
  assert.equal(pollEvery({ drafts: [draft({ state: "running" })] }), CUTS_FAST_MS);
  // Waiting for 21:30 changes nothing for hours; asking every 3 s would be noise.
  assert.equal(pollEvery({ drafts: [draft({ state: "waiting" })] }), CUTS_SLOW_MS);
  assert.equal(pollEvery({ drafts: [draft({ state: "failed" })] }), CUTS_SLOW_MS);
  assert.equal(readAgainIn(null, true), CUTS_SLOW_MS, "a failed first read is tried again");
  assert.equal(readAgainIn(null, false), null);
  assert.equal(readAgainIn({ drafts: [draft({ state: "running" })] }, true), CUTS_FAST_MS);
});

test("a draft that left the list or stopped being active is one that ended", () => {
  const before = {
    drafts: [
      draft({ id: "a", state: "running" }),
      draft({ id: "b", state: "waiting" }),
      draft({ id: "c", state: "queued" }),
      draft({ id: "old", state: "failed" }),
    ],
  };
  const after = {
    drafts: [
      draft({ id: "b", state: "waiting" }),
      draft({ id: "c", state: "failed" }),
      draft({ id: "old", state: "failed" }),
    ],
  };
  // "a" became a cut and left the list; "c" failed; "old" had already ended.
  assert.deepEqual(endedDrafts(before, after), ["a", "c"]);
  assert.deepEqual(endedDrafts(null, after), [], "the first reading reports nothing");
});

test("how a draft ended is said in one sentence with its title", () => {
  assert.equal(endedSentence(draft({ state: "done" })), "“Pete Dye — 30 second teaser” is ready.");
  assert.equal(
    endedSentence(draft({ state: "failed", message: "The Editor answered, but not in the form asked for." })),
    "“Pete Dye — 30 second teaser” did not finish. The Editor answered, but not in the form asked for.",
  );
  assert.equal(endedSentence(draft({ state: "cancelled" })), "“Pete Dye — 30 second teaser” was cancelled.");
});

test("a draft card leads with the server's sentence, except while a cancel waits", () => {
  const waiting = draft({
    state: "waiting",
    message: "Waiting for the overnight window (starts 21:30): the Editor needs 10.1 GB.",
  });
  assert.equal(draftSentence(waiting), waiting.message);
  assert.equal(draftSentence(draft({ state: "running", message: "The Editor is drafting it.", stopping: true })), STOPPING);
  // A stopping flag on a draft that has ended means nothing any more.
  assert.equal(draftSentence(draft({ state: "cancelled", message: "Cancelled.", stopping: true })), "Cancelled.");
});

test("without a server sentence a draft is worded from its state and figures", () => {
  assert.equal(draftSentence(draft({ state: "queued" })), "Waiting its turn — the Editor does one thing at a time.");
  assert.equal(draftSentence(draft({ state: "running", shots_offered: 62 })), "The Editor is choosing from 62 shots.");
  assert.equal(draftSentence(draft({ state: "running" })), "The Editor is drafting it.");
  assert.equal(draftSentence(draft({ state: "done", seconds: 31.5 })), "Ready: 31.5 s.");
  assert.equal(draftSentence(draft({ state: "cancelled" })), "Cancelled.");
});

test("what the Editor chose from is said only once it is known", () => {
  assert.equal(offerLine(draft()), null);
  assert.equal(
    offerLine(draft({ shots_offered: 62, shots_in_scope: 480, clips: 41 })),
    "The Editor is offered 62 of the 480 analysed shots, from 41 clips.",
  );
  assert.equal(
    offerLine(draft({ shots_offered: 8, shots_in_scope: 8, clips: 1 })),
    "The Editor is offered 8 analysed shots, from 1 clip.",
  );
  assert.equal(askedLine(draft({ exclude_used: true })), "30 s · shots 1.5 s to 8 s · leaving out shots already used");
});

test("Ask Claude instead words the brief and the footage, and attaches nothing", () => {
  assert.equal(
    askClaudeText(draft()),
    "Please make a rough cut for Premiere from the folder GELCO › Pete Dye, about 30 s long. " +
      "The brief: Thirty seconds of smooth aerials at golden hour.",
  );
  const ids = Array.from({ length: 32 }, (_, i) => `${String(i).padStart(8, "0")}-aaaa-bbbb-cccc-dddddddddddd`);
  const ticked = askClaudeText(
    draft({ scope: { kind: "selection", library_id: null, folder: "", contains: "", asset_ids: ids, label: "32 clips ticked in Search" } }),
  );
  assert.match(ticked, /these clips \(00000000, 00000001, .*00000029 and 2 more\)/);
  assert.doesNotMatch(ticked, /00000030/, "at most 30 ids are written out");
  assert.match(
    askClaudeText(draft({ scope: { kind: "folder", library_id: null, folder: "", contains: "Pete", asset_ids: [], label: "" } })),
    /the folders matching "Pete"/,
  );
});

test("the Mac's wall clock is read without a time zone", () => {
  // No Date: the server's 21:30 is the Mac's 21:30, wherever the browser is.
  assert.equal(clockOf("2026-11-14T21:30:00"), "21:30");
  assert.equal(clockOf(null), null);
  assert.equal(clockOf("tonight"), null);
});

test("the line about who drafts says now, overnight, paused, or why not", () => {
  assert.deepEqual(editorLine(editor()), {
    tone: "ok",
    pill: null,
    text: "gemma-4-12B-it-OptiQ-4bit will draft it. It can take a few minutes; you can leave this page.",
  });
  const night = editorLine(editor({ runs: "overnight", starts_at: "2026-11-13T21:30:00" }));
  assert.equal(night.tone, "wait");
  assert.equal(night.pill, "overnight only");
  assert.match(night.text, /^The Editor \(gemma-4-12B-it-OptiQ-4bit\) needs more memory than the daytime limit, so a draft you ask for now starts at 21:30\./);
  // Overnight-only at night: no start time from the server, so it runs now.
  assert.equal(editorLine(editor({ runs: "overnight", starts_at: null })).tone, "ok");
  assert.equal(editorLine(editor({ paused: true, runs: "overnight", starts_at: "2026-11-13T21:30:00" })).pill, "paused");
  const blocked = editorLine(editor({ why_not: "No model has the Editor role yet. Give one the role on the AI models page." }));
  assert.equal(blocked.tone, "alert");
  assert.equal(blocked.text, "No model has the Editor role yet. Give one the role on the AI models page.");
});

test("a folder is named with its library and its clips counted", () => {
  const folder = { library_id: LIB, library_name: "GELCO", path: "2026/Pete Dye", clips: 41, analysed: 38 };
  assert.equal(folderLabel(folder), "GELCO › 2026/Pete Dye");
  assert.equal(folderHint(folder), "41 clips");
  assert.equal(folderHint({ clips: 1 }), "1 clip");
  assert.deepEqual(folderReadiness(folder), {
    text: "38 of its 41 clips have been analysed into shots; the other 3 are not offered yet.",
    blocks: false,
  });
  assert.equal(folderReadiness({ ...folder, analysed: 41 }), null);
  assert.deepEqual(folderReadiness({ ...folder, analysed: 0 }), { text: NO_SHOTS_FOLDER, blocks: true });
  assert.equal(
    folderReadiness({ ...folder, clips: 2, analysed: 1 }).text,
    "1 of its 2 clips has been analysed into shots; the other 1 is not offered yet.",
  );
});

test("the form sends a folder request with the server's field names", () => {
  const form = {
    ...blankForm(),
    brief: "  Smooth aerials, opening on the clubhouse. ",
    folder: { library_id: LIB, path: "Pete Dye" },
    lengthText: "45",
    shortestText: "2,5",
    excludeUsed: true,
  };
  assert.deepEqual(draftBody(form), {
    body: {
      brief: "Smooth aerials, opening on the clubhouse.",
      title: "",
      scope: { kind: "folder", library_id: LIB, folder: "Pete Dye" },
      target_s: 45,
      min_shot_s: 2.5,
      max_shot_s: 8,
      exclude_used: true,
    },
  });
  const again = draftBody({ ...form, revises: "cut-1", assetIds: ["a", "b"] });
  assert.deepEqual(again.body.scope, { kind: "selection", asset_ids: ["a", "b"] }, "ticked clips win over a folder");
  assert.equal(again.body.revises, "cut-1");
});

test("the form says what to change instead of sending what the server would refuse", () => {
  const ok = { ...blankForm(), brief: "Aerials.", folder: { library_id: LIB, path: "Pete Dye" } };
  assert.deepEqual(draftBody({ ...ok, brief: "   " }), { problem: "Say what the video should be first." });
  assert.deepEqual(draftBody({ ...ok, folder: null }), { problem: "Choose a folder of footage first." });
  assert.deepEqual(draftBody({ ...ok, lengthText: "4" }), { problem: "The length is seconds, from 5 to 600." });
  assert.deepEqual(draftBody({ ...ok, lengthText: "thirty" }), { problem: "The length is seconds, from 5 to 600." });
  assert.deepEqual(draftBody({ ...ok, shortestText: "9" }), {
    problem: "The shortest shot cannot be longer than the longest.",
  });
  assert.deepEqual(draftBody({ ...ok, longestText: "61" }), { problem: "The longest shot is seconds, from 1 to 60." });
  assert.match(draftBody({ ...ok, brief: "x".repeat(1001) }).problem, /at most 1000 are read/);
});

test("Try again fills the form in as the draft was asked for", () => {
  const form = formFromDraft(draft({ target_s: 60, min_shot_s: 2, max_shot_s: 6, exclude_used: true, revises_cut_id: "cut-9" }));
  assert.deepEqual(form.folder, { library_id: LIB, path: "Pete Dye" });
  assert.equal(form.assetIds, null);
  assert.equal(form.lengthText, "60");
  assert.equal(form.excludeUsed, true);
  assert.equal(form.revises, "cut-9");
  assert.equal(form.title, "Pete Dye — 30 second teaser");
  assert.ok("body" in draftBody(form), "a draft that was accepted once is a form that sends");
});

test("a refusal points where it can be put right", () => {
  assert.deepEqual(refusalLink(409, "No model has the Editor role yet. Give one the role on the AI models page."), {
    href: "/models#roles",
    label: "AI models",
  });
  assert.equal(refusalLink(409, "A draft with this title for this footage is already waiting."), null);
  assert.deepEqual(refusalLink(400, NO_SHOTS_FOLDER), { href: "/processing", label: "Processing" });
  assert.equal(refusalLink(400, "This folder holds no video on the share now."), null);
  assert.equal(refusalLink(503, "The processing queue is unavailable. Nothing was started."), null);
});

// ---- Try again keeps the footage, whatever it was ---------------------------------------
//
// "Try again" used to come back with the brief filled in and an empty footage
// field for every draft that was not made from a picked folder — which is
// every draft Claude asks the Editor for from the chat. Draft then answered
// "Choose a folder of footage first." and the same clips could not be asked
// for again.

test("Try again on a draft made from clips named one by one asks for those clips again", () => {
  const ids = ["c1", "c2", "c3"];
  const scope = {
    kind: "selection",
    library_id: null,
    folder: "",
    contains: "",
    asset_ids: ids,
    label: "3 clips named in the chat",
  };
  const form = formFromDraft(draft({ scope }));
  assert.equal(form.folder, null);
  assert.deepEqual(form.assetIds, ids);
  assert.notEqual(form.assetIds, ids, "a copy: the page's answer is not the form's to change");
  // The form says which footage it holds, in the draft's own words.
  assert.equal(footageWords(form), "The 3 clips named in the chat");
  assert.equal(footageWords({ ...form, footageLabel: null }), "The 3 clips it was asked for");
  assert.equal(footageWords({ ...form, assetIds: ["c1"], footageLabel: null }), "The 1 clip it was asked for");
  const made = draftBody(form);
  assert.ok("body" in made, "the form sends as it stands, with no folder chosen");
  assert.deepEqual(made.body.scope, { kind: "selection", asset_ids: ids });
});

test("Try again on the chat's \"folders matching\" asks for the same folders", () => {
  const scope = {
    kind: "folder",
    library_id: LIB,
    folder: "",
    contains: "Pete Dye",
    asset_ids: [],
    label: 'GELCO › folders matching "Pete Dye"',
  };
  const form = formFromDraft(draft({ scope }));
  assert.equal(form.folder, null);
  assert.equal(form.assetIds, null);
  assert.deepEqual(form.matching, { library_id: LIB, contains: "Pete Dye" });
  assert.equal(footageWords(form), 'GELCO › folders matching "Pete Dye"');
  assert.deepEqual(draftBody(form).body.scope, { kind: "folder", contains: "Pete Dye", library_id: LIB });

  // In any library: no library is sent, and the words still start a line.
  const anywhere = formFromDraft(draft({ scope: { ...scope, library_id: null, label: "" } }));
  assert.equal(footageWords(anywhere), 'Folders matching "Pete Dye"');
  assert.deepEqual(draftBody(anywhere).body.scope, { kind: "folder", contains: "Pete Dye" });

  // A folder picked afterwards is the footage, and the form says nothing more of the rest.
  const picked = { ...form, folder: { library_id: LIB, path: "2026/Pete Dye" } };
  assert.equal(footageWords(picked), null);
  assert.deepEqual(draftBody(picked).body.scope, { kind: "folder", library_id: LIB, folder: "2026/Pete Dye" });
});

test("a form with nothing but a picked folder says nothing of other footage", () => {
  assert.equal(footageWords(blankForm()), null);
  assert.equal(footageWords(formFromDraft(draft())), null);
  // A scope that names nothing at all is not footage.
  const empty = { kind: "folder", library_id: LIB, folder: "", contains: "  ", asset_ids: [], label: "" };
  const form = formFromDraft(draft({ scope: empty }));
  assert.equal(form.matching, null);
  assert.deepEqual(draftBody(form), { problem: "Choose a folder of footage first." });
});

// ---- the cut a draft was to be the next version of --------------------------------------

test("a form that would add a version to a cut says which, and a deleted cut is said for what it is", () => {
  assert.equal(
    revisesLine("Pete Dye — 30 second teaser"),
    "The result will be kept as the next version of “Pete Dye — 30 second teaser”, not as a new cut.",
  );
  // Before the cut has been looked up there is still something true to say.
  assert.match(revisesLine(null), /next version of the cut this draft was for, not as a new cut\.$/);
  // "No such cut." is the only 404 a draft request gets, and only a form that revises one can get it.
  assert.equal(revisedIsGone(404, "cut-9"), true);
  assert.equal(revisedIsGone(404, null), false);
  assert.equal(revisedIsGone(400, "cut-9"), false);
  assert.equal(revisedIsGone(409, "cut-9"), false);
  assert.match(REVISED_GONE, /has been deleted, so it will be a new cut\.$/);
});

// ---- the folder picker ------------------------------------------------------------------

test("a picker row says which folder it is by itself", () => {
  // The form used to look a clicked row up in the last list it had heard
  // back; an earlier search answering late replaced that list, and the click
  // on a row that was on screen did nothing.
  const folder = { library_id: LIB, path: "2026/Pete Dye" };
  assert.deepEqual(folderFromKey(folderKey(folder)), folder);
  const odd = { library_id: LIB, path: "2026/A|B/Reel 3" };
  assert.deepEqual(folderFromKey(folderKey(odd)), odd, "a path may hold the separator; a library id cannot");
  assert.equal(folderFromKey("no separator"), null);
  assert.equal(folderFromKey("|only a path"), null);
  assert.equal(folderFromKey(`${LIB}|`), null);
});

// ---- how much of the list ---------------------------------------------------------------

test("a full list says it is only the newest, and offers the older cuts", () => {
  assert.deepEqual(cutsShown(12, CUTS_FIRST), { text: "12 shown", more: false, note: null });
  assert.deepEqual(cutsShown(0, CUTS_FIRST), { text: "0 shown", more: false, note: null });
  // Thirty of thirty asked for: there may be a thirty-first.
  assert.deepEqual(cutsShown(30, 30), { text: "the newest 30", more: true, note: null });
  assert.equal(moreCuts(30), 60);
  assert.equal(moreCuts(60), 120);
  assert.equal(moreCuts(120), CUTS_MOST);
  assert.equal(moreCuts(CUTS_MOST), CUTS_MOST);
  // Everything came back with room to spare: nothing older to offer.
  assert.deepEqual(cutsShown(41, 60), { text: "41 shown", more: false, note: null });
  // At the most the server lists there is no more to ask for, and the page says so.
  const most = cutsShown(CUTS_MOST, CUTS_MOST);
  assert.equal(most.text, "the newest 200");
  assert.equal(most.more, false);
  assert.match(most.note, /^Only the newest 200 cuts can be listed here\./);
});

test("past the most one answer holds, the server says whether there is more, and Show more asks for it", () => {
  // Below the most, nothing changes whatever the server says.
  assert.deepEqual(cutsShown(30, 30, false), { text: "the newest 30", more: true, note: null });
  assert.deepEqual(cutsShown(12, CUTS_FIRST, true), { text: "12 shown", more: false, note: null });
  assert.equal(offerFurther(120, true), false, "under 200 it is Show older cuts that asks");
  // At 200 with more after them: "Show more", and no sentence that the list ends.
  assert.deepEqual(cutsShown(200, CUTS_MOST, true), { text: "the newest 200", more: false, note: null });
  assert.equal(offerFurther(CUTS_MOST, true), true);
  // Exactly 200, or every page brought: that is all of them, said plainly.
  assert.deepEqual(cutsShown(200, CUTS_MOST, false), { text: "200 shown", more: false, note: null });
  assert.equal(offerFurther(CUTS_MOST, false), false);
  assert.deepEqual(cutsShown(437, CUTS_MOST, false), { text: "437 shown", more: false, note: null });
  assert.deepEqual(cutsShown(400, CUTS_MOST, true), { text: "the newest 400", more: false, note: null });
  // A server from before paging says nothing: no button that could not work.
  assert.equal(offerFurther(CUTS_MOST, undefined), false);
});

test("the later pages are asked for one answer at a time, after the first", () => {
  assert.deepEqual(laterPages(0), []);
  assert.deepEqual(laterPages(CUTS_MOST), [{ offset: 200, limit: 200 }]);
  assert.deepEqual(laterPages(2 * CUTS_MOST), [
    { offset: 200, limit: 200 },
    { offset: 400, limit: 200 },
  ]);
});

test("a cut that arrives on two pages is listed once, and the last page says whether there is more", () => {
  const family = (root_id) => ({ root_id, title: root_id });
  // A new cut was made between the two requests: "b" slid onto the next page too.
  const joined = joinPages(
    { cuts: [family("a"), family("b")], more: true },
    [{ cuts: [family("b"), family("c")], more: false }],
  );
  assert.deepEqual(
    joined.cuts.map((one) => one.root_id),
    ["a", "b", "c"],
  );
  assert.equal(joined.more, false);
  assert.equal(joinPages({ cuts: [family("a")], more: true }, []).more, true);
});
