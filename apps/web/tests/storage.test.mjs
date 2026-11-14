// Run with `npm test` (Node's own test runner; nothing to install).
//
// The Storage page tells a business owner where the bulky files are and how
// far a move has got. These pin the figures and the words it shows: a size
// that reads the same in every row, a bar that never runs backwards, and a
// folder field that is only ever filled in with something the server said.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  REMOVAL_WAIT_MS,
  comesBack,
  copiedWords,
  count,
  diskSize,
  gbToBytes,
  kindsSummary,
  layoutBehind,
  leftBehind,
  looksLikeMac,
  mayStillBeRemoving,
  moveProgress,
  noAnswer,
  phaseWords,
  refusal,
  stillAbout,
  suggestedTarget,
} from "../src/lib/storage.ts";

const MB = 1024 ** 2;
const GB = 1024 ** 3;

const move = (more = {}) => ({
  id: "m1",
  kind: "renders",
  label: "Slideshow videos",
  source: "/Users/dj/FrameFound/data/renders",
  target: "/Volumes/Photos/Framefound/renders",
  mode: "copy",
  phase: "copying",
  active: true,
  cancelling: false,
  files_total: 0,
  files_done: 0,
  bytes_total: 0,
  bytes_done: 0,
  copied: null,
  error: null,
  requested_by: "owner@example.com",
  requested_at: "2026-11-12T10:00:00Z",
  started_at: null,
  finished_at: null,
  ...more,
});

const kind = (more = {}) => ({
  kind: "renders",
  label: "Slideshow videos",
  description: "",
  path: "/Users/dj/FrameFound/data/renders",
  default_path: "/Users/dj/FrameFound/data/renders",
  bulk_path: null,
  where: "local",
  connected: true,
  problem: null,
  files: 0,
  size_bytes: 0,
  free_gb: 100,
  total_gb: 500,
  movable: true,
  old_path: null,
  ...more,
});

test("a size is GB with one decimal, and whole MB below 1 GB", () => {
  assert.equal(diskSize(40.06 * GB), "40.1 GB");
  assert.equal(diskSize(GB), "1.0 GB");
  assert.equal(diskSize(1536 * GB), "1,536.0 GB");
  assert.equal(diskSize(512 * MB), "512 MB");
  assert.equal(diskSize(0), "0 MB");
});

test("a size never reads 1,024 MB, nothing never reads as something, and unknown is a dash", () => {
  assert.equal(diskSize(1023.7 * MB), "1.0 GB");
  assert.equal(diskSize(300), "under 1 MB");
  assert.equal(diskSize(null), "—");
  assert.equal(diskSize(undefined), "—");
});

test("free space arrives in GB and is shown by the same helper", () => {
  assert.equal(diskSize(gbToBytes(237.1)), "237.1 GB");
  assert.equal(diskSize(gbToBytes(0.5)), "512 MB");
  assert.equal(gbToBytes(null), null);
});

test("one file is not 1 files", () => {
  assert.equal(count(1, "file"), "1 file");
  assert.equal(count(3310, "file"), "3,310 files");
  assert.equal(count(2, "copy", "copies"), "2 copies");
});

test("the Mac example folder is offered only when the server's folders read like a Mac's", () => {
  assert.equal(looksLikeMac(["/Users/dj/FrameFound/data", null]), true);
  assert.equal(looksLikeMac([null, "/Volumes/Photos/Framefound/proxies"]), true);
  assert.equal(looksLikeMac(["/data", "/cache/bulk/renders", null]), false);
  assert.equal(looksLikeMac(["C:\\Users\\dj\\data"]), false);
  assert.equal(looksLikeMac([]), false);
});

test("a move's folder is filled in with the kind's place in the bulk folder when there is one", () => {
  assert.equal(
    suggestedTarget(kind({ bulk_path: "/Volumes/Photos/Framefound/renders" })),
    "/Volumes/Photos/Framefound/renders",
  );
});

test("with no bulk folder the field is left for the owner to fill in", () => {
  assert.equal(suggestedTarget(kind()), "");
  // Backup copies with no folder yet: nothing to suggest, and no path to reuse.
  assert.equal(suggestedTarget(kind({ kind: "backups", path: null, where: "none", connected: false })), "");
});

test("a kind whose drive is away is offered its own folder, to be started empty on the replacement", () => {
  const away = kind({
    path: "/Volumes/Photos/Framefound/renders",
    where: "bulk",
    connected: false,
    problem: "The slideshow store is not connected.",
  });
  assert.equal(suggestedTarget(away), "/Volumes/Photos/Framefound/renders");
});

test("the bar follows the copy by size", () => {
  const got = moveProgress(
    move({ files_total: 3310, files_done: 1204, bytes_total: 40 * GB, bytes_done: 10 * GB }),
  );
  assert.equal(got.percent, 25);
  assert.equal(got.counts, "1,204 of 3,310 files · 10.0 GB of 40.0 GB");
});

test("the bar follows the file count when the files have no size to speak of", () => {
  const got = moveProgress(move({ files_total: 4, files_done: 1 }));
  assert.equal(got.percent, 25);
});

test("a move still waiting or checking draws no bar and invents no figures", () => {
  for (const phase of ["queued", "checking"]) {
    assert.deepEqual(moveProgress(move({ phase })), { percent: null, counts: null }, phase);
  }
});

test("the bar does not run backwards when the totals restart for a catch-up pass", () => {
  const catchingUp = moveProgress(
    move({ phase: "finishing", files_total: 3, files_done: 0, bytes_total: 9 * MB, bytes_done: 0 }),
  );
  assert.equal(catchingUp.percent, 100);
  assert.match(catchingUp.counts, /^Catching up: 0 of 3 files/);
  assert.equal(moveProgress(move({ phase: "switching" })).percent, 100);
});

test("a finished move says what the new folder holds", () => {
  const got = moveProgress(
    move({ phase: "done", active: false, files_total: 3310, files_done: 3310, bytes_total: 40 * GB, bytes_done: 40 * GB, copied: 3310 }),
  );
  assert.equal(got.percent, 100);
  assert.equal(got.counts, "3,310 files · 40.0 GB in the new folder");
});

test("a finished move says what it had to copy, and a second run is not read as doing nothing", () => {
  const done = (more) => move({ phase: "done", active: false, files_total: 14, files_done: 14, ...more });
  assert.equal(copiedWords(done({ copied: 14 })), "14 files were copied.");
  assert.equal(copiedWords(done({ copied: 11 })), "11 files had to be copied; the rest were already there.");
  assert.equal(copiedWords(done({ copied: 0 })), "Every file was already there, so nothing had to be copied.");
  assert.equal(copiedWords(done({ copied: 0, files_total: 0 })), "There was nothing to copy yet.");
  assert.equal(copiedWords(done({ copied: 1, files_total: 1 })), "1 file was copied.");
  // Not finished, or the server did not say: nothing is claimed.
  assert.equal(copiedWords(move({ copied: 3 })), null);
  assert.equal(copiedWords(done({ copied: null })), null);
});

test("a move that stopped shows how far it got and no bar", () => {
  for (const phase of ["failed", "cancelled"]) {
    const got = moveProgress(
      move({ phase, active: false, files_total: 10, files_done: 4, bytes_total: 2 * GB, bytes_done: GB }),
    );
    assert.equal(got.percent, null, phase);
    assert.equal(got.counts, "It had copied 4 of 10 files · 1.0 GB of 2.0 GB.", phase);
  }
  // One that never started has nothing to report.
  assert.equal(moveProgress(move({ phase: "cancelled", active: false })).counts, null);
});

test("every phase has plain words, and only the running ones look busy", () => {
  const tones = {
    queued: "warn",
    checking: "warn",
    copying: "warn",
    finishing: "warn",
    switching: "warn",
    done: "ok",
    failed: "bad",
    cancelled: undefined,
  };
  for (const [phase, tone] of Object.entries(tones)) {
    const words = phaseWords(move({ phase }));
    assert.equal(words.tone, tone, phase);
    assert.ok(words.short.length > 0 && words.sentence.endsWith("."), phase);
    // The server's own word for a phase is not shown to the owner.
    assert.ok(!words.sentence.toLowerCase().includes("queued"), phase);
  }
  assert.match(phaseWords(move({ phase: "done" })).sentence, /Slideshow videos now live in the new folder/);
});

test("a phase this page has never heard of reads as stopped, not as blank", () => {
  assert.equal(phaseWords(move({ phase: "paused" })).short, "Stopped");
});

test("a refusal is the server's sentence, and a 409 says a move is running", () => {
  assert.equal(refusal(400, "There is not enough room in /Volumes/Photos.", "x"), "There is not enough room in /Volumes/Photos.");
  assert.equal(
    refusal(409, "Slideshow videos are being moved right now. Wait for that to finish.", "x"),
    "A move is already running. Slideshow videos are being moved right now. Wait for that to finish.",
  );
  assert.equal(refusal(503, "The processing queue is unavailable", "x"), "The processing queue is unavailable.");
  // An answer with no sentence in it: say what was being tried instead.
  assert.equal(
    refusal(500, "Request failed (500)", "Could not read where the bulky files live."),
    "Could not read where the bulky files live.",
  );
  // A question keeps its question mark.
  assert.equal(refusal(400, "Start it empty?", "x"), "Start it empty?");
  assert.equal(refusal(null, "", "The move could not be started."), "The move could not be started.");
});

test("each movable kind says how its files come back after starting empty", () => {
  const said = ["proxies", "renders", "exports", "basemaps", "backups", "models"].map(comesBack);
  assert.equal(new Set(said).size, 6);
  assert.match(comesBack("renders"), /Render again/);
  // A model is downloaded, not made: "the files are made again" was untrue.
  assert.match(comesBack("models"), /downloaded again on the AI models page/);
  assert.doesNotMatch(comesBack("models"), /made again/);
});

// ---- the word beside the heading ------------------------------------------

const NAS = "/Volumes/Photos";

test("the heading says all on this machine only when every line is", () => {
  const local = [kind({ kind: "proxies" }), kind(), kind({ kind: "backups", path: null, where: "none", connected: false })];
  assert.deepEqual(kindsSummary(local), { text: "all on this machine", bad: false });
});

test("proxies in a folder of their own are not on this machine, bulk folder or no bulk folder", () => {
  // The Mac install as it was before there was a bulk folder: proxies on a
  // drive of their own, and no bulk folder named.
  const kinds = [
    kind({ kind: "proxies", path: `${NAS}/proxies`, where: "custom" }),
    kind(),
  ];
  assert.deepEqual(kindsSummary(kinds), { text: "all connected", bad: false });
});

test("the heading counts every line that shows a not-connected pill, the model library's too", () => {
  const kinds = [
    kind({ kind: "proxies", path: `${NAS}/Framefound/proxies`, where: "bulk", connected: false }),
    kind({ path: `${NAS}/Framefound/renders`, where: "bulk", connected: false }),
    // The AI models page manages the library now: a drive it is on that has
    // gone away is said, like any other.
    kind({ kind: "models", path: `${NAS}/Framefound/models`, where: "bulk", connected: false }),
    kind({ kind: "grabs", movable: false }),
  ];
  assert.deepEqual(kindsSummary(kinds), { text: "3 not connected", bad: true });
});

test("the model library is an ordinary line: no Reserved on it, and it counts where it is", () => {
  const library = kind({ kind: "models", path: `${NAS}/Framefound/models`, where: "bulk" });
  assert.equal(kindsSummary([kind(), library]).text, "all connected");
  // The quiet "Reserved" line is gone from the Storage page altogether.
  const page = readFileSync(new URL("../src/components/BulkStorage.tsx", import.meta.url), "utf8");
  assert.ok(!page.includes("Reserved") && !page.includes("isReserved"));
});

// ---- removing old copies ---------------------------------------------------

const said = (more = {}) => ({ folder: "/A", files: 12, bytes: 3 * GB, kept: 3, everything: false, ...more });

test("the second step is offered only for the folder the first step looked at", () => {
  assert.equal(leftBehind(said(), "/A"), 3);
  // Moved again since: the old copies are in another folder, which nothing
  // has looked at. The line starts again from its first step.
  assert.equal(leftBehind(said(), "/B"), 0);
  assert.equal(leftBehind(said(), null), 0);
});

test("there is no second step after the second step, or when nothing was left", () => {
  assert.equal(leftBehind(said({ everything: true }), "/A"), 0);
  assert.equal(leftBehind(said({ kept: 0 }), "/A"), 0);
  assert.equal(leftBehind(null, "/A"), 0);
});

test("what was removed is still said once the folder is off the record, what was left is not", () => {
  // Everything had a copy: the server forgets the old folder, and the line
  // still says what was removed from it.
  assert.equal(stillAbout(said({ kept: 0 }), null), true);
  assert.equal(stillAbout(said(), "/A"), true);
  // "3 files were left alone" in a folder nobody has on record any more.
  assert.equal(stillAbout(said(), null), false);
});

test("an answer about one folder says nothing about the next one", () => {
  assert.equal(stillAbout(said(), "/B"), false);
  assert.equal(stillAbout(said({ kept: 0 }), "/B"), false);
  assert.equal(stillAbout(null, "/A"), false);
});

test("a dropped connection or a gateway that gave up is not a refusal", () => {
  assert.equal(noAnswer(null, "Failed to fetch"), true);
  assert.equal(noAnswer(502, "Request failed (502)"), true);
  assert.equal(noAnswer(504, "Request failed (504)"), true);
  assert.equal(noAnswer(524, " Request failed (524) "), true);
});

test("a sentence from the server is an answer, whatever its status", () => {
  assert.equal(noAnswer(400, "/A and /B are the same folder under two names. Nothing was removed."), false);
  assert.equal(noAnswer(409, "Slideshow videos are being moved right now. Nothing was removed."), false);
  assert.equal(noAnswer(503, "The slideshow store is not connected. Nothing was removed."), false);
  // No sentence, but not from something in between either.
  assert.equal(noAnswer(403, "Request failed (403)"), false);
});

test("an unanswered removal is taken as running for its own folder and for a few minutes", () => {
  const started = { folder: "/A", at: 1_000_000 };
  assert.equal(mayStillBeRemoving(started, "/A", 1_000_000 + 60_000), true);
  assert.equal(mayStillBeRemoving(started, "/A", 1_000_000 + REMOVAL_WAIT_MS), false);
  // It finished (the folder is off the record), or the kind was moved again.
  assert.equal(mayStillBeRemoving(started, null, 1_000_000 + 60_000), false);
  assert.equal(mayStillBeRemoving(started, "/B", 1_000_000 + 60_000), false);
  // A clock that went backwards is no reason to wait for ever.
  assert.equal(mayStillBeRemoving(started, "/A", 999_000), false);
  assert.equal(mayStillBeRemoving(null, "/A", 1_000_000), false);
});

// ---- an answer from two moments -------------------------------------------

test("a finished move above a line still on its old folder is read again", () => {
  const done = move({ phase: "done", active: false });
  const before = { bulk_root: null, problem: null, busy: null, can_move: true, move: done, kinds: [kind()] };
  assert.equal(layoutBehind(before), true);
  const after = { ...before, kinds: [kind({ path: done.target, where: "custom" })] };
  assert.equal(layoutBehind(after), false);
});

test("only a finished move says where a line should be", () => {
  const base = { bulk_root: null, problem: null, busy: null, can_move: true, kinds: [kind()] };
  for (const phase of ["queued", "copying", "failed", "cancelled"]) {
    assert.equal(layoutBehind({ ...base, move: move({ phase }) }), false, phase);
  }
  assert.equal(layoutBehind({ ...base, move: null }), false);
  // The move was of a kind this answer has no line for.
  assert.equal(layoutBehind({ ...base, move: move({ phase: "done", kind: "exports" }) }), false);
});
