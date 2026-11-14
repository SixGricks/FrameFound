// Run with `npm test` (Node's own test runner; nothing to install).
//
// The server speaks in tool names, states and reasons; the person at the
// chat panel runs a photography business. These pin the sentences the panel
// puts in place of `search_media`, `interrupted` and `awaiting_confirmation`,
// when the message box is offered at all, and what the figures say.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  askDetails,
  itemName,
  MAX_MESSAGE_CHARS,
  MAX_SPOKEN_CHARS,
  cannotSend,
  chipWords,
  contextWords,
  endWords,
  gate,
  inputLines,
  modelName,
  money,
  pictureAsset,
  sectionName,
  shownTitle,
  spendWords,
  spoken,
  stateWords,
  titleOf,
  workingWords,
} from "../src/lib/chatWords.ts";

const chip = (more = {}) => ({
  id: "toolu_1",
  name: "search_media",
  input: { query: "barn at dusk" },
  state: "ran",
  needs_confirm: false,
  ok: true,
  summary: "",
  ask: null,
  images: [],
  artifact: null,
  ...more,
});

const status = (more = {}) => ({
  available: true,
  configured: true,
  enabled: true,
  problem: null,
  can_chat: true,
  model: "claude-opus-5-5",
  models: ["claude-opus-5-5"],
  budget_usd: 5,
  spent_this_month_usd: 0.42,
  ...more,
});

const message = (more = {}) => ({
  key: "k",
  id: null,
  seq: null,
  role: "assistant",
  text: "",
  context: null,
  tools: [],
  sending: false,
  ...more,
});

test("a sum is never shown as nothing when something was spent", () => {
  assert.equal(money(0), "$0.00");
  assert.equal(money(0.0028), "under $0.01");
  assert.equal(money(0.014), "$0.01");
  assert.equal(money(12.5), "$12.50");
  assert.equal(money(null), "$0.00");
  assert.equal(money(Number.NaN), "$0.00");
});

test("the spend line gives this conversation and the month against the budget", () => {
  assert.deepEqual(spendWords(0.03, status()), {
    here: "$0.03 this conversation",
    month: "$0.42 of $5.00 this month",
    over: false,
  });
  assert.equal(spendWords(0, status({ spent_this_month_usd: 5 })).over, true);
  // A budget of 0 pauses the panel: nothing can be spent, so it is reached.
  assert.equal(spendWords(0, status({ budget_usd: 0, spent_this_month_usd: 0 })).over, true);
  // Before the status has been read there is no month to speak of.
  assert.deepEqual(spendWords(0, null), { here: "$0.00 this conversation", month: null, over: false });
});

test("a model is named as people say it, and an unfamiliar id is left alone", () => {
  assert.equal(modelName("claude-opus-5-5"), "Claude Opus 5.5");
  assert.equal(modelName("claude-sonnet-5"), "Claude Sonnet 5");
  assert.equal(modelName("claude-haiku-5-5-20260301"), "Claude Haiku 5.5");
  assert.equal(modelName("something-else"), "something-else");
});

test("a conversation nothing has been said in still has a name", () => {
  assert.equal(titleOf(""), "New conversation");
  assert.equal(titleOf("  "), "New conversation");
  assert.equal(titleOf(null), "New conversation");
  assert.equal(titleOf("Find the barn"), "Find the barn");
  assert.equal(stateWords("idle"), null);
  assert.equal(stateWords("running"), "Claude is answering");
  assert.equal(stateWords("awaiting_confirmation"), "waiting for your yes or no");
});

test("this page is said the way a person would, and never by its ids", () => {
  assert.deepEqual(
    contextWords({
      path: "/tags",
      title: "Tags",
      tag: "silo",
      selection: ["a", "b", "c"],
      selection_count: 214,
    }),
    ["Tags", "tag “silo”", "214 selected"],
  );
  assert.deepEqual(
    contextWords({ path: "/assets/1704c870", asset_id: "1704c870", filename: "DJI_0042.MP4", at_s: 72.4 }),
    ["An item", "“DJI_0042.MP4”", "at 1:12"],
  );
  assert.deepEqual(contextWords({ path: "/assets/x", asset_id: "x" }), ["An item", "the item on screen"]);
  assert.deepEqual(contextWords({ path: "/listings/L1", title: "12 Mill Lane", listing_id: "L1" }), [
    "12 Mill Lane",
    "this listing",
  ]);
  assert.deepEqual(contextWords({ path: "/", title: "Search", query: "barn", library: "Farm" }), [
    "Search",
    "library “Farm”",
    "search “barn”",
  ]);
  // The count falls back to what is there when the true total was not given.
  assert.deepEqual(contextWords({ path: "/people/p", person_id: "p", selection: ["a", "b"] }), [
    "People",
    "this person",
    "2 selected",
  ]);
  assert.deepEqual(contextWords(null), []);
});

test("a page that never named itself is called by its place in the menu", () => {
  // Several pages set no tab title; "/tags" is not a name anyone uses.
  assert.equal(sectionName("/tags"), "Tags");
  assert.equal(sectionName("/"), "Search");
  assert.equal(sectionName("/health"), "System");
  assert.equal(sectionName("/listings/abc"), "Listings");
  assert.equal(sectionName("/edit/abc"), "The editor");
  assert.deepEqual(contextWords({ path: "/tags" }), ["Tags"]);
  // Its own title wins when it has one.
  assert.deepEqual(contextWords({ path: "/health", title: "System health" }), ["System health"]);
});

test("a tool call says what was looked up and whether it worked", () => {
  assert.deepEqual(chipWords(chip()), { text: "Searched for “barn at dusk”", tone: "ok" });
  assert.deepEqual(chipWords(chip({ state: "running" })), {
    text: "Searching for “barn at dusk”…",
    tone: "busy",
  });
  assert.deepEqual(chipWords(chip({ state: "failed", ok: false })), {
    text: "Could not search for “barn at dusk”",
    tone: "bad",
  });
  // While Claude is still writing the call there is nothing to say it is for.
  assert.deepEqual(chipWords(chip({ state: "preparing", input: null })), {
    text: "Searching for…",
    tone: "busy",
  });
});

test("a call the turn left unanswered did not finish, and is not shown working", () => {
  const left = chipWords(chip({ state: "interrupted", ok: false }));
  assert.equal(left.text, "Did not finish: search for “barn at dusk”");
  assert.equal(left.tone, "bad");
});

test("every connector tool has words of its own", () => {
  const said = (name, input = {}) => chipWords(chip({ name, input })).text;
  assert.equal(said("overview"), "Looked over the catalogue");
  assert.equal(said("list_tags"), "Listed the tags");
  assert.equal(said("list_tags", { query: "barn" }), "Listed the tags matching “barn”");
  assert.equal(said("find_media", { tags: ["Silo", "Dusk"] }), "Found media tagged Silo, Dusk");
  assert.equal(said("get_media", { id: "1704c870-aaaa" }), "Read about item 1704c870");
  assert.equal(said("view_media", { id: "1704c870", ts_ms: 72400 }), "Looked at item 1704c870 at 1:12");
  assert.equal(said("review_queue"), "Read the tag suggestions waiting for review");
  assert.equal(
    said("propose_tags", { id: "1704c870", tags: ["Silo"] }),
    "Suggested the tags Silo for item 1704c870",
  );
  assert.equal(said("find_shots", { folder: "2026/Mill Lane" }), "Found shots in “2026/Mill Lane”");
  assert.equal(said("contact_sheet"), "Made a contact sheet");
  assert.equal(said("get_transcript", { id: "1704c870" }), "Read what is said in item 1704c870");
  assert.equal(said("build_rough_cut", { title: "Ledgerock 60" }), "Built the rough cut “Ledgerock 60”");
  assert.equal(said("build_selects"), "Built selects");
  // No tool name with an underscore in it is ever what the reader sees.
  for (const name of [
    "overview",
    "list_tags",
    "find_media",
    "search_media",
    "get_media",
    "view_media",
    "review_queue",
    "propose_tags",
    "api_review_status",
    "find_shots",
    "contact_sheet",
    "recent_cuts",
    "path_profiles",
    "get_media_files",
    "get_transcript",
    "get_scenes",
    "build_rough_cut",
    "build_selects",
    "start_folder_review",
  ]) {
    assert.doesNotMatch(said(name), /_/, name);
  }
  // A tool from a later phase reads as its name, made readable.
  assert.equal(said("start_folder_review"), "Ran “start folder review”");
});

test("a change is said in the sentence the reader was asked to agree to", () => {
  const ask = "Confirm the tag “Silo” on item 1704c870";
  const of = (state) => chipWords(chip({ name: "decide_tag", needs_confirm: true, ask, state }));
  assert.deepEqual(of("pending"), { text: `${ask}: waiting for your yes or no`, tone: "ask" });
  assert.deepEqual(of("running"), { text: `${ask}…`, tone: "busy" });
  assert.deepEqual(of("approved"), { text: `Done: ${ask}`, tone: "ok" });
  assert.deepEqual(of("declined"), { text: `Not done, you said no: ${ask}`, tone: "off" });
  assert.deepEqual(of("failed"), { text: `Did not work: ${ask}`, tone: "bad" });
  assert.deepEqual(of("interrupted"), { text: `Did not finish: ${ask}`, tone: "bad" });
});

test("the details of a request are short lines, however much was asked for", () => {
  const ids = Array.from({ length: 200 }, (_, n) => `id${n}`);
  assert.deepEqual(inputLines({ tag: "Silo", accept: ids, reject: [], note: "", really: true }), [
    { name: "tag", value: "Silo" },
    { name: "accept", value: "id0, id1, id2, id3, id4, id5 and 194 more" },
    { name: "really", value: "yes" },
  ]);
  assert.deepEqual(inputLines({ asset_ids: ["a"], limit: 0 }), [
    { name: "asset ids", value: "a" },
    { name: "limit", value: "0" },
  ]);
  assert.equal(inputLines({ text: "x".repeat(1000) })[0].value.length, 240);
  assert.deepEqual(inputLines(null), []);
});

test("a picture opens in the app's viewer only when it is known which item it is", () => {
  const whole = "1704c870-1111-4222-8333-444455556666";
  assert.deepEqual(pictureAsset({ name: "view_media", input: { id: whole } }), { assetId: whole });
  assert.deepEqual(pictureAsset({ name: "view_media", input: { id: whole, ts_ms: 12000 } }), {
    assetId: whole,
    startAt: 12,
  });
  // The short id Claude usually uses is not enough to open anything by.
  assert.equal(pictureAsset({ name: "view_media", input: { id: "1704c870" } }), null);
  // A contact sheet is many items in one picture.
  assert.equal(pictureAsset({ name: "contact_sheet", input: { asset: whole } }), null);
  // And an id handed to any other tool is not known to be an item's: a
  // later tool's `id` may be a listing or a person.
  assert.equal(pictureAsset({ name: "some_later_tool", input: { id: whole } }), null);
  assert.equal(pictureAsset({ name: "view_media", input: null }), null);
});

test("the line under a running turn says what Claude is doing", () => {
  const view = (more = {}) => ({ running: true, stranded: false, thinking: false, messages: [], ...more });
  assert.equal(workingWords(view({ running: false }), "idle", false), null);
  assert.equal(
    workingWords(view({ messages: [message({ role: "user", sending: true })] }), "live", false),
    "Sending…",
  );
  assert.equal(workingWords(view({ messages: [message({ role: "user" })] }), "live", false), "Waiting for Claude…");
  assert.equal(workingWords(view({ thinking: true }), "live", false), "Thinking…");
  assert.equal(workingWords(view({ messages: [message({ text: "I" })] }), "live", false), "Writing…");
  assert.equal(
    workingWords(view({ messages: [message({ tools: [chip({ state: "running" })] })] }), "live", false),
    "Working…",
  );
});

test("trouble hearing a turn is said as trouble hearing it, not as the turn failing", () => {
  const view = { running: true, stranded: false, thinking: true, messages: [] };
  assert.match(workingWords(view, "reconnecting", false), /Reconnecting… Claude is still working/);
  assert.match(workingWords(view, "watching", false), /still working/);
  assert.match(workingWords(view, "live", true), /^Stopping…/);
  // A turn the server lost is said first: nothing else about it is true.
  assert.match(workingWords({ ...view, stranded: true }, "watching", true), /cut off/);
});

test("an ordinary ending says nothing, a stop is quiet, and trouble is the server's sentence", () => {
  assert.equal(endWords(null), null);
  assert.equal(endWords({ reason: "end", message: null, kind: null }), null);
  assert.equal(endWords({ reason: "awaiting_confirmation", message: null, kind: null }), null);
  assert.deepEqual(endWords({ reason: "stopped", message: "Stopped.", kind: null }), {
    text: "Stopped.",
    tone: "quiet",
  });
  assert.deepEqual(
    endWords({ reason: "budget", message: "Monthly AI budget reached ($5.00).", kind: "budget" }),
    { text: "Monthly AI budget reached ($5.00).", tone: "bad" },
  );
  assert.deepEqual(endWords({ reason: "refusal", message: "Claude declined to answer that.", kind: null }), {
    text: "Claude declined to answer that.",
    tone: "bad",
  });
  // A reason with no sentence still gets one.
  assert.equal(endWords({ reason: "error", message: null, kind: null }).tone, "bad");
  assert.ok(endWords({ reason: "error", message: null, kind: null }).text.length > 10);
});

test("the box is not offered when a message would not be answered", () => {
  assert.equal(gate(null, true), null);
  assert.equal(gate(status(), false), null);

  const noKey = status({
    configured: false,
    problem: "The chat panel needs an Anthropic API key. An administrator can add one on the Security page.",
  });
  // Only an administrator can put it right, so only one is sent there.
  assert.deepEqual(gate(noKey, true), {
    sentence: noKey.problem,
    fix: { href: "/security#services", label: "Open the Claude settings on Security" },
    fault: true,
  });
  assert.equal(gate(noKey, false).fix, null);
  assert.equal(gate(noKey, false).sentence, noKey.problem);

  const off = status({ enabled: false, problem: "Claude is switched off for this server." });
  assert.notEqual(gate(off, true).fix, null);

  // Missing packages are not put right on any page of the app.
  const bare = status({ available: false, configured: false, problem: "The chat panel needs the connector's tools." });
  assert.equal(gate(bare, true).fix, null);
});

test("a read-only account is told it can read and not write", () => {
  const readOnly = gate(status({ can_chat: false }), false);
  assert.match(readOnly.sentence, /read-only/);
  assert.equal(readOnly.fault, false);
  assert.equal(readOnly.fix, null);
  // A fault with the server is said first: it is why nobody can write.
  const both = gate(status({ can_chat: false, configured: false, problem: "No key." }), false);
  assert.equal(both.sentence, "No key.");
});

test("why a message cannot go just now is said under the box", () => {
  const idle = { running: false, stranded: false };
  assert.equal(cannotSend({ view: idle, opening: false, draft: "hello" }), null);
  // An empty box is not a problem to announce; the button is simply off.
  assert.equal(cannotSend({ view: idle, opening: false, draft: "" }), null);
  assert.match(cannotSend({ view: idle, opening: true, draft: "hi" }), /Opening/);
  assert.match(
    cannotSend({ view: { running: true, stranded: false }, opening: false, draft: "hi" }),
    /when Claude has finished/,
  );
  assert.match(
    cannotSend({ view: { running: true, stranded: true }, opening: false, draft: "hi" }),
    /once the conversation is free/,
  );
  assert.equal(cannotSend({ view: idle, opening: false, draft: "x".repeat(MAX_MESSAGE_CHARS) }), null);
  assert.equal(
    cannotSend({ view: idle, opening: false, draft: "x".repeat(MAX_MESSAGE_CHARS + 1) }),
    "That message is too long (20,001 characters; the limit is 20,000).",
  );
});

test("a finished answer is read aloud once, and not all of a long one", () => {
  assert.equal(spoken("I found  three\nclips."), "Claude: I found three clips.");
  assert.equal(spoken(""), "Claude has finished.");
  const long = spoken("word ".repeat(400));
  assert.ok(long.length < MAX_SPOKEN_CHARS + 60);
  assert.match(long, /The rest is in the conversation\.$/);
});

test("a page's title is shown without the detail it carries for Claude, and never runs on", () => {
  assert.deepEqual(
    contextWords({
      path: "/tags",
      title: "Tags — reviewing “silo”, 25 suggestions waiting",
      tag: "silo",
      selection_count: 3,
    }),
    ["Tags", "tag “silo”", "3 selected"],
  );
  assert.equal(shownTitle("Search: red barn"), "Search: red barn");
  assert.equal(shownTitle("A".repeat(80)).length, 60);
  assert.ok(shownTitle("A".repeat(80)).endsWith("…"));
  // A title that is only detail falls back to the page's section.
  assert.deepEqual(contextWords({ path: "/browse", title: " — video only" }), ["Browse"]);
});

test("a confirm card names the items it would change, in words, and nothing the sentence says", () => {
  // The card's sentence already says the tag and the counts; under it the
  // reader needs the items themselves, to open and look at before saying
  // yes, not "accept 3fa85f64-5717-…" and "limit 0".
  const A = "3fa85f64-5717-4562-b3fc-2c963f66afa6";
  const B = "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d";
  const bulk = askDetails("decide_tags", { tag: "silo", accept: [A, "1704c870"], reject: [B] });
  assert.deepEqual(bulk.lines, []);
  assert.deepEqual(bulk.items, [
    { name: "Confirm the tag on", ids: [A, "1704c870"], openable: [A] },
    { name: "Reject the tag on", ids: [B], openable: [B] },
  ]);
  assert.deepEqual(askDetails("decide_tag", { id: A, tag: "silo", accept: false }).items, [
    { name: "Reject the tag on", ids: [A], openable: [A] },
  ]);
  assert.deepEqual(askDetails("start_api_review", { asset_ids: [], limit: 0 }), { lines: [], items: [] });
  assert.deepEqual(askDetails("start_api_review", { limit: 40 }).lines, [
    { name: "At most", value: "40 items, the newest without tags" },
  ]);
  assert.deepEqual(askDetails("start_api_review", { asset_ids: [A], limit: 5 }), {
    lines: [],
    items: [{ name: "Review", ids: [A], openable: [A] }],
  });
  // Arguments of the wrong kind are not a crash, and an unknown request
  // still shows what it was asked to do.
  assert.deepEqual(askDetails("decide_tags", { tag: "silo", accept: 3 }).items, []);
  assert.deepEqual(askDetails("rename_folder", { new_name: "Barns" }).lines, [{ name: "new name", value: "Barns" }]);
  assert.equal(itemName(A), "item 3fa85f64");
});
