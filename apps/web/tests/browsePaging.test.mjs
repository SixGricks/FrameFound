// Run with `npm test`.
//
// Browse's ← and → step from the page the URL is about to show, held to
// the page count only once these filters have been counted. Clamping to a
// count that was not there yet (a reload, a link, a failed load: "5 / 1")
// sent ← from page 5 to page 1.

import assert from "node:assert/strict";
import { test } from "node:test";

import { filtersOf, turnTo } from "../src/app/browse/paging.ts";

test("with no count yet, ← from page 5 goes to page 4, not page 1", () => {
  assert.equal(turnTo("library=a&page=5", -1, null), 4);
});

test("with no count yet, nothing goes below page 1", () => {
  assert.equal(turnTo("library=a", -1, null), 1);
  assert.equal(turnTo("page=2", -1, null), 1);
});

test("a known count still stops → at the last page", () => {
  assert.equal(turnTo("page=3", 1, 3), 3);
  assert.equal(turnTo("page=2", 1, 3), 3);
});

test("from a page past the end, ← lands on the last page there is", () => {
  assert.equal(turnTo("page=9", -1, 3), 3);
  assert.equal(turnTo("page=4", -1, 3), 3);
});

test("two quick presses build on each other through the query the URL is about to have", () => {
  const first = turnTo("sort=name", 1, 5);
  assert.equal(first, 2);
  assert.equal(turnTo(`sort=name&page=${first}`, 1, 5), 3);
});

test("the page is not one of the filters, and the order of the rest is kept", () => {
  assert.equal(filtersOf("library=a&page=4&media=image"), "library=a&media=image");
  assert.equal(filtersOf("page=2"), "");
  assert.notEqual(filtersOf("library=a"), filtersOf("library=b"));
});
