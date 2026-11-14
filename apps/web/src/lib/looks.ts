// The words and sums of folders and of looking: Browse's Folders view, the
// score a local model gave, and what a folder review says before and while
// it runs. Kept apart from the pages so they can be tested.
//
// Nothing is imported, not even types, so `node --test` loads this file as it
// is (tests/looks.test.mjs). The shapes below are the few fields each helper
// reads; the full answers are typed in lib/api.ts, and TypeScript checks that
// those fit these wherever a page hands one over.
//
// A folder is a library and a path inside it, exactly as the catalogue wrote
// it: a name may end in a space or hold a backslash, and trimming or
// "tidying" one would open a different folder from the one that was listed.
// The only thing dropped is a slash at either end, as the server drops it.
//
// A look is a local model's opinion, and every sentence about a review's
// state is the server's own. What is here counts and words what the server
// said; it never decides that something was looked at, or how long is left.

// ---- folders -------------------------------------------------------------------------

export interface Crumb {
  name: string;
  path: string;
}

function trimSlashes(path: string): string {
  let start = 0;
  let end = path.length;
  while (start < end && path[start] === "/") start += 1;
  while (end > start && path[end - 1] === "/") end -= 1;
  return path.slice(start, end);
}

/** Each folder on the way down to `path`: "Pete Dye/Drone" is Pete Dye, then
 *  Pete Dye/Drone. Nothing for the library itself. */
export function folderParts(path: string): Crumb[] {
  const folder = trimSlashes(path);
  if (!folder) return [];
  const names = folder.split("/");
  return names.map((name, index) => ({ name, path: names.slice(0, index + 1).join("/") }));
}

/** The folder that holds `path`; "" (the library) for a top folder and for
 *  the library itself. */
export function parentOf(path: string): string {
  const folder = trimSlashes(path);
  const cut = folder.lastIndexOf("/");
  return cut < 0 ? "" : folder.slice(0, cut);
}

/** The folder a file is in, from its path in the library; "" at the top. */
export function folderOf(relativePath: string): string {
  const cut = relativePath.lastIndexOf("/");
  return cut < 0 ? "" : relativePath.slice(0, cut);
}

// ---- Browse's address ----------------------------------------------------------------

/** Where Browse is, for a link or the address bar. */
export function browseHref(query: string): string {
  return query ? `/browse?${query}` : "/browse";
}

/**
 * A Browse query with `changes` made: null or "" takes a name out. A change
 * starts again at page 1 unless told to keep the page (the page arrows).
 *
 * Every control on Browse builds on the query the address is about to have,
 * through this, so two changes made faster than the address updates both
 * land.
 */
export function withChanges(
  query: string,
  changes: Record<string, string | null>,
  keepPage = false,
): string {
  const usp = new URLSearchParams(query);
  for (const [name, value] of Object.entries(changes)) {
    if (value === null || value === "") usp.delete(name);
    else usp.set(name, value);
  }
  if (!keepPage) usp.delete("page");
  return usp.toString();
}

/**
 * The query that shows `folder` of a library in the Folders view ("" is the
 * library's own top). Filters that are about files — the media type, the
 * sort, "Ready to view", "Include sub-folders", a tag — are kept, so going
 * down a folder does not reset what was chosen; the page is not.
 */
export function folderQuery(query: string, libraryId: string, folder: string): string {
  return withChanges(query, { view: "folders", library: libraryId, folder: trimSlashes(folder) });
}

/** The query for the list of libraries, the top of the Folders view. */
export function librariesQuery(query: string): string {
  return withChanges(query, { view: "folders", library: null, folder: null });
}

/**
 * The query after the view switch is pressed. "Everything" is the flat view
 * Browse has always had: no folder, and no "Include sub-folders", which
 * means nothing there. The library chosen stays chosen either way.
 */
export function viewQuery(query: string, view: "flat" | "folders"): string {
  if (view === "folders") return withChanges(query, { view: "folders", folder: null });
  return withChanges(query, { view: null, folder: null, deep: null });
}

/**
 * Whose files a Browse query lists: the library and folder of the Folders
 * view, and nothing for the flat view (where the library is one filter
 * among the others). While a filter changes, the files on screen stay until
 * the new ones arrive; when this changes they must not — they would be
 * another folder's files under this folder's name.
 */
export function placeOf(query: string): string {
  const usp = new URLSearchParams(query);
  if (usp.get("view") !== "folders") return "";
  return `folders\n${usp.get("library") ?? ""}\n${trimSlashes(usp.get("folder") ?? "")}`;
}

// ---- counts in words -----------------------------------------------------------------

function count(n: number, one: string, many: string): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}

function joined(parts: string[]): string {
  if (parts.length <= 1) return parts[0] ?? "";
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1] ?? ""}`;
}

interface Counts {
  items: number;
  photos: number;
  clips: number;
}

/**
 * What is beneath a folder: "134 items — 110 photographs, 24 clips". A
 * folder of one kind says only that ("20 photographs"). `items` counts
 * sound files too, so whatever is neither a photograph nor a clip is named
 * as audio rather than left as a sum that does not add up.
 */
export function contentsText(counts: Counts): string {
  const audio = Math.max(0, counts.items - counts.photos - counts.clips);
  const kinds = [
    counts.photos > 0 ? count(counts.photos, "photograph", "photographs") : "",
    counts.clips > 0 ? count(counts.clips, "clip", "clips") : "",
    audio > 0 ? count(audio, "audio file", "audio files") : "",
  ].filter(Boolean);
  if (kinds.length === 0) return "nothing catalogued";
  if (kinds.length === 1) return kinds[0] ?? "";
  return `${count(counts.items, "item", "items")} — ${kinds.join(", ")}`;
}

/**
 * How much of a folder a local model has finished looking at, or "" when
 * none of it has been (most folders, and every folder where nobody uses
 * local AI: a column of "0 looked at" says nothing). Only photographs and
 * clips are ever looked at, so audio is not in the "of".
 */
export function lookedText(counts: { photos: number; clips: number; looked: number }): string {
  const can = counts.photos + counts.clips;
  if (counts.looked <= 0 || can <= 0) return "";
  if (counts.looked >= can) return "all looked at";
  return `${counts.looked.toLocaleString("en-US")} of ${can.toLocaleString("en-US")} looked at`;
}

/** The heading over a folder's files: how many the folder holds (whatever
 *  the filters then show of them), and whether the folders inside are
 *  counted. */
export function filesHeading(files: number, deep: boolean): string {
  const many = files <= 0 ? "No files" : count(files, "file", "files");
  return deep ? `${many} in this folder and the folders inside it` : `${many} in this folder`;
}

/** Beside that heading when the filters hide some of the files, so a short
 *  grid under "134 files" is not taken for the whole folder; "" when
 *  nothing is hidden. */
export function shownNote(shown: number, files: number): string {
  if (shown >= files) return "";
  return `${shown.toLocaleString("en-US")} shown with the filters chosen`;
}

interface Level {
  path: string;
  folders: unknown[];
  here: Counts;
  inside: Counts;
}

/**
 * Why a folder's grid is empty, so the page can say the right thing instead
 * of "no assets match" for all four:
 *
 * - "library": nothing has been catalogued in this library at all;
 * - "gone": nothing is catalogued under this path (renamed or moved);
 * - "folders": the files are in the folders inside, and those are not
 *   being included;
 * - "filters": there are files here and the filters hide every one.
 */
export function emptyReason(level: Level, deep: boolean): "library" | "gone" | "folders" | "filters" {
  if (level.inside.items <= 0) return level.path === "" ? "library" : "gone";
  if (!deep && level.here.items <= 0) return "folders";
  return "filters";
}

// ---- a model's score -----------------------------------------------------------------

/** How a 0–100 score is tinted: 70 and up reads as good, under 40 as a
 *  warning, between them plain. */
export function scoreTone(score: number): "good" | "plain" | "low" {
  if (score >= 70) return "good";
  return score < 40 ? "low" : "plain";
}

/** A score for a screen reader and a tooltip: whose it is and out of what. */
export function scoreLabel(score: number): string {
  return `AI score ${score} of 100`;
}

// ---- a review, before it starts ------------------------------------------------------

/** A length of time as the server writes it: "under a minute", "25 min",
 *  "1 h 40 min", "3 h". */
export function lengthText(seconds: number): string {
  if (!(seconds >= 60)) return "under a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `${hours} h` : `${hours} h ${rest} min`;
}

interface Estimate {
  seconds: number | null;
  basis: string;
  words?: string | null;
}

/**
 * "About how long", in the server's own sentence: it says what the figure
 * rests on (measured answers, one test, nothing yet) and what it leaves
 * out. Only an answer with no sentence is worded here, and then with no
 * more than the figure.
 */
export function estimateText(estimate: Estimate): string {
  const words = (estimate.words ?? "").trim();
  if (words) return words;
  if (estimate.seconds === null || estimate.basis === "none") {
    return "No estimate yet: nothing has been looked at with this model. The first few will tell.";
  }
  const length = lengthText(estimate.seconds);
  const lead = estimate.basis === "test" ? "Roughly" : "About";
  return estimate.seconds < 60 ? "Under a minute." : `${lead} ${length}.`;
}

interface Todo {
  photos: number;
  clips: number;
  shots: number;
  stills_only: number;
}

/** Whether a review of this would have anything to look at right now
 *  (clips still to be made ready are counted apart, in `firstText`). */
export function todoItems(todo: Todo): number {
  return todo.photos + todo.shots + todo.stills_only;
}

/**
 * What has not been looked at yet: "214 photographs and 37 clips (291
 * shots)." Clips judged from one still are clips too, but have no shots, so
 * the shots in the brackets are only those of the clips that have them.
 * With nothing ready it says which kind of nothing: all looked at, only
 * clips that are not ready, or nothing a model looks at.
 */
export function todoText(preview: {
  todo: Todo;
  first?: First;
  already?: { items: number };
}): string {
  const { photos, clips, shots, stills_only: stills } = preview.todo;
  if (todoItems(preview.todo) <= 0) {
    // A folder of clips with no playable copies has nothing ready and
    // everything still to do: "everything has been looked at" would be the
    // opposite of true. Nor is it true of a folder that holds only sound.
    const first = preview.first;
    if (first && first.need_copy + first.need_shots + (first.copy_away ?? 0) > 0) {
      return "Nothing that is ready yet: only the clips named under Needed first.";
    }
    if (preview.already && preview.already.items <= 0) {
      return "There is nothing here a model can look at.";
    }
    return "Everything here has been looked at.";
  }
  return `${kindsText({ photos, clips: clips + stills, shots })}.`;
}

/** Items by kind, as the server counts them beside `already`: photographs,
 *  clips (each once), and the shots counted one by one in those clips. */
export interface Kinds {
  photos: number;
  clips: number;
  shots: number;
}

/**
 * "214 photographs and 37 clips (291 shots)", with no full stop: the one way
 * the dialog says a number of things looked at, or to look at. A clip judged
 * from one still is a clip with no shots, so the shots in the brackets are
 * only those of the clips that have them. "" for nothing.
 */
export function kindsText(kinds: Kinds): string {
  const { photos, clips, shots } = kinds;
  const parts = [
    photos > 0 ? count(photos, "photograph", "photographs") : "",
    clips > 0
      ? count(clips, "clip", "clips") + (shots > 0 ? ` (${count(shots, "shot", "shots")})` : "")
      : "",
  ].filter(Boolean);
  return joined(parts);
}

interface First {
  need_copy: number;
  need_copy_footage_s: number;
  copy_away?: number;
  need_shots: number;
  still_processing: number;
  problem: string | null;
}

/**
 * What has to happen before some of a folder can be looked at, one sentence
 * each, in the order the dialog shows them; empty when nothing does.
 * `prepare` is the dialog's "Make clips ready first" box: unticked, nothing
 * is made ready, and a clip that is not ready is judged from one still (the
 * review does that with whatever never became ready; it skips nothing).
 */
export function firstText(preview: { todo: Todo; first: First }, prepare = true): string[] {
  const { first, todo } = preview;
  const lines: string[] = [];
  const copies =
    first.need_copy > 0
      ? `${count(first.need_copy, "clip has", "clips have")} no playable copy yet` +
        (first.need_copy_footage_s >= 1
          ? ` (${lengthText(first.need_copy_footage_s)} of footage)`
          : "")
      : "";
  const shots =
    first.need_shots > 0
      ? `${first.need_shots.toLocaleString("en-US")} ${
          first.need_copy > 0
            ? first.need_shots === 1
              ? "has not had its shots found"
              : "have not had their shots found"
            : first.need_shots === 1
              ? "clip has not had its shots found"
              : "clips have not had their shots found"
        }`
      : "";
  const waiting = [copies, shots].filter(Boolean);
  if (waiting.length > 0) {
    const one = first.need_copy + first.need_shots === 1;
    lines.push(
      `${joined(waiting)}. ` +
        (prepare
          ? "These are made automatically; what is ready is looked at meanwhile, and the rest as they finish."
          : `${one ? "It is" : "Each is"} judged from one still this time.`),
    );
  }
  if (first.problem) lines.push(first.problem);
  const away = first.copy_away ?? 0;
  if (away > 0) {
    lines.push(
      `${count(away, "clip has", "clips have")} no playable copy and ${
        away === 1 ? "its" : "their"
      } drive is away; ${away === 1 ? "it is" : "each is"} judged from one still.`,
    );
  }
  if (todo.stills_only > 0) {
    lines.push(
      `${count(todo.stills_only, "clip", "clips")} cannot be split into shots; ${
        todo.stills_only === 1 ? "it is" : "each is"
      } judged from one still.`,
    );
  }
  if (first.still_processing > 0) {
    lines.push(
      `${count(first.still_processing, "item is", "items are")} still being processed and ${
        first.still_processing === 1 ? "is" : "are"
      } skipped this time.`,
    );
  }
  return lines;
}

/** What a review leaves out, or "" when it leaves nothing out: "4 files are
 *  missing and 9 are audio." */
export function skippedText(skipped: { missing: number; audio: number }): string {
  const { missing, audio } = skipped;
  if (missing > 0 && audio > 0) {
    return `${count(missing, "file is", "files are")} missing and ${audio.toLocaleString("en-US")} ${
      audio === 1 ? "is" : "are"
    } audio.`;
  }
  if (missing > 0) return `${count(missing, "file is", "files are")} missing.`;
  if (audio > 0) return `${count(audio, "file is", "files are")} audio, which is not looked at.`;
  return "";
}

// ---- the review dialog: what is asked, and what stands in the way ----------------------

/**
 * What a review would cover, under the dialog's title: "GELCO › Pete Dye —
 * this folder and the 3 folders inside it." A review always takes everything
 * beneath the folder; the count is of the folders directly inside it, and
 * null before anyone has counted.
 */
export function scopeText(
  libraryName: string,
  folder: string,
  foldersInside: number | null,
): string {
  const library = libraryName || "This library";
  const names = folderParts(folder).map((part) => part.name);
  if (names.length === 0) return `${library} — the whole library.`;
  // Not counted yet (the dialog has just opened): the path alone, rather
  // than "this folder" for one that turns out to hold forty.
  if (foldersInside === null) return [library, ...names].join(" › ");
  const inside =
    foldersInside <= 0
      ? "this folder"
      : foldersInside === 1
        ? "this folder and the folder inside it"
        : `this folder and the ${foldersInside.toLocaleString("en-US")} folders inside it`;
  return `${[library, ...names].join(" › ")} — ${inside}.`;
}

function kindsFrom(value: unknown): Kinds | null {
  if (!value || typeof value !== "object") return null;
  const { photos, clips, shots } = value as Record<string, unknown>;
  if (![photos, clips, shots].every((n) => typeof n === "number" && Number.isFinite(n))) return null;
  return { photos: photos as number, clips: clips as number, shots: shots as number };
}

/**
 * A preview's `already_kinds` and `by_another_kinds`: what was already
 * looked at, and what redo would add, by kind. Either is null when the
 * server did not send it whole, and the dialog then says the number in
 * items as what it counts.
 */
export function previewKinds(preview: {
  already_kinds?: Kinds | null;
  by_another_kinds?: Kinds | null;
}): { already: Kinds | null; byAnother: Kinds | null } {
  return { already: kindsFrom(preview.already_kinds), byAnother: kindsFrom(preview.by_another_kinds) };
}

/** A number of items with no breakdown beside it: a server older than
 *  `already_kinds`. Never "N items": the folder list beside the dialog says
 *  items for files, and a clip of six shots counts six here. */
function itemsText(items: number): string {
  return `${items.toLocaleString("en-US")}, counting each shot of a clip by itself`;
}

/**
 * What has already been looked at, said as "Not looked at yet" is: "6
 * photographs and 2 clips (6 shots)." — never "12 items" in a folder of 8.
 * `kinds` is the server's `already_kinds`; without it, the number in items,
 * said as what it counts.
 */
export function alreadyText(already: { items: number }, kinds?: Kinds | null): string {
  const said = kinds ? kindsText(kinds) : "";
  return `${said || itemsText(already.items)}.`;
}

/** Whether a review of this would be started at all: something to look at
 *  now, or clips that would be (made ready, or judged from one still). The
 *  server refuses a review of anything less, in its own words. */
export function anythingToReview(preview: { todo: Todo; first: First }): boolean {
  const { todo, first } = preview;
  return todoItems(todo) + first.need_copy + first.need_shots + (first.copy_away ?? 0) > 0;
}

/**
 * Why Start now cannot be pressed, or null when it can. The model's own
 * sentence comes first (it may not look at all, or only overnight); then a
 * folder with nothing in it for a review. Each is the sentence the server
 * would refuse with, so the button says no before a request has to.
 */
export function startBlock(preview: {
  todo: Todo;
  first: First;
  already: { items: number };
  model: { why_not_now: string | null } | null;
}): string | null {
  const model = preview.model?.why_not_now ?? null;
  if (model) return model;
  if (anythingToReview(preview)) return null;
  return preview.already.items > 0
    ? "Everything here has been looked at."
    : "There is nothing here a model can look at.";
}

interface Tonight {
  starts_at: string | null;
  open_now: boolean;
  problem: string | null;
}

/**
 * Why Tonight cannot be pressed, or null when it can: the window is open
 * now (Start now uses it), there is no window, or the model may not look.
 * All three are the server's sentences. Nothing to do is said once, beside
 * Start now.
 */
export function tonightBlock(preview: {
  tonight: Tonight;
  model: { tonight_problem: string | null } | null;
}): string | null {
  return preview.tonight.problem ?? preview.model?.tonight_problem ?? null;
}

/**
 * The Tonight button: "Tonight (21:30)". The time is the Mac's own wall
 * clock as the server wrote it ("2026-11-13T21:30:00", no zone), so it is
 * read off the text, never through a Date, which would move it to the
 * browser's zone. While the window is open, or there is none, just
 * "Tonight".
 */
export function tonightLabel(tonight: Tonight): string {
  const clock = /T(\d{2}:\d{2})/.exec(tonight.starts_at ?? "")?.[1];
  return clock && !tonight.open_now ? `Tonight (${clock})` : "Tonight";
}

/** A model in the dialog's choice: "gemma-4-e4b-it-4bit — about 5.8 GB, fits
 *  by day", or "… — about 12.6 GB, overnight only". One whose program is not
 *  installed here is offered as not available, since a review of it would
 *  only fail: "demo-vision-gguf — not available: the program that runs it is
 *  not installed". The server's own sentence, with where to install it, is
 *  said beside the choice. */
export function modelChoiceText(model: {
  name: string;
  memory_gb: number;
  fits: string;
  program_missing?: boolean;
}): string {
  if (model.program_missing) return `${model.name} — ${PROGRAM_NOT_INSTALLED}`;
  const size = Number.isFinite(model.memory_gb) ? `about ${Number(model.memory_gb.toFixed(1))} GB, ` : "";
  return `${model.name} — ${size}${model.fits === "night" ? "overnight only" : "fits by day"}`;
}

/** How a model whose program is not installed reads in the dialog's choice. */
export const PROGRAM_NOT_INSTALLED = "not available: the program that runs it is not installed";

/**
 * The sentence to say beside the dialog's model choice when the model chosen
 * (the Looker by default) cannot be used because its program is not
 * installed here: the server's own (`problem`), "" otherwise. Start stays
 * off with it (`startBlock` reads the same sentence as `why_not_now`), and
 * the page links it to Software that runs models.
 */
export function programMissingText(
  model: { problem: string | null; program_missing?: boolean } | null,
): string {
  if (!model?.program_missing) return "";
  return model.problem ?? "The program that runs this model is not installed.";
}

/** What ticking "redo" adds, as last counted with it unticked: in items, and
 *  by kind (the server's `by_another_kinds`). */
export interface RedoMemory {
  items: number;
  kinds: Kinds | null;
}

/** Nothing remembered: what the dialog starts with, and goes back to once a
 *  review has ended (that review looked at what the memory counted). */
export const NO_REDO_MEMORY: RedoMemory = { items: 0, kinds: null };

/**
 * What the dialog remembers after a preview arrives. Counted with redo
 * unticked, the preview is the truth and is kept. With it ticked, the
 * preview may no longer count what redo adds (those items are now "to look
 * at"), so the memory from before is kept, and the box still says how many.
 */
export function rememberRedo(
  redo: boolean,
  remembered: RedoMemory,
  preview: { already: { by_another_model: number }; by_another_kinds?: Kinds | null },
): RedoMemory {
  if (redo) return remembered;
  return { items: preview.already.by_another_model, kinds: previewKinds(preview).byAnother };
}

/**
 * What the redo box says it adds: the preview's count, or, while it is
 * ticked, the remembered one when that is larger. A review that has ended
 * looked at what was remembered, so the dialog forgets it then
 * (`NO_REDO_MEMORY`, redo unticked) and the next preview is believed: else
 * "Also look again at the 6 photographs …" outlived the review that did it.
 */
export function redoShown(redo: boolean, remembered: RedoMemory, counted: RedoMemory): RedoMemory {
  return redo && remembered.items > counted.items ? remembered : counted;
}

/**
 * Under the "Redo with the current model" box: how much ticking it adds, or
 * that it would add nothing. `byAnother` is what another model, or an older
 * version of the question, already looked at, in items; `kinds` the same by
 * kind (the server's `by_another_kinds`), said as the other rows are.
 */
export function redoText(byAnother: number, kinds?: Kinds | null): string {
  if (byAnother <= 0) return "Nothing here was looked at by another model.";
  const said = kinds ? kindsText(kinds) : "";
  return said
    ? `Also look again at the ${said} a different model, or an older version of the question, already looked at.`
    : `Also look again at what a different model, or an older version of the question, already looked at: ${itemsText(byAnother)}.`;
}

/** Items a review tried before and could not look at, which it tries again;
 *  "" when there are none. */
export function failedBeforeText(failed: number): string {
  if (failed <= 0) return "";
  return failed === 1
    ? "1 of these was tried before and could not be looked at; it will be tried again."
    : `${failed.toLocaleString("en-US")} of these were tried before and could not be looked at; they will be tried again.`;
}

/**
 * What ticking "Look at new footage with AI" on a library means, and why it
 * may not do what was hoped: the first line always, the others only where
 * they apply. The box is about what arrives from now on; what is already in
 * the library is a review's to look at.
 */
export function ingestNotes(library: { on: boolean; makesCopies: boolean; hasLooker: boolean }): string[] {
  const lines = [
    "New photographs and clips are looked at as they come in. What is already here is not — use Review with AI for that.",
  ];
  if (!library.on) return lines;
  if (!library.makesCopies) {
    lines.push(
      "This library does not make playable copies, so new clips wait until you review their folder.",
    );
  }
  if (!library.hasLooker) {
    lines.push("No model has the Looker role yet, so nothing will be looked at until one does.");
  }
  return lines;
}

// ---- a review, while it runs and after -----------------------------------------------

/** Asked before a review is stopped, in the dialog and on Processing alike. */
export const CANCEL_QUESTION = "Stop this review? What it has already looked at is kept.";

/**
 * Clips a review is having made ready (a playable copy, their shots), or ""
 * when it asked for none: "3 of 12 clips made ready." Once that is over,
 * what could not be made ready is said, since those are judged from one
 * still.
 */
export function preparingText(preparing: {
  state: string;
  total: number;
  ready: number;
  gave_up: number;
}): string {
  if (preparing.total <= 0) return "";
  const made = `${preparing.ready.toLocaleString("en-US")} of ${count(preparing.total, "clip", "clips")} made ready`;
  if (preparing.gave_up <= 0) return `${made}.`;
  return `${made}; ${preparing.gave_up.toLocaleString("en-US")} could not be.`;
}

/** What a review has put on the Tags page so far, or "" when nothing: "310
 *  tags suggested; 4 of the tags are new." The first counts suggestions (a
 *  tag on a file), the second tags nobody had used before. */
export function suggestedText(review: { tags_suggested: number; new_tags: number }): string {
  if (review.tags_suggested <= 0) return "";
  const suggested = count(review.tags_suggested, "tag suggested", "tags suggested");
  if (review.new_tags <= 0) return `${suggested}.`;
  return `${suggested}; ${
    review.new_tags === 1
      ? "1 of the tags is new"
      : `${review.new_tags.toLocaleString("en-US")} of the tags are new`
  }.`;
}

/**
 * Under a review that ended with clips judged from one still (the server's
 * closing sentence says how many): where to find out why, or "" when there
 * were none. Only a copy that FAILED to be made is listed on Processing; a
 * clip judged from a still because "Make clips ready first" was unticked,
 * or because its library makes no copies, has nothing there, so the
 * sentence promises only what the page holds.
 */
export function stillsOnlyWhere(review: { stills_only?: number }): string {
  const clips = review.stills_only ?? 0;
  if (!(clips > 0)) return "";
  return clips === 1
    ? "If making its playable copy failed, Processing says why under Recent failures."
    : "Where making a playable copy failed, Processing says why under Recent failures.";
}

/**
 * Where the way out of a review's state is, when it is on another page: a
 * paused review carries on once local AI is resumed, and one the model
 * stopped needs the model seen to; both are done on the AI models page. The
 * server's sentence says what happened; this says where to go, with the
 * words for the link. null for every state whose way out is on this page
 * (Cancel, wait, start again).
 */
export function reviewWayOut(review: { state: string }): { text: string; link: string; href: string } | null {
  if (review.state === "paused") {
    return { text: "Local AI is resumed on the", link: "AI models page", href: "/models" };
  }
  if (review.state === "failed") {
    return {
      text: "Test the model, or roll it back, on the",
      link: "AI models page",
      href: "/models",
    };
  }
  return null;
}

export type ReviewState =
  | "tonight"
  | "waiting"
  | "paused"
  | "looking"
  | "done"
  | "failed"
  | "cancelled";

interface Review {
  state: string;
  message: string;
  done: number;
  total: number;
  seconds_left: number | null;
  stills_only?: number;
}

/** Whether a review is still going to do something (so it is polled, and can
 *  be cancelled by whoever may). */
export function reviewActive(review: { state: string }): boolean {
  return ["tonight", "waiting", "paused", "looking"].includes(review.state);
}

/**
 * What a review's two numbers count. A review counts each photograph once,
 * each shot of a clip once, and a clip judged from one still once — not
 * files: a folder of 8 files (2 of them clips of 3 shots) is 10 to look at,
 * and "10 of 10" beside a folder of 8 read as wrong. The review's row has
 * no photograph count, so the photographs are always named.
 */
export function reviewUnit(review: { stills_only?: number }): string {
  return (review.stills_only ?? 0) > 0 ? "photographs, shots and clips" : "photographs and shots";
}

/** Whether a review's two numbers can be said as "N of M": never with N
 *  past M. A row stored by an older build can hold "20 of 8"; such a count
 *  is said without its total rather than as more than all of it. */
function countFits(done: number, total: number): boolean {
  return done <= total;
}

/** "120 of 505 photographs and shots", for beside a review's bar; "20
 *  photographs and shots" when the count has passed its total. */
export function progressText(review: { done: number; total: number; stills_only?: number }): string {
  const done = review.done.toLocaleString("en-US");
  if (!countFits(review.done, review.total)) return `${done} ${reviewUnit(review)}`;
  return `${done} of ${review.total.toLocaleString("en-US")} ${reviewUnit(review)}`;
}

// The server's closing sentences ("Finished: 4 of 4 were looked at.",
// "Cancelled. 120 of 505 were looked at; …") count the same things as the
// bar; the unit is put in so they do not read as files either.
const ENDED_COUNT = /^(Finished: |Cancelled\. )([\d,]+) of ([\d,]+) were looked at/;

/**
 * A review's one sentence. The server's message is the sentence (what it is
 * waiting for, why it stopped, how it ended — its count given its unit);
 * while it is looking, the
 * message is only the file it is on, so that is said as such, with the time
 * to go when the server has measured one. A sentence sent while looking
 * (the model still loading, say) is left as the sentence it is: it ends in
 * a full stop, which a file's name does not.
 */
export function reviewLine(review: Review): string {
  const message = review.message.trim();
  if (review.state !== "looking") {
    return message.replace(ENDED_COUNT, (_, lead: string, done: string, total: string) =>
      countFits(Number(done.replace(/,/g, "")), Number(total.replace(/,/g, "")))
        ? `${lead}${done} of ${total} ${reviewUnit(review)} were looked at`
        : `${lead}${done} ${reviewUnit(review)} were looked at`,
    );
  }
  const sentence = /[.…!?]$/.test(message);
  const on = sentence ? message : message ? `Looking at ${message}.` : "Looking.";
  if (review.seconds_left === null || review.done >= review.total) return on;
  const left = lengthText(review.seconds_left);
  return `${on} ${left === "under a minute" ? "Under a minute" : `About ${left}`} to go.`;
}

/** The pill for a review's state, in the page's words: "failed" is shown as
 *  Stopped (the sentence beside it says why). */
export function reviewStateWord(state: string): string {
  const words: Record<string, string> = {
    tonight: "Tonight",
    waiting: "Waiting",
    paused: "Paused",
    looking: "Looking",
    done: "Done",
    failed: "Stopped",
    cancelled: "Cancelled",
  };
  return words[state] ?? state;
}

/** How that pill is tinted, in the dialog and on Processing alike: amber
 *  while it is looking, sage when it finished, red when it stopped. Waiting,
 *  paused and cancelled are plain. */
export function reviewTone(state: string): "ok" | "warn" | "bad" | undefined {
  if (state === "looking") return "warn";
  if (state === "done") return "ok";
  return state === "failed" ? "bad" : undefined;
}

// ---- Processing: the reviews, and new footage library by library -----------------------

/** What a review covers, as a row's name: "GELCO › Pete Dye › Drone", or
 *  "GELCO (the whole library)". */
export function reviewPlace(review: { library_name: string; folder: string }): string {
  const library = review.library_name || "A library";
  const names = folderParts(review.folder).map((part) => part.name);
  return names.length ? [library, ...names].join(" › ") : `${library} (the whole library)`;
}

/** Where a review's folder is in Browse, so its row leads to the footage. */
export function reviewHref(review: { library_id: string; folder: string }): string {
  return browseHref(folderQuery("", review.library_id, review.folder));
}

/** How far along a review's bar is, 0 to 100. A review that counted more
 *  than it was asked for still fills the bar and no further. */
export function reviewPercent(review: { done: number; total: number }): number {
  if (!(review.total > 0)) return 0;
  return Math.max(0, Math.min(100, (review.done / review.total) * 100));
}

interface IngestLine {
  library_name: string;
  waiting: number;
  state: string;
  message: string;
}

/**
 * One library that has its new footage looked at as it arrives: "New
 * footage in GELCO: 14 waiting — looking at IMG_2291.JPG." The server's
 * message follows what waits: while it is looking that is the file it is on
 * (said as such, as a review's is); in every other state it is a sentence —
 * waiting its turn, paused, or, for a library left alone after the model
 * failed, that looking stopped and when it is tried again.
 */
export function ingestText(line: IngestLine): string {
  const head = `New footage in ${line.library_name || "a library"}: ${
    line.waiting > 0 ? `${line.waiting.toLocaleString("en-US")} waiting` : "nothing waiting"
  }`;
  const message = line.message.trim();
  if (!message) return `${head}.`;
  if (line.state === "looking" && !/[.…!?]$/.test(message)) return `${head} — looking at ${message}.`;
  return `${head}. ${message}`;
}

/** Whether that library's looking has stopped (it is held for some hours
 *  after a failure): the line is then shown as a problem, not as progress. */
export function ingestStopped(line: { state: string }): boolean {
  return line.state === "failed";
}

// ---- Tags: a local model's suggestion ---------------------------------------------------

interface Suggested {
  origin: string;
  model?: string | null;
  demo?: boolean;
  at_ms?: number | null;
}

/**
 * The picture of the moment a suggestion is about, as the media address
 * takes it ("frame?ts=8331"), or null when it is about a photograph or a
 * whole clip, which keep the picture they always had. A local model is
 * shown one shot of a clip; the tile shows that shot, not the clip's first
 * frame, so the question is asked about what the model saw.
 */
export function momentPicture(suggestion: { at_ms?: number | null }): string | null {
  const at = suggestion.at_ms;
  return typeof at === "number" && Number.isFinite(at) && at >= 0 ? `frame?ts=${Math.round(at)}` : null;
}

/** That moment in seconds, where the viewer starts the clip; undefined when
 *  there is none (the clip starts where it always did). */
export function momentSeconds(suggestion: { at_ms?: number | null }): number | undefined {
  const at = suggestion.at_ms;
  return typeof at === "number" && Number.isFinite(at) && at >= 0 ? at / 1000 : undefined;
}

/**
 * Who suggested a tag, for the line under a tile: the page's name for the
 * origin, "(made up)" after it when the demonstration invented it, and for
 * a local model its name as the tooltip (several can have looked at one
 * library). An origin the page has no name for is shown as it came.
 */
export function suggestedBy(
  suggestion: Suggested,
  names: Record<string, string>,
): { label: string; title: string | undefined } {
  const name = names[suggestion.origin] ?? suggestion.origin;
  const local = suggestion.origin === "local";
  return {
    label: local && suggestion.demo ? `${name} (made up)` : name,
    title: local && suggestion.model ? `${suggestion.model}, a model on this Mac` : undefined,
  };
}

/** The local card on Tags: how many of a local model's suggestions wait. */
export function localWaitingText(pending: number): string {
  return pending > 0 ? `${pending.toLocaleString("en-US")} waiting.` : "None waiting.";
}

/** Where a local model's suggestions come from, said on that card. */
export const LOCAL_SUGGESTIONS_FROM =
  "They come from reviews you start on a folder in Browse and from new footage in libraries you have switched on.";

// ---- the AI models page: what the Looker does ------------------------------------------

/** How many pictures of a shot the Looker is shown. The server takes 1 or 3. */
export const STILLS_CHOICES: ReadonlyArray<{ value: number; label: string }> = [
  { value: 3, label: "3 — start, middle, end" },
  { value: 1, label: "1 — the middle" },
];

/** Under that choice: when to pick the smaller one. */
export const STILLS_NOTE =
  "Use 1 if a model gives poor answers on clips; some models only read one picture well.";

/** "41 suggestions waiting on Tags", for the line under the Looker's settings. */
export function waitingOnTagsText(pending: number): string {
  if (pending <= 0) return "No suggestions waiting on Tags";
  return `${count(pending, "suggestion", "suggestions")} waiting on Tags`;
}

/**
 * The reviews under way, for the same line: "2 reviews running", "1 review
 * running and 1 waiting", "1 review waiting", "no review running". Running
 * is a review the model is looking for right now; one set for tonight,
 * waiting its turn or paused is waiting.
 */
export function reviewsUnderWayText(reviews: ReadonlyArray<{ state: string }>): string {
  const running = reviews.filter((review) => review.state === "looking").length;
  const waiting = reviews.filter((review) => reviewActive(review)).length - running;
  if (running > 0 && waiting > 0) {
    return `${count(running, "review", "reviews")} running and ${waiting.toLocaleString("en-US")} waiting`;
  }
  if (running > 0) return `${count(running, "review", "reviews")} running`;
  if (waiting > 0) return `${count(waiting, "review", "reviews")} waiting`;
  return "no review running";
}

/** What saving one of the Looker's two settings says. They apply to what is
 *  looked at from now on: nothing already looked at is looked at again. */
export function lookSettingSaved(changed: { new_tags?: boolean; stills_per_shot?: number }): string {
  if (typeof changed.new_tags === "boolean") {
    return changed.new_tags
      ? "Saved. The Looker may propose new tags from now on."
      : "Saved. The Looker only suggests tags you already use from now on.";
  }
  if (typeof changed.stills_per_shot === "number") {
    return changed.stills_per_shot === 1
      ? "Saved. The Looker is shown one picture of each shot from now on."
      : `Saved. The Looker is shown ${changed.stills_per_shot} pictures of each shot from now on.`;
  }
  return "Saved.";
}

// ---- where a look shows: an asset's page, its shots, Search ------------------------------

/** Under every caption and score: whose word it is. */
export const OPINION_NOTE = "A local model's opinion, not a fact.";

/** A file nothing has looked at, said where its caption would be. */
export const NOT_LOOKED_AT = "Not looked at yet.";

/** A clip with a best shot but with shots still to look at. */
export const INCOMPLETE_NOTE = "Some of this clip's shots have not been looked at yet.";

/** A clip whose shots the motion analysis has not found. */
export const NO_SHOTS_NOTE = "This clip's shots have not been found yet. It needs a playable copy first.";

/** What a made-up look's pill says when it is pointed at. */
export const MADE_UP_NOTE = "Made up for the demonstration: no model looked at this.";

/**
 * A moment the server wrote, as milliseconds since 1970; NaN when it cannot
 * be read. A time with no zone is UTC: that is what the server stores, and
 * a catalogue on SQLite hands it back without the "Z". Read as the
 * browser's own time it would be hours out, and near midnight a day out.
 */
export function utcMoment(iso: string | null | undefined): number {
  if (!iso) return Number.NaN;
  const zoned = /(?:[zZ]|[+-]\d\d:?\d\d)$/.test(iso);
  return new Date(zoned ? iso : `${iso}Z`).getTime();
}

// The programs that run models, as the AI models page names them. A name
// not listed here is shown as the server recorded it.
const RUNTIME_NAMES: Record<string, string> = {
  "mlx-vlm": "MLX-VLM",
  "llama-cpp": "llama.cpp",
  demo: "demo program",
  "demo-gguf": "demo program",
};

interface SaidBy {
  model: string;
  revision_short?: string;
  variant?: string;
  runtime?: string;
  runtime_version?: string;
  prompt_version?: string;
}

/**
 * Who said it, in one line: "gemma-4-e4b-it-4bit · build 475b908 · MLX-VLM
 * 0.7.12 · look-1 · 12 Nov". The model by the last part of its name (as
 * Tags names it), the file it was read from in brackets when a model has
 * several, then the build, the program and its version, which wording of
 * the question, and the day when the page gives one. Only what the server
 * recorded is said: a part that is empty is left out, never guessed.
 */
export function lookSaidBy(look: SaidBy, day = ""): string {
  const name = look.model.slice(look.model.lastIndexOf("/") + 1);
  const program = look.runtime ? (RUNTIME_NAMES[look.runtime] ?? look.runtime) : "";
  return [
    name && look.variant ? `${name} (${look.variant})` : name,
    look.revision_short ? `build ${look.revision_short}` : "",
    [program, look.runtime_version ?? ""].filter(Boolean).join(" "),
    look.prompt_version ?? "",
    day,
  ]
    .filter(Boolean)
    .join(" · ");
}

/**
 * What a look on an asset's page is about: a photograph; one shot of a clip
 * (the best one, with its times); or a whole clip judged from one picture,
 * which is what a clip gets while its shots have not been found.
 */
export function lookAbout(
  mediaType: string,
  look: { whole: boolean; start_ms: number | null; end_ms: number | null },
): { kind: "photograph" | "shot" | "clip"; startMs: number | null; endMs: number | null } {
  if (mediaType !== "video") return { kind: "photograph", startMs: null, endMs: null };
  if (look.whole || look.start_ms === null || look.end_ms === null) {
    return { kind: "clip", startMs: null, endMs: null };
  }
  return { kind: "shot", startMs: look.start_ms, endMs: look.end_ms };
}

/**
 * Whether an asset's page has the card "What the local model saw". With a
 * look, always: an opinion that was written is shown wherever it is read.
 * Without one, only where a model could look (so the card can say "not yet"
 * and where to ask), and never for sound, which no model here looks at. On
 * an install that cannot run models the page is exactly the page it was.
 */
export function showsLookCard(asset: { media_type: string; look?: unknown }, available: boolean): boolean {
  if (asset.look) return true;
  return available && asset.media_type !== "audio";
}

interface Shot {
  idx: number;
  start_ms: number;
  end_ms: number;
  still_ms: number;
}

/**
 * The pictures to try for a shot's row, best first, as the media address
 * takes them.
 *
 * The media address serves the frames sampled for visual search (`frames`,
 * the ones the page's scene strip already lists) and nothing else: a shot's
 * own middle still is a file with no such row, so asking for it at
 * `frame?ts=<still_ms>` is answered 404 unless a sampled frame happens to
 * sit at that very moment. So, as the server stands: the sampled frame
 * nearest the middle of the shot, if one lies inside it, and otherwise
 * nothing (the row says it has no picture).
 *
 * Nothing rather than the clip's thumbnail or the nearest frame outside the
 * shot: either is a picture of a different shot, and beside this shot's
 * caption ("a pull-out over the clubhouse" next to a tree line) it reads as
 * the model being wrong. The row's button still plays the shot.
 *
 * `stillsServed` is for the day the server answers a shot's still at that
 * address: then the still comes first, and the sampled frame is what is
 * tried if its file has gone.
 */
export function shotPictures(
  shot: Shot,
  frames: ReadonlyArray<{ ts_ms: number }> = [],
  stillsServed = false,
): string[] {
  let nearest: number | null = null;
  for (const frame of frames) {
    if (frame.ts_ms < shot.start_ms || frame.ts_ms >= shot.end_ms) continue;
    if (nearest === null || Math.abs(frame.ts_ms - shot.still_ms) < Math.abs(nearest - shot.still_ms)) {
      nearest = frame.ts_ms;
    }
  }
  const kinds: string[] = [];
  if (stillsServed) kinds.push(`frame?ts=${Math.round(shot.still_ms)}`);
  if (nearest !== null) {
    const kind = `frame?ts=${Math.round(nearest)}`;
    if (!kinds.includes(kind)) kinds.push(kind);
  }
  return kinds;
}

/** Why a shot's row has no picture, for the empty box's tooltip. */
export const NO_SHOT_PICTURE_NOTE =
  "No picture of this shot has been kept: none of the clip's sampled frames falls inside it. Press to play it.";

/** How the camera moved: "orbit right", or "rise" when it has no direction. */
export function shotMove(shot: { move: string; direction: string | null }): string {
  return [shot.move, shot.direction ?? ""].filter(Boolean).join(" ");
}

/**
 * How steady a shot is, as the figure the analysis measured (0 to 1, steady
 * towards 1): "0.96". The number is shown as it is; the page does not grade
 * it into words, because where "steady enough" starts depends on the cut.
 */
export function steadinessText(smooth: number | null | undefined): string {
  if (typeof smooth !== "number" || !Number.isFinite(smooth)) return "—";
  return Math.min(1, Math.max(0, smooth)).toFixed(2);
}

/** What that figure means, for its tooltip. */
export const STEADINESS_NOTE = "Measured from the footage at normal speed. 1.00 is perfectly steady.";

/**
 * What a button that plays a shot says, to a screen reader (`label`) and on
 * hover (`title`). `what` is "shot 3" or "the best shot"; `from` and `to`
 * its times as the page shows them. Where the clip cannot play here, the
 * button only brings the reason into view, so it does not promise to play:
 * "Shot 3, 00:33 to 00:49 — this clip cannot play here".
 */
export function shotButtonWords(
  what: string,
  from: string,
  to: string,
  playable: boolean,
): { label: string; title: string } {
  if (playable) return { label: `Play ${what}, ${from} to ${to}`, title: "Play this shot" };
  const named = what.charAt(0).toUpperCase() + what.slice(1);
  return {
    label: `${named}, ${from} to ${to} — this clip cannot play here`,
    title: "This clip cannot play here. Click to see why.",
  };
}

/** The shot the player is in (its `idx`), or -1 between shots and before
 *  the player has said where it is. A shot ends where the next begins. */
export function shotAt(shots: ReadonlyArray<Shot>, playheadMs: number | null): number {
  if (playheadMs === null || !Number.isFinite(playheadMs)) return -1;
  const found = shots.find((shot) => playheadMs >= shot.start_ms && playheadMs < shot.end_ms);
  return found ? found.idx : -1;
}

/**
 * Beside the heading "Shots": "3 shots", and how many of them a model has
 * looked at where that is worth saying (`withLooks`: a model can look here,
 * or one already has).
 */
export function shotsCountText(shots: ReadonlyArray<{ look?: unknown }>, withLooks: boolean): string {
  const total = count(shots.length, "shot", "shots");
  if (!withLooks || shots.length === 0) return total;
  const looked = shots.filter((shot) => shot.look).length;
  if (looked === 0) return `${total} · none looked at yet`;
  return looked === shots.length ? `${total} · all looked at` : `${total} · ${looked} looked at`;
}

/**
 * The caption to show on a search hit where a spoken quote would be: only
 * when the hit matched by its caption and there is no quote to show. A hit
 * that matched by words someone said keeps the quote, which is a fact; the
 * caption is a model's sentence and never pushes one out.
 */
export function captionShown(hit: {
  reasons: readonly string[];
  caption?: string | null;
  snippet?: string | null;
}): string | null {
  if (!hit.reasons.includes("caption")) return null;
  if (hit.snippet) return null;
  return hit.caption ? hit.caption : null;
}
