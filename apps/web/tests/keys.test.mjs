// Run with `npm test` (Node's own test runner; nothing to install).
//
// Tags and People answer a selection with bare Y / N / Esc. These pin which
// key presses those shortcuts must leave alone — the rule that broke when
// selecting on Tags moved to a tick box and every <input> was still skipped.

import assert from "node:assert/strict";
import { test } from "node:test";

import { typingInto } from "../src/lib/keys.ts";

/** As much of an element as a key handler looks at. */
const el = (tagName, more = {}) => ({ tagName, isContentEditable: false, ...more });

test("a tick box is not typing, so Y / N / Esc still answer the selection after a tick", () => {
  assert.equal(typingInto(el("INPUT", { type: "checkbox" })), false);
});

test("the other inputs that are pressed or picked are not typing either", () => {
  for (const type of ["radio", "button", "submit", "reset", "file", "color", "image"]) {
    assert.equal(typingInto(el("INPUT", { type })), false, type);
  }
});

test("a field that takes text is typing, whatever kind of text", () => {
  for (const type of ["text", "search", "email", "number", "password", "date", "TEXT"]) {
    assert.equal(typingInto(el("INPUT", { type })), true, type);
  }
});

test("an input of a type nobody declared is a text field, as the browser treats it", () => {
  assert.equal(typingInto(el("INPUT")), true);
  assert.equal(typingInto(el("INPUT", { type: "made-up" })), true);
});

test("a text area, a drop-down and anything editable in place are typing", () => {
  assert.equal(typingInto(el("TEXTAREA")), true);
  // Letters in a drop-down jump to an option.
  assert.equal(typingInto(el("SELECT")), true);
  assert.equal(typingInto(el("DIV", { isContentEditable: true })), true);
});

test("a slider keeps its keys", () => {
  assert.equal(typingInto(el("INPUT", { type: "range" })), true);
});

test("a button, a link, a heading and the page itself are not typing", () => {
  for (const tag of ["BUTTON", "A", "H2", "BODY", "VIDEO"]) {
    assert.equal(typingInto(el(tag)), false, tag);
  }
});

test("a key event with no element behind it is not typing", () => {
  assert.equal(typingInto(null), false);
  // The window or the document, which have no tag name.
  assert.equal(typingInto({}), false);
});
