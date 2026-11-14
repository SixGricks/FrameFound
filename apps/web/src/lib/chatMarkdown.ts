// Claude's answers, read into something the chat panel can draw.
//
// Claude writes a light Markdown: paragraphs, lists, **bold**, `code`, links.
// The panel must show that as formatting, and must do it without ever
// handing the text to the browser as HTML: the text comes from a model that
// has been reading file names, transcripts and tags out of the catalogue, any
// of which could carry markup of its own. So this file turns the text into a
// small tree of plain objects, and the panel draws the tree with ordinary
// elements. Nothing here can produce a tag, an attribute or a script; the
// only thing that reaches an attribute is a link's address, and that goes
// through `linkTarget`, which lets http, https and mailto through and
// nothing else.
//
// It is read while the answer is still arriving, so half-written formatting
// must stay readable: an unclosed `**` is shown as it was typed until its
// other half arrives, and an unclosed code fence runs to the end.
//
// What is not understood is shown as written (a table stays lines of text
// with bars in them). That is the right way round for a parser this small.
//
// Nothing is imported, and no lookbehind is used in any pattern (Safari
// before 16.4 refuses the whole file for one), so `node --test` loads this
// file as it is (tests/chatMarkdown.test.mjs).

export type Inline =
  | { kind: "text"; text: string }
  | { kind: "code"; text: string }
  | { kind: "bold"; children: Inline[] }
  | { kind: "italic"; children: Inline[] }
  | { kind: "link"; text: string; href: string; internal: boolean };

export interface ListItem {
  /** 0 for the outer list, 1 for a list inside it, and so on (at most 3). */
  depth: number;
  /** What stands in front of the item: "•", or its number as written ("2."). */
  marker: string;
  lines: Inline[][];
}

export type Block =
  | { kind: "paragraph"; lines: Inline[][] }
  | { kind: "heading"; text: Inline[] }
  | { kind: "list"; items: ListItem[] }
  | { kind: "code"; text: string }
  | { kind: "rule" };

/**
 * Where a link written in an answer may lead, or null when it may not be a
 * link at all.
 *
 * `internal` is a page of this app (same origin, and not a file under
 * /api/): the panel opens those in place, beside itself, and everything
 * else in a new tab. `origin` is the app's own, e.g. "http://framefound.lan".
 *
 * Always through the URL parser, never by looking at the text: a browser
 * drops tabs and line breaks from inside a scheme, so "java\nscript:" is
 * javascript: to it and must be to this.
 */
export function linkTarget(raw: string, origin = ""): { href: string; internal: boolean } | null {
  const written = raw.trim();
  if (written === "") return null;
  const base = "http://framefound.invalid";
  let parsed: URL;
  try {
    parsed = written.startsWith("/") ? new URL(written, origin || base) : new URL(written);
  } catch {
    return null;
  }
  if (parsed.protocol === "mailto:") return { href: parsed.href, internal: false };
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  const here = parsed.origin === (origin || base);
  // "/\\elsewhere.example" and "//elsewhere.example" start with a slash and
  // are another site all the same: the parser says so, and it is believed.
  if (written.startsWith("/") && !here) return null;
  if (!here) return { href: parsed.href, internal: false };
  const path = parsed.pathname + parsed.search + parsed.hash;
  if (parsed.pathname.startsWith("/api/")) {
    // A file the server hands out (a preview, a download), not a page.
    return { href: origin ? parsed.href : path, internal: false };
  }
  return { href: path, internal: true };
}

/**
 * Where a link that leaves the app goes, to be shown beside its words: the
 * site's name, or the address of a mailto. Null when there is nothing to
 * add — a page or file of this app, or words that already name the place.
 *
 * Claude's words are shaped by what it read in the catalogue, and a file
 * name or a transcript can carry an instruction of its own. "[Open the clip
 * in Premiere](https://elsewhere.example/login)" drawn as its words alone
 * looks like a button of this app; with "elsewhere.example" beside it, it
 * does not. (A phone shows no address on hover.)
 */
export function offsite(href: string, text: string, origin = ""): string | null {
  let parsed: URL;
  try {
    parsed = new URL(href, origin || "http://framefound.invalid");
  } catch {
    return null;
  }
  const words = text.trim().toLowerCase();
  if (parsed.protocol === "mailto:") {
    const address = decodeURIComponent(parsed.pathname);
    return address === "" || words.includes(address.toLowerCase()) ? null : address;
  }
  if (origin !== "" && parsed.origin === origin) return null;
  if (origin === "" && href.startsWith("/")) return null;
  const host = parsed.hostname.replace(/^www\./, "");
  return host === "" || words.includes(host.toLowerCase()) ? null : host;
}

const WORD = /[A-Za-z0-9]/;
const SPACE = /\s/;
const LINK = /^\[([^\]\n]+)\]\(([^)\s]+)\)/;
const BARE = /^https?:\/\/[^\s<>"'`]+/;

interface Allowed {
  bold: boolean;
  italic: boolean;
}

/** One line of text as runs of plain text, code, emphasis and links. */
export function inline(text: string, origin = "", allowed: Allowed = { bold: true, italic: true }): Inline[] {
  const out: Inline[] = [];
  let plain = "";
  const flush = () => {
    if (plain !== "") out.push({ kind: "text", text: plain });
    plain = "";
  };
  let at = 0;
  while (at < text.length) {
    const ch = text[at] ?? "";
    const before = at > 0 ? (text[at - 1] ?? "") : "";
    const after = text[at + 1] ?? "";

    if (ch === "`") {
      const end = text.indexOf("`", at + 1);
      if (end > at + 1) {
        flush();
        out.push({ kind: "code", text: text.slice(at + 1, end) });
        at = end + 1;
        continue;
      }
    }

    if (ch === "[") {
      const found = LINK.exec(text.slice(at));
      if (found) {
        const target = linkTarget(found[2] ?? "", origin);
        if (target) {
          flush();
          out.push({ kind: "link", text: found[1] ?? "", ...target });
        } else {
          // Not somewhere a link may lead: shown as it was written, address
          // and all, so nothing is hidden behind friendly words.
          plain += found[0];
        }
        at += found[0].length;
        continue;
      }
    }

    if (ch === "h" && !WORD.test(before)) {
      const found = BARE.exec(text.slice(at));
      if (found) {
        // The full stop that ends the sentence is not part of the address;
        // nor is a bracket that was opened before it.
        let address = found[0].replace(/[.,;:!?]+$/, "");
        while (address.endsWith(")") && !address.includes("(")) address = address.slice(0, -1);
        const target = linkTarget(address, origin);
        if (target) {
          flush();
          out.push({ kind: "link", text: address, ...target });
          at += address.length;
          continue;
        }
      }
    }

    if (allowed.bold && ch === "*" && after === "*") {
      const end = text.indexOf("**", at + 2);
      const inner = end === -1 ? "" : text.slice(at + 2, end);
      if (inner !== "" && !SPACE.test(inner[0] ?? " ") && !SPACE.test(inner[inner.length - 1] ?? " ")) {
        flush();
        out.push({ kind: "bold", children: inline(inner, origin, { bold: false, italic: true }) });
        at = end + 2;
        continue;
      }
    }

    // *like this* or _like this_. An underscore inside a word is a file
    // name (IMG_0412_final.jpg), and a star with a space after it is
    // arithmetic or a bullet: neither is emphasis.
    if (allowed.italic && (ch === "*" || ch === "_") && after !== ch && after !== "" && !SPACE.test(after)) {
      const opens = ch === "*" ? before !== "*" : !WORD.test(before) && before !== "_";
      if (opens) {
        let end = text.indexOf(ch, at + 1);
        while (end !== -1) {
          const last = text[end - 1] ?? " ";
          const next = text[end + 1] ?? "";
          const closes = !SPACE.test(last) && next !== ch && (ch === "*" || !WORD.test(next));
          if (closes) break;
          end = text.indexOf(ch, end + 1);
        }
        if (end > at + 1) {
          flush();
          out.push({
            kind: "italic",
            children: inline(text.slice(at + 1, end), origin, { bold: false, italic: false }),
          });
          at = end + 1;
          continue;
        }
      }
    }

    plain += ch;
    at += 1;
  }
  flush();
  return out;
}

const FENCE = /^\s*```/;
const HEADING = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/;
const RULE = /^\s*([-*_])(\s*\1){2,}\s*$/;
const BULLET = /^(\s*)[-*+•]\s+(.*)$/;
const NUMBERED = /^(\s*)(\d{1,9})[.)]\s+(.*)$/;

function indentOf(spaces: string): number {
  let width = 0;
  for (const ch of spaces) width += ch === "\t" ? 4 : 1;
  return width;
}

/**
 * An answer as blocks to draw, in order. `origin` is the app's own address,
 * for telling a link to one of its pages from a link elsewhere.
 */
export function markdownLite(text: string, origin = ""): Block[] {
  const blocks: Block[] = [];
  let paragraph: Inline[][] | null = null;
  let list: { items: ListItem[]; base: number } | null = null;
  let code: string[] | null = null;

  const close = () => {
    if (paragraph) blocks.push({ kind: "paragraph", lines: paragraph });
    if (list) blocks.push({ kind: "list", items: list.items });
    paragraph = null;
    list = null;
  };

  for (const raw of text.replace(/\r\n?/g, "\n").split("\n")) {
    if (code !== null) {
      if (FENCE.test(raw)) {
        blocks.push({ kind: "code", text: code.join("\n") });
        code = null;
      } else {
        code.push(raw);
      }
      continue;
    }
    if (FENCE.test(raw)) {
      close();
      code = [];
      continue;
    }
    if (raw.trim() === "") {
      close();
      continue;
    }
    if (RULE.test(raw)) {
      close();
      blocks.push({ kind: "rule" });
      continue;
    }
    const heading = HEADING.exec(raw);
    if (heading) {
      close();
      blocks.push({ kind: "heading", text: inline(heading[1] ?? "", origin) });
      continue;
    }
    const bullet = BULLET.exec(raw);
    const numbered = bullet ? null : NUMBERED.exec(raw);
    if (bullet || numbered) {
      if (paragraph) close();
      const indent = indentOf((bullet ?? numbered)?.[1] ?? "");
      if (list === null) list = { items: [], base: indent };
      list.items.push({
        depth: Math.max(0, Math.min(3, Math.floor((indent - list.base) / 2))),
        marker: bullet ? "•" : `${numbered?.[2] ?? "1"}.`,
        lines: [inline((bullet ? bullet[2] : numbered?.[3]) ?? "", origin)],
      });
      continue;
    }
    const words = inline(raw.trim(), origin);
    const lastItem = list?.items[list.items.length - 1];
    if (lastItem && /^\s/.test(raw)) {
      // Indented under a list item: the item carries on.
      lastItem.lines.push(words);
      continue;
    }
    if (list) close();
    if (paragraph === null) paragraph = [];
    paragraph.push(words);
  }
  // Still open at the end of what has arrived: Claude is part-way through.
  if (code !== null) blocks.push({ kind: "code", text: code.join("\n") });
  close();
  return blocks;
}

/** The words of an answer with the formatting marks left out, for reading
 *  aloud: a screen reader should say "silo", not "star star silo star star". */
export function plainText(text: string): string {
  const said = (runs: Inline[]): string =>
    runs
      .map((run) =>
        run.kind === "bold" || run.kind === "italic" ? said(run.children) : run.text,
      )
      .join("");
  const parts: string[] = [];
  for (const block of markdownLite(text)) {
    if (block.kind === "paragraph") parts.push(block.lines.map(said).join(" "));
    else if (block.kind === "heading") parts.push(said(block.text));
    else if (block.kind === "list") {
      for (const item of block.items) parts.push(item.lines.map(said).join(" "));
    } else if (block.kind === "code") parts.push(block.text);
  }
  return parts.join("\n").trim();
}
