// Run with `npm test` (Node's own test runner; nothing to install).
//
// Approved publishers, the token and Recent activity: what a list change
// sends and asks, what the page says afterwards, that the token's state never
// carries the token, and which work Recent activity offers to cancel. The
// refusals of a bad name are the server's sentences; these pin what the page
// decides and words itself.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  JUMPS,
  activityAt,
  activityState,
  addProblem,
  blockQuestion,
  cancelInActivity,
  dependents,
  listAfter,
  listPatch,
  ownNames,
  publisherName,
  publishersSavedSentence,
  removeListQuestion,
  tokenSavedSentence,
  tokenState,
} from "../src/lib/localModels.ts";

const PUBLISHERS = {
  makers: ["google", "allenai", "nvidia", "apple"],
  converters: ["mlx-community"],
  blocked: [
    { name: "BuiltInLab", built_in: true },
    { name: "someone-else", built_in: false },
  ],
};

const ROLES = [
  { role: "looker", title: "Looker", model_id: "m1" },
  { role: "editor", title: "Editor", model_id: null },
];

function model(over = {}) {
  return {
    id: "m1",
    name: "gemma-4-e4b-it-4bit",
    repo_id: "mlx-community/gemma-4-e4b-it-4bit",
    publisher: "mlx-community",
    maker: "google",
    built_from: [{ repo_id: "google/gemma-4-E4B-it", publisher: "google", publisher_is: "maker", found: true }],
    approved: true,
    roles: ["looker"],
    ...over,
  };
}

const OTHER = model({
  id: "m2",
  name: "Molmo-tiny",
  repo_id: "allenai/Molmo-tiny",
  publisher: "allenai",
  maker: "allenai",
  built_from: [],
  roles: [],
});

test("a pasted address or an @name is read as the publisher's name", () => {
  assert.equal(publisherName("  https://huggingface.co/google/ "), "google");
  assert.equal(publisherName("@mlx-community"), "mlx-community");
  assert.equal(publisherName("hf.co/apple"), "apple");
});

test("adding a name already on the list, in any case, says so instead of sending", () => {
  assert.equal(addProblem(PUBLISHERS, "makers", "Google"), "Google is already a maker.");
  assert.equal(addProblem(PUBLISHERS, "blocked", "builtinlab"), "builtinlab is already blocked.");
  assert.equal(addProblem(PUBLISHERS, "blocked", "someone-else"), "someone-else is already blocked.");
  assert.match(addProblem(PUBLISHERS, "makers", "  "), /Type a publisher's name/);
  assert.equal(addProblem(PUBLISHERS, "makers", "ibm"), null);
});

test("a converter cannot also be added as a maker, and the sentence says which list it is on", () => {
  // The server would answer "mlx-community is already a maker.", which is wrong.
  assert.equal(
    addProblem(PUBLISHERS, "makers", "mlx-community"),
    "mlx-community is one of your converters. Take it off that list first (the × beside it), then add it as a maker.",
  );
  assert.match(addProblem(PUBLISHERS, "blocked", "google"), /one of your makers\..*then block it\.$/);
});

test("a maker's approved models, and those built from theirs, are what depend on it", () => {
  const unapproved = model({ id: "m3", approved: false });
  const models = [model(), OTHER, unapproved];
  assert.deepEqual(dependents("google", models).map((m) => m.id), ["m1"]);
  assert.deepEqual(dependents("MLX-community", models).map((m) => m.id), ["m1"]);
  assert.deepEqual(dependents("allenai", models).map((m) => m.id), ["m2"]);
  assert.deepEqual(dependents("apple", models), []);
});

test("removing a maker nothing depends on asks nothing", () => {
  assert.equal(removeListQuestion("apple", [model(), OTHER], ROLES), null);
  assert.equal(blockQuestion("ibm", [model(), OTHER], ROLES), null);
});

test("removing a maker one model depends on names the model and its role before asking", () => {
  assert.equal(
    removeListQuestion("google", [model(), OTHER], ROLES),
    "gemma-4-e4b-it-4bit is from google or built from theirs. It stays in the library but will " +
      "not run until google is approved again. It is the Looker. Remove google?",
  );
});

test("removing a publisher several models depend on counts them and says the Looker is among them", () => {
  const second = model({ id: "m4", name: "gemma-4-12B-it-OptiQ-4bit", roles: [] });
  const question = removeListQuestion("mlx-community", [model(), second], ROLES);
  assert.equal(
    question,
    "2 installed models are from mlx-community or built from theirs. They stay in the library " +
      "but will not run until mlx-community is approved again. The Looker is one of them. " +
      "Remove mlx-community?",
  );
  assert.match(blockQuestion("google", [model(), second], ROLES), /will not run while google is blocked\..*Block google\?$/);
});

test("the answer names the models a removal left unapproved, from the server's own list", () => {
  const answer = {
    publishers: PUBLISHERS,
    no_longer_approved: [
      { id: "m1", repo_id: "mlx-community/gemma-4-e4b-it-4bit", reason: "…", roles: ["looker"] },
    ],
  };
  assert.equal(
    publishersSavedSentence("removed", "makers", "google", answer),
    "google removed from the makers. gemma-4-e4b-it-4bit is no longer approved. " +
      "It keeps its role, but will not run until it is approved again.",
  );
  const none = { publishers: PUBLISHERS, no_longer_approved: [] };
  assert.equal(publishersSavedSentence("added", "makers", "google", none), "google added to the makers.");
  assert.equal(publishersSavedSentence("added", "blocked", "acme", none), "acme is blocked now.");
  assert.equal(publishersSavedSentence("removed", "blocked", "acme", none), "acme is off the blocked list.");
});

test("a list is sent whole, and the blocked list only with the owner's own additions", () => {
  assert.deepEqual(ownNames(PUBLISHERS, "blocked"), ["someone-else"]);
  assert.deepEqual(ownNames(PUBLISHERS, "makers"), PUBLISHERS.makers);
  assert.deepEqual(listPatch("blocked", ["acme"]), { blocked_added: ["acme"] });
  assert.deepEqual(listPatch("converters", []), { converters: [] });
});

test("a change is built from the lists as they are now, so another window's block is not lifted", () => {
  // This window drew the lists before another one blocked "some-lab".
  const now = {
    ...PUBLISHERS,
    blocked: [...PUBLISHERS.blocked, { name: "some-lab", built_in: false }],
  };
  assert.deepEqual(listAfter(now, "blocked", "added", " nvidia-research "), [
    "someone-else",
    "some-lab",
    "nvidia-research",
  ]);
  // A maker another window already took off is not put back by a removal here.
  const fewer = { ...PUBLISHERS, makers: ["google", "allenai", "apple"] };
  assert.deepEqual(listAfter(fewer, "makers", "removed", "Apple"), ["google", "allenai"]);
});

test("adding a name another window added meanwhile sends it once", () => {
  const now = { ...PUBLISHERS, converters: ["mlx-community", "acme-packaging"] };
  assert.deepEqual(listAfter(now, "converters", "added", "https://huggingface.co/acme-packaging/"), [
    "mlx-community",
    "acme-packaging",
  ]);
});

test("the token's state says whether one is stored, or that it can no longer be read", () => {
  const settings = { hf_token_configured: true, hf_token_problem: null };
  assert.deepEqual(tokenState(settings), { text: "A token is stored.", tone: "ok" });
  assert.deepEqual(tokenState({ hf_token_configured: false, hf_token_problem: null }), {
    text: "None stored.",
    tone: null,
  });
  const unreadable = "The stored token can no longer be read. Paste it again.";
  assert.deepEqual(tokenState({ hf_token_configured: true, hf_token_problem: unreadable }), {
    text: unreadable,
    tone: "bad",
  });
  assert.doesNotMatch(tokenSavedSentence("hf_secretvalue"), /secretvalue/);
  assert.match(tokenSavedSentence(""), /removed/);
});

function job(over = {}) {
  return {
    id: "j1",
    kind: "download",
    title: "Downloading gemma-4-e4b-it-4bit",
    subject: "mlx-community/gemma-4-e4b-it-4bit@0123abcd",
    state: "running",
    message: "",
    phase: "downloading",
    done: 0,
    total: 0,
    cancelling: false,
    can_cancel: true,
    created_at: "2026-11-12T13:00:00Z",
    started_at: "2026-11-12T13:01:00Z",
    finished_at: null,
    ...over,
  };
}

test("Recent activity offers Cancel only for work no other part of the page shows", () => {
  assert.equal(cancelInActivity(job()), false);
  assert.equal(cancelInActivity(job({ kind: "test" })), false);
  assert.equal(cancelInActivity(job({ kind: "compare" })), true);
  assert.equal(cancelInActivity(job({ kind: "compare", state: "done" })), false);
});

test("Recent activity says how a job ended and dates it by when it ended", () => {
  assert.deepEqual(activityState(job({ state: "failed", message: "" })), {
    label: "failed",
    tone: "bad",
    sentence: "It failed.",
  });
  assert.equal(activityState(job({ state: "done", message: "Installed." })).label, "done");
  assert.equal(activityState(job({ cancelling: true })).label, "stopping");
  assert.equal(activityState(job({ state: "queued" })).sentence, "Waiting its turn.");
  assert.equal(activityAt(job({ finished_at: "2026-11-12T14:00:00Z" })), "2026-11-12T14:00:00Z");
  assert.equal(activityAt(job()), "2026-11-12T13:01:00Z");
  assert.equal(activityAt(job({ started_at: null })), "2026-11-12T13:00:00Z");
});

test("the jump bar reaches Recent activity, at the end, and no section twice", () => {
  const ids = JUMPS.map((jump) => jump.id);
  assert.equal(ids.at(-1), "activity");
  assert.equal(new Set(ids).size, ids.length);
  assert.ok(!ids.includes("start"));
});
