// Run with `npm test` (Node's own test runner; nothing to install).
//
// The chat panel reads Claude's answer as a stream of events over one
// long-lived response. The network hands that response over in pieces of
// its own choosing, and through a proxy the pieces are not the server's.
// These pin that the events come out whole whatever the pieces were, and
// what the reader says when the server refuses, goes quiet or goes away.
// No network: `fetch` is passed in.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  StreamRefused,
  chatEvent,
  eventParser,
  readEventStream,
  refusalMessage,
} from "../src/lib/chatStream.ts";

/** A turn's first frames as the server writes them (api/v1/chat.py `_frame`). */
const TURN =
  ": open\n\n" +
  'id: 0\nevent: start\ndata: {"conversation_id":"C","model":"claude-opus-5-5","pictures":true,"message_id":"M1","seq":1}\n\n' +
  'id: 1\nevent: thinking\ndata: {"on":true}\n\n' +
  ": keep-alive\n\n" +
  'id: 2\nevent: text\ndata: {"delta":"Confirm the tag “Silo”?"}\n\n' +
  'id: 3\nevent: done\ndata: {"reason":"end","message":null}\n\n';

function parseAll(pieces) {
  const parser = eventParser();
  const events = [];
  for (const piece of pieces) events.push(...parser.feed(piece));
  events.push(...parser.end());
  return events;
}

const names = (events) => events.map((event) => event.event);

test("a turn read in one piece gives its events and skips the comments", () => {
  const events = parseAll([TURN]);

  assert.deepEqual(names(events), ["start", "thinking", "text", "done"]);
  assert.deepEqual(
    events.map((event) => event.id),
    ["0", "1", "2", "3"],
  );
  assert.equal(JSON.parse(events[2].data).delta, "Confirm the tag “Silo”?");
});

test("the same turn cut at every possible place gives the same events", () => {
  // A proxy re-chunks as it likes: mid-event, mid-line, between the two
  // line breaks that end an event. Every cut must read the same.
  const whole = parseAll([TURN]);
  for (let cut = 0; cut <= TURN.length; cut++) {
    assert.deepEqual(parseAll([TURN.slice(0, cut), TURN.slice(cut)]), whole, `cut at ${cut}`);
  }
  assert.deepEqual(parseAll([...TURN]), whole, "one character at a time");
});

test("an event cut in half is held until its other half arrives", () => {
  const parser = eventParser();

  assert.deepEqual(parser.feed('id: 4\nevent: text\ndata: {"del'), []);
  assert.deepEqual(parser.feed('ta":"Hello"}\n'), []);

  assert.deepEqual(parser.feed("\n"), [{ id: "4", event: "text", data: '{"delta":"Hello"}' }]);
});

test("lines ending CR LF read the same, even with the pair split between pieces", () => {
  const crlf = TURN.replaceAll("\n", "\r\n");
  const whole = parseAll([TURN]);

  assert.deepEqual(parseAll([crlf]), whole);
  for (let cut = 0; cut <= crlf.length; cut++) {
    assert.deepEqual(parseAll([crlf.slice(0, cut), crlf.slice(cut)]), whole, `cut at ${cut}`);
  }
  // A bare CR ends a line too.
  assert.deepEqual(parseAll([TURN.replaceAll("\n", "\r")]), whole);
});

test("several data lines are one event, joined by line breaks", () => {
  const events = parseAll(["event: text\ndata: first\ndata:second\ndata:\ndata:  indented\n\n"]);

  // One space after the colon is the separator; any more belong to the value.
  assert.deepEqual(events, [{ id: null, event: "text", data: "first\nsecond\n\n indented" }]);
});

test("a last event whose closing blank line never came is still given", () => {
  // A proxy that closes a moment early must not turn a finished answer
  // into "the connection dropped".
  const cut = TURN.slice(0, -1);
  assert.deepEqual(names(parseAll([cut])), ["start", "thinking", "text", "done"]);

  // No line break at all after the data.
  const bare = 'id: 3\nevent: done\ndata: {"reason":"end","message":null}';
  assert.deepEqual(chatEvent(parseAll([bare])[0]), {
    event: "done",
    index: 3,
    data: { reason: "end", message: null },
  });
});

test("an event the connection cut off mid-line is not mistaken for a whole one", () => {
  const events = parseAll(['id: 3\nevent: text\ndata: {"delta":"Hel']);

  assert.equal(events.length, 1);
  assert.equal(chatEvent(events[0]), null);
  // And one that never reached its data is nothing at all.
  assert.deepEqual(parseAll(["id: 3\nevent: done\n"]), []);
});

test("an event carries its own id, not the one before it", () => {
  // The panel counts what it has by these. An event without one must not
  // look like a repeat of the last.
  const events = parseAll(["id: 7\nevent: text\ndata: {}\n\nevent: text\ndata: {}\n\n"]);

  assert.deepEqual(
    events.map((event) => event.id),
    ["7", null],
  );
});

test("an unnamed event is a message, and one with no data is not an event", () => {
  assert.deepEqual(parseAll(["data: hello\n\n"]), [{ id: null, event: "message", data: "hello" }]);
  assert.deepEqual(parseAll(["event: done\nid: 1\n\n", "retry: 500\n\n", "\n\n\n"]), []);
});

test("a byte order mark at the very start is not part of the first field", () => {
  assert.deepEqual(parseAll(["﻿data: x\n\n"]), [{ id: null, event: "message", data: "x" }]);
});

test("only the chat's own events with a JSON object in them are passed on", () => {
  const raw = (event, data, id = "5") => ({ id, event, data });

  assert.deepEqual(chatEvent(raw("text", '{"delta":"hi"}')), {
    event: "text",
    index: 5,
    data: { delta: "hi" },
  });
  // A newer server may say things this version does not know.
  assert.equal(chatEvent(raw("progress", "{}")), null);
  assert.equal(chatEvent(raw("message", "{}")), null);
  assert.equal(chatEvent(raw("text", "not json")), null);
  assert.equal(chatEvent(raw("text", "[1]")), null);
  assert.equal(chatEvent(raw("text", "null")), null);
  assert.equal(chatEvent(raw("text", '"words"')), null);
  // No id, or one that is not a count: the event stands, unnumbered.
  assert.equal(chatEvent(raw("text", "{}", null)).index, null);
  assert.equal(chatEvent(raw("text", "{}", "abc")).index, null);
});

// --- the reader --------------------------------------------------------------

/** A response whose body arrives in the given pieces (strings or bytes). */
function streamed(pieces, { status = 200, failAfter = false } = {}) {
  const encoder = new TextEncoder();
  const left = [...pieces];
  // One piece per read: a body that fails drops whatever it had not yet
  // handed over, so the failure must come after the pieces were read.
  const body = new ReadableStream({
    pull(controller) {
      const piece = left.shift();
      if (piece !== undefined) {
        controller.enqueue(typeof piece === "string" ? encoder.encode(piece) : piece);
      } else if (failAfter) controller.error(new TypeError("network error"));
      else controller.close();
    },
  });
  return new Response(body, { status, headers: { "Content-Type": "text/event-stream" } });
}

async function read(response, init = {}) {
  const events = [];
  const calls = [];
  const status = await readEventStream(
    "/api/v1/chat/conversations/C/messages",
    init,
    (event) => events.push(event),
    async (url, given) => {
      calls.push({ url, init: given });
      return response;
    },
  );
  return { status, events, calls };
}

test("a streamed answer is read to its end and the request is made as given", async () => {
  const init = { method: "POST", body: '{"text":"hi"}' };

  const { status, events, calls } = await read(streamed([TURN]), init);

  assert.equal(status, 200);
  assert.deepEqual(names(events), ["start", "thinking", "text", "done"]);
  assert.equal(calls[0].url, "/api/v1/chat/conversations/C/messages");
  assert.equal(calls[0].init, init);
});

test("a character split between two pieces of the response is put back together", async () => {
  // The curly quotes are three bytes each. Cut everywhere, byte by byte.
  const bytes = new TextEncoder().encode(TURN);
  for (let cut = 1; cut < bytes.length; cut++) {
    const { events } = await read(streamed([bytes.slice(0, cut), bytes.slice(cut)]));
    assert.equal(events.length, 4, `cut at byte ${cut}`);
    assert.equal(JSON.parse(events[2].data).delta, "Confirm the tag “Silo”?", `cut at byte ${cut}`);
  }
});

test("a refusal before the stream is the server's own sentence and status", async () => {
  const envelope = {
    error: { code: "conflict", message: "Claude is still working on the last message.", detail: null },
  };
  const refused = new Response(JSON.stringify(envelope), {
    status: 409,
    headers: { "Content-Type": "application/json" },
  });

  await assert.rejects(read(refused), (err) => {
    assert.ok(err instanceof StreamRefused);
    assert.equal(err.status, 409);
    assert.equal(err.message, "Claude is still working on the last message.");
    return true;
  });
});

test("a refusal with no sentence in it still says something", async () => {
  // A proxy's own error page, or nothing at all.
  await assert.rejects(read(new Response("<html>Bad Gateway</html>", { status: 502 })), {
    name: "StreamRefused",
    status: 502,
    message: "Request failed (502)",
  });
  assert.equal(refusalMessage(500, ""), "Request failed (500)");
  assert.equal(refusalMessage(500, '{"error":{"message":"  "}}'), "Request failed (500)");
  assert.equal(refusalMessage(400, '{"error":null}'), "Request failed (400)");
  assert.equal(refusalMessage(404, '{"error":{"message":"No such conversation"}}'), "No such conversation");
});

test("nothing to follow is said as such and reads no events", async () => {
  const { status, events } = await read(new Response(null, { status: 204 }));

  assert.equal(status, 204);
  assert.deepEqual(events, []);
});

test("a connection that drops part-way keeps what arrived and does not throw", async () => {
  // The turn carries on on the server; the caller sees there was no `done`
  // and picks it up again.
  const half = TURN.slice(0, TURN.indexOf("id: 3"));

  const { status, events } = await read(streamed([half], { failAfter: true }));

  assert.equal(status, 200);
  assert.deepEqual(names(events), ["start", "thinking", "text"]);
});

test("a server that cannot be reached is the caller's to report", async () => {
  await assert.rejects(
    readEventStream("/x", {}, () => undefined, async () => {
      throw new TypeError("fetch failed");
    }),
    { name: "TypeError", message: "fetch failed" },
  );
});

test("stopping listening ends the read with the abort, and lets the connection go", async () => {
  const controller = new AbortController();
  let cancelled = false;
  let push;
  const body = new ReadableStream({
    start(c) {
      push = c;
      c.enqueue(new TextEncoder().encode(TURN.slice(0, TURN.indexOf("id: 2"))));
    },
    cancel() {
      cancelled = true;
    },
  });
  const events = [];
  const reading = readEventStream(
    "/x",
    { signal: controller.signal },
    (event) => {
      events.push(event.event);
      // The owner leaves the conversation as soon as the turn has started.
      if (event.event === "thinking") {
        controller.abort();
        push.error(controller.signal.reason);
      }
    },
    async () => new Response(body, { status: 200 }),
  );

  await assert.rejects(reading, { name: "AbortError" });
  assert.deepEqual(events, ["start", "thinking"]);
  assert.equal(cancelled, false, "an errored body has nothing left to cancel");
});

test("an event handler that throws does not leave the response open", async () => {
  let cancelled = false;
  const body = new ReadableStream({
    start(c) {
      c.enqueue(new TextEncoder().encode(TURN));
    },
    cancel() {
      cancelled = true;
    },
  });

  await assert.rejects(
    readEventStream(
      "/x",
      {},
      () => {
        throw new Error("reducer bug");
      },
      async () => new Response(body, { status: 200 }),
    ),
    { message: "reducer bug" },
  );
  assert.equal(cancelled, true);
});
