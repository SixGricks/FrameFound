// Run with `npm test` (Node's own test runner; nothing to install).
//
// "Make it the Looker", pressed under a comparison's decision
// (src/components/CompareVerdict.tsx). The button goes through AI models' own
// role route and says what that route answered with AI models' own sentence
// (`roleGivenSentence`). These tests pin what the component hands that
// sentence: Compare's names for the models, which already carry the build, and
// no build beside them. The component cannot be loaded here (it is JSX), so
// the shapes below are written out as it builds them.

import assert from "node:assert/strict";
import { test } from "node:test";

import { lookerQuestion } from "../src/lib/compare.ts";
import { roleGivenSentence } from "../src/lib/localModels.ts";

// A model as the component hands it on: the id, the name as a column or the
// Compare overview has it ("<repository> <build>"), and no build beside it.
const asNamed = (id, name) => ({ id, name, variant: "", variant_label: "" });

const Q4 = asNamed("q4", "demo-vision-gguf Q4_K_M");
const Q8 = asNamed("q8", "demo-vision-gguf Q8_0");

// The role route's answer while the new model is copied to this Mac first.
// It names both models after their repository alone.
const copying = {
  role: "looker",
  title: "Looker",
  model_id: "q4",
  model_name: "demo-vision-gguf",
  pending_model_id: "q8",
  pending_model_name: "demo-vision-gguf",
};

test("the old Looker is named with its build while another build of the same repository is copied", () => {
  const said = roleGivenSentence(copying, Q8.name, [Q4, Q8]);
  assert.equal(
    said,
    "demo-vision-gguf Q8_0 is being copied to this Mac. demo-vision-gguf Q4_K_M is still the Looker " +
      "until it finishes.",
  );
  // The same two names the question a moment earlier asked about.
  assert.equal(
    lookerQuestion(Q8.name, Q4.name),
    "Make demo-vision-gguf Q8_0 the Looker instead of demo-vision-gguf Q4_K_M?",
  );
  // What it said before the names were handed on: either build could be meant.
  assert.match(roleGivenSentence(copying, Q8.name), /\. demo-vision-gguf is still the Looker/);
});

test("a Compare name handed on with its build beside it would say the build twice", () => {
  // Why the component blanks the build: Compare's name is not the bare
  // repository name the AI models page starts from.
  const twice = { id: "q4", name: Q4.name, variant: "Q4_K_M", variant_label: "Q4_K_M" };
  assert.match(roleGivenSentence(copying, Q8.name, [twice]), /demo-vision-gguf Q4_K_M \(Q4_K_M\) is still/);
  assert.doesNotMatch(roleGivenSentence(copying, Q8.name, [Q4]), /\(/);
});

test("a Looker the page has no name for is named as the role route named it", () => {
  // The overview could not be read and the old Looker is not a column here.
  assert.equal(
    roleGivenSentence(copying, Q8.name, [Q8]),
    "demo-vision-gguf Q8_0 is being copied to this Mac. demo-vision-gguf is still the Looker until it finishes.",
  );
});

test("where the overview and a column both name a model, the overview read just now is believed", () => {
  // The component lists the overview's models first, then its own columns.
  const fromOverview = asNamed("q4", "as the overview has it");
  const fromColumn = asNamed("q4", "as the column was named");
  assert.match(
    roleGivenSentence(copying, Q8.name, [fromOverview, fromColumn]),
    /\. as the overview has it is still the Looker/,
  );
});

test("a role given at once, and a copy asked for again, name the model as its column does", () => {
  const given = { ...copying, model_id: "q8", pending_model_id: null, pending_model_name: null };
  assert.equal(roleGivenSentence(given, Q8.name, [Q4, Q8]), "demo-vision-gguf Q8_0 is now the Looker.");
  const again = { ...copying, model_id: "q8", pending_model_id: "q8" };
  assert.equal(roleGivenSentence(again, Q8.name, [Q4, Q8]), "Copying demo-vision-gguf Q8_0 to this Mac again.");
});
