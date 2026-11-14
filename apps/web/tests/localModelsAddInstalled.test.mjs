// Run with `npm test` (Node's own test runner; nothing to install).
//
// Add a model and Installed models: what a row of the table says about a
// model, what its last cell offers, why Remove cannot be pressed, and the
// lines around Check and Download. The refusals themselves are the server's
// sentences and are shown as they come; these pin only what the page words
// itself.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  SUGGESTED_MODEL,
  buildLine,
  builtFromText,
  byLine,
  checkName,
  checkToneWord,
  dayText,
  downloadBlocker,
  downloadLabel,
  hfLink,
  licenceText,
  memoryText,
  otherProblems,
  removeBlocker,
  removeQuestion,
  removedSentence,
  roleLabels,
  rowAction,
  runsOnText,
  startedSentence,
  whereText,
} from "../src/lib/localModels.ts";

const GB = 1024 ** 3;

const ROLES = [
  { role: "looker", title: "Looker" },
  { role: "editor", title: "Editor" },
];

function model(over = {}) {
  return {
    id: "m1",
    repo_id: "mlx-community/gemma-4-e4b-it-4bit",
    name: "gemma-4-e4b-it-4bit",
    revision: "475b9088d29754a3379866cf5aeb6b41acd313c2",
    revision_short: "475b908",
    variant: "",
    publisher: "mlx-community",
    publisher_is: "converter",
    maker: "google",
    licence: "gemma",
    licence_name: "",
    licence_url: "https://ai.google.dev/gemma/terms",
    size_bytes: 4.8 * GB,
    memory_gb: 5.8,
    memory_measured: false,
    runtime: "mlx-vlm",
    runtime_label: "MLX-VLM",
    runtime_known: true,
    runtime_installed: true,
    runtime_problem: null,
    state: "installed",
    in_library: true,
    on_this_machine: false,
    roles: [],
    pending_roles: [],
    job: null,
    test: null,
    ...over,
  };
}

function check(over = {}) {
  return {
    repo_id: "mlx-community/gemma-4-e4b-it-4bit",
    revision: "475b9088d29754a3379866cf5aeb6b41acd313c2",
    revision_short: "475b908",
    build_date: "2026-07-06T01:11:51Z",
    ok: true,
    verdict: "This model can be added.",
    checks: [],
    problems: [],
    warnings: [],
    notes: [],
    size_bytes: 4.8 * GB,
    ...over,
  };
}

function library(over = {}) {
  return {
    path: "/Volumes/Photos/Framefound/models/library",
    where: "bulk",
    connected: true,
    problem: null,
    free_gb: 812,
    copies_to_this_machine: true,
    ...over,
  };
}

test("the suggested first model is a converter's packaging of a Google model", () => {
  assert.equal(SUGGESTED_MODEL, "mlx-community/gemma-4-e4b-it-4bit");
  assert.equal(hfLink(SUGGESTED_MODEL), "https://huggingface.co/mlx-community/gemma-4-e4b-it-4bit");
});

test("an empty field asks nothing, and a pasted address goes to the server as it is", () => {
  assert.equal(checkName("   "), null);
  assert.equal(checkName("  google/gemma-4-E4B-it \n"), "google/gemma-4-E4B-it");
  assert.equal(
    checkName("https://huggingface.co/mlx-community/gemma-4-e4b-it-4bit"),
    "https://huggingface.co/mlx-community/gemma-4-e4b-it-4bit",
  );
});

test("a build is dated by its UTC day, so a late-night build is not a day out", () => {
  assert.equal(dayText("2026-07-06T01:11:51Z"), "6 Jul 2026");
  assert.equal(dayText("2026-07-06T23:59:00Z"), "6 Jul 2026");
  assert.equal(dayText(null), "");
  assert.equal(dayText("not a date"), "");
});

test("Download says its size, and the line under it names the exact build", () => {
  assert.equal(downloadLabel(check()), "Download (4.8 GB)");
  assert.equal(
    buildLine(check()),
    "Build 475b908, 6 Jul 2026. FrameFound downloads exactly this build and never changes it " +
      "by itself. You can leave this page; the download carries on.",
  );
  assert.match(buildLine(check({ build_date: null })), /^Build 475b908\. FrameFound/);
});

test("a refused check lists its other problems once, not the verdict again", () => {
  const refused = check({
    ok: false,
    verdict: "Refused. X is on the blocked list, and blocked always wins.",
    problems: [
      "Refused. X is on the blocked list, and blocked always wins.",
      "There is not enough room: it needs 5.2 GB and the model library has 3.1 GB free.",
    ],
  });
  assert.deepEqual(otherProblems(refused), [
    "There is not enough room: it needs 5.2 GB and the model library has 3.1 GB free.",
  ]);
  assert.deepEqual(otherProblems(check()), []);
  assert.deepEqual(
    ["ok", "warn", "bad"].map(checkToneWord),
    ["ok", "warning", "problem"],
  );
});

test("a row names the maker, the packager and the build", () => {
  assert.equal(byLine(model()), "by google · packaged by mlx-community · build 475b908");
  assert.equal(
    byLine(model({ publisher: "google", publisher_is: "maker", maker: "google" })),
    "by google · build 475b908",
  );
  // A converter whose source could not be named still says who packaged it.
  assert.equal(byLine(model({ maker: null })), "packaged by mlx-community · build 475b908");
  // One build of a repository that holds several (L6 8.3): its name last.
  assert.equal(
    byLine(model({ variant: "Q4_K_M", variant_label: "Q4_K_M" })),
    "by google · packaged by mlx-community · build 475b908 · Q4_K_M",
  );
  assert.equal(startedSentence(model()), "gemma-4-e4b-it-4bit is downloading. Its bar is under Models, above. You can leave this page; the download carries on.");
});

test("memory reads as an estimate until the model has run", () => {
  assert.equal(memoryText(model()), "≈ 5.8 GB");
  assert.equal(memoryText(model({ memory_gb: 6.1, memory_measured: true })), "6.1 GB measured");
});

test("a model whose page states no licence says so rather than showing a blank", () => {
  assert.deepEqual(licenceText(model({ licence: "", licence_url: null })), {
    text: "none stated",
    url: null,
    missing: true,
  });
  assert.equal(licenceText(model()).url, "https://ai.google.dev/gemma/terms");
  assert.equal(
    licenceText(model({ licence: "other", licence_name: "gemma-terms" })).text,
    "other — gemma-terms",
  );
});

test("Runs on warns when the software is not installed and says when it is not known", () => {
  assert.deepEqual(runsOnText(model()), { label: "MLX-VLM", pill: null, sentence: null });
  assert.deepEqual(runsOnText(model({ runtime_installed: false })).pill, {
    tone: "warn",
    text: "not installed",
  });
  const unknown = runsOnText(model({ runtime_known: false, runtime_label: "", runtime: "llama-cpp" }));
  assert.equal(unknown.label, "llama-cpp");
  assert.equal(unknown.pill?.tone, "bad");
  assert.equal(unknown.sentence, "It needs a program this install does not have.");
});

test("the Role cell shows a role held and one being copied for", () => {
  assert.deepEqual(roleLabels(model({ roles: ["looker"], pending_roles: ["editor"] }), ROLES), [
    "Looker",
    "→ Editor",
  ]);
  assert.deepEqual(roleLabels(model(), ROLES), []);
});

test("a row's last cell follows the model's state, work under way first", () => {
  assert.equal(rowAction(model({ job: { state: "running", kind: "download" } })), "job");
  assert.equal(rowAction(model({ job: { state: "queued", kind: "place" } })), "job");
  assert.equal(rowAction(model({ state: "incomplete" })), "incomplete");
  assert.equal(rowAction(model({ state: "downloading" })), "stalled");
  assert.equal(rowAction(model({ in_library: false })), "missing");
  // Not known is not missing: a library that did not answer offers no
  // "Download again" for files that are probably there.
  assert.equal(rowAction(model({ in_library: null })), "installed");
  assert.equal(rowAction(model()), "installed");
});

test("Remove is held back, with the server's reason, while the model has a role", () => {
  assert.equal(
    removeBlocker(model({ roles: ["looker"] }), ROLES, library()),
    "It is the Looker. Give that role to another model, or take it away, first.",
  );
  assert.equal(
    removeBlocker(model({ roles: ["looker", "editor"] }), ROLES, library()),
    "It is the Looker and the Editor. Give those roles to other models, or take them away, first.",
  );
  assert.equal(
    removeBlocker(model({ pending_roles: ["editor"] }), ROLES, library()),
    "It is being made the Editor: its files are being copied to this machine. Cancel that first.",
  );
  assert.equal(
    removeBlocker(model({ job: { state: "running", kind: "download" } }), ROLES, library()),
    "It is being downloaded. Cancel the download first.",
  );
  assert.equal(
    removeBlocker(model({ test: { state: "running" } }), ROLES, library()),
    "It is being tested right now. Remove it when the test has finished.",
  );
  assert.match(
    removeBlocker(model(), ROLES, library({ connected: false })),
    /^The model library is not connected/,
  );
  assert.equal(removeBlocker(model(), ROLES, library()), null);
});

test("a download waits for the library, in the library's own words", () => {
  assert.equal(downloadBlocker(library()), null);
  assert.equal(downloadBlocker(null), null);
  assert.equal(
    downloadBlocker(library({ connected: false, problem: "The drive Photos is not connected." })),
    "The drive Photos is not connected.",
  );
  assert.equal(downloadBlocker(library({ connected: false })), "The model library is not connected.");
});

test("Remove asks with what it frees, and says what it freed", () => {
  assert.equal(
    removeQuestion(model()),
    "Remove gemma-4-e4b-it-4bit? This frees 4.8 GB in the model library. It can be downloaded " +
      "again. Anything it already suggested is kept.",
  );
  assert.match(removeQuestion(model({ state: "incomplete" })), /What was downloaded of it is deleted/);
  assert.equal(removedSentence("gemma-4-e4b-it-4bit", 4.8 * GB), "gemma-4-e4b-it-4bit is removed; 4.8 GB freed.");
  assert.equal(removedSentence("gemma-4-e4b-it-4bit", 0), "gemma-4-e4b-it-4bit is removed.");
});

test("Details says where the files are, and a copy on this Mac only where copies are made", () => {
  assert.equal(
    whereText(model({ on_this_machine: true }), library()),
    "In the model library on /Volumes/Photos/Framefound/models/library · also on this Mac.",
  );
  assert.equal(
    whereText(model({ on_this_machine: true }), library({ copies_to_this_machine: false, path: null })),
    "In the model library.",
  );
  assert.equal(whereText(model({ in_library: false }), library()), "Not in the model library.");
  assert.match(whereText(model({ in_library: null }), library()), /could not be checked just now/);
});

test("what a model was built from names each publisher's standing", () => {
  assert.equal(
    builtFromText({ repo_id: "google/gemma-4-E4B-it", publisher: "google", publisher_is: "maker", found: true }),
    "google/gemma-4-E4B-it, by google — an approved maker",
  );
  assert.equal(
    builtFromText({ repo_id: "google/gemma-4-E4B-it", publisher: "google", publisher_is: "maker", found: false }),
    "google/gemma-4-E4B-it — not on Hugging Face now",
  );
});
