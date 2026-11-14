// Run with `npm test` (Node's own test runner; nothing to install).
//
// Final QA of the AI models pages (batch E2): Compare offered a model whose
// program was not installed and its turn stopped at once; the Software
// section told the demo's llama.cpp row that each program has "its own
// Python"; and a model's Details showed "made 1 Oct 2026" beside
// "Nov 13, 2026".

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { INSTALL_PROGRAM, columnLink, fixLink } from "../src/lib/localModelsCompare.ts";
import {
  addedText,
  checkTitle,
  dayText,
  isLlamaCppKind,
  localDayText,
  rolesNote,
  softwareNotes,
} from "../src/lib/localModels.ts";
import { firstTicks, tickBlocker } from "../src/lib/compare.ts";

const MISSING =
  "Demo llama.cpp (not a real program) is not installed. Install it under Software that runs " +
  "models on the AI models page.";

function choice(over = {}) {
  return {
    id: "ee60d4e8-568d-4404-b046-6261838f4bbd",
    name: "demo-vision-gguf Q4_K_M",
    can_compare: true,
    reason: null,
    seconds_hint: null,
    program_missing: false,
    ...over,
  };
}

function runtime(over = {}) {
  return { name: "mlx-vlm", label: "MLX-VLM", demo: false, ...over };
}

test("a model whose program is missing is not ticked, says the server's sentence and links to install it", () => {
  const held = choice({ can_compare: false, reason: MISSING, program_missing: true });
  // Opened from its row's "Compare it first" link, it is not ticked.
  assert.deepEqual(firstTicks([held], 6, held.id), []);
  assert.equal(tickBlocker(held, [], 6), MISSING);
  assert.deepEqual(fixLink(held), INSTALL_PROGRAM);
  assert.equal(INSTALL_PROGRAM.href, "/models#software");
});

test("only a missing program sends the owner to Software that runs models", () => {
  assert.equal(fixLink(choice()), null);
  assert.equal(fixLink(choice({ can_compare: false, reason: "It does not read pictures." })), null);
  // An older server's answer says nothing about the program: no link is guessed.
  const older = choice({ can_compare: false, reason: "It does not read pictures." });
  delete older.program_missing;
  assert.equal(fixLink(older), null);
});

test("the demo's llama.cpp stand-in is read as llama.cpp's kind, not as a Python program", () => {
  const demo = runtime({ name: "demo-gguf", label: "Demo llama.cpp (not a real program)", demo: true });
  assert.equal(isLlamaCppKind(demo), true);
  assert.equal(isLlamaCppKind(runtime({ name: "llama-cpp", label: "llama.cpp" })), true);
  assert.equal(isLlamaCppKind(runtime()), false);
  assert.equal(isLlamaCppKind(runtime({ name: "demo", label: "Demo runtime (not a real model)" })), false);

  const notes = softwareNotes([runtime({ name: "demo", demo: true }), demo]);
  assert.doesNotMatch(notes.join(" "), /its own Python/);
  // It downloads nothing itself: the line says what the real program does.
  assert.equal(
    notes.at(-1),
    "On a real install, llama.cpp is downloaded from its makers' page on GitHub and checked " +
      "against the checksum listed there.",
  );
  assert.equal(checkTitle(demo), "Ask GitHub for the newest numbered Demo llama.cpp (not a real program) now");
  assert.match(rolesNote({ models: [], runtimes: [demo] }), /runs on llama\.cpp is new here/);
});

test("the real llama.cpp row still names itself, and a page of MLX-VLM alone keeps its own wording", () => {
  const notes = softwareNotes([runtime(), runtime({ name: "llama-cpp", label: "llama.cpp" })]);
  assert.equal(
    notes.at(-1),
    "llama.cpp is downloaded from its makers' page on GitHub and checked against the checksum " +
      "listed there.",
  );
  assert.match(softwareNotes([runtime()])[0], /^Each program lives in its own folder with its own Python/);
});

test("Details writes the day a model was added as it writes the day its build was made", () => {
  // Midday on this machine's clock, so the test reads the same in any time zone.
  const added = new Date(2026, 10, 13, 12).toISOString();
  assert.equal(localDayText(added), "13 Nov 2026");
  assert.equal(dayText("2026-10-01T12:00:00Z"), "1 Oct 2026");
  assert.equal(addedText({ added_at: added, added_by: "owner@example.com" }), "13 Nov 2026 by owner@example.com");
  assert.equal(addedText({ added_at: added, added_by: null }), "13 Nov 2026");
  assert.equal(addedText({ added_at: null, added_by: null }), "—");
  assert.equal(localDayText("not a date"), "");
});

test("a comparison column stopped by its program links its name to Software that runs models", () => {
  // The program removed after the comparison started: the column's sentence
  // is step 5's, and its name used to lead to the model's row instead.
  assert.equal(columnLink({ message: MISSING }), "/models#software");
  const recorded =
    "llama.cpp b6890 is recorded but its program is missing. Install b6890 again under Software " +
    "that runs models (Install a particular version): that installs it afresh.";
  assert.equal(columnLink({ message: recorded }), "/models#software");
  assert.equal(
    columnLink({
      message:
        "FrameFound's record of which version is in use is damaged. An administrator can install a version to replace it.",
    }),
    "/models#software",
  );
  // runtimes.describe's sentence reaches the column unchanged (it names no
  // path), and the builder's program_missing counts it too.
  assert.equal(
    columnLink({
      message:
        "The version recorded as in use (b6890) is missing or incomplete on disk. Install it again, or install another version.",
    }),
    "/models#software",
  );
  // Anything else keeps the model's row on AI models.
  assert.equal(columnLink({ message: "Finished." }), "/models#models");
  assert.equal(columnLink({ message: "Waiting its turn." }), "/models#models");
});

test("the program sentences columnLink reads are still the server's", () => {
  const server = (path) => readFileSync(new URL(`../../server/framefound/local_models/${path}`, import.meta.url), "utf8");
  const ask = server("ask.py");
  assert.match(ask, /is not installed\. Install it under Software that runs models on "/);
  assert.match(ask, /record of which version is in use is damaged/);
  assert.match(server("kinds.py"), /again under Software that runs models/);
  assert.match(server("runtimes.py"), /The version recorded as in use \(\{current\}\) is missing or incomplete on disk\. "/);
});
