// Run with `npm test` (Node's own test runner; nothing to install).
//
// The AI models page tells the owner of the Mac what a model costs in memory,
// what is running and what is left to set up. These pin the figures and the
// words it shows, and when it reads itself again: quickly while something
// moves, slowly when nothing does, never where models cannot run.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  FAST_MS,
  SLOW_MS,
  aheadOf,
  clockLine,
  endedJobs,
  endedSentence,
  fitsText,
  gbFigure,
  gbText,
  jobPercent,
  jobSentence,
  leadSentence,
  memoryLine,
  nameOf,
  ofText,
  pollEvery,
  serverLine,
  startSteps,
  takesText,
  waitWords,
  whenText,
} from "../src/lib/localModels.ts";

const GB = 1024 ** 3;
const NOW = Date.parse("2026-11-12T14:05:00Z");

function settings(over = {}) {
  return {
    day_budget_gb: 8,
    night_budget_gb: 20,
    night_start: "21:30",
    night_end: "05:30",
    idle_unload_minutes: 5,
    paused: false,
    paused_at: null,
    paused_by: null,
    hf_token_configured: false,
    hf_token_problem: null,
    ...over,
  };
}

function job(over = {}) {
  return {
    id: "j1",
    kind: "download",
    title: "Downloading gemma-4-e4b-it-4bit",
    subject: "mlx-community/gemma-4-e4b-it-4bit@475b9081",
    model_id: "m1",
    state: "running",
    message: "",
    detail: null,
    not_before: null,
    phase: "downloading",
    done: 0,
    total: 0,
    cancelling: false,
    can_cancel: true,
    requested_by: "owner@example.com",
    created_at: "2026-11-12T14:00:00Z",
    started_at: "2026-11-12T14:00:01Z",
    finished_at: null,
    ...over,
  };
}

function model(over = {}) {
  return {
    id: "m1",
    repo_id: "mlx-community/gemma-4-e4b-it-4bit",
    name: "gemma-4-e4b-it-4bit",
    state: "installed",
    approved: true,
    runtime_installed: true,
    job: null,
    test: null,
    ...over,
  };
}

function overview(over = {}) {
  return {
    mode: "ready",
    note: null,
    can_manage: true,
    machine: { system: "Darwin", machine: "arm64", memory_gb: 32 },
    now: { local_time: "2026-11-12T14:05:00", overnight: false, budget_gb: 8, changes_at: "2026-11-12T21:30:00" },
    settings: settings(),
    publishers: { makers: ["google"], converters: ["mlx-community"], blocked: [] },
    server: { state: "idle" },
    library: null,
    roles: [],
    models: [],
    runtimes: [],
    jobs: [],
    ...over,
  };
}

test("a size reads in MB below a gigabyte, GB with one decimal, and TB past 1024 GB", () => {
  assert.equal(gbText(0), "0 MB");
  assert.equal(gbText(512 * 1024 ** 2), "512 MB");
  assert.equal(gbText(4.8 * GB), "4.8 GB");
  assert.equal(gbText(1023.99 * 1024 ** 2), "1.0 GB");
  assert.equal(gbText(1.5 * 1024 * GB), "1.5 TB");
  assert.equal(gbText(null), "—");
  assert.equal(gbText(-1), "—");
});

test("bytes done are given in the unit of the whole, so the two figures compare", () => {
  assert.equal(ofText(2 * GB, 4.8 * GB), "2.0 of 4.8 GB");
  assert.equal(ofText(100 * 1024 ** 2, 600 * 1024 ** 2), "100 of 600 MB");
});

test("a budget reads without a trailing .0", () => {
  assert.equal(gbFigure(8), "8 GB");
  assert.equal(gbFigure(6.14), "6.1 GB");
  assert.equal(gbFigure(null), "—");
});

test("a download shows how far it has got, and a copy says it is coming to this Mac", () => {
  const download = job({ done: 2 * GB, total: 4.8 * GB });
  assert.equal(jobPercent(download), 41);
  assert.equal(jobSentence(download), "2.0 of 4.8 GB · 41%");
  const copy = job({ kind: "place", phase: "copying", done: 2 * GB, total: 4.8 * GB });
  assert.equal(jobSentence(copy), "Copying to this Mac — 2.0 of 4.8 GB · 41%");
});

test("a job with no byte count gets no bar, only its own sentence", () => {
  const install = job({ kind: "runtime_install", phase: "", message: "Installing 0.7.13 — a few minutes" });
  assert.equal(jobPercent(install), null);
  assert.equal(jobSentence(install), "Installing 0.7.13 — a few minutes");
  assert.equal(jobSentence(job({ message: "" })), "Working…");
});

test("a bar never runs past either end", () => {
  assert.equal(jobPercent(job({ done: 6 * GB, total: 4 * GB })), 100);
  assert.equal(jobPercent(job({ done: -5, total: 4 * GB })), 0);
});

test("a queued job names the work it waits behind; a cancelled one says it is stopping", () => {
  const running = job({ id: "j0", title: "Installing MLX-VLM 0.7.13" });
  const queued = job({ id: "j1", state: "queued" });
  assert.equal(aheadOf(queued, [running, queued]), "Installing MLX-VLM 0.7.13");
  assert.equal(aheadOf(running, [running, queued]), null);
  assert.equal(jobSentence(queued, "Installing MLX-VLM 0.7.13"), "Waiting its turn — Installing MLX-VLM 0.7.13.");
  assert.equal(jobSentence(queued), "Waiting its turn.");
  assert.equal(jobSentence(job({ cancelling: true, done: GB, total: 2 * GB })), "Stopping…");
  const waiting = job({ state: "waiting", message: "Waiting for the overnight window (starts 21:30)." });
  assert.equal(jobSentence(waiting), "Waiting for the overnight window (starts 21:30).");
});

test("the page reads itself quickly while anything moves and slowly when nothing does", () => {
  assert.equal(pollEvery(overview(), NOW), SLOW_MS);
  assert.equal(pollEvery(overview({ jobs: [job()] }), NOW), FAST_MS);
  assert.equal(pollEvery(overview({ jobs: [job({ state: "queued" })] }), NOW), FAST_MS);
  assert.equal(pollEvery(overview({ server: { state: "loading" } }), NOW), FAST_MS);
  assert.equal(pollEvery(overview({ server: { state: "busy" } }), NOW), FAST_MS);
  // Finished work does not keep the page busy.
  assert.equal(pollEvery(overview({ jobs: [job({ state: "done" })] }), NOW), SLOW_MS);
});

test("a wait reads quickly only when its time is near", () => {
  const soon = job({ state: "waiting", not_before: new Date(NOW + 30_000).toISOString() });
  const tonight = job({ state: "waiting", not_before: new Date(NOW + 7 * 3600_000).toISOString() });
  const paused = job({ state: "waiting", not_before: null });
  assert.equal(pollEvery(overview({ jobs: [soon] }), NOW), FAST_MS);
  assert.equal(pollEvery(overview({ jobs: [tonight] }), NOW), SLOW_MS);
  assert.equal(pollEvery(overview({ jobs: [paused] }), NOW), SLOW_MS);
  assert.equal(pollEvery(overview({ jobs: [{ ...tonight, cancelling: true }] }), NOW), FAST_MS);
});

test("where models cannot run the page never reads itself again", () => {
  assert.equal(pollEvery(overview({ mode: "unavailable", jobs: [job()] }), NOW), null);
});

test("a model's name comes out of a job's subject, variant or not", () => {
  assert.equal(nameOf("mlx-community/gemma-4-e4b-it-4bit@475b9081"), "gemma-4-e4b-it-4bit");
  assert.equal(nameOf("google/gemma-4-E4B@0123abcd/q4"), "gemma-4-E4B");
  assert.equal(nameOf("MLX-VLM 0.7.13"), "MLX-VLM 0.7.13");
});

test("a job that ended while the page was open is noticed once, in plain words", () => {
  const before = [job({ id: "a" }), job({ id: "b", state: "done" })];
  const after = [job({ id: "a", state: "done" }), job({ id: "b", state: "done" })];
  const ended = endedJobs(before, after);
  assert.deepEqual(ended.map((j) => j.id), ["a"]);
  assert.equal(endedSentence(ended[0]), "gemma-4-e4b-it-4bit is installed.");
  assert.equal(
    endedSentence(job({ kind: "runtime_install", subject: "MLX-VLM 0.7.13", state: "done" })),
    "MLX-VLM 0.7.13 is installed.",
  );
  assert.equal(
    endedSentence(job({ state: "failed", message: "Hugging Face did not answer." })),
    "Downloading gemma-4-e4b-it-4bit did not finish. Hugging Face did not answer.",
  );
  assert.equal(endedSentence(job({ state: "cancelled" })), "Downloading gemma-4-e4b-it-4bit: cancelled.");
});

test("the heading line says the Mac's time, the budget in force and when the other starts", () => {
  const day = { local_time: "2026-11-12T14:05:00", overnight: false, budget_gb: 8, changes_at: "2026-11-12T21:30:00" };
  assert.equal(
    clockLine(day, settings()),
    "It is 14:05 on this Mac — daytime, models up to 8 GB. Overnight (up to 20 GB) starts at 21:30.",
  );
  const night = { local_time: "2026-11-12T23:10:00", overnight: true, budget_gb: 20, changes_at: "2026-11-13T05:30:00" };
  assert.equal(
    clockLine(night, settings()),
    "It is 23:10 on this Mac — overnight, models up to 20 GB. Daytime (up to 8 GB) starts at 05:30.",
  );
  assert.equal(
    clockLine({ ...day, changes_at: null }, settings({ paused: true })),
    "It is 14:05 on this Mac — models up to 8 GB at any hour. Local AI is paused.",
  );
});

test("the Memory tile reads the budget now and the one that follows", () => {
  const day = { local_time: "2026-11-12T14:05:00", overnight: false, budget_gb: 8, changes_at: "2026-11-12T21:30:00" };
  assert.equal(memoryLine(day, settings()), "8 GB until 21:30, then 20 GB");
  const night = { local_time: "2026-11-12T23:10:00", overnight: true, budget_gb: 20, changes_at: "2026-11-13T05:30:00" };
  assert.equal(memoryLine(night, settings()), "20 GB until 05:30, then 8 GB");
  assert.equal(memoryLine({ ...day, changes_at: null }, settings()), "8 GB at any hour");
});

test("a wait is said in minutes, and in hours once it is long", () => {
  assert.equal(waitWords(20), "less than a minute");
  assert.equal(waitWords(75), "1 minute");
  assert.equal(waitWords(130), "2 minutes");
  assert.equal(waitWords(3 * 3600), "about 3 hours");
});

test("a time this week reads as its day, an older one as its date", () => {
  // Built on this machine's own clock, as the page reads it.
  const tuesday = new Date(2026, 10, 10, 14, 2);
  const thursday = new Date(2026, 10, 12, 9, 0).getTime();
  assert.equal(whenText(tuesday.toISOString(), thursday), "Tue 14:02");
  assert.equal(whenText(new Date(2026, 10, 3, 8, 5).toISOString(), thursday), "3 Nov 08:05");
  assert.equal(whenText(null, thursday), "");
});

test("Right now says what the model server is doing, and offers Unload only when it can free something", () => {
  const loaded = serverLine(
    { state: "ready", model_name: "gemma-4-e4b-it-4bit", memory_gb: 6.1, unloads_in_s: 120 },
    settings(),
  );
  assert.equal(loaded.text, "gemma-4-e4b-it-4bit is loaded, using 6.1 GB — unloads in 2 minutes if nothing asks");
  assert.equal(loaded.unload, true);
  assert.deepEqual(serverLine({ state: "idle" }, settings()), {
    text: "Idle — nothing loaded",
    detail: null,
    unload: false,
  });
  assert.equal(serverLine({ state: "loading", model_name: "x" }, settings()).text, "Starting x…");
  const down = serverLine({ state: "down", problem: "It exited." }, settings());
  assert.equal(down.text, "The model server is not running — it restarts by itself within a minute.");
  assert.equal(down.detail, "It exited.");
  assert.equal(down.unload, false);
});

test("while paused, Right now says since when and by whom", () => {
  const at = new Date(2026, 10, 10, 14, 2).toISOString();
  const line = serverLine(
    { state: "idle" },
    settings({ paused: true, paused_at: at, paused_by: "owner@example.com" }),
    new Date(2026, 10, 12, 9, 0).getTime(),
  );
  assert.equal(line.text, "Paused since Tue 14:02 by owner@example.com");
});

test("the memory pill says by day, overnight only or over budget, and why", () => {
  assert.deepEqual(
    [fitsText("day", settings()).tone, fitsText("day", settings()).label],
    ["ok", "by day"],
  );
  const night = fitsText("night", settings());
  assert.equal(night.tone, "warn");
  assert.equal(night.label, "overnight only");
  assert.equal(night.sentence, "It needs more than the daytime budget (8 GB), so it only runs overnight, 21:30–05:30.");
  const never = fitsText("never", settings());
  assert.equal(never.tone, "bad");
  assert.match(never.sentence, /overnight budget \(20 GB\)/);
});

test("what a model reads is said in the page's words", () => {
  assert.equal(takesText(["image", "text"]), "pictures, text");
  assert.equal(takesText(["audio"]), "sound");
  assert.equal(takesText([]), "not stated");
});

test("a note's first sentence is set apart to be shown in bold", () => {
  assert.deepEqual(leadSentence("Demonstration only. Nothing is real."), ["Demonstration only.", "Nothing is real."]);
  assert.deepEqual(leadSentence("One sentence"), ["One sentence", ""]);
});

test("Getting started on a new Mac: nothing done, and Install names the software to install", () => {
  const runtime = { name: "mlx_vlm", label: "MLX-VLM", supported: true, installed: null, job: null, unsupported_reason: null };
  const plan = startSteps(overview({ runtimes: [runtime] }));
  assert.deepEqual(plan.steps.map((s) => [s.key, s.done]), [
    ["software", false],
    ["model", false],
    ["role", false],
    ["test", false],
  ]);
  assert.equal(plan.done, false);
  assert.equal(plan.install?.label, "MLX-VLM");
  assert.equal(plan.cannotInstall, null);
  assert.equal(plan.testModel, null);
});

test("Getting started shows the work under way on each line", () => {
  const install = job({ id: "i", kind: "runtime_install", subject: "MLX-VLM 0.7.13" });
  const download = job({ id: "d", state: "queued" });
  const runtime = { name: "mlx_vlm", label: "MLX-VLM", supported: true, installed: null, job: install };
  const plan = startSteps(overview({ runtimes: [runtime], jobs: [install, download] }));
  assert.equal(plan.steps[0].job?.id, "i");
  assert.equal(plan.steps[1].job?.id, "d");
});

test("an Intel Mac gets the reason instead of an Install button", () => {
  const runtime = {
    name: "mlx_vlm",
    label: "MLX-VLM",
    supported: false,
    installed: null,
    job: null,
    unsupported_reason: "MLX models need a Mac with Apple silicon.",
  };
  const plan = startSteps(overview({ runtimes: [runtime] }));
  assert.equal(plan.install, null);
  assert.equal(plan.cannotInstall, "MLX models need a Mac with Apple silicon.");
});

test("Getting started folds into one line once all four are done, naming the ready roles", () => {
  const runtime = { name: "mlx_vlm", label: "MLX-VLM", supported: true, installed: "0.7.12", job: null };
  const tested = model({ test: { state: "done" } });
  const roles = [
    { role: "looker", title: "Looker", ready: true, model_id: "m1", job: null },
    { role: "editor", title: "Editor", ready: true, model_id: "m1", job: null },
  ];
  const plan = startSteps(overview({ runtimes: [runtime], models: [tested], roles }));
  assert.equal(plan.done, true);
  assert.equal(plan.summary, "Set up — the Looker and the Editor are ready.");
  const one = startSteps(overview({ runtimes: [runtime], models: [tested], roles: [roles[0], { ...roles[1], model_id: null, ready: false }] }));
  assert.equal(one.summary, "Set up — the Looker is ready.");
});

test("Test picks an installed model that can run before one that cannot", () => {
  const blocked = model({ id: "b", approved: false });
  const downloading = model({ id: "d", state: "downloading" });
  const good = model({ id: "g" });
  assert.equal(startSteps(overview({ models: [downloading, blocked, good] })).testModel?.id, "g");
  assert.equal(startSteps(overview({ models: [downloading, blocked] })).testModel?.id, "b");
});
