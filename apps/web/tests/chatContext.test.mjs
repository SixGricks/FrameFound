// Run with `npm test` (Node's own test runner; nothing to install).
//
// "Tag these as silo" only works if the message says what "these" are. The
// chat panel sends, with each message, what is on screen: read from the
// address bar, plus what the page published. These pin what is sent, and
// that one page's selection never rides along with a message written on
// another.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_CONTEXT_CHARS,
  MAX_SELECTION_SENT,
  contextFromUrl,
  getChatContext,
  knowLibraries,
  pageContext,
  setChatContext,
  subscribeChatContext,
  tidy,
  withLibraryName,
} from "../src/lib/chatContext.ts";

test("the address alone says which clip, listing, person, search or tag is on screen", () => {
  assert.deepEqual(contextFromUrl("/assets/1704c870", ""), { asset_id: "1704c870" });
  assert.deepEqual(contextFromUrl("/assets/1704c870", "?t=12.5"), {
    asset_id: "1704c870",
    at_s: 12.5,
  });
  assert.deepEqual(contextFromUrl("/edit/abc", "?listing=L1"), { asset_id: "abc", listing_id: "L1" });
  assert.deepEqual(contextFromUrl("/listings/L1", ""), { listing_id: "L1" });
  assert.deepEqual(contextFromUrl("/listings", "?folder=2026%2FLedgerock&library=lib1"), {
    folder: "2026/Ledgerock",
    library_id: "lib1",
  });
  assert.deepEqual(contextFromUrl("/people/P1", ""), { person_id: "P1" });
  assert.deepEqual(contextFromUrl("/", "?q=red+barn&library=lib1&type=video"), {
    query: "red barn",
    library_id: "lib1",
  });
  assert.deepEqual(contextFromUrl("/browse", "?tag=silo&page=3"), { tag: "silo" });
});

test("an address that says nothing gives nothing, and a bad one does not throw", () => {
  assert.deepEqual(contextFromUrl("/tags", ""), {});
  assert.deepEqual(contextFromUrl("/", "?q=+"), {});
  assert.deepEqual(contextFromUrl("/assets/", ""), {});
  assert.deepEqual(contextFromUrl("/assets/abc", "?t=soon"), { asset_id: "abc" });
  // Not valid percent-encoding: kept as written rather than lost.
  assert.deepEqual(contextFromUrl("/assets/100%", ""), { asset_id: "100%" });
  // Search keeps its `q` to itself: another page's `q` is not a search.
  assert.deepEqual(contextFromUrl("/storage", "?q=barn"), {});
});

test("a message always says which page, and its name when the page has one", () => {
  assert.deepEqual(pageContext({ pathname: "/tags", search: "", title: "Tags · FrameFound" }, null), {
    title: "Tags",
    path: "/tags",
  });
  assert.deepEqual(pageContext({ pathname: "/storage", title: "FrameFound" }, null), {
    path: "/storage",
  });
  assert.deepEqual(pageContext({ pathname: "/health" }, null), { path: "/health" });
});

test("what the page published is added to what the address says, and wins over it", () => {
  const set = {
    path: "/assets/abc",
    context: { filename: "DJI_0042.MP4", at_s: 31, selection: ["a", "b"] },
  };

  assert.deepEqual(pageContext({ pathname: "/assets/abc", search: "?t=12", title: "DJI_0042.MP4 · FrameFound" }, set), {
    title: "DJI_0042.MP4",
    asset_id: "abc",
    at_s: 31,
    filename: "DJI_0042.MP4",
    path: "/assets/abc",
    selection: ["a", "b"],
    selection_count: 2,
  });
});

test("a selection published on one page is not sent with a message written on another", () => {
  // The panel outlives every page. A page that failed to take its context
  // back must not make Claude act on tiles nobody can see any more.
  const set = { path: "/tags", context: { tag: "silo", selection: ["a", "b", "c"] } };

  assert.deepEqual(pageContext({ pathname: "/browse", search: "?tag=barn" }, set), {
    tag: "barn",
    path: "/browse",
  });
});

test("a page cannot claim to be another page", () => {
  const set = { path: "/tags", context: { path: "/security", tag: "silo" } };

  assert.equal(pageContext({ pathname: "/tags" }, set).path, "/tags");
});

test("publishing replaces, withdrawing is by the page that published, and listeners hear both", (t) => {
  t.after(() => setChatContext(null));
  let heard = 0;
  const stop = subscribeChatContext(() => {
    heard += 1;
  });

  const leaveTags = setChatContext({ path: "/tags", tag: "silo" });
  assert.deepEqual(getChatContext(), { path: "/tags", tag: "silo" });
  assert.deepEqual(pageContext({ pathname: "/tags" }), { tag: "silo", path: "/tags" });

  // The next page arrives before the last one has cleaned up.
  const leaveBrowse = setChatContext({ path: "/browse", tag: "barn" });
  leaveTags();
  assert.deepEqual(getChatContext(), { path: "/browse", tag: "barn" }, "a late cleanup wipes nothing");

  leaveBrowse();
  assert.equal(getChatContext(), null);
  leaveBrowse();
  assert.equal(heard, 3, "set, set, withdrawn; the two cleanups that did nothing said nothing");

  stop();
  setChatContext(null);
  assert.equal(heard, 3);
});

test("what is sent is cut to what the server keeps, and empty values are left out", () => {
  const long = "x".repeat(MAX_CONTEXT_CHARS + 50);

  const sent = tidy({
    folder: long,
    tag: "  silo  ",
    query: "   ",
    filename: undefined,
    library: null,
    at_s: Number.NaN,
    listing_id: 7,
  });

  assert.deepEqual(sent, { folder: "x".repeat(MAX_CONTEXT_CHARS), tag: "silo", listing_id: 7 });
});

test("a large selection is capped, and the count is of everything selected", () => {
  const ids = Array.from({ length: MAX_SELECTION_SENT + 25 }, (_, n) => `id-${n}`);

  const sent = tidy({ selection: ["", ...ids, 7, null] });

  assert.equal(sent.selection.length, MAX_SELECTION_SENT);
  assert.equal(sent.selection[0], "id-0");
  assert.equal(sent.selection_count, MAX_SELECTION_SENT + 25);
  // Nothing selected is not "a selection of none".
  assert.deepEqual(tidy({ selection: [], selection_count: 4 }), {});
});

test("a key the page left empty does not wipe what the address says", () => {
  // Search publishes the library's name and id as soon as it renders; before
  // the list of libraries has loaded both are still unknown.
  const published = {
    path: "/",
    context: { library_id: undefined, library: "", query: null, selection: ["a1"] },
  };

  const sent = pageContext({ pathname: "/", search: "?q=barn&library=lib1" }, published);

  assert.deepEqual(sent, {
    query: "barn",
    library_id: "lib1",
    selection: ["a1"],
    selection_count: 1,
    path: "/",
  });
});

test("a player back at the start says so, over the moment in the address", () => {
  // Opened at ?t=45 and dragged back to 0:00: "this moment" is the frame on
  // screen, not the one the link pointed at.
  const published = { context: { filename: "DJI_0042.MP4", at_s: 0 }, path: "/assets/abc" };

  const sent = pageContext({ pathname: "/assets/abc", search: "?t=45" }, published);

  assert.equal(sent.at_s, 0);
});

test("a library known only by its id is sent by name too, so Claude's request to confirm names it", () => {
  const names = new Map([["lib1", "GELCO"]]);
  // A clip's page publishes the library's id alone.
  const clip = { path: "/assets/abc", context: { library_id: "lib1", folder: "2026/Pete Dye" } };
  assert.deepEqual(pageContext({ pathname: "/assets/abc" }, clip, names), {
    asset_id: "abc",
    library_id: "lib1",
    library: "GELCO",
    folder: "2026/Pete Dye",
    path: "/assets/abc",
  });
  // Browse with ?library= in the address, before the page has named it.
  const browse = pageContext({ pathname: "/browse", search: "?library=lib1" }, null, names);
  assert.equal(browse.library, "GELCO");
  // The page's own name wins; a library not known is sent by id alone, as before.
  assert.equal(withLibraryName({ library_id: "lib1", library: "GELCO (old name)" }, names).library, "GELCO (old name)");
  assert.deepEqual(withLibraryName({ library_id: "lib9" }, names), { library_id: "lib9" });
  assert.deepEqual(withLibraryName({ query: "barn" }, names), { query: "barn" });
});

test("learning the libraries' names tells the panel once, and only when a name is new", () => {
  let told = 0;
  const stop = subscribeChatContext(() => {
    told += 1;
  });
  try {
    knowLibraries([{ id: "lib1", name: "GELCO" }]);
    knowLibraries([{ id: "lib1", name: "GELCO" }]);
    assert.equal(told, 1);
    setChatContext({ library_id: "lib1", path: "/assets/abc" });
    assert.equal(pageContext({ pathname: "/assets/abc" }).library, "GELCO");
    knowLibraries([{ id: "lib1", name: "Intel" }]);
    assert.equal(told, 3);
    assert.equal(pageContext({ pathname: "/assets/abc" }).library, "Intel");
  } finally {
    stop();
    setChatContext(null);
    knowLibraries([]);
  }
});
