// Run with `npm test` (Node's own test runner; nothing to install).
//
// The viewer asks the server why a clip would not play, and words its
// apology from the answer: 404 is "no playable copy has been made", 503 is
// "the drive is not connected". These pin how the question is asked. No
// network: `fetch` is replaced in every test.

import assert from "node:assert/strict";
import { test } from "node:test";

import { urlStatus } from "../src/lib/probe.ts";

/** Replace `fetch` for one test; returns the calls it received. */
function answerWith(t, respond) {
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, init) => {
    calls.push({ url, init });
    return respond();
  });
  return calls;
}

test("the question goes to the server, never to the browser's cache", async (t) => {
  // Derivatives are cacheable for a day. Answered from the cache, a clip
  // played this morning said "here" after its drive had gone, and the
  // viewer blamed the browser.
  const calls = answerWith(t, () => new Response(null, { status: 503 }));

  assert.equal(await urlStatus("/api/v1/media/abc/proxy"), 503);

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/v1/media/abc/proxy");
  assert.equal(calls[0].init.cache, "no-store");
});

test("one byte is asked for, with the session, and the rest is let go", async (t) => {
  let cancelled = false;
  const body = new ReadableStream({
    cancel() {
      cancelled = true;
    },
  });
  const calls = answerWith(t, () => new Response(body, { status: 206 }));

  assert.equal(await urlStatus("/api/v1/slideshows/s1/video"), 206);

  assert.equal(calls[0].init.headers.Range, "bytes=0-0");
  assert.equal(calls[0].init.credentials, "same-origin");
  assert.equal(cancelled, true);
});

test("the status is handed back as it is, for the page to word", async (t) => {
  for (const status of [200, 401, 404, 503]) {
    answerWith(t, () => new Response(null, { status }));
    assert.equal(await urlStatus("/x"), status);
    t.mock.restoreAll();
  }
});

test("a server that cannot be reached is status 0, not an error thrown at the page", async (t) => {
  t.mock.method(globalThis, "fetch", async () => {
    throw new TypeError("fetch failed");
  });

  assert.equal(await urlStatus("/api/v1/media/abc/proxy"), 0);
});
