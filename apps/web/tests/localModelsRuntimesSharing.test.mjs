// Run with `npm test` (Node's own test runner; nothing to install).
//
// Software that runs models and Sharing this Mac: which buttons the
// software's row offers and what each asks, what a failed install says, and
// what Save sends. The refusals are the server's sentences; these pin what
// the page decides and words itself.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BAD_BUDGET,
  BAD_CLOCK,
  BAD_IDLE,
  checkedSentence,
  failedInstallSentence,
  installStartedSentence,
  lastInstall,
  returnQuestion,
  rollbackQuestion,
  runtimePlan,
  sharingPatch,
  sharingSavedSentence,
  sharingValue,
  updateQuestion,
  versionQuestion,
} from "../src/lib/localModels.ts";

function runtime(over = {}) {
  return {
    name: "mlx-vlm",
    label: "MLX-VLM",
    supported: true,
    installed: "0.7.12",
    previous: "0.7.11",
    latest: "0.7.12",
    latest_problem: null,
    update_available: false,
    job: null,
    ...over,
  };
}

function install(over = {}) {
  return {
    id: "j1",
    kind: "runtime_install",
    title: "Installing MLX-VLM 0.7.13",
    subject: "MLX-VLM 0.7.13",
    state: "done",
    message: "",
    detail: null,
    cancelling: false,
    ...over,
  };
}

const SETTINGS = {
  day_budget_gb: 8,
  night_budget_gb: 20,
  night_start: "21:30",
  night_end: "05:30",
  idle_unload_minutes: 5,
  paused: false,
};

test("a row with nothing installed offers Install, and nothing to update or roll back", () => {
  const plan = runtimePlan(runtime({ installed: null, previous: null, latest: "0.7.12" }), []);
  assert.equal(plan.install, true);
  assert.equal(plan.update, null);
  assert.equal(plan.rollback, null);
  assert.equal(plan.failed, null);
});

test("a newer version is offered by the server's own comparison, never by the strings differing", () => {
  // latest differs but the kind says it is not newer (an older index answer).
  const notNewer = runtimePlan(runtime({ latest: "0.7.10", update_available: false }), []);
  assert.equal(notNewer.update, null);
  const newer = runtimePlan(runtime({ latest: "0.7.13", update_available: true }), []);
  assert.equal(newer.update, "0.7.13");
  assert.equal(newer.install, false);
  assert.equal(newer.rollback, "0.7.11");
});

test("after a roll back the newer version kept is offered as the swap back, not a second download", () => {
  // The server swapped them: 0.7.11 in use, 0.7.12 kept, and 0.7.12 is the newest.
  const swapped = runtime({ installed: "0.7.11", previous: "0.7.12", latest: "0.7.12", update_available: true });
  const plan = runtimePlan(swapped, []);
  assert.equal(plan.rollback, "0.7.12");
  assert.equal(plan.rollbackIsNewer, true);
  assert.equal(plan.update, null);
  assert.match(returnQuestion("0.7.12"), /^Go back to 0\.7\.12\? .*Nothing is downloaded/);
  // An ordinary kept older version is a roll back, and Update stays.
  const older = runtimePlan(runtime({ latest: "0.7.13", update_available: true }), []);
  assert.equal(older.rollbackIsNewer, false);
  assert.equal(older.update, "0.7.13");
});

test("the install under way is the row's progress and hides an older failure", () => {
  const working = install({ id: "j2", state: "running", message: "Making its own environment…" });
  const failed = install({ id: "j1", state: "failed", message: "uv could not resolve it." });
  const plan = runtimePlan(runtime({ job: working }), [working, failed]);
  assert.equal(plan.working?.id, "j2");
  assert.equal(plan.failed, null);
  assert.equal(plan.retry, null);
});

test("a failed newest install is shown with Try again in place of the Update it would repeat", () => {
  const failed = install({ state: "failed", message: "The package index could not be reached." });
  const plan = runtimePlan(runtime({ latest: "0.7.13", update_available: true }), [failed]);
  assert.equal(plan.failed?.id, "j1");
  assert.equal(plan.retry, "0.7.13");
  assert.equal(plan.update, null, "two buttons would install the same version");
  assert.equal(plan.rollback, "0.7.11");
});

test("an install that finished after a failed one leaves nothing to report", () => {
  const done = install({ id: "j3", state: "done" });
  const failed = install({ id: "j1", state: "failed" });
  assert.equal(runtimePlan(runtime(), [done, failed]).failed, null);
});

test("another program's install is not this row's", () => {
  const other = install({ subject: "llama.cpp b4500", state: "failed" });
  assert.equal(lastInstall(runtime(), [other]), null);
  assert.equal(runtimePlan(runtime(), [other]).failed, null);
});

test("a failed install says the version in use was left alone, or that nothing was installed", () => {
  const failed = install({ state: "failed", message: "uv could not resolve it." });
  assert.equal(
    failedInstallSentence(failed, runtime()),
    "Installing MLX-VLM 0.7.13 did not finish. uv could not resolve it. MLX-VLM 0.7.12 is still the version in use.",
  );
  assert.equal(
    failedInstallSentence(failed, runtime({ installed: null })),
    "Installing MLX-VLM 0.7.13 did not finish. uv could not resolve it. Nothing was installed.",
  );
});

test("Update and Roll back ask before the version in use changes; a first install does not", () => {
  assert.match(updateQuestion(runtime(), "0.7.13"), /^Update MLX-VLM from 0\.7\.12 to 0\.7\.13\?/);
  assert.match(updateQuestion(runtime(), "0.7.13"), /0\.7\.12 is kept/);
  assert.match(rollbackQuestion("0.7.11"), /^Go back to 0\.7\.11\? The model in memory is unloaded/);
  assert.equal(versionQuestion(runtime({ installed: null }), "0.7.10"), null);
  assert.equal(versionQuestion(runtime(), "0.7.12"), null);
  assert.match(versionQuestion(runtime(), "0.7.10") ?? "", /in place of 0\.7\.12\?/);
});

test("the install's sentence names the version the server looked up", () => {
  const answer = runtime({ job: install({ state: "queued", subject: "MLX-VLM 0.7.13" }) });
  assert.equal(
    installStartedSentence(answer),
    "Installing MLX-VLM 0.7.13. It takes a few minutes; you can leave this page.",
  );
  assert.match(installStartedSentence(runtime()), /^Installing MLX-VLM\. /);
});

test("Check now says whether a newer version is out", () => {
  assert.equal(checkedSentence(runtime()), "MLX-VLM 0.7.12 is the newest.");
  assert.equal(
    checkedSentence(runtime({ latest: "0.7.13", update_available: true })),
    "MLX-VLM 0.7.13 is out; this Mac has 0.7.12.",
  );
  assert.equal(
    checkedSentence(runtime({ installed: null, latest: "0.7.13" })),
    "The newest MLX-VLM is 0.7.13.",
  );
  assert.equal(
    checkedSentence(runtime({ latest_problem: "The package index could not be reached." })),
    "The package index could not be reached.",
  );
});

test("Save sends only the figures that changed, as numbers and HH:MM", () => {
  const { patch, changed, problem } = sharingPatch(
    { day_budget_gb: "12", night_budget_gb: "20", night_start: "9:05", idle_unload_minutes: "5" },
    SETTINGS,
  );
  assert.equal(problem, null);
  assert.deepEqual(patch, { day_budget_gb: 12, night_start: "09:05" });
  assert.deepEqual(changed, ["day_budget_gb", "night_start"]);
});

test("a time input's seconds and a decimal comma are read as the owner meant", () => {
  const { patch } = sharingPatch({ night_end: "06:00:00", day_budget_gb: "8,5" }, SETTINGS);
  assert.deepEqual(patch, { day_budget_gb: 8.5, night_end: "06:00" });
});

test("what is not a number or a time is refused with the server's sentence and nothing is sent", () => {
  assert.equal(sharingPatch({ day_budget_gb: "" }, SETTINGS).problem, BAD_BUDGET);
  assert.equal(sharingPatch({ night_budget_gb: "lots" }, SETTINGS).problem, BAD_BUDGET);
  assert.equal(sharingPatch({ night_start: "half nine" }, SETTINGS).problem, BAD_CLOCK);
  assert.equal(sharingPatch({ idle_unload_minutes: "" }, SETTINGS).problem, BAD_IDLE);
  assert.deepEqual(sharingPatch({ day_budget_gb: "12", night_end: "x" }, SETTINGS).patch, {});
});

test("a figure out of range is sent, so the server's own sentence is the one shown", () => {
  // Only the server knows the Mac's memory ("This Mac has 32 GB…").
  assert.deepEqual(sharingPatch({ day_budget_gb: "600" }, SETTINGS).patch, { day_budget_gb: 600 });
  assert.deepEqual(sharingPatch({ night_budget_gb: "4" }, SETTINGS).patch, { night_budget_gb: 4 });
});

test("the fields show the server's figures without a trailing .0", () => {
  assert.equal(sharingValue({ ...SETTINGS, day_budget_gb: 8.0 }, "day_budget_gb"), "8");
  assert.equal(sharingValue({ ...SETTINGS, day_budget_gb: 12.5 }, "day_budget_gb"), "12.5");
  assert.equal(sharingValue(SETTINGS, "night_start"), "21:30");
});

test("a saved budget says where the changed pills are; the idle time alone does not", () => {
  assert.match(sharingSavedSentence(["day_budget_gb"]), /what fits by day and overnight/);
  assert.equal(sharingSavedSentence(["idle_unload_minutes"]), "Saved.");
});
