// The words and sums of Compare (app/models/compare/ and the Compare*.tsx
// components, and the card on the AI models page), kept apart from the pages
// so they can be tested.
//
// Nothing is imported, not even types, so `node --test` loads this file as it
// is (tests/compare.test.mjs). The shapes below are the few fields each helper
// reads; the full answers are typed in lib/api.ts, and TypeScript checks that
// those fit these wherever a page hands one over.
//
// Two rules hold everywhere in this file. A figure is a count of agreement
// with what a person already decided — never a grade, a rank or a percentage
// score — and no model is ordered or marked by one. And every sentence the
// server sends is shown as it comes: the page lays out what the server
// found, it never decides anything about a model itself.

// ---- the shapes read here ------------------------------------------------------------

export type CompareState = "running" | "waiting" | "finished" | "stopped" | "empty";
export type CompareWhat = "shots" | "photos" | "both";
export type CompareKind = "cuts" | "folder" | "listing";
export type Tone = "ok" | "warn" | "bad" | null;

interface Limits {
  min_size: number;
  max_size: number;
  max_models: number;
}

interface ModelChoice {
  id: string;
  can_compare: boolean;
  reason: string | null;
  seconds_hint: number | null;
}

interface Summary {
  state: string;
  size: number;
  photos: number;
  shots: number;
  verdict: string;
  verdict_model: string | null;
  models: unknown[];
}

interface Cut {
  id: string;
  title: string;
  client: string;
  source: string;
  created_at: string | null;
  pieces: number;
}

interface Folder {
  library_name: string;
  path: string;
  photos: number;
  clips: number;
  clips_with_shots: number;
}

interface Preview {
  ok: boolean;
  what: string | null;
  items: number;
  photos: number;
  shots: number;
  clips: number;
  pool: { photos: number; shots: number; clips: number };
  truth: {
    tags: { footage: number; yes: number; no: number };
    used: { used: number; unused: number; cuts: { title: string }[] };
  };
  left_out: { sentence: string }[];
  notes: string[];
}

// ---- the list and the card -----------------------------------------------------------

/** How often the list of comparisons is read again: often while a model is
 *  running (its pill moves), slowly otherwise, never where models cannot run
 *  (nothing there can move). */
export const LIST_FAST_MS = 5_000;
export const LIST_SLOW_MS = 30_000;

export function listEvery(
  latest: { mode: string; comparisons: { state: string }[] } | null,
  failed: boolean,
): number | null {
  if (!latest) return failed ? LIST_SLOW_MS : null;
  if (latest.mode === "unavailable") return null;
  return latest.comparisons.some((one) => one.state === "running") ? LIST_FAST_MS : LIST_SLOW_MS;
}

/** A comparison's state pill. "waiting" is not called "tonight": a column
 *  also waits while local AI is paused or the model server is down. */
export function statePill(state: string): { tone: Tone; label: string } {
  switch (state) {
    case "running":
      return { tone: "warn", label: "running" };
    case "waiting":
      return { tone: "warn", label: "waiting" };
    case "finished":
      return { tone: "ok", label: "finished" };
    case "stopped":
      return { tone: "bad", label: "stopped" };
    case "empty":
      return { tone: null, label: "no models" };
    default:
      return { tone: null, label: state };
  }
}

/** What was decided, in words; null when nothing was. */
export function decisionText(summary: Pick<Summary, "verdict" | "verdict_model">): string | null {
  if (summary.verdict === "chosen") {
    return summary.verdict_model
      ? `${summary.verdict_model} looks`
      : "a model looks (no longer in it)";
  }
  if (summary.verdict === "none") return "none good enough yet";
  return null;
}

function plural(count: number, one: string, many: string): string {
  return `${count.toLocaleString("en-GB")} ${count === 1 ? one : many}`;
}

/** "60 shots", "40 photographs", "30 shots and 30 photographs". */
export function sampleText(counts: { photos: number; shots: number; size?: number }): string {
  const parts: string[] = [];
  if (counts.shots) parts.push(plural(counts.shots, "shot", "shots"));
  if (counts.photos) parts.push(plural(counts.photos, "photograph", "photographs"));
  if (parts.length) return parts.join(" and ");
  return plural(counts.size ?? 0, "item", "items");
}

/** "60 shots · 2 models", the card's line under a comparison's name. */
export function summaryLine(summary: Summary): string {
  return `${sampleText(summary)} · ${plural(summary.models.length, "model", "models")}`;
}

// ---- dates ---------------------------------------------------------------------------

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "9 Nov" on this browser's calendar, "9 Nov 2025" in another year. */
export function shortDay(iso: string | null | undefined, nowMs: number = Date.now()): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const day = `${at.getDate()} ${MONTHS[at.getMonth()]}`;
  return at.getFullYear() === new Date(nowMs).getFullYear() ? day : `${day} ${at.getFullYear()}`;
}

// ---- 1 · What to compare on ----------------------------------------------------------

export const SOURCE_CARDS: ReadonlyArray<{ kind: CompareKind; label: string; text: string }> = [
  {
    kind: "cuts",
    label: "Shots used in cuts",
    text:
      "The shots of a finished video, and the ones from the same folders that were left out. " +
      "The only sample that can check a model's scores.",
  },
  {
    kind: "folder",
    label: "A folder",
    text: "Everything in one folder of a library, and in the folders inside it.",
  },
  {
    kind: "listing",
    label: "A listing",
    text: "The photographs of one property listing.",
  },
];

export const NO_CUTS =
  "No cuts are recorded yet. A cut is recorded when Claude builds one through the connector, " +
  "or when a finished timeline is imported.";

/** Who made a cut, in words: "imported timeline", "built with Claude". */
export function cutMaker(source: string): string {
  if (source === "mcp") return "built with Claude";
  if (source === "import") return "imported timeline";
  return source;
}

/** "Brookside Quick 30 - Finished Work - Rev 2 · imported timeline · 9 Nov ·
 *  11 pieces" — the title apart, so it can be set in its own weight. */
export function cutLine(cut: Cut, nowMs: number = Date.now()): { title: string; rest: string } {
  const rest = [cutMaker(cut.source), shortDay(cut.created_at, nowMs), plural(cut.pieces, "piece", "pieces")]
    .filter(Boolean)
    .join(" · ");
  return { title: cut.title, rest };
}

/** The recorded cuts whose title holds `filter` (any case), grouped by who
 *  they were for, in the order the server gave them (newest first). Cuts
 *  with no client are grouped last, under "No client". */
export function cutsByClient<T extends Cut>(cuts: T[], filter: string): { client: string; cuts: T[] }[] {
  const wanted = filter.trim().toLowerCase();
  const groups = new Map<string, T[]>();
  for (const cut of cuts) {
    if (wanted && !cut.title.toLowerCase().includes(wanted)) continue;
    const client = cut.client.trim();
    const listed = groups.get(client);
    if (listed) listed.push(cut);
    else groups.set(client, [cut]);
  }
  const named = [...groups.entries()].filter(([client]) => client);
  const unnamed = groups.get("");
  return [
    ...named.map(([client, listed]) => ({ client, cuts: listed })),
    ...(unnamed ? [{ client: "No client", cuts: unnamed }] : []),
  ];
}

/** A folder's place, "GELCO › Brookside Country Club/2026"; a library's
 *  root is "GELCO › the whole library". */
export function folderPlace(folder: Pick<Folder, "library_name" | "path">): string {
  return `${folder.library_name} › ${folder.path || "the whole library"}`;
}

/** "42 photographs · 18 clips (16 with shots)". */
export function folderCounts(folder: Folder): string {
  const parts: string[] = [];
  parts.push(plural(folder.photos, "photograph", "photographs"));
  const clips = plural(folder.clips, "clip", "clips");
  parts.push(folder.clips ? `${clips} (${folder.clips_with_shots.toLocaleString("en-GB")} with shots)` : clips);
  return parts.join(" · ");
}

export const WHAT_LABELS: Record<CompareWhat, string> = {
  shots: "Shots",
  photos: "Photographs",
  both: "Both",
};

/** The three "Look at" buttons for a folder; a side with nothing to show is
 *  blocked, with the reason. */
export function whatChoices(folder: Folder): { what: CompareWhat; label: string; blocked: string | null }[] {
  const noShots = folder.clips_with_shots
    ? null
    : folder.clips
      ? "None of its clips has shot analysis yet."
      : "It holds no clips.";
  const noPhotos = folder.photos ? null : "It holds no photographs.";
  return [
    { what: "shots", label: WHAT_LABELS.shots, blocked: noShots },
    { what: "photos", label: WHAT_LABELS.photos, blocked: noPhotos },
    { what: "both", label: WHAT_LABELS.both, blocked: noShots ?? noPhotos },
  ];
}

/** What a newly chosen folder looks at: both when it has both, otherwise
 *  the side it has; null when it has neither. */
export function defaultWhat(folder: Folder): CompareWhat | null {
  const open = whatChoices(folder).filter((choice) => !choice.blocked);
  if (open.some((choice) => choice.what === "both")) return "both";
  return open[0]?.what ?? null;
}

/** "130 Davis Rd Auction — 14 photographs". */
export function listingLine(listing: { name: string; item_count: number }): string {
  return `${listing.name} — ${plural(listing.item_count, "photograph", "photographs")}`;
}

// ---- the request the builder sends ---------------------------------------------------

export type CompareSource =
  | { kind: "folder"; library_id: string; folder: string }
  | { kind: "listing"; listing_id: string }
  | { kind: "cuts"; cut_ids: string[]; neighbours: boolean }
  | { kind: "comparison"; comparison_id: string };

export interface SampleBody {
  source: CompareSource;
  what: CompareWhat | null;
  size: number;
  stills: 1 | 3;
  truth_cut_ids: string[] | null;
}

/** Everything the builder holds, as plain values. */
export interface BuilderState {
  kind: CompareKind | null;
  cutIds: string[];
  neighbours: boolean;
  folder: { library_id: string; path: string } | null;
  what: CompareWhat | null;
  /** The cuts that touch the folder (from the first preview of it), and
   *  which of them the owner unticked. */
  folderCutIds: string[];
  untickedCutIds: string[];
  listingId: string | null;
  sizeText: string;
  stills: 1 | 3;
}

export const SIZE_PROBLEM = (limits: Pick<Limits, "min_size" | "max_size">) =>
  `A sample is between ${limits.min_size} and ${limits.max_size} items.`;

/** The size as a whole number within the limits, else null. */
export function sizeOf(text: string, limits: Pick<Limits, "min_size" | "max_size">): number | null {
  const trimmed = text.trim();
  if (!/^\d{1,4}$/.test(trimmed)) return null;
  const size = Number(trimmed);
  return size >= limits.min_size && size <= limits.max_size ? size : null;
}

/** What is still missing before there is a sample to ask about; null when
 *  the request can be sent. */
export function sampleMissing(state: BuilderState, limits: Pick<Limits, "min_size" | "max_size">): string | null {
  if (!state.kind) return "Choose what to compare on.";
  if (state.kind === "cuts" && !state.cutIds.length) return "Tick at least one cut.";
  if (state.kind === "folder" && !state.folder) return "Find a folder and choose it.";
  if (state.kind === "folder" && !state.what) return "That folder has nothing to compare on.";
  if (state.kind === "listing" && !state.listingId) return "Choose a listing.";
  if (sizeOf(state.sizeText, limits) === null) return SIZE_PROBLEM(limits);
  return null;
}

/**
 * The body of the preview (and, with the models, of Start); null while
 * something is missing (`sampleMissing` says what). Cuts always look at
 * shots and a listing at photographs, so their `what` is left to the
 * server. A folder's counted cuts are sent only once one is unticked:
 * null means "every finished cut that touches the folder", which is also
 * what a cut recorded since the first preview should be.
 */
export function sampleBody(state: BuilderState, limits: Pick<Limits, "min_size" | "max_size">): SampleBody | null {
  if (sampleMissing(state, limits)) return null;
  const size = sizeOf(state.sizeText, limits) as number;
  const common = { size, stills: state.stills };
  if (state.kind === "cuts") {
    return {
      source: { kind: "cuts", cut_ids: [...state.cutIds], neighbours: state.neighbours },
      what: null,
      truth_cut_ids: null,
      ...common,
    };
  }
  if (state.kind === "listing") {
    return {
      source: { kind: "listing", listing_id: state.listingId as string },
      what: null,
      truth_cut_ids: null,
      ...common,
    };
  }
  const folder = state.folder as { library_id: string; path: string };
  const unticked = new Set(state.untickedCutIds);
  const truth = unticked.size ? state.folderCutIds.filter((id) => !unticked.has(id)) : null;
  return {
    source: { kind: "folder", library_id: folder.library_id, folder: folder.path },
    what: state.what,
    truth_cut_ids: truth,
    ...common,
  };
}

/** One key per distinct request, so an answer can be matched to the
 *  request it answers (the builder drops one that is no longer current). */
export function bodyKey(body: SampleBody | null): string {
  return body ? JSON.stringify(body) : "";
}

// ---- 2 · How many --------------------------------------------------------------------

/** Minutes for `items` at `seconds` each, rounded up, at least one. */
export function minutes(seconds: number, items: number): number {
  return Math.max(1, Math.ceil((seconds * Math.max(0, items)) / 60));
}

function durationWords(mins: number): string {
  if (mins < 90) return plural(mins, "minute", "minutes");
  return `${Math.round(mins / 60)} hours`;
}

/** The faint line beside the size: how long a model may take, going by how
 *  long each ticked model took before. A range when they differ; the
 *  models are not named or ordered by it. */
export function paceHint(ticked: Pick<ModelChoice, "seconds_hint">[], items: number): string {
  const hints = ticked
    .map((model) => model.seconds_hint)
    .filter((hint): hint is number => typeof hint === "number" && Number.isFinite(hint) && hint > 0);
  if (!hints.length) return "The first run will show how long a model takes.";
  const each = hints.map((hint) => minutes(hint, items));
  const low = Math.min(...each);
  const high = Math.max(...each);
  const span =
    low === high
      ? durationWords(low)
      : high < 90
        ? `${low} to ${high} minutes`
        : `${durationWords(low)} to ${durationWords(high)}`;
  const basis = ticked.length === 1 ? "going by how long it took before" : "going by how long each took before";
  const untimed = hints.length < ticked.length ? " Not every ticked model has been timed yet." : "";
  return `About ${span} a model, ${basis}.${untimed}`;
}

// ---- 3 · Which models ----------------------------------------------------------------

export const NO_MODEL = "No installed model reads pictures yet. Compare needs at least one.";
export const ONE_MODEL =
  "With one model there is nothing to set it beside yet. Its answers are still checked " +
  "against your decisions; add another model to this comparison later.";
export const TOO_MANY = (max: number) =>
  `A comparison holds at most ${max === 6 ? "six" : String(max)} models.`;

/** The model id in the address's `?model=` (L6's link from a model that has
 *  not been compared yet), or null. */
export function modelInAddress(search: string): string | null {
  const id = new URLSearchParams(search).get("model");
  return id && id.trim() ? id.trim() : null;
}

/**
 * The models ticked when the builder opens: only the one in the address when
 * there is one (unticked if it cannot be compared — its row says why), else
 * every model that can be compared, up to the most a comparison holds, in
 * the server's order.
 */
export function firstTicks(models: ModelChoice[], max: number, wanted: string | null): string[] {
  if (wanted) {
    const found = models.find((model) => model.id === wanted);
    return found && found.can_compare ? [found.id] : [];
  }
  return models.filter((model) => model.can_compare).slice(0, max).map((model) => model.id);
}

/** The ticks that still count: a model that can no longer be compared (or
 *  is gone) since it was ticked drops out. */
export function liveTicks(models: ModelChoice[], ticked: string[]): string[] {
  const open = new Set(models.filter((model) => model.can_compare).map((model) => model.id));
  return ticked.filter((id) => open.has(id));
}

/** Why a model's tick box cannot be ticked now; null when it can. One that
 *  is ticked can always be unticked. */
export function tickBlocker(model: ModelChoice, ticked: string[], max: number): string | null {
  if (ticked.includes(model.id)) return null;
  if (!model.can_compare) return model.reason ?? "It cannot be compared.";
  if (ticked.length >= max) return TOO_MANY(max);
  return null;
}

// ---- 4 · What you will get -----------------------------------------------------------

/** The sentence the server adds to every sample with shots; it belongs
 *  under the tags line rather than among the warnings. */
export const PER_CLIP = "Tags are checked per clip, using the shots of it that are in the sample.";

export interface PreviewLine {
  tone: "plain" | "warn";
  /** Set in bold before the text: "Scores", "Tags". */
  lead?: string;
  text: string;
}

function joinTitles(titles: string[]): string {
  if (titles.length <= 1) return titles[0] ?? "";
  if (titles.length === 2) return `${titles[0]} and ${titles[1]}`;
  return `${titles.slice(0, -1).join(", ")} and ${titles[titles.length - 1]}`;
}

/** "About 60 shots from 14 clips (of 212 shots in 31 clips)." */
export function sizeLine(preview: Preview): string {
  const shots = preview.shots
    ? `${plural(preview.shots, "shot", "shots")} from ${plural(preview.clips, "clip", "clips")}`
    : "";
  const photos = preview.photos ? plural(preview.photos, "photograph", "photographs") : "";
  const ofShots = preview.pool.shots
    ? `${plural(preview.pool.shots, "shot", "shots")} in ${plural(preview.pool.clips, "clip", "clips")}`
    : "";
  const ofPhotos = preview.pool.photos ? plural(preview.pool.photos, "photograph", "photographs") : "";
  const here = [shots, photos].filter(Boolean).join(" and ");
  const pool = [shots ? ofShots : "", photos ? ofPhotos : ""].filter(Boolean).join(" and ");
  return `About ${here || plural(preview.items, "item", "items")}${pool ? ` (of ${pool})` : ""}.`;
}

/**
 * The preview card, line by line. Counts come straight from the server; a
 * truth that is missing is said in amber (the server's own sentence), not
 * hidden. The preview is drawn with a fixed seed and Start with a fresh one,
 * so the counts are "about".
 */
export function previewLines(preview: Preview): PreviewLine[] {
  if (!preview.ok) {
    return preview.notes.map((note) => ({ tone: "warn" as const, text: note }));
  }
  const lines: PreviewLine[] = [{ tone: "plain", text: sizeLine(preview) }];
  const { used, tags } = preview.truth;
  if (preview.shots && used.used > 0) {
    const titles = used.cuts.map((cut) => cut.title);
    const where = titles.length ? ` in ${joinTitles(titles)}` : " in a recorded cut";
    lines.push({
      tone: "plain",
      lead: "Scores",
      text: `can be checked: ${used.used.toLocaleString("en-GB")} of them ${used.used === 1 ? "was" : "were"} used${where}, ${used.unused.toLocaleString("en-GB")} ${used.unused === 1 ? "was" : "were"} not.`,
    });
  }
  const perClip = preview.notes.includes(PER_CLIP);
  if (tags.footage > 0) {
    const noun =
      preview.what === "photos"
        ? plural(tags.footage, "photograph", "photographs")
        : preview.what === "shots"
          ? plural(tags.footage, "clip", "clips")
          : plural(tags.footage, "piece of footage", "pieces of footage");
    lines.push({
      tone: "plain",
      lead: "Tags",
      text:
        `can be checked on ${noun}: you said yes to ${plural(tags.yes, "tag", "tags")} on ` +
        `${tags.footage === 1 ? "it" : "them"} and no to ${tags.no.toLocaleString("en-GB")}.` +
        (perClip ? ` ${PER_CLIP}` : ""),
    });
  }
  for (const note of preview.notes) {
    if (note === PER_CLIP && tags.footage > 0) continue;
    lines.push({ tone: note === PER_CLIP ? "plain" : "warn", text: note });
  }
  for (const left of preview.left_out) {
    lines.push({ tone: "plain", text: `Left out: ${left.sentence}` });
  }
  return lines;
}

// ---- Start ---------------------------------------------------------------------------

export const START_NOTE =
  "The sample is fixed when you press Start: every model is shown exactly the same pictures. " +
  "You can leave the page; the comparison carries on.";

/**
 * Why Start cannot be pressed yet; null when it can. It waits for the
 * preview of exactly what would be sent, so nothing is started on a sample
 * the owner has not seen counted.
 */
export function startBlocker(options: {
  missing: string | null;
  key: string;
  preview: { key: string; ok: boolean } | null;
  previewFailed: boolean;
  ticked: number;
}): string | null {
  if (options.missing) return options.missing;
  if (!options.preview || options.preview.key !== options.key) {
    return options.previewFailed ? "The sample could not be counted. Change it or try again." : "Counting…";
  }
  if (!options.preview.ok) return "There is nothing to compare on here.";
  if (!options.ticked) return "Tick at least one model.";
  return null;
}

// =====================================================================================
// One comparison (app/models/compare/[id], CompareMeasures, CompareVerdict)
// =====================================================================================

// ---- the shapes read here ------------------------------------------------------------

type EntryState = "queued" | "running" | "waiting" | "done" | "stopped" | "failed";

interface Pace {
  answered: number;
  median_s: number | null;
  slowest_s: number | null;
  per_hour: number | null;
}

interface TagFigures {
  floor: number;
  footage: number;
  yes_total: number;
  yes_found: number;
  no_total: number;
  no_repeated: number;
  proposed: number;
  unjudged: number;
  unjudged_new: number;
}

interface ScoreFigures {
  used: number;
  unused: number;
  pairs: number;
  higher: number;
  lower: number;
  same: number;
  scored: number;
  distinct: number;
  few: boolean;
}

interface FormFigures {
  asked: number;
  answered: number;
  second_tries: number;
  failed: Record<string, number>;
}

interface Entry {
  id: string;
  model_id: string | null;
  installed: boolean;
  name: string;
  revision_short: string;
  runtime_label: string | null;
  runtime_version: string | null;
  state: EntryState;
  message: string;
  not_before: string | null;
  cancelling: boolean;
  done: number;
  failed: number;
  total: number;
  load_seconds: number | null;
  ran_from: string | null;
  memory_gb: number | null;
  memory_source: string | null;
  roles: string[] | null;
  finished_at: string | null;
}

interface Verdict {
  verdict: string;
  entry_id: string | null;
  note: string;
  at: string | null;
  by: string | null;
}

interface OneComparison {
  mode: string;
  what: string;
  stills: number;
  prompt_version: string;
  source_label: string;
  truth_at: string | null;
  created_at: string | null;
  over: number;
  counts: { photos: number; shots: number; clips: number; used: number; unused: number; footage_with_tags: number };
  entries: Entry[];
  verdict: Verdict;
}

// ---- reading it again ----------------------------------------------------------------

/** How often one comparison is read again: quickly while a column is queued,
 *  running or being stopped (its bar moves), slowly while columns only wait,
 *  never once nothing can move — every action reads it again at once. */
export const COMPARE_FAST_MS = 2_500;
export const COMPARE_SLOW_MS = 30_000;
// A wait that ends within this is watched quickly, so the column is seen to
// start rather than half a minute later.
const NEAR_MS = 2 * 60_000;

export function pollEvery(comparison: Pick<OneComparison, "mode" | "entries">, nowMs: number = Date.now()): number | null {
  if (comparison.mode === "unavailable") return null;
  let waiting = false;
  for (const entry of comparison.entries) {
    if (entry.cancelling || entry.state === "queued" || entry.state === "running") return COMPARE_FAST_MS;
    if (entry.state === "waiting") {
      const at = entry.not_before ? Date.parse(entry.not_before) : NaN;
      if (Number.isFinite(at) && at - nowMs < NEAR_MS) return COMPARE_FAST_MS;
      waiting = true;
    }
  }
  return waiting ? COMPARE_SLOW_MS : null;
}

/** `pollEvery` for L1's `rereads()`. After a failed read it always comes back
 *  (the page says "It tries again shortly"), at most `COMPARE_SLOW_MS` later —
 *  even when the last good read had nothing moving: an action's POST may just
 *  have started a column, and the read after it was the one that failed.
 *  Never before a first answer otherwise. */
export function comparisonEvery(
  latest: Pick<OneComparison, "mode" | "entries"> | null,
  failed: boolean,
): number | null {
  const every = latest ? pollEvery(latest) : null;
  if (failed) return every === null ? COMPARE_SLOW_MS : Math.min(every, COMPARE_SLOW_MS);
  return every;
}

// ---- words shared by the cells -------------------------------------------------------

/** "4.2 s", "14 s", "2 min 5 s". */
export function secondsWords(seconds: number): string {
  if (seconds < 10) return `${(Math.round(seconds * 10) / 10).toFixed(1)} s`;
  if (seconds < 120) return `${Math.round(seconds)} s`;
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)} min ${whole % 60} s`;
}

function clock(at: Date): string {
  return `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
}

/** "14 Nov 19:02" on this browser's clock (the year when it is not this one). */
export function dayClock(iso: string | null | undefined, nowMs: number = Date.now()): string {
  const day = shortDay(iso, nowMs);
  return day ? `${day} ${clock(new Date(iso as string))}` : "";
}

function gbWords(gb: number): string {
  const rounded = Math.round(gb * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)} GB`;
}

// ---- the header ----------------------------------------------------------------------

/** "60 shots from 14 clips · Shots used in … · fixed 14 Nov 19:02 · three
 *  stills a shot · question look-1" — the faint line under the name. */
export function comparisonLine(comparison: OneComparison, nowMs: number = Date.now()): string {
  const { counts } = comparison;
  const sample = [
    counts.shots ? `${plural(counts.shots, "shot", "shots")} from ${plural(counts.clips, "clip", "clips")}` : "",
    counts.photos ? plural(counts.photos, "photograph", "photographs") : "",
  ]
    .filter(Boolean)
    .join(" and ");
  const fixed = dayClock(comparison.truth_at ?? comparison.created_at, nowMs);
  const stills = counts.shots ? (comparison.stills === 1 ? "one still a shot" : "three stills a shot") : "";
  return [
    sample || "nothing in it",
    comparison.source_label,
    fixed ? `fixed ${fixed}` : "",
    stills,
    `question ${comparison.prompt_version}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** Row 16: a comparison made with an earlier question can be read, not added to. */
export function earlierQuestion(version: string): string {
  return `Made with an earlier version of the question (${version}). It can be read but not added to.`;
}

// ---- a column's head and Status ------------------------------------------------------

/** "build 475b908 · MLX-VLM 0.7.12". */
export function buildLine(entry: Pick<Entry, "revision_short" | "runtime_label" | "runtime_version">): string {
  const runtime = [entry.runtime_label, entry.runtime_version].filter(Boolean).join(" ");
  return [`build ${entry.revision_short}`, runtime].filter(Boolean).join(" · ");
}

/** The roles a column's model holds now, as the page names them. */
export function roleWords(roles: string[] | null): string[] {
  const names: Record<string, string> = { looker: "Looker", editor: "Editor" };
  return (roles ?? []).map((role) => names[role] ?? role);
}

export interface Status {
  text: string;
  tone: Tone;
  /** A bar, only while it runs: how many items it has been asked of all. */
  progress: { value: number; max: number; percent: number } | null;
  /** "34 of 60 asked so far." for a column that is not running and not done. */
  sofar: string | null;
}

function asked(entry: Pick<Entry, "done" | "failed">): number {
  return Math.max(0, entry.done) + Math.max(0, entry.failed);
}

// A sentence that already names a time ("starts 21:30") needs no second one.
const HAS_CLOCK = /\b\d{1,2}:\d{2}\b/;

/**
 * The Status cell: the server's sentence as it comes, a bar while it runs,
 * and the time it looks again when the server gave one the sentence does not
 * already say. "Stopping…" while a Cancel is being noticed.
 */
export function statusOf(entry: Entry, nowMs: number = Date.now()): Status {
  const count = asked(entry);
  const total = Math.max(0, entry.total);
  const sofar =
    entry.state !== "running" && entry.state !== "done" && count > 0
      ? `${count.toLocaleString("en-GB")} of ${total.toLocaleString("en-GB")} asked so far.`
      : null;
  if (entry.state === "running") {
    const percent = total > 0 ? Math.min(100, Math.floor((count / total) * 100)) : 0;
    const failed = entry.failed > 0 ? ` · ${entry.failed.toLocaleString("en-GB")} failed` : "";
    return {
      text: entry.cancelling ? "Stopping…" : `${count.toLocaleString("en-GB")} of ${total.toLocaleString("en-GB")}${failed}`,
      tone: null,
      progress: { value: count, max: total, percent },
      sofar: null,
    };
  }
  if (entry.cancelling) return { text: "Stopping…", tone: null, progress: null, sofar };
  switch (entry.state) {
    case "done": {
      const when = dayClock(entry.finished_at, nowMs);
      return { text: when ? `Finished ${when}.` : "Finished.", tone: "ok", progress: null, sofar: null };
    }
    case "queued":
      return { text: entry.message || "Waiting its turn.", tone: null, progress: null, sofar };
    case "waiting": {
      const at = entry.not_before ? new Date(entry.not_before) : null;
      const message = entry.message || "Waiting.";
      const again =
        at && !Number.isNaN(at.getTime()) && !HAS_CLOCK.test(message) ? ` It looks again at ${clock(at)}.` : "";
      return { text: `${message}${again}`, tone: "warn", progress: null, sofar };
    }
    case "stopped":
      return { text: entry.message || "It stopped.", tone: "warn", progress: null, sofar };
    case "failed":
      return { text: entry.message || "It failed.", tone: "bad", progress: null, sofar };
  }
  return { text: entry.message, tone: null, progress: null, sofar };
}

/** The columns that have ended since the page last read the comparison, as
 *  the one status line says it ("gemma-4-e4b-it-4bit has finished."). */
export function endedColumns(before: Pick<Entry, "id" | "name" | "state">[], after: Pick<Entry, "id" | "name" | "state">[]): string | null {
  const active = new Set(before.filter((entry) => ["queued", "running", "waiting"].includes(entry.state)).map((entry) => entry.id));
  const said = after
    .filter((entry) => active.has(entry.id))
    .map((entry) =>
      entry.state === "done" ? `${entry.name} has finished.` : entry.state === "stopped" || entry.state === "failed" ? `${entry.name} has stopped.` : "",
    )
    .filter(Boolean);
  return said.length ? said.join(" ") : null;
}

// ---- Answered in the form asked ------------------------------------------------------

// What each kind of failure did, as a phrase after its count. In a fixed
// order, so the sentence does not reshuffle as counts change.
const FAILED_AS: ReadonlyArray<[string, string]> = [
  ["bad_answer", "did not answer in the form asked"],
  ["timeout", "took too long"],
  ["died", "stopped the model unexpectedly"],
  ["runtime_error", "met an error in the software that runs it"],
  ["no_picture", "had no picture to look at"],
];

/** "57 of 60. 9 needed a second try. 3 failed: 2 did not answer in the form
 *  asked, 1 took too long." */
export function formSentence(form: FormFigures): string {
  const reasons = Object.entries(form.failed).filter(([, n]) => n > 0);
  const failed = reasons.reduce((sum, [, n]) => sum + n, 0);
  if (!form.answered && !failed) return "Nothing answered yet.";
  const parts = [`${form.answered.toLocaleString("en-GB")} of ${form.asked.toLocaleString("en-GB")}.`];
  if (form.second_tries > 0) parts.push(`${form.second_tries.toLocaleString("en-GB")} needed a second try.`);
  if (failed > 0) {
    const known = new Map(FAILED_AS);
    const ordered = [
      ...FAILED_AS.filter(([key]) => form.failed[key]).map(([key]) => key),
      ...reasons.map(([key]) => key).filter((key) => !known.has(key)),
    ];
    const phrases = ordered.map((key) => `${(form.failed[key] ?? 0).toLocaleString("en-GB")} ${known.get(key) ?? `failed (${key})`}`);
    parts.push(phrases.length === 1 ? `${phrases[0]}.` : `${failed.toLocaleString("en-GB")} failed: ${phrases.join(", ")}.`);
  }
  return parts.join(" ");
}

// ---- Tags you had decided ------------------------------------------------------------

export const FIGURES_LATER = "Appears when it finishes.";
export const NO_TAG_TRUTH =
  "Nobody had decided any tags on this footage, so there is nothing to check its tags against.";
export const NO_TAG_OVERLAP =
  "None of the footage with decided tags was answered by every finished model, so there is nothing to check its tags against yet.";

function tagLines(figures: TagFigures): string[] {
  const yes = figures.yes_total
    ? `Found ${figures.yes_found.toLocaleString("en-GB")} of the ${plural(figures.yes_total, "tag", "tags")} you had said yes to.`
    : "You had said yes to no tags here.";
  const no = figures.no_total
    ? `Suggested ${figures.no_repeated.toLocaleString("en-GB")} of the ${figures.no_total.toLocaleString("en-GB")} you had said no to.`
    : "You had said no to no tags here.";
  const open = figures.unjudged
    ? `${figures.unjudged.toLocaleString("en-GB")} more that nobody has ruled on` +
      (figures.unjudged_new
        ? ` — ${figures.unjudged_new.toLocaleString("en-GB")} of them ${figures.unjudged_new === 1 ? "a word" : "words"} the catalogue does not have.`
        : ".")
    : "Nothing more that nobody has ruled on.";
  return [yes, no, open];
}

export interface TagSentences {
  /** At every confidence (the floor of 0): three lines, or the one reason
   *  there is nothing to check against. */
  lines: string[];
  /** "Only the ones it was surer of": the same three counts at each floor. */
  surer: { label: string; lines: string[] }[];
}

/**
 * The Tags row of one column. Null until the column is done (the cell says
 * the figures appear when it finishes). Counts only, of agreement with tags
 * a person decided: a tag nobody ruled on is neither right nor wrong.
 */
export function tagSentences(
  tags: TagFigures[] | null,
  options: { done: boolean; truth: boolean },
): TagSentences | null {
  if (!options.done) return null;
  if (!tags) return { lines: [options.truth ? NO_TAG_OVERLAP : NO_TAG_TRUTH], surer: [] };
  const [every, ...surer] = [...tags].sort((a, b) => a.floor - b.floor);
  if (!every) return { lines: [options.truth ? NO_TAG_OVERLAP : NO_TAG_TRUTH], surer: [] };
  return {
    lines: tagLines(every),
    surer: surer.map((figures) => ({
      label: `At least ${Math.round(figures.floor * 100)}% sure`,
      lines: tagLines(figures),
    })),
  };
}

// ---- Scores against the cuts ---------------------------------------------------------

export const NO_SCORE_TRUTH =
  "None of these shots was used in a recorded cut, so there is nothing to check its scores against.";
export const ALL_USED =
  "Every one of these shots was used, so there is no unused shot to set a used one against.";
export const NO_SCORE_OVERLAP =
  "Not enough used and unused shots were answered by every finished model to set one against the other yet.";
export const PHOTOS_ONLY =
  "Scores are only checked for video shots: cut history says which shots were used.";
// Fewer different scores than this and the page says how few there were.
export const FEW_SCORES = 5;

/**
 * The Scores row of one column: every used shot set against every unused
 * one — higher, lower, the same — and what makes those counts weak. Null
 * until the column is done.
 */
export function scoreSentences(
  scores: ScoreFigures | null,
  options: { done: boolean; what: string; used: number; unused: number },
): string[] | null {
  if (!options.done) return null;
  if (options.what === "photos") return [PHOTOS_ONLY];
  if (!scores) {
    if (!options.used) return [NO_SCORE_TRUTH];
    if (!options.unused) return [ALL_USED];
    return [NO_SCORE_OVERLAP];
  }
  const n = (value: number) => value.toLocaleString("en-GB");
  const lines = [
    `You used ${n(scores.used)} of these ${plural(scores.scored, "shot", "shots")}. ` +
      `Setting each used shot against each unused one (${plural(scores.pairs, "pair", "pairs")}), ` +
      `it scored the used shot higher in ${n(scores.higher)}, lower in ${n(scores.lower)}, ` +
      `the same in ${n(scores.same)}. Guessing would get about half.`,
  ];
  if (scores.distinct <= 1) {
    lines.push("It gave every shot the same score, so its scores say nothing here.");
  } else if (scores.distinct < FEW_SCORES) {
    lines.push(`It only gave ${n(scores.distinct)} different scores.`);
  }
  if (scores.few) {
    lines.push(
      scores.used === 1
        ? "Only 1 used shot is in the sample: read this as a hint."
        : `Only ${n(scores.used)} used shots are in the sample: read this as a hint.`,
    );
  }
  return lines;
}

// ---- Time and Memory -----------------------------------------------------------------

/** "4.2 s a shot, typically; the slowest took 11 s. About 820 shots an
 *  hour." Null when it answered nothing of that kind. */
export function paceSentence(pace: Pace, noun: "shot" | "photograph"): string | null {
  if (!pace.answered) return null;
  const nouns = `${noun}s`;
  if (pace.median_s == null) return `${plural(pace.answered, noun, nouns)} answered, not timed.`;
  const slowest = pace.slowest_s != null ? `; the slowest took ${secondsWords(pace.slowest_s)}` : "";
  const hour = pace.per_hour ? ` About ${pace.per_hour.toLocaleString("en-GB")} ${nouns} an hour.` : "";
  return `${secondsWords(pace.median_s)} a ${noun}, typically${slowest}.${hour}`;
}

/** The Time cell: the pace of each kind it answered, then how long it took
 *  to load and from where. */
export function timeSentences(
  entry: Pick<Entry, "load_seconds" | "ran_from">,
  measures: { shots: Pace; photos: Pace },
): string[] {
  const lines = [paceSentence(measures.shots, "shot"), paceSentence(measures.photos, "photograph")].filter(
    (line): line is string => !!line,
  );
  if (entry.load_seconds != null && Number.isFinite(entry.load_seconds)) {
    const where = entry.ran_from === "library" ? " from the model library" : "";
    lines.push(`It took ${secondsWords(entry.load_seconds)} to load${where}.`);
  }
  return lines.length ? lines : ["Not timed yet."];
}

/** "6.1 GB at most, as the software reports it." — and how that was measured. */
export function memorySentence(entry: Pick<Entry, "memory_gb" | "memory_source">): string {
  if (entry.memory_gb == null || !Number.isFinite(entry.memory_gb)) return "Not measured yet.";
  const how =
    entry.memory_source === "runtime"
      ? "as the software reports it"
      : entry.memory_source === "process"
        ? "the size of its process"
        : "as measured";
  return `${gbWords(entry.memory_gb)} at most, ${how}.`;
}

/** The memory pill of a column whose model is still installed. */
export function fitsPill(fits: string | null): { tone: Tone; label: string; title: string } | null {
  if (fits === "day") return { tone: "ok", label: "by day", title: "Fits the daytime budget, so it can run at any hour." };
  if (fits === "night") {
    return {
      tone: "warn",
      label: "overnight only",
      title: "It needs more than the daytime budget, so it runs in the overnight window.",
    };
  }
  if (fits === "never") return { tone: "bad", label: "over budget", title: "It needs more than the overnight budget." };
  return null;
}

/** The sentence under the figures: what they are counted over and what
 *  they do not say. */
export function overNote(comparison: Pick<OneComparison, "over" | "entries">): string[] {
  const done = comparison.entries.filter((entry) => entry.state === "done").length;
  const later = done < comparison.entries.length ? "Figures for a model appear when it finishes." : "";
  if (done === 0) return [later || "Figures for a model appear when it finishes."];
  const items = plural(comparison.over, "item", "items");
  const first =
    done === 1
      ? `The tag and score figures use the ${items} it answered. They say how far it agrees with decisions you already made. They do not say whether it is good — look at the answers.`
      : `The tag and score figures use the ${items} every finished model answered, so the columns can be set beside each other. They say how far each model agrees with decisions you already made. They do not say which is good — look at the answers.`;
  return later ? [first, later] : [first];
}

// ---- Add a model, Compare again ------------------------------------------------------

/** The models that can be added to a comparison: every installed model not
 *  already in it, each with why it cannot be added, if it cannot. */
export function addChoices<T extends ModelChoice & { name: string }>(
  models: T[],
  entries: Pick<Entry, "model_id">[],
  max: number,
): { model: T; blocked: string | null }[] {
  const inIt = new Set(entries.map((entry) => entry.model_id).filter(Boolean));
  const full = entries.length >= max;
  return models
    .filter((model) => !inIt.has(model.id))
    .map((model) => ({
      model,
      blocked: !model.can_compare ? (model.reason ?? "It cannot be compared.") : full ? TOO_MANY(max) : null,
    }));
}

/** Compare again ticks the models compared here that can still be compared. */
export function againTicks(models: ModelChoice[], entries: Pick<Entry, "model_id">[], max: number): string[] {
  const here = new Set(entries.map((entry) => entry.model_id).filter(Boolean));
  return models
    .filter((model) => model.can_compare && here.has(model.id))
    .slice(0, max)
    .map((model) => model.id);
}

/** What Delete asks. */
export const DELETE_QUESTION =
  "Delete this comparison? Its answers and its copies of the pictures are removed. Nothing in the catalogue changes.";

/** What "Take it out of this comparison" asks. */
export function removeQuestion(entry: Pick<Entry, "id" | "name">, verdict: Pick<Verdict, "verdict" | "entry_id">): string {
  const chosen =
    verdict.verdict === "chosen" && verdict.entry_id === entry.id
      ? " The decision that chose it is cleared; its note is kept."
      : "";
  return `Take ${entry.name} out of this comparison? Its answers here are removed.${chosen} Nothing in the catalogue changes.`;
}

// ---- What did you decide? ------------------------------------------------------------

/** A radio's value: one per column, "none", or "open" (not decided). */
export type VerdictChoice = string;

export const NONE_GOOD = "None is good enough yet";
export const NOT_DECIDED = "Not decided";

export function choiceOf(verdict: Pick<Verdict, "verdict" | "entry_id">): VerdictChoice {
  if (verdict.verdict === "chosen" && verdict.entry_id) return `entry:${verdict.entry_id}`;
  if (verdict.verdict === "none") return "none";
  return "open";
}

/**
 * What Save sends: only what changed, so a decision and a rename made in two
 * tabs do not undo each other. Null when nothing changed.
 */
export function verdictChange(
  choice: VerdictChoice,
  note: string,
  saved: Pick<Verdict, "verdict" | "entry_id" | "note">,
): { verdict?: "" | "chosen" | "none"; entry_id?: string | null; note?: string } | null {
  const change: { verdict?: "" | "chosen" | "none"; entry_id?: string | null; note?: string } = {};
  if (choice !== choiceOf(saved)) {
    if (choice.startsWith("entry:")) {
      change.verdict = "chosen";
      change.entry_id = choice.slice("entry:".length);
    } else {
      change.verdict = choice === "none" ? "none" : "";
      change.entry_id = null;
    }
  }
  if (note !== saved.note) change.note = note;
  return Object.keys(change).length ? change : null;
}

/** The decision in words, for the page and for a reader who cannot change it. */
export function verdictWords(comparison: Pick<OneComparison, "verdict" | "entries">): string {
  const { verdict } = comparison;
  if (verdict.verdict === "chosen") {
    const entry = comparison.entries.find((one) => one.id === verdict.entry_id);
    return entry ? `${entry.name} looks` : "A model that was taken out of this comparison looks";
  }
  if (verdict.verdict === "none") return NONE_GOOD;
  return "Not decided yet";
}

/** "Decided 14 Nov by dj@example.com." Null when nothing was ever recorded. */
export function decidedLine(verdict: Verdict, nowMs: number = Date.now()): string | null {
  if (!verdict.at) return null;
  const by = verdict.by ? ` by ${verdict.by}` : "";
  const day = shortDay(verdict.at, nowMs);
  return verdict.verdict ? `Decided ${day}${by}.` : `Last changed ${day}${by}.`;
}

/**
 * The column "Make it the Looker" is offered for: the saved decision chose
 * it, its model is still installed, and it is not the Looker already. Null
 * otherwise — a draft not yet saved offers nothing.
 */
export function lookerOffer<T extends Entry>(comparison: { verdict: Verdict; entries: T[] }): T | null {
  const { verdict } = comparison;
  if (verdict.verdict !== "chosen" || !verdict.entry_id) return null;
  const entry = comparison.entries.find((one) => one.id === verdict.entry_id);
  if (!entry || !entry.installed || !entry.model_id || !entry.roles) return null;
  return entry.roles.includes("looker") ? null : entry;
}

/** What "Make it the Looker" asks. */
export function lookerQuestion(name: string, current: string | null): string {
  return current ? `Make ${name} the Looker instead of ${current}?` : `Make ${name} the Looker?`;
}

// =====================================================================================
// The answers, item by item (CompareAnswers, and the viewer on the comparison page)
// =====================================================================================

// ---- the shapes read here ------------------------------------------------------------

interface AnswerTag {
  name: string;
  confidence: number | null;
  in_vocabulary: boolean;
  ruling: string | null;
}

interface Answer {
  ok: boolean;
  problem: string | null;
  message: string;
  tags: AnswerTag[];
  caption: string;
  score: number | null;
  reason: string;
  seconds: number | null;
  attempts: number;
  raw: string | null;
}

interface Item {
  id: string;
  position: number;
  kind: string;
  asset_id: string | null;
  in_catalogue: boolean;
  start_s: number | null;
  end_s: number | null;
  truth: { yes: string[]; no: string[] };
  used: boolean | null;
  counted: boolean;
  answers: Record<string, Answer>;
}

// ---- reading the items again ---------------------------------------------------------

/** The items are read again when something about the answers moved, and no
 *  more often than this: a fast model stores an answer every second or two,
 *  and two hundred items with every column is not a small read. */
export const ITEMS_EVERY_MS = 5_000;

/**
 * What says the items may have changed: the number of answers stored, and
 * each column's own counts. The total alone can stand still while the
 * answers change — "Try the failed ones again" deletes three failures and the
 * next three answers put the count back where it was.
 */
export function itemsKey(comparison: {
  answers_stored: number;
  entries: { id: string; state: string; done: number; failed: number }[];
}): string {
  const columns = comparison.entries.map((entry) => `${entry.id}:${entry.state}:${entry.done}:${entry.failed}`);
  return [comparison.answers_stored, ...columns].join("|");
}

/** How long to wait before reading the items, given when they were last read
 *  (0: never). */
export function itemsWait(lastAtMs: number, nowMs: number = Date.now()): number {
  if (!lastAtMs) return 0;
  return Math.max(0, ITEMS_EVERY_MS - (nowMs - lastAtMs));
}

/** After a failed items read, the next try comes this much later even if
 *  nothing moves (sooner if the answers move). */
export const ITEMS_RETRY_MS = 30_000;

export interface ItemsReader {
  /** The answers now stand at `key`: read the items if they were not read for it. */
  moved(key: string): void;
  /** The page is going (or showing another comparison): nothing more is read
   *  and nothing that lands later is used. */
  stop(): void;
}

/**
 * Reads one comparison's items when its answers move, at most every
 * `ITEMS_EVERY_MS`. A read that has started is always let finish and used:
 * the comparison is polled every 2.5 s while a column runs and its answers
 * move on nearly every poll, so a read dropped whenever they move would,
 * on a busy machine, never land at all. Only one read is in flight; if the
 * answers moved while it was, the next one is armed when it lands.
 *
 * `failed` returns whether to try again (a 404 is the comparison going,
 * which its own read says).
 */
export function itemsReader<T>(options: {
  read: () => Promise<T>;
  answered: (value: T) => void;
  failed: (err: unknown) => boolean;
  later: (run: () => void, ms: number) => unknown;
  cancel: (handle: unknown) => void;
  now?: () => number;
}): ItemsReader {
  const clockNow = options.now ?? (() => Date.now());
  let wanted: string | null = null;
  let readFor: string | null = null;
  // The key whose read failed and is waiting for the slow retry; the answers
  // moving past it brings the read forward.
  let retryFor: string | null = null;
  let lastAt = 0;
  let reading = false;
  let stopped = false;
  let timer: unknown = null;

  function arm(ms: number) {
    if (timer !== null) options.cancel(timer);
    timer = options.later(() => {
      timer = null;
      void start();
    }, ms);
  }

  function consider() {
    if (stopped || reading || wanted === null || wanted === readFor || wanted === retryFor) return;
    arm(itemsWait(lastAt, clockNow()));
  }

  async function start() {
    if (stopped || reading || wanted === null) return;
    reading = true;
    readFor = wanted;
    retryFor = null;
    lastAt = clockNow();
    try {
      const value = await options.read();
      if (stopped) return;
      options.answered(value);
    } catch (err) {
      if (stopped) return;
      if (options.failed(err)) {
        retryFor = readFor;
        readFor = null;
        arm(ITEMS_RETRY_MS);
      }
    } finally {
      reading = false;
    }
    // The answers may have moved while it read; the 5 s floor still holds.
    consider();
  }

  return {
    moved(key) {
      wanted = key;
      consider();
    },
    stop() {
      stopped = true;
      if (timer !== null) options.cancel(timer);
      timer = null;
    },
  };
}

// ---- Show: which items ---------------------------------------------------------------

/** Two scores this far apart (or more) on one item are a difference worth
 *  looking at. */
export const DIFFER_POINTS = 30;

/** Highest minus lowest of the scores given; null with fewer than two. */
export function spread(scores: (number | null | undefined)[]): number | null {
  const given = scores.filter((one): one is number => typeof one === "number" && Number.isFinite(one));
  if (given.length < 2) return null;
  return Math.max(...given) - Math.min(...given);
}

function okAnswers(item: Pick<Item, "answers">, entryIds: string[]): Answer[] {
  const out: Answer[] = [];
  for (const id of entryIds) {
    const answer = item.answers[id];
    if (answer?.ok) out.push(answer);
  }
  return out;
}

function tagKey(name: string): string {
  return name.trim().toLowerCase();
}

/**
 * Where the models differ on an item: their scores are DIFFER_POINTS or more
 * apart, or one proposed a tag you had ruled on and another did not. Only
 * answers given in the form asked count; one answer cannot differ from
 * anything.
 */
export function differs(item: Pick<Item, "answers">, entryIds: string[]): boolean {
  const answers = okAnswers(item, entryIds);
  if (answers.length < 2) return false;
  const apart = spread(answers.map((answer) => answer.score));
  if (apart !== null && apart >= DIFFER_POINTS) return true;
  const ruled = answers.map(
    (answer) =>
      new Set(answer.tags.filter((tag) => tag.ruling === "yes" || tag.ruling === "no").map((tag) => tagKey(tag.name))),
  );
  const every = new Set<string>();
  for (const one of ruled) for (const name of one) every.add(name);
  for (const name of every) if (ruled.some((one) => !one.has(name))) return true;
  return false;
}

/** Whether any column's answer on this item is a failure. */
export function failedOn(item: Pick<Item, "answers">, entryIds: string[]): boolean {
  return entryIds.some((id) => item.answers[id]?.ok === false);
}

export type Show = "all" | "differ" | "used" | "unused" | "decided" | "failed";

export const SHOW_CHOICES: { value: Show; label: string }[] = [
  { value: "all", label: "All" },
  { value: "differ", label: "Where the models differ" },
  { value: "used", label: "Used in a cut" },
  { value: "unused", label: "Not used" },
  { value: "decided", label: "With decided tags" },
  { value: "failed", label: "Something failed" },
];

/** Whether an item is shown under `show`. `entryIds` are the columns now in
 *  the comparison: a column taken out has no say. */
export function keep(item: Item, show: Show, entryIds: string[]): boolean {
  switch (show) {
    case "differ":
      return differs(item, entryIds);
    case "used":
      return item.used === true;
    case "unused":
      return item.used === false;
    case "decided":
      return item.truth.yes.length + item.truth.no.length > 0;
    case "failed":
      return failedOn(item, entryIds);
    default:
      return true;
  }
}

/** How many items each choice of Show would leave, for its button. */
export function showCounts(items: Item[], entryIds: string[]): Record<Show, number> {
  const counts: Record<Show, number> = { all: 0, differ: 0, used: 0, unused: 0, decided: 0, failed: 0 };
  for (const item of items) {
    for (const choice of SHOW_CHOICES) if (keep(item, choice.value, entryIds)) counts[choice.value] += 1;
  }
  return counts;
}

/** Said when a choice of Show leaves nothing. */
export function nothingShown(show: Show): string {
  const choice = SHOW_CHOICES.find((one) => one.value === show);
  return `No item fits “${choice ? choice.label : show}”.`;
}

// ---- Order ---------------------------------------------------------------------------

/** "shot" (as shot), or "entry:<column id>" (by that model's score). */
export type Order = string;

export const AS_SHOT: Order = "shot";

/** "As shot", then one "By <model>'s score, highest first" per column. Items
 *  are ordered by a score; the models never are. */
export function orderChoices(entries: Pick<Entry, "id" | "name">[]): { value: Order; label: string }[] {
  return [
    { value: AS_SHOT, label: "As shot" },
    ...entries.map((entry) => ({ value: `entry:${entry.id}`, label: `By ${entry.name}'s score, highest first` })),
  ];
}

/** The order to use: the one chosen, or "as shot" once its column is gone. */
export function orderOf(order: Order, entries: Pick<Entry, "id">[]): Order {
  if (!order.startsWith("entry:")) return AS_SHOT;
  const id = order.slice("entry:".length);
  return entries.some((entry) => entry.id === id) ? order : AS_SHOT;
}

/**
 * The items in the order asked for. As shot is the sample's own order. By a
 * model's score puts its highest first; items it gave no score (not asked
 * yet, or failed) come after, as shot. Ties keep the sample's order, so the
 * list does not reshuffle while a column runs.
 */
export function sorted<T extends Pick<Item, "position" | "answers">>(items: T[], order: Order): T[] {
  const byPosition = [...items].sort((a, b) => a.position - b.position);
  if (!order.startsWith("entry:")) return byPosition;
  const id = order.slice("entry:".length);
  const scoreOf = (item: T): number | null => {
    const answer = item.answers[id];
    return answer?.ok && typeof answer.score === "number" ? answer.score : null;
  };
  return byPosition
    .map((item, at) => ({ item, at, score: scoreOf(item) }))
    .sort((a, b) => {
      if (a.score === null || b.score === null) {
        if (a.score === b.score) return a.at - b.at;
        return a.score === null ? 1 : -1;
      }
      return b.score - a.score || a.at - b.at;
    })
    .map((one) => one.item);
}

// ---- the item's cell -----------------------------------------------------------------

/** "2.0–14.0 s" for a shot; "" for a photograph. */
export function spanWords(start: number | null, end: number | null): string {
  if (start === null || end === null) return "";
  return `${start.toFixed(1)}–${end.toFixed(1)} s`;
}

/** The asset page at the shot's start (whole seconds, as the asset page reads `t`). */
export function assetHref(item: Pick<Item, "asset_id" | "start_s">): string | null {
  if (!item.asset_id) return null;
  const at = item.start_s && item.start_s > 0 ? Math.floor(item.start_s) : 0;
  return at > 0 ? `/assets/${item.asset_id}?t=${at}` : `/assets/${item.asset_id}`;
}

/** Said in place of a way to the footage once the item has left the catalogue. */
export function goneLine(kind: string): string {
  return kind === "photo"
    ? "This photograph is no longer in the catalogue."
    : "This clip is no longer in the catalogue.";
}

export const NOT_COUNTED = "Not counted in the figures: a model failed on it.";

/** The faint line under an item the figures leave out — only once a column
 *  has finished, since before that no item is counted at all. */
export function notCountedLine(item: Pick<Item, "counted">, entries: Pick<Entry, "state">[]): string | null {
  if (item.counted) return null;
  return entries.some((entry) => entry.state === "done") ? NOT_COUNTED : null;
}

export const NO_TRUTH_HERE = "No tags decided here.";

// ---- a model's cell ------------------------------------------------------------------

/** A tag pill's title: how sure the model said it was. */
export function sureTitle(confidence: number | null): string | null {
  if (confidence === null || !Number.isFinite(confidence)) return null;
  const share = Math.round(Math.min(1, Math.max(0, confidence)) * 100);
  return `it was ${share}% sure`;
}

/** The mark on a tag you had ruled on — as text, so it is not colour alone. */
export function rulingMark(ruling: string | null): { mark: string; words: string } | null {
  if (ruling === "yes") return { mark: "✓", words: "you had said yes" };
  if (ruling === "no") return { mark: "✗", words: "you had said no" };
  return null;
}

/** "Score 72 — Steady and well framed." The reason alone when no score. */
export function scoreLine(answer: Pick<Answer, "score" | "reason">): string {
  const reason = answer.reason.trim();
  if (answer.score === null) return reason;
  return reason ? `Score ${answer.score} — ${reason}` : `Score ${answer.score}`;
}

/** "4.1 s · second try". */
export function answerFoot(answer: Pick<Answer, "seconds" | "attempts">): string {
  const parts: string[] = [];
  if (answer.seconds !== null && Number.isFinite(answer.seconds)) parts.push(secondsWords(answer.seconds));
  if (answer.attempts === 2) parts.push("second try");
  else if (answer.attempts > 2) parts.push(`${answer.attempts} tries`);
  return parts.join(" · ");
}

const PROBLEM_WORDS: Record<string, string> = {
  bad_answer: "It answered, but not in the form asked for.",
  died: "The model stopped unexpectedly.",
  timeout: "It took too long.",
  runtime_error: "The model stopped unexpectedly.",
  no_picture: "Its picture is no longer there.",
};

/** A failed answer's sentence: the server's, or the design's for its kind. */
export function failureSentence(answer: Pick<Answer, "message" | "problem">): string {
  const said = answer.message.trim();
  if (said) return said;
  return (answer.problem && PROBLEM_WORDS[answer.problem]) || "It did not answer.";
}

/** A cell with no answer yet. */
export const NOT_ASKED = "—";

/** The item a running column is looking at: the first, as shot, it has not
 *  answered. It answers in the sample's order, skipping what it has. */
export function lookingAt(
  items: Pick<Item, "id" | "position" | "answers">[],
  entry: Pick<Entry, "id" | "state">,
): string | null {
  if (entry.state !== "running") return null;
  let first: Pick<Item, "id" | "position" | "answers"> | null = null;
  for (const item of items) {
    if (item.answers[entry.id]) continue;
    if (!first || item.position < first.position) first = item;
  }
  return first ? first.id : null;
}

// ---- the viewer ----------------------------------------------------------------------

/** Whether an item's footage can be opened: it must still be in the catalogue. */
export function canOpen(item: Pick<Item, "asset_id" | "in_catalogue">): boolean {
  return !!item.asset_id && item.in_catalogue;
}

/**
 * Where the viewer stands in the rows as shown (filtered and ordered), and
 * the rows ← and → go to: the nearest each way whose footage can be opened.
 * An item that has left the rows (an answer changed what Show keeps) has no
 * place and no steps; the viewer stays on it until it is closed.
 */
export function viewerSteps<T extends Pick<Item, "id" | "asset_id" | "in_catalogue">>(
  rows: T[],
  currentId: string,
): { position: string | null; prev: T | null; next: T | null } {
  const at = rows.findIndex((row) => row.id === currentId);
  if (at < 0) return { position: null, prev: null, next: null };
  let prev: T | null = null;
  for (let i = at - 1; i >= 0; i -= 1) {
    const row = rows[i];
    if (row && canOpen(row)) {
      prev = row;
      break;
    }
  }
  let next: T | null = null;
  for (let i = at + 1; i < rows.length; i += 1) {
    const row = rows[i];
    if (row && canOpen(row)) {
      next = row;
      break;
    }
  }
  return { position: `${at + 1} of ${rows.length}`, prev, next };
}
