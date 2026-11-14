// Run with `npm test` (Node's own test runner; nothing to install).
//
// A page's tab title (src/lib/hooks.ts `usePageTitle`): Next can write the
// site's own title back after a page has set its own (seen on Search on a
// fresh load). The page's title wins then, and only then: a title another
// page chose is never fought over.

import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

import { SITE_TITLE, tabTitle, titleTakenBack } from "../src/lib/hooks.ts";

const APP = join(import.meta.dirname, "..", "src", "app");

/** Every page.tsx under src/app, as a path from there ("tags/page.tsx"). */
function pages(dir = APP, prefix = "") {
  const found = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const here = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) found.push(...pages(join(dir, entry.name), here));
    else if (entry.name === "page.tsx") found.push(here);
  }
  return found;
}

test("every page names itself in the tab, so no two read only 'FrameFound'", () => {
  const all = pages();
  assert.ok(all.length > 20, `only ${all.length} pages found under ${APP}`);
  // A client page calls the hook; a server page (none today) would export
  // Next's metadata instead, as a hook cannot run there.
  const untitled = all.filter((page) => {
    const source = readFileSync(join(APP, page), "utf8");
    return !/usePageTitle\(|export (const metadata|async function generateMetadata|function generateMetadata)/.test(
      source,
    );
  });
  assert.deepEqual(untitled, []);
});

test("a page's tab title names the page, then the site", () => {
  assert.equal(tabTitle("Search: golf"), "Search: golf · FrameFound");
  assert.equal(tabTitle(""), SITE_TITLE);
  assert.equal(tabTitle(null), SITE_TITLE);
  assert.equal(tabTitle(undefined), SITE_TITLE);
});

test("the site's own title written over the page's is taken back; another page's is not", () => {
  const wanted = tabTitle("Search");
  // Next applied the layout's title after the page had set its own.
  assert.equal(titleTakenBack("FrameFound", wanted), true);
  assert.equal(titleTakenBack("", wanted), true);
  // Already the page's: nothing to write (and no write to see again).
  assert.equal(titleTakenBack(wanted, wanted), false);
  // Another page's choice, such as a cut's name: left alone.
  assert.equal(titleTakenBack("Pete Dye Quick 30 · FrameFound", wanted), false);
  // A page that wants the site's own title has nothing to take back.
  assert.equal(titleTakenBack(SITE_TITLE, tabTitle(null)), false);
});
