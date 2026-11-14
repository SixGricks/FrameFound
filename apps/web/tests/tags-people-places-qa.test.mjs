// Run with `npm test`.
//
// Final QA, Tags, People and Places: the tag viewer's line about who
// suggested a picture can be read on a phone, counts read "1 person" and not
// "1 people", and Places points at the Basemaps page for a real map.
//
// These pages are React with no DOM to render them in here, so the words are
// pinned through the helper they share and the sources are held to it.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { count } from "../src/lib/storage.ts";

const read = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), "utf8");
const uncommented = (css) => css.replace(/\/\*[\s\S]*?\*\//g, "");

// ---- the tag viewer on a phone ----------------------------------------------------------

/** The declarations of the last rule in `css` whose selector is exactly `selector`. */
function rule(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const found = [...css.matchAll(new RegExp(`(?:^|\\})\\s*${escaped}\\s*\\{([^}]*)\\}`, "g"))];
  return found.length ? found[found.length - 1][1] : null;
}

test("the tag viewer's line wraps, at every width, instead of cutting off the model", () => {
  // QA: at 375 px the line stopped at "suggested by local", hiding
  // "model (made up)", the % and the model's name, and touch has no tooltip.
  const css = uncommented(read("app/globals.css"));
  const line = rule(css, ".tag-viewer .preview-title p");
  assert.ok(line, "no rule lets the tag viewer's line wrap");
  assert.match(line, /white-space:\s*normal/);
  // Outside any @media block: a tablet held upright cut it too.
  const before = css.slice(0, css.lastIndexOf(".tag-viewer .preview-title p"));
  const depth = [...before].reduce((d, ch) => d + (ch === "{" ? 1 : ch === "}" ? -1 : 0), 0);
  assert.equal(depth, 0, "the wrapping rule sits inside a media query");
  // The wrapper must not become a box of its own around the dialog.
  assert.match(rule(css, ".tag-viewer") ?? "", /display:\s*contents/);
});

test("the Tags page opens its pictures in the viewer that wraps, and the line has a title", () => {
  const page = read("app/tags/page.tsx");
  assert.ok(page.includes("<TagViewer"), "the Tags page no longer uses TagViewer");
  assert.ok(!page.includes("<MediaPreview"), "the Tags page opens the unwrapped viewer again");
  const viewer = read("components/TagViewer.tsx");
  assert.ok(viewer.includes('className="tag-viewer"'));
  assert.ok(viewer.includes("<MediaPreview {...props} />"));
  // The title is the shared viewer's own now, in every viewer.
  assert.match(read("components/MediaPreview.tsx"), /<p className="faint mono" title=\{about\}>/);
});

// ---- counts that read as English --------------------------------------------------------

test("one named person reads '1 person', and the other counts agree in number", () => {
  assert.equal(count(1, "person", "people"), "1 person");
  assert.equal(count(2, "person", "people"), "2 people");
  assert.equal(count(1, "face"), "1 face");
  assert.equal(count(1, "tag"), "1 tag");
  assert.equal(count(1, "asset"), "1 asset");
  assert.equal(count(1200, "asset"), "1,200 assets");
});

test("no count on these pages is followed by a plural written out by hand", () => {
  // "{named.length} people" read "1 people". Every count before a noun goes
  // through count(), which picks the word by the number.
  const nouns = "people|persons|faces|tags|assets|groups|places|examples";
  const bare = new RegExp(
    String.raw`(?:\{|\$\{)[\w.?]+(?:length|count|_count)\}\s+(?:${nouns})\b`,
    "g",
  );
  const sources = [
    "app/people/page.tsx",
    "app/people/[id]/page.tsx",
    "app/tags/page.tsx",
    "components/PlaceMap.tsx",
  ];
  const found = sources.flatMap((path) => (read(path).match(bare) ?? []).map((m) => `${path}: ${m}`));
  assert.deepEqual(found, []);
});

// ---- Places points at the Basemaps page -------------------------------------------------

test("Places sends people to the Basemaps page for a real map, as a link", () => {
  const map = read("components/PlaceMap.tsx");
  // QA: the hint named only the Security page, in plain text, after the
  // Basemaps page had made a real map one download and one press.
  const links = [...map.matchAll(/<Link[^>]*href="\/basemaps"[^>]*>\s*Basemaps\s*<\/Link>/g)];
  assert.equal(links.length, 2, "both the hint and the failure line should link to Basemaps");
  // The tile-server route is still offered, not taken away.
  assert.match(map, /<Link[^>]*href="\/security"[^>]*>\s*Security\s*<\/Link>/);
  assert.ok(!map.includes("add a self-hosted tile style\n          on the Security page"));
});
