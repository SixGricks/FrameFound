// The words and sums of colour looks: the clip card on a video's page
// (components/ClipLookCard.tsx) and the local model's suggestions on a
// listing (components/LocalEditPanel.tsx), kept apart from the pages so they
// can be tested.
//
// Nothing is imported, not even types, so `node --test` loads this file as it
// is (tests/clipLooks.test.mjs). The shapes below are the few fields each
// helper reads; the full answers are typed in lib/api.ts, and TypeScript
// checks that those fit these wherever a page hands one over.
//
// Two rules hold everywhere in this file. Colour is judged by eye: nothing
// here shows a colour difference, a score or a rank, and no look is put
// before another (the three tiles always come in the server's order). And
// every sentence the server sends is shown as it comes: the page lays out
// what the server found, it never decides anything about a look itself.

// ---- the shapes read here ------------------------------------------------------------

export type LookSource = "model" | "learned" | "none";
export type ChosenSource = LookSource | "custom";
export type Tone = "ok" | "warn" | "bad" | null;

/** A request to the local model, as the server shows it (clip or listing). */
interface Job {
  state: string;
  message: string;
  not_before: string | null;
  done: number;
  total: number;
  cancelling: boolean;
}

/** The eight sliders a LUT carries, as the editor names them. */
export type SliderKey =
  | "exposure"
  | "contrast"
  | "temperature"
  | "tint"
  | "saturation"
  | "vibrance"
  | "shadows"
  | "auto_wb";

export type Sliders = Record<SliderKey, number>;

interface Provenance {
  model: string | null;
  model_revision: string | null;
  variant: string;
  runtime: string | null;
  runtime_version: string | null;
  prompt_version: string | null;
}

interface Candidate {
  source: LookSource;
  available: boolean;
  recipe: Partial<Sliders>;
  measured_at_ms: number | null;
  basis: "clips" | "photos" | null;
  examples: number;
  version: string;
  /** When the look was stored; a suggestion asked for again moves it on. */
  made_at: string | null;
}

interface Chosen {
  chosen_at: string | null;
  chosen_by: string | null;
}

interface Folder {
  clips: number;
  chosen: number;
  suggested: number;
}

interface LocalEditTone {
  from: "look" | "preset";
  examples: number;
}

interface LastRun {
  failed: number;
  unreadable: number;
  first_problem: string | null;
}

interface Suggestion {
  item_id: string;
  large: boolean;
  state: string;
}

// ---- small words ---------------------------------------------------------------------

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "12 Nov" on this browser's clock, with the year when it is not this one. */
export function shortDay(iso: string | null | undefined, nowMs: number = Date.now()): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const day = `${at.getDate()} ${MONTHS[at.getMonth()]}`;
  return at.getFullYear() === new Date(nowMs).getFullYear() ? day : `${day} ${at.getFullYear()}`;
}

/**
 * A moment in a clip: "0:07", "12:40", "1:02:03". Tenths are added only when
 * the moment is not on a whole second ("0:21.5"), so two stills a fraction
 * apart are never two buttons with the same label.
 */
export function momentLabel(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "0:00";
  const tenths = Math.round(ms / 100);
  const whole = Math.floor(tenths / 10);
  const rest = tenths % 10;
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const seconds = String(whole % 60).padStart(2, "0");
  const clock = hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}` : `${minutes}:${seconds}`;
  return rest ? `${clock}.${rest}` : clock;
}

/** The words over the card's strip of stills. It looks like the page's
 *  scene strip, where a press plays the clip from there; this one moves the
 *  looks, not the player. */
export const MOMENTS_LINE = "Compare the looks at another moment (the player stays where it is):";

/** Those words on the page as it is: a clip that cannot play here has no
 *  player (its poster and the reason stand in its place), so there is none
 *  to say stays where it is. */
export function momentsLine(hasPlayer: boolean): string {
  return hasPlayer ? MOMENTS_LINE : "Compare the looks at another moment:";
}

/** A still's button in that strip, as a screen reader says it. */
export function momentButtonLabel(ms: number): string {
  return `Show the looks at ${momentLabel(ms)}`;
}

// ---- a request to the local model ----------------------------------------------------

const ACTIVE = new Set(["queued", "waiting", "running"]);

// The guard names the states, not just "a request": a guard of plain `Job`
// told TypeScript that a request which is not on its way is not a request
// at all, so a failed one could not be read after the check.
type ActiveJob = Job & { state: "queued" | "waiting" | "running" };

export function isActive(job: Job | null | undefined): job is ActiveJob {
  return !!job && ACTIVE.has(job.state);
}

/**
 * The line beside a request's spinner, or its ending. The server's sentence
 * is used whenever it has one (why it waits and until when, why it failed);
 * the page's own words fill in only where the server says nothing. A
 * listing's request names the photograph it is on ("IMG_0398.jpg, 13 of
 * 40"); a clip's has one still and says so.
 */
export function jobLine(job: Job): string {
  if (job.cancelling && ACTIVE.has(job.state)) return "Stopping…";
  switch (job.state) {
    case "queued":
      return "Waiting its turn…";
    case "waiting":
      return job.message || "Waiting…";
    case "running":
      if (job.message && job.total > 1) {
        const at = Math.min(job.done + 1, job.total);
        return `Looking at ${job.message}, ${at} of ${job.total}…`;
      }
      // A listing's request before it has named its first photograph (a
      // clip's has one still, so a total of one).
      return job.total > 1 ? "Looking at the photographs…" : "Looking at the still…";
    case "failed":
      return job.message || "It did not finish.";
    case "cancelled":
      return job.message || "Cancelled.";
    default:
      return "";
  }
}

/** How often a card reads itself again while a request moves. */
export const FAST_MS = 2_500;
export const SLOW_MS = 15_000;
// A wait that ends within this is watched quickly, so the request is seen to
// start rather than a quarter of a minute later.
const NEAR_MS = 2 * 60_000;

/**
 * How long until a card reads itself again: quickly while its request is
 * queued, running, being stopped or about to stop waiting; slowly while it
 * waits for a time far off (the overnight window, a pause) — reading every
 * 2.5 s for hours shows nothing new; never once nothing is on its way, since
 * every press reads the card again at once.
 */
export function pollEvery(job: Job | null | undefined, nowMs: number = Date.now()): number | null {
  if (!isActive(job)) return null;
  if (job.cancelling || job.state !== "waiting") return FAST_MS;
  const at = job.not_before ? Date.parse(job.not_before) : NaN;
  return Number.isFinite(at) && at - nowMs < NEAR_MS ? FAST_MS : SLOW_MS;
}

/**
 * `pollEvery` for L1's `rereads()`, given the request of the last answer the
 * card has (`latest?.suggest.job`, `latest?.job`). After a failed read it
 * always comes back, at most `SLOW_MS` later — a press may just have started
 * a request and the read after it was the one that failed.
 */
export function readAgainIn(job: Job | null | undefined, failed: boolean, nowMs: number = Date.now()): number | null {
  const every = pollEvery(job, nowMs);
  if (failed) return every === null ? SLOW_MS : Math.min(every, SLOW_MS);
  return every;
}

// ---- asking the local model about a clip ---------------------------------------------

/** The model tile's button: "Suggest a look" until there is a suggestion,
 *  then "Ask again". */
export function suggestLabel(hasSuggestion: boolean): string {
  return hasSuggestion ? "Ask again" : "Suggest a look";
}

/**
 * Whether the AI models page is where to go about a refused Suggest: models
 * can run here but none has the Looker role. Where models cannot run at all
 * (the container server) that page cannot change it, so no link is offered.
 */
export function needsLooker(suggest: { mode: string; ok: boolean }): boolean {
  return !suggest.ok && suggest.mode !== "unavailable";
}

interface Looks {
  suggest: { job: Job | null };
  candidates: Pick<Candidate, "source" | "available" | "version" | "made_at">[];
}

export const SUGGESTED_LINE = "The local model has suggested a look.";
export const CANCELLED_LINE = "The request was cancelled. Nothing was stored.";

/**
 * What the card says when a request it saw on its way has ended between two
 * reads: a new suggestion has arrived, or the request was cancelled. null
 * otherwise — a failure is shown by the tile itself, in the server's words,
 * and nothing is said about a request the card never saw moving (a page
 * opened after it ended shows the result and needs no news).
 *
 * A finished request and a cancelled one both read back with no request, so
 * the suggestion itself tells them apart. Its version is a sum of the look
 * alone, and a model asked again about the same still often answers the
 * same sliders: the moment it was stored is compared too, or a request that
 * finished would be called cancelled.
 */
export function suggestEnded(before: Looks | null | undefined, after: Looks): string | null {
  if (!before || !isActive(before.suggest.job) || isActive(after.suggest.job)) return null;
  if (after.suggest.job?.state === "failed") return null;
  const was = before.candidates.find((candidate) => candidate.source === "model");
  const now = after.candidates.find((candidate) => candidate.source === "model");
  if (
    now?.available &&
    (!was?.available || was.version !== now.version || (was.made_at ?? null) !== (now.made_at ?? null))
  ) {
    return SUGGESTED_LINE;
  }
  return CANCELLED_LINE;
}

/**
 * What the card says when Cancel is answered. A request that was waiting or
 * queued ends at once and comes back cancelled: it is said now, since no
 * later read shows a request that has ended. One under way comes back still
 * running and being stopped, and its ending is said by `suggestEnded`.
 */
export function cancelSaid(job: Job): string | null {
  return job.state === "cancelled" ? CANCELLED_LINE : null;
}

// ---- the sliders ---------------------------------------------------------------------

export interface SliderSpec {
  key: SliderKey;
  label: string;
  min: number;
  max: number;
  step: number;
}

/** Adjust's eight range inputs, in the editor's order, with the server's
 *  ranges (`LookIn`). Shadows only darken: a LUT cannot lift them. */
export const SLIDERS: ReadonlyArray<SliderSpec> = [
  { key: "exposure", label: "Exposure", min: -2, max: 2, step: 0.05 },
  { key: "contrast", label: "Contrast", min: -1, max: 1, step: 0.02 },
  { key: "temperature", label: "Temperature", min: -1, max: 1, step: 0.02 },
  { key: "tint", label: "Tint", min: -1, max: 1, step: 0.02 },
  { key: "saturation", label: "Saturation", min: -1, max: 1, step: 0.02 },
  { key: "vibrance", label: "Vibrance", min: -1, max: 1, step: 0.02 },
  { key: "shadows", label: "Darken shadows", min: -1, max: 0, step: 0.02 },
  { key: "auto_wb", label: "Remove colour cast", min: 0, max: 1, step: 0.02 },
];

export const NO_SLIDERS: Sliders = {
  exposure: 0,
  contrast: 0,
  temperature: 0,
  tint: 0,
  saturation: 0,
  vibrance: 0,
  shadows: 0,
  auto_wb: 0,
};

function held(spec: SliderSpec, value: unknown): number {
  const number = typeof value === "number" && Number.isFinite(value) ? value : 0;
  return Math.min(spec.max, Math.max(spec.min, number));
}

/** A look's recipe as all eight sliders, for Adjust to start from: a slider
 *  the look leaves alone is 0, anything else a LUT cannot carry is dropped,
 *  and every value is kept inside what the server accepts. */
export function slidersOf(recipe: Partial<Sliders> | null | undefined): Sliders {
  const out = { ...NO_SLIDERS };
  for (const spec of SLIDERS) out[spec.key] = held(spec, recipe?.[spec.key]);
  return out;
}

/** The body of a preview or a choice of one's own: the sliders that move,
 *  held in range. Nothing moved is `{}`, the clip as it was shot. */
export function slidersBody(sliders: Partial<Sliders>): Partial<Sliders> {
  const out: Partial<Sliders> = {};
  for (const spec of SLIDERS) {
    const value = held(spec, sliders[spec.key]);
    if (value !== 0) out[spec.key] = value;
  }
  return out;
}

/** One slider's figure as the editor shows it: exposure in stops ("+0.25
 *  stops"), the rest from −100 to 100. */
export function sliderFigure(key: SliderKey, value: number): string {
  const sign = (n: number) => (n > 0 ? `+${n}` : n < 0 ? `−${Math.abs(n)}` : "0");
  if (key === "exposure") {
    const stops = Math.round(value * 100) / 100;
    return `${sign(Number(stops.toFixed(2)))} ${Math.abs(stops) === 1 ? "stop" : "stops"}`;
  }
  return sign(Math.round(value * 100));
}

/** "Show the numbers": the sliders a look moves, in the editor's order and
 *  words. Empty for no change. */
export function sliderRows(recipe: Partial<Sliders> | null | undefined): { key: SliderKey; label: string; value: string }[] {
  const rows: { key: SliderKey; label: string; value: string }[] = [];
  for (const spec of SLIDERS) {
    const value = recipe?.[spec.key];
    if (typeof value !== "number" || !Number.isFinite(value) || Math.round(value * 100) === 0) continue;
    rows.push({ key: spec.key, label: spec.label, value: sliderFigure(spec.key, value) });
  }
  return rows;
}

/**
 * Whether a look changes anything, so that a `.cube` can be made of it. The
 * server refuses the file for a look that moves no slider ("The clip as it
 * was shot needs no LUT."), and an empty look is an ordinary answer: a model
 * that asked only to lift the shadows, or to leave the clip as it was shot,
 * is stored with nothing a LUT carries. A link to that file would save the
 * refusal as the download, so the page says `NO_LUT_LINE` instead.
 *
 * Any value that is not zero counts, however small: the server keeps
 * three decimals, more than "Show the numbers" prints.
 */
export function needsLut(recipe: Partial<Sliders> | null | undefined): boolean {
  if (!recipe) return false;
  return Object.values(recipe).some((value) => typeof value === "number" && Number.isFinite(value) && value !== 0);
}

/** Said where a look's download would be when the look changes nothing. */
export const NO_LUT_LINE = "No LUT needed: this look changes nothing.";

/**
 * Where Adjust starts when it is opened: the look whose tile was pressed
 * last (its picture, the flip view, or Use this look), else the chosen look,
 * else nothing moved. `from` names it for the line above the sliders.
 */
export function adjustStart(
  candidates: Pick<Candidate, "source" | "available" | "recipe" | "basis">[],
  pressed: LookSource | null,
  chosen: { recipe: Partial<Sliders> } | null | undefined,
): { from: string; sliders: Sliders } {
  const tile = pressed ? candidates.find((candidate) => candidate.source === pressed && candidate.available) : undefined;
  if (tile) return { from: tileTitle(tile), sliders: slidersOf(tile.recipe) };
  if (chosen) return { from: "The chosen look", sliders: slidersOf(chosen.recipe) };
  return { from: "No change", sliders: { ...NO_SLIDERS } };
}

/** What "Use my version" stores: the sliders as a look of one's own, or —
 *  with nothing moved — No change, which needs no LUT (a look of one's own
 *  that changes nothing would offer a download the server refuses). */
export function adjustChoice(sliders: Partial<Sliders>): { source: "custom" | "none"; recipe: Partial<Sliders> } {
  const recipe = slidersBody(sliders);
  return Object.keys(recipe).length ? { source: "custom", recipe } : { source: "none", recipe: {} };
}

/** How long Adjust waits after the last slider move before asking for its
 *  picture, as the editor does. */
export const ADJUST_WAIT_MS = 250;

// ---- the three tiles -----------------------------------------------------------------

/** The tiles' titles, in the order the server gives them. */
export function tileTitle(candidate: Pick<Candidate, "source" | "basis">): string {
  if (candidate.source === "model") return "The local model's suggestion";
  if (candidate.source === "none") return "No change";
  return candidate.basis === "photos" ? "Your photograph look" : "Your look";
}

/** Where the learned look comes from, under its title; "" for the others or
 *  when it is not learned yet (its note says so). */
export function learnedFrom(candidate: Pick<Candidate, "source" | "basis" | "examples" | "available">): string {
  if (candidate.source !== "learned" || !candidate.available) return "";
  if (candidate.basis === "clips") {
    return `learned from the ${plural(candidate.examples, "clip", "clips")} you have chosen a look for`;
  }
  if (candidate.basis === "photos") {
    return `from ${plural(candidate.examples, "shipped photograph", "shipped photographs")}`;
  }
  return "";
}

/** The buttons of the flip view, in the tiles' order. */
export const FLIP_LABELS: Record<LookSource, string> = {
  model: "The model",
  learned: "Your look",
  none: "No change",
};

/** ← and → in the flip view: the next look that can be shown, round the
 *  ends. `sources` are the available ones, in the tiles' order. */
export function flipTo(sources: LookSource[], current: LookSource, step: 1 | -1): LookSource {
  const at = sources.indexOf(current);
  const next = at < 0 ? sources[0] : sources[(at + step + sources.length) % sources.length];
  return next ?? current;
}

/** "A LUT cannot carry: lifting the shadows, window pull, local contrast." —
 *  null when the look carries whole. */
export function cannotCarry(leftOut: string[] | null | undefined): string | null {
  const words = (leftOut ?? []).filter(Boolean);
  return words.length ? `A LUT cannot carry: ${words.join(", ")}.` : null;
}

/** What made a model's suggestion, in one faint line:
 *  "mlx-community/gemma-4-e4b-it-4bit · build 475b9088 · mlx-vlm 0.7.12 ·
 *  prompt colour-1". Only what the server recorded is said. */
export function provenanceLine(provenance: Provenance | null | undefined): string {
  if (!provenance) return "";
  const name = provenance.model ? (provenance.variant ? `${provenance.model} (${provenance.variant})` : provenance.model) : "";
  const parts = [
    name,
    provenance.model_revision ? `build ${provenance.model_revision.slice(0, 8)}` : "",
    [provenance.runtime, provenance.runtime_version].filter(Boolean).join(" "),
    provenance.prompt_version ? `prompt ${provenance.prompt_version}` : "",
  ];
  return parts.filter(Boolean).join(" · ");
}

/**
 * The line under the still picker: where each look's white balance was
 * measured, when any look on show evens it out ("auto_wb"). A look made on
 * another still says its own moment, so the line never claims one moment
 * for all. null when no look measures white balance.
 */
export function whiteBalanceLine(candidates: Pick<Candidate, "available" | "recipe" | "measured_at_ms">[]): string | null {
  const moments: number[] = [];
  for (const candidate of candidates) {
    if (!candidate.available || candidate.measured_at_ms == null) continue;
    if (!((candidate.recipe.auto_wb ?? 0) > 0)) continue;
    if (!moments.includes(candidate.measured_at_ms)) moments.push(candidate.measured_at_ms);
  }
  if (moments.length === 0) return null;
  const at = moments
    .sort((a, b) => a - b)
    .map(momentLabel)
    .join(" and ");
  const whose = moments.length === 1 ? "Each look's white balance was" : "The looks' white balance was";
  return `${whose} measured at ${at}. Look at other moments to see how it holds.`;
}

/**
 * The moment a preview may be asked for: `ts` when it is one of the clip's
 * listed stills (or the reference), else the reference. The server answers
 * any other moment with 400, so a picker left on a still that is no longer
 * listed falls back rather than breaking every picture.
 */
export function shownMoment(
  looks: { stills: { ts_ms: number }[]; reference_ts_ms: number | null },
  ts: number | null | undefined,
): number | null {
  if (ts != null && (ts === looks.reference_ts_ms || looks.stills.some((still) => still.ts_ms === ts))) return ts;
  return looks.reference_ts_ms;
}

/** "Chosen 12 Nov by dj@example.com." */
export function chosenLine(chosen: Chosen, nowMs: number = Date.now()): string {
  const day = shortDay(chosen.chosen_at, nowMs);
  const by = chosen.chosen_by ? ` by ${chosen.chosen_by}` : "";
  return `Chosen${day ? ` ${day}` : ""}${by}.`;
}

/**
 * The tile that carries "chosen": the one showing the very look that was
 * chosen — the same source at the same version. A choice is a copy made when
 * it was pressed, so once the model is asked again or the learned look moves
 * on, the tile of that name shows something else and must not claim it; nor
 * does any tile hold a look set by hand. null then, and the card shows the
 * chosen look by itself (`chosenApart`).
 */
export function chosenTile(
  candidates: Pick<Candidate, "source" | "available" | "version">[],
  chosen: { source: ChosenSource; version: string } | null | undefined,
): LookSource | null {
  if (!chosen || chosen.source === "custom") return null;
  const tile = candidates.find((candidate) => candidate.source === chosen.source);
  return tile && tile.available && tile.version === chosen.version ? tile.source : null;
}

/** What the chosen look is when no tile shows it (see `chosenTile`). */
export function chosenApart(source: ChosenSource): string {
  if (source === "custom") return "Your own version, set with the sliders.";
  if (source === "model") return "An earlier suggestion from the local model.";
  if (source === "learned") return "Your look as it was when it was chosen.";
  return "No change.";
}

/** The card's line under its heading. */
export const CARD_LINE = "Three versions of the same moment. Choose by eye.";

/** Said in the flip view, where the picture is shown larger than it was made. */
export const FLIP_NOTE = "Stills are 480 pixels wide; judge colour here and sharpness in the player above.";

/** That note on the page as it is: where the clip cannot play there is no
 *  player above to judge sharpness in, only its poster and the reason. */
export function flipNote(hasPlayer: boolean): string {
  return hasPlayer ? FLIP_NOTE : "Stills are 480 pixels wide; judge colour here.";
}

/** "Using the LUT in Premiere", paragraph by paragraph. */
export const LUT_IN_PREMIERE: ReadonlyArray<string> = [
  "The file changes colour only: it does what the picture above shows to every frame of the clip. " +
    "In Premiere, select the clip, open Lumetri Color, and load the file under Creative › Look › Browse " +
    "(or Basic Correction › Input LUT › Browse).",
  "It is made for ordinary (Rec. 709) video as it looks in FrameFound's preview. If the clip looks " +
    "different in Premiere than it does here before any LUT — log or HDR footage that Premiere converts — " +
    "the look will land differently.",
  "A LUT cannot straighten, lift shadows region by region or replace a sky; those stay in the photograph editor.",
];

/** The LUT's two sizes, as the size select offers them. */
export const LUT_SIZES: ReadonlyArray<{ size: number; label: string }> = [
  { size: 33, label: "Standard (33)" },
  { size: 65, label: "Finer (65) — a 7 MB file, for strong looks" },
];

// ---- the folder ----------------------------------------------------------------------

// A clip with no colour look is said to have "no look suggested yet", never
// to be "not looked at": that is the review's phrase (lib/looks.ts), for a
// clip no local model has scored. The final QA found Browse calling a folder
// "all looked at" while this card, on a clip of that folder with a model's
// score, said "2 not looked at" and marked the other clip "not looked at".

/** Clips of the folder with no suggestion and no choice. */
export function withNoLook(folder: Folder): number {
  return Math.max(0, folder.clips - folder.chosen - folder.suggested);
}

/** "This folder has 12 clips: 3 with a chosen look, 5 with a suggestion
 *  waiting for you, 4 with no look suggested yet." The counts include the
 *  clip on the page. */
export function folderLine(folder: Folder): string {
  const parts = [
    `${folder.chosen} with a chosen look`,
    `${folder.suggested} with a suggestion waiting for you`,
    `${withNoLook(folder)} with no look suggested yet`,
  ];
  return `This folder has ${plural(folder.clips, "clip", "clips")}: ${parts.join(", ")}.`;
}

/** The folder's Suggest button, or null when every clip has a look
 *  suggested or chosen. */
export function folderSuggestLabel(folder: Folder): string | null {
  const count = withNoLook(folder);
  if (count === 0) return null;
  return count === 1
    ? "Suggest a look for the 1 clip that has none"
    : `Suggest looks for the ${count} clips that have none`;
}

/** What the folder's Suggest did (`{queued, already, no_still}`). */
export function folderAskedLine(answer: { queued: number; already: number; no_still: number }): string {
  const lines: string[] = [];
  lines.push(
    answer.queued === 0
      ? "No clip needed asking."
      : `Asked for ${plural(answer.queued, "look", "looks")}. They appear here as they arrive.`,
  );
  if (answer.already) {
    lines.push(
      answer.already === 1
        ? "1 clip already had a look or one on its way."
        : `${answer.already} clips already had a look or one on its way.`,
    );
  }
  if (answer.no_still) {
    lines.push(
      answer.no_still === 1
        ? "1 clip has no still yet, so nothing could be shown to the model."
        : `${answer.no_still} clips have no still yet, so nothing could be shown to the model.`,
    );
  }
  return lines.join(" ");
}

/**
 * Why the folder's zip is not offered, or null when it may be asked for.
 *
 * Only a folder with no chosen look at all is certain to be refused. The
 * page cannot know more than that: `chosen` counts every clip with a choice,
 * "No change" included, while the zip holds only looks that change
 * something, and the finer size's limit is on those. So the page asks for
 * the zip and shows the server's sentence when it is refused
 * (`api.folderLooksFile`), rather than guessing from a count that is not the
 * zip's — a guess that both offered a zip of nothing and refused one the
 * server would make.
 */
export function folderZipBlocked(folder: Pick<Folder, "chosen">): string | null {
  return folder.chosen <= 0 ? "None chosen yet." : null;
}

/**
 * The name to save a downloaded file under, from the answer's
 * Content-Disposition: the RFC 5987 name (`filename*=UTF-8''…`, the real
 * one, accents and all) first, then the plain `filename="…"`, else
 * `fallback`. Never a path: anything up to a slash is dropped.
 */
export function fileNameOf(disposition: string | null | undefined, fallback: string): string {
  const header = disposition ?? "";
  let name = "";
  const encoded = /filename\*\s*=\s*([^']*)'[^']*'([^;]+)/i.exec(header);
  if (encoded?.[2]) {
    try {
      name = decodeURIComponent(encoded[2].trim());
    } catch {
      name = "";
    }
  }
  if (!name) {
    const plain = /filename\s*=\s*"([^"]*)"|filename\s*=\s*([^;]+)/i.exec(header);
    name = (plain?.[1] ?? plain?.[2] ?? "").trim();
  }
  name = name.slice(Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\")) + 1).trim();
  return name || fallback;
}

/** The heading of the folder's list of clips: "The other 11 clips in this
 *  folder", and when the server sent only some, how many of them. */
export function folderListTitle(folder: Pick<Folder, "clips"> & { others: unknown[] }): string {
  const others = Math.max(0, folder.clips - 1);
  const listed = folder.others.length;
  if (listed < others) return `The first ${listed} of the other ${others} clips in this folder, by name`;
  return others === 1 ? "The other clip in this folder" : `The other ${others} clips in this folder`;
}

/** After the folder's Suggest: read the card again slowly until as many
 *  clips as were asked about have an answer, or for at most this long. */
export const FOLDER_WATCH_MS = 20 * 60_000;

export interface FolderWatch {
  until: number;
  /** Clips with no look that would be left once every request has answered. */
  left: number;
}

/** What to watch for after the folder's Suggest asked about `queued` clips;
 *  null when it asked about none. */
export function watchFolder(folder: Folder, queued: number, nowMs: number = Date.now()): FolderWatch | null {
  if (queued <= 0) return null;
  return { until: nowMs + FOLDER_WATCH_MS, left: Math.max(0, withNoLook(folder) - queued) };
}

/** How long until the card reads itself again for the folder's sake: slowly
 *  while answers are still to come, never once they are in or the watch has
 *  run out (requests waiting for the overnight window come in the morning,
 *  and the next visit reads them). */
export function folderEvery(watch: FolderWatch | null, folder: Folder | null | undefined, nowMs: number = Date.now()): number | null {
  if (!watch || !folder || nowMs >= watch.until) return null;
  return withNoLook(folder) > watch.left ? SLOW_MS : null;
}

/** The sooner of two re-reads, either of which may be "never" (null). */
export function sooner(a: number | null, b: number | null): number | null {
  if (a === null) return b;
  if (b === null) return a;
  return Math.min(a, b);
}

/** The pill beside a clip of the folder. */
export function folderPill(state: string): { tone: Tone; label: string } {
  if (state === "chosen") return { tone: "ok", label: "chosen" };
  if (state === "suggested") return { tone: "warn", label: "suggestion waiting" };
  return { tone: null, label: "no look suggested yet" };
}

// ---- a listing's photographs ---------------------------------------------------------

// Below these a correction is not worth naming (the server's dead bands are
// 0.2° and 0.05; these only keep a rounding from printing "0.0°").
const ROTATE_SHOWN = 0.05;
const KEYSTONE_SHOWN = 0.01;

/**
 * A proposal's framing in words: "Rotate 1.2° counterclockwise, straighten
 * the verticals a little", "Leave the framing as it is". A positive rotate
 * turns the picture counterclockwise (the engine's convention); a positive
 * keystone pulls in verticals that lean together toward the top, the usual
 * case with the camera tilted up.
 */
export function straightenText(rotate: number, keystone: number): string {
  const parts: string[] = [];
  if (Number.isFinite(rotate) && Math.abs(rotate) >= ROTATE_SHOWN) {
    const way = rotate > 0 ? "counterclockwise" : "clockwise";
    parts.push(`Rotate ${Math.abs(rotate).toFixed(1)}° ${way}`);
  }
  if (Number.isFinite(keystone) && Math.abs(keystone) >= KEYSTONE_SHOWN) {
    const size = Math.abs(keystone);
    const how = size < 0.3 ? " a little" : size >= 0.6 ? " a lot" : "";
    const what = keystone > 0 ? "the verticals" : "verticals that spread toward the top";
    parts.push(`${parts.length ? "straighten" : "Straighten"} ${what}${how}`);
  }
  return parts.length ? parts.join(", ") : "Leave the framing as it is";
}

export function skyText(worthReplacing: boolean): string {
  return worthReplacing ? "Sky: would replace it" : "Sky: fine as it is";
}

/** The rows the list shows: those still waiting for a decision, in the
 *  server's (gallery) order. Applied and dismissed ones leave the list. */
export function waitingSuggestions<T extends Pick<Suggestion, "state">>(suggestions: T[]): T[] {
  return suggestions.filter((suggestion) => suggestion.state === "proposed");
}

/** The ticks a list starts with: every waiting row, less the large ones,
 *  which are ticked only after looking. */
export function firstTicks(suggestions: Suggestion[]): string[] {
  return waitingSuggestions(suggestions)
    .filter((suggestion) => !suggestion.large)
    .map((suggestion) => suggestion.item_id);
}

/**
 * A shift-click: every row from `from` to `to` (in the list's order, either
 * way round) is ticked when `on`, else unticked; the rest are left as they
 * are. A row that is not in the list is a plain click on `to`.
 */
export function tickRange(order: string[], ticked: string[], from: string | null, to: string, on: boolean): string[] {
  const a = from === null ? -1 : order.indexOf(from);
  const b = order.indexOf(to);
  const span = a < 0 || b < 0 ? [to] : order.slice(Math.min(a, b), Math.max(a, b) + 1);
  const next = new Set(ticked);
  for (const id of span) {
    if (on) next.add(id);
    else next.delete(id);
  }
  return order.filter((id) => next.has(id));
}

/** Where Apply's tone comes from, in a few words. */
export function toneWords(tone: LocalEditTone): string {
  if (tone.from === "look") {
    return `your learned look (${plural(tone.examples, "shipped photograph", "shipped photographs")})`;
  }
  return "the listing preset";
}

/** "37 photographs · tone from your learned look (429 shipped photographs)" —
 *  the faint line under the list's heading. */
export function localEditLine(edit: { proposed: number; tone: LocalEditTone }): string {
  return `${plural(edit.proposed, "photograph", "photographs")} · tone from ${toneWords(edit.tone)}`;
}

/** "3 photographs have no suggestion — first: IMG_0412.jpg: …", or null when
 *  the last run dealt with every photograph. */
export function missingLine(lastRun: LastRun | null | undefined): string | null {
  if (!lastRun) return null;
  const count = lastRun.failed + lastRun.unreadable;
  if (count <= 0) return null;
  const head = count === 1 ? "1 photograph has no suggestion" : `${count} photographs have no suggestion`;
  return lastRun.first_problem ? `${head} — first: ${lastRun.first_problem}` : `${head}.`;
}

/** Whether the list is shown at all: something waits for a decision, the
 *  model is at work, or the last run left photographs out. */
export function panelShown(edit: { proposed: number; job: Job | null; last_run: LastRun | null }): boolean {
  return edit.proposed > 0 || isActive(edit.job) || missingLine(edit.last_run) !== null;
}

/** The rail's button: "Looking 12/40…" while the model is at work, "Ask
 *  again" once it has suggested anything, else "Ask the local model". */
export function askLabel(edit: { job: Job | null; suggestions: unknown[] }): string {
  if (isActive(edit.job)) {
    return edit.job.total > 0 ? `Looking ${edit.job.done}/${edit.job.total}…` : "Looking…";
  }
  return edit.suggestions.length > 0 ? "Ask again" : "Ask the local model";
}

/** Apply's button; "Tick at least one photograph." stands beside it at 0. */
export function applyLabel(ticked: number): string {
  return ticked === 1 ? "Apply to the 1 ticked" : `Apply to the ${ticked} ticked`;
}

export const TICK_ONE = "Tick at least one photograph.";

/** The notice when Apply starts. The tone is said with where it was made:
 *  nothing about these photographs leaves the building. */
export function applyNotice(ticked: number, tone: LocalEditTone): string {
  const where =
    tone.from === "look"
      ? `Tone comes from your learned look (${plural(tone.examples, "shipped photograph", "shipped photographs")}, on this machine).`
      : "Tone comes from the listing preset.";
  return `Applying the local model's suggestions to ${plural(ticked, "photograph", "photographs")}. ${where} Nothing left the building.`;
}

// ---- the listing page: asking, ticking, applying (L4·W4) -----------------------------

/** The panel's heading. */
export const PANEL_TITLE = "What the local model suggests";

/** The rail's words round "Ask the local model", under Auto-edit. */
export const ASK_OR_LINE = "— or, on this Mac, without the Claude API —";
export const ASK_EXPLAINED =
  "It looks at each photograph for straightening, the sky and a name. Nothing changes until you choose.";

/** Apply's two choices for a picked sky, as the select offers them. */
export const SKY_WHERE: ReadonlyArray<{ value: "suggested" | "all"; label: string }> = [
  { value: "suggested", label: "only where the model would replace it" },
  { value: "all", label: "wherever there is sky" },
];

/** The sky Apply would put in: the sky picked under Auto-edit (its shown
 *  name), or how to pick one. */
export function skyChoiceLine(shownName: string | null | undefined): string {
  return shownName ? `Sky: ${shownName}` : "Sky: none chosen (pick one under Auto-edit to replace skies)";
}

/** What Ask did: how many photographs the model will look at. */
export function askedLine(toLookAt: number): string {
  return `The local model will look at ${plural(toLookAt, "photograph", "photographs")}. Nothing changes until you choose.`;
}

/** Over the list in demo mode, where no model made what it shows. */
export const LISTING_DEMO_LINE = "Demonstration only. These suggestions are made up, not made by a model.";

/** Said when the model stops looking. Each photograph's suggestion is stored
 *  as it is made, so stopping part way keeps the ones already made. */
export const ASK_STOPPED_LINE = "Stopped. Suggestions already made are kept.";
export const ASK_FINISHED_LINE = "The local model has finished looking. Nothing changes until you choose.";

/**
 * What the page says when the request it saw on its way has ended between
 * two reads. The server shows no request once one is done or cancelled, so
 * an ending seen while it was being stopped is a stop, any other a finish.
 * null when nothing was seen moving, or when it failed — the failure is
 * shown in the server's words where the request was.
 */
export function askEnded(before: Job | null | undefined, after: Job | null | undefined): string | null {
  if (!isActive(before) || isActive(after)) return null;
  if (!after) return before.cancelling ? ASK_STOPPED_LINE : ASK_FINISHED_LINE;
  return after.state === "cancelled" ? ASK_STOPPED_LINE : null;
}

/** What Dismiss did. */
export function dismissedLine(count: number): string {
  return count === 1 ? "1 suggestion dismissed." : `${count} suggestions dismissed.`;
}

/** Asked before Dismiss: it puts aside every suggestion in the list, ticked
 *  or not, and making them again is a fresh run of the model on the Mac. */
export function dismissConfirm(count: number): string {
  return count === 1
    ? "Dismiss the 1 suggestion? Asking the local model again makes it anew."
    : `Dismiss all ${count} suggestions, ticked or not? Asking the local model again makes them anew.`;
}

/**
 * How many of the suggestions sent to Dismiss its answer shows dismissed.
 * The sentence counts what the server did, not the rows that were on screen:
 * a row another tab had already applied is not counted as dismissed here.
 */
export function dismissedOf(sent: string[], answer: Array<{ item_id: string; state: string }>): number {
  const asked = new Set(sent);
  return answer.filter((row) => asked.has(row.item_id) && row.state === "dismissed").length;
}

/** Beside a disabled Apply and Dismiss while the model looks; see `askHoldsList`. */
export const APPLY_WAITS_FOR_ASK =
  "Apply and Dismiss wait while the local model looks at the photographs (Cancel beside Ask stops it).";

/**
 * Whether Apply and Dismiss wait for the listing's request: while it is
 * queued or running, a suggestion asked for again can replace a ticked one
 * before Apply's run reads it — Apply names only which rows, so it would
 * write a suggestion nobody looked at. A request waiting for a time far off
 * (the night window) does not hold them: Apply's run is done long before it
 * starts, and the list would otherwise be locked for the whole day.
 */
export function askHoldsList(job: Job | null | undefined): boolean {
  return job?.state === "queued" || job?.state === "running";
}

/** Beside a disabled Apply while another kind of auto-edit runs here. */
export const APPLY_WAITS_FOR_RUN = "Auto-edit is running on this listing; Apply waits for it.";

/**
 * Beside Apply while the live run is this listing's own local Apply. The
 * ticked rows stay in the list until the run has written them, so it is
 * said that they are being applied — not that Apply waits for some other
 * run, which read as if the press had done nothing. `total` is null when
 * what the run covers is not known in this tab.
 */
export function applyingLine(done: number, total: number | null): string {
  const tail = "Each photograph's tile says “✓ edited” when it is done, and its row then leaves this list.";
  return total === null
    ? `Applying the suggestions: ${done} done so far. ${tail}`
    : `Applying the suggestions: ${done} of ${total} done. ${tail}`;
}

/**
 * What a local Apply's run is remembered to cover (asset ids), for the
 * gallery's "editing…" pills and the count on Auto-edit. The server edits
 * only the ticked rows still waiting and says how many it queued; when that
 * is fewer than were sent (another tab applied or dismissed some since this
 * list was read) which ones it left out is not known here, so nothing is
 * remembered (null) and only the photographs already done are marked.
 */
export function appliedTargets(assetIds: string[], queued: number): string[] | null {
  return queued === assetIds.length ? assetIds : null;
}

/**
 * What Apply tells the server the operator saw: each chosen row's `made_at`,
 * exactly as the list read it. The model can make a photograph's suggestion
 * again between the last read and the press (another tab asked, or a
 * request still under way finished it); Apply used to send only which rows,
 * and the run wrote the new suggestion, which nobody had looked at. A row
 * with no time is left out, and the server applies it unchecked, as before.
 */
export function madeAtShown(rows: { item_id: string; made_at: string | null }[]): Record<string, string> {
  const shown: Record<string, string> = {};
  for (const row of rows) if (row.made_at) shown[row.item_id] = row.made_at;
  return shown;
}

/** What the rail's focus rule last saw; see `askFocus`. */
export interface AskFocusSeen {
  active: boolean;
  /** How many presses in the rail have settled. */
  settled: number;
  /** A request this rail's press started (or stopped) is still on its way. */
  watching: boolean;
}

/**
 * Where the rail's keyboard focus goes after a change: to Cancel or Ask —
 * whichever is there to press — once a press there has settled, and when a
 * request that press started ends (Cancel goes, and the browser would drop
 * focus to the top of the page). Never on the first answer (`before` null):
 * a request already waiting for the night when the page opens put focus on
 * Cancel, and Space — meant to scroll — cancelled it. Nor when a request
 * starts or ends on the server while nobody pressed anything here.
 */
export function askFocus(
  before: AskFocusSeen | null,
  active: boolean,
  settled: number,
): { focus: "ask" | "cancel" | null; seen: AskFocusSeen } {
  const target = active ? "cancel" : "ask";
  if (!before) return { focus: null, seen: { active, settled, watching: false } };
  if (settled !== before.settled) return { focus: target, seen: { active, settled, watching: active } };
  if (active !== before.active) {
    return {
      focus: before.watching ? target : null,
      seen: { active, settled, watching: before.watching && active },
    };
  }
  return { focus: null, seen: before };
}

interface TickRow {
  item_id: string;
  large: boolean;
  state: string;
  made_at: string | null;
}

/** The ticks of the list, and which suggestions they were decided for. */
export interface Ticks {
  ticked: string[];
  /** One key per waiting suggestion already drawn: item and when it was made. */
  seen: string[];
}

export const NO_TICKS: Ticks = { ticked: [], seen: [] };

function tickKey(row: TickRow): string {
  return `${row.item_id}@${row.made_at ?? ""}`;
}

/**
 * The ticks after a new read. A suggestion seen for the first time starts
 * ticked unless it is large (so one that arrives while the model works, or
 * one asked for again, starts as `firstTicks` would have it); one already
 * seen keeps what the operator made of it; one that is no longer waiting
 * (applied, dismissed) drops out. The ticks come in the list's order.
 */
export function mergeTicks(before: Ticks, suggestions: TickRow[]): Ticks {
  const seen = new Set(before.seen);
  const was = new Set(before.ticked);
  const waiting = waitingSuggestions(suggestions);
  const ticked = waiting
    .filter((row) => (seen.has(tickKey(row)) ? was.has(row.item_id) : !row.large))
    .map((row) => row.item_id);
  return { ticked, seen: waiting.map(tickKey) };
}

/**
 * Which photographs (asset ids) a live auto-edit run is working on, for the
 * gallery's "editing…" pills and the count on the button. Every photograph
 * for the other modes. A local run edits only what was ticked: the ones this
 * tab asked for when it is the same run (`started_at` matches), else null —
 * not known here (another tab, another browser), so only the photographs
 * already done are marked rather than every one said to be in hand.
 */
export function runTargets(
  mode: string,
  imageIds: string[],
  startedAt: string | null,
  remembered: { startedAt: string; assetIds: string[] } | null,
): string[] | null {
  if (mode !== "local") return imageIds;
  if (!remembered || !startedAt || remembered.startedAt !== startedAt) return null;
  const here = new Set(imageIds);
  return remembered.assetIds.filter((id) => here.has(id));
}

/** The Auto-edit button while a run edits: "Editing 12/34…", or, when what
 *  the run covers is not known here, how many are done so far. */
export function editingLabel(done: number, total: number | null): string {
  return total === null ? `Editing… ${done} done` : `Editing ${done}/${total}…`;
}

/** A proposal's picture as the suggestion would leave it, at an address that
 *  changes when the suggestion does (asked for again), so the browser never
 *  shows the old picture under the new words. */
export function proposalPicture(url: string, madeAt: string | null): string {
  if (!madeAt) return url;
  return `${url}${url.includes("?") ? "&" : "?"}v=${encodeURIComponent(madeAt)}`;
}
