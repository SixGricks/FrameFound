// Run with `npm test` (Node's own test runner; nothing to install).
//
// Two builds of one GGUF repository side by side (L6 W1 lets the owner
// download a second one; the server names both rows after the repository):
// every sentence about one row has to say which build it means. Also the
// two fixes to "Which build" and "Install a particular version" that review
// found: a settled choice is checked once, and only a refusal of the version
// typed is said beside the field.

import assert from "node:assert/strict";
import { mock, test } from "node:test";

import {
  BUILD_RECHECK_MS,
  isVersionRefusal,
  removeQuestion,
  removedSentence,
  roleOptions,
  roleQuestion,
  rowName,
  settler,
  startedSentence,
  testTitle,
} from "../src/lib/localModels.ts";

const GB = 1024 ** 3;

function build(over = {}) {
  return {
    id: "q4",
    name: "demo-vision-gguf",
    revision_short: "7777777",
    variant: "Q4_K_M",
    variant_label: "Q4_K_M",
    size_bytes: 3.1 * GB,
    state: "installed",
    takes: ["image", "text"],
    role_problems: { looker: null, editor: null },
    ...over,
  };
}

const q8 = (over = {}) =>
  build({ id: "q8", variant: "Q8_0", variant_label: "Q8_0", size_bytes: 4.6 * GB, ...over });

function role(over = {}) {
  return {
    role: "looker",
    title: "Looker",
    model_id: null,
    model_name: null,
    pending_model_id: null,
    pending_model_name: null,
    job: null,
    ...over,
  };
}

test("two builds of one repository at one revision read apart in Give this role to…", () => {
  const options = roleOptions([build(), q8()], role());
  assert.deepEqual(
    options.map((option) => option.label),
    ["demo-vision-gguf (Q4_K_M)", "demo-vision-gguf (Q8_0)"],
  );
  // The reason still follows the name.
  const held = roleOptions([build(), q8()], role({ model_id: "q8" }));
  assert.equal(held[1].label, "demo-vision-gguf (Q8_0) — has this role");
});

test("one build downloaded at two revisions adds the revision, and MLX reads as before", () => {
  const options = roleOptions([build(), build({ id: "old", revision_short: "1234567" })], role());
  assert.deepEqual(
    options.map((option) => option.label),
    ["demo-vision-gguf (Q4_K_M, build 7777777)", "demo-vision-gguf (Q4_K_M, build 1234567)"],
  );
  const mlx = (over) => build({ name: "gemma-4-e4b-it-4bit", variant: "", variant_label: "", ...over });
  assert.deepEqual(
    roleOptions([mlx({ id: "a", revision_short: "475b908" }), mlx({ id: "b", revision_short: "3f1c2aa" })], role()).map(
      (option) => option.label,
    ),
    ["gemma-4-e4b-it-4bit (build 475b908)", "gemma-4-e4b-it-4bit (build 3f1c2aa)"],
  );
  assert.equal(rowName(mlx({})), "gemma-4-e4b-it-4bit");
});

test("Remove asks about the build it deletes, and says which went", () => {
  assert.equal(
    removeQuestion(q8()),
    "Remove demo-vision-gguf (Q8_0)? This frees 4.6 GB in the model library. It can be " +
      "downloaded again. Anything it already suggested is kept.",
  );
  assert.notEqual(removeQuestion(build()), removeQuestion(q8({ size_bytes: 3.1 * GB })));
  assert.match(removeQuestion(q8({ state: "incomplete" })), /^Remove demo-vision-gguf \(Q8_0\)\? What was downloaded/);
  assert.equal(removedSentence(rowName(q8()), 4.6 * GB), "demo-vision-gguf (Q8_0) is removed; 4.6 GB freed.");
  assert.match(startedSentence(q8()), /^demo-vision-gguf \(Q8_0\) is downloading\./);
  assert.match(
    roleQuestion(role({ model_id: "q4", model_name: "demo-vision-gguf" }), q8()),
    /^Make demo-vision-gguf \(Q8_0\) the Looker instead of/,
  );
});

test("Test's title promises a picture only to a build that reads pictures", () => {
  assert.equal(testTitle(build()), "Ask demo-vision-gguf (Q4_K_M) one question about a picture");
  assert.equal(testTitle(q8({ takes: ["text"] })), "Ask demo-vision-gguf (Q8_0) one question in words");
});

test("arrowing through the builds checks only the one the keyboard stops on", () => {
  mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const checked = [];
    const wait = settler(BUILD_RECHECK_MS);
    // Eleven rows passed with the arrow held down, a press every 30 ms.
    for (let row = 1; row <= 11; row += 1) {
      wait.choose(() => checked.push(row));
      mock.timers.tick(30);
    }
    assert.deepEqual(checked, []);
    mock.timers.tick(BUILD_RECHECK_MS);
    assert.deepEqual(checked, [11]);
    // An edit of the field (stop) drops a choice still waiting.
    wait.choose(() => checked.push("dropped"));
    wait.stop();
    mock.timers.tick(BUILD_RECHECK_MS * 2);
    assert.deepEqual(checked, [11]);
  } finally {
    mock.timers.reset();
  }
});

test("only a refusal of the version typed is said beside the field", () => {
  assert.ok(isVersionRefusal(400, "That is not a llama.cpp version: use 0.9.0 or a daily build such as b12318."));
  assert.ok(isVersionRefusal(409, "b12318 is already the version in use."));
  // The endpoint's other 409s are not about what was typed.
  assert.ok(!isVersionRefusal(409, "MLX-VLM is already being installed."));
  assert.ok(!isVersionRefusal(409, "FrameFound's installer (uv) is missing on this Mac. Run `framefound install` again."));
  assert.ok(!isVersionRefusal(409, "MLX-VLM runs only on a Mac with Apple silicon."));
  assert.ok(!isVersionRefusal(503, "The work list is not answering."));
});
