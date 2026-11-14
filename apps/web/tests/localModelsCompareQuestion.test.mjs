// Run with `npm test` (Node's own test runner; nothing to install).
//
// L6 W3: the Compare question when a role is given. A model on llama.cpp
// that nobody has seen answer on this footage is asked about once (the
// server's 428); a yes is the same request with `skip_compare`, a no changes
// nothing and the card offers Compare instead. Also the words around it:
// the option and pill "not compared yet", the way out for a model that reads
// no pictures (Compare cannot look at it; its Test can clear it), the line
// under the role cards, and Test's empty panel.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DECLINED,
  compareHref,
  firstUsable,
  givenUncompared,
  notCompared,
  notComparedTitle,
  notTestedSentence,
  roleGivenSentence,
  roleOptions,
  roleQuestion,
  roleTakeQuestion,
  rolesNote,
  rowName,
  sendAsking,
  wayOut,
  wayOutForViewer,
} from "../src/lib/localModels.ts";

// What api.ts's `request` throws: an Error carrying the HTTP status.
class Refused extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

const QUESTION =
  "demo-vision-gguf runs on llama.cpp and has not been through Compare on your footage, so " +
  "nobody has seen how well it looks at your pictures. Make it the Looker anyway?";

function gguf(over = {}) {
  return {
    id: "q4",
    name: "demo-vision-gguf",
    revision_short: "7777777",
    variant: "Q4_K_M",
    variant_label: "Q4_K_M",
    takes: ["image", "text"],
    needs_compare: true,
    compared: false,
    role_problems: { looker: null, editor: null },
    ...over,
  };
}

const mlx = (over = {}) =>
  gguf({
    id: "e4b",
    name: "gemma-4-e4b-it-4bit",
    revision_short: "475b908",
    variant: "",
    variant_label: "",
    needs_compare: false,
    compared: true,
    ...over,
  });

function role(over = {}) {
  return {
    role: "looker",
    title: "Looker",
    model_id: null,
    model_name: null,
    pending_model_id: null,
    pending_model_name: null,
    job: null,
    compared: null,
    ...over,
  };
}

// ---- the 428 and the yes ---------------------------------------------------------

test("a 428 is put as the server's question and a yes sends the same request with the yes", async () => {
  const sent = [];
  const asked = [];
  const result = await sendAsking(
    async (yes) => {
      sent.push(yes);
      if (!yes) throw new Refused(428, QUESTION);
      return { role: "looker", model_id: "q4" };
    },
    (question) => {
      asked.push(question);
      return true;
    },
  );
  assert.deepEqual(sent, [false, true]);
  assert.deepEqual(asked, [QUESTION]);
  assert.deepEqual(result, { role: "looker", model_id: "q4" });
});

test("a no sends nothing more and says it was declined, not that it failed", async () => {
  const sent = [];
  const result = await sendAsking(
    async (yes) => {
      sent.push(yes);
      throw new Refused(428, QUESTION);
    },
    () => false,
  );
  assert.equal(result, DECLINED);
  assert.deepEqual(sent, [false]);
});

test("a refusal is not a question, and a second 428 is not asked again", async () => {
  let asked = 0;
  const ask = () => {
    asked += 1;
    return true;
  };
  await assert.rejects(
    sendAsking(async () => {
      throw new Refused(409, "That role is already being given to another model.");
    }, ask),
    { status: 409, message: "That role is already being given to another model." },
  );
  assert.equal(asked, 0);
  await assert.rejects(
    sendAsking(async () => {
      throw new Refused(428, QUESTION);
    }, ask),
    { status: 428 },
  );
  assert.equal(asked, 1);
});

test("a question answered at once (no 428) is never put", async () => {
  const result = await sendAsking(
    async (yes) => ({ yes }),
    () => assert.fail("nothing to ask"),
  );
  assert.deepEqual(result, { yes: false });
});

// ---- not compared yet ----------------------------------------------------------------

test("only software that asks, with an explicit false, is 'not compared yet'", () => {
  assert.equal(notCompared(gguf()), true);
  assert.equal(notCompared(gguf({ compared: true })), false);
  assert.equal(notCompared(mlx({ compared: false })), false);
  // An answer that does not judge it (an older server) says nothing.
  assert.equal(notCompared(gguf({ compared: undefined })), false);
});

test("the option names the build and says not compared yet, and stays choosable", () => {
  const [option, plain] = roleOptions([gguf(), mlx()], role());
  assert.equal(option.label, "demo-vision-gguf (Q4_K_M) — not compared yet");
  assert.equal(option.disabled, false);
  assert.equal(plain.label, "gemma-4-e4b-it-4bit");
  // A reason it cannot take the role says that instead.
  const held = roleOptions([gguf()], role({ model_id: "q4" }));
  assert.equal(held[0].label, "demo-vision-gguf (Q4_K_M) — has this role");
  assert.equal(held[0].disabled, true);
  const compared = roleOptions([gguf({ compared: true })], role());
  assert.equal(compared[0].label, "demo-vision-gguf (Q4_K_M)");
});

test("a picture model's way out is Compare with it ticked; a text-only one's is its Test", () => {
  assert.equal(compareHref("a b/c"), "/models/compare?model=a%20b%2Fc");
  assert.deepEqual(wayOut(gguf(), true), { label: "Compare it first", href: "/models/compare?model=q4" });
  assert.deepEqual(wayOut(gguf()), { label: "Compare it", href: "/models/compare?model=q4" });
  const words = gguf({ takes: ["text"] });
  assert.deepEqual(wayOut(words, true), { label: "Test it first", href: "#models" });
  assert.equal(notComparedTitle(gguf()), "It has not been through Compare on your footage.");
  assert.match(notComparedTitle(words), /one question in words/);
  assert.equal(givenUncompared(gguf()), "It was given this role without being compared on your footage.");
});

test("a text-only model is said to lack an answer in the form asked, never to have no answer", () => {
  // The server clears it only for a Test in words answered in the form asked
  // (gate.py); one answered in free text shows under the row as its last test.
  const untested = gguf({ takes: ["text"], test: null });
  assert.equal(
    notComparedTitle(untested),
    "It has not yet answered a Test in the form FrameFound asks for. Test asks it one question in " +
      "words; an answer in the form FrameFound asks for clears this.",
  );
  assert.equal(
    givenUncompared(untested),
    "It was given this role before it answered a Test in the form FrameFound asks for.",
  );
  const freeText = gguf({
    takes: ["text"],
    test: { state: "done", kind: "text", structured_ok: false, raw: "Sure! Here is a cut." },
  });
  assert.match(notComparedTitle(freeText), /^Its last Test answered, but not in the form FrameFound asks for/);
  assert.match(givenUncompared(freeText), /its last answer was not in that form\.$/);
  for (const words of [untested, freeText]) {
    assert.doesNotMatch(`${notComparedTitle(words)} ${givenUncompared(words)}`, /[Nn]obody|anybody saw/);
  }
  // A Test still running, or one that failed, is no answer at all.
  const running = gguf({ takes: ["text"], test: { state: "running", kind: "text", structured_ok: false } });
  assert.equal(notComparedTitle(running), notComparedTitle(untested));
});

test("someone who cannot Compare or Test is told who can, with no link to follow", () => {
  assert.equal(wayOutForViewer(gguf()), "An administrator can compare it on your footage.");
  assert.equal(wayOutForViewer(gguf({ takes: ["text"] })), "An administrator can test it.");
});

test("the sentence after a role is given names the build", () => {
  const answer = { title: "Looker", model_id: "q4", model_name: "demo-vision-gguf", pending_model_id: null };
  assert.equal(roleGivenSentence(answer, rowName(gguf())), "demo-vision-gguf (Q4_K_M) is now the Looker.");
});

test("switching between two builds of one repository names both builds", () => {
  const q8 = gguf({ id: "q8", variant: "Q8_0", variant_label: "Q8_0" });
  const models = [gguf(), q8];
  // The server names the holder after its repository alone.
  const copying = {
    title: "Looker",
    model_id: "q4",
    model_name: "demo-vision-gguf",
    pending_model_id: "q8",
    pending_model_name: "demo-vision-gguf",
  };
  assert.equal(
    roleGivenSentence(copying, rowName(q8), models),
    "demo-vision-gguf (Q8_0) is being copied to this Mac. demo-vision-gguf (Q4_K_M) is still the " +
      "Looker until it finishes.",
  );
  // Without the list (another page's caller) it says what the server said.
  assert.match(roleGivenSentence(copying, rowName(q8)), /\. demo-vision-gguf is still the Looker/);
  const held = role({ model_id: "q4", model_name: "demo-vision-gguf" });
  assert.equal(
    roleQuestion(held, q8, models),
    "Make demo-vision-gguf (Q8_0) the Looker instead of demo-vision-gguf (Q4_K_M)?",
  );
  assert.match(
    roleTakeQuestion(held, { copies_to_this_machine: true }, models),
    /^Take the Looker role away from demo-vision-gguf \(Q4_K_M\)\?/,
  );
  const onlyPending = role({ pending_model_id: "q8", pending_model_name: "demo-vision-gguf" });
  assert.match(
    roleTakeQuestion(onlyPending, { copies_to_this_machine: true }, models),
    /^Stop making demo-vision-gguf \(Q8_0\) the Looker\?/,
  );
});

// ---- where the keyboard goes after a press --------------------------------------------

test("focus goes to the first control that can be pressed, never to a disabled one", () => {
  const assign = { name: "assign", disabled: true };
  const copy = { name: "copy", disabled: false };
  const list = { name: "list", disabled: false };
  // A refused Assign whose choice is now disabled, or Copy again with
  // nothing chosen: Assign cannot take focus, so the list does.
  assert.equal(firstUsable(assign, list), list);
  assert.equal(firstUsable(copy, list), copy);
  // Copy again gone (the copy started after all): the list.
  assert.equal(firstUsable(null, list), list);
  // Nothing usable at all: the caller falls back to the card.
  assert.equal(firstUsable(assign, { disabled: true }, undefined), null);
  // A card has no disabled state and can always be given focus.
  const card = { tagName: "DIV" };
  assert.equal(firstUsable(card), card);
});

// ---- the line under the role cards, and Test's empty panel -----------------------------

test("the line under the roles says what each role does now, and asks once only where llama.cpp is", () => {
  const plain = rolesNote({ models: [mlx()], runtimes: [{ name: "mlx-vlm" }] });
  assert.doesNotMatch(plain, /later updates|only decides which model is kept ready/);
  // Folder reviews can be started from a page now: the line says where,
  // and no longer that they are on their way.
  assert.match(plain, /The Looker looks through a folder when you ask it to \(Review with AI,/);
  assert.match(plain, /at new footage in the libraries you switch on/);
  assert.doesNotMatch(plain, /on its way/);
  assert.match(plain, /suggests colour looks/);
  // A cut is drafted on the Cuts page as well as in the chat panel: the line
  // names both, and no longer the chat panel alone.
  assert.match(plain, /The Editor drafts a rough cut when you ask for one: Draft a cut on the Cuts page, in Search or Browse, or ask in the chat panel\./);
  assert.doesNotMatch(plain, /when you ask for one in the chat panel/);
  assert.doesNotMatch(plain, /llama\.cpp/);
  const asks = "FrameFound asks once before giving it a role it has not been compared for.";
  assert.ok(rolesNote({ models: [], runtimes: [{ name: "mlx-vlm" }, { name: "llama-cpp" }] }).includes(asks));
  assert.ok(rolesNote({ models: [gguf()], runtimes: [{ name: "demo-gguf" }] }).includes(asks));
});

test("Test's empty panel promises a picture only to a model that reads them", () => {
  assert.match(notTestedSentence({ takes: ["image", "text"] }), /one question about a picture/);
  assert.equal(
    notTestedSentence({ takes: ["text"] }),
    "Not tested yet. Test asks it one question in words; nothing it answers is saved.",
  );
});
