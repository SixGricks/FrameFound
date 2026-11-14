// The words and sums of the Cuts pages (app/cuts/page.tsx, the draft form and
// the draft card; further down, one cut's page, its shot list and its
// download), kept apart from the pages so they can be tested.
//
// Nothing is imported, not even types, so `node --test` loads this file as it
// is (tests/cuts.test.mjs, cuts-page, cuts-compare and cuts-ask: the last two
// are Compare and "Ask Claude to review", at the end of this file). The
// shapes below are the few fields each helper reads; the full answers are
// typed in lib/api.ts, and TypeScript checks that those fit these wherever a
// page hands one over.
//
// A draft is a request to the Editor, a model running on the Mac. Everything
// about it is the server's: its state and its sentence are read off the
// server every time, and the page never decides that a draft has finished,
// failed or will start at some hour by itself. What is here only words what
// the server said, and checks a form before it is sent so a slip is said in
// a sentence rather than as a refusal with no words in it.

// ---- the shapes read here ------------------------------------------------------------

export type DraftState = "queued" | "waiting" | "running" | "done" | "failed" | "cancelled";

/** The Editor as the server describes it (`GET /cuts` → `editor`). */
interface Editor {
  mode: "ready" | "demo" | "unavailable";
  note: string | null;
  model_name: string | null;
  runs: "now" | "overnight" | "never" | null;
  /** The Mac's own wall clock, no zone: "2026-11-14T21:30:00". Only while it
   *  is day and the Editor fits only overnight. */
  starts_at: string | null;
  paused: boolean;
  why_not: string | null;
}

interface Scope {
  kind: "folder" | "selection";
  library_id: string | null;
  folder: string;
  contains: string;
  asset_ids: string[];
  label: string;
}

/** A draft as the server shows it (`Draft` in lib/api.ts). */
interface Draft {
  id: string;
  state: DraftState;
  stopping: boolean;
  message: string;
  title: string;
  brief: string;
  scope: Scope;
  target_s: number;
  min_shot_s: number;
  max_shot_s: number;
  exclude_used: boolean;
  shots_offered: number;
  shots_in_scope: number;
  clips: number;
  seconds: number | null;
}

/** One folder the picker offers (`GET /cuts/folders`). */
interface Folder {
  library_id: string;
  library_name: string;
  path: string;
  clips: number;
  analysed: number;
}

// ---- figures --------------------------------------------------------------------------

/** Seconds as the page writes them: "30 s", "31.5 s"; "—" for none. */
export function secondsText(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const tenths = Math.round(seconds * 10) / 10;
  return Number.isInteger(tenths) ? `${tenths} s` : `${tenths.toFixed(1)} s`;
}

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** Who made a cut, when the server's label is missing. */
export function sourceLabel(source: string): string {
  if (source === "local") return "Local draft";
  if (source === "mcp") return "Claude";
  if (source === "import") return "Imported timeline";
  return source;
}

/**
 * The note on a draft that came out shorter than asked; null when it did
 * not (within a quarter of a second, which no one would call short).
 * FrameFound never pads a draft with shots the Editor did not choose, so
 * this is said rather than fixed.
 */
export function lengthNote(total: number, asked: number | null | undefined): string | null {
  if (asked === null || asked === undefined || !(asked > 0)) return null;
  const short = asked - total;
  if (short < 0.25) return null;
  return (
    `It is ${secondsText(short)} short of the ${secondsText(asked)} asked for. ` +
    "FrameFound never adds shots the Editor did not choose."
  );
}

// ---- reading the page again -----------------------------------------------------------

/** While a draft is queued or running something will change within seconds. */
export const CUTS_FAST_MS = 3_000;
/** Waiting for tonight, or nothing under way: a cut Claude makes still shows. */
export const CUTS_SLOW_MS = 30_000;

const ACTIVE: ReadonlySet<string> = new Set(["queued", "waiting", "running"]);

export function isActive(draft: { state: string }): boolean {
  return ACTIVE.has(draft.state);
}

/**
 * How soon to read the list again: quickly while a draft is queued or being
 * drafted, slowly while one only waits for the overnight window (or for
 * Resume) and while nothing is under way.
 */
export function pollEvery(overview: { drafts: { state: string }[] }): number {
  return overview.drafts.some((draft) => draft.state === "queued" || draft.state === "running")
    ? CUTS_FAST_MS
    : CUTS_SLOW_MS;
}

/** `rereads`' clock: as `pollEvery` says for the last answer the page has,
 *  and slowly when it has none yet and the read failed. */
export function readAgainIn(
  latest: { drafts: { state: string }[] } | null,
  failed: boolean,
): number | null {
  if (latest) return pollEvery(latest);
  return failed ? CUTS_SLOW_MS : null;
}

// ---- how much of the list ---------------------------------------------------------------

/** How many cuts the list asks for at first. */
export const CUTS_FIRST = 30;
/** The most the server lists in one answer (`GET /cuts?limit=`). */
export const CUTS_MOST = 200;

/** What "Show older cuts" asks for next: twice as many, up to the most. */
export function moreCuts(limit: number): number {
  return Math.min(CUTS_MOST, Math.max(limit, CUTS_FIRST) * 2);
}

/**
 * What the list says of itself. Up to the server's most in one answer a full
 * list is the only sign that there are older cuts: it then reads "the newest
 * 30" and offers more (`more`, "Show older cuts"), where "30 shown" read as
 * if that were all of them and the thirty-first cut could not be opened from
 * anywhere. From there on the server says whether more come after
 * (`further`: the list's `more`), and "Show more" asks for the next page.
 * A server from before paging says nothing (`further` undefined), and the
 * list says it can go no further back.
 */
export function cutsShown(
  count: number,
  limit: number,
  further?: boolean,
): { text: string; more: boolean; note: string | null } {
  if (count < limit) return { text: `${count} shown`, more: false, note: null };
  if (limit < CUTS_MOST) return { text: `the newest ${count}`, more: true, note: null };
  if (further === true) return { text: `the newest ${count}`, more: false, note: null };
  if (further === false) return { text: `${count} shown`, more: false, note: null };
  return {
    text: `the newest ${count}`,
    more: false,
    note:
      `Only the newest ${CUTS_MOST} cuts can be listed here. An older one still opens from a link ` +
      "to it; deleting cuts that are no longer wanted brings older ones back into the list.",
  };
}

/** "Show more", past the most one answer holds: offered while the list asks
 *  for the most and the server says more come after what is listed. */
export function offerFurther(limit: number, further: boolean | undefined): boolean {
  return limit >= CUTS_MOST && further === true;
}

/** The pages after the first that "Show more" has brought into the list,
 *  `beyond` cuts in all, one server answer (`CUTS_MOST`) each. Each reading
 *  asks for all of them again, so they stay as current as the first page. */
export function laterPages(beyond: number): { offset: number; limit: number }[] {
  const pages: { offset: number; limit: number }[] = [];
  for (let offset = CUTS_MOST; offset < CUTS_MOST + beyond; offset += CUTS_MOST) {
    pages.push({ offset, limit: CUTS_MOST });
  }
  return pages;
}

/**
 * The cut list as one, from its first page and the later ones. A cut made
 * between the requests pushes every page along by one, so a family can
 * arrive on two pages: it is listed once, where it came first. Whether more
 * come after is what the last page said.
 */
export function joinPages<T extends { root_id: string }>(
  first: { cuts: T[]; more?: boolean },
  later: { cuts: T[]; more?: boolean }[],
): { cuts: T[]; more?: boolean } {
  const seen = new Set<string>();
  const cuts: T[] = [];
  for (const page of [first, ...later]) {
    for (const family of page.cuts) {
      if (seen.has(family.root_id)) continue;
      seen.add(family.root_id);
      cuts.push(family);
    }
  }
  const last = later[later.length - 1] ?? first;
  return { cuts, more: last.more };
}

/**
 * Drafts that were under way in the previous answer and are not now: gone
 * from the list (a finished draft becomes a cut and leaves it) or listed as
 * failed or cancelled. The page reads each one to say how it ended.
 */
export function endedDrafts(
  before: { drafts: { id: string; state: string }[] } | null,
  after: { drafts: { id: string; state: string }[] },
): string[] {
  if (!before) return [];
  const now = new Map(after.drafts.map((draft) => [draft.id, draft.state]));
  return before.drafts
    .filter((draft) => isActive(draft))
    .filter((draft) => {
      const state = now.get(draft.id);
      return state === undefined || !ACTIVE.has(state);
    })
    .map((draft) => draft.id);
}

/**
 * Whether "Asked. … is under In progress below" (or "under Drafts of this
 * cut") is still true of the reading `drafts` came from: only while that
 * draft is listed as under way. Asked once a draft has been asked for, of
 * every reading after it; once it has finished, failed or been cancelled
 * the page says how it ended, and the sentence promising it had stayed
 * beside "Version 2 is ready." pointing at a section that was gone. A draft
 * that finished before the first reading after it is not listed at all, and
 * the sentence goes as well.
 */
export function askedStands(draftId: string, drafts: { id: string; state: string }[]): boolean {
  return drafts.some((draft) => draft.id === draftId && isActive(draft));
}

/** What the page says when a draft ends while it is open. */
export function endedSentence(draft: { title: string; state: string; message: string }): string {
  const title = `“${draft.title}”`;
  if (draft.state === "done") return `${title} is ready.`;
  if (draft.state === "cancelled") return `${title} was cancelled.`;
  if (draft.state === "failed") {
    return draft.message ? `${title} did not finish. ${draft.message}` : `${title} did not finish.`;
  }
  return `${title}: ${draft.message || "it is still under way."}`;
}

// ---- a draft --------------------------------------------------------------------------

export const STOPPING = "Stopping — the answer under way is thrown away when it arrives.";

/**
 * The sentence a draft card leads with. The server's own sentence when it
 * gives one ("Waiting for the overnight window (starts 21:30): …"), except
 * while a cancel waits for the answer under way, which no server sentence
 * says.
 */
export function draftSentence(draft: Draft): string {
  if (draft.stopping && isActive(draft)) return STOPPING;
  if (draft.message) return draft.message;
  switch (draft.state) {
    case "queued":
      return "Waiting its turn — the Editor does one thing at a time.";
    case "waiting":
      return "Waiting to start.";
    case "running":
      return draft.shots_offered
        ? `The Editor is choosing from ${plural(draft.shots_offered, "shot")}.`
        : "The Editor is drafting it.";
    case "done":
      return draft.seconds === null ? "Ready." : `Ready: ${secondsText(draft.seconds)}.`;
    case "failed":
      return "It did not finish.";
    case "cancelled":
      return "Cancelled.";
  }
  return draft.message;
}

/** "62 of its 480 analysed shots, from 41 clips." — what the Editor was
 *  given to choose from; null before it has been worked out. */
export function offerLine(draft: Draft): string | null {
  if (!draft.shots_offered) return null;
  const from = draft.clips ? `, from ${plural(draft.clips, "clip")}` : "";
  if (draft.shots_in_scope > draft.shots_offered) {
    return `The Editor is offered ${draft.shots_offered} of the ${draft.shots_in_scope} analysed shots${from}.`;
  }
  return `The Editor is offered ${plural(draft.shots_offered, "analysed shot")}${from}.`;
}

/** The length and shot lengths a draft was asked for, in one line. */
export function askedLine(draft: Draft): string {
  const used = draft.exclude_used ? " · leaving out shots already used" : "";
  return (
    `${secondsText(draft.target_s)} · shots ${secondsText(draft.min_shot_s)} to ` +
    `${secondsText(draft.max_shot_s)}${used}`
  );
}

function shortId(id: string): string {
  return id.slice(0, 8);
}

/**
 * What "Ask Claude instead" puts in the chat panel's text box: the brief and
 * the footage as a sentence, nothing attached. The person reads it and
 * presses Send; nothing is sent from here. Clips ticked in Search are named
 * by their short ids (every tool accepts them), at most 30 of them.
 */
export function askClaudeText(draft: Draft): string {
  let footage: string;
  if (draft.scope.kind === "folder" && draft.scope.folder) {
    footage = `the folder ${draft.scope.label || draft.scope.folder}`;
  } else if (draft.scope.kind === "folder" && draft.scope.contains) {
    footage = `the folders matching "${draft.scope.contains}"`;
  } else {
    const ids = draft.scope.asset_ids;
    const named = ids.slice(0, 30).map(shortId).join(", ");
    const more = ids.length > 30 ? ` and ${ids.length - 30} more` : "";
    footage = ids.length ? `these clips (${named}${more})` : draft.scope.label || "the clips";
  }
  return (
    `Please make a rough cut for Premiere from ${footage}, about ${secondsText(draft.target_s)} ` +
    `long. The brief: ${draft.brief.trim()}`
  );
}

// ---- the Editor, before the button is pressed ------------------------------------------

export type EditorTone = "ok" | "wait" | "alert";

export interface EditorLine {
  tone: EditorTone;
  /** A short pill before the sentence ("overnight only"), or null. */
  pill: string | null;
  text: string;
}

/** "21:30" from the Mac's wall clock "2026-11-14T21:30:00"; null if unreadable. */
export function clockOf(naive: string | null): string | null {
  if (!naive) return null;
  const found = /T(\d{2}):(\d{2})/.exec(naive);
  return found ? `${found[1]}:${found[2]}` : null;
}

/**
 * The one line about who will draft, from what the server said of the
 * Editor. Nothing here works out a window or a budget: an overnight start
 * time is said only when the server gave one (it gives one only by day).
 */
export function editorLine(editor: Editor): EditorLine {
  if (editor.why_not) return { tone: "alert", pill: null, text: editor.why_not };
  const name = editor.model_name ?? "The Editor";
  if (editor.paused) {
    return {
      tone: "wait",
      pill: "paused",
      text: "Local AI is paused, so a draft waits until it is resumed — on the AI models page.",
    };
  }
  const at = clockOf(editor.starts_at);
  if (editor.runs === "overnight" && at) {
    return {
      tone: "wait",
      pill: "overnight only",
      text:
        `The Editor (${name}) needs more memory than the daytime limit, so a draft you ask for ` +
        `now starts at ${at}. To draft by day, raise the daytime memory or give the Editor role ` +
        "to a smaller model — on the AI models page.",
    };
  }
  return {
    tone: "ok",
    pill: null,
    text: `${name} will draft it. It can take a few minutes; you can leave this page.`,
  };
}

/** The demonstration banner on the Cuts page. */
export const DEMO_DRAFTS =
  "The draft is not a model's work: shots are taken evenly from the list.";

// ---- the folder picker ----------------------------------------------------------------

/** "GELCO › 2026/Pete Dye" */
export function folderLabel(folder: { library_name: string; path: string }): string {
  return folder.library_name ? `${folder.library_name} › ${folder.path}` : folder.path;
}

/** "41 clips" */
export function folderHint(folder: { clips: number }): string {
  return plural(folder.clips, "clip");
}

/** The server's sentence for a folder none of whose clips has shots yet. */
export const NO_SHOTS_FOLDER =
  "None of the clips in this folder have been analysed into shots yet. That happens after a " +
  "clip's preview copy is made — Processing shows how far along it is.";

/**
 * What to say under a chosen folder: nothing when every clip has shots; how
 * many are not offered yet when some have none; and, when none has, the
 * refusal the server would give (`blocks`: the Draft button waits).
 */
export function folderReadiness(folder: Folder): { text: string; blocks: boolean } | null {
  if (folder.clips <= 0) return null;
  if (folder.analysed <= 0) return { text: NO_SHOTS_FOLDER, blocks: true };
  if (folder.analysed >= folder.clips) return null;
  const rest = folder.clips - folder.analysed;
  return {
    text:
      `${folder.analysed} of its ${plural(folder.clips, "clip")} ` +
      `${folder.analysed === 1 ? "has" : "have"} been analysed into shots; ` +
      `the other ${rest} ${rest === 1 ? "is" : "are"} not offered yet.`,
    blocks: false,
  };
}

/** The key the picker gives a folder: a path is only unique inside its library. */
export function folderKey(folder: { library_id: string; path: string }): string {
  return `${folder.library_id}|${folder.path}`;
}

/**
 * The folder a picker row's key names. A row carries everything a pick
 * needs, so a pick never depends on which list the form heard back last: an
 * earlier, broader search that answered late used to leave the row on screen
 * unknown to the form, and the click on it did nothing. A library's id has
 * no "|" in it; a path may.
 */
export function folderFromKey(key: string): { library_id: string; path: string } | null {
  const at = key.indexOf("|");
  if (at <= 0 || at === key.length - 1) return null;
  return { library_id: key.slice(0, at), path: key.slice(at + 1) };
}

// ---- the form -------------------------------------------------------------------------

export const LENGTHS: readonly number[] = [15, 30, 60, 90];
export const BRIEF_MAX = 1000;
export const TITLE_MAX = 120;
export const DEFAULT_LENGTH = 30;
export const DEFAULT_SHORTEST = 1.5;
export const DEFAULT_LONGEST = 8;

/** The form as typed. Numbers stay text until sent, so "7." can be typed. */
export interface DraftForm {
  brief: string;
  title: string;
  /** null: no folder chosen (or clips ticked in Search instead). */
  folder: { library_id: string; path: string } | null;
  /** Clips ticked in Search; used instead of a folder when given. */
  assetIds: string[] | null;
  /** The chat's "folders matching": these letters anywhere in a folder's
   *  path, in one library or (null) in any. The picker never makes one;
   *  "Try again" on a draft Claude asked for this way does. */
  matching: { library_id: string | null; contains: string } | null;
  /** The draft's own words for `assetIds` or `matching` ("9 clips named in
   *  the chat"), so the form can say which footage it still holds. */
  footageLabel: string | null;
  lengthText: string;
  shortestText: string;
  longestText: string;
  excludeUsed: boolean;
  /** "Draft again": the cut this is the next version of. */
  revises: string | null;
}

export function blankForm(): DraftForm {
  return {
    brief: "",
    title: "",
    folder: null,
    assetIds: null,
    matching: null,
    footageLabel: null,
    lengthText: String(DEFAULT_LENGTH),
    shortestText: String(DEFAULT_SHORTEST),
    longestText: String(DEFAULT_LONGEST),
    excludeUsed: false,
    revises: null,
  };
}

/**
 * The footage a draft was asked for, as the form holds it: a folder the
 * picker could have given, the clips named one by one, or the chat's
 * "folders matching". Whatever it was, the form can ask for it again —
 * "Try again" used to come back with an empty footage field for everything
 * but a picked folder.
 */
function footageOf(scope: Scope): Pick<DraftForm, "folder" | "assetIds" | "matching" | "footageLabel"> {
  const none = { folder: null, assetIds: null, matching: null, footageLabel: null };
  if (scope.kind === "selection") {
    if (!scope.asset_ids.length) return none;
    return { ...none, assetIds: [...scope.asset_ids], footageLabel: scope.label || null };
  }
  if (scope.folder) {
    // A folder with no library is not one the picker offers; it is chosen again.
    return scope.library_id ? { ...none, folder: { library_id: scope.library_id, path: scope.folder } } : none;
  }
  const contains = scope.contains.trim();
  if (!contains) return none;
  return { ...none, matching: { library_id: scope.library_id, contains }, footageLabel: scope.label || null };
}

/** The form filled in as a draft was asked for ("Try again"). */
export function formFromDraft(draft: Draft & { revises_cut_id?: string | null }): DraftForm {
  return {
    brief: draft.brief,
    title: draft.title,
    ...footageOf(draft.scope),
    lengthText: String(draft.target_s),
    shortestText: String(draft.min_shot_s),
    longestText: String(draft.max_shot_s),
    excludeUsed: draft.exclude_used,
    revises: draft.revises_cut_id ?? null,
  };
}

/** The body `POST /cuts/drafts` takes. */
export interface DraftBody {
  brief: string;
  title: string;
  scope: {
    kind: "folder" | "selection";
    library_id?: string;
    folder?: string;
    /** "Folders matching": these letters anywhere in the path. */
    contains?: string;
    asset_ids?: string[];
  };
  target_s: number;
  min_shot_s: number;
  max_shot_s: number;
  exclude_used: boolean;
  revises?: string;
}

function number(text: string): number | null {
  const clean = text.trim().replace(",", ".");
  if (!clean) return null;
  const value = Number(clean);
  return Number.isFinite(value) ? value : null;
}

/**
 * The request the form makes, or the sentence that says what to change
 * first. The limits are the server's; checking them here means a slip is
 * said in words, where the server would answer only "Invalid request".
 */
export function draftBody(form: DraftForm): { body: DraftBody } | { problem: string } {
  const brief = form.brief.trim();
  if (!brief) return { problem: "Say what the video should be first." };
  if (brief.length > BRIEF_MAX) {
    return { problem: `The brief is ${brief.length} characters; at most ${BRIEF_MAX} are read.` };
  }
  const title = form.title.trim();
  if (title.length > TITLE_MAX) {
    return { problem: `The title is ${title.length} characters; at most ${TITLE_MAX} fit.` };
  }
  let scope: DraftBody["scope"];
  if (form.assetIds && form.assetIds.length) {
    scope = { kind: "selection", asset_ids: [...form.assetIds] };
  } else if (form.folder) {
    scope = { kind: "folder", library_id: form.folder.library_id, folder: form.folder.path };
  } else if (form.matching && form.matching.contains.trim()) {
    scope = { kind: "folder", contains: form.matching.contains.trim() };
    if (form.matching.library_id) scope.library_id = form.matching.library_id;
  } else {
    return { problem: "Choose a folder of footage first." };
  }
  const target = number(form.lengthText);
  if (target === null || target < 5 || target > 600) {
    return { problem: "The length is seconds, from 5 to 600." };
  }
  const shortest = number(form.shortestText);
  if (shortest === null || shortest < 0.5 || shortest > 30) {
    return { problem: "The shortest shot is seconds, from 0.5 to 30." };
  }
  const longest = number(form.longestText);
  if (longest === null || longest < 1 || longest > 60) {
    return { problem: "The longest shot is seconds, from 1 to 60." };
  }
  if (shortest > longest) return { problem: "The shortest shot cannot be longer than the longest." };
  const body: DraftBody = {
    brief,
    title,
    scope,
    target_s: target,
    min_shot_s: shortest,
    max_shot_s: longest,
    exclude_used: form.excludeUsed,
  };
  if (form.revises) body.revises = form.revises;
  return { body };
}

/**
 * What the form says of footage that is not a picked folder: the clips or
 * the "folders matching" a draft was asked for, kept when Try again fills
 * the form in. Null when the form holds a folder, or nothing yet.
 */
export function footageWords(form: DraftForm): string | null {
  if (form.assetIds && form.assetIds.length) {
    return `The ${form.footageLabel || `${plural(form.assetIds.length, "clip")} it was asked for`}`;
  }
  if (form.folder) return null;
  const contains = form.matching?.contains.trim();
  if (!contains) return null;
  const label = form.footageLabel || `folders matching "${contains}"`;
  return label.charAt(0).toUpperCase() + label.slice(1);
}

/** Said in place of the server's "No such cut." when the cut a draft was to
 *  be the next version of has been deleted. */
export const REVISED_GONE =
  "The cut this was to be the next version of has been deleted, so it will be a new cut.";

/**
 * On the Cuts list, where no cut is on screen: what a form that revises a
 * cut will do. `title` is the cut's, once it has been looked up. Without
 * this line a draft tried again was filed under the old cut with nothing on
 * the page saying so, and could not be asked for as a new cut.
 */
export function revisesLine(title: string | null): string {
  return title
    ? `The result will be kept as the next version of “${title}”, not as a new cut.`
    : "The result will be kept as the next version of the cut this draft was for, not as a new cut.";
}

/**
 * Whether a refusal of the form is the server's "No such cut." for the cut
 * the form revises: a 404 can only be that (a folder or a clip that is gone
 * is a 400 with its own sentence).
 */
export function revisedIsGone(status: number, revises: string | null | undefined): boolean {
  return status === 404 && Boolean(revises);
}

/**
 * Where a refusal of the form points, besides its sentence: the AI models
 * page when the Editor is the problem (409), Processing when the footage has
 * not been analysed into shots yet. Read off the status and the server's
 * sentence; null when there is nowhere better to go.
 */
export function refusalLink(status: number, message: string): { href: string; label: string } | null {
  if (status === 409 && !/already waiting/i.test(message)) {
    return { href: "/models#roles", label: "AI models" };
  }
  if (status === 400 && /analysed into shots/i.test(message)) {
    return { href: "/processing", label: "Processing" };
  }
  return null;
}

// ---- the other ways in: a folder named by a link, clips ticked in Search ----------------
//
// Search, a clip's page and a folder in Browse each lead to the form on the
// Cuts page with the footage already chosen. What they hand over is in the
// address and nowhere else, so the form a reload shows is the form the link
// opened: nothing is kept where it cannot be seen, and nothing is dropped
// without a word.

/**
 * Where "Draft a cut from this folder" leads: the Cuts page with the folder
 * chosen as the footage. Null at the top of a library — a draft is always
 * of a folder or of named clips, never of a whole library — so no link is
 * drawn there.
 */
export function draftFolderHref(libraryId: string, folder: string): string | null {
  const path = folder.replace(/^\/+|\/+$/g, "");
  if (!libraryId || !path) return null;
  return `/cuts?library=${encodeURIComponent(libraryId)}&folder=${encodeURIComponent(path)}`;
}

/** A folder's clips as Browse › Folders counts them: directly in it, and beneath it. */
export interface DraftCounts {
  here: { clips: number };
  inside: { clips: number };
}

/**
 * Whether Browse asks the form's picker about a folder before offering to
 * draft from it: when some of its clips lie in folders inside it.
 */
export function draftAsksPicker(counts: DraftCounts): boolean {
  return counts.inside.clips > counts.here.clips;
}

/** Whether the picker's answer lists this folder of this library. */
export function pickerOffers(
  rows: readonly { library_id: string; path: string }[],
  libraryId: string,
  folder: string,
): boolean {
  const wanted = folderKey({ library_id: libraryId, path: folder });
  return rows.some((row) => folderKey(row) === wanted);
}

/**
 * "Draft a cut from this folder" for the folder Browse › Folders shows. A
 * folder draft takes everything beneath the folder. Where every clip is
 * directly in it (a shoot), it is offered as it always was. Where clips lie
 * in folders inside it ("GELCO › 2026", or a client's whole folder), only
 * once the form's picker lists it (`offered`, null until it has answered):
 * the picker counts everything beneath a folder (FU-3), and leaves out a
 * folder holding more shots than one draft reads, which would be drafted
 * from an arbitrary part of its footage. Offered without the picker, such a
 * folder opened a form that showed the bare path, with no count and no word
 * on how much is ready, until Draft was refused.
 */
export function browseDraftHref(
  libraryId: string,
  folder: string,
  counts: DraftCounts,
  offered: boolean | null = null,
): string | null {
  if (counts.inside.clips <= 0) return null;
  if (!draftAsksPicker(counts)) return draftFolderHref(libraryId, folder);
  return offered === true ? draftFolderHref(libraryId, folder) : null;
}

/** Why the Cuts page lets go of the clips Search handed over. */
export type LetGo = "unselect" | "drafted" | "retried";

/**
 * How the address lets go of handed-over clips. The × ("Choose a folder
 * instead") only changes the form's mind, so it is a step of its own that
 * Back undoes: replacing the address there skipped Back straight to Search,
 * and one press lost the whole selection. Once a draft has been asked for,
 * or Try again has filled the form with another draft's footage, the clips
 * are done with, and Back should not offer them for a second draft.
 */
export function letGoBy(why: LetGo): "push" | "replace" {
  return why === "unselect" ? "push" : "replace";
}

/** The most clips one draft may name one by one (the server's limit). */
export const CLIPS_MOST = 300;

const CLIP_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * Where Search's "Draft a cut…" leads: the Cuts page with the ticked clips
 * as the footage and the search as the start of a brief. Only video is
 * handed over (the Editor is given shots, and a photograph has none); how
 * many rows were ticked goes too when some were not video, so the form can
 * say what became of them. Null while no clip is ticked: the button is off.
 */
export function draftTickedHref(
  ticked: { asset_id: string; media_type: string }[],
  search: string,
): string | null {
  const clips = Array.from(
    new Set(ticked.filter((row) => row.media_type === "video").map((row) => row.asset_id)),
  ).slice(0, CLIPS_MOST);
  if (!clips.length) return null;
  const parts = [`clips=${clips.map(encodeURIComponent).join(",")}`];
  // Counted from the photographs, not from what was left out: clips beyond
  // the most one draft may name are not photographs.
  const photos = new Set(ticked.filter((row) => row.media_type !== "video").map((row) => row.asset_id)).size;
  if (photos) parts.push(`ticked=${clips.length + photos}`);
  const brief = search.trim().slice(0, BRIEF_MAX);
  if (brief) parts.push(`brief=${encodeURIComponent(brief)}`);
  return `/cuts?${parts.join("&")}`;
}

/** Clips that arrived in the Cuts page's address from Search. */
export interface HandedOver {
  assetIds: string[];
  /** "6 clips ticked in Search" — the form writes "The" before it. The
   *  server gives a draft made from them the same words. */
  label: string;
  /** What became of ticked rows that were not video; null when all were. */
  note: string | null;
  /** The search they were found with, as the start of a brief; "" for none. */
  brief: string;
}

/**
 * The clips an address hands the Cuts page (`?clips=<id>,<id>&ticked=9&brief=…`),
 * or null when it names none. Read off the address at every render, not
 * copied into the page: while the address names them the form holds them
 * (a reload included), and the page takes them out of the address the moment
 * the form lets go of them — a draft asked for, or "Choose a folder
 * instead" (which Back undoes: `letGoBy`). Anything that is not a clip's id is left out; an address is
 * something people paste and trim.
 */
export function handedOver(params: { get(name: string): string | null }): HandedOver | null {
  const assetIds = Array.from(
    new Set(
      (params.get("clips") ?? "")
        .split(",")
        .map((id) => id.trim().toLowerCase())
        .filter((id) => CLIP_ID.test(id)),
    ),
  ).slice(0, CLIPS_MOST);
  const count = assetIds.length;
  if (!count) return null;
  const ticked = Number(params.get("ticked") ?? "");
  const others = Number.isInteger(ticked) && ticked > count ? ticked - count : 0;
  return {
    assetIds,
    // In the server's own words for the same footage ("1 clip ticked in
    // Search"), so the form and the draft it becomes name it alike.
    label: `${plural(count, "clip")} ticked in Search`,
    note: others
      ? `${others} of the ${ticked} ticked items ${others === 1 ? "is a photograph" : "are photographs"}; ` +
        `the Editor works from the ${count === 1 ? "one clip" : `${count} clips`}.`
      : null,
    brief: (params.get("brief") ?? "").trim().slice(0, BRIEF_MAX),
  };
}

/** How many clips the form names one by one; the rest are counted. */
export const CLIPS_NAMED = 20;

/** One clip the form names, linked to its own page. */
export interface ClipRow {
  id: string;
  href: string;
  /** Its file's name once read; until then, or when it cannot be read, a
   *  sentence that says so (the link still opens the clip's page). */
  label: string;
  named: boolean;
}

/**
 * The clips a form holds by id, as a list of links to each clip's page: the
 * first `CLIPS_NAMED` of them, and "…and N more" for the rest. The footage
 * was only ever counted ("The 6 clips ticked in Search"), so a clip could be
 * neither seen nor opened from the form that was about to cut it.
 * `names` holds a file's name by id once read, null when it could not be.
 */
export function clipRows(
  assetIds: string[],
  names: ReadonlyMap<string, string | null>,
): { rows: ClipRow[]; more: string | null } {
  const rows = assetIds.slice(0, CLIPS_NAMED).map((id): ClipRow => {
    const name = names.get(id);
    const href = `/assets/${encodeURIComponent(id)}`;
    if (name) return { id, href, label: name, named: true };
    return { id, href, label: name === null ? "A clip whose name could not be read" : "Reading its name…", named: false };
  });
  const rest = assetIds.length - rows.length;
  return { rows, more: rest > 0 ? `…and ${rest} more` : null };
}

// ---- Search's ticks, kept for the way back ----------------------------------------------
//
// Search holds its ticks in the page. "Draft a cut…" (and every row's link)
// leaves the page, and Back ran the search again with nothing ticked: six
// clips found among forty had to be found and ticked again. So the ticks are
// kept, per search, for as long as the tab is open (not across a reload),
// and given back when the same search's results come back.

/** How many searches' ticks are kept: a few, for going back; not a history. */
export const TICKS_KEPT = 12;

/** Which search a set of ticks belongs to: its words and its filters. */
export function ticksKey(query: string, library: string, type: string): string {
  return JSON.stringify([query.trim(), library, type]);
}

/** Keep (or, with nothing ticked, forget) one search's ticks; the oldest
 *  search's go first once more than `TICKS_KEPT` are kept. */
export function keepTicks(kept: Map<string, string[]>, key: string, ticked: Iterable<string>): void {
  if (!key) return;
  const ids = Array.from(new Set(ticked));
  kept.delete(key);
  if (!ids.length) return;
  kept.set(key, ids);
  for (const oldest of kept.keys()) {
    if (kept.size <= TICKS_KEPT) break;
    kept.delete(oldest);
  }
}

/** The ticks to show when a search's results arrive: those kept for it that
 *  are still among the results (the catalogue may have changed since). A new
 *  search starts with nothing ticked. */
export function ticksBack(kept: Map<string, string[]>, key: string, shown: string[]): Set<string> {
  const here = new Set(shown);
  return new Set((kept.get(key) ?? []).filter((id) => here.has(id)));
}

// ---- one cut (app/cuts/[id]/page.tsx, CutShots, PremiereDownload) ----------------------
//
// A cut's page shows one version of a family at a time. What is here words a
// version, a piece and a download, and decides the few things the page would
// otherwise decide in the middle of its markup: which version an address
// names, which shots the viewer steps to, where to go once a version is
// deleted. A piece's note and a shot's description are a model's or a file's
// words; they are only ever joined into text here.

/** What a piece's shot was when the cut was made (`CutShot` in lib/api.ts). */
interface PieceShot {
  move: string | null;
  direction: string | null;
  light: string | null;
  sky: string | null;
  subjects: string[];
}

/** One piece of a version (`CutPiece`). */
interface Piece {
  position: number;
  /** Null once the clip has been deleted from the catalogue. */
  asset_id: string | null;
  clip: string;
  available: boolean;
  in_s: number;
  out_s: number;
  shot: PieceShot | null;
}

/** The draft a version was made from (`CutMadeFrom`), as read here. */
interface MadeFrom {
  brief: string;
  scope: Scope;
  target_s: number;
  min_shot_s: number;
  max_shot_s: number;
  shots_offered: number;
  shots_in_scope: number;
  seconds: number | null;
}

/** One version (`CutVersion`), as read here. */
interface Version {
  id: string;
  version: number;
  source: string;
  label: string;
  title: string;
  duration_s: number;
  piece_count: number;
  provenance: {
    model_name: string;
    revision_short: string | null;
    runtime: string | null;
    runtime_version: string | null;
    prompt_version: string | null;
  } | null;
  draft: MadeFrom | null;
}

// ---- which version -------------------------------------------------------------------

/**
 * The version a page address names. `?v=2` is the version numbered 2 — the
 * number stays with a version when another is deleted, so a pasted link
 * keeps meaning the same cut. Without it, or when that number is gone, the
 * version whose id is in the address; failing that, the newest.
 */
export function shownVersion<T extends { id: string; version: number }>(
  versions: T[],
  askedId: string,
  v: string | null,
): T | null {
  if (v !== null && /^[0-9]{1,6}$/.test(v.trim())) {
    const number = Number(v.trim());
    const named = versions.find((version) => version.version === number);
    if (named) return named;
  }
  return versions.find((version) => version.id === askedId) ?? versions[versions.length - 1] ?? null;
}

/** The address that shows a version of the family the page is on. */
export function versionHref(askedId: string, version: { version: number }): string {
  return `/cuts/${encodeURIComponent(askedId)}?v=${version.version}`;
}

/** A version's button: "2 · Claude". */
export function versionButton(version: { version: number; source: string; label: string }): string {
  return `${version.version} · ${version.label || sourceLabel(version.source)}`;
}

/**
 * The line under the title: "version 2 of 2 · Claude · 8 shots · 29.8 s".
 * "of 2" is only said while the numbers run 1 to 2; once a version has been
 * deleted the numbers have a gap, and "version 3 of 2" would be nonsense.
 */
export function versionLine(version: Version, versions: { version: number }[]): string {
  const count = versions.length;
  const unbroken = versions.every((each, index) => each.version === index + 1);
  const which = unbroken
    ? `version ${version.version} of ${count}`
    : `version ${version.version} · ${plural(count, "version")} kept`;
  return [
    which,
    version.label || sourceLabel(version.source),
    plural(version.piece_count, "shot"),
    secondsText(version.duration_s),
  ].join(" · ");
}

// ---- who drafted it ------------------------------------------------------------------

const RUNTIMES: Record<string, string> = { "mlx-vlm": "MLX-VLM", "llama-cpp": "llama.cpp" };

/** "MLX-VLM 0.7.12"; null for the demonstration, which is no program. */
function runtimeWords(runtime: string | null, version: string | null): string | null {
  if (!runtime || runtime.startsWith("demo")) return null;
  const name = RUNTIMES[runtime] ?? runtime;
  return version ? `${name} ${version}` : name;
}

/**
 * Which build drafted a local version, in how long and from how much:
 * "gemma-4-12B-it-OptiQ-4bit (build de9e183, on MLX-VLM 0.7.12, prompt
 * editor-1) drafted this in 48 s from 62 of the 480 shots in GELCO ›
 * 2026/Pete Dye." Null for a version no local model made. Whatever the
 * server did not record is left out rather than guessed.
 */
export function madeLine(version: Version): string | null {
  if (version.source !== "local") return null;
  const made = version.provenance;
  const draft = version.draft;
  const details: string[] = [];
  if (made?.revision_short) details.push(`build ${made.revision_short}`);
  const on = runtimeWords(made?.runtime ?? null, made?.runtime_version ?? null);
  if (on) details.push(`on ${on}`);
  if (made?.prompt_version) details.push(`prompt ${made.prompt_version}`);
  let text = made?.model_name || "A local model";
  if (details.length) text += ` (${details.join(", ")})`;
  text += " drafted this";
  if (draft && draft.seconds !== null && draft.seconds > 0) text += ` in ${secondsText(draft.seconds)}`;
  if (draft && draft.shots_offered > 0) {
    text +=
      draft.shots_in_scope > draft.shots_offered
        ? ` from ${draft.shots_offered} of the ${draft.shots_in_scope} shots`
        : ` from ${plural(draft.shots_offered, "shot")}`;
    if (draft.scope.label) {
      text += draft.scope.kind === "selection" ? ` in the ${draft.scope.label}` : ` in ${draft.scope.label}`;
    }
  }
  return `${text}.`;
}

// ---- a piece -------------------------------------------------------------------------

/** "3.0–7.5 s" */
export function spanText(start: number, end: number): string {
  return `${start.toFixed(1)}–${end.toFixed(1)} s`;
}

/**
 * What a shot is, from the snapshot stored with the piece: "orbit right ·
 * golden hour · green, bunker". The sky is said only when overcast, as the
 * shot lines the Editor and Claude read say it. Empty when nothing was kept.
 */
export function shotWords(shot: PieceShot | null): string {
  if (!shot) return "";
  const parts: string[] = [];
  if (shot.move) {
    parts.push(shot.direction && shot.move !== "static" ? `${shot.move} ${shot.direction}` : shot.move);
  }
  if (shot.light) parts.push(shot.light);
  if (shot.sky === "overcast") parts.push("overcast");
  if (shot.subjects.length) parts.push(shot.subjects.join(", "));
  return parts.join(" · ");
}

/** A clip's own page at a moment (whole seconds, as that page reads `t`). */
export function momentHref(assetId: string, at: number): string {
  const whole = at > 0 ? Math.floor(at) : 0;
  return whole > 0 ? `/assets/${assetId}?t=${whole}` : `/assets/${assetId}`;
}

/**
 * Whether a piece's footage can be opened. A clip that is merely off the
 * share still has its page and, usually, its playable copy, which lives
 * with FrameFound rather than on the share — so it opens, and the viewer
 * says so itself if the copy is away too. Only a deleted clip cannot.
 */
export function canLook(piece: { asset_id: string | null }): boolean {
  return piece.asset_id !== null;
}

/** Said on a piece whose clip is gone or away; null when it is there. */
export function goneSentence(piece: { asset_id: string | null; available: boolean }): string | null {
  if (piece.asset_id === null) return "This clip has been deleted from the catalogue.";
  if (!piece.available) return "This clip is not on the share right now.";
  return null;
}

/** Whose words a piece's note is: the Editor's, Claude's, or a marker's. */
export function noteLead(source: string): string {
  if (source === "local") return "Editor:";
  if (source === "mcp") return "Claude:";
  return "Note:";
}

/**
 * Where the viewer stands in a version, and the pieces ← and → go to: the
 * nearest each way that can be opened, in timeline order. Null when the
 * piece is not in the version (it was deleted while the viewer was open).
 */
export function pieceSteps<T extends { position: number; asset_id: string | null }>(
  pieces: T[],
  position: number,
): { at: number; count: number; prev: T | null; next: T | null } | null {
  const at = pieces.findIndex((piece) => piece.position === position);
  if (at < 0) return null;
  let prev: T | null = null;
  for (let i = at - 1; i >= 0; i -= 1) {
    const piece = pieces[i];
    if (piece && canLook(piece)) {
      prev = piece;
      break;
    }
  }
  let next: T | null = null;
  for (let i = at + 1; i < pieces.length; i += 1) {
    const piece = pieces[i];
    if (piece && canLook(piece)) {
      next = piece;
      break;
    }
  }
  return { at: at + 1, count: pieces.length, prev, next };
}

/** The viewer's heading: "Shot 3 of 9 — DJI_0639". */
export function pieceHeading(piece: { clip: string }, at: number, count: number): string {
  return `Shot ${at} of ${count} — ${piece.clip}`;
}

/** The viewer's line under it: "3.0–7.5 s · orbit right · golden hour". */
export function pieceDetail(piece: Piece): string {
  return [spanText(piece.in_s, piece.out_s), shotWords(piece.shot)].filter(Boolean).join(" · ");
}

/**
 * Under the shot list: the length, against what was asked for when the
 * version came from a draft. A draft that came out short says by how much
 * and why nothing was added (`lengthNote`).
 */
export function totalLine(total: number, asked: number | null | undefined): string {
  if (asked === null || asked === undefined || !(asked > 0)) return `${secondsText(total)} in all.`;
  const short = lengthNote(total, asked);
  if (short) return `${secondsText(total)} in all. ${short}`;
  return `${secondsText(total)} of the ${secondsText(asked)} asked for.`;
}

// ---- the Premiere file ---------------------------------------------------------------

/** The answer of `POST /cuts/{id}/send`, as read here. */
interface Sent {
  filename: string;
  note: string;
  missing: string[];
  offline: string[];
}

function named(files: string[]): string {
  const shown = files.slice(0, 5).join(", ");
  return files.length > 5 ? `${shown} and ${files.length - 5} more` : shown;
}

/**
 * What the page says after a download: the server's own sentence first (it
 * counts the cuts and says which files have no path for this computer),
 * then the clips left out or written offline, by name. `firstOfDraft`: this
 * was the first download of a local draft, which is when its shots start to
 * count as used.
 */
export function sendLines(sent: Sent, firstOfDraft: boolean): string[] {
  const lines = [`${sent.filename}: ${sent.note} In Premiere, File → Import.`];
  if (sent.missing.length) {
    const n = sent.missing.length;
    lines.push(
      `${plural(n, "clip")} ${n === 1 ? "has" : "have"} been deleted from the catalogue and ` +
        `${n === 1 ? "was" : "were"} left out: ${named(sent.missing)}.`,
    );
  }
  if (sent.offline.length) {
    const n = sent.offline.length;
    lines.push(
      `${plural(n, "clip")} ${n === 1 ? "is" : "are"} not on the share right now: ` +
        `${named(sent.offline)}. Premiere shows ${n === 1 ? "it" : "them"} offline until the share is back.`,
    );
  }
  if (firstOfDraft) {
    lines.push("From now on its shots are marked as used, so the next draft can avoid them.");
  }
  return lines;
}

/**
 * What the page says when a download finishes after another version was
 * put on screen: the file is the earlier version's, and its sentence must
 * not be read as this one's (two versions usually share a title, so the
 * file's name does not tell them apart). `what` names the version the file
 * is: "version 1".
 */
export function sentElsewhere(sent: { filename: string }, what: string): string[] {
  return [
    `${sent.filename} was downloaded. It is ${what}, which was on screen when Download was ` +
      "pressed — not the version shown now.",
  ];
}

/** Where this computer's path profiles have got to: asked for, answered, or not answered. */
export type ProfilesRead = "reading" | "read" | "failed";

/**
 * What the path-profile list says in place of a profile, or null when it
 * lists profiles. "No path profiles yet" is only true once the server has
 * said so: drawn before it answered, it was said of every cut and then
 * replaced by the computer's own names. When the list could not be read the
 * file is still written, with the server's own paths.
 */
export function profilesWord(read: ProfilesRead, count: number): string | null {
  if (read === "reading") return "Reading path profiles…";
  if (read === "failed") return "Path profiles could not be read";
  return count ? null : "No path profiles yet";
}

// ---- drafting again, and deleting ----------------------------------------------------

/**
 * The form "Draft again" opens: this version's brief, footage and length,
 * asking for the next version of the same cut. A version nothing local
 * drafted (Claude's, an imported timeline) has no brief to start from; the
 * form opens with only the title, and the result still lands here.
 * The form holds the same footage, whatever it was: a folder, the clips
 * named one by one (also given as `selection`, for a page that shows them
 * itself), or the chat's "folders matching".
 */
export function formFromVersion(version: Version): {
  form: DraftForm;
  selection: { assetIds: string[]; label: string } | null;
} {
  const draft = version.draft;
  const form: DraftForm = { ...blankForm(), title: version.title, revises: version.id };
  if (!draft) return { form, selection: null };
  form.brief = draft.brief;
  form.lengthText = String(draft.target_s);
  form.shortestText = String(draft.min_shot_s);
  form.longestText = String(draft.max_shot_s);
  Object.assign(form, footageOf(draft.scope));
  return { form, selection: selectionOf(draft.scope) };
}

/** The clips a draft's own scope names, for the form's `selection`; null
 *  for a folder ("Try again" on a draft made from clips ticked in Search). */
export function selectionOf(scope: Scope): { assetIds: string[]; label: string } | null {
  if (scope.kind !== "selection" || !scope.asset_ids.length) return null;
  return { assetIds: [...scope.asset_ids], label: scope.label || "clips ticked in Search" };
}

/** What Delete asks before anything is deleted. */
export function deleteQuestion(title: string, version: number, versions: number): string {
  const others = versions - 1;
  if (others <= 0) {
    return `Delete “${title}”? It is the only version, so the cut leaves the list. The footage is not touched.`;
  }
  return (
    `Delete version ${version} of “${title}”? ` +
    (others === 1 ? "The other version stays." : `The other ${others} versions stay.`)
  );
}

/**
 * The version to show once one is deleted: the newest that is left, or null
 * when it was the last (the page goes back to the list).
 */
export function afterDelete<T extends { id: string }>(versions: T[], deletedId: string): T | null {
  const left = versions.filter((version) => version.id !== deletedId);
  return left[left.length - 1] ?? null;
}

/**
 * Where to look when the version an address names turns out to be gone
 * (deleted in another tab, or by someone else): the other versions the page
 * last knew of, newest first. The cut itself is only gone when none of them
 * answers — the page used to say "This cut no longer exists." while version
 * 1 was still there and still listed.
 */
export function otherVersions(versions: { id: string }[], goneId: string): string[] {
  return versions
    .filter((version) => version.id !== goneId)
    .map((version) => version.id)
    .reverse();
}

// ---- two versions side by side (CutCompare) ------------------------------------------
//
// What changed between two versions of a cut, piece by piece. Nothing here
// reads a note or a file's words: two pieces are "the same shot" when they
// are the same clip and mostly the same stretch of it.

/** The few fields of a piece that say which stretch of which clip it is. */
interface ComparedPiece {
  position: number;
  asset_id: string | null;
  clip: string;
  in_s: number;
  out_s: number;
}

interface ComparedVersion {
  version: number;
  duration_s: number;
  pieces: ComparedPiece[];
}

/** What became of a piece in the other version. */
export interface PieceMark {
  /** `kept`: the same stretch, in and out within a tenth of a second.
   *  `trimmed`: the same shot with a different in or out. `only`: the other
   *  version has nothing like it. */
  change: "kept" | "trimmed" | "only";
  /** Kept or trimmed, but at a different place in the order. */
  moved: boolean;
  /** The piece it was matched with: its number in the other version's list
   *  and its in and out there. Null for `only`. */
  other: { number: number; in_s: number; out_s: number } | null;
}

export interface Compared {
  /** "Version 2 keeps 6 of version 1's 9 shots (2 trimmed), drops 3 and
   *  adds 2. 31.5 s → 29.8 s." */
  sentence: string;
  /** A mark for every piece of each version, by the piece's `position`. */
  older: Map<number, PieceMark>;
  newer: Map<number, PieceMark>;
  kept: number;
  trimmed: number;
  dropped: number;
  added: number;
  moved: number;
}

/** In and out this close are the same cut point: the server rounds to a
 *  tenth, and a hair over a tenth is still a tenth to the eye. */
const SAME_POINT_S = 0.1 + 1e-6;

function sameClip(a: ComparedPiece, b: ComparedPiece): boolean {
  // A clip deleted from the catalogue has no id left in either version; its
  // name is all there is to go by.
  if (a.asset_id === null || b.asset_id === null) {
    return a.asset_id === null && b.asset_id === null && a.clip === b.clip;
  }
  return a.asset_id === b.asset_id;
}

function samePoints(a: ComparedPiece, b: ComparedPiece): boolean {
  return Math.abs(a.in_s - b.in_s) <= SAME_POINT_S && Math.abs(a.out_s - b.out_s) <= SAME_POINT_S;
}

/** The same shot, cut differently: the two stretches share at least half of
 *  the shorter one. Two pieces from opposite ends of one long clip do not. */
function sameShot(a: ComparedPiece, b: ComparedPiece): boolean {
  const shorter = Math.min(a.out_s - a.in_s, b.out_s - b.in_s);
  if (!(shorter > 0)) return samePoints(a, b);
  const shared = Math.min(a.out_s, b.out_s) - Math.max(a.in_s, b.in_s);
  return shared >= shorter / 2;
}

/**
 * Which of the matched pairs kept their order. `olderAt[i]` is where the
 * i-th matched piece of the newer version stood in the older one; the
 * longest run of them that still climbs is the order that was kept, and the
 * rest were moved. (Counting places instead would call every shot after an
 * inserted one "moved".)
 */
function inOrder(olderAt: number[]): boolean[] {
  const n = olderAt.length;
  const length: number[] = new Array<number>(n).fill(1);
  const from: number[] = new Array<number>(n).fill(-1);
  let best = -1;
  for (let i = 0; i < n; i += 1) {
    for (let j = 0; j < i; j += 1) {
      if ((olderAt[j] ?? 0) < (olderAt[i] ?? 0) && (length[j] ?? 0) + 1 > (length[i] ?? 0)) {
        length[i] = (length[j] ?? 0) + 1;
        from[i] = j;
      }
    }
    if (best < 0 || (length[i] ?? 0) > (length[best] ?? 0)) best = i;
  }
  const kept: boolean[] = new Array<boolean>(n).fill(false);
  for (let at = best; at >= 0; at = from[at] ?? -1) kept[at] = true;
  return kept;
}

/**
 * Compare two versions of a cut, the older first. Each piece is matched at
 * most once, in order: first the pieces that are exactly the same stretch of
 * the same clip, then, among what is left, the same shot cut differently.
 * Whatever finds no partner is in one version only.
 */
export function compareVersions(older: ComparedVersion, newer: ComparedVersion): Compared {
  const a = older.pieces;
  const b = newer.pieces;
  // partner[i]: the index in `a` of the piece b[i] was matched with.
  const partner: number[] = new Array<number>(b.length).fill(-1);
  const taken: boolean[] = new Array<boolean>(a.length).fill(false);
  const exact: boolean[] = new Array<boolean>(b.length).fill(false);

  for (const same of [true, false]) {
    for (let i = 0; i < b.length; i += 1) {
      const piece = b[i];
      if (!piece || (partner[i] ?? -1) >= 0) continue;
      const found = a.findIndex(
        (other, j) =>
          !taken[j] && sameClip(other, piece) && (same ? samePoints(other, piece) : sameShot(other, piece)),
      );
      if (found < 0) continue;
      partner[i] = found;
      taken[found] = true;
      exact[i] = same;
    }
  }

  const matched = partner.map((at, i) => ({ at, i })).filter((pair) => pair.at >= 0);
  const stayed = inOrder(matched.map((pair) => pair.at));
  const movedAt = new Set(matched.filter((_, index) => !stayed[index]).map((pair) => pair.i));

  const olderMarks = new Map<number, PieceMark>();
  const newerMarks = new Map<number, PieceMark>();
  let kept = 0;
  let trimmed = 0;
  for (let i = 0; i < b.length; i += 1) {
    const piece = b[i];
    if (!piece) continue;
    const at = partner[i] ?? -1;
    const other = at >= 0 ? a[at] : undefined;
    if (!other) {
      newerMarks.set(piece.position, { change: "only", moved: false, other: null });
      continue;
    }
    const change = exact[i] ? "kept" : "trimmed";
    if (change === "kept") kept += 1;
    else trimmed += 1;
    const moved = movedAt.has(i);
    newerMarks.set(piece.position, {
      change,
      moved,
      other: { number: at + 1, in_s: other.in_s, out_s: other.out_s },
    });
    olderMarks.set(other.position, {
      change,
      moved,
      other: { number: i + 1, in_s: piece.in_s, out_s: piece.out_s },
    });
  }
  for (const piece of a) {
    if (!olderMarks.has(piece.position)) {
      olderMarks.set(piece.position, { change: "only", moved: false, other: null });
    }
  }

  const both = kept + trimmed;
  const dropped = a.length - both;
  const added = b.length - both;
  const moved = movedAt.size;
  return {
    sentence: comparedSentence(older, newer, { both, trimmed, dropped, added, moved }),
    older: olderMarks,
    newer: newerMarks,
    kept,
    trimmed,
    dropped,
    added,
    moved,
  };
}

function comparedSentence(
  older: ComparedVersion,
  newer: ComparedVersion,
  n: { both: number; trimmed: number; dropped: number; added: number; moved: number },
): string {
  const was = older.pieces.length;
  const lengths = `${secondsText(older.duration_s)} → ${secondsText(newer.duration_s)}.`;
  const name = `Version ${newer.version}`;
  const of = `version ${older.version}'s`;
  if (was === 0 && newer.pieces.length === 0) return "Neither version has any shots.";
  if (was === 0) return `${name} has ${plural(n.added, "shot")}; version ${older.version} has none. ${lengths}`;
  if (n.both === was && !n.added && !n.trimmed && !n.moved) {
    return `${name} has the same ${plural(was, "shot")} as version ${older.version}, in the same order. ${lengths}`;
  }
  if (n.both === 0) {
    const adds = n.added ? ` and adds ${n.added}` : "";
    return `${name} keeps none of ${of} ${plural(was, "shot")}: it drops ${was === 1 ? "it" : `all ${was}`}${adds}. ${lengths}`;
  }
  const how = [n.trimmed ? `${n.trimmed} trimmed` : "", n.moved ? `${n.moved} moved` : ""].filter(Boolean);
  let text =
    n.both === was
      ? `${name} keeps ${was === 1 ? `${of} one shot` : `all ${was} of ${of} shots`}`
      : `${name} keeps ${n.both} of ${of} ${plural(was, "shot")}`;
  if (how.length) text += ` (${how.join(", ")})`;
  if (n.dropped && n.added) text += `, drops ${n.dropped} and adds ${n.added}`;
  else if (n.dropped) text += ` and drops ${n.dropped}`;
  else if (n.added) text += ` and adds ${n.added}`;
  return `${text}. ${lengths}`;
}

export interface MarkPill {
  text: string;
  tone: "ok" | "warn" | null;
  /** What it means, for whoever hovers. */
  title: string;
}

/** The pills a piece wears in Compare, in the order they are drawn. */
export function markPills(mark: PieceMark, otherVersion: number): MarkPill[] {
  if (mark.change === "only") {
    return [{ text: "only here", tone: null, title: `Version ${otherVersion} has no shot like this one.` }];
  }
  const where = mark.other ? `shot ${mark.other.number} of version ${otherVersion}` : `version ${otherVersion}`;
  const pills: MarkPill[] = [
    mark.change === "kept"
      ? { text: "kept", tone: "ok", title: `The same stretch of the same clip is ${where}.` }
      : { text: "trimmed", tone: "warn", title: `The same shot, with a different in or out, is ${where}.` },
  ];
  if (mark.moved) {
    pills.push({ text: "moved", tone: null, title: `It comes at a different place in the order: it is ${where}.` });
  }
  return pills;
}

/**
 * Under a trimmed piece, the in and out it has in the other version: "was
 * 3.0–7.5 s" on the newer version's row, "3.0–6.0 s in version 2" on the
 * older one's. Null for a piece that was not trimmed.
 */
export function trimNote(mark: PieceMark, side: "older" | "newer", otherVersion: number): string | null {
  if (mark.change !== "trimmed" || !mark.other) return null;
  const span = spanText(mark.other.in_s, mark.other.out_s);
  return side === "newer" ? `was ${span}` : `${span} in version ${otherVersion}`;
}

// ---- which two versions --------------------------------------------------------------

/**
 * The version the one on screen is being compared with: `?with=1` names it
 * by number, as `?v=` names the one shown. Null when the address asks for no
 * comparison, or for one that cannot be made (that number is gone, or it is
 * the version shown itself).
 */
export function comparedWith<T extends { id: string; version: number }>(
  versions: T[],
  shown: T,
  asked: string | null,
): T | null {
  if (asked === null || !/^[0-9]{1,6}$/.test(asked.trim())) return null;
  const number = Number(asked.trim());
  if (number === shown.version) return null;
  return versions.find((version) => version.version === number) ?? null;
}

/**
 * What Compare starts with: the version this one revises, when it is still
 * kept; otherwise the one before it in the list, or the one after for the
 * first. Null when there is nothing to compare with.
 */
export function compareDefault<T extends { id: string; version: number; parent_id?: string | null }>(
  versions: T[],
  shown: T,
): T | null {
  const parent = shown.parent_id ? versions.find((version) => version.id === shown.parent_id) : undefined;
  if (parent && parent.id !== shown.id) return parent;
  const at = versions.findIndex((version) => version.id === shown.id);
  if (at < 0) return null;
  return versions[at - 1] ?? versions[at + 1] ?? null;
}

/** The address that shows one version beside another. */
export function compareHref(askedId: string, shown: { version: number }, other: { version: number }): string {
  return `${versionHref(askedId, shown)}&with=${other.version}`;
}

/**
 * A version chosen while two are being compared: the comparison stays, with
 * the chosen version as the one shown. Choosing the version it is compared
 * with swaps the two, so there is always something on each side.
 */
export function compareAfterShow<T extends { version: number }>(
  shown: T,
  other: T,
  pressed: T,
): { shown: T; other: T } {
  return pressed.version === other.version ? { shown: other, other: shown } : { shown: pressed, other };
}

// ---- Ask Claude to review ------------------------------------------------------------
//
// The cut's page fetches the request the server wrote and hands it to the
// chat panel as an attachment on a new conversation. Nothing leaves until
// Send is pressed there; what is here words the hand-over and the wait for
// the version Claude may build.

/** While a review opened from the page may be under way: often enough that
 *  the version Claude builds shows up without a reload. */
export const REVIEW_MS = 10_000;
/** A review that has built nothing after this long is not waited on. */
export const REVIEW_WAIT_MS = 30 * 60_000;

/**
 * How soon to read the cut again. `base` is what the drafts ask for
 * (`readAgainIn`); `openedAt` is when Ask Claude to review was last pressed
 * for this cut in this tab, or null. For half an hour after that the page
 * reads at least every ten seconds.
 */
export function reviewEvery(base: number | null, openedAt: number | null, now: number): number | null {
  if (base === null || openedAt === null) return base;
  const waited = now - openedAt;
  if (waited < 0 || waited >= REVIEW_WAIT_MS) return base;
  return Math.min(base, REVIEW_MS);
}

/** Versions the cut has now and did not have at the last reading. */
export function arrivedVersions<T extends { id: string }>(
  before: { versions: { id: string }[] } | null,
  after: { versions: T[] },
): T[] {
  if (!before) return [];
  const had = new Set(before.versions.map((version) => version.id));
  return after.versions.filter((version) => !had.has(version.id));
}

/**
 * A review opened from a cut's page: when the button was pressed, and the
 * versions the cut had the last time the page read it. Kept for the tab's
 * life, so it outlives the page — the reader may go elsewhere while Claude
 * works.
 */
export interface ReviewWatch {
  at: number;
  versions: string[];
}

/**
 * Versions that appeared since a review was opened and have not been said
 * yet. A page that has only just been opened has no earlier reading of its
 * own to compare with; without this, the version Claude built while the
 * reader was on another page was never announced. Empty when no review was
 * opened for this cut.
 */
export function arrivedSinceReview<T extends { id: string }>(
  watch: ReviewWatch | null | undefined,
  after: { versions: T[] },
): T[] {
  if (!watch) return [];
  return arrivedVersions({ versions: watch.versions.map((id) => ({ id })) }, after);
}

/** What the page says when a version appears while it is open. */
export function arrivedSentence(version: { version: number; source: string }): string {
  if (version.source === "mcp") return `Claude built version ${version.version}.`;
  if (version.source === "local") return `Version ${version.version} is ready.`;
  if (version.source === "import") return `Version ${version.version} was imported.`;
  return `Version ${version.version} was added.`;
}

/**
 * The message that goes when Send is pressed with something attached: what
 * was typed, a blank line, then the attachment — or the attachment alone
 * when nothing was typed.
 */
export function attachedMessage(typed: string, attachment: string): string {
  const words = typed.trim();
  const rest = attachment.trim();
  if (!words) return rest;
  if (!rest) return words;
  return `${words}\n\n${rest}`;
}

/**
 * The message box after a page writes a request into it. Words the reader
 * had typed and not sent are theirs (the store keeps them while the panel
 * is closed and across a reload): they stay, with the request after a blank
 * line, where they used to be replaced with no way back. A request that is
 * already there (the button pressed twice) is not written a second time.
 */
export function withRequest(typed: string, request: string): string {
  const mine = typed.trim();
  const asked = request.trim();
  if (!mine) return request;
  if (!asked || mine.includes(asked)) return typed;
  return `${typed.trimEnd()}\n\n${request}`;
}

/**
 * What a key pressed in the message box does. Enter sends; Shift+Enter is a
 * new line; an Enter that ends a word being composed (Japanese, Chinese,
 * Korean input) is the input method's. An Enter that is only the keyboard
 * repeating a held key never sends: a page's button pressed with Enter and
 * held moved the keyboard into the box, and the repeat sent the attached
 * request, paid for, before it had been seen. It is swallowed, so it adds
 * no line either.
 */
export function enterInBox(event: {
  key: string;
  shiftKey: boolean;
  repeat: boolean;
  composing: boolean;
}): "send" | "swallow" | "leave" {
  if (event.key !== "Enter" || event.shiftKey || event.composing) return "leave";
  return event.repeat ? "swallow" : "send";
}

/** Something a page attached to the next chat message. */
export interface ChatAttachment {
  /** Shown above the box: "Local draft, version 1 — 9 shots, …". */
  label: string;
  /** What is sent after the typed words. */
  text: string;
}

/** The part of the chat store (lib/chatSession.ts) an attachment needs. */
interface AskedStore<Options> {
  get(): { user: string | null; draft: string };
  subscribe(listener: () => void): () => void;
  newConversation(): void;
  setDraft(text: string): void;
  setOpen(open: boolean): void;
  send(text: string, options?: Options): Promise<boolean>;
}

export interface ChatAsk<Options> {
  /** For `useSyncExternalStore`: the same object until it changes. */
  get(): ChatAttachment | null;
  /** How many times a page has asked. It goes up with every `ask`, so the
   *  panel can tell one arriving (and move the keyboard to the box) from
   *  one it has already answered. */
  asked(): number;
  subscribe(listener: () => void): () => void;
  /**
   * Open the panel on a new conversation with `attachment` above the box.
   * `text`, when it is not empty, is written in the box — after whatever
   * the reader had typed and not sent, which always stays (`withRequest`).
   * Sends nothing. False, and nothing done, when nobody is signed in.
   */
  ask(request: { text?: string; attachment?: ChatAttachment }): boolean;
  /** Take the attachment away without sending it. */
  drop(): void;
  /**
   * Send what was typed, with the attachment after it when there is one.
   * Resolves as the store's own `send` does. A message the server did not
   * take comes back as it was: the typed words in the box, the attachment
   * above it.
   */
  send(typed: string, options?: Options): Promise<boolean>;
}

/**
 * What a page attached to the next chat message, held beside the chat store
 * until the reader sends or removes it. Nothing here makes a request: the
 * only thing that sends is `send`, which the panel calls from its Send
 * button. (The app's one is made in lib/chatStore.ts.)
 */
export function createChatAsk<Options>(store: AskedStore<Options>): ChatAsk<Options> {
  let held: ChatAttachment | null = null;
  let asks = 0;
  let whose = store.get().user;
  const listeners = new Set<() => void>();

  function tell(): void {
    for (const listener of [...listeners]) listener();
  }

  function hold(next: ChatAttachment | null): void {
    if (next === held) return;
    held = next;
    tell();
  }

  // One person's attachment is not the next person's.
  store.subscribe(() => {
    const user = store.get().user;
    if (user === whose) return;
    whose = user;
    hold(null);
  });

  return {
    get: () => held,
    asked: () => asks,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    ask({ text = "", attachment }) {
      if (store.get().user === null) return false;
      store.newConversation();
      if (text.trim() !== "") store.setDraft(withRequest(store.get().draft, text));
      // An attachment with nothing in it is no attachment.
      const has = attachment !== undefined && attachment.text.trim() !== "";
      held = has ? { label: attachment.label, text: attachment.text } : null;
      asks += 1;
      tell();
      store.setOpen(true);
      return true;
    },
    drop: () => hold(null),
    async send(typed, options) {
      const mine = held;
      if (mine === null) return store.send(typed, options);
      const words = typed.trim();
      const whole = attachedMessage(words, mine.text);
      // The store puts a message it could not send back in the box. This
      // one is the typed words and the attachment joined, and 14,000
      // characters of shot lines do not belong in the box: the words go
      // back there and the attachment back above it, the moment it happens.
      let handedBack = false;
      const stop = store.subscribe(() => {
        if (store.get().draft !== whole) return;
        handedBack = true;
        if (held === null) hold(mine);
        store.setDraft(words);
      });
      hold(null);
      store.setDraft("");
      let took = false;
      try {
        took = await store.send(whole, options);
      } finally {
        stop();
      }
      if (took) {
        // Taken after all (the answer was lost, not the message): what was
        // handed back meanwhile must not be sent a second time.
        if (handedBack && held === mine) hold(null);
        if (handedBack && store.get().draft === words) store.setDraft("");
      } else if (!handedBack) {
        // Not sent and not put back by the store (it was in no state to
        // send, or the reader had typed something else meanwhile).
        if (held === null) hold(mine);
        if (store.get().draft === "") store.setDraft(words);
      }
      return took;
    },
  };
}

/**
 * The line under an attachment: where it goes when Send is pressed and what
 * pays for it. `used` and `budget` are sums already written as money; null
 * when the month's figures are not known yet.
 */
export function sendingLine(pictures: boolean, used: string | null, budget: string | null): string {
  const what = pictures
    ? "Pressing Send gives this to Claude (Anthropic), with any stills Claude asks to look at."
    : "Pressing Send gives this to Claude (Anthropic). Pictures are switched off, so no stills go with it.";
  const paid =
    used && budget
      ? `It is paid for from the Claude budget: ${used} of ${budget} used this month.`
      : "It is paid for from the Claude budget.";
  return `${what} ${paid}`;
}

/** A message of the reader's own longer than this is shown folded. */
export const FOLD_OVER_CHARS = 1200;
const FOLD_HEAD_CHARS = 480;

/**
 * A long message of the reader's own, split for the conversation: its start
 * as it was written, and the rest to be opened. A review request is some
 * 14,000 characters of shot lines; shown whole it pushes Claude's answer a
 * dozen screens down. Nothing is dropped: `head` and `rest` are the message.
 * Null for a message short enough to show as it is.
 */
export function foldMessage(text: string): { head: string; rest: string } | null {
  if (text.length <= FOLD_OVER_CHARS) return null;
  const start = text.slice(0, FOLD_HEAD_CHARS);
  // At the end of a line when there is one, else of a word, else where it falls.
  let cut = start.lastIndexOf("\n");
  if (cut < FOLD_HEAD_CHARS / 4) cut = start.lastIndexOf(" ");
  if (cut < FOLD_HEAD_CHARS / 4) cut = FOLD_HEAD_CHARS;
  const head = text.slice(0, cut).trimEnd();
  const rest = text.slice(cut).replace(/^\s+/, "");
  return rest ? { head, rest } : null;
}
