// What the chat panel draws, worked out from what the server says.
//
// The server says a conversation two ways: stored (GET the conversation:
// its messages, each with the tools Claude asked for) and live (the events
// of a turn as it happens). The panel draws one thing, a `ChatView`, and
// this file is the only place that knows how either becomes it:
//
//   viewOf(stored conversation)   the view after a load or a reload
//   reduce(view, event)           the view after one more event
//
// One reducer serves a turn read live, a turn picked up after the
// connection dropped, and a turn replayed from its start after a reload.
// Everything here is a pure function of its arguments.
//
// Nothing is imported at run time (the one import is types only), so
// `node --test` can load this file as it is (tests/chatReducer.test.mjs).

import type {
  ChatArtifact,
  ChatChip,
  ChatContext,
  ChatConversation,
  ChatEvent,
  ChatEventData,
  ChatPending,
  ChatPicture,
} from "./api";

/**
 * A tool call as drawn. The stored states, plus two only the panel has:
 * `preparing` (Claude is still writing the call) and `interrupted` (the
 * turn ended, or was lost, before the call got its answer — it will never
 * finish, so it must not be drawn as still working).
 */
export interface ViewChip {
  id: string;
  name: string;
  /** Null while Claude is still writing the call. */
  input: Record<string, unknown> | null;
  state: ChatChip["state"] | "preparing" | "interrupted";
  needs_confirm: boolean;
  ok: boolean;
  summary: string;
  ask: string | null;
  images: ChatPicture[];
  artifact: ChatArtifact | null;
}

export interface ViewMessage {
  /** Stable for as long as the message is on screen (a React key). */
  key: string;
  /** The stored message's id and seq; null for one only seen live so far. */
  id: string | null;
  seq: number | null;
  role: "user" | "assistant";
  text: string;
  /** The page context that went with a typed message. */
  context: ChatContext | null;
  tools: ViewChip[];
  /** Typed here and not yet acknowledged by the server. */
  sending: boolean;
}

/** How a turn ended. `kind` is the `error` event's, when there was one. */
export interface TurnEnd {
  /** end | awaiting_confirmation | stopped | budget | steps | refusal | max_tokens | error
   *  ("stopped" also comes from the stored `last_turn` after a reload) */
  reason: string;
  /** The server's sentence; null for `end` and `awaiting_confirmation`. */
  message: string | null;
  kind: string | null;
}

export interface ChatView {
  /** The conversation; null for a new one nothing has been sent in yet. */
  id: string | null;
  title: string;
  /** "" for a new conversation: the server's current choice applies. */
  model: string;
  pictures: boolean;
  messages: ViewMessage[];
  /** Calls waiting for a yes or no. */
  pending: ChatPending[];
  /** A turn is under way. */
  running: boolean;
  /** The running turn has said `start`: the server has taken the message or
   *  the answer, and there is a turn there to stop. False from the moment a
   *  message is sent until then. */
  begun: boolean;
  /** The `seq` the turn this view is drawing began from (its `start`, or
   *  the stored `run.seq`); null when not known. Picking a turn up part-way
   *  is only right if the server's turn is still this one. */
  turn: number | null;
  /** The server says a turn is under way but holds none to follow: it was
   *  lost with a restart, and the conversation frees itself in a while. */
  stranded: boolean;
  thinking: boolean;
  /** How many events of the running turn are in this view: what to ask
   *  for as `after` when picking the turn up again. */
  events: number;
  /** How the last turn ended; null while one runs and before the first. */
  ended: TurnEnd | null;
  /** What this conversation has cost so far. */
  spent_usd: number;

  // Bookkeeping for the reducer; nothing to draw.
  /** Key of the answer being written, if one is. */
  writing: string | null;
  /** Key of the answer the last `usage` closed, until anything follows it. */
  closed: string | null;
  /** The next text starts a new paragraph (a tool call or a stretch of
   *  thinking came between two pieces of text in one answer). */
  gap: boolean;
  /** How many keys this view has made up. */
  minted: number;
}

/** A conversation nothing has been said in. */
export function emptyView(pictures = true): ChatView {
  return {
    id: null,
    title: "",
    model: "",
    pictures,
    messages: [],
    pending: [],
    running: false,
    begun: false,
    turn: null,
    stranded: false,
    thinking: false,
    events: 0,
    ended: null,
    spent_usd: 0,
    writing: null,
    closed: null,
    gap: false,
    minted: 0,
  };
}

/**
 * The view of a stored conversation.
 *
 * When the server still holds a turn that is running (`run.active`), only
 * the messages up to the one the turn began from are drawn, and the caller
 * then feeds the turn's events, from the first, through `reduce`: the
 * stored rows of a turn in progress and its events say the same things, and
 * drawing both would say them twice.
 *
 * Otherwise the stored conversation is the whole truth. A call still
 * marked `running` in it has nothing running it — the turn was interrupted
 * — and is drawn as interrupted, not as a spinner that never stops.
 */
export function viewOf(stored: ChatConversation): ChatView {
  const following = stored.run !== null && stored.run.active;
  const upTo = following && stored.run !== null ? stored.run.seq : null;
  const running = following || stored.state === "running";
  const messages: ViewMessage[] = [];
  const drawn = new Set<string>();
  for (const message of stored.messages) {
    if (upTo !== null && message.seq > upTo) continue;
    const tools: ViewChip[] = (message.tools ?? []).map((chip) => {
      drawn.add(chip.id);
      return !following && chip.state === "running"
        ? { ...chip, state: "interrupted", ok: false }
        : { ...chip };
    });
    messages.push({
      key: message.id,
      id: message.id,
      seq: message.seq,
      role: message.role,
      text: message.text,
      context: message.context ?? null,
      tools,
      sending: false,
    });
  }
  return {
    ...emptyView(stored.pictures),
    id: stored.id,
    title: stored.title,
    model: stored.model,
    messages,
    // A waiting call whose message is not drawn yet comes back with the
    // turn's own `confirm` event.
    pending: (stored.pending ?? []).filter((call) => !following || drawn.has(call.id)),
    running,
    begun: upTo !== null,
    turn: upTo,
    stranded: running && !following,
    // While a turn runs, `last_turn` is the one before it.
    ended:
      !running && stored.last_turn
        ? { reason: stored.last_turn.reason, message: stored.last_turn.message, kind: null }
        : null,
    spent_usd: stored.spent_usd,
  };
}

/** The view with a message the reader has just sent, before the server has
 *  said anything about it. `start` makes it a stored one. */
export function withSent(view: ChatView, text: string, context: ChatContext | null): ChatView {
  const minted = view.minted + 1;
  const message: ViewMessage = {
    key: `sent-${minted}`,
    id: null,
    seq: null,
    role: "user",
    text,
    context,
    tools: [],
    sending: true,
  };
  return {
    ...expecting(view),
    // What the server will call it: the first line typed.
    title: view.title || (text.split("\n")[0] ?? "").slice(0, 80),
    messages: [...view.messages, message],
    minted,
  };
}

/** The view with that message taken back: the server would not take it. */
export function withoutSent(view: ChatView): ChatView {
  return {
    ...view,
    messages: view.messages.filter((message) => !message.sending),
    running: false,
    thinking: false,
  };
}

/** The view just before a turn's first event: nothing of it counted yet. */
export function expecting(view: ChatView): ChatView {
  return {
    ...view,
    running: true,
    begun: false,
    turn: null,
    stranded: false,
    thinking: false,
    events: 0,
    ended: null,
    writing: null,
    closed: null,
    gap: false,
  };
}

/**
 * The view after one more event of a turn.
 *
 * An event this view already has (its number is below `view.events`) changes
 * nothing, so a turn picked up from a little too early is not drawn twice.
 */
export function reduce(view: ChatView, event: ChatEvent): ChatView {
  if (event.index !== null && event.index < view.events) return view;
  const next = apply(view, event);
  return { ...next, events: event.index !== null ? event.index + 1 : view.events + 1 };
}

function apply(view: ChatView, event: ChatEvent): ChatView {
  switch (event.event) {
    case "start":
      return started(view, event.data);
    case "thinking": {
      const on = event.data.on === true;
      return { ...view, thinking: on, gap: view.gap || (on && view.writing !== null) };
    }
    case "text":
      return said(view, event.data);
    case "tool":
      return asked(view, event.data);
    case "tool_result":
      return answered(view, event.data);
    case "confirm":
      return waiting(view, event.data);
    case "usage":
      // The request is over, so the answer it was writing is too: whatever
      // Claude says next is a new answer. (Not "an answer was stored": a
      // request that failed part-way is billed and says `usage` as well.)
      return {
        ...view,
        writing: null,
        closed: view.writing,
        gap: false,
        thinking: false,
        spent_usd:
          typeof event.data.conversation_usd === "number"
            ? event.data.conversation_usd
            : view.spent_usd,
      };
    case "error":
      // `done` follows with the same sentence and the real reason.
      return {
        ...view,
        ended: { reason: "error", message: String(event.data.message ?? ""), kind: event.data.kind },
      };
    case "done":
      return finished(view, event.data);
    default:
      return view;
  }
}

function started(view: ChatView, data: ChatEventData["start"]): ChatView {
  const from = typeof data.seq === "number" ? data.seq : null;
  // After a reload the stored messages may already include what this turn
  // has written so far; the events are about to say it again.
  let messages = view.messages.filter(
    (message) => from === null || message.seq === null || message.seq <= from,
  );
  if (messages.length === view.messages.length) messages = view.messages;
  let pending = view.pending;
  if (data.message_id !== null && data.message_id !== undefined) {
    // The turn began from a typed message: the one sent from here, now stored.
    const stored = data.message_id;
    messages = messages.map((message) =>
      message.sending ? { ...message, sending: false, id: stored, seq: from } : message,
    );
    // And typing instead of answering is a "no" to whatever was waiting.
    // The server records that without an event of its own.
    messages = eachChip(messages, (chip) =>
      chip.state === "pending" ? { ...chip, state: "declined", ok: false } : chip,
    );
    pending = [];
  }
  return {
    ...view,
    id: view.id ?? data.conversation_id,
    model: data.model || view.model,
    pictures: typeof data.pictures === "boolean" ? data.pictures : view.pictures,
    messages,
    pending,
    running: true,
    begun: true,
    turn: from,
    stranded: false,
    thinking: false,
    ended: null,
    writing: null,
    closed: null,
    gap: false,
  };
}

function said(view: ChatView, data: ChatEventData["text"]): ChatView {
  const delta = typeof data.delta === "string" ? data.delta : "";
  if (delta === "") return view;
  const { view: open, at } = answering(view);
  const messages = open.messages.map((message, index) =>
    index === at
      ? { ...message, text: message.text + (open.gap && message.text !== "" ? "\n\n" : "") + delta }
      : message,
  );
  return { ...open, messages, gap: false, closed: null };
}

function asked(view: ChatView, data: ChatEventData["tool"]): ChatView {
  const id = String(data.id);
  const running = data.state === "running";
  const patch = (chip: ViewChip): ViewChip =>
    running
      ? { ...chip, name: data.name || chip.name, input: data.input ?? chip.input, state: "running" }
      : chip;
  // A call that is running is no longer waiting to be allowed.
  const pending = running ? view.pending.filter((call) => call.id !== id) : view.pending;
  if (has(view, id)) {
    return { ...view, messages: eachChip(view.messages, patch, id), pending, closed: null };
  }
  const { view: open, at } = answering(view);
  const chip = patch({
    id,
    name: String(data.name ?? ""),
    input: null,
    state: "preparing",
    needs_confirm: false,
    ok: false,
    summary: "",
    ask: null,
    images: [],
    artifact: null,
  });
  return { ...withChip(open, at, chip), pending, gap: true, closed: null };
}

function answered(view: ChatView, data: ChatEventData["tool_result"]): ChatView {
  const id = String(data.id);
  const patch = (chip: ViewChip): ViewChip => ({
    ...chip,
    name: data.name || chip.name,
    state: data.state,
    ok: data.ok === true,
    summary: typeof data.summary === "string" ? data.summary : "",
    images: Array.isArray(data.images) ? data.images : [],
    artifact: data.artifact ?? null,
  });
  const pending = view.pending.filter((call) => call.id !== id);
  if (has(view, id)) {
    return { ...view, messages: eachChip(view.messages, patch, id), pending, closed: null };
  }
  // An answer to a call this view never saw being asked: shown all the
  // same, since it happened.
  const { view: open, at } = answering(view);
  const chip = patch({
    id,
    name: String(data.name ?? ""),
    input: null,
    state: "running",
    needs_confirm: false,
    ok: false,
    summary: "",
    ask: null,
    images: [],
    artifact: null,
  });
  return { ...withChip(open, at, chip), pending, closed: null };
}

function waiting(view: ChatView, data: ChatEventData["confirm"]): ChatView {
  const id = String(data.id);
  const input = data.input ?? {};
  const patch = (chip: ViewChip): ViewChip => ({
    ...chip,
    name: data.name || chip.name,
    input,
    state: "pending",
    needs_confirm: true,
    ok: false,
    ask: data.summary,
  });
  const call: ChatPending = { id, name: data.name, input, summary: data.summary };
  const pending = [...view.pending.filter((other) => other.id !== id), call];
  if (has(view, id)) {
    return { ...view, messages: eachChip(view.messages, patch, id), pending, closed: null };
  }
  const { view: open, at } = answering(view);
  const chip = patch({
    id,
    name: String(data.name ?? ""),
    input,
    state: "pending",
    needs_confirm: true,
    ok: false,
    summary: "",
    ask: data.summary,
    images: [],
    artifact: null,
  });
  return { ...withChip(open, at, chip), pending, closed: null };
}

function finished(view: ChatView, data: ChatEventData["done"]): ChatView {
  let messages = view.messages;
  // A refused answer is not kept by the server and is not to be read: the
  // words that arrived before the refusal go, and the server's sentence
  // about it is what is shown.
  if (data.reason === "refusal" && view.closed !== null) {
    messages = messages.filter((message) => message.key !== view.closed);
  }
  if (data.reason === "stopped") {
    // A call Claude was still writing when the answer was stopped was
    // never asked for: the server does not keep it, so neither does the
    // screen (a reload would not show it). An answer that was only that
    // call goes with it.
    messages = messages
      .map((message) =>
        message.tools.some((chip) => chip.state === "preparing")
          ? { ...message, tools: message.tools.filter((chip) => chip.state !== "preparing") }
          : message,
      )
      .filter(
        (message) =>
          message.role !== "assistant" ||
          message.id !== null ||
          message.text !== "" ||
          message.tools.length > 0,
      );
  }
  // Nothing is running any more, so a call with no answer by now will
  // never get one: say so rather than leave it spinning.
  messages = eachChip(messages, (chip) =>
    chip.state === "preparing" || chip.state === "running"
      ? { ...chip, state: "interrupted", ok: false }
      : chip,
  );
  return {
    ...view,
    messages,
    running: false,
    stranded: false,
    thinking: false,
    writing: null,
    closed: null,
    gap: false,
    ended: {
      reason: String(data.reason ?? "end"),
      message: typeof data.message === "string" ? data.message : null,
      kind: view.ended?.kind ?? null,
    },
  };
}

// --- helpers -----------------------------------------------------------------

function has(view: ChatView, chipId: string): boolean {
  return view.messages.some((message) => message.tools.some((chip) => chip.id === chipId));
}

/**
 * Change chips (all of them, or the one with `only` as its id) and leave
 * every message that did not change the very same object, so a page that
 * draws from this re-draws only what moved.
 */
function eachChip(
  messages: ViewMessage[],
  change: (chip: ViewChip) => ViewChip,
  only?: string,
): ViewMessage[] {
  let touched = false;
  const next = messages.map((message) => {
    let changed = false;
    const tools = message.tools.map((chip) => {
      if (only !== undefined && chip.id !== only) return chip;
      const after = change(chip);
      if (after !== chip) changed = true;
      return after;
    });
    if (!changed) return message;
    touched = true;
    return { ...message, tools };
  });
  return touched ? next : messages;
}

/** The view with an answer being written in it, and where that answer is. */
function answering(view: ChatView): { view: ChatView; at: number } {
  if (view.writing !== null) {
    const at = view.messages.findIndex((message) => message.key === view.writing);
    if (at !== -1) return { view, at };
  }
  const minted = view.minted + 1;
  const message: ViewMessage = {
    key: `live-${minted}`,
    id: null,
    seq: null,
    role: "assistant",
    text: "",
    context: null,
    tools: [],
    sending: false,
  };
  return {
    view: {
      ...view,
      messages: [...view.messages, message],
      writing: message.key,
      gap: false,
      minted,
    },
    at: view.messages.length,
  };
}

function withChip(view: ChatView, at: number, chip: ViewChip): ChatView {
  return {
    ...view,
    messages: view.messages.map((message, index) =>
      index === at ? { ...message, tools: [...message.tools, chip] } : message,
    ),
  };
}
