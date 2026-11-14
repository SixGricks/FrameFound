// Reading an answer as it is written: the chat panel's event streams.
//
// Two of the three streams are POSTs (a message, a yes or no), which the
// browser's own EventSource cannot make, so the stream is read by hand:
// `fetch`, the body's reader, and the parser below. The parser is fed
// whatever pieces the network delivers — a piece can end in the middle of
// an event, of a line, or of one character — and hands back whole events.
//
// Nothing is imported at run time (the one import is types only), so
// `node --test` can load this file as it is (tests/chatStream.test.mjs).

import type { ChatEvent } from "./api";

/** One event as it arrived: its `id:`, `event:` and `data:` fields. */
export interface StreamEvent {
  /** This event's own `id:` line; null when it had none. */
  id: string | null;
  /** "message" when the event did not name itself, as the browser has it. */
  event: string;
  /** The `data:` lines, joined by a line break. */
  data: string;
}

export interface EventParser {
  /** Take the next piece of text; returns the events it completed. */
  feed(text: string): StreamEvent[];
  /** The stream has ended; returns the event it was in the middle of, if any. */
  end(): StreamEvent[];
}

/**
 * An incremental parser for `text/event-stream`.
 *
 * It follows the browser's rules (lines end in LF, CR or CRLF; a line that
 * starts with a colon is a comment; one space after the colon is not part
 * of the value; a blank line ends an event; an event with no data is not an
 * event) with two differences, both on purpose:
 *
 * - `id` is the event's own, not the last one seen. The panel uses it to
 *   count what it has, and an event that carried none must not be taken for
 *   a repeat of the one before.
 * - `end()` gives back an event whose closing blank line never came. The
 *   last thing a turn says is `done`, and a proxy that closes the connection
 *   a moment early would otherwise turn a finished answer into "the
 *   connection dropped". An event cut off mid-line is given back too; its
 *   data is then not valid JSON and `chatEvent` throws it away.
 */
export function eventParser(): EventParser {
  let line = "";
  // The last character fed was a CR: a LF straight after it — which may be
  // the first character of the next piece — ends the same line, not another.
  let afterCR = false;
  let begun = false;
  let id: string | null = null;
  let name = "";
  let data: string[] = [];

  function endEvent(out: StreamEvent[]): void {
    if (data.length > 0) out.push({ id, event: name || "message", data: data.join("\n") });
    id = null;
    name = "";
    data = [];
  }

  function endLine(text: string, out: StreamEvent[]): void {
    if (text === "") {
      endEvent(out);
      return;
    }
    if (text.startsWith(":")) return;
    const colon = text.indexOf(":");
    const field = colon === -1 ? text : text.slice(0, colon);
    let value = colon === -1 ? "" : text.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "data") data.push(value);
    else if (field === "event") name = value;
    else if (field === "id" && !value.includes("\u0000")) id = value;
    // `retry` and anything unknown: ignored, as the browser does.
  }

  return {
    feed(piece: string): StreamEvent[] {
      const out: StreamEvent[] = [];
      let text = piece;
      if (!begun && text !== "") {
        begun = true;
        if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
      }
      let from = 0;
      for (let at = 0; at < text.length; at++) {
        const code = text.charCodeAt(at);
        if (code !== 10 && code !== 13) {
          afterCR = false;
          continue;
        }
        if (code === 10 && afterCR) {
          afterCR = false;
          from = at + 1;
          continue;
        }
        endLine(line + text.slice(from, at), out);
        line = "";
        from = at + 1;
        afterCR = code === 13;
      }
      line += text.slice(from);
      return out;
    },
    end(): StreamEvent[] {
      const out: StreamEvent[] = [];
      if (line !== "") endLine(line, out);
      endEvent(out);
      line = "";
      afterCR = false;
      begun = false;
      return out;
    },
  };
}

const EVENT_NAMES = new Set([
  "start",
  "thinking",
  "text",
  "tool",
  "usage",
  "tool_result",
  "confirm",
  "error",
  "done",
]);

/**
 * A stream event as one of the chat's own, or null for anything else: a
 * name this version does not know (a newer server may say more), data that
 * is not a JSON object (an event cut off by a dropped connection).
 *
 * `index` is the event's place in its turn, counted from 0 — what the panel
 * sends back as `after` to pick a turn up where it left off.
 */
export function chatEvent(raw: StreamEvent): ChatEvent | null {
  if (!EVENT_NAMES.has(raw.event)) return null;
  let data: unknown;
  try {
    data = JSON.parse(raw.data);
  } catch {
    return null;
  }
  if (data === null || typeof data !== "object" || Array.isArray(data)) return null;
  const index = raw.id !== null && /^[0-9]+$/.test(raw.id) ? Number(raw.id) : null;
  return { event: raw.event, index, data } as ChatEvent;
}

/** The server said no before any stream began: its status and its sentence. */
export class StreamRefused extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "StreamRefused";
    this.status = status;
  }
}

/**
 * The sentence in an error answer. Every FrameFound error is
 * `{"error": {"code", "message", "detail"}}`; anything else (a proxy's HTML
 * page, an empty body) gets the same filler `request` in api.ts uses.
 */
export function refusalMessage(status: number, body: string): string {
  try {
    const parsed = JSON.parse(body) as { error?: { message?: unknown } } | null;
    const message = parsed?.error?.message;
    if (typeof message === "string" && message.trim() !== "") return message;
  } catch {
    // Not JSON: fall through to the filler.
  }
  return `Request failed (${status})`;
}

/**
 * Make a request whose answer is an event stream and hand each event to
 * `onEvent` as it arrives.
 *
 * Resolves with the HTTP status once the stream is over: 200 for a stream
 * that was read to its end, 204 when the server had nothing to stream. A
 * connection that drops part-way through also resolves (with 200): what
 * arrived stands, and whether the turn finished is for the caller to judge
 * by whether it saw `done`.
 *
 * Rejects with `StreamRefused` when the server answered with an error
 * before streaming; with the abort error when `init.signal` was aborted;
 * and with whatever `fetch` threw when the server was never reached.
 */
export async function readEventStream(
  url: string,
  init: RequestInit,
  onEvent: (event: StreamEvent) => void,
  fetcher: typeof fetch = fetch,
): Promise<number> {
  const resp = await fetcher(url, init);
  if (resp.status === 204) return 204;
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    throw new StreamRefused(resp.status, refusalMessage(resp.status, body));
  }
  const parser = eventParser();
  const reader = resp.body?.getReader();
  if (!reader) {
    for (const event of parser.feed(await resp.text())) onEvent(event);
    for (const event of parser.end()) onEvent(event);
    return resp.status;
  }
  // `stream: true` keeps the first bytes of a character that was split
  // between two pieces until the rest arrives.
  const decoder = new TextDecoder();
  let finished = false;
  try {
    for (;;) {
      let step: ReadableStreamReadResult<Uint8Array>;
      try {
        step = await reader.read();
      } catch (err) {
        if (init.signal?.aborted) throw err;
        // The connection went away mid-answer. The turn itself carries on
        // on the server, so this is an end of listening, not a failure.
        break;
      }
      if (step.done) break;
      for (const event of parser.feed(decoder.decode(step.value, { stream: true }))) onEvent(event);
    }
    for (const event of parser.feed(decoder.decode())) onEvent(event);
    for (const event of parser.end()) onEvent(event);
    finished = true;
  } finally {
    // Leaving early (aborted, or `onEvent` threw): let the connection go.
    if (!finished) reader.cancel().catch(() => undefined);
  }
  return resp.status;
}
