// Where the reader is, for the chat panel: "this clip" and "these" only mean
// something to Claude if the message says what is on screen.
//
// Two sources, joined when a message is sent:
//
// - the address bar. Every page has one, so every page gives something with
//   no code of its own: the clip of an asset page, the listing, the person,
//   the search that was typed, the tag being browsed.
// - what a page publishes with `setChatContext()`: the things the address
//   does not say — the selection, the folder, the file's name, the queue
//   that is open.
//
// It is read at the moment of sending, never earlier: the reader may have
// ticked three more tiles since the panel was opened.
//
// A library is sent by name (`library`) beside its id wherever one is on
// screen: a request Claude makes from "this library" or "this folder" is
// shown to the operator to confirm, and one that named the library only by
// its id read "library 3f2a…" there. The address and some pages know only
// the id, so the names of the libraries are kept here (`knowLibraries`,
// which the panel calls) and filled in when the context is read.
//
// The panel is mounted once for the whole app and pages come and go beneath
// it, so what a page published must not outlive the page. Two guards: the
// function `setChatContext` returns takes the context back (use it as an
// effect's cleanup), and a context is only used on the page it was
// published from.
//
// Nothing is imported at run time (the one import is types only), so
// `node --test` can load this file as it is (tests/chatContext.test.mjs).

import type { ChatContext } from "./api";

/** The server cuts every string to this; cut here too, so what the panel
 *  shows as "this page" is what is sent. */
export const MAX_CONTEXT_CHARS = 300;

/**
 * The most selected ids sent with a message. The server passes Claude the
 * first 50 and the count of what it received, so sending more than 50 is
 * what makes "the 214 I have selected" come out as 214. Past this many the
 * count Claude is told stops here; a message should stay a message.
 */
export const MAX_SELECTION_SENT = 1000;

const SUFFIX = " · FrameFound";

type Listener = () => void;

let published: { context: ChatContext; path: string } | null = null;
const listeners = new Set<Listener>();
/** Library id → name, as the panel last read the libraries. */
let libraryNames: ReadonlyMap<string, string> = new Map();

function here(): string {
  return typeof window === "undefined" ? "" : window.location.pathname;
}

function changed(): void {
  for (const listener of [...listeners]) listener();
}

/**
 * Say what is on screen, from a page. Replaces whatever was published
 * before; null takes it away.
 *
 * Returns a function that takes this context back if it is still the one
 * published — the cleanup for the effect that published it, so a page that
 * goes leaves nothing behind and a page that arrives is not wiped by the
 * one before it cleaning up late.
 *
 * The context belongs to the page whose address is in the address bar at
 * the time of the call (or `context.path`, when the page gives it).
 */
export function setChatContext(context: ChatContext | null): () => void {
  const mine = context === null ? null : { context, path: context.path ?? here() };
  published = mine;
  changed();
  return () => {
    if (mine !== null && published === mine) {
      published = null;
      changed();
    }
  };
}

/** What a page last published, whichever page that was. For the panel's
 *  "this page" line use `pageContext`, which also checks it is this page's. */
export function getChatContext(): ChatContext | null {
  return published?.context ?? null;
}

/** The libraries by name, so a context that has only a library's id says
 *  which library it is. Tells the listeners when a name is new or changed. */
export function knowLibraries(libraries: ReadonlyArray<{ id: string; name: string }>): void {
  const next = new Map(libraries.filter((one) => one.id && one.name).map((one) => [one.id, one.name]));
  const same =
    next.size === libraryNames.size && [...next].every(([id, name]) => libraryNames.get(id) === name);
  if (same) return;
  libraryNames = next;
  changed();
}

/**
 * The context with its library named: a page's own `library` is kept; one
 * with only `library_id` gets the name it is known by. A library not known
 * (yet) is left as the id alone, as before.
 */
export function withLibraryName(context: ChatContext, names: ReadonlyMap<string, string>): ChatContext {
  if (context.library || !context.library_id) return context;
  const name = names.get(context.library_id);
  return name ? { ...context, library: name } : context;
}

/** Be told when a page publishes or withdraws its context. */
export function subscribeChatContext(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** What the address alone says about what is on screen. */
export function contextFromUrl(pathname: string, search: string): ChatContext {
  const context: ChatContext = {};
  const params = new URLSearchParams(search);
  const param = (name: string): string | undefined => params.get(name)?.trim() || undefined;
  const under = (prefix: string): string | undefined => {
    if (!pathname.startsWith(prefix)) return undefined;
    const rest = pathname.slice(prefix.length).split("/")[0] ?? "";
    try {
      return decodeURIComponent(rest) || undefined;
    } catch {
      return rest || undefined;
    }
  };

  if (pathname === "/") {
    context.query = param("q");
    context.library_id = param("library");
  } else if (pathname === "/browse") {
    context.tag = param("tag");
    context.library_id = param("library");
  } else if (pathname === "/listings") {
    context.folder = param("folder");
    context.library_id = param("library");
  } else if (under("/assets/")) {
    context.asset_id = under("/assets/");
    const at = Number(param("t"));
    if (Number.isFinite(at) && at > 0) context.at_s = at;
  } else if (under("/edit/")) {
    context.asset_id = under("/edit/");
    context.listing_id = param("listing");
  } else if (under("/listings/")) {
    context.listing_id = under("/listings/");
  } else if (under("/people/")) {
    context.person_id = under("/people/");
  }
  return tidy(context);
}

/**
 * The page context to send with a message written now: the address, what
 * the address says, and on top of both what the page published — if it was
 * this page that published it. Always has `path`.
 *
 * `set` is what was published and from where, and `names` the libraries'
 * names; both default to the module's own and are parameters so this can be
 * tested without a browser.
 */
export function pageContext(
  at: { pathname: string; search?: string; title?: string },
  set: { context: ChatContext; path: string } | null = published,
  names: ReadonlyMap<string, string> = libraryNames,
): ChatContext {
  const title = pageName(at.title ?? "");
  // A key the page left empty does not wipe what the address says: a
  // page publishing `{library_id: undefined}` before its libraries have
  // loaded still has the library that is in `?library=`.
  const mine =
    set !== null && set.path === at.pathname
      ? Object.fromEntries(
          Object.entries(set.context).filter(
            ([, value]) => value !== undefined && value !== null && value !== "",
          ),
        )
      : {};
  return tidy(
    withLibraryName(
      {
        ...(title ? { title } : {}),
        ...contextFromUrl(at.pathname, at.search ?? ""),
        ...mine,
        path: at.pathname,
      },
      names,
    ),
  );
}

/** A tab title without the app's name: "Tags · FrameFound" is "Tags", and
 *  a page that set no title has none. */
function pageName(title: string): string {
  const name = title.endsWith(SUFFIX) ? title.slice(0, -SUFFIX.length) : title;
  return name === "FrameFound" ? "" : name.trim();
}

/**
 * A context as it will be sent: empty values dropped, strings cut to what
 * the server keeps, the selection capped. `selection_count` is how many
 * were selected before the cap, for the panel's own "this page" line; the
 * server counts for itself.
 */
export function tidy(context: ChatContext): ChatContext {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(context)) {
    if (key === "selection" || key === "selection_count") continue;
    if (typeof value === "string") {
      const text = value.trim();
      if (text !== "") out[key] = text.slice(0, MAX_CONTEXT_CHARS);
    } else if (typeof value === "number" && Number.isFinite(value)) {
      out[key] = value;
    }
  }
  const chosen = Array.isArray(context.selection)
    ? context.selection.filter((id) => typeof id === "string" && id !== "")
    : [];
  if (chosen.length > 0) {
    out.selection = chosen.slice(0, MAX_SELECTION_SENT);
    out.selection_count = chosen.length;
  }
  return out as ChatContext;
}
