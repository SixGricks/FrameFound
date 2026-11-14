// Run with `npm test` (Node's own test runner; nothing to install).
//
// Where a local model's look shows (src/lib/looks.ts, its last section): the
// card on an asset's page, a clip's shot list, and a search hit. The caption,
// the score and the reason are the server's; these check what the page adds
// around them: who said it, what it is about, which picture a shot's row
// tries, and when a caption stands where a spoken quote would.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  INCOMPLETE_NOTE,
  MADE_UP_NOTE,
  NOT_LOOKED_AT,
  NO_SHOTS_NOTE,
  NO_SHOT_PICTURE_NOTE,
  OPINION_NOTE,
  STEADINESS_NOTE,
  captionShown,
  lookAbout,
  lookSaidBy,
  reviewWayOut,
  shotAt,
  shotMove,
  shotPictures,
  shotsCountText,
  showsLookCard,
  steadinessText,
  stillsOnlyWhere,
  utcMoment,
} from "../src/lib/looks.ts";

const LOOK = {
  model: "mlx-community/gemma-4-e4b-it-4bit",
  revision_short: "475b908",
  variant: "",
  runtime: "mlx-vlm",
  runtime_version: "0.7.12",
  prompt_version: "look-1",
};

const SHOTS = [
  { idx: 0, start_ms: 0, end_ms: 25890, still_ms: 12945, look: null },
  { idx: 1, start_ms: 25890, end_ms: 51780, still_ms: 38835, look: { score: 70 } },
  { idx: 2, start_ms: 51780, end_ms: 77671, still_ms: 64725, look: null },
];

// ---- who said it -----------------------------------------------------------------------

test("who said it: the model's short name, its build, the program, the question and the day", () => {
  assert.equal(
    lookSaidBy(LOOK, "12 Nov"),
    "gemma-4-e4b-it-4bit · build 475b908 · MLX-VLM 0.7.12 · look-1 · 12 Nov",
  );
  // Without a day (the line under a shot list covers looks of several days).
  assert.equal(lookSaidBy(LOOK), "gemma-4-e4b-it-4bit · build 475b908 · MLX-VLM 0.7.12 · look-1");
});

test("a model with several files names the one that answered", () => {
  const said = lookSaidBy({ ...LOOK, model: "google/gemma-4-e4b-it-GGUF", variant: "Q4_K_M", runtime: "llama-cpp" });
  assert.equal(said, "gemma-4-e4b-it-GGUF (Q4_K_M) · build 475b908 · llama.cpp 0.7.12 · look-1");
});

test("only what the server recorded is said: an empty part is left out, an unknown program shown as it came", () => {
  assert.equal(lookSaidBy({ model: "allenai/some-model" }), "some-model");
  assert.equal(
    lookSaidBy({ model: "plain-name", revision_short: "", runtime: "something-new", runtime_version: "" }),
    "plain-name · something-new",
  );
  // The demonstration's program is named as one, not as a model server.
  assert.equal(
    lookSaidBy({ ...LOOK, runtime: "demo", runtime_version: "1.9" }),
    "gemma-4-e4b-it-4bit · build 475b908 · demo program 1.9 · look-1",
  );
});

test("a time the server wrote without a zone is UTC, not the browser's own time", () => {
  const at = Date.UTC(2026, 10, 13, 21, 46, 2, 781);
  assert.equal(utcMoment("2026-11-13T21:46:02.781"), at);
  assert.equal(utcMoment("2026-11-13T21:46:02.781Z"), at);
  // A zone that is written is respected.
  assert.equal(utcMoment("2026-11-13T23:46:02.781+02:00"), at);
  assert.equal(utcMoment("2026-11-13T16:46:02.781-05:00"), at);
  assert.ok(Number.isNaN(utcMoment("")));
  assert.ok(Number.isNaN(utcMoment(null)));
  assert.ok(Number.isNaN(utcMoment("yesterday")));
});

// ---- the card on an asset's page ---------------------------------------------------------

test("a look is about a photograph, a clip's best shot with its times, or a whole clip", () => {
  assert.deepEqual(lookAbout("image", { whole: true, start_ms: null, end_ms: null }), {
    kind: "photograph",
    startMs: null,
    endMs: null,
  });
  assert.deepEqual(lookAbout("video", { whole: false, start_ms: 2000, end_ms: 14000 }), {
    kind: "shot",
    startMs: 2000,
    endMs: 14000,
  });
  // A clip judged from one still, while its shots have not been found.
  assert.deepEqual(lookAbout("video", { whole: true, start_ms: null, end_ms: null }), {
    kind: "clip",
    startMs: null,
    endMs: null,
  });
  // Times that did not come are never made up.
  assert.equal(lookAbout("video", { whole: false, start_ms: 2000, end_ms: null }).kind, "clip");
});

test("the card is there with a look; without one only where a model could look, and never for sound", () => {
  const look = { score: 82 };
  // An opinion that was written is shown wherever it is read.
  assert.equal(showsLookCard({ media_type: "image", look }, false), true);
  assert.equal(showsLookCard({ media_type: "video", look }, true), true);
  // Where models cannot run, a file nothing looked at has the page it always had.
  assert.equal(showsLookCard({ media_type: "image", look: null }, false), false);
  assert.equal(showsLookCard({ media_type: "video", look: null }, false), false);
  // Where they can, the card says "not yet" and where to ask.
  assert.equal(showsLookCard({ media_type: "image", look: null }, true), true);
  assert.equal(showsLookCard({ media_type: "video", look: null }, true), true);
  assert.equal(showsLookCard({ media_type: "audio", look: null }, true), false);
  // A server from before looks sends no field at all.
  assert.equal(showsLookCard({ media_type: "image" }, false), false);
});

test("the sentences around a look say whose word it is and what is missing", () => {
  assert.equal(OPINION_NOTE, "A local model's opinion, not a fact.");
  assert.equal(NOT_LOOKED_AT, "Not looked at yet.");
  assert.equal(INCOMPLETE_NOTE, "Some of this clip's shots have not been looked at yet.");
  assert.equal(NO_SHOTS_NOTE, "This clip's shots have not been found yet. It needs a playable copy first.");
  assert.match(MADE_UP_NOTE, /no model looked at this/);
  assert.match(STEADINESS_NOTE, /1\.00 is perfectly steady/);
});

// ---- a clip's shots -----------------------------------------------------------------------

const FRAMES = [{ ts_ms: 0 }, { ts_ms: 10000 }, { ts_ms: 20000 }, { ts_ms: 30000 }, { ts_ms: 40000 }];

test("a shot's row shows the sampled frame nearest its middle, from inside the shot", () => {
  assert.deepEqual(shotPictures(SHOTS[0], FRAMES), ["frame?ts=10000"]);
  // Never a frame from the next shot: 25890 begins shot 1, so 20000 is not its.
  assert.deepEqual(shotPictures(SHOTS[1], FRAMES), ["frame?ts=40000"]);
});

test("a shot with no sampled frame inside it has no picture, never another shot's", () => {
  // Not the clip's thumbnail and not the nearest frame outside: beside this
  // shot's caption either would read as the model being wrong.
  assert.deepEqual(shotPictures(SHOTS[2], FRAMES), []);
  assert.deepEqual(shotPictures(SHOTS[2]), []);
  // A shot's end belongs to the next shot.
  const shot = { idx: 0, start_ms: 0, end_ms: 20000, still_ms: 10000 };
  assert.deepEqual(shotPictures(shot, [{ ts_ms: 20000 }]), []);
  assert.match(NO_SHOT_PICTURE_NOTE, /none of the clip's sampled frames falls inside it/);
});

test("a shot's own still is not asked for while the server does not serve it", () => {
  // Asked for, every row would be answered 404.
  for (const shot of SHOTS) {
    assert.ok(!shotPictures(shot, FRAMES).includes(`frame?ts=${shot.still_ms}`));
  }
  // A sampled frame that sits at the still's own moment is served, and used.
  const shot = { idx: 0, start_ms: 0, end_ms: 20000, still_ms: 10000 };
  assert.deepEqual(shotPictures(shot, [{ ts_ms: 10000 }]), ["frame?ts=10000"]);
});

test("once the server serves a shot's still it comes first, and the sampled frame is tried after it", () => {
  assert.deepEqual(shotPictures(SHOTS[0], FRAMES, true), ["frame?ts=12945", "frame?ts=10000"]);
  assert.deepEqual(shotPictures(SHOTS[2], FRAMES, true), ["frame?ts=64725"]);
  // The same moment is not asked for twice.
  const shot = { idx: 0, start_ms: 0, end_ms: 20000, still_ms: 10000 };
  assert.deepEqual(shotPictures(shot, [{ ts_ms: 10000 }], true), ["frame?ts=10000"]);
});

test("the move is said with its direction when it has one", () => {
  assert.equal(shotMove({ move: "orbit", direction: "right" }), "orbit right");
  assert.equal(shotMove({ move: "rise", direction: null }), "rise");
});

test("steadiness is the measured figure, never a grade", () => {
  assert.equal(steadinessText(0.96), "0.96");
  assert.equal(steadinessText(0.4), "0.40");
  assert.equal(steadinessText(1), "1.00");
  // Out of range or missing is not dressed up as a measurement.
  assert.equal(steadinessText(1.4), "1.00");
  assert.equal(steadinessText(-0.2), "0.00");
  assert.equal(steadinessText(null), "—");
  assert.equal(steadinessText(Number.NaN), "—");
});

test("the player is in the shot whose times hold it, and in none before it has said where it is", () => {
  assert.equal(shotAt(SHOTS, 0), 0);
  assert.equal(shotAt(SHOTS, 25889), 0);
  // A shot ends where the next begins.
  assert.equal(shotAt(SHOTS, 25890), 1);
  assert.equal(shotAt(SHOTS, 77000), 2);
  assert.equal(shotAt(SHOTS, 90000), -1);
  assert.equal(shotAt(SHOTS, null), -1);
  assert.equal(shotAt([], 1000), -1);
});

test("beside the heading: how many shots, and how many a model looked at where that is worth saying", () => {
  assert.equal(shotsCountText(SHOTS, true), "3 shots · 1 looked at");
  assert.equal(shotsCountText(SHOTS.slice(0, 1), true), "1 shot · none looked at yet");
  assert.equal(shotsCountText(SHOTS.slice(1, 2), true), "1 shot · all looked at");
  // Where models cannot run and none has looked: the measured facts only.
  assert.equal(shotsCountText(SHOTS, false), "3 shots");
  assert.equal(shotsCountText([], true), "0 shots");
});

// ---- a review that ended with clips judged from one still ----------------------------------

test("a review that judged clips from one still says where the reason is; any other says nothing", () => {
  assert.equal(
    stillsOnlyWhere({ stills_only: 3 }),
    "Where making a playable copy failed, Processing says why under Recent failures.",
  );
  assert.equal(
    stillsOnlyWhere({ stills_only: 1 }),
    "If making its playable copy failed, Processing says why under Recent failures.",
  );
  assert.equal(stillsOnlyWhere({ stills_only: 0 }), "");
  // A server that sent no figure is not read as "some".
  assert.equal(stillsOnlyWhere({}), "");
});

test("a paused review and one the model stopped say where the way out is; the rest have theirs on the page", () => {
  assert.deepEqual(reviewWayOut({ state: "paused" }), {
    text: "Local AI is resumed on the",
    link: "AI models page",
    href: "/models",
  });
  assert.deepEqual(reviewWayOut({ state: "failed" }), {
    text: "Test the model, or roll it back, on the",
    link: "AI models page",
    href: "/models",
  });
  for (const state of ["tonight", "waiting", "looking", "done", "cancelled", "something-new"]) {
    assert.equal(reviewWayOut({ state }), null);
  }
});

// ---- Search ------------------------------------------------------------------------------

test("a hit matched by its caption shows the caption where a quote would be", () => {
  const caption = "The outside of a building on a clear day.";
  assert.equal(captionShown({ reasons: ["caption"], caption, snippet: null }), caption);
  assert.equal(captionShown({ reasons: ["tag", "caption"], caption, snippet: null }), caption);
});

test("words someone said keep their place: a caption never pushes a quote out", () => {
  const caption = "An auctioneer at a podium.";
  assert.equal(captionShown({ reasons: ["transcript", "caption"], caption, snippet: "starting bid" }), null);
});

test("a hit that did not match by caption shows none, whatever else came with it", () => {
  assert.equal(captionShown({ reasons: ["visual"], caption: "A barn.", snippet: null }), null);
  assert.equal(captionShown({ reasons: ["caption"], caption: null, snippet: null }), null);
  assert.equal(captionShown({ reasons: ["caption"], caption: "", snippet: null }), null);
  // A server from before captions sends neither field.
  assert.equal(captionShown({ reasons: ["name"] }), null);
});
