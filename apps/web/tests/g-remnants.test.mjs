// Run with `npm test`.
//
// Final QA, the remnants (batch G): Search's Send to Premiere said "No path
// profiles yet" while it was still reading them, and every viewer but the
// Tags one cut its line under the title short on a phone, with no way to
// read the rest.
//
// These pages are React with no DOM to render them in here, so the words are
// pinned through the helpers they share and the sources are held to them.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { profilesWord } from "../src/lib/cuts.ts";

const read = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");
const uncommented = (css) => css.replace(/\/\*[\s\S]*?\*\//g, "");

// ---- Search's Send to Premiere ------------------------------------------------------------

test("Search says it is reading the path profiles until the server has answered", () => {
  assert.equal(profilesWord("reading", 0), "Reading path profiles…");
  assert.equal(profilesWord("read", 0), "No path profiles yet");
  assert.equal(profilesWord("failed", 0), "Path profiles could not be read");
  assert.equal(profilesWord("read", 2), null);

  const page = read("app/page.tsx");
  assert.ok(!page.includes(">No path profiles yet<"), "Search writes its own sentence again");
  assert.match(page, /profilesWord\(profilesRead, profiles\.length\)/);
  assert.match(page, /setProfilesRead\("read"\)/);
  assert.match(page, /setProfilesRead\("failed"\)/);
  // Pressed while the list was being read, the file had the server's paths.
  assert.match(page, /disabled=\{picked\.size === 0 \|\| sending \|\| profilesRead === "reading"\}/);
});

// ---- the viewer's line under its title ----------------------------------------------------

/** The declarations of `selector`'s rule inside the first `@media (max-width: 640px)`
 *  block that has one. */
function phoneRule(css, selector) {
  for (const at of css.matchAll(/@media \(max-width: 640px\) \{/g)) {
    let depth = 1;
    let end = at.index + at[0].length;
    while (depth > 0 && end < css.length) {
      if (css[end] === "{") depth += 1;
      if (css[end] === "}") depth -= 1;
      end += 1;
    }
    const block = css.slice(at.index + at[0].length, end - 1);
    const found = block.match(new RegExp(`(?:^|\\})\\s*${selector.replace(/[.]/g, "\\.")}\\s*\\{([^}]*)\\}`));
    if (found) return found[1];
  }
  return null;
}

test("every viewer's line wraps on a phone, and carries its own words as a title", () => {
  const css = uncommented(read("app/globals.css"));
  const line = phoneRule(css, ".preview-title p");
  assert.ok(line, "no phone rule lets the viewer's line wrap");
  assert.match(line, /white-space:\s*normal/);
  assert.match(line, /overflow-wrap:\s*anywhere/);
  // The Tags viewer still wraps at every width.
  assert.ok(css.includes(".tag-viewer .preview-title p"));

  const viewer = read("components/MediaPreview.tsx");
  assert.match(viewer, /<p className="faint mono" title=\{about\}>/);
  // The ways to open and leave the item are all still there.
  assert.ok(viewer.includes("Open full page ↗"));
  assert.match(viewer, /onClick=\{onClose\}/);
});
