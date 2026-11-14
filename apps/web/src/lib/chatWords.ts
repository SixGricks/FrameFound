// The chat panel's words and sums (components/ChatPanel.tsx), kept apart from
// the panel so they can be tested.
//
// The server speaks in tool names, states and reasons: `search_media`,
// `interrupted`, `awaiting_confirmation`. The person at the panel runs a
// photography business and should never read one of those. Everything that
// turns the first into a sentence for the second is here.
//
// Nothing is imported at run time (the imports are types only), so
// `node --test` can load this file as it is (tests/chatWords.test.mjs).

import type { ChatContext, ChatState, ChatStatus } from "./api";
import type { ChatView, TurnEnd, ViewChip } from "./chatReducer";

/** The longest message the server takes; it answers 400 past this. */
export const MAX_MESSAGE_CHARS = 20_000;

/** How much of an answer is read aloud when it finishes. A screen reader
 *  cannot be interrupted politely, and the whole answer is in the
 *  conversation to be read at the listener's own pace. */
export const MAX_SPOKEN_CHARS = 600;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

// --- money and models --------------------------------------------------------

/** A sum of money. A conversation's first answers cost fractions of a cent,
 *  and "$0.00" for something that was not free would be a small lie. */
export function money(usd: number | null | undefined): string {
  if (usd === null || usd === undefined || !Number.isFinite(usd) || usd <= 0) return "$0.00";
  if (usd < 0.01) return "under $0.01";
  return `$${usd.toFixed(2)}`;
}

/**
 * What has been spent: by this conversation, and this month by everything
 * that uses Claude, against the one budget they share. `over` is true when
 * the month's spend has reached the budget, which is when the next request
 * will be turned away.
 */
export function spendWords(
  spent: number,
  status: Pick<ChatStatus, "budget_usd" | "spent_this_month_usd"> | null,
): { here: string; month: string | null; over: boolean } {
  const here = `${money(spent)} this conversation`;
  if (status === null) return { here, month: null, over: false };
  const budget = status.budget_usd;
  const month = `${money(status.spent_this_month_usd)} of ${money(budget)} this month`;
  return { here, month, over: status.spent_this_month_usd >= budget };
}

/** "claude-opus-5-5" as people say it: "Claude Opus 5.5". An id that is not
 *  of that shape is shown as it is. */
export function modelName(id: string): string {
  const found = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/.exec(id.trim());
  if (!found) return id;
  const family = found[1] ?? "";
  const version = found[3] ? `${found[2]}.${found[3]}` : (found[2] ?? "");
  return `Claude ${family.charAt(0).toUpperCase()}${family.slice(1)} ${version}`;
}

// --- a conversation in the list -----------------------------------------------

/** A conversation is named by the first line typed in it, so one nothing
 *  has been said in has no name of its own. */
export function titleOf(title: string | null | undefined): string {
  return (title ?? "").trim() || "New conversation";
}

/** What a conversation in the list is doing, when it is doing something. */
export function stateWords(state: ChatState): string | null {
  if (state === "running") return "Claude is answering";
  if (state === "awaiting_confirmation") return "waiting for your yes or no";
  return null;
}

// --- where you are -------------------------------------------------------------

function clock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

// The app's sections as the menu names them, for a page that has not given
// itself a title (several never do): "/tags" is "Tags" to the reader, and
// "/health" is the page the menu calls System.
const SECTIONS: Record<string, string> = {
  "": "Search",
  assets: "An item",
  edit: "The editor",
  health: "System",
};

/** A page's name when it has none of its own: its section of the app. */
export function sectionName(path: string): string {
  const first = path.split("/")[1] ?? "";
  const known = Object.hasOwn(SECTIONS, first) ? SECTIONS[first] : undefined;
  if (known) return known;
  return first === "" ? "" : `${first.charAt(0).toUpperCase()}${first.slice(1)}`;
}

/** Pages put the detail Claude is given (counts, filters, a place's
 *  coordinates) after this in the title they publish; the "this page" line
 *  shows only what comes before it. */
export const TITLE_DETAIL = " — ";
const MAX_TITLE_SHOWN = 60;

/** A published page title as the "this page" line shows it: the part
 *  before the detail, and never longer than a short line. */
export function shownTitle(title: string): string {
  const head = (title.split(TITLE_DETAIL)[0] ?? "").trim();
  return head.length > MAX_TITLE_SHOWN ? `${head.slice(0, MAX_TITLE_SHOWN - 1)}…` : head;
}

/**
 * The page context as the "this page" line shows it, in the order a person
 * would say it: the page, then what on it. Empty when there is nothing to
 * say. The ids themselves are never shown; they mean nothing to the reader.
 */
export function contextWords(context: ChatContext | null | undefined): string[] {
  if (!context) return [];
  const words: string[] = [];
  const page =
    (context.title ? shownTitle(context.title) : "") ||
    (context.path ? sectionName(context.path) : "");
  if (page) words.push(page);
  if (context.library) words.push(`library “${context.library}”`);
  if (context.folder) words.push(`folder “${context.folder}”`);
  if (context.filename) words.push(`“${context.filename}”`);
  else if (context.asset_id) words.push("the item on screen");
  if (typeof context.at_s === "number" && context.at_s > 0) words.push(`at ${clock(context.at_s)}`);
  if (context.listing_id && !context.asset_id) words.push("this listing");
  if (context.person_id) words.push("this person");
  if (context.tag) words.push(`tag “${context.tag}”`);
  if (context.query) words.push(`search “${context.query}”`);
  const chosen = context.selection_count ?? context.selection?.length ?? 0;
  if (chosen > 0) words.push(`${chosen.toLocaleString("en-US")} selected`);
  return words;
}

// --- a tool call -----------------------------------------------------------------

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : typeof value === "number" ? String(value) : "";
}

function quoted(value: unknown): string {
  const said = text(value);
  return said === "" ? "" : `“${said.length > 80 ? `${said.slice(0, 79)}…` : said}”`;
}

function listed(value: unknown): string {
  if (!Array.isArray(value)) return "";
  const names = value.map(text).filter((name) => name !== "");
  if (names.length === 0) return "";
  const shown = names.slice(0, 4).join(", ");
  return names.length > 4 ? `${shown} and ${names.length - 4} more` : shown;
}

/** An item as the reader can recognise it. The id's first eight characters
 *  are what Claude's answers call it by, so they match what is on screen. */
function item(value: unknown): string {
  const id = text(value);
  return id === "" ? "an item" : `item ${id.slice(0, 8)}`;
}

/** [doing, done, to do] for each tool: "Searching for", "Searched for",
 *  "search for". The last follows "Could not". */
type Verb = [string, string, string];

function describe(name: string, input: Record<string, unknown>): { verb: Verb; what: string } {
  switch (name) {
    case "overview":
      return { verb: ["Looking over", "Looked over", "look over"], what: "the catalogue" };
    case "list_tags":
      return {
        verb: ["Listing", "Listed", "list"],
        what: input.query ? `the tags matching ${quoted(input.query)}` : "the tags",
      };
    case "find_media":
      return {
        verb: ["Finding", "Found", "find"],
        what: listed(input.tags) ? `media tagged ${listed(input.tags)}` : "media",
      };
    case "search_media":
      return { verb: ["Searching for", "Searched for", "search for"], what: quoted(input.query) || "media" };
    case "get_media":
      return { verb: ["Reading about", "Read about", "read about"], what: item(input.id) };
    case "view_media":
      return {
        verb: ["Looking at", "Looked at", "look at"],
        what:
          typeof input.ts_ms === "number"
            ? `${item(input.id)} at ${clock(input.ts_ms / 1000)}`
            : item(input.id),
      };
    case "review_queue":
      return { verb: ["Reading", "Read", "read"], what: "the tag suggestions waiting for review" };
    case "propose_tags":
      return {
        verb: ["Suggesting", "Suggested", "suggest"],
        what: `${listed(input.tags) ? `the tags ${listed(input.tags)}` : "tags"} for ${item(input.id)}`,
      };
    case "api_review_status":
      return { verb: ["Checking", "Checked", "check"], what: "the paid tag review" };
    case "find_shots":
      return {
        verb: ["Finding", "Found", "find"],
        what: input.folder ? `shots in ${quoted(input.folder)}` : "shots",
      };
    case "contact_sheet":
      return { verb: ["Making", "Made", "make"], what: "a contact sheet" };
    case "recent_cuts":
      return { verb: ["Reading", "Read", "read"], what: "the recent cuts" };
    case "path_profiles":
      return { verb: ["Checking", "Checked", "check"], what: "where the files are on your computers" };
    case "get_media_files":
      return { verb: ["Finding", "Found", "find"], what: `the files of ${item(input.id)}` };
    case "get_transcript":
      return { verb: ["Reading", "Read", "read"], what: `what is said in ${item(input.id)}` };
    case "get_scenes":
      return { verb: ["Reading", "Read", "read"], what: `the scenes of ${item(input.id)}` };
    case "build_rough_cut":
      return {
        verb: ["Building", "Built", "build"],
        what: input.title ? `the rough cut ${quoted(input.title)}` : "a rough cut",
      };
    case "build_selects":
      return {
        verb: ["Building", "Built", "build"],
        what: input.title ? `the selects ${quoted(input.title)}` : "selects",
      };
    default:
      // A tool this page has not been taught words for (a later phase's):
      // its own name, made readable, is better than nothing.
      return { verb: ["Running", "Ran", "run"], what: quoted(name.replace(/_/g, " ")) || "a tool" };
  }
}

export interface ChipWords {
  /** The one line on the chip. */
  text: string;
  /** How it stands: still going, worked, did not, waiting on the reader,
   *  or set aside. Drives the colour and the mark in front. */
  tone: "busy" | "ok" | "bad" | "ask" | "off";
}

/**
 * A tool call in plain words: what was looked up or done, and whether it
 * worked. A call that changes something (`ask` is set) is said in the
 * server's own sentence for it, which is the one the reader said yes or no
 * to.
 */
export function chipWords(chip: ViewChip): ChipWords {
  if (chip.ask) {
    const ask = chip.ask.replace(/[.\s]+$/, "");
    switch (chip.state) {
      case "pending":
        return { text: `${ask}: waiting for your yes or no`, tone: "ask" };
      case "preparing":
      case "running":
        return { text: `${ask}…`, tone: "busy" };
      case "approved":
      case "ran":
        return { text: `Done: ${ask}`, tone: "ok" };
      case "declined":
        return { text: `Not done, you said no: ${ask}`, tone: "off" };
      case "failed":
        return { text: `Did not work: ${ask}`, tone: "bad" };
      default:
        return { text: `Did not finish: ${ask}`, tone: "bad" };
    }
  }
  const { verb, what } = describe(chip.name, chip.input ?? {});
  switch (chip.state) {
    case "preparing":
      // Claude is still writing the call, so what it is for is not known yet.
      return { text: `${verb[0]}…`, tone: "busy" };
    case "running":
    case "pending":
      return { text: `${verb[0]} ${what}…`, tone: "busy" };
    case "ran":
    case "approved":
      return { text: `${verb[1]} ${what}`, tone: "ok" };
    case "declined":
      return { text: `Not done: ${verb[2]} ${what}`, tone: "off" };
    case "failed":
      return { text: `Could not ${verb[2]} ${what}`, tone: "bad" };
    default:
      return { text: `Did not finish: ${verb[2]} ${what}`, tone: "bad" };
  }
}

/**
 * What a call was asked to do, as short lines for "the details": each
 * argument's name and value, with long lists and long text cut so a request
 * to decide two hundred tags stays a card and not a page.
 */
export function inputLines(input: Record<string, unknown> | null): { name: string; value: string }[] {
  if (!input) return [];
  const lines: { name: string; value: string }[] = [];
  for (const [key, value] of Object.entries(input)) {
    if (value === null || value === undefined || value === "") continue;
    let shown: string;
    if (Array.isArray(value)) {
      if (value.length === 0) continue;
      const first = value.slice(0, 6).map((one) => (typeof one === "string" ? one : JSON.stringify(one)));
      shown = first.join(", ") + (value.length > 6 ? ` and ${value.length - 6} more` : "");
    } else if (typeof value === "boolean") {
      shown = value ? "yes" : "no";
    } else if (typeof value === "object") {
      shown = JSON.stringify(value);
    } else {
      shown = String(value);
    }
    lines.push({
      name: key.replace(/_/g, " "),
      value: shown.length > 240 ? `${shown.slice(0, 239)}…` : shown,
    });
  }
  return lines;
}

/** Catalogue items a request would change, under what it would do to them. */
export interface AskItems {
  /** "Confirm the tag on", "Reject the tag on", "Review". */
  name: string;
  /** As Claude wrote them: whole ids, or the short ids it reads them by. */
  ids: string[];
  /** The whole ids among them, which the app's viewer can open. */
  openable: string[];
}

/** What a request for a yes or no would touch, for the card that asks. */
export interface AskDetails {
  /** Anything more to say, each as a name and a value in words. */
  lines: { name: string; value: string }[];
  /** The items, to be looked at before answering. */
  items: AskItems[];
}

function ids(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map(text).filter((id) => id !== "");
}

function group(name: string, value: unknown): AskItems[] {
  const all = ids(value);
  return all.length === 0 ? [] : [{ name, ids: all, openable: all.filter((id) => UUID.test(id)) }];
}

/**
 * The details under the server's sentence on a confirm card, said for a
 * person: the items a tag decision or a paid review would touch, so they can
 * be opened and looked at before saying yes, and nothing that sentence
 * already says. Parameter names and 36-character ids mean nothing to the
 * person answering. A request this page has no words for falls back to its
 * arguments as written (`inputLines`).
 */
export function askDetails(name: string, input: Record<string, unknown> | null): AskDetails {
  const given = input ?? {};
  switch (name) {
    case "decide_tag":
      return {
        lines: [],
        items: group(given.accept === true ? "Confirm the tag on" : "Reject the tag on", [given.id]),
      };
    case "decide_tags":
      return {
        lines: [],
        items: [...group("Confirm the tag on", given.accept), ...group("Reject the tag on", given.reject)],
      };
    case "start_api_review": {
      const limit = typeof given.limit === "number" && given.limit > 0 ? Math.floor(given.limit) : 0;
      return {
        lines:
          limit > 0 && ids(given.asset_ids).length === 0
            ? [{ name: "At most", value: `${limit.toLocaleString("en-US")} items, the newest without tags` }]
            : [],
        items: group("Review", given.asset_ids),
      };
    }
    default:
      return { lines: inputLines(input), items: [] };
  }
}

/** An item on a confirm card, as the reader can tell it from the next. */
export function itemName(id: string): string {
  return item(id);
}

/**
 * The catalogue item a picture in a chip is of, when that can be told: the
 * viewer the rest of the app uses can then open it, with its clip playable
 * and its full page one click away. Only `view_media` with a whole id says
 * so. A short id (the eight characters Claude usually uses) and a contact
 * sheet (many items in one picture) do not, and the picture is then simply
 * shown larger.
 */
export function pictureAsset(chip: Pick<ViewChip, "name" | "input">): { assetId: string; startAt?: number } | null {
  if (chip.name !== "view_media" || !chip.input) return null;
  const id = text(chip.input.id);
  if (!UUID.test(id)) return null;
  const at = chip.input.ts_ms;
  return typeof at === "number" && at > 0 ? { assetId: id, startAt: at / 1000 } : { assetId: id };
}

// --- a turn ----------------------------------------------------------------------

/**
 * The line under the conversation while Claude is at work: what it is
 * doing, and when something is wrong with hearing it, that the work itself
 * goes on. Null when nothing is running.
 */
export function workingWords(
  view: Pick<ChatView, "running" | "stranded" | "thinking" | "messages">,
  link: "idle" | "live" | "reconnecting" | "watching",
  stopping: boolean,
): string | null {
  if (!view.running) return null;
  if (view.stranded) {
    return (
      "The last answer here was cut off before it finished. FrameFound frees the " +
      "conversation by itself, within five minutes at most; this panel is checking."
    );
  }
  if (stopping) return "Stopping… a step that had already started is left to finish.";
  if (link === "reconnecting") {
    return "The connection dropped. Reconnecting… Claude is still working meanwhile.";
  }
  if (link === "watching") return "Claude is still working. Waiting for it to finish…";
  if (view.thinking) return "Thinking…";
  const last = view.messages[view.messages.length - 1];
  if (!last || last.role === "user") return last?.sending ? "Sending…" : "Waiting for Claude…";
  if (last.tools.some((chip) => chip.state === "running" || chip.state === "preparing")) {
    return "Working…";
  }
  return "Writing…";
}

export interface EndWords {
  text: string;
  /** `quiet` is information (Stopped.); `bad` is something that went wrong
   *  or was refused and wants the reader's attention. */
  tone: "quiet" | "bad";
}

/**
 * How the last turn ended, when that needs saying. An ordinary ending and a
 * question waiting for a yes say nothing here (the answer and the card speak
 * for themselves); a stop is a quiet line; everything else is the server's
 * own sentence, because it knows which of a dozen things happened.
 */
export function endWords(ended: TurnEnd | null): EndWords | null {
  if (ended === null) return null;
  if (ended.reason === "end" || ended.reason === "awaiting_confirmation") return null;
  if (ended.reason === "stopped") return { text: ended.message || "Stopped.", tone: "quiet" };
  return {
    text: ended.message || "Claude could not finish that answer. Ask again.",
    tone: "bad",
  };
}

/**
 * Why a message cannot be sent just now, as a sentence to show under the
 * box; null when it can. The box itself stays open through all of these:
 * typing the next message while Claude answers is ordinary.
 *
 * (Whether the box is offered at all is `gate`, below.)
 */
export function cannotSend(state: {
  view: Pick<ChatView, "running" | "stranded">;
  opening: boolean;
  draft: string;
}): string | null {
  if (state.opening) return "Opening the conversation…";
  if (state.view.stranded) return "You can send again once the conversation is free.";
  if (state.view.running) return "You can type meanwhile, and send when Claude has finished.";
  const length = state.draft.trim().length;
  if (length > MAX_MESSAGE_CHARS) {
    return (
      `That message is too long (${length.toLocaleString("en-US")} characters; ` +
      `the limit is ${MAX_MESSAGE_CHARS.toLocaleString("en-US")}).`
    );
  }
  return null;
}

export interface Gate {
  /** Shown in place of the message box. */
  sentence: string;
  /** Where an administrator can put it right, when there is such a place. */
  fix: { href: string; label: string } | null;
  /** Something is wrong with the server's set-up (as opposed to this
   *  account simply not being allowed to write). */
  fault: boolean;
}

/**
 * Whether the message box is offered, and what is said instead when it is
 * not. Null means: offer it.
 *
 * This has to be asked before sending, not learned from a failed send:
 * with no key the server still takes a message, stores it and only then
 * ends the turn with an error.
 */
export function gate(status: ChatStatus | null, isAdmin: boolean): Gate | null {
  if (status === null) return null;
  if (status.problem) {
    // A missing key and a switched-off Claude are both settled on the
    // Security page, by an administrator. Missing packages are not settled
    // in the app at all, so no link is offered for those.
    const fixable = status.available && (!status.configured || !status.enabled);
    return {
      sentence: status.problem,
      fix:
        fixable && isAdmin
          ? { href: "/security#services", label: "Open the Claude settings on Security" }
          : null,
      fault: true,
    };
  }
  if (!status.can_chat) {
    return {
      sentence:
        "This account is read-only: you can read your conversations here, but not " +
        "send a message or answer a request.",
      fix: null,
      fault: false,
    };
  }
  return null;
}

/** What is read aloud when an answer has finished: the answer, once, and
 *  not too much of it. `said` is the answer with its formatting removed. */
export function spoken(said: string): string {
  const words = said.replace(/\s+/g, " ").trim();
  if (words === "") return "Claude has finished.";
  if (words.length <= MAX_SPOKEN_CHARS) return `Claude: ${words}`;
  return `Claude: ${words.slice(0, MAX_SPOKEN_CHARS)}… The rest is in the conversation.`;
}
