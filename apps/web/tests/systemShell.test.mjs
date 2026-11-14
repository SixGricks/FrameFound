// Run with `npm test` (Node's own test runner; nothing to install).
//
// The system pages and the shell, from the final QA:
// - a link to a Security section (/security#services) arrives while the
//   page is still "Loading…", so the page has to go there itself;
// - the Manage menu is marked up as a menu, so it has to answer a menu's
//   keys, and Escape must not leave the focus on nothing;
// - the Tailscale card must not tell a Mac to run the Linux installer.
//
// The menu and the card are components (JSX), which Node cannot load, so
// those two are checked in their source.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

import { jumpTo, sectionFor } from "../src/app/security/jump.ts";
import { ANY_DEVICE_DOWNLOAD, onMac } from "../src/app/security/tailnetHost.ts";

const SRC = join(import.meta.dirname, "..", "src");
const source = (path) => readFileSync(join(SRC, path), "utf8");

// ---- going to a Security section -------------------------------------------

test("the hash names the section, and an empty or broken one names none", () => {
  assert.equal(sectionFor("#services"), "services");
  assert.equal(sectionFor("services"), "services");
  assert.equal(sectionFor("#two%20words"), "two words");
  assert.equal(sectionFor(""), null);
  assert.equal(sectionFor("#"), null);
  assert.equal(sectionFor("#%E0%A4%A"), null);
});

/** An element as far as going to it goes, recording what was done to it. */
function place(tagName, { parent = null, open, attrs = {} } = {}) {
  const done = [];
  return {
    tagName,
    open,
    parentElement: parent,
    attrs: { ...attrs },
    done,
    hasAttribute(name) {
      return name in this.attrs;
    },
    setAttribute(name, value) {
      this.attrs[name] = value;
    },
    scrollIntoView(options) {
      done.push(["scroll", options]);
    },
    focus(options) {
      done.push(["focus", options]);
    },
  };
}

test("going to a section scrolls to it and gives it the focus without a second scroll", () => {
  const services = place("SECTION");
  const found = jumpTo("#services", (id) => (id === "services" ? services : null));
  assert.equal(found, services);
  assert.deepEqual(services.done, [
    ["scroll", { block: "start" }],
    ["focus", { preventScroll: true }],
  ]);
  // Focusable, but not a stop on the way through the page with Tab.
  assert.equal(services.attrs.tabindex, "-1");
});

test("a section inside a closed disclosure is opened before it is gone to", () => {
  const advanced = place("DETAILS", { open: false });
  const inner = place("DIV", { parent: advanced });
  jumpTo("#inner", () => inner);
  assert.equal(advanced.open, true);
  // The disclosure itself named in the hash opens too.
  const closed = place("DETAILS", { open: false });
  jumpTo("#advanced", () => closed);
  assert.equal(closed.open, true);
});

test("a hash that names nothing on the page leaves the page alone", () => {
  assert.equal(jumpTo("#gone", () => null), null);
  assert.equal(jumpTo("", () => assert.fail("nothing to look for")), null);
});

test("an element that already has a tab stop keeps it", () => {
  const button = place("BUTTON", { attrs: { tabindex: "0" } });
  jumpTo("#b", () => button);
  assert.equal(button.attrs.tabindex, "0");
});

test("the Security page goes to the hash itself once its sections are drawn, and on a hash change", () => {
  const page = source("app/security/page.tsx");
  assert.match(page, /import \{ holdInView, jumpTo \} from "\.\/jump"/);
  assert.match(page, /const drawn = remote !== null && user !== null;/);
  assert.match(page, /jumpTo\(window\.location\.hash/);
  assert.match(page, /addEventListener\("hashchange", go\)/);
});

// ---- the Manage menu -------------------------------------------------------

test("Escape in the Manage menu gives the focus back to Manage", () => {
  const shell = source("components/Shell.tsx");
  const escape = shell.slice(shell.indexOf('if (event.key === "Escape")'));
  const branch = escape.slice(0, escape.indexOf("return;"));
  assert.match(branch, /setMenuOpen\(false\)/);
  assert.match(branch, /triggerRef\.current\?\.focus\(\)/);
  assert.match(shell, /ref=\{triggerRef\}/);
});

test("the arrows, Home and End move between the menu's items", () => {
  const shell = source("components/Shell.tsx");
  for (const key of ["ArrowDown", "ArrowUp", "Home", "End"]) {
    assert.match(shell, new RegExp(`case "${key}":`), `${key} is not handled`);
  }
  // Wrapping round at both ends, and the Manage button counting as before the first.
  assert.match(shell, /return at < 0 \? 0 : \(at \+ 1\) % count;/);
  assert.match(shell, /return at < 0 \? count - 1 : \(at - 1 \+ count\) % count;/);
});

// ---- the Tailscale card ----------------------------------------------------

test("a Mac is shown the Mac app's way in, and Linux keeps its one command", () => {
  const card = source("components/TailnetCard.tsx");
  const mac = card.match(/const MAC_NAME =\s*"([^"]+)"/)?.[1];
  const linux = card.match(/const LINUX_INSTALL =\s*"([^"]+)"/)?.[1];
  assert.ok(mac && linux, "both commands are named constants");
  assert.match(mac, /^\/Applications\/Tailscale\.app\/Contents\/MacOS\/Tailscale up --hostname framefound$/);
  assert.doesNotMatch(mac, /curl|install\.sh|1000/);
  assert.match(linux, /install\.sh \| sh && sudo tailscale up --hostname framefound/);
  assert.match(card, /value=\{mac \? MAC_NAME : LINUX_INSTALL\}/);
  assert.match(card, /Mac App\s+Store/);
});

test("the card knows a Mac from the answer the server really sends", () => {
  // The re-check: the card looked only at `platform`, which the Security
  // page's answer did not carry then, so a Mac still got the Linux
  // installer. It carries `platform` now; a server older than that still
  // names the download page, the Mac one on a Mac (ddns/tailscale_local.py).
  const server = readFileSync(
    join(SRC, "..", "..", "server", "framefound", "ddns", "tailscale_local.py"),
    "utf8",
  );
  const macPage = server.match(/^MAC_DOWNLOAD = "([^"]+)"$/m)?.[1];
  const linuxPage = server.match(/^LINUX_DOWNLOAD = "([^"]+)"$/m)?.[1];
  assert.ok(macPage && linuxPage, "the server names both download pages");

  assert.equal(onMac({ state: "not_installed", download: macPage }), true);
  assert.equal(onMac({ state: "unavailable", download: macPage, message: "x" }), true);
  assert.equal(onMac({ state: "not_installed", download: linuxPage }), false);
  // A server too old to say anything is taken as Linux.
  assert.equal(onMac({ state: "not_installed" }), false);
  // When the answer does say `platform`, that wins.
  assert.equal(onMac({ platform: "macos", download: linuxPage }), true);
  assert.equal(onMac({ platform: "linux", download: macPage }), false);
  // Nothing that merely mentions a Mac counts.
  assert.equal(onMac({ download: "https://example.com/tailscale.com/download/mac" }), false);

  const card = source("components/TailnetCard.tsx");
  assert.match(card, /const mac = onMac\(ts\);/);
  assert.doesNotMatch(card, /\.platform === "macos"/);
});

test("a phone or laptop is sent to the page for every device, not the server's Mac page", () => {
  // On the Mac mini `download` is the macOS page; the connected card's
  // "Get Tailscale for your phone or laptop" sent a phone there.
  const server = readFileSync(
    join(SRC, "..", "..", "server", "framefound", "ddns", "tailscale_local.py"),
    "utf8",
  );
  assert.equal(ANY_DEVICE_DOWNLOAD, server.match(/^LINUX_DOWNLOAD = "([^"]+)"$/m)?.[1]);
  const card = source("components/TailnetCard.tsx");
  assert.match(card, /href=\{ANY_DEVICE_DOWNLOAD\}[^>]*>\s*Get Tailscale for your phone or laptop/);
  // The server's own install still uses the server's page.
  assert.match(card, /<a href=\{ts\.download\}[^>]*>\s*tailscale\.com\/download/);
});

test("a turned-off Mac app is told to connect, not to log in", () => {
  const card = source("components/TailnetCard.tsx");
  assert.match(card, /ts\.state === "stopped"\s*\?\s*"[^"]*choose Connect/);
});
