// Run with `npm test` (Node's own test runner; nothing to install).
//
// Final-QA fixes on the listing and Slideshows pages: a local Apply that
// finished before the page first looked, "Add photos > Search" by what is in
// the photograph, the gallery tile at phone width, a room the list does not
// know, and a refused slideshow render. The decisions live beside the pages
// (src/app/listings/[id]/listingWords.ts, src/app/slideshows/slideshowWords.ts);
// where the fix is in the page's markup, the page's source is read.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { noOffersLine, runEnded, searchOffers } from "../src/app/listings/[id]/listingWords.ts";
import { renderRefused } from "../src/app/slideshows/slideshowWords.ts";

const LISTING_PAGE = readFileSync(
  new URL("../src/app/listings/[id]/page.tsx", import.meta.url),
  "utf8",
);
const SLIDESHOWS_PAGE = readFileSync(
  new URL("../src/app/slideshows/page.tsx", import.meta.url),
  "utf8",
);

function hit(asset_id, filename = `${asset_id}.jpg`) {
  return { asset_id, filename, library_id: "lib", media_type: "image" };
}

function response(parts) {
  return {
    query: "kitchen",
    transcript_hits: [],
    filename_hits: [],
    visual_hits: [],
    visual_available: false,
    visual_status: "unavailable",
    tag_hits: [],
    caption_hits: [],
    results: [],
    ...parts,
  };
}

test("a run already over when the page first looks is said as over, not as applying", () => {
  // A one-photograph Apply can be done before the first reload after the
  // press; the page kept "Applying the local model's suggestions…" for good.
  assert.deepEqual(runEnded({ ai_edit_state: "done", ai_edit_message: "1 edited" }), {
    kind: "notice",
    text: "Auto-edit finished: 1 edited",
  });
  assert.deepEqual(runEnded({ ai_edit_state: "failed", ai_edit_message: "" }), {
    kind: "error",
    text: "Auto-edit failed",
  });
  for (const state of ["queued", "running", "idle"]) {
    assert.equal(runEnded({ ai_edit_state: state, ai_edit_message: "" }), null);
  }
  assert.equal(runEnded(null), null);
});

test("both ways of starting a run look at the first reload before saying it has started", () => {
  const starts = LISTING_PAGE.match(/runEnded\((await load\(\)|fresh)\)/g) ?? [];
  assert.equal(starts.length, 2, "aiEditAll and applyLocal both ask whether the run is over");
});

test("a search by what is in the photo offers the tagged and captioned photographs", () => {
  // Picture search unavailable, so only tags and captions matched "kitchen".
  const found = response({
    tag_hits: [{ ...hit("a"), tag_name: "kitchen", tag_slug: "kitchen", confirmed: true }],
    caption_hits: [{ ...hit("b"), caption: "A kitchen with an island", ts_ms: null }],
    results: [{ ...hit("b"), score: 1 }, { ...hit("a"), score: 0.5 }],
  });
  const { offers, found: count } = searchOffers(found, new Set());
  // Best first, as the fused list ranks them, and each once.
  assert.deepEqual(
    offers.map((o) => o.asset_id),
    ["b", "a"],
  );
  assert.deepEqual(offers[0], { asset_id: "b", filename: "b.jpg", media_type: "image" });
  assert.equal(count, 2);
});

test("a search offers what the fused list left out, and never what the listing has", () => {
  const found = response({
    filename_hits: [{ ...hit("c"), captured_at: null }],
    visual_hits: [{ ...hit("d"), ts_ms: 0, similarity: 0.3 }],
    tag_hits: [{ ...hit("a"), tag_name: "kitchen", tag_slug: "kitchen", confirmed: true }],
    results: [{ ...hit("a"), score: 1 }],
  });
  const { offers, found: count } = searchOffers(found, new Set(["a"]));
  assert.deepEqual(
    offers.map((o) => o.asset_id),
    ["d", "c"],
  );
  assert.equal(count, 3);
  // A server from before captions sends none.
  const older = response({ results: [{ ...hit("e"), score: 1 }] });
  delete older.caption_hits;
  assert.deepEqual(
    searchOffers(older, new Set()).offers.map((o) => o.asset_id),
    ["e"],
  );
  assert.deepEqual(searchOffers(null, new Set()), { offers: [], found: 0 });
});

test("a search that offers nothing says why", () => {
  assert.equal(
    noOffersLine("kitchen", 0),
    "Nothing matched “kitchen”. Search looks at file names, folders, tags and what is in the photo.",
  );
  assert.equal(
    noOffersLine("kitchen", 4),
    "Everything that matched “kitchen” is already in this listing.",
  );
  assert.match(LISTING_PAGE, /noOffersLine\(results\.query, searchFound\)/);
});

test("a tile's file name and room select stay inside a phone's narrow tile", () => {
  // An inline span never shows its ellipsis; a select sized to its longest
  // room ran past a 167 px tile.
  const name = LISTING_PAGE.slice(LISTING_PAGE.indexOf("{item.export_name && ("));
  const span = name.slice(0, name.indexOf("</span>"));
  assert.match(span, /display: "block"/);
  assert.match(span, /textOverflow: "ellipsis"/);
  const select = name.slice(name.indexOf("<select"), name.indexOf("onChange"));
  assert.match(select, /maxWidth: "100%"/);
  // The action row (Edit, Remove, ← →) wraps rather than pushing → past
  // the tile's edge, where overflow:hidden cut it in half on a phone.
  const row = name.slice(name.indexOf("</select>"), name.indexOf('className="tile-move"'));
  assert.match(row, /display: "flex", gap: 4, flexWrap: "wrap"/);
});

test("a stored room the room list does not have is shown as itself", () => {
  assert.match(
    LISTING_PAGE,
    /item\.room && !rooms\.some\(\(room\) => room\.key === item\.room\)/,
  );
});

test("a refused render says why beside Render, as an alert, and that nothing was made", () => {
  assert.equal(
    renderRefused("The processing queue is unavailable"),
    "Not rendered: the processing queue is unavailable. Nothing was made, and your selection " +
      "is still here to render again.",
  );
  // An acronym at the start keeps its capitals.
  assert.match(renderRefused("API key missing."), /^Not rendered: API key missing\. Nothing/);
  assert.equal(
    renderRefused(null),
    "Not rendered: the server could not be reached. Nothing was made, and your selection " +
      "is still here to render again.",
  );
  // Beside the toolbar that holds Render, not at the top of the page.
  const toolbar = SLIDESHOWS_PAGE.indexOf("ref={renderButton}");
  const alert = SLIDESHOWS_PAGE.indexOf('{renderProblem && (\n            <p className="faint" role="alert">');
  assert.ok(toolbar > 0 && alert > toolbar, "the refusal is drawn after the Render button");
  assert.ok(alert - toolbar < 800, "and right after it");
  // Focus returns to the button once it is enabled again.
  assert.match(SLIDESHOWS_PAGE, /focusAfterRender\.current = "button"/);
});
