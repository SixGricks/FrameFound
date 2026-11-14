// Going to the section a link names (/security#services), once the page has
// something to go to.
//
// The page shows only "Loading…" until its data arrives, so when the
// browser (a fresh load) or Next (a link from another page) looked for
// #services there was nothing by that id, and the page stayed at the top:
// the chat panel's "Open the Claude settings on Security" landed 2,200 px
// above the Claude settings. The page calls `jumpTo` itself once it has
// drawn its sections.
//
// Nothing is imported, so `node --test` loads this file as it is
// (tests/systemShell.test.mjs).

/** The id a location's hash names, or null for none ("", "#", a bad escape). */
export function sectionFor(hash: string): string | null {
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  if (!raw) return null;
  try {
    return decodeURIComponent(raw) || null;
  } catch {
    return null;
  }
}

/** As much of an element as going to it needs; a real element is one. */
export interface Place {
  tagName: string;
  open?: boolean;
  parentElement: Place | null;
  hasAttribute(name: string): boolean;
  setAttribute(name: string, value: string): void;
  scrollIntoView(options?: ScrollIntoViewOptions): void;
  focus(options?: FocusOptions): void;
}

/**
 * Scroll to the section `hash` names and give it the focus, so the next Tab
 * starts there and a screen reader reads it. A section inside a closed
 * disclosure ("Advanced") is opened first, or there would be nothing to
 * see. Returns the element gone to, or null when the hash names nothing on
 * the page.
 */
export function jumpTo(hash: string, find: (id: string) => Place | null): Place | null {
  const id = sectionFor(hash);
  const target = id ? find(id) : null;
  if (!target) return null;
  for (let at: Place | null = target; at; at = at.parentElement) {
    if (at.tagName === "DETAILS" && !at.open) at.open = true;
  }
  // A section is not focusable by itself; -1 keeps it out of the Tab order.
  if (!target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
  target.scrollIntoView({ block: "start" });
  target.focus({ preventScroll: true });
  return target;
}

/**
 * Keep a section that was just gone to in place while the cards above it
 * finish loading (each asks the server for its own data, and grows when it
 * answers), until the person scrolls, clicks or types, or `ms` has passed.
 * Returns the function that stops it.
 */
export function holdInView(target: Place, ms = 3000): () => void {
  if (typeof ResizeObserver === "undefined") return () => undefined;
  const page = document.body;
  const watcher = new ResizeObserver(() => target.scrollIntoView({ block: "start" }));
  const events = ["wheel", "touchstart", "keydown", "pointerdown"] as const;
  let stopped = false;
  const stop = () => {
    if (stopped) return;
    stopped = true;
    watcher.disconnect();
    clearTimeout(timer);
    for (const name of events) window.removeEventListener(name, stop, true);
  };
  const timer = setTimeout(stop, ms);
  for (const name of events) window.addEventListener(name, stop, { capture: true, passive: true });
  watcher.observe(page);
  return stop;
}
