// Run with `npm test` (Node's own test runner; nothing to install).
//
// Which build: what the Add card says about a repository that holds several
// GGUF builds, and the line that keeps an empty llama.cpp row from reading as
// a fault. Every figure is the server's for that build; these pin the page's
// own words, the order of the list, and what the Download button names.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BUILDS_INTRO,
  BUILDS_SHOWN,
  buildText,
  buildsInOrder,
  downloadLabel,
  runtimeNeeded,
} from "../src/lib/localModels.ts";

const MB = 1024 ** 2;
const GB = 1024 ** 3;

const SETTINGS = {
  day_budget_gb: 8,
  night_budget_gb: 20,
  night_start: "21:30",
  night_end: "05:30",
};

function files(model, pictures = 812 * MB) {
  const found = [{ path: `demo-vision-${model}.gguf`, size: 3 * GB, part: "model" }];
  if (pictures) found.push({ path: "mmproj-demo-vision-F16.gguf", size: pictures, part: "pictures" });
  found.push({ path: "README.md", size: 2324, part: "text" });
  return found;
}

function build(id, size, over = {}) {
  return {
    id,
    label: `${id} — x`,
    quant: id,
    bits: 4,
    size_bytes: size,
    memory_gb: 3.8,
    fits: "day",
    recommended: false,
    takes: ["text", "image"],
    note: "",
    files: files(id),
    ...over,
  };
}

// The demo's vision repository as the server lists it: suggested first,
// then the others in its own order — not by size.
const VISION = [
  build("Q4_K_M", 3_341_026_438, { recommended: true }),
  build("F16", 8_619_072_006, { memory_gb: 9.7, fits: "night" }),
  build("Q8_0", 4_981_494_918, { memory_gb: 5.6 }),
];

function check(over = {}) {
  return {
    repo_id: "google/demo-vision-gguf",
    revision: "7".repeat(40),
    revision_short: "7777777",
    build_date: "2026-10-01T00:00:00Z",
    ok: true,
    verdict: "This model can be added.",
    checks: [],
    problems: [],
    warnings: [],
    notes: [],
    access: "open",
    size_bytes: 3_341_026_438,
    memory_gb: 3.8,
    fits: "day",
    takes: ["text", "image"],
    runtime: { name: "llama-cpp", label: "llama.cpp", installed: true },
    variants: VISION,
    variant: "Q4_K_M",
    existing: null,
    ...over,
  };
}

test("the builds are listed smallest first, whatever order the server sent", () => {
  assert.deepEqual(
    buildsInOrder(check()).map((variant) => variant.id),
    ["Q4_K_M", "Q8_0", "F16"],
  );
});

test("builds of the same size keep the server's order", () => {
  const same = [
    build("UD-IQ4_NL", 21 * GB),
    build("UD-IQ2_M", 20 * GB, { recommended: true }),
    build("UD-IQ4_XS", 21 * GB),
    build("UD-Q3_K_M", 21 * GB),
  ];
  assert.deepEqual(
    buildsInOrder(check({ variants: same, variant: "UD-IQ2_M" })).map((variant) => variant.id),
    ["UD-IQ2_M", "UD-IQ4_NL", "UD-IQ4_XS", "UD-Q3_K_M"],
  );
});

test("a repository of one build (every MLX one) has no list to show", () => {
  assert.deepEqual(buildsInOrder(check({ variants: [], variant: null })), []);
  assert.deepEqual(buildsInOrder(check({ variants: [VISION[0]], variant: "Q4_K_M" })), []);
});

test("the list sorts a copy and leaves the check as the server sent it", () => {
  const card = check();
  buildsInOrder(card);
  assert.deepEqual(
    card.variants.map((variant) => variant.id),
    ["Q4_K_M", "F16", "Q8_0"],
  );
});

test("a row says its size, its memory, when it can run and whether it is the suggestion", () => {
  assert.deepEqual(buildText(VISION[0], SETTINGS), {
    size: "3.1 GB",
    memory: "about 3.8 GB of memory",
    fits: {
      tone: "ok",
      label: "by day",
      sentence: "Fits the daytime budget (8 GB), so it can run at any hour.",
    },
    suggested: true,
    note: "",
    reads: "Includes the part that lets it read pictures (812 MB).",
  });
  const f16 = buildText(VISION[1], SETTINGS);
  assert.equal(f16.fits.label, "overnight only");
  assert.equal(f16.fits.tone, "warn");
  assert.equal(f16.suggested, false);
  assert.equal(f16.memory, "about 9.7 GB of memory");
});

test("a build that fits no budget says so with the same pill as an installed model", () => {
  const big = buildText(build("Q8_0", 35 * GB, { memory_gb: 39.4, fits: "never" }), SETTINGS);
  assert.equal(big.fits.tone, "bad");
  assert.equal(big.fits.label, "over budget");
  assert.match(big.fits.sentence, /overnight budget \(20 GB\)/);
});

test("a very small build carries the server's sentence about it", () => {
  const note = "A very small build: it answers noticeably worse than a 4-bit one.";
  assert.equal(buildText(build("UD-IQ2_M", 20 * GB, { note }), SETTINGS).note, note);
});

test("a build with no part that reads pictures says it reads text only", () => {
  const textOnly = build("Q4_K_M", 3 * GB, { files: files("Q4_K_M", 0), takes: ["text"] });
  assert.equal(buildText(textOnly, SETTINGS).reads, "This build reads text only.");
});

test("Download names the build it fetches only when there is a choice", () => {
  assert.equal(downloadLabel(check()), "Download Q4_K_M (3.1 GB)");
  assert.equal(
    downloadLabel(check({ variant: "Q8_0", size_bytes: 4_981_494_918 })),
    "Download Q8_0 (4.6 GB)",
  );
  assert.equal(
    downloadLabel(check({ variants: [], variant: null, size_bytes: 4.8 * GB })),
    "Download (4.8 GB)",
  );
});

test("the list scrolls in its own box past eight builds, and says why builds differ", () => {
  assert.equal(BUILDS_SHOWN, 8);
  assert.match(BUILDS_INTRO, /several builds of the same model/);
  assert.match(BUILDS_INTRO, /Smaller ones need less memory/);
});

test("an empty llama.cpp row says nothing needs it rather than looking broken", () => {
  const llama = { name: "llama-cpp", label: "llama.cpp", installed: null, models: 0 };
  assert.equal(runtimeNeeded(llama), "Nothing you have added needs it.");
  assert.equal(runtimeNeeded({ ...llama, models: 1 }), null);
  assert.equal(runtimeNeeded({ ...llama, installed: "0.9.0" }), null);
});
