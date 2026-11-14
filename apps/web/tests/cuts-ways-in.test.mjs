// Run with `npm test` (Node's own test runner; nothing to install).
//
// The other ways in to drafting a cut (src/lib/cuts.ts): the link a clip's
// page and a folder in Browse draw, the link Search's "Draft a cut…" follows
// with the ticked clips, and what the Cuts page reads back out of that
// address. What is handed over lives in the address and nowhere else, so a
// reload shows the same form; these check that nothing in it is dropped or
// made up on the way.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BRIEF_MAX,
  CLIPS_MOST,
  TICKS_KEPT,
  blankForm,
  browseDraftHref,
  draftAsksPicker,
  draftBody,
  draftFolderHref,
  draftTickedHref,
  handedOver,
  keepTicks,
  letGoBy,
  pickerOffers,
  ticksBack,
  ticksKey,
} from "../src/lib/cuts.ts";

const A = "0b4f3c1e-9a7d-4c2b-8e6f-1a2b3c4d5e6f";
const B = "1c5a4d2f-8b6e-4d3c-9f7a-2b3c4d5e6f70";
const C = "2d6b5e3a-7c5f-4e4d-8a8b-3c4d5e6f7081";

/** Read an address the way the Cuts page does (`useSearchParams`). */
function arriving(href) {
  return handedOver(new URL(href, "http://framefound.test").searchParams);
}

test("a folder's link opens the form with that folder, and a library's top draws no link", () => {
  assert.equal(
    draftFolderHref("lib-1", "2026/Pete Dye"),
    "/cuts?library=lib-1&folder=2026%2FPete%20Dye",
  );
  // Browse and an asset's path may carry slashes at either end; the Cuts
  // picker matches the folder as the server reads it.
  assert.equal(draftFolderHref("lib-1", "/2026/Pete Dye/"), "/cuts?library=lib-1&folder=2026%2FPete%20Dye");
  // A draft is always of a folder or of named clips, never a whole library.
  assert.equal(draftFolderHref("lib-1", ""), null);
  assert.equal(draftFolderHref("lib-1", "/"), null);
  assert.equal(draftFolderHref("", "2026"), null);
});

test("a folder named with & or # arrives whole on the Cuts page", () => {
  const href = draftFolderHref("lib-1", "Clients/Smith & Sons #2");
  const params = new URL(href, "http://framefound.test").searchParams;
  assert.equal(params.get("library"), "lib-1");
  assert.equal(params.get("folder"), "Clients/Smith & Sons #2");
});

test("Search hands over only the ticked clips, with the search as the start of a brief", () => {
  const href = draftTickedHref(
    [
      { asset_id: A, media_type: "video" },
      { asset_id: B, media_type: "video" },
    ],
    "  golden hour  ",
  );
  assert.equal(href, `/cuts?clips=${A},${B}&brief=golden%20hour`);
  const handed = arriving(href);
  assert.deepEqual(handed.assetIds, [A, B]);
  assert.equal(handed.label, "2 clips ticked in Search");
  assert.equal(handed.note, null);
  assert.equal(handed.brief, "golden hour");
});

test("nothing is handed over while no clip is ticked: photographs alone are not footage for a cut", () => {
  assert.equal(draftTickedHref([], "drone"), null);
  assert.equal(draftTickedHref([{ asset_id: A, media_type: "image" }], "drone"), null);
});

test("ticked photographs are counted, so the form says what became of them", () => {
  const href = draftTickedHref(
    [
      { asset_id: A, media_type: "video" },
      { asset_id: B, media_type: "image" },
      { asset_id: C, media_type: "image" },
    ],
    "",
  );
  // No search, no brief in the address.
  assert.equal(href, `/cuts?clips=${A}&ticked=3`);
  const handed = arriving(href);
  assert.equal(handed.label, "1 clip ticked in Search");
  assert.equal(handed.note, "2 of the 3 ticked items are photographs; the Editor works from the one clip.");
  assert.equal(handed.brief, "");

  const one = arriving(`/cuts?clips=${A},${B}&ticked=3`);
  assert.equal(one.note, "1 of the 3 ticked items is a photograph; the Editor works from the 2 clips.");
});

test("the form's request for handed-over clips names them one by one", () => {
  const handed = arriving(`/cuts?clips=${A},${B}&brief=dusk`);
  const made = draftBody({ ...blankForm(), brief: handed.brief, assetIds: handed.assetIds });
  assert.deepEqual(made.body.scope, { kind: "selection", asset_ids: [A, B] });
});

test("an address with no clips in it hands nothing over, and the folder link is read instead", () => {
  assert.equal(arriving("/cuts"), null);
  assert.equal(arriving("/cuts?library=lib-1&folder=2026"), null);
  assert.equal(arriving("/cuts?clips="), null);
});

test("a pasted or trimmed address keeps only what are clips' ids, once each", () => {
  const handed = arriving(`/cuts?clips=${A.toUpperCase()}, ${A},not-an-id,${B.slice(0, 20)},${C}&ticked=junk`);
  assert.deepEqual(handed.assetIds, [A, C]);
  // A count that cannot be read says nothing about photographs.
  assert.equal(handed.note, null);
  // Fewer ticked than clips named cannot be: no note either.
  assert.equal(arriving(`/cuts?clips=${A},${B}&ticked=1`).note, null);
});

test("no more clips are handed over than one draft may name, and the brief is cut to what is read", () => {
  const many = Array.from({ length: CLIPS_MOST + 5 }, (_, n) => ({
    asset_id: `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    media_type: "video",
  }));
  const href = draftTickedHref(many, "x".repeat(BRIEF_MAX + 50));
  const handed = arriving(href);
  assert.equal(handed.assetIds.length, CLIPS_MOST);
  assert.equal(handed.brief.length, BRIEF_MAX);
  // The five left over are not called photographs.
  assert.equal(handed.note, null);
});

test("Browse offers a draft of a shoot at once, and of a folder of folders only once the form's picker lists it", () => {
  const shoot = { here: { clips: 4 }, inside: { clips: 4 } };
  const year = { here: { clips: 0 }, inside: { clips: 7 } };
  // Every clip directly in it: offered as it always was, picker or not.
  assert.equal(browseDraftHref("lib-1", "2026/Pete Dye", shoot), "/cuts?library=lib-1&folder=2026%2FPete%20Dye");
  assert.equal(draftAsksPicker(shoot), false);
  // "GELCO › 2026" holds only shoot folders. A folder draft takes everything
  // beneath it, which the picker counts — and leaves out when it holds more
  // shots than one draft reads (a client's whole folder). So the picker decides.
  assert.equal(draftAsksPicker(year), true);
  assert.equal(browseDraftHref("lib-1", "2026", year, true), "/cuts?library=lib-1&folder=2026");
  assert.equal(browseDraftHref("lib-1", "2026", year, false), null);
  // Not answered yet, or it could not be asked: not offered.
  assert.equal(browseDraftHref("lib-1", "2026", year), null);
  assert.equal(browseDraftHref("lib-1", "2026", year, null), null);
  // Clips of its own and in folders inside it: the draft takes all of them.
  assert.equal(browseDraftHref("lib-1", "Footage", { here: { clips: 2 }, inside: { clips: 5000 } }, false), null);
  // Nothing to draft from: photographs only, or an empty folder.
  assert.equal(browseDraftHref("lib-1", "2026/Stills", { here: { clips: 0 }, inside: { clips: 0 } }, true), null);
  // Clips at a library's top are still not a folder.
  assert.equal(browseDraftHref("lib-1", "", { here: { clips: 9 }, inside: { clips: 9 } }), null);
});

test("the picker lists a folder only by its library and its exact path", () => {
  const rows = [
    { library_id: "lib-1", path: "2026/Pete Dye" },
    { library_id: "lib-2", path: "2026" },
  ];
  assert.equal(pickerOffers(rows, "lib-1", "2026/Pete Dye"), true);
  // The same path in another library is another folder.
  assert.equal(pickerOffers(rows, "lib-1", "2026"), false);
  // A sub-folder listed is not its parent listed.
  assert.equal(pickerOffers([{ library_id: "lib-1", path: "2026/Pete Dye/Drone" }], "lib-1", "2026/Pete Dye"), false);
  assert.equal(pickerOffers([], "lib-1", "2026"), false);
});

test("choosing a folder instead is a step Back undoes; a draft asked for, or Try again, is not", () => {
  assert.equal(letGoBy("unselect"), "push");
  assert.equal(letGoBy("drafted"), "replace");
  assert.equal(letGoBy("retried"), "replace");
});

test("Back to a search finds its rows still ticked, and a new search starts with none", () => {
  const kept = new Map();
  const golf = ticksKey("  golf ", "", "");
  keepTicks(kept, golf, new Set([A, C]));
  // The same search again, its results in another order.
  assert.deepEqual([...ticksBack(kept, ticksKey("golf", "", ""), [B, C, A])].sort(), [A, C].sort());
  // Another search, or the same words with another filter, is another set.
  assert.equal(ticksBack(kept, ticksKey("drone", "", ""), [A, C]).size, 0);
  assert.equal(ticksBack(kept, ticksKey("golf", "", "video"), [A, C]).size, 0);
  assert.equal(ticksBack(kept, ticksKey("golf", "lib-1", ""), [A, C]).size, 0);
});

test("a ticked row the search no longer finds is not ticked out of sight", () => {
  const kept = new Map();
  const key = ticksKey("golf", "", "");
  keepTicks(kept, key, [A, B]);
  assert.deepEqual([...ticksBack(kept, key, [B, C])], [B]);
});

test("unticking everything forgets the search, and only the latest searches are kept", () => {
  const kept = new Map();
  const key = ticksKey("golf", "", "");
  keepTicks(kept, key, [A]);
  keepTicks(kept, key, []);
  assert.equal(kept.has(key), false);
  // Results arriving with no search behind them keep nothing.
  keepTicks(kept, "", [A]);
  assert.equal(kept.size, 0);

  for (let n = 0; n <= TICKS_KEPT; n++) keepTicks(kept, ticksKey(`search ${n}`, "", ""), [A]);
  assert.equal(kept.size, TICKS_KEPT);
  assert.equal(ticksBack(kept, ticksKey("search 0", "", ""), [A]).size, 0);
  assert.equal(ticksBack(kept, ticksKey(`search ${TICKS_KEPT}`, "", ""), [A]).size, 1);
  // Ticking an older search again makes it the newest, so it outlives the rest.
  keepTicks(kept, ticksKey("search 1", "", ""), [A, B]);
  keepTicks(kept, ticksKey("one more", "", ""), [A]);
  assert.equal(ticksBack(kept, ticksKey("search 1", "", ""), [A, B]).size, 2);
  assert.equal(ticksBack(kept, ticksKey("search 2", "", ""), [A]).size, 0);
});
