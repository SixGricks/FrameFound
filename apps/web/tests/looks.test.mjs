// Run with `npm test` (Node's own test runner; nothing to install).
//
// Folders and looking, in words and sums (src/lib/looks.ts): what a folder
// path means, the address Browse is about to have, what is beneath a folder,
// why a folder's grid is empty, how a score is tinted, and what a folder
// review says before it starts and while it runs. Every sentence about a
// review's state is the server's; these check the page only words it.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  CANCEL_QUESTION,
  LOCAL_SUGGESTIONS_FROM,
  STILLS_CHOICES,
  STILLS_NOTE,
  alreadyText,
  anythingToReview,
  browseHref,
  contentsText,
  emptyReason,
  estimateText,
  failedBeforeText,
  filesHeading,
  firstText,
  folderOf,
  folderParts,
  folderQuery,
  ingestNotes,
  ingestStopped,
  ingestText,
  lengthText,
  librariesQuery,
  localWaitingText,
  lookSettingSaved,
  lookedText,
  modelChoiceText,
  momentPicture,
  momentSeconds,
  parentOf,
  placeOf,
  preparingText,
  progressText,
  redoText,
  reviewActive,
  reviewHref,
  reviewLine,
  reviewPercent,
  reviewPlace,
  reviewStateWord,
  reviewTone,
  reviewsUnderWayText,
  scopeText,
  scoreLabel,
  scoreTone,
  shownNote,
  skippedText,
  startBlock,
  suggestedBy,
  suggestedText,
  todoItems,
  todoText,
  tonightBlock,
  tonightLabel,
  viewQuery,
  waitingOnTagsText,
  withChanges,
} from "../src/lib/looks.ts";

// ---- folders -------------------------------------------------------------------------

test("a path is each folder on the way down, and the library itself is none", () => {
  assert.deepEqual(folderParts("Pete Dye/Drone"), [
    { name: "Pete Dye", path: "Pete Dye" },
    { name: "Drone", path: "Pete Dye/Drone" },
  ]);
  assert.deepEqual(folderParts(""), []);
  assert.deepEqual(folderParts("/"), []);
});

test("only a slash at either end is dropped: a name keeps its space and its backslash", () => {
  // A folder renamed "Rev 2 " in Finder, and one named with a backslash, are
  // real folders; trimming either opens a folder that does not exist.
  assert.deepEqual(folderParts("/Brookside/Rev 2 /"), [
    { name: "Brookside", path: "Brookside" },
    { name: "Rev 2 ", path: "Brookside/Rev 2 " },
  ]);
  assert.deepEqual(folderParts("Drone\\RAW"), [{ name: "Drone\\RAW", path: "Drone\\RAW" }]);
});

test("the folder above a top folder is the library", () => {
  assert.equal(parentOf("Pete Dye/Drone/RAW"), "Pete Dye/Drone");
  assert.equal(parentOf("Pete Dye"), "");
  assert.equal(parentOf(""), "");
  assert.equal(parentOf("/Pete Dye/Drone/"), "Pete Dye");
});

test("a file's folder is its path without its name, and nothing at the top", () => {
  assert.equal(folderOf("Pete Dye/Drone/DJI_0440.mp4"), "Pete Dye/Drone");
  assert.equal(folderOf("DJI_0440.mp4"), "");
});

// ---- Browse's address ----------------------------------------------------------------

test("Browse with nothing chosen has no question mark", () => {
  assert.equal(browseHref(""), "/browse");
  assert.equal(browseHref("library=a"), "/browse?library=a");
});

test("a change starts again at page 1, and the page arrows keep the page", () => {
  assert.equal(withChanges("library=a&page=4", { media: "image" }), "library=a&media=image");
  assert.equal(withChanges("library=a&page=4", { page: "5" }, true), "library=a&page=5");
  assert.equal(withChanges("sort=name&page=2", { page: null }, true), "sort=name");
});

test("null and an empty value both take a filter out", () => {
  assert.equal(withChanges("library=a&media=image", { media: "" }), "library=a");
  assert.equal(withChanges("tag=golf&suggested=1", { tag: null, suggested: null }), "");
});

test("two quick changes built one on the other both land", () => {
  // The fault this guards: each change built on the address as it was drawn,
  // so the second undid the first.
  const first = withChanges("", { library: "a" });
  const second = withChanges(first, { media: "video" });
  const third = withChanges(second, { deep: "1" });
  assert.equal(third, "library=a&media=video&deep=1");
});

test("opening a folder keeps what was chosen about files and goes back to page 1", () => {
  const q = folderQuery("view=folders&library=a&media=image&all=1&sort=score&deep=1&page=3", "a", "Pete Dye/Drone");
  const usp = new URLSearchParams(q);
  assert.equal(usp.get("folder"), "Pete Dye/Drone");
  assert.equal(usp.get("library"), "a");
  assert.equal(usp.get("view"), "folders");
  assert.equal(usp.get("media"), "image");
  assert.equal(usp.get("all"), "1");
  assert.equal(usp.get("sort"), "score");
  assert.equal(usp.get("deep"), "1");
  assert.equal(usp.get("page"), null);
});

test("a folder opened from the flat view lands in the Folders view of its own library", () => {
  const usp = new URLSearchParams(folderQuery("library=b&tag=golf&page=2", "a", "Pete Dye"));
  assert.equal(usp.get("view"), "folders");
  assert.equal(usp.get("library"), "a");
  assert.equal(usp.get("folder"), "Pete Dye");
  assert.equal(usp.get("tag"), "golf");
  assert.equal(usp.get("page"), null);
});

test("a library's own top is the Folders view with no folder", () => {
  const usp = new URLSearchParams(folderQuery("view=folders&library=a&folder=Pete+Dye", "a", ""));
  assert.equal(usp.get("folder"), null);
  assert.equal(usp.get("library"), "a");
});

test("the list of libraries is the Folders view with no library", () => {
  assert.equal(librariesQuery("view=folders&library=a&folder=x&media=image&page=2"), "view=folders&media=image");
});

test("Everything is the flat view as it always was: no folder and no sub-folders, the library kept", () => {
  assert.equal(viewQuery("view=folders&library=a&folder=x&deep=1&sort=name&page=2", "flat"), "library=a&sort=name");
  assert.equal(viewQuery("", "flat"), "");
});

test("Folders starts at the chosen library's top, or the list of libraries", () => {
  assert.equal(viewQuery("library=a&page=3", "folders"), "library=a&view=folders");
  assert.equal(viewQuery("", "folders"), "view=folders");
});

test("a filter, the sort and the page do not change whose files are listed; the folder does", () => {
  const here = placeOf("view=folders&library=a&folder=Pete+Dye");
  assert.equal(placeOf("view=folders&library=a&folder=Pete+Dye&media=image&deep=1&sort=name&page=3"), here);
  assert.notEqual(placeOf("view=folders&library=a&folder=Pete+Dye%2FDrone"), here);
  assert.notEqual(placeOf("view=folders&library=b&folder=Pete+Dye"), here);
  assert.notEqual(placeOf("view=folders&library=a"), here);
});

test("in the flat view the library is a filter like any other", () => {
  assert.equal(placeOf("library=a&media=image"), placeOf("library=b"));
  assert.equal(placeOf(""), "");
  assert.notEqual(placeOf("view=folders"), placeOf(""));
});

// ---- counts in words -----------------------------------------------------------------

test("what is beneath a folder is its total and its kinds", () => {
  assert.equal(
    contentsText({ items: 134, photos: 110, clips: 24 }),
    "134 items — 110 photographs, 24 clips",
  );
  assert.equal(contentsText({ items: 12480, photos: 12000, clips: 480 }), "12,480 items — 12,000 photographs, 480 clips");
});

test("a folder of one kind says only that, in the singular for one", () => {
  assert.equal(contentsText({ items: 20, photos: 20, clips: 0 }), "20 photographs");
  assert.equal(contentsText({ items: 1, photos: 0, clips: 1 }), "1 clip");
});

test("audio is named, so the kinds add up to the total", () => {
  assert.equal(contentsText({ items: 10, photos: 6, clips: 2 }), "10 items — 6 photographs, 2 clips, 2 audio files");
  assert.equal(contentsText({ items: 1, photos: 0, clips: 0 }), "1 audio file");
});

test("an empty folder says so", () => {
  assert.equal(contentsText({ items: 0, photos: 0, clips: 0 }), "nothing catalogued");
});

test("a folder nobody has looked at says nothing about looking", () => {
  assert.equal(lookedText({ photos: 100, clips: 20, looked: 0 }), "");
  assert.equal(lookedText({ photos: 0, clips: 0, looked: 0 }), "");
});

test("how much was looked at is out of the photographs and clips, never the audio", () => {
  assert.equal(lookedText({ photos: 100, clips: 20, looked: 40 }), "40 of 120 looked at");
  assert.equal(lookedText({ photos: 6, clips: 2, looked: 8 }), "all looked at");
});

test("the heading over the files says whether the folders inside are counted", () => {
  assert.equal(filesHeading(14, false), "14 files in this folder");
  assert.equal(filesHeading(1, false), "1 file in this folder");
  assert.equal(filesHeading(134, true), "134 files in this folder and the folders inside it");
  assert.equal(filesHeading(0, false), "No files in this folder");
});

test("a grid the filters have shortened says how many it shows", () => {
  assert.equal(shownNote(9, 14), "9 shown with the filters chosen");
  assert.equal(shownNote(0, 1200), "0 shown with the filters chosen");
  assert.equal(shownNote(14, 14), "");
  // A file catalogued between the two counts must not read as "hidden".
  assert.equal(shownNote(15, 14), "");
});

const level = (path, folders, here, inside) => ({
  path,
  folders: Array.from({ length: folders }, () => ({})),
  here: { items: here, photos: here, clips: 0 },
  inside: { items: inside, photos: inside, clips: 0 },
});

test("an empty grid says why: nothing catalogued, a path nothing is under, files further down, or the filters", () => {
  assert.equal(emptyReason(level("", 0, 0, 0), false), "library");
  assert.equal(emptyReason(level("No/Such", 0, 0, 0), false), "gone");
  assert.equal(emptyReason(level("Pete Dye", 3, 0, 40), false), "folders");
  assert.equal(emptyReason(level("Pete Dye", 3, 14, 40), false), "filters");
});

test("with sub-folders included, an empty grid is the filters' doing", () => {
  assert.equal(emptyReason(level("Pete Dye", 3, 0, 40), true), "filters");
  assert.equal(emptyReason(level("", 3, 0, 0), true), "library");
});

// ---- a model's score -----------------------------------------------------------------

test("a score is good from 70, a warning under 40, plain between", () => {
  assert.equal(scoreTone(100), "good");
  assert.equal(scoreTone(70), "good");
  assert.equal(scoreTone(69), "plain");
  assert.equal(scoreTone(40), "plain");
  assert.equal(scoreTone(39), "low");
  assert.equal(scoreTone(0), "low");
});

test("a score is read out with whose it is and out of what", () => {
  assert.equal(scoreLabel(82), "AI score 82 of 100");
});

// ---- a review, before it starts ------------------------------------------------------

test("a length of time reads as the server writes it", () => {
  assert.equal(lengthText(0), "under a minute");
  assert.equal(lengthText(59), "under a minute");
  assert.equal(lengthText(60), "1 min");
  assert.equal(lengthText(1500), "25 min");
  assert.equal(lengthText(6000), "1 h 40 min");
  assert.equal(lengthText(10800), "3 h");
  assert.equal(lengthText(11400), "3 h 10 min");
  // 59.6 minutes is an hour, not "60 min".
  assert.equal(lengthText(3576), "1 h");
});

test("the estimate is the server's own sentence when it sent one", () => {
  const words = "Under a minute, from the one test this model has run. Loading the model and making playable copies are extra.";
  assert.equal(estimateText({ seconds: 9, basis: "test", words }), words);
});

test("an estimate with no sentence says no more than its figure", () => {
  assert.equal(estimateText({ seconds: 6000, basis: "measured" }), "About 1 h 40 min.");
  assert.equal(estimateText({ seconds: 7200, basis: "test", words: "" }), "Roughly 2 h.");
  assert.equal(estimateText({ seconds: 20, basis: "measured", words: null }), "Under a minute.");
});

test("with nothing measured there is no figure to give", () => {
  const none = "No estimate yet: nothing has been looked at with this model. The first few will tell.";
  assert.equal(estimateText({ seconds: null, basis: "none" }), none);
  assert.equal(estimateText({ seconds: null, basis: "measured" }), none);
});

const todo = (photos, clips, shots, stills = 0) => ({ todo: { photos, clips, shots, stills_only: stills } });

test("what is not looked at yet is photographs, and clips with their shots", () => {
  assert.equal(todoText(todo(214, 37, 291)), "214 photographs and 37 clips (291 shots).");
  assert.equal(todoText(todo(6, 0, 0)), "6 photographs.");
  assert.equal(todoText(todo(0, 1, 1)), "1 clip (1 shot).");
  assert.equal(todoText(todo(1, 0, 0)), "1 photograph.");
});

test("a clip judged from one still is a clip, and adds no shots", () => {
  assert.equal(todoText(todo(0, 0, 0, 2)), "2 clips.");
  assert.equal(todoText(todo(3, 4, 12, 2)), "3 photographs and 6 clips (12 shots).");
  assert.equal(todoItems(todo(3, 4, 12, 2).todo), 17);
});

test("with nothing left it says everything has been looked at", () => {
  assert.equal(todoText(todo(0, 0, 0)), "Everything here has been looked at.");
  assert.equal(todoItems(todo(0, 0, 0).todo), 0);
});

const first = (over = {}, stills = 0) => ({
  todo: { photos: 0, clips: 0, shots: 0, stills_only: stills },
  first: {
    need_copy: 0,
    need_copy_footage_s: 0,
    copy_away: 0,
    need_shots: 0,
    still_processing: 0,
    problem: null,
    ...over,
  },
});

test("nothing needed first is no lines at all", () => {
  assert.deepEqual(firstText(first()), []);
});

test("clips with no playable copy and clips with no shots are one sentence, with the footage's length", () => {
  assert.deepEqual(firstText(first({ need_copy: 12, need_copy_footage_s: 11400, need_shots: 3 })), [
    "12 clips have no playable copy yet (3 h 10 min of footage) and 3 have not had their shots found. " +
      "These are made automatically; what is ready is looked at meanwhile, and the rest as they finish.",
  ]);
});

test("one clip reads in the singular, and shots alone name the clips", () => {
  assert.equal(
    firstText(first({ need_copy: 1, need_copy_footage_s: 127.662 }))[0],
    "1 clip has no playable copy yet (2 min of footage). " +
      "These are made automatically; what is ready is looked at meanwhile, and the rest as they finish.",
  );
  assert.match(firstText(first({ need_shots: 1 }))[0], /^1 clip has not had its shots found\. /);
  assert.match(firstText(first({ need_shots: 4 }))[0], /^4 clips have not had their shots found\. /);
  assert.match(firstText(first({ need_copy: 2, need_shots: 1 }))[0], / and 1 has not had its shots found\. /);
});

test("with Make clips ready first unticked, a clip that is not ready is judged from one still, not skipped", () => {
  // The review judges whatever never became ready from one still; unticked,
  // nothing is made ready, so that is every clip named here. The dialog
  // said "skipped" once, and the review then looked at them anyway.
  assert.deepEqual(firstText(first({ need_copy: 2, need_copy_footage_s: 0 }), false), [
    "2 clips have no playable copy yet. Each is judged from one still this time.",
  ]);
  assert.deepEqual(firstText(first({ need_shots: 1 }), false), [
    "1 clip has not had its shots found. It is judged from one still this time.",
  ]);
  assert.ok(
    firstText(first({ need_copy: 1, need_shots: 1 }), false)[0].endsWith(
      " Each is judged from one still this time.",
    ),
  );
});

test("the drive's own sentence, stills-only clips and items still processing each get a line, in order", () => {
  const lines = firstText(
    first(
      {
        need_copy: 2,
        need_copy_footage_s: 600,
        copy_away: 1,
        still_processing: 5,
        problem: "The drive for playable copies is away.",
      },
      2,
    ),
  );
  assert.equal(lines.length, 5);
  assert.match(lines[0], /^2 clips have no playable copy yet \(10 min of footage\)\./);
  assert.equal(lines[1], "The drive for playable copies is away.");
  assert.equal(lines[2], "1 clip has no playable copy and its drive is away; it is judged from one still.");
  assert.equal(lines[3], "2 clips cannot be split into shots; each is judged from one still.");
  assert.equal(lines[4], "5 items are still being processed and are skipped this time.");
});

test("an answer from before the away count existed is read without it", () => {
  const old = first({ still_processing: 1 });
  delete old.first.copy_away;
  assert.deepEqual(firstText(old), ["1 item is still being processed and is skipped this time."]);
});

test("what is left out is said only when something is", () => {
  assert.equal(skippedText({ missing: 0, audio: 0 }), "");
  assert.equal(skippedText({ missing: 4, audio: 9 }), "4 files are missing and 9 are audio.");
  assert.equal(skippedText({ missing: 1, audio: 0 }), "1 file is missing.");
  assert.equal(skippedText({ missing: 0, audio: 1 }), "1 file is audio, which is not looked at.");
});

// ---- the review dialog: what is asked, and what stands in the way ----------------------

test("the dialog says whose review it is: the library, the folder, and the folders inside", () => {
  assert.equal(scopeText("GELCO", "", 3), "GELCO — the whole library.");
  assert.equal(scopeText("GELCO", "Pete Dye", 0), "GELCO › Pete Dye — this folder.");
  assert.equal(scopeText("GELCO", "Pete Dye", 1), "GELCO › Pete Dye — this folder and the folder inside it.");
  assert.equal(
    scopeText("GELCO", "/Pete Dye/Drone/", 3),
    "GELCO › Pete Dye › Drone — this folder and the 3 folders inside it.",
  );
  // Before the library's name has been read.
  assert.equal(scopeText("", "", 0), "This library — the whole library.");
  // Before the folders inside have been counted, it does not say "this folder" alone.
  assert.equal(scopeText("GELCO", "Pete Dye/Drone", null), "GELCO › Pete Dye › Drone");
  assert.equal(scopeText("GELCO", "", null), "GELCO — the whole library.");
});

test("nothing ready is not called all looked at while clips still wait, or where nothing can be looked at", () => {
  const none = { photos: 0, clips: 0, shots: 0, stills_only: 0 };
  // A folder of clips with no playable copies: nothing ready, everything to do.
  assert.equal(
    todoText({ ...first({ need_copy: 5 }), already: { items: 0 } }),
    "Nothing that is ready yet: only the clips named under Needed first.",
  );
  assert.equal(
    todoText({ ...first({ copy_away: 1 }), already: { items: 12 } }),
    "Nothing that is ready yet: only the clips named under Needed first.",
  );
  // A folder that holds only sound.
  assert.equal(todoText({ todo: none, ...first(), already: { items: 0 } }), "There is nothing here a model can look at.");
  assert.equal(todoText({ todo: none, ...first(), already: { items: 9 } }), "Everything here has been looked at.");
});

test("what was already looked at, from a server with no breakdown, says what it counts", () => {
  // Never "180 items": the folder list beside it says items for files.
  assert.equal(alreadyText({ items: 180 }), "180, counting each shot of a clip by itself.");
  assert.equal(alreadyText({ items: 1 }), "1, counting each shot of a clip by itself.");
});

const asked = (over = {}) => ({
  ...first(),
  already: { items: 0 },
  model: { why_not_now: null, tonight_problem: null },
  tonight: { starts_at: "2026-11-13T21:30:00", open_now: false, problem: null },
  ...over,
});

test("a review is worth starting for something ready, or for clips that would be made ready", () => {
  assert.equal(anythingToReview(asked()), false);
  assert.equal(anythingToReview(asked({ todo: { photos: 1, clips: 0, shots: 0, stills_only: 0 } })), true);
  assert.equal(anythingToReview(asked({ todo: { photos: 0, clips: 1, shots: 4, stills_only: 0 } })), true);
  assert.equal(anythingToReview(asked({ todo: { photos: 0, clips: 0, shots: 0, stills_only: 1 } })), true);
  assert.equal(anythingToReview(first({ need_copy: 2 })), true);
  assert.equal(anythingToReview(first({ need_shots: 1 })), true);
  assert.equal(anythingToReview(first({ copy_away: 1 })), true);
  // Files still being processed are not something a review can start on.
  assert.equal(anythingToReview(first({ still_processing: 5 })), false);
});

test("Start now is off with the sentence the server would refuse with", () => {
  const something = { todo: { photos: 6, clips: 0, shots: 0, stills_only: 0 } };
  assert.equal(startBlock(asked(something)), null);
  assert.equal(startBlock(asked({ already: { items: 6 } })), "Everything here has been looked at.");
  assert.equal(startBlock(asked()), "There is nothing here a model can look at.");
  // The model's own reason is the server's sentence, and comes first, as it
  // does when the server refuses.
  const overnight = "This model only runs overnight (needs about 12.6 GB; the daytime budget is 8 GB).";
  assert.equal(
    startBlock(asked({ ...something, model: { why_not_now: overnight, tonight_problem: null } })),
    overnight,
  );
  assert.equal(startBlock(asked({ model: { why_not_now: overnight, tonight_problem: null } })), overnight);
  // No model at all is said elsewhere in the dialog, not beside the button.
  assert.equal(startBlock(asked({ ...something, model: null })), null);
});

test("Tonight is off while the window is open, where there is none, and for a model that may not look", () => {
  assert.equal(tonightBlock(asked()), null);
  const open = "The overnight window is open now — Start now uses it.";
  assert.equal(tonightBlock(asked({ tonight: { starts_at: null, open_now: true, problem: open } })), open);
  const none = "There is no overnight window: it starts and ends at 21:30.";
  assert.equal(tonightBlock(asked({ tonight: { starts_at: null, open_now: false, problem: none } })), none);
  const broken = "Its files are incomplete. Download it again on the AI models page.";
  assert.equal(tonightBlock(asked({ model: { why_not_now: broken, tonight_problem: broken } })), broken);
});

test("the Tonight button reads the Mac's own clock off the text, never through a zone", () => {
  assert.equal(
    tonightLabel({ starts_at: "2026-11-13T21:30:00", open_now: false, problem: null }),
    "Tonight (21:30)",
  );
  // 00:15 on the Mac must not become another hour in a browser somewhere else.
  assert.equal(
    tonightLabel({ starts_at: "2026-11-14T00:15:00", open_now: false, problem: null }),
    "Tonight (00:15)",
  );
  assert.equal(tonightLabel({ starts_at: null, open_now: true, problem: "open" }), "Tonight");
  assert.equal(tonightLabel({ starts_at: null, open_now: false, problem: "no window" }), "Tonight");
});

test("a model in the choice says how big it is and when it fits", () => {
  assert.equal(
    modelChoiceText({ name: "gemma-4-e4b-it-4bit", memory_gb: 5.8, fits: "day" }),
    "gemma-4-e4b-it-4bit — about 5.8 GB, fits by day",
  );
  assert.equal(
    modelChoiceText({ name: "demo-medium-4bit", memory_gb: 12.60001, fits: "night" }),
    "demo-medium-4bit — about 12.6 GB, overnight only",
  );
  assert.equal(modelChoiceText({ name: "molmo", memory_gb: 12, fits: "day" }), "molmo — about 12 GB, fits by day");
});

test("redo says how much it adds, or that it adds nothing", () => {
  assert.equal(redoText(0), "Nothing here was looked at by another model.");
  assert.equal(
    redoText(60),
    "Also look again at what a different model, or an older version of the question, already looked at: 60, counting each shot of a clip by itself.",
  );
  assert.ok(!redoText(1).includes("item"));
});

test("items that could not be looked at before are said to be tried again", () => {
  assert.equal(failedBeforeText(0), "");
  assert.equal(
    failedBeforeText(1),
    "1 of these was tried before and could not be looked at; it will be tried again.",
  );
  assert.equal(
    failedBeforeText(7),
    "7 of these were tried before and could not be looked at; they will be tried again.",
  );
});

test("a library's new-footage box says what it covers, and warns only once it is on", () => {
  const always =
    "New photographs and clips are looked at as they come in. What is already here is not — use Review with AI for that.";
  assert.deepEqual(ingestNotes({ on: false, makesCopies: false, hasLooker: false }), [always]);
  assert.deepEqual(ingestNotes({ on: true, makesCopies: true, hasLooker: true }), [always]);
  assert.deepEqual(ingestNotes({ on: true, makesCopies: false, hasLooker: false }), [
    always,
    "This library does not make playable copies, so new clips wait until you review their folder.",
    "No model has the Looker role yet, so nothing will be looked at until one does.",
  ]);
});

// ---- a review, while it runs and after -----------------------------------------------

test("clips being made ready are counted, and those that could not be are said", () => {
  assert.equal(preparingText({ state: "", total: 0, ready: 0, gave_up: 0 }), "");
  assert.equal(preparingText({ state: "running", total: 12, ready: 3, gave_up: 0 }), "3 of 12 clips made ready.");
  assert.equal(preparingText({ state: "running", total: 1, ready: 0, gave_up: 0 }), "0 of 1 clip made ready.");
  assert.equal(
    preparingText({ state: "done", total: 12, ready: 10, gave_up: 2 }),
    "10 of 12 clips made ready; 2 could not be.",
  );
});

test("what a review put on Tags is said only when it put something there", () => {
  assert.equal(suggestedText({ tags_suggested: 0, new_tags: 0 }), "");
  assert.equal(suggestedText({ tags_suggested: 1, new_tags: 0 }), "1 tag suggested.");
  assert.equal(suggestedText({ tags_suggested: 310, new_tags: 4 }), "310 tags suggested; 4 of the tags are new.");
  assert.equal(suggestedText({ tags_suggested: 12, new_tags: 1 }), "12 tags suggested; 1 of the tags is new.");
});

test("stopping a review is asked about once, in words that say nothing is lost", () => {
  assert.equal(CANCEL_QUESTION, "Stop this review? What it has already looked at is kept.");
});

const review = (over = {}) => ({
  state: "looking",
  message: "DJI_0639.MP4",
  done: 120,
  total: 505,
  seconds_left: 4300,
  ...over,
});

test("a review still to do something is active; one that ended is not", () => {
  for (const state of ["tonight", "waiting", "paused", "looking"]) {
    assert.equal(reviewActive({ state }), true, state);
  }
  for (const state of ["done", "failed", "cancelled", "something new"]) {
    assert.equal(reviewActive({ state }), false, state);
  }
});

test("progress is how many of how many, naming what it counts", () => {
  assert.equal(progressText(review()), "120 of 505 photographs and shots");
  assert.equal(progressText({ done: 1200, total: 12480 }), "1,200 of 12,480 photographs and shots");
  // Clips judged from one still count once each, so they are named too.
  assert.equal(progressText(review({ stills_only: 3 })), "120 of 505 photographs, shots and clips");
});

test("while looking, the file it is on is said as such, with the measured time to go", () => {
  assert.equal(reviewLine(review()), "Looking at DJI_0639.MP4. About 1 h 12 min to go.");
  assert.equal(reviewLine(review({ seconds_left: 20 })), "Looking at DJI_0639.MP4. Under a minute to go.");
});

test("with no measured speed, no time to go is made up", () => {
  assert.equal(reviewLine(review({ seconds_left: null })), "Looking at DJI_0639.MP4.");
  assert.equal(reviewLine(review({ message: "", seconds_left: null })), "Looking.");
});

test("a sentence sent while looking is left as the sentence it is", () => {
  assert.equal(
    reviewLine(review({ message: "Loading the model.", seconds_left: null })),
    "Loading the model.",
  );
});

test("in every other state the server's sentence is the line, its closing count given a unit", () => {
  const waiting = "Waiting for 3 clips to be made ready.";
  assert.equal(reviewLine(review({ state: "waiting", message: waiting })), waiting);
  const ended = "Cancelled. 120 of 505 were looked at; their results are kept.";
  assert.equal(
    reviewLine(review({ state: "cancelled", message: ended, seconds_left: 99 })),
    "Cancelled. 120 of 505 photographs and shots were looked at; their results are kept.",
  );
  assert.equal(reviewLine(review({ state: "tonight", message: "Starts tonight at 21:30." })), "Starts tonight at 21:30.");
});

test("a finished review's count names its unit instead of reading as files", () => {
  // Pete Dye: 8 files (6 photographs, 2 clips of 3 shots) are 12 to look at.
  const finished = { state: "done", message: "Finished: 12 of 12 were looked at.", done: 12, total: 12, seconds_left: null };
  assert.equal(reviewLine(finished), "Finished: 12 of 12 photographs and shots were looked at.");
  assert.equal(
    reviewLine({ ...finished, message: "Finished: 1,200 of 1,204 were looked at. 4 could not be.", stills_only: 2 }),
    "Finished: 1,200 of 1,204 photographs, shots and clips were looked at. 4 could not be.",
  );
  // A sentence that does not lead with the count is left alone.
  const other = "The model stopped: it ran out of memory.";
  assert.equal(reviewLine({ ...finished, state: "failed", message: other }), other);
});

test("a failed review's pill reads Stopped; an unknown state shows as it came", () => {
  assert.equal(reviewStateWord("failed"), "Stopped");
  assert.equal(reviewStateWord("looking"), "Looking");
  assert.equal(reviewStateWord("tonight"), "Tonight");
  assert.equal(reviewStateWord("resting"), "resting");
});

test("the pill is amber while looking, sage when finished, red when stopped, plain otherwise", () => {
  assert.equal(reviewTone("looking"), "warn");
  assert.equal(reviewTone("done"), "ok");
  assert.equal(reviewTone("failed"), "bad");
  for (const state of ["tonight", "waiting", "paused", "cancelled", "resting"]) {
    assert.equal(reviewTone(state), undefined, state);
  }
});

// ---- Processing: the reviews, and new footage library by library -----------------------

test("a review's row is named by its library and folder, and says when it is the whole library", () => {
  assert.equal(reviewPlace({ library_name: "GELCO", folder: "Pete Dye/Drone" }), "GELCO › Pete Dye › Drone");
  assert.equal(reviewPlace({ library_name: "GELCO", folder: "" }), "GELCO (the whole library)");
  // A library deleted since still has a row to read.
  assert.equal(reviewPlace({ library_name: "", folder: "2026" }), "A library › 2026");
});

test("a review's row leads to its folder in Browse's Folders view", () => {
  assert.equal(
    reviewHref({ library_id: "lib-1", folder: "Pete Dye/Drone" }),
    "/browse?view=folders&library=lib-1&folder=Pete+Dye%2FDrone",
  );
  // The whole library: its top, not the list of libraries.
  assert.equal(reviewHref({ library_id: "lib-1", folder: "" }), "/browse?view=folders&library=lib-1");
  // A name with an ampersand or a space at its end opens that folder, not another.
  const odd = new URL(reviewHref({ library_id: "lib-1", folder: "A&B/Rev 2 " }), "http://x");
  assert.equal(odd.searchParams.get("folder"), "A&B/Rev 2 ");
});

test("the bar never runs past its end, and a review of nothing has no bar to fill", () => {
  assert.equal(reviewPercent({ done: 120, total: 480 }), 25);
  assert.equal(reviewPercent({ done: 6, total: 6 }), 100);
  // Seen on the review server: a review that counted "20 of 8".
  assert.equal(reviewPercent({ done: 20, total: 8 }), 100);
  assert.equal(reviewPercent({ done: 0, total: 0 }), 0);
  assert.equal(reviewPercent({ done: -1, total: 5 }), 0);
});

const ingest = (over = {}) => ({
  library_name: "GELCO",
  waiting: 14,
  state: "looking",
  message: "IMG_2291.JPG",
  ...over,
});

test("a library's new footage: what waits, and the file being looked at", () => {
  assert.equal(ingestText(ingest()), "New footage in GELCO: 14 waiting — looking at IMG_2291.JPG.");
  assert.equal(
    ingestText(ingest({ waiting: 1200, message: "A.JPG" })),
    "New footage in GELCO: 1,200 waiting — looking at A.JPG.",
  );
});

test("a library with nothing waiting says so and nothing more", () => {
  assert.equal(ingestText(ingest({ waiting: 0, state: "idle", message: "" })), "New footage in GELCO: nothing waiting.");
  assert.equal(ingestText(ingest({ waiting: 3, state: "idle", message: "" })), "New footage in GELCO: 3 waiting.");
});

test("waiting, paused and a sentence sent while looking are the server's words after the count", () => {
  assert.equal(
    ingestText(ingest({ state: "waiting", message: "Waiting its turn." })),
    "New footage in GELCO: 14 waiting. Waiting its turn.",
  );
  assert.equal(
    ingestText(ingest({ state: "paused", message: "Paused. It carries on when local AI is resumed." })),
    "New footage in GELCO: 14 waiting. Paused. It carries on when local AI is resumed.",
  );
  assert.equal(
    ingestText(ingest({ message: "Loading the model." })),
    "New footage in GELCO: 14 waiting. Loading the model.",
  );
});

test("a library held after the model failed says that it stopped and when it is tried again", () => {
  const held =
    "Looking at new footage stopped because of a problem with the model, and is tried again in about 6 hours.";
  const line = ingest({ state: "failed", message: held, retry_at: "2026-11-14T03:10:00Z" });
  assert.equal(ingestText(line), `New footage in GELCO: 14 waiting. ${held}`);
  assert.equal(ingestStopped(line), true);
  for (const state of ["idle", "waiting", "paused", "looking"]) {
    assert.equal(ingestStopped({ state }), false, state);
  }
});

// ---- Tags: a local model's suggestion ---------------------------------------------------

test("a suggestion about a moment of a clip shows that moment, and the viewer starts there", () => {
  assert.equal(momentPicture({ at_ms: 8331 }), "frame?ts=8331");
  assert.equal(momentSeconds({ at_ms: 8331 }), 8.331);
  // The first frame of a clip is a moment too.
  assert.equal(momentPicture({ at_ms: 0 }), "frame?ts=0");
  assert.equal(momentSeconds({ at_ms: 0 }), 0);
});

test("a suggestion with no moment keeps the picture and the start it always had", () => {
  // Every suggestion from before local models, and a local one about a
  // photograph or a whole clip.
  for (const row of [{ at_ms: null }, {}, { at_ms: undefined }, { at_ms: Number.NaN }, { at_ms: -5 }]) {
    assert.equal(momentPicture(row), null);
    assert.equal(momentSeconds(row), undefined);
  }
});

const ORIGINS = { clip: "match", claude: "Claude", mcp: "Claude (chat)", local: "local model" };

test("who suggested a tag reads as it always did for the learner and for Claude", () => {
  assert.deepEqual(suggestedBy({ origin: "clip" }, ORIGINS), { label: "match", title: undefined });
  assert.deepEqual(suggestedBy({ origin: "claude", model: null, demo: false }, ORIGINS), {
    label: "Claude",
    title: undefined,
  });
  assert.deepEqual(suggestedBy({ origin: "mcp", at_ms: null }, ORIGINS), { label: "Claude (chat)", title: undefined });
  // An origin a newer server sends is shown as it came, as before.
  assert.deepEqual(suggestedBy({ origin: "somewhere" }, ORIGINS), { label: "somewhere", title: undefined });
});

test("a local model's suggestion names the model in its tooltip and says when it was made up", () => {
  assert.deepEqual(suggestedBy({ origin: "local", model: "gemma-4-e4b-it-4bit", demo: false }, ORIGINS), {
    label: "local model",
    title: "gemma-4-e4b-it-4bit, a model on this Mac",
  });
  assert.deepEqual(suggestedBy({ origin: "local", model: "demo-small-4bit", demo: true }, ORIGINS), {
    label: "local model (made up)",
    title: "demo-small-4bit, a model on this Mac",
  });
  // The look that made it has been removed: still a local model's.
  assert.deepEqual(suggestedBy({ origin: "local", model: null, demo: false }, ORIGINS), {
    label: "local model",
    title: undefined,
  });
  // Only a local suggestion can be the demonstration's.
  assert.equal(suggestedBy({ origin: "clip", demo: true, model: "x" }, ORIGINS).label, "match");
});

test("the local card on Tags counts what waits and says where it comes from", () => {
  assert.equal(localWaitingText(41), "41 waiting.");
  assert.equal(localWaitingText(1), "1 waiting.");
  assert.equal(localWaitingText(1200), "1,200 waiting.");
  assert.equal(localWaitingText(0), "None waiting.");
  assert.equal(
    LOCAL_SUGGESTIONS_FROM,
    "They come from reviews you start on a folder in Browse and from new footage in libraries you have switched on.",
  );
});

// ---- the AI models page: what the Looker does ------------------------------------------

test("a shot is shown as three pictures or one, the two the server takes, three first", () => {
  assert.deepEqual(
    STILLS_CHOICES.map((choice) => choice.value),
    [3, 1],
  );
  assert.equal(STILLS_CHOICES[0].label, "3 — start, middle, end");
  assert.equal(STILLS_CHOICES[1].label, "1 — the middle");
  assert.match(STILLS_NOTE, /^Use 1 if a model gives poor answers on clips/);
});

test("the line under the Looker's settings counts what waits on Tags", () => {
  assert.equal(waitingOnTagsText(41), "41 suggestions waiting on Tags");
  assert.equal(waitingOnTagsText(1), "1 suggestion waiting on Tags");
  assert.equal(waitingOnTagsText(0), "No suggestions waiting on Tags");
});

test("reviews under way: running is one being looked at now, the rest of the active ones wait", () => {
  const states = (...names) => names.map((state) => ({ state }));
  assert.equal(reviewsUnderWayText(states("looking", "looking")), "2 reviews running");
  assert.equal(reviewsUnderWayText(states("looking", "tonight", "paused")), "1 review running and 2 waiting");
  assert.equal(reviewsUnderWayText(states("waiting")), "1 review waiting");
  assert.equal(reviewsUnderWayText(states("tonight", "paused")), "2 reviews waiting");
  // Finished ones stay in the list and are not under way.
  assert.equal(reviewsUnderWayText(states("done", "failed", "cancelled")), "no review running");
  assert.equal(reviewsUnderWayText([]), "no review running");
});

test("saving a Looker setting says what changes, and that it is from now on", () => {
  assert.equal(lookSettingSaved({ new_tags: true }), "Saved. The Looker may propose new tags from now on.");
  assert.equal(
    lookSettingSaved({ new_tags: false }),
    "Saved. The Looker only suggests tags you already use from now on.",
  );
  assert.equal(
    lookSettingSaved({ stills_per_shot: 1 }),
    "Saved. The Looker is shown one picture of each shot from now on.",
  );
  assert.equal(
    lookSettingSaved({ stills_per_shot: 3 }),
    "Saved. The Looker is shown 3 pictures of each shot from now on.",
  );
  assert.equal(lookSettingSaved({}), "Saved.");
});
