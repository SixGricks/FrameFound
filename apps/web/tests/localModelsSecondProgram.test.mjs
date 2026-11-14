// Run with `npm test` (Node's own test runner; nothing to install).
//
// The second program (llama.cpp) in Software that runs models, and one build
// of a GGUF repository in the Installed models table (L6 8.3, 8.5). The
// runtime rows are in the server's shapes (api/L1-D.md, L6-S5); the
// sentences the server writes are shown as they come, so these pin only what
// the page decides and words itself — and that the MLX-VLM row reads as it
// did before.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  buildPartsText,
  byLine,
  checkTitle,
  dailyBuildSentence,
  installedText,
  runtimeNeeded,
  runtimePlan,
  softwareNotes,
  versionPlaceholder,
  versionQuestion,
} from "../src/lib/localModels.ts";

function mlx(over = {}) {
  return {
    name: "mlx-vlm",
    label: "MLX-VLM",
    supported: true,
    installed: "0.7.12",
    previous: "0.7.11",
    installed_at: null,
    about: "",
    installed_detail: null,
    update_note: "",
    version_hint: "0.7.12",
    models: 1,
    latest: "0.7.12",
    latest_problem: null,
    update_available: false,
    job: null,
    ...over,
  };
}

function llama(over = {}) {
  return mlx({
    name: "llama-cpp",
    label: "llama.cpp",
    installed: null,
    previous: null,
    update_note:
      "llama.cpp brings out a numbered version every few weeks and a daily build most days. " +
      "FrameFound offers the numbered ones. A daily build can be installed by its number when a " +
      "model needs something newer.",
    version_hint: "0.9.0 or a daily build such as b12318",
    models: 0,
    latest: null,
    ...over,
  });
}

function gguf(over = {}) {
  return {
    name: "demo-omni-30b-GGUF",
    revision_short: "49a5b1a",
    publisher: "example-gguf",
    publisher_is: "converter",
    maker: "nvidia",
    variant: "Q4_K_M",
    variant_label: "Q4_K_M",
    takes: ["text", "image"],
    runtime: "llama-cpp",
    runtime_label: "llama.cpp",
    ...over,
  };
}

test("an empty llama.cpp row says nothing needs it, but the first program never does", () => {
  assert.equal(runtimeNeeded(llama()), "Nothing you have added needs it.");
  // MLX-VLM with nothing installed is step one of Getting started, not spare.
  assert.equal(runtimeNeeded(mlx({ installed: null, models: 0 }), true), null);
  // Once a GGUF model is added (even one still downloading) the row is needed.
  assert.equal(runtimeNeeded(llama({ models: 1 })), null);
});

test("the installed line names the daily build a numbered version came from", () => {
  const numbered = llama({
    installed: "0.9.0",
    installed_detail: "build b12110",
    about: "build b12110",
    installed_at: "2026-11-12T10:00:00Z",
  });
  assert.match(installedText(numbered), /^0\.9\.0 \(build b12110\), since 1[23] Nov 2026$/);
  assert.equal(installedText(llama()), "Not installed.");
  // MLX-VLM has nothing more to say: the line reads exactly as before.
  assert.equal(installedText(mlx()), "0.7.12");
});

test("a daily build in use says why there is no Update button, and a numbered one does not", () => {
  const daily = llama({ installed: "b12318", previous: "0.9.0", latest: "0.10.0" });
  assert.equal(
    dailyBuildSentence(daily),
    "A daily build (b12318) is installed. FrameFound does not offer updates over a daily " +
      "build; install a numbered version below when you want one.",
  );
  // The server never calls a numbered version newer than a daily build, so the
  // plan offers no Update, only the way back to the version kept.
  const plan = runtimePlan(daily, []);
  assert.equal(plan.update, null);
  assert.equal(plan.rollback, "0.9.0");
  assert.equal(dailyBuildSentence(llama({ installed: "0.9.0" })), null);
  assert.equal(dailyBuildSentence(llama()), null);
  assert.equal(dailyBuildSentence(mlx()), null);
});

test("the second program's row offers the same buttons as the first", () => {
  assert.equal(runtimePlan(llama({ latest: "0.9.0" }), []).install, true);
  const older = llama({ installed: "0.9.0", previous: "b12318", latest: "0.10.0", update_available: true });
  const plan = runtimePlan(older, []);
  assert.equal(plan.update, "0.10.0");
  assert.equal(plan.rollback, "b12318");
  assert.equal(
    versionQuestion(older, "b12400"),
    "Install llama.cpp b12400 in place of 0.9.0? The model in memory is unloaded; it loads " +
      "again with b12400 the next time it is needed.",
  );
  // Reinstalling the version in use (doctor's way back for a broken one) is
  // sent without a question: nothing is replaced by anything else.
  assert.equal(versionQuestion(older, "0.9.0"), null);
});

test("the version field's example is the server's hint for that program", () => {
  assert.equal(versionPlaceholder(llama()), "0.9.0 or a daily build such as b12318");
  assert.equal(versionPlaceholder(mlx()), "0.7.12");
  // An older server's answer has no hint: a version it knows stands in.
  assert.equal(versionPlaceholder(mlx({ version_hint: "" })), "0.7.11");
  assert.equal(versionPlaceholder(mlx({ version_hint: "", previous: null, latest: null })), "");
});

test("Check now says where it asks, and llama.cpp's source is named under the section", () => {
  assert.equal(checkTitle(mlx()), "Ask the package index for the newest MLX-VLM now");
  assert.equal(checkTitle(llama()), "Ask GitHub for the newest numbered llama.cpp now");
  const before = softwareNotes([mlx()]);
  assert.equal(before.length, 1);
  assert.match(before[0], /^Each program lives in its own folder with its own Python/);
  const both = softwareNotes([mlx(), llama()]);
  assert.equal(
    both.at(-1),
    "llama.cpp is downloaded from its makers' page on GitHub and checked against the checksum " +
      "listed there.",
  );
  // llama.cpp has no Python of its own; the shared sentence must not say it does.
  assert.doesNotMatch(both[0], /Python/);
});

test("a GGUF model's row names its build after the build id, and Details what came with it", () => {
  assert.equal(byLine(gguf()), "by nvidia · packaged by example-gguf · build 49a5b1a · Q4_K_M");
  assert.equal(
    buildPartsText(gguf()),
    "Q4_K_M · the model file, and the part that reads pictures",
  );
  assert.equal(
    buildPartsText(gguf({ variant: "Q8_0", variant_label: "Q8_0", takes: ["text"] })),
    "Q8_0 · the model file; it reads text only",
  );
  // A GGUF repository of one build has no label: nothing to tell apart.
  const single = gguf({ variant_label: "" });
  assert.equal(byLine(single), "by nvidia · packaged by example-gguf · build 49a5b1a");
  assert.equal(buildPartsText(single), null);
  // MLX: unchanged.
  const mlxModel = gguf({ variant: "", variant_label: "", publisher: "mlx-community", maker: "google", revision_short: "475b908" });
  assert.equal(byLine(mlxModel), "by google · packaged by mlx-community · build 475b908");
  assert.equal(buildPartsText(mlxModel), null);
});
