// Run with `npm test` (Node's own test runner; nothing to install).
//
// How the AI models page reads itself again. The page once re-armed its
// timer from a React effect on its own state: a second failed read in a row
// set the same sentence, nothing changed, and the page never read again — a
// download bar froze across a restart of FrameFound under a sentence that
// promised another try. These drive the reader with a hand-turned clock.

import assert from "node:assert/strict";
import { test } from "node:test";

import { FAST_MS, SLOW_MS, readAgainIn, rereads } from "../src/lib/localModels.ts";

const QUIET = { mode: "ready", server: { state: "idle" }, jobs: [] };
const MOVING = { mode: "ready", server: { state: "idle" }, jobs: [{ state: "running" }] };

/** A reader whose reads are answered by `answers` in turn (an Error rejects)
 *  and whose timers wait until the test turns them. */
function harness(answers, { hidden = () => false } = {}) {
  const timers = [];
  const drawn = [];
  const failures = [];
  let reads = 0;
  const reader = rereads({
    read: async () => {
      const answer = answers[reads++];
      if (answer instanceof Error) throw answer;
      return answer;
    },
    answered: (value) => drawn.push(value),
    failed: (err) => failures.push(err.message),
    every: readAgainIn,
    hidden,
    later: (run, ms) => {
      const timer = { run, ms, cancelled: false };
      timers.push(timer);
      return timer;
    },
    cancel: (timer) => {
      timer.cancelled = true;
    },
  });
  const armed = () => timers.filter((timer) => !timer.cancelled && !timer.fired);
  async function turn() {
    const [next] = armed();
    assert.ok(next, "a timer should be armed");
    next.fired = true;
    next.run();
    await settle();
  }
  return { reader, timers, drawn, failures, armed, turn, reads: () => reads };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("two failed reads in a row still lead to a third, which draws when the server is back", async () => {
  const down = new Error("FrameFound could not be reached");
  const page = harness([QUIET, down, down, MOVING]);
  await page.reader.now();
  assert.deepEqual(page.drawn, [QUIET]);

  await page.turn();
  await page.turn();
  assert.deepEqual(page.failures, [down.message, down.message]);
  // The same failure twice is not "no change": the timer is armed again,
  // at the pace the last good overview asks for.
  assert.equal(page.armed().length, 1);
  assert.equal(page.armed()[0].ms, SLOW_MS);

  await page.turn();
  assert.equal(page.reads(), 4);
  assert.deepEqual(page.drawn, [QUIET, MOVING]);
  assert.equal(page.armed()[0].ms, FAST_MS);
});

test("a page whose first reads fail keeps trying, slowly", async () => {
  const down = new Error("Request failed (502)");
  const page = harness([down, down, down, QUIET]);
  await page.reader.now();
  await page.turn();
  await page.turn();
  assert.equal(page.armed()[0].ms, SLOW_MS);
  await page.turn();
  assert.deepEqual(page.drawn, [QUIET]);
});

test("only the newest read draws, and only one timer is ever armed", async () => {
  let release;
  const slow = new Promise((resolve) => {
    release = resolve;
  });
  const answers = [slow, MOVING];
  let reads = 0;
  const drawn = [];
  const timers = [];
  const reader = rereads({
    read: () => answers[reads++],
    answered: (value) => drawn.push(value),
    failed: () => {},
    every: readAgainIn,
    hidden: () => false,
    later: (run, ms) => {
      const timer = { run, ms, cancelled: false };
      timers.push(timer);
      return timer;
    },
    cancel: (timer) => {
      timer.cancelled = true;
    },
  });
  const first = reader.now();
  // An action's read overtakes the timer's.
  await reader.now();
  release(QUIET);
  await first;
  assert.deepEqual(drawn, [MOVING]);
  assert.equal(timers.filter((timer) => !timer.cancelled).length, 1);
});

test("a turn that comes while the tab is hidden is let pass until it is shown again", async () => {
  let hidden = false;
  const page = harness([QUIET, MOVING], { hidden: () => hidden });
  await page.reader.now();
  hidden = true;
  await page.turn();
  assert.equal(page.reads(), 1);
  assert.equal(page.armed().length, 0);
  // Showing the tab reads at once, and that read arms the timer again.
  hidden = false;
  await page.reader.now();
  assert.deepEqual(page.drawn, [QUIET, MOVING]);
  assert.equal(page.armed().length, 1);
});

test("after stop nothing in flight is drawn and nothing more is read", async () => {
  let release;
  const slow = new Promise((resolve) => {
    release = resolve;
  });
  const page = harness([slow]);
  const inFlight = page.reader.now();
  page.reader.stop();
  release(QUIET);
  await inFlight;
  assert.deepEqual(page.drawn, []);
  assert.equal(page.armed().length, 0);
  await page.reader.now();
  assert.equal(page.reads(), 1);
});

test("where models cannot run the page is never read again", () => {
  assert.equal(readAgainIn({ ...QUIET, mode: "unavailable" }, false), null);
  assert.equal(readAgainIn({ ...QUIET, mode: "unavailable" }, true), null);
  assert.equal(readAgainIn(null, true), SLOW_MS);
  assert.equal(readAgainIn(null, false), null);
});
