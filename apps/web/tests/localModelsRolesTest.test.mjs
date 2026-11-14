// Run with `npm test` (Node's own test runner; nothing to install).
//
// Who does what and Test: which models a role can be given to and why the
// others cannot, what the page says around a copy to this Mac, when Test can
// be pressed, and how a finished test's answer and figures are read. The
// refusals are the server's sentences; these pin what the page words itself.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  assignBlocker,
  isRoleName,
  orderText,
  randomPage,
  roleGivenSentence,
  roleOptions,
  roleQuestion,
  roleTakeQuestion,
  rollbackOffer,
  secondsText,
  shortReason,
  testAnswer,
  testBlocker,
  testFacts,
  testOpenByDefault,
  testPictureCaption,
  testSentence,
  TEST_SHOWN_MS,
} from "../src/lib/localModels.ts";

const SETTINGS = {
  day_budget_gb: 8,
  night_budget_gb: 20,
  night_start: "21:30",
  night_end: "05:30",
  paused: false,
};

const RUNTIMES = [{ name: "mlx-vlm", label: "MLX-VLM", installed: "0.7.12", previous: "0.7.11" }];

function model(over = {}) {
  return {
    id: "m1",
    name: "gemma-4-e4b-it-4bit",
    revision_short: "475b908",
    url: "https://huggingface.co/mlx-community/gemma-4-e4b-it-4bit/tree/475b908",
    approved: true,
    approval_problem: null,
    fits: "day",
    takes: ["image", "text"],
    runtime: "mlx-vlm",
    runtime_label: "MLX-VLM",
    runtime_known: true,
    runtime_installed: true,
    state: "installed",
    on_this_machine: false,
    roles: [],
    pending_roles: [],
    role_problems: { looker: null, editor: null },
    last_loaded_with: null,
    job: null,
    test: null,
    ...over,
  };
}

function role(over = {}) {
  return {
    role: "looker",
    title: "Looker",
    model_id: null,
    model_name: null,
    ready: false,
    problem: null,
    pending_model_id: null,
    pending_model_name: null,
    job: null,
    ...over,
  };
}

function library(over = {}) {
  return { connected: true, problem: null, copies_to_this_machine: true, ...over };
}

function job(over = {}) {
  return { id: "j1", state: "running", message: "", cancelling: false, total: 0, done: 0, ...over };
}

function finished(over = {}) {
  return {
    job_id: "t1",
    state: "done",
    message: "",
    detail: null,
    asked_at: "2026-11-12T14:00:00Z",
    finished_at: "2026-11-12T14:00:20Z",
    kind: "picture",
    sample: { asset_id: "a1", media_type: "image", filename: "DJI_0639.JPG", label: "The newest photograph in the catalogue" },
    answer: { caption: "A red barn on a green lawn.", tags: ["barn", "lawn"] },
    raw: '{"caption": "A red barn on a green lawn."}',
    structured_ok: true,
    seconds: 4.2,
    load_seconds: 14,
    memory_gb: 6.1,
    memory_source: "runtime",
    ran_from: "this_machine",
    model: { repo_id: "mlx-community/gemma-4-e4b-it-4bit", revision_short: "475b908" },
    runtime: "mlx-vlm",
    runtime_version: "0.7.12",
    demo: false,
    ...over,
  };
}

test("only the Looker and the Editor can be given from this page", () => {
  assert.ok(isRoleName("looker"));
  assert.ok(isRoleName("editor"));
  assert.ok(!isRoleName("narrator"));
});

test("a text-only model stays in the Looker's list, disabled, with the server's reason", () => {
  const reader = model({ id: "m2", name: "gemma-4-12B-it-OptiQ-4bit", role_problems: { looker: "It does not read pictures.", editor: null } });
  const options = roleOptions([model(), reader], role());
  assert.deepEqual(options[1], { id: "m2", label: "gemma-4-12B-it-OptiQ-4bit — does not read pictures", disabled: true });
  assert.equal(options[0].disabled, false);
  assert.equal(shortReason("It is no longer approved: google is not on your makers list."), "is no longer approved: google is not on your makers list");
});

test("the model that has the role and the one being copied for it cannot be chosen again", () => {
  const options = roleOptions(
    [model({ id: "old" }), model({ id: "new", name: "other" })],
    role({ model_id: "old", pending_model_id: "new" }),
  );
  assert.equal(options[0].label, "gemma-4-e4b-it-4bit — has this role");
  assert.equal(options[1].label, "other — being copied to this Mac for it");
  assert.ok(options.every((option) => option.disabled));
});

test("two builds of one model are told apart by their build", () => {
  const options = roleOptions([model(), model({ id: "m2", revision_short: "3f1c2aa" })], role());
  assert.deepEqual(
    options.map((option) => option.label),
    ["gemma-4-e4b-it-4bit (build 475b908)", "gemma-4-e4b-it-4bit (build 3f1c2aa)"],
  );
});

test("Assign is held while another copy is under way, and while the library it needs is away", () => {
  const copying = role({ pending_model_id: "m9", job: job() });
  assert.equal(assignBlocker(copying, model(), library()), "That role is already being given to another model. Cancel that first.");
  assert.equal(
    assignBlocker(role({ pending_model_id: "m1", job: job({ state: "queued" }) }), model(), library()),
    "It is already being copied to this machine for that role.",
  );
  // A pending id left with no job under way does not hold anything.
  assert.equal(assignBlocker(role({ pending_model_id: "m9" }), model(), library()), null);

  const away = library({ connected: false, problem: "The model library at /Volumes/Photos is not connected." });
  assert.equal(assignBlocker(role(), model(), away), "The model library at /Volumes/Photos is not connected.");
  // Already on this Mac: it can be given the role while the library is away.
  assert.equal(assignBlocker(role(), model({ on_this_machine: true }), away), null);
});

test("replacing the Looker asks, naming both models; giving an empty role does not", () => {
  const next = model({ id: "m2", name: "gemma-4-12B-it" });
  assert.equal(
    roleQuestion(role({ model_id: "m1", model_name: "gemma-4-e4b-it-4bit" }), next),
    "Make gemma-4-12B-it the Looker instead of gemma-4-e4b-it-4bit?",
  );
  assert.equal(roleQuestion(role(), next), null);
});

test("a copy to this Mac says the old model keeps the role until it is whole", () => {
  assert.equal(
    roleGivenSentence(role({ model_id: "m1", model_name: "old-one", pending_model_id: "m2" }), "new-one"),
    "new-one is being copied to this Mac. old-one is still the Looker until it finishes.",
  );
  assert.equal(
    roleGivenSentence(role({ pending_model_id: "m2" }), "new-one"),
    "new-one is being copied to this Mac. It becomes the Looker when the copy is whole.",
  );
  assert.equal(roleGivenSentence(role({ model_id: "m2", model_name: "new-one" }), "new-one"), "new-one is now the Looker.");
  // Copy again: the same model, its copy missing.
  assert.equal(roleGivenSentence(role({ model_id: "m2", pending_model_id: "m2" }), "new-one"), "Copying new-one to this Mac again.");
});

test("taking the role away says what happens to the copy and the model", () => {
  const held = role({ model_id: "m1", model_name: "gemma-4-e4b-it-4bit" });
  assert.equal(
    roleTakeQuestion(held, library()),
    "Take the Looker role away from gemma-4-e4b-it-4bit? No model will be the Looker. Its copy on this Mac is removed; the model stays in the library.",
  );
  assert.match(roleTakeQuestion(held, library({ copies_to_this_machine: false })), /The model stays installed\.$/);
  assert.match(roleTakeQuestion(role({ pending_model_name: "new-one" }), library()), /^Stop making new-one the Looker\?/);
});

test("Test is refused in the server's words, but an overnight-only model may be tested", () => {
  assert.equal(testBlocker(model(), SETTINGS), null);
  assert.equal(testBlocker(model({ fits: "night" }), SETTINGS), null);
  assert.equal(testBlocker(model({ state: "downloading" }), SETTINGS), "It is still downloading.");
  assert.equal(testBlocker(model({ runtime_installed: false }), SETTINGS), "Install MLX-VLM first (Software that runs models, below).");
  assert.equal(testBlocker(model({ runtime_known: false }), SETTINGS), "It needs a program this install does not have.");
  assert.equal(
    testBlocker(model({ approved: false, approval_problem: "google is not on your makers list." }), SETTINGS),
    "It is no longer approved: google is not on your makers list.",
  );
  assert.match(testBlocker(model({ fits: "never" }), SETTINGS), /overnight budget \(20 GB\)/);
  assert.equal(testBlocker(model({ test: finished({ state: "waiting" }) }), SETTINGS), "A test of this model is already waiting.");
  assert.equal(testBlocker(model({ test: finished({ state: "running" }) }), SETTINGS), "It is being tested right now.");
  assert.equal(testBlocker(model({ test: finished() }), SETTINGS), null);
});

test("a waiting test says the server's sentence; a running one says what takes the time", () => {
  const idle = { state: "idle" };
  const window = "Waiting for the overnight window (starts 21:30): it needs about 10 GB and the daytime budget is 8 GB.";
  assert.equal(testSentence(job({ state: "waiting", message: window }), model(), idle), window);
  assert.equal(testSentence(job({ state: "queued" }), model(), idle, "Downloading gemma"), "Waiting its turn — Downloading gemma.");
  assert.equal(testSentence(job(), model(), idle), "Loading it from the library; this is slower than a model that has a role.");
  assert.equal(testSentence(job(), model({ roles: ["looker"] }), idle), "Starting the model — the first answer after a load takes longest.");
  assert.equal(testSentence(job(), model(), { state: "busy", model_id: "m1" }), "Asking it — the model is already loaded.");
  assert.equal(testSentence(job({ cancelling: true }), model(), idle), "Stopping…");
});

test("a test's panel opens by itself while it runs and for a while after, not for ever", () => {
  const now = Date.parse("2026-11-12T14:05:00Z");
  assert.ok(testOpenByDefault(finished({ state: "queued", finished_at: null }), now));
  assert.ok(testOpenByDefault(finished(), now));
  assert.ok(!testOpenByDefault(finished(), Date.parse("2026-11-12T14:00:20Z") + TEST_SHOWN_MS + 1));
  assert.ok(!testOpenByDefault(null, now));
});

test("a picture answer gives its caption and tags; an order answer its order and reason", () => {
  assert.deepEqual(testAnswer(finished()), { form: "look", caption: "A red barn on a green lawn.", tags: ["barn", "lawn"] });
  const text = finished({ kind: "text", sample: null, answer: { order: [2, 1, 3], reason: "Wide first." } });
  assert.deepEqual(testAnswer(text), { form: "order", order: [2, 1, 3], reason: "Wide first." });
  assert.equal(orderText([2, 1, 3]), "2, then 1, then 3");
  // Whatever a model put in the fields, only text and numbers are shown.
  assert.deepEqual(testAnswer(finished({ answer: { caption: 7, tags: ["ok", 3, "", null] } })).tags, ["ok"]);
  assert.equal(testAnswer(finished({ state: "running" })), null);
});

test("a prose answer is kept as the model's own text", () => {
  const prose = finished({ structured_ok: false, answer: null, raw: "<b>Sure!</b> It is a barn." });
  assert.deepEqual(testAnswer(prose), { form: "prose", raw: "<b>Sure!</b> It is a barn." });
});

test("the picture a test used is named, and the drawn one says it was drawn", () => {
  assert.equal(testPictureCaption(finished()), "The newest photograph in the catalogue: DJI_0639.JPG");
  assert.equal(
    testPictureCaption(finished({ sample: { asset_id: null, media_type: "image", filename: null, label: "A picture FrameFound drew" } })),
    "A picture FrameFound drew for the test",
  );
  assert.equal(testPictureCaption(finished({ kind: "text" })), null);
});

test("a finished test lists both times, memory, build, software and where it ran from", () => {
  const facts = Object.fromEntries(testFacts(finished(), RUNTIMES, Date.parse("2026-11-12T15:00:00Z")));
  assert.equal(facts["Time to answer"], "4.2 s");
  assert.equal(facts["Time to load"], "14 s");
  assert.equal(facts.Memory, "6.1 GB (as the software reports it)");
  assert.equal(facts.Build, "475b908");
  assert.equal(facts.Software, "MLX-VLM 0.7.12");
  assert.equal(facts["Ran from"], "this Mac");
  assert.ok(facts.When);
  const warm = Object.fromEntries(testFacts(finished({ load_seconds: 0, memory_source: "process", ran_from: "library" }), RUNTIMES));
  assert.equal(warm["Time to load"], "none — it was already loaded");
  assert.equal(warm.Memory, "6.1 GB (the size of its process)");
  assert.equal(warm["Ran from"], "the model library");
  assert.deepEqual(testFacts(finished({ seconds: null, load_seconds: null, memory_gb: null, model: null, runtime: null, ran_from: null, finished_at: null, asked_at: null }), RUNTIMES), []);
  assert.equal(secondsText(125), "2 min 5 s");
});

test("a model that last loaded under the kept older version is offered a roll back", () => {
  const offer = rollbackOffer(model({ last_loaded_with: "mlx-vlm/0.7.11" }), RUNTIMES);
  assert.equal(offer.sentence, "It last loaded under MLX-VLM 0.7.11.");
  assert.equal(offer.version, "0.7.11");
  // Said, but not offered, when that version is not the one kept.
  assert.equal(rollbackOffer(model({ last_loaded_with: "mlx-vlm/0.7.9" }), RUNTIMES).version, null);
  assert.equal(rollbackOffer(model({ last_loaded_with: "mlx-vlm/0.7.12" }), RUNTIMES), null);
  assert.equal(rollbackOffer(model(), RUNTIMES), null);
});

test("another picture is any page of the photographs, and none when there are none", () => {
  assert.equal(randomPage(0), null);
  assert.equal(randomPage(40, () => 0), 1);
  assert.equal(randomPage(40, () => 0.999999), 40);
  assert.equal(randomPage(40, () => 0.5), 21);
});
