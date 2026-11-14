// Run with `npm test` (Node's own test runner; nothing to install).
//
// Claude's answers are drawn from a tree this parser makes, never handed to
// the browser as HTML: the text comes from a model that has been reading
// file names and transcripts out of the catalogue. These pin the formatting
// the panel shows, that half an answer reads sensibly while the rest is
// still arriving, and above all where a link is allowed to lead.

import assert from "node:assert/strict";
import { test } from "node:test";

import { inline, linkTarget, markdownLite, offsite, plainText } from "../src/lib/chatMarkdown.ts";

const HERE = "http://framefound.lan";
const text = (value) => ({ kind: "text", text: value });

test("paragraphs are split on blank lines and keep their own line breaks", () => {
  assert.deepEqual(markdownLite("One line.\nA second.\n\nAnother paragraph."), [
    { kind: "paragraph", lines: [[text("One line.")], [text("A second.")]] },
    { kind: "paragraph", lines: [[text("Another paragraph.")]] },
  ]);
  assert.deepEqual(markdownLite(""), []);
  assert.deepEqual(markdownLite("\r\nWindows\r\nline ends\r\n"), [
    { kind: "paragraph", lines: [[text("Windows")], [text("line ends")]] },
  ]);
});

test("bold, emphasis and code are runs inside a line", () => {
  assert.deepEqual(inline("The **north barn** at *dusk*, file `DJI_0042.MP4`."), [
    text("The "),
    { kind: "bold", children: [text("north barn")] },
    text(" at "),
    { kind: "italic", children: [text("dusk")] },
    text(", file "),
    { kind: "code", text: "DJI_0042.MP4" },
    text("."),
  ]);
  // Nothing inside code is formatting.
  assert.deepEqual(inline("`**not bold**`"), [{ kind: "code", text: "**not bold**" }]);
});

test("a file name with underscores and a sum with stars are not emphasis", () => {
  assert.deepEqual(inline("IMG_0412_final.jpg and my_file_name"), [
    text("IMG_0412_final.jpg and my_file_name"),
  ]);
  assert.deepEqual(inline("2 * 3 * 4 = 24"), [text("2 * 3 * 4 = 24")]);
  // Each end is checked on its own: a mark that could only close is not one
  // that opens.
  assert.deepEqual(inline("the file_name_ is odd"), [text("the file_name_ is odd")]);
  assert.deepEqual(inline("a * b* c"), [text("a * b* c")]);
  assert.deepEqual(inline("_really_ sharp"), [
    { kind: "italic", children: [text("really")] },
    text(" sharp"),
  ]);
});

test("formatting that has not been closed yet is shown as typed", () => {
  // An answer is drawn while it arrives; the other half may be a moment away.
  assert.deepEqual(inline("The **north ba"), [text("The **north ba")]);
  assert.deepEqual(inline("see `DJI_00"), [text("see `DJI_00")]);
  assert.deepEqual(inline("[the clip](http://framefound.lan/ass"), [
    text("[the clip]("),
    { kind: "link", text: "http://framefound.lan/ass", href: "http://framefound.lan/ass", internal: false },
  ]);
});

test("lists keep each item's marker and how deep it sits", () => {
  const [list] = markdownLite("- first\n- second\n  - inside\n    carried on\n- third");
  assert.equal(list.kind, "list");
  assert.deepEqual(
    list.items.map((item) => [item.depth, item.marker, item.lines.length]),
    [
      [0, "•", 1],
      [0, "•", 1],
      [1, "•", 2],
      [0, "•", 1],
    ],
  );
  const [numbered] = markdownLite("1. open it\n2. play it\n10) stop");
  assert.deepEqual(
    numbered.items.map((item) => item.marker),
    ["1.", "2.", "10."],
  );
});

test("a line of bold is not mistaken for a bullet, and a rule is not a list", () => {
  assert.equal(markdownLite("**Summary** of the folder")[0].kind, "paragraph");
  assert.deepEqual(markdownLite("above\n\n---\n\nbelow").map((block) => block.kind), [
    "paragraph",
    "rule",
    "paragraph",
  ]);
});

test("a heading is a block of its own and ends what came before it", () => {
  assert.deepEqual(markdownLite("intro\n## What I found\n- one"), [
    { kind: "paragraph", lines: [[text("intro")]] },
    { kind: "heading", text: [text("What I found")] },
    { kind: "list", items: [{ depth: 0, marker: "•", lines: [[text("one")]] }] },
  ]);
});

test("a code fence keeps its lines as they are, and one still open runs to the end", () => {
  assert.deepEqual(markdownLite("```xml\n<clip **x** />\n\n  indented\n```\nafter"), [
    { kind: "code", text: "<clip **x** />\n\n  indented" },
    { kind: "paragraph", lines: [[text("after")]] },
  ]);
  assert.deepEqual(markdownLite("```\nstill arriv"), [{ kind: "code", text: "still arriv" }]);
});

test("a link to a page of this app is opened in place; any other in a new tab", () => {
  assert.deepEqual(linkTarget(`${HERE}/assets/1704c870?t=12`, HERE), {
    href: "/assets/1704c870?t=12",
    internal: true,
  });
  assert.deepEqual(linkTarget("/browse?tag=silo", HERE), { href: "/browse?tag=silo", internal: true });
  assert.deepEqual(linkTarget("https://docs.example.com/a", HERE), {
    href: "https://docs.example.com/a",
    internal: false,
  });
  assert.deepEqual(linkTarget("mailto:office@example.com", HERE), {
    href: "mailto:office@example.com",
    internal: false,
  });
  // A file the server hands out is not a page: it must not be opened by the
  // app's own router, which would only fall over on it.
  assert.deepEqual(linkTarget(`${HERE}/api/v1/media/abc/thumbnail`, HERE), {
    href: `${HERE}/api/v1/media/abc/thumbnail`,
    internal: false,
  });
});

test("a link that would run something, or pretends to be this site, is not a link", () => {
  for (const bad of [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "java\nscript:alert(1)",
    " javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "//elsewhere.example/path",
    "/\\elsewhere.example/path",
    "not a link",
    "",
  ]) {
    assert.equal(linkTarget(bad, HERE), null, JSON.stringify(bad));
  }
  // And what was written is still shown, address included.
  assert.deepEqual(inline("[click me](javascript:alert(1))", HERE), [
    text("[click me](javascript:alert(1))"),
  ]);
});

test("an address written out bare becomes a link, without the sentence's full stop", () => {
  assert.deepEqual(inline(`Open ${HERE}/assets/1704c870.`, HERE), [
    text("Open "),
    { kind: "link", text: `${HERE}/assets/1704c870`, href: "/assets/1704c870", internal: true },
    text("."),
  ]);
  assert.deepEqual(inline("(see https://example.com/a)", HERE), [
    text("(see "),
    { kind: "link", text: "https://example.com/a", href: "https://example.com/a", internal: false },
    text(")"),
  ]);
});

test("markup in an answer stays text: nothing is ever read as a tag", () => {
  const blocks = markdownLite('<img src=x onerror="alert(1)"> and <script>alert(2)</script>');
  assert.deepEqual(blocks, [
    {
      kind: "paragraph",
      lines: [[text('<img src=x onerror="alert(1)"> and <script>alert(2)</script>')]],
    },
  ]);
});

test("read aloud, an answer is its words without the marks", () => {
  assert.equal(
    plainText("## Found\n\nThe **north barn** at *dusk*:\n\n- `DJI_0042.MP4`\n- [the clip](/assets/1)"),
    "Found\nThe north barn at dusk:\nDJI_0042.MP4\nthe clip",
  );
});

test("a link that leaves the app says where it goes, unless its words already do", () => {
  // Words Claude wrote after reading a booby-trapped file name must not
  // pass for a button of this app.
  assert.equal(offsite("https://evil.example/login", "Open the clip in Premiere", HERE), "evil.example");
  assert.equal(offsite("https://www.adobe.com/premiere", "Premiere", HERE), "adobe.com");
  assert.equal(offsite("mailto:office@example.com", "write to the office", HERE), "office@example.com");
  // Nothing to add.
  assert.equal(offsite("https://example.com/a", "https://example.com/a", HERE), null);
  assert.equal(offsite("https://docs.example.com/a", "the docs.example.com guide", HERE), null);
  assert.equal(offsite(`${HERE}/api/v1/media/abc/thumbnail`, "the thumbnail", HERE), null);
  assert.equal(offsite("/api/v1/media/abc/thumbnail", "the thumbnail"), null);
});
