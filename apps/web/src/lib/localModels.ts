// The words and sums of the AI models page (app/models/page.tsx and the
// components/Models*.tsx it lays out), kept apart from the page so they can
// be tested.
//
// Nothing is imported at run time (the one import is types only), so
// `node --test` can load this file as it is (tests/localModels.test.mjs).
//
// The page is for the owner of the Mac, not for whoever set it up: it says
// "software that runs models", not runtime; "build", not revision; and it
// never invents a figure the server did not give.

import type {
  LocalAiRoleName,
  LocalModel,
  LocalModelBuiltFrom,
  LocalModelCheck,
  LocalModelFits,
  LocalModelJob,
  LocalModelLibrary,
  LocalModelPreflight,
  LocalModelRole,
  LocalModelRuntime,
  LocalModelServer,
  LocalModelsNow,
  LocalModelsOverview,
  LocalModelsPublishers,
  LocalModelsPublishersPatch,
  LocalModelsPublishersSaved,
  LocalModelsSettings,
  LocalModelsSettingsPatch,
  LocalModelTest,
  LocalModelVariant,
} from "./api";

/** The page's sections, in the jump bar's order. "start" is Getting started
 *  and Right now, at the top, which the jump bar does not need. */
export type ModelsSectionId =
  | "start"
  | "roles"
  | "models"
  | "add"
  | "software"
  | "sharing"
  | "publishers"
  | "activity";

export const JUMPS: ReadonlyArray<{ id: ModelsSectionId; label: string }> = [
  { id: "roles", label: "Who does what" },
  { id: "models", label: "Models" },
  { id: "add", label: "Add" },
  { id: "software", label: "Software" },
  { id: "sharing", label: "Sharing" },
  { id: "publishers", label: "Publishers" },
  // The last section of a long page: without a jump it is reached only by
  // scrolling past everything else.
  { id: "activity", label: "Activity" },
];

/**
 * Every change on the page goes through one of these, owned by the page:
 * `work(false)` is sent; a 428 is asked with `confirm()` and, on a yes, sent
 * again as `work(true)`; the page reads itself again either way; a refusal's
 * sentence is shown at the top of section `where`. `done` (or what it makes
 * of the answer) is said there when it worked. Resolves true when it worked.
 * `declined` is told when the owner answered the question "no" (nothing was
 * sent the second time), for a caller that offers another way.
 */
export type ModelsAct = <T>(
  where: ModelsSectionId,
  work: (acknowledged: boolean) => Promise<T>,
  done?: string | ((result: T) => string | null),
  declined?: () => void,
) => Promise<boolean>;

/** What `sendAsking` gives back when the question was answered "no". */
export const DECLINED: unique symbol = Symbol("declined");

/**
 * `work(false)`; when the server answers 428 its message is a question, put
 * to the owner with `ask`. A yes sends the same work again as `work(true)`
 * (the route's own yes: `accept_warnings`, `skip_compare`); a no sends
 * nothing more and resolves DECLINED. Any other refusal, and a second 428,
 * is thrown as it came.
 */
export async function sendAsking<T>(
  work: (acknowledged: boolean) => Promise<T>,
  ask: (question: string) => boolean,
): Promise<T | typeof DECLINED> {
  try {
    return await work(false);
  } catch (err) {
    if (!(err instanceof Error && (err as Error & { status?: unknown }).status === 428)) throw err;
    if (!ask(err.message)) return DECLINED;
    return work(true);
  }
}

/** How often the page reads itself while something moves, and otherwise. */
export const FAST_MS = 2_500;
export const SLOW_MS = 15_000;
/** A job waiting for a time closer than this is about to move. */
export const NEAR_MS = 2 * 60_000;

const ACTIVE = new Set(["queued", "waiting", "running"]);

export function isActiveJob(job: LocalModelJob | null | undefined): job is LocalModelJob {
  return !!job && ACTIVE.has(job.state);
}

const MB = 1024 ** 2;
const GB = 1024 ** 3;
const TB = 1024 ** 4;

/**
 * A size: GB with one decimal, whole MB below 1 GB, TB above 1024 GB.
 * 1024-based like the Storage page, so a model and the free space it is
 * compared with read in the same units.
 */
export function gbText(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return "—";
  const mb = Math.round(bytes / MB);
  if (mb < 1024) return `${mb} MB`;
  const gb = Math.round((bytes / GB) * 10) / 10;
  if (gb < 1024) return `${gb.toFixed(1)} GB`;
  return `${(bytes / TB).toFixed(1)} TB`;
}

/** "2.0 of 4.8 GB": both figures in the unit of the whole. */
export function ofText(done: number, total: number): string {
  const whole = gbText(total);
  const unit = whole.slice(whole.lastIndexOf(" ") + 1);
  const scale = unit === "TB" ? TB : unit === "GB" ? GB : MB;
  const part = done / scale;
  return `${unit === "MB" ? Math.round(part) : part.toFixed(1)} of ${whole}`;
}

/** A figure of GB the server gave (a budget, a memory estimate): "8 GB",
 *  "6.1 GB" — no trailing ".0". */
export function gbFigure(gb: number | null | undefined): string {
  if (gb == null || !Number.isFinite(gb)) return "—";
  const rounded = Math.round(gb * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)} GB`;
}

/** How far a job that moves bytes has got, 0–100; null when it reports no
 *  total (an install, a test), so no bar is invented. */
export function jobPercent(job: LocalModelJob): number | null {
  if (!(job.total > 0)) return null;
  const share = Math.floor((Math.max(0, job.done) / job.total) * 100);
  return Math.min(100, Math.max(0, share));
}

/**
 * The line under a job's bar or beside its spinner. `ahead` is the title of
 * the job running now, for one still waiting its turn (one runs at a time).
 */
export function jobSentence(job: LocalModelJob, ahead?: string | null): string {
  if (job.cancelling && ACTIVE.has(job.state)) return "Stopping…";
  if (job.state === "queued") return ahead ? `Waiting its turn — ${ahead}.` : "Waiting its turn.";
  if (job.state === "running") {
    const share = jobPercent(job);
    if (share !== null) {
      const figures = `${ofText(job.done, job.total)} · ${share}%`;
      return job.phase === "copying" ? `Copying to this Mac — ${figures}` : figures;
    }
    return job.message || "Working…";
  }
  if (job.state === "waiting") return job.message || "Waiting.";
  if (job.message) return job.message;
  return job.state === "done" ? "Done." : job.state === "cancelled" ? "Cancelled." : "It failed.";
}

/** The title of the job running now, for a job queued behind it. */
export function aheadOf(job: LocalModelJob, jobs: LocalModelJob[]): string | null {
  if (job.state !== "queued") return null;
  return jobs.find((other) => other.state === "running" && other.id !== job.id)?.title ?? null;
}

/**
 * How long until the page reads itself again; null for never (where models
 * cannot run, nothing on the page can change). Quick while anything moves —
 * a job queued or running, a wait that ends within a couple of minutes, a
 * model loading or answering — and slow otherwise. Nothing about a run is
 * kept in the tab, so a slow read only delays a bar, it never loses one.
 */
export function pollEvery(overview: LocalModelsOverview, nowMs: number = Date.now()): number | null {
  if (overview.mode === "unavailable") return null;
  if (overview.server.state === "loading" || overview.server.state === "busy") return FAST_MS;
  for (const job of overview.jobs) {
    if (job.state === "queued" || job.state === "running") return FAST_MS;
    if (job.state === "waiting") {
      if (job.cancelling) return FAST_MS;
      const at = job.not_before ? Date.parse(job.not_before) : NaN;
      if (Number.isFinite(at) && at - nowMs < NEAR_MS) return FAST_MS;
    }
  }
  return SLOW_MS;
}

/**
 * How long after a read, worked or not, until the next one: as `pollEvery`
 * says for the last overview the page has; slowly when it has none yet and
 * the read failed (the page says "It tries again shortly"); null for never.
 */
export function readAgainIn(latest: LocalModelsOverview | null, failed: boolean): number | null {
  if (latest) return pollEvery(latest);
  return failed ? SLOW_MS : null;
}

export interface Rereader<T> {
  /** Read now; the timer is armed again from this read's end. */
  now: () => Promise<void>;
  /** No more reads, and nothing from one in flight is drawn. */
  stop: () => void;
}

/**
 * The page's reading of itself, kept out of React so a test can drive it.
 *
 * Every read arms the next one when it ends, whether it worked or not. The
 * timer used to be re-armed by a React effect on the page's state; a second
 * failed read in a row set the same sentence, React saw no change, and the
 * page never read again — a download bar froze for good across a restart of
 * FrameFound, under a sentence promising another try.
 *
 * Only the newest read may draw: an action's read can overtake the timer's,
 * and the older answer must not replace the newer. A turn that comes while
 * the tab is hidden is let pass without arming another; showing the tab
 * calls `now()` again.
 */
export function rereads<T>(options: {
  read: () => Promise<T>;
  answered: (value: T) => void;
  failed: (err: unknown) => void;
  every: (latest: T | null, failed: boolean) => number | null;
  hidden: () => boolean;
  later: (run: () => void, ms: number) => unknown;
  cancel: (handle: unknown) => void;
}): Rereader<T> {
  let ticket = 0;
  let timer: unknown = null;
  let latest: T | null = null;
  let stopped = false;

  function disarm() {
    if (timer !== null) options.cancel(timer);
    timer = null;
  }

  async function now(): Promise<void> {
    if (stopped) return;
    const mine = ++ticket;
    disarm();
    let failed = false;
    try {
      const value = await options.read();
      if (stopped || mine !== ticket) return;
      latest = value;
      options.answered(value);
    } catch (err) {
      if (stopped || mine !== ticket) return;
      failed = true;
      options.failed(err);
    }
    const ms = options.every(latest, failed);
    if (ms === null) return;
    timer = options.later(() => {
      timer = null;
      if (!options.hidden()) void now();
    }, ms);
  }

  return {
    now,
    stop() {
      stopped = true;
      disarm();
    },
  };
}

/**
 * Keyboard focus for when the control that was pressed has gone (its row
 * removed, its card closed): `target` gets it, so the next Tab carries on
 * from there instead of from the top of a long page. Nothing happens if
 * focus is already somewhere real — the owner may have moved on.
 */
export function focusIfLost(target: HTMLElement | null | undefined): void {
  if (!target || typeof document === "undefined") return;
  const active = document.activeElement;
  if (active && active !== document.body && active.isConnected) return;
  if (!target.hasAttribute("tabindex") && target.tabIndex < 0) target.setAttribute("tabindex", "-1");
  target.focus({ preventScroll: true });
}

/**
 * The first of `candidates` that is there and can take focus now. Focus sent
 * to a disabled button goes nowhere, so a card that wants the keyboard back
 * on, say, Assign names what to use when Assign cannot be pressed (nothing
 * chosen, or the choice was refused and is now disabled).
 */
export function firstUsable<T extends object>(...candidates: (T | null | undefined)[]): T | null {
  for (const candidate of candidates) {
    if (candidate && !("disabled" in candidate && candidate.disabled)) return candidate;
  }
  return null;
}

/** The model's own name from a job's subject `publisher/name@build[/variant]`. */
export function nameOf(subject: string): string {
  const before = subject.split("@")[0] ?? subject;
  return before.slice(before.lastIndexOf("/") + 1) || subject;
}

/** Jobs that were under way in `before` and have ended in `after`. */
export function endedJobs(before: LocalModelJob[], after: LocalModelJob[]): LocalModelJob[] {
  const wasActive = new Set(before.filter((job) => ACTIVE.has(job.state)).map((job) => job.id));
  return after.filter((job) => wasActive.has(job.id) && !ACTIVE.has(job.state));
}

/** What the page says when a job ends while it is open. */
export function endedSentence(job: LocalModelJob): string {
  if (job.state === "cancelled") return `${job.title}: cancelled.`;
  if (job.state === "failed") {
    return job.message ? `${job.title} did not finish. ${job.message}` : `${job.title} did not finish.`;
  }
  const name = nameOf(job.subject);
  switch (job.kind) {
    case "download":
      return `${name} is installed.`;
    case "place":
      return `${name} is copied to this Mac.`;
    case "runtime_install":
      return `${job.subject} is installed.`;
    case "test":
      return `The test of ${name} has finished.`;
    default:
      return `${job.title}: done.`;
  }
}

/** "14:05" from the Mac's wall clock "2026-11-12T14:05:00". */
export function clockText(local: string | null | undefined): string {
  if (!local) return "";
  const found = /T(\d{2}:\d{2})/.exec(local);
  return found?.[1] ?? local;
}

/**
 * The line under the page's heading: the Mac's time, which budget is in
 * force, and when the other one starts.
 */
export function clockLine(now: LocalModelsNow, settings: LocalModelsSettings): string {
  const at = `It is ${clockText(now.local_time)} on this Mac`;
  let line: string;
  if (!now.changes_at) {
    line = `${at} — models up to ${gbFigure(now.budget_gb)} at any hour.`;
  } else if (now.overnight) {
    line =
      `${at} — overnight, models up to ${gbFigure(now.budget_gb)}. ` +
      `Daytime (up to ${gbFigure(settings.day_budget_gb)}) starts at ${clockText(now.changes_at)}.`;
  } else {
    line =
      `${at} — daytime, models up to ${gbFigure(now.budget_gb)}. ` +
      `Overnight (up to ${gbFigure(settings.night_budget_gb)}) starts at ${clockText(now.changes_at)}.`;
  }
  return settings.paused ? `${line} Local AI is paused.` : line;
}

/** The Memory tile: "8 GB until 21:30, then 20 GB" (or the reverse overnight). */
export function memoryLine(now: LocalModelsNow, settings: LocalModelsSettings): string {
  const current = gbFigure(now.budget_gb);
  if (!now.changes_at) return `${current} at any hour`;
  const next = gbFigure(now.overnight ? settings.day_budget_gb : settings.night_budget_gb);
  return `${current} until ${clockText(now.changes_at)}, then ${next}`;
}

/** "less than a minute", "1 minute", "12 minutes", "about 2 hours". */
export function waitWords(seconds: number): string {
  if (seconds < 60) return "less than a minute";
  const minutes = Math.round(seconds / 60);
  if (minutes < 120) return minutes === 1 ? "1 minute" : `${minutes} minutes`;
  return `about ${Math.round(minutes / 60)} hours`;
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** When something happened, on this browser's clock: "Tue 14:02" within the
 *  last week, "3 Nov 14:02" before that. */
export function whenText(iso: string | null | undefined, nowMs: number = Date.now()): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const time = `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
  const recent = nowMs - at.getTime() < 6 * 24 * 3600_000;
  return recent
    ? `${DAYS[at.getDay()]} ${time}`
    : `${at.getDate()} ${MONTHS[at.getMonth()]} ${time}`;
}

/**
 * The Right now tile: what the model server is doing, in words. `unload` is
 * true when Unload now has something to free.
 */
export function serverLine(
  server: LocalModelServer,
  settings: LocalModelsSettings,
  nowMs: number = Date.now(),
): { text: string; detail: string | null; unload: boolean } {
  const name = server.model_name || "A model";
  const unload = server.state === "ready";
  if (settings.paused) {
    const since = whenText(settings.paused_at, nowMs);
    const who = settings.paused_by ? ` by ${settings.paused_by}` : "";
    return { text: since ? `Paused since ${since}${who}` : `Paused${who}`, detail: null, unload };
  }
  switch (server.state) {
    case "down":
      return {
        text: "The model server is not running — it restarts by itself within a minute.",
        detail: server.problem ?? null,
        unload: false,
      };
    case "idle":
      return { text: "Idle — nothing loaded", detail: null, unload: false };
    case "loading":
      return { text: `Starting ${name}…`, detail: null, unload: false };
    case "busy":
      return { text: `Working — ${name} is answering a question`, detail: null, unload: false };
    case "ready": {
      const using = server.memory_gb != null ? `, using ${gbFigure(server.memory_gb)}` : "";
      const until =
        server.unloads_in_s != null
          ? ` — unloads in ${waitWords(server.unloads_in_s)} if nothing asks`
          : "";
      return { text: `${name} is loaded${using}${until}`, detail: null, unload };
    }
  }
}

/** The memory pill of a model: whether it fits by day, only overnight, or not
 *  at all, with the sentence that says why. */
export function fitsText(
  fits: LocalModelFits,
  settings: LocalModelsSettings,
): { tone: "ok" | "warn" | "bad"; label: string; sentence: string } {
  const hours = `${settings.night_start}–${settings.night_end}`;
  if (fits === "day") {
    return {
      tone: "ok",
      label: "by day",
      sentence: `Fits the daytime budget (${gbFigure(settings.day_budget_gb)}), so it can run at any hour.`,
    };
  }
  if (fits === "night") {
    return {
      tone: "warn",
      label: "overnight only",
      sentence:
        `It needs more than the daytime budget (${gbFigure(settings.day_budget_gb)}), ` +
        `so it only runs overnight, ${hours}.`,
    };
  }
  return {
    tone: "bad",
    label: "over budget",
    sentence:
      `It needs more than the overnight budget (${gbFigure(settings.night_budget_gb)}). ` +
      "It will not run until the budget is raised.",
  };
}

const TAKES: Record<string, string> = {
  image: "pictures",
  text: "text",
  audio: "sound",
  video: "video",
};

/** What a model reads, as its page says: "pictures, text". */
export function takesText(takes: string[]): string {
  if (!takes.length) return "not stated";
  return takes.map((kind) => TAKES[kind] ?? kind).join(", ");
}

/** The first sentence apart from the rest, to set it in bold. */
export function leadSentence(text: string): [string, string] {
  const found = /^(.+?[.!?])\s+([\s\S]*)$/.exec(text.trim());
  return found ? [found[1] ?? text, found[2] ?? ""] : [text.trim(), ""];
}

export type StartKey = "software" | "model" | "role" | "test";

export interface StartStep {
  key: StartKey;
  text: string;
  done: boolean;
  /** The work under way for this line, shown as its progress. */
  job: LocalModelJob | null;
}

export interface StartPlan {
  steps: StartStep[];
  /** All four done: the list folds into `summary`. */
  done: boolean;
  summary: string;
  /** What "Install" installs: the first kind supported here and not installed. */
  install: LocalModelRuntime | null;
  /** Why nothing can be installed here, when no kind is supported. */
  cannotInstall: string | null;
  /** What "Test" tests. */
  testModel: LocalModel | null;
}

function joinTitles(titles: string[]): string {
  if (titles.length <= 1) return titles[0] ?? "";
  return `${titles.slice(0, -1).join(", ")} and ${titles[titles.length - 1]}`;
}

/**
 * Getting started: four lines, each done or not, read off the overview —
 * never off anything the tab remembers.
 */
export function startSteps(overview: LocalModelsOverview): StartPlan {
  const { runtimes, models, roles, jobs } = overview;
  const installed = models.filter((model) => model.state === "installed");
  const readyRoles = roles.filter((role) => role.ready && role.model_id);
  const steps: StartStep[] = [
    {
      key: "software",
      text: "Install the software that runs models",
      done: runtimes.some((runtime) => !!runtime.installed),
      job: runtimes.map((runtime) => runtime.job).find(isActiveJob) ?? null,
    },
    {
      key: "model",
      text: "Add a model",
      done: installed.length > 0,
      job: jobs.find((job) => job.kind === "download" && isActiveJob(job)) ?? null,
    },
    {
      key: "role",
      text: "Give it a role",
      done: readyRoles.length > 0,
      job: roles.map((role) => role.job).find(isActiveJob) ?? null,
    },
    {
      key: "test",
      text: "Test it",
      done: models.some((model) => model.test?.state === "done"),
      job: jobs.find((job) => job.kind === "test" && isActiveJob(job)) ?? null,
    },
  ];
  const done = steps.every((step) => step.done);
  const titles = readyRoles.map((role) => `the ${role.title}`);
  const summary = done
    ? `Set up — ${joinTitles(titles)} ${titles.length > 1 ? "are" : "is"} ready.`
    : "";
  const supported = runtimes.filter((runtime) => runtime.supported);
  const runnable = installed.filter((model) => model.approved && model.runtime_installed);
  return {
    steps,
    done,
    summary,
    install: supported.find((runtime) => !runtime.installed) ?? null,
    cannotInstall:
      runtimes.length && !supported.length
        ? (runtimes.find((runtime) => runtime.unsupported_reason)?.unsupported_reason ?? null)
        : null,
    testModel: runnable[0] ?? installed[0] ?? null,
  };
}

// ---- Add a model and Installed models (ModelsAdd, ModelsInstalled) ----------

/**
 * The first model the page suggests: Google's Gemma 4 E4B, packaged for the
 * Mac by mlx-community. Small enough for the daytime budget, from a maker
 * and a converter that are on the built-in lists.
 */
export const SUGGESTED_MODEL = "mlx-community/gemma-4-e4b-it-4bit";

/** A model's page on Hugging Face (any build). */
export function hfLink(repoId: string): string {
  return `https://huggingface.co/${repoId}`;
}

/** A day on the calendar, "6 Jul 2026". UTC, as Hugging Face dates a build:
 *  a build made late at night must not read as the day after. */
export function dayText(iso: string | null | undefined): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return `${at.getUTCDate()} ${MONTHS[at.getUTCMonth()]} ${at.getUTCFullYear()}`;
}

/** The same "13 Nov 2026" as `dayText`, on this browser's clock: for a moment
 *  FrameFound recorded itself (a model added), whose day is the one the owner
 *  lived through. Details once showed "made 1 Oct 2026" beside "Nov 13, 2026". */
export function localDayText(iso: string | null | undefined): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return `${at.getDate()} ${MONTHS[at.getMonth()]} ${at.getFullYear()}`;
}

/** Details' "Added" line: "13 Nov 2026 by owner@example.com". */
export function addedText(model: Pick<LocalModel, "added_at" | "added_by">): string {
  const day = localDayText(model.added_at) || "—";
  return model.added_by ? `${day} by ${model.added_by}` : day;
}

/** What the field holds, ready to ask about; null when there is nothing to
 *  ask. A pasted address is sent as it is: the server reads it, and its own
 *  sentence says what is wrong with anything it cannot. */
export function checkName(text: string): string | null {
  const name = text.trim();
  return name ? name : null;
}

/** The pill of one row of the Check card. */
export function checkToneWord(tone: LocalModelCheck["tone"]): string {
  return tone === "ok" ? "ok" : tone === "warn" ? "warning" : "problem";
}

/** The problems a refused check found besides the one its verdict names. */
export function otherProblems(check: LocalModelPreflight): string[] {
  return check.problems.filter((problem) => problem !== check.verdict);
}

/** The Download button: "Download (4.8 GB)", or "Download Q4_K_M (24.3 GB)"
 *  when the repository holds several builds, so the button says which one
 *  it fetches. */
export function downloadLabel(check: LocalModelPreflight): string {
  // `?? []`: a card drawn from an answer of an older server has no list.
  const several = (check.variants ?? []).length > 1;
  const which = several && check.variant ? ` ${check.variant}` : "";
  return `Download${which} (${gbText(check.size_bytes)})`;
}

// ---- Which build (a repository of several GGUF builds) ---------------------

/** The sentence under "Which build". */
export const BUILDS_INTRO =
  "This repository holds several builds of the same model. Smaller ones need less memory and " +
  "answer a little worse.";

/** Above this many builds the list scrolls inside its own box: a repository
 *  read for the plan holds 21, which would push Download off the screen. */
export const BUILDS_SHOWN = 8;

/** How long "Which build" waits after a choice before it asks the server to
 *  check it. A native radio is chosen by every arrow press, so without the
 *  wait holding an arrow down a 21-build list sends a Hugging Face check for
 *  every row it passes; with it, only the build the keyboard stops on. */
export const BUILD_RECHECK_MS = 400;

/** Runs only the last of a quick run of choices, once they have stopped for
 *  `delayMs`; `stop` drops the one waiting. */
export interface Settler {
  choose(run: () => void): void;
  stop(): void;
}

export function settler(delayMs: number): Settler {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const stop = () => {
    if (timer !== null) clearTimeout(timer);
    timer = null;
  };
  return {
    choose(run) {
      stop();
      timer = setTimeout(() => {
        timer = null;
        run();
      }, delayMs);
    },
    stop,
  };
}

/**
 * The builds a check offers, smallest first; builds of the same size keep
 * the server's order. Empty for a repository of one build (every MLX one):
 * the page then shows no list at all.
 */
export function buildsInOrder(check: LocalModelPreflight): LocalModelVariant[] {
  if (check.variants.length < 2) return [];
  return [...check.variants].sort((a, b) => a.size_bytes - b.size_bytes);
}

export interface BuildText {
  /** What it downloads: "24.3 GB". */
  size: string;
  /** "about 29.2 GB of memory" — the server's estimate, model and picture
   *  part together. */
  memory: string;
  /** The same pill an installed model shows: by day, overnight only, over
   *  budget. */
  fits: { tone: "ok" | "warn" | "bad"; label: string; sentence: string };
  suggested: boolean;
  /** "" or the server's sentence about a very small build. */
  note: string;
  /** Whether it can look at pictures, and what that part adds. */
  reads: string;
}

/** One row of "Which build", in the page's words; every figure is the
 *  server's for that build. */
export function buildText(variant: LocalModelVariant, settings: LocalModelsSettings): BuildText {
  const pictures = variant.files.filter((file) => file.part === "pictures");
  const picturesBytes = pictures.reduce((sum, file) => sum + file.size, 0);
  return {
    size: gbText(variant.size_bytes),
    memory: `about ${gbFigure(variant.memory_gb)} of memory`,
    fits: fitsText(variant.fits, settings),
    suggested: variant.recommended,
    note: variant.note,
    reads: pictures.length
      ? `Includes the part that lets it read pictures (${gbText(picturesBytes)}).`
      : "This build reads text only.",
  };
}

/** Why a row of "Software that runs models" is empty without being a fault:
 *  nothing installed, and nothing added runs on it. Null otherwise, and null
 *  for the `first` row: that is the program Getting started installs, and
 *  telling the owner nothing needs it would steer them away from step one
 *  (doctor gives the first kind a line for the same reason). */
export function runtimeNeeded(runtime: LocalModelRuntime, first = false): string | null {
  if (first || runtime.installed || runtime.models > 0) return null;
  return "Nothing you have added needs it.";
}

/** The line under Download: which build, and that it is kept to exactly. */
export function buildLine(check: LocalModelPreflight): string {
  const day = dayText(check.build_date);
  return (
    `Build ${check.revision_short}${day ? `, ${day}` : ""}. FrameFound downloads exactly this ` +
    "build and never changes it by itself. You can leave this page; the download carries on."
  );
}

/** What the page says once a download has been asked for. */
export function startedSentence(model: LocalModel): string {
  return (
    `${rowName(model)} is downloading. Its bar is under Models, above. ` +
    "You can leave this page; the download carries on."
  );
}

/** "by google · packaged by mlx-community · build 475b908", and for one build
 *  of a repository that holds several, "… · build 49a5b1a · Q4_K_M". */
export function byLine(model: LocalModel): string {
  const parts: string[] = [];
  if (model.publisher_is === "converter") {
    if (model.maker) parts.push(`by ${model.maker}`);
    parts.push(`packaged by ${model.publisher}`);
  } else {
    parts.push(`by ${model.maker ?? model.publisher}`);
  }
  parts.push(`build ${model.revision_short}`);
  // Two builds of one repository (llama.cpp's files) are two rows that differ
  // only here, so the build's name is what tells them apart (the bare
  // `variant` when an older server's answer has no label).
  const which = buildName(model);
  if (which) parts.push(which);
  return parts.join(" · ");
}

/** The build's name ("Q4_K_M") of one build of a repository that holds
 *  several; null for a model of one build (every MLX one). The label is the
 *  server's word on it, "" included; the bare `variant` only stands in when
 *  an older server's answer has no label at all. */
function buildName(model: Pick<LocalModel, "variant" | "variant_label">): string | null {
  return (model.variant_label ?? model.variant) || null;
}

/**
 * A row's model as a sentence names it: "demo-vision-gguf (Q8_0)" for one
 * build of a repository that holds several, the plain name otherwise. Two
 * builds of one repository share their name (the server's is the
 * repository's), so a question about one row — above all Remove's, the last
 * look before gigabytes are deleted — has to say which build it means.
 */
export function rowName(model: Pick<LocalModel, "name" | "variant" | "variant_label">): string {
  const which = buildName(model);
  return which ? `${model.name} (${which})` : model.name;
}

/** Test's title: a model that does not read pictures is asked in words
 *  (ModelsTest does exactly that), so the title must not promise a picture. */
export function testTitle(model: LocalModel): string {
  return model.takes.includes("image")
    ? `Ask ${rowName(model)} one question about a picture`
    : `Ask ${rowName(model)} one question in words`;
}

/**
 * Details' line for one build of a repository that holds several: which build,
 * and what was downloaded with it. A GGUF build reads pictures exactly when
 * it came with the part that does (the server sets `takes` from that part).
 * Null for a model of one build (every MLX one): the Build line says it all.
 */
export function buildPartsText(model: LocalModel): string | null {
  const which = buildName(model);
  if (!which) return null;
  return model.takes.includes("image")
    ? `${which} · the model file, and the part that reads pictures`
    : `${which} · the model file; it reads text only`;
}

/** "≈ 5.8 GB" while it is an estimate, "6.1 GB measured" once it has run. */
export function memoryText(model: LocalModel): string {
  return model.memory_measured ? `${gbFigure(model.memory_gb)} measured` : `≈ ${gbFigure(model.memory_gb)}`;
}

/** The licence as its page states it; `missing` when it states none. */
export function licenceText(model: LocalModel): { text: string; url: string | null; missing: boolean } {
  if (!model.licence) return { text: "none stated", url: null, missing: true };
  const text =
    model.licence === "other" && model.licence_name
      ? `other — ${model.licence_name}`
      : model.licence;
  return { text, url: model.licence_url, missing: false };
}

/** The "Runs on" cell: the software's name, a pill when it cannot run yet,
 *  and the server's sentence when it has one. */
export function runsOnText(model: LocalModel): {
  label: string;
  pill: { tone: "warn" | "bad"; text: string } | null;
  sentence: string | null;
} {
  if (!model.runtime_known) {
    return {
      label: model.runtime_label || model.runtime,
      pill: { tone: "bad", text: "missing" },
      sentence: model.runtime_problem ?? "It needs a program this install does not have.",
    };
  }
  if (!model.runtime_installed) {
    return {
      label: model.runtime_label,
      pill: { tone: "warn", text: "not installed" },
      sentence: model.runtime_problem,
    };
  }
  return { label: model.runtime_label, pill: null, sentence: model.runtime_problem };
}

function titleOfRole(roles: LocalModelRole[], role: string): string {
  return roles.find((entry) => entry.role === role)?.title ?? role;
}

/** The Role cell: "Looker" for a role it holds, "→ Editor" for one it is
 *  being copied here to take over. */
export function roleLabels(model: LocalModel, roles: LocalModelRole[]): string[] {
  return [
    ...model.roles.map((role) => titleOfRole(roles, role)),
    ...model.pending_roles.map((role) => `→ ${titleOfRole(roles, role)}`),
  ];
}

/** What a row's last cell offers: the work under way, Resume for a download
 *  that stopped, Download again for files gone from the library, or the
 *  usual buttons. */
export type RowAction = "job" | "incomplete" | "stalled" | "missing" | "installed";

export function rowAction(model: LocalModel): RowAction {
  if (isActiveJob(model.job)) return "job";
  if (model.state === "incomplete") return "incomplete";
  // "downloading" with nothing running: its worker went away. Resume picks
  // it up (the server ends the dead job first).
  if (model.state === "downloading") return "stalled";
  if (model.in_library === false) return "missing";
  return "installed";
}

/**
 * Why Remove cannot be pressed now, in the words the server would refuse it
 * with (it says the same if asked); null when it can. The button stays on
 * screen, disabled, with this as its reason, so it is never a mystery why.
 */
export function removeBlocker(
  model: LocalModel,
  roles: LocalModelRole[],
  library: LocalModelLibrary | null,
): string | null {
  const held = model.roles.map((role) => titleOfRole(roles, role));
  if (held.length === 1) {
    return `It is the ${held[0]}. Give that role to another model, or take it away, first.`;
  }
  if (held.length > 1) {
    return (
      `It is the ${held.join(" and the ")}. Give those roles to other models, or take them ` +
      "away, first."
    );
  }
  const waiting = model.pending_roles.map((role) => titleOfRole(roles, role));
  if (waiting.length) {
    return (
      `It is being made the ${waiting.join(" and the ")}: its files are being copied to this ` +
      "machine. Cancel that first."
    );
  }
  if (isActiveJob(model.job) && model.job.kind === "download") {
    return "It is being downloaded. Cancel the download first.";
  }
  if (model.test?.state === "running") {
    return "It is being tested right now. Remove it when the test has finished.";
  }
  if (library && !library.connected) {
    return (
      "The model library is not connected, so its files cannot be removed. Reconnect the " +
      "drive or share and try again."
    );
  }
  return null;
}

/** Why a download (Download, Resume, Download again) cannot start now. */
export function downloadBlocker(library: LocalModelLibrary | null): string | null {
  if (!library || library.connected) return null;
  return library.problem ?? "The model library is not connected.";
}

/** The question Remove asks. */
export function removeQuestion(model: LocalModel): string {
  if (model.state === "installed") {
    return (
      `Remove ${rowName(model)}? This frees ${gbText(model.size_bytes)} in the model library. ` +
      "It can be downloaded again. Anything it already suggested is kept."
    );
  }
  return (
    `Remove ${rowName(model)}? What was downloaded of it is deleted from the model library. ` +
    "It can be downloaded again."
  );
}

/** What the page says once a model is removed. */
export function removedSentence(name: string, freedBytes: number): string {
  return freedBytes > 0 ? `${name} is removed; ${gbText(freedBytes)} freed.` : `${name} is removed.`;
}

/** Where a model's files are, for its Details. */
export function whereText(model: LocalModel, library: LocalModelLibrary | null): string {
  let where: string;
  if (model.in_library === false) where = "Not in the model library";
  else if (model.in_library === null) where = "Whether it is in the model library could not be checked just now";
  else where = library?.path ? `In the model library on ${library.path}` : "In the model library";
  if (library?.copies_to_this_machine && model.on_this_machine) where += " · also on this Mac";
  return `${where}.`;
}

const STANDING: Record<LocalModelBuiltFrom["publisher_is"], string> = {
  maker: "an approved maker",
  converter: "a converter",
  blocked: "a blocked publisher",
  unknown: "not on your lists",
};

/** One model it says it was built from: "google/gemma-4-E4B-it, by google —
 *  an approved maker". */
export function builtFromText(entry: LocalModelBuiltFrom): string {
  if (!entry.found) return `${entry.repo_id} — not on Hugging Face now`;
  const by = entry.publisher ? `, by ${entry.publisher}` : "";
  return `${entry.repo_id}${by} — ${STANDING[entry.publisher_is]}`;
}

// ---- Who does what and Test (ModelsRoles, ModelsTest) -----------------------

/** The roles this page can give; a role a later update adds is shown, not given. */
export function isRoleName(role: string): role is LocalAiRoleName {
  return role === "looker" || role === "editor";
}

/** A refusal cut down to follow a model's name in a list:
 *  "It does not read pictures." → "does not read pictures". */
export function shortReason(sentence: string): string {
  let text = sentence.trim().replace(/\.$/, "");
  if (text.startsWith("It ")) text = text.slice(3);
  return text;
}

export interface RoleOption {
  id: string;
  label: string;
  disabled: boolean;
}

/**
 * The "Give this role to…" list: every model, so none goes missing from it;
 * one that cannot hold the role is disabled with the server's reason in its
 * text ("gemma-4-12B-it-OptiQ-4bit — does not read pictures"), the same
 * reason the server would refuse it with.
 */
export function roleOptions(models: LocalModel[], role: LocalModelRole): RoleOption[] {
  // Each option names its build ("demo-vision-gguf (Q8_0)"), so two builds of
  // one repository — same name, often the same revision — read apart. Rows
  // that still read the same (one build downloaded at two revisions, or two
  // MLX downloads of one name) add the revision as well.
  const named = new Map<string, number>();
  for (const model of models) named.set(rowName(model), (named.get(rowName(model)) ?? 0) + 1);
  return models.map((model) => {
    let reason: string | null = null;
    if (model.id === role.pending_model_id) reason = "being copied to this Mac for it";
    else if (model.id === role.model_id) reason = "has this role";
    else {
      const problem = model.role_problems[role.role];
      if (problem) reason = shortReason(problem);
    }
    const which = buildName(model);
    const name =
      (named.get(rowName(model)) ?? 0) > 1
        ? `${model.name} (${which ? `${which}, ` : ""}build ${model.revision_short})`
        : rowName(model);
    // Not disabled: the owner may give it the role anyway, after one question.
    const label = reason ? `${name} — ${reason}` : notCompared(model) ? `${name} — ${NOT_COMPARED}` : name;
    return { id: model.id, label, disabled: reason !== null };
  });
}

// ---- The Compare question (L6): a llama.cpp model nobody has seen answer ----

/** The pill on a model, and on a role held by one, that has not been
 *  through Compare; and the pill's title. */
export const NOT_COMPARED = "not compared yet";
export const NOT_COMPARED_TITLE = "It has not been through Compare on your footage.";

/** A model whose software asks before it is given a role (llama.cpp) and
 *  that nobody has yet seen answer on this footage. Only an explicit false
 *  counts: an answer from a server that does not judge it says nothing. */
export function notCompared(model: Pick<LocalModel, "needs_compare" | "compared">): boolean {
  return model.needs_compare && model.compared === false;
}

/** The Compare view with this model ticked (and only it). */
export function compareHref(modelId: string): string {
  return `/models/compare?model=${encodeURIComponent(modelId)}`;
}

/** A Test in words that finished with an answer, but not in the form asked
 *  for. The server does not count that answer (only one in the form asked
 *  clears "not compared yet"), though the row shows it as its last test. */
export function answeredOutOfForm(model: Pick<LocalModel, "test">): boolean {
  const test = model.test;
  return !!test && test.state === "done" && test.kind === "text" && !test.structured_ok;
}

const IN_FORM = "Test asks it one question in words; an answer in the form FrameFound asks for clears this.";

/** The pill's title. Compare looks at pictures, so a model that reads none
 *  is cleared instead by a Test in words — and only by one answered in the
 *  form asked (the server's rule), so a Test answered in free text, which
 *  the row shows, is said not to count rather than said not to exist. */
export function notComparedTitle(model: Pick<LocalModel, "takes" | "test">): string {
  if (model.takes.includes("image")) return NOT_COMPARED_TITLE;
  return answeredOutOfForm(model)
    ? `Its last Test answered, but not in the form FrameFound asks for, so that answer does not count. ${IN_FORM}`
    : `It has not yet answered a Test in the form FrameFound asks for. ${IN_FORM}`;
}

/**
 * The way to stop being asked about a model: Compare for one that reads
 * pictures, its Test (in its row under Models) for one that does not —
 * Compare would only say it cannot look. `first` for the card's line after
 * the question was answered "no" ("Compare it first").
 */
export function wayOut(
  model: Pick<LocalModel, "id" | "takes">,
  first = false,
): { label: string; href: string } {
  const then = first ? " first" : "";
  return model.takes.includes("image")
    ? { label: `Compare it${then}`, href: compareHref(model.id) }
    : { label: `Test it${then}`, href: "#models" };
}

/** Said in the role's card when the owner answered the question "no"; the
 *  card follows it with `wayOut(model, true)`. */
export const NOTHING_CHANGED = "Nothing was changed.";

/** Under the name of a model that holds a role without being compared for
 *  it (given anyway); the card follows it with `wayOut(model)` for someone
 *  who can act on it, `wayOutForViewer(model)` for anyone else. Goes by
 *  itself once a Compare run (or, for a model that reads no pictures, a
 *  Test in words answered in the form asked) finishes. */
export function givenUncompared(model: Pick<LocalModel, "takes" | "test">): string {
  if (model.takes.includes("image")) return "It was given this role without being compared on your footage.";
  const before = "It was given this role before it answered a Test in the form FrameFound asks for";
  return answeredOutOfForm(model) ? `${before}; its last answer was not in that form.` : `${before}.`;
}

/** The way out said, not linked, to someone who cannot Compare or Test: the
 *  Compare page would only say an administrator starts one, and the row has
 *  no Test button for them. */
export function wayOutForViewer(model: Pick<LocalModel, "takes">): string {
  return model.takes.includes("image")
    ? "An administrator can compare it on your footage."
    : "An administrator can test it.";
}

/**
 * The line under both role cards. What a role does is said from what this
 * FrameFound has built: the Looker looks through a folder when asked
 * ("Review with AI", in Browse's Folders view and on Libraries) and at new
 * footage in the libraries switched on for it, and suggests colour looks (a
 * video's page, a listing's photographs); the Editor drafts a rough cut when
 * asked on the Cuts page ("Draft a cut") or in the chat panel. Each sentence
 * names where the work is asked for, so it is corrected whenever such a
 * place is added: this one went on saying "in the chat panel" alone after
 * the Cuts page existed. The llama.cpp sentence only where a model can run
 * on software that asks.
 */
export function rolesNote(overview: Pick<LocalModelsOverview, "models" | "runtimes">): string {
  const parts = [
    "Giving a model a role copies it to this Mac so it loads quickly; taking the role away " +
      "frees that copy. Changing a role's model does not redo work already done.",
    "The Looker looks through a folder when you ask it to (Review with AI, in Browse's Folders " +
      "view and on Libraries) and at new footage in the libraries you switch on, and suggests " +
      "colour looks for a video and for a listing's photographs. The Editor drafts a rough cut " +
      "when you ask for one: Draft a cut on the Cuts page, in Search or Browse, or ask in the " +
      "chat panel.",
  ];
  const asks =
    overview.models.some((model) => model.needs_compare) ||
    overview.runtimes.some(isLlamaCppKind);
  if (asks) {
    parts.push(
      "A model that runs on llama.cpp is new here: FrameFound asks once before giving it a role " +
        "it has not been compared for.",
    );
  }
  return parts.join(" ");
}

/** Test's empty panel: what Test will ask this model. */
export function notTestedSentence(model: Pick<LocalModel, "takes">): string {
  return model.takes.includes("image")
    ? "Not tested yet. Test asks it one question about a picture; nothing it answers is saved."
    : "Not tested yet. Test asks it one question in words; nothing it answers is saved.";
}

/** Why Assign cannot be pressed for this model now; null when it can. In the
 *  server's words, so the disabled button says what pressing it would. */
export function assignBlocker(
  role: LocalModelRole,
  model: LocalModel | null,
  library: LocalModelLibrary | null,
): string | null {
  if (role.pending_model_id && isActiveJob(role.job)) {
    return role.pending_model_id === model?.id
      ? "It is already being copied to this machine for that role."
      : "That role is already being given to another model. Cancel that first.";
  }
  if (!model) return null;
  const problem = model.role_problems[role.role];
  if (problem) return problem;
  // A model that has to come from the library cannot while it is away; one
  // already on this Mac can be given the role all the same.
  if (library && !library.connected && model.on_this_machine !== true) return downloadBlocker(library);
  return null;
}

type Named = Pick<LocalModel, "id" | "name" | "variant" | "variant_label">;

/**
 * A role's model by its row name, build and all, when it is among `models`;
 * else the server's name for it. The server names a role's model after its
 * repository alone, and two builds of one repository both answer to that, so
 * a sentence about switching from one to the other could not say which.
 */
export function nameIn(models: readonly Named[], id: string | null, fallback: string | null): string | null {
  const model = id ? models.find((one) => one.id === id) : undefined;
  return model ? rowName(model) : fallback;
}

/** What Assign asks before it replaces the model that has the role; null
 *  when nothing is replaced. `models` names the holder's build. */
export function roleQuestion(
  role: LocalModelRole,
  chosen: LocalModel,
  models: readonly Named[] = [],
): string | null {
  if (!role.model_id || role.model_id === chosen.id) return null;
  const holder = nameIn(models, role.model_id, role.model_name) ?? "the model it has now";
  return `Make ${rowName(chosen)} the ${role.title} instead of ${holder}?`;
}

/**
 * What the page says once a role was asked for. The server answers 202 with
 * `pending_model_id` set when the model has to be copied to this Mac first;
 * the old model keeps the role until the copy is whole. `models` names the
 * old model's build.
 */
export function roleGivenSentence(answer: LocalModelRole, chosen: string, models: readonly Named[] = []): string {
  if (!answer.pending_model_id) return `${chosen} is now the ${answer.title}.`;
  if (answer.pending_model_id === answer.model_id) return `Copying ${chosen} to this Mac again.`;
  const holder = nameIn(models, answer.model_id, answer.model_name);
  const meanwhile = holder
    ? `${holder} is still the ${answer.title} until it finishes.`
    : `It becomes the ${answer.title} when the copy is whole.`;
  return `${chosen} is being copied to this Mac. ${meanwhile}`;
}

/** What "Take the role away" asks. `models` names the builds. */
export function roleTakeQuestion(
  role: LocalModelRole,
  library: LocalModelLibrary | null,
  models: readonly Named[] = [],
): string {
  const pending = nameIn(models, role.pending_model_id, role.pending_model_name);
  if (!role.model_id && role.pending_model_name && pending) {
    return (
      `Stop making ${pending} the ${role.title}? The copy under way is ` +
      "cancelled; the model stays in the library."
    );
  }
  const who = nameIn(models, role.model_id, role.model_name) ?? "its model";
  const after = library?.copies_to_this_machine
    ? "Its copy on this Mac is removed; the model stays in the library."
    : "The model stays installed.";
  return `Take the ${role.title} role away from ${who}? No model will be the ${role.title}. ${after}`;
}

/** A test waiting or under way. */
export function isActiveTest(test: LocalModelTest | null | undefined): boolean {
  return !!test && ACTIVE.has(test.state);
}

/** How long a finished test stays open under its row on its own: long enough
 *  to come back to a test started from Getting started, not so long that
 *  every model's old answer crowds the table. */
export const TEST_SHOWN_MS = 15 * 60_000;

/** Whether a model's Test panel is open when nobody has opened or closed it. */
export function testOpenByDefault(test: LocalModelTest | null | undefined, nowMs: number = Date.now()): boolean {
  if (!test) return false;
  if (isActiveTest(test)) return true;
  const ended = test.finished_at ? Date.parse(test.finished_at) : NaN;
  return Number.isFinite(ended) && nowMs - ended < TEST_SHOWN_MS;
}

/**
 * Why Test cannot be pressed now; null when it can. The server's own
 * refusals, so the disabled button says what pressing it would. A model
 * that only fits overnight, or a pause, is NOT a reason: the test waits and
 * says until when.
 */
export function testBlocker(model: LocalModel, settings: LocalModelsSettings): string | null {
  if (model.state === "downloading") return "It is still downloading.";
  if (model.state !== "installed") return "Its download did not finish. Resume the download first.";
  if (!model.runtime_known) return "It needs a program this install does not have.";
  if (!model.runtime_installed) {
    return `Install ${model.runtime_label} first (Software that runs models, below).`;
  }
  if (!model.approved) {
    return model.approval_problem
      ? `It is no longer approved: ${model.approval_problem}`
      : "It is no longer approved.";
  }
  if (model.fits === "never") return fitsText("never", settings).sentence;
  if (model.test?.state === "running") return "It is being tested right now.";
  if (isActiveTest(model.test)) return "A test of this model is already waiting.";
  return null;
}

/**
 * The line beside a test's spinner. The server's own sentence when it has
 * one (why it waits and until when); while it runs with nothing to say,
 * what is most likely taking the time.
 */
export function testSentence(
  job: LocalModelJob,
  model: LocalModel,
  server: LocalModelServer,
  ahead?: string | null,
): string {
  if (job.state !== "running" || job.cancelling || job.message) return jobSentence(job, ahead);
  if (server.model_id === model.id && (server.state === "ready" || server.state === "busy")) {
    return "Asking it — the model is already loaded.";
  }
  if (model.roles.length || model.on_this_machine) {
    return "Starting the model — the first answer after a load takes longest.";
  }
  return "Loading it from the library; this is slower than a model that has a role.";
}

/** What a finished test's answer is, read defensively: the server keeps what
 *  the model said, and a model can say anything. */
export type TestAnswer =
  | { form: "look"; caption: string; tags: string[] }
  | { form: "order"; order: number[]; reason: string }
  | { form: "prose"; raw: string };

export function testAnswer(test: LocalModelTest): TestAnswer | null {
  if (test.state !== "done") return null;
  const answer = test.answer;
  if (!test.structured_ok || !answer) return { form: "prose", raw: test.raw ?? "" };
  if (test.kind === "text") {
    const order = Array.isArray(answer.order)
      ? answer.order.filter((step): step is number => typeof step === "number")
      : [];
    return { form: "order", order, reason: typeof answer.reason === "string" ? answer.reason : "" };
  }
  const tags = Array.isArray(answer.tags)
    ? answer.tags.filter((tag): tag is string => typeof tag === "string" && tag.trim() !== "")
    : [];
  return { form: "look", caption: typeof answer.caption === "string" ? answer.caption : "", tags };
}

/** "2, then 1, then 3": the shots in the order a text test put them. */
export function orderText(order: number[]): string {
  return order.length ? order.join(", then ") : "no order given";
}

/** "4.2 s", "14 s", "2 min 5 s". */
export function secondsText(seconds: number): string {
  if (seconds < 10) return `${(Math.round(seconds * 10) / 10).toFixed(1)} s`;
  if (seconds < 120) return `${Math.round(seconds)} s`;
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)} min ${whole % 60} s`;
}

/** The picture a test showed the model, in words: the drawn one says so. */
export function testPictureCaption(test: LocalModelTest): string | null {
  const sample = test.sample;
  if (!sample || test.kind !== "picture") return null;
  if (!sample.asset_id) return "A picture FrameFound drew for the test";
  return sample.filename ? `${sample.label}: ${sample.filename}` : sample.label;
}

const RAN_FROM: Record<string, string> = { this_machine: "this Mac", library: "the model library" };
const MEMORY_SOURCE: Record<string, string> = {
  runtime: "as the software reports it",
  process: "the size of its process",
};

/** The figures of a finished test, as label and value, only those it has. */
export function testFacts(
  test: LocalModelTest,
  runtimes: LocalModelRuntime[],
  nowMs: number = Date.now(),
): Array<[string, string]> {
  const facts: Array<[string, string]> = [];
  if (test.seconds != null) facts.push(["Time to answer", secondsText(test.seconds)]);
  if (test.load_seconds != null) {
    facts.push([
      "Time to load",
      test.load_seconds > 0 ? secondsText(test.load_seconds) : "none — it was already loaded",
    ]);
  }
  if (test.memory_gb != null) {
    const source = test.memory_source ? MEMORY_SOURCE[test.memory_source] : undefined;
    facts.push(["Memory", source ? `${gbFigure(test.memory_gb)} (${source})` : gbFigure(test.memory_gb)]);
  }
  if (test.model) facts.push(["Build", test.model.revision_short]);
  if (test.runtime) {
    const label = runtimes.find((runtime) => runtime.name === test.runtime)?.label ?? test.runtime;
    facts.push(["Software", test.runtime_version ? `${label} ${test.runtime_version}` : label]);
  }
  if (test.ran_from) facts.push(["Ran from", RAN_FROM[test.ran_from] ?? test.ran_from]);
  const when = whenText(test.finished_at ?? test.asked_at, nowMs);
  if (when) facts.push(["When", when]);
  return facts;
}

/**
 * After a failed test: when the model last loaded under another version of
 * its software than the one in use, say so, and offer to go back to it when
 * that version is the one kept. `last_loaded_with` is "mlx-vlm/0.7.11".
 */
export function rollbackOffer(
  model: LocalModel,
  runtimes: LocalModelRuntime[],
): { sentence: string; runtime: LocalModelRuntime; version: string | null } | null {
  const loaded = model.last_loaded_with ?? "";
  const cut = loaded.indexOf("/");
  if (cut <= 0) return null;
  const name = loaded.slice(0, cut);
  const version = loaded.slice(cut + 1);
  const runtime = runtimes.find((entry) => entry.name === name);
  if (!version || !runtime?.installed || runtime.installed === version) return null;
  return {
    sentence: `It last loaded under ${runtime.label} ${version}.`,
    runtime,
    version: runtime.previous === version ? version : null,
  };
}

/** What going back to an older version of the software asks. */
export function rollbackQuestion(version: string): string {
  return (
    `Go back to ${version}? The model in memory is unloaded; it loads again with the older ` +
    "version the next time it is needed."
  );
}

/** Which page of one-photograph pages "Try another picture" asks for: any of
 *  them, so the next test is not the newest photograph again. */
export function randomPage(total: number, random: () => number = Math.random): number | null {
  if (!(total > 0)) return null;
  return Math.min(total, Math.floor(random() * total) + 1);
}

// ---- Software that runs models and Sharing this Mac (ModelsRuntimes, ModelsSharing) ----

/**
 * The newest install of this software in the overview's jobs (they come
 * active first, then finished, newest first). An install's subject is
 * "<label> <version>", e.g. "MLX-VLM 0.7.13".
 */
export function lastInstall(runtime: LocalModelRuntime, jobs: LocalModelJob[]): LocalModelJob | null {
  const prefix = `${runtime.label} `;
  return (
    jobs.find((job) => job.kind === "runtime_install" && job.subject.startsWith(prefix)) ?? null
  );
}

/** The version an install job installs, from its subject. */
export function installVersion(job: LocalModelJob, runtime: LocalModelRuntime): string | null {
  const prefix = `${runtime.label} `;
  if (!job.subject.startsWith(prefix)) return null;
  return job.subject.slice(prefix.length).trim() || null;
}

export interface RuntimePlan {
  /** The install under way, shown as a spinner and its sentence. */
  working: LocalModelJob | null;
  /** The newest install failed and nothing is under way: its sentence and the
   *  installer's last lines are shown, and the version in use is untouched. */
  failed: LocalModelJob | null;
  /** "Try again" installs this version (the one that failed). */
  retry: string | null;
  /** "Install": nothing installed yet; it installs the newest. */
  install: boolean;
  /** "Update to X": a newer version than the one in use is out. */
  update: string | null;
  /** "Roll back to X": the version before the last update is kept. */
  rollback: string | null;
  /** The kept version is the newest, not an older one: after a roll back the
   *  two swap, and the way forward again is the same instant swap. */
  rollbackIsNewer: boolean;
}

/**
 * Which buttons the software's row offers. While an install runs they cannot
 * be pressed (the server takes one install at a time and refuses a roll back
 * meanwhile). "Try again" takes the place of an Install or Update that would
 * install the very version that just failed. After a roll back the version
 * kept is the newer one: going back to it is offered as that swap, and not
 * also as an Update that would download it again.
 */
export function runtimePlan(runtime: LocalModelRuntime, jobs: LocalModelJob[]): RuntimePlan {
  const working = isActiveJob(runtime.job) ? runtime.job : null;
  const newest = lastInstall(runtime, jobs);
  const failed = !working && newest?.state === "failed" ? newest : null;
  const failedVersion = failed ? installVersion(failed, runtime) : null;
  const retry = failedVersion && failedVersion !== runtime.installed ? failedVersion : null;
  const rollback =
    runtime.installed && runtime.previous && runtime.previous !== runtime.installed
      ? runtime.previous
      : null;
  // The server compares versions in the kind's own way; the page only knows
  // the kept one is newer when it is the newest and that is newer than ours.
  const rollbackIsNewer = !!rollback && runtime.update_available && rollback === runtime.latest;
  const update =
    runtime.installed && runtime.update_available && runtime.latest ? runtime.latest : null;
  return {
    working,
    failed,
    retry,
    install: !runtime.installed && !retry,
    update: update && update !== retry && !rollbackIsNewer ? update : null,
    rollback,
    rollbackIsNewer,
  };
}

/** What going back to the newer version kept from before a roll back asks. */
export function returnQuestion(version: string): string {
  return (
    `Go back to ${version}? The model in memory is unloaded; it loads again with ${version} the ` +
    "next time it is needed. Nothing is downloaded: it is still on this Mac."
  );
}

/** What Update asks, naming both versions. */
export function updateQuestion(runtime: LocalModelRuntime, version: string): string {
  const from = runtime.installed ?? "the version in use";
  return (
    `Update ${runtime.label} from ${from} to ${version}? The model in memory is unloaded; it ` +
    `loads again with ${version} the next time it is needed. ${from} is kept, so you can go ` +
    "back to it."
  );
}

/** What installing a particular version asks; null when there is nothing in
 *  use for it to replace. */
export function versionQuestion(runtime: LocalModelRuntime, version: string): string | null {
  if (!runtime.installed || version === runtime.installed) return null;
  return (
    `Install ${runtime.label} ${version} in place of ${runtime.installed}? The model in memory ` +
    `is unloaded; it loads again with ${version} the next time it is needed.`
  );
}

/**
 * Whether a refusal of "Install a particular version" is about the version
 * typed, and so belongs beside the field (marked invalid) rather than at the
 * top of the section. Only two are: the 400 for what is not a version, and
 * the 409 for the version already in use. The endpoint's other 409s — being
 * installed already (from another tab), the installer missing, the program
 * not supported on this machine — say nothing about what was typed and go
 * to the section like every other refusal.
 */
export function isVersionRefusal(status: number, message: string): boolean {
  if (status === 400) return true;
  return status === 409 && message.trim().endsWith("is already the version in use.");
}

/** What the page says once an install is queued. The server's answer names
 *  the version it looked up when none was asked for. */
export function installStartedSentence(answer: LocalModelRuntime): string {
  const version = answer.job ? installVersion(answer.job, answer) : null;
  const what = version ? `${answer.label} ${version}` : answer.label;
  return `Installing ${what}. It takes a few minutes; you can leave this page.`;
}

/** What Check now found. */
export function checkedSentence(answer: LocalModelRuntime): string {
  if (answer.latest_problem) return answer.latest_problem;
  if (!answer.latest) return `The newest version of ${answer.label} is not known.`;
  if (answer.update_available) {
    return `${answer.label} ${answer.latest} is out; this Mac has ${answer.installed}.`;
  }
  if (answer.installed && answer.installed === answer.latest) {
    return `${answer.label} ${answer.installed} is the newest.`;
  }
  return `The newest ${answer.label} is ${answer.latest}.`;
}

/** The sentence over a failed install's last lines: what failed, why, and
 *  that the version in use was left alone. */
export function failedInstallSentence(job: LocalModelJob, runtime: LocalModelRuntime): string {
  const version = installVersion(job, runtime);
  const what = version ? `${runtime.label} ${version}` : runtime.label;
  const why = job.message ? ` ${job.message}` : "";
  const kept = runtime.installed
    ? ` ${runtime.label} ${runtime.installed} is still the version in use.`
    : " Nothing was installed.";
  return `Installing ${what} did not finish.${why}${kept}`;
}

/** The row's "Installed" line: "0.9.0 (build b12110), since 12 Nov", or
 *  "Not installed.". The part in brackets is the server's own more-about
 *  (llama.cpp's numbered version comes from a daily build). */
export function installedText(runtime: LocalModelRuntime): string {
  if (!runtime.installed) return "Not installed.";
  const detail = runtime.installed_detail ? ` (${runtime.installed_detail})` : "";
  const day = runtime.installed_at ? dayText(runtime.installed_at) : "";
  return `${runtime.installed}${detail}${day ? `, since ${day}` : ""}`;
}

/** llama.cpp's daily builds are named by number, "b12318" (the server's own
 *  pattern); no MLX-VLM version looks like that. */
const DAILY_BUILD = /^b\d{3,7}$/;

/**
 * What the row says in place of an Update button while a daily build is in
 * use. The server never offers an update over one (a daily build is neither
 * older nor newer than a numbered version), so without this sentence the
 * missing button would read as "you have the newest". Null otherwise.
 */
export function dailyBuildSentence(runtime: LocalModelRuntime): string | null {
  if (!runtime.installed || !DAILY_BUILD.test(runtime.installed)) return null;
  return (
    `A daily build (${runtime.installed}) is installed. FrameFound does not offer updates over ` +
    "a daily build; install a numbered version below when you want one."
  );
}

/** The programs of llama.cpp's kind: llama.cpp itself and the demo's stand-in
 *  for it, which takes GGUF builds by llama.cpp's own rules so the page reads
 *  as an owner's would. One program file from GitHub, with no Python of its
 *  own: what the page says about where a program comes from follows the kind,
 *  not the name (the demo's "demo-gguf" once got MLX-VLM's "its own Python"). */
const LLAMA_CPP_KINDS = new Set(["llama-cpp", "demo-gguf"]);

/** Whether the program called `runtime.name` is of llama.cpp's kind. */
export function isLlamaCppKind(runtime: Pick<LocalModelRuntime, "name">): boolean {
  return LLAMA_CPP_KINDS.has(runtime.name);
}

/** Check now's title: where the newest version is asked. */
export function checkTitle(runtime: LocalModelRuntime): string {
  return isLlamaCppKind(runtime)
    ? `Ask GitHub for the newest numbered ${runtime.label} now`
    : `Ask the package index for the newest ${runtime.label} now`;
}

/** The example in "Install a particular version": the server's hint for this
 *  program ("0.9.0 or a daily build such as b12318"); an older server's
 *  answer has none, and then a version it knows stands in as before. */
export function versionPlaceholder(runtime: LocalModelRuntime): string {
  return runtime.version_hint || runtime.previous || runtime.latest || "";
}

/** The section's closing lines; llama.cpp, which comes from GitHub rather
 *  than a package index, adds where it comes from and how it is checked. */
export function softwareNotes(runtimes: LocalModelRuntime[]): string[] {
  const llama = runtimes.find(isLlamaCppKind);
  if (!llama) {
    return [
      "Each program lives in its own folder with its own Python, so updating one cannot break " +
        "FrameFound. The version before the last update is kept. A new kind of software arrives " +
        "with a FrameFound update; a new model on software already here never needs one.",
    ];
  }
  return [
    "Each program lives in its own folder, apart from FrameFound's own, so updating one cannot " +
      "break FrameFound. The version before the last update is kept. A new kind of software " +
      "arrives with a FrameFound update; a new model on software already here never needs one.",
    // The demo's stand-in downloads nothing: it says what the real one does.
    `${llama.demo ? "On a real install, llama.cpp" : llama.label} is downloaded from its makers' ` +
      "page on GitHub and checked against the checksum listed there.",
  ];
}

export type SharingField =
  | "day_budget_gb"
  | "night_budget_gb"
  | "night_start"
  | "night_end"
  | "idle_unload_minutes";

/** What the owner has typed and not saved, field by field. A field not in
 *  here shows the server's value, so a change made elsewhere shows up. */
export type SharingDrafts = Partial<Record<SharingField, string>>;

export const SHARING_FIELDS: ReadonlyArray<SharingField> = [
  "day_budget_gb",
  "night_budget_gb",
  "night_start",
  "night_end",
  "idle_unload_minutes",
];

// The server's own sentences (api/v1/local_models.py), for what is not a
// number or a time at all. Anything that is goes to the server, which knows
// the Mac's memory and says the rest.
export const BAD_BUDGET = "Choose a budget between 1 and 512 GB.";
export const BAD_CLOCK = "Give the time as hours and minutes, like 21:30.";
export const BAD_IDLE = "Choose between 1 and 120 minutes.";

/** A setting as its field shows it: "8", "12.5", "21:30", "5". */
export function sharingValue(settings: LocalModelsSettings, field: SharingField): string {
  const value = settings[field];
  if (typeof value === "number") return String(Math.round(value * 10) / 10);
  return value;
}

function numberOf(text: string): number | null {
  const trimmed = text.trim().replace(",", ".");
  if (!trimmed) return null;
  const number = Number(trimmed);
  return Number.isFinite(number) ? number : null;
}

function clockOf(text: string): string | null {
  const found = /^\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*$/.exec(text);
  if (!found) return null;
  return `${(found[1] ?? "").padStart(2, "0")}:${found[2] ?? ""}`;
}

/**
 * What Save sends: only the fields that differ from the server's, so a
 * setting someone else changed meanwhile is not put back. `problem` is the
 * sentence for the first field that is not a number or a time at all;
 * nothing is sent then.
 */
export function sharingPatch(
  drafts: SharingDrafts,
  settings: LocalModelsSettings,
): { patch: LocalModelsSettingsPatch; changed: SharingField[]; problem: string | null } {
  const patch: LocalModelsSettingsPatch = {};
  const changed: SharingField[] = [];
  for (const field of SHARING_FIELDS) {
    const text = drafts[field];
    if (text === undefined) continue;
    if (field === "night_start" || field === "night_end") {
      const clock = clockOf(text);
      if (clock === null) return { patch: {}, changed: [], problem: BAD_CLOCK };
      if (clock !== settings[field]) {
        patch[field] = clock;
        changed.push(field);
      }
      continue;
    }
    const number = numberOf(text);
    if (number === null) {
      const problem = field === "idle_unload_minutes" ? BAD_IDLE : BAD_BUDGET;
      return { patch: {}, changed: [], problem };
    }
    if (number !== settings[field]) {
      patch[field] = number;
      changed.push(field);
    }
  }
  return { patch, changed, problem: null };
}

/** What the page says after a save. A budget or the window decides which
 *  models fit by day, so a change to one says where that shows. */
export function sharingSavedSentence(changed: SharingField[]): string {
  return changed.some((field) => field !== "idle_unload_minutes")
    ? "Saved. The models above show what fits by day and overnight now."
    : "Saved.";
}

// ---- Approved publishers, the token, Recent activity (ModelsPublishers, ModelsActivity)

export type PublisherList = "makers" | "converters" | "blocked";

/** What each list is called in a sentence, and one of its entries. */
const LIST_WORDS: Record<PublisherList, { plural: string; one: string }> = {
  makers: { plural: "makers", one: "a maker" },
  converters: { plural: "converters", one: "a converter" },
  blocked: { plural: "blocked list", one: "blocked" },
};

const ADDRESS = /^(?:https?:\/\/)?(?:www\.)?(?:huggingface\.co|hf\.co)\//i;

/**
 * A publisher's name as typed or pasted, the way the server reads it: a
 * huggingface.co address, a leading "@" and a trailing "/" come off. The
 * server is still the judge of whether it is a name at all; this is only so
 * the page can see that "google" is already on a list before asking.
 */
export function publisherName(text: string): string {
  let name = text.trim().replace(ADDRESS, "");
  if (name.startsWith("@")) name = name.slice(1);
  while (name.endsWith("/")) name = name.slice(0, -1);
  return name;
}

function same(a: string, b: string): boolean {
  return a.toLowerCase() === b.toLowerCase();
}

/** The names a list holds that the owner can change: for the blocked list
 *  only their own additions — the built-in names are not theirs to send. */
export function ownNames(publishers: LocalModelsPublishers, list: PublisherList): string[] {
  if (list === "makers") return [...publishers.makers];
  if (list === "converters") return [...publishers.converters];
  return publishers.blocked.filter((entry) => !entry.built_in).map((entry) => entry.name);
}

/** The request that stores `names` as the whole of `list`. */
export function listPatch(list: PublisherList, names: string[]): LocalModelsPublishersPatch {
  if (list === "makers") return { makers: names };
  if (list === "converters") return { converters: names };
  return { blocked_added: names };
}

/**
 * The whole of `list` to send for one change, built from `publishers` as the
 * server holds them now — not from what the page last drew. The server takes
 * each list it is given as the whole list, so a list built from a stale
 * overview undoes whatever another window changed meanwhile: a block added
 * there would be silently lifted by an add here.
 *
 * "added" puts the owner's `text` (as typed; the server reads it) at the end,
 * unless that name is there already; "removed" takes off every spelling of
 * `text`'s name.
 */
export function listAfter(
  publishers: LocalModelsPublishers,
  list: PublisherList,
  change: "added" | "removed",
  text: string,
): string[] {
  const own = ownNames(publishers, list);
  const name = publisherName(text);
  if (change === "removed") return own.filter((other) => !same(publisherName(other), name));
  if (own.some((other) => same(publisherName(other), name))) return own;
  return [...own, text.trim()];
}

/**
 * Why adding `text` to `list` would do nothing or be refused for a reason
 * the server words for a different case; null to send it. A publisher is
 * either a maker or a converter (the two are judged differently), and the
 * server's sentence for a converter added as a maker would wrongly say "is
 * already a maker". Whether it is a name at all, and the blocked rules, are
 * left to the server's own sentences.
 */
export function addProblem(
  publishers: LocalModelsPublishers,
  list: PublisherList,
  text: string,
): string | null {
  const name = publisherName(text);
  if (!name) return "Type a publisher's name, as Hugging Face spells it — like google.";
  const inList = (names: string[]) => names.some((other) => same(other, name));
  const blocked = publishers.blocked.map((entry) => entry.name);
  if (list === "blocked") {
    if (inList(blocked)) return `${name} is already blocked.`;
  } else if (inList(list === "makers" ? publishers.makers : publishers.converters)) {
    return `${name} is already ${LIST_WORDS[list].one}.`;
  }
  for (const other of ["makers", "converters"] as const) {
    if (other !== list && inList(publishers[other])) {
      const then = list === "blocked" ? "then block it" : `then add it as ${LIST_WORDS[list].one}`;
      return (
        `${name} is one of your ${LIST_WORDS[other].plural}. ` +
        `Take it off that list first (the × beside it), ${then}.`
      );
    }
  }
  return null;
}

/**
 * The installed models that stop running when `name` is no longer approved
 * (or is blocked): those still approved that are published by it, or that
 * say they were built from one of its models.
 */
export function dependents(name: string, models: LocalModel[]): LocalModel[] {
  return models.filter(
    (model) =>
      model.approved &&
      (same(model.publisher, name) ||
        (!!model.maker && same(model.maker, name)) ||
        model.built_from.some((entry) => !!entry.publisher && same(entry.publisher, name))),
  );
}

function roleTitles(models: LocalModel[], roles: LocalModelRole[]): string[] {
  const held = new Set(models.flatMap((model) => model.roles));
  return roles.filter((role) => held.has(role.role)).map((role) => role.title);
}

function andList(words: string[]): string {
  if (words.length <= 1) return words[0] ?? "";
  return `${words.slice(0, -1).join(", ")} and ${words[words.length - 1]}`;
}

function lossQuestion(
  name: string,
  affected: LocalModel[],
  roles: LocalModelRole[],
  until: string,
  ask: string,
): string | null {
  if (!affected.length) return null;
  const titles = roleTitles(affected, roles);
  const one = affected.length === 1;
  const who = one
    ? `${affected[0]?.name} is from ${name} or built from theirs. It stays in the library but will not run ${until}.`
    : `${affected.length} installed models are from ${name} or built from theirs. ` +
      `They stay in the library but will not run ${until}.`;
  let holding = "";
  if (titles.length && one) holding = ` It is the ${andList(titles)}.`;
  else if (titles.length === 1) holding = ` The ${titles[0]} is one of them.`;
  else if (titles.length) holding = ` The ${andList(titles)} are among them.`;
  return `${who}${holding} ${ask}`;
}

/** Asked before a maker or converter that installed models depend on is
 *  removed; null when none do (nothing to ask). */
export function removeListQuestion(
  name: string,
  models: LocalModel[],
  roles: LocalModelRole[],
): string | null {
  return lossQuestion(
    name,
    dependents(name, models),
    roles,
    `until ${name} is approved again`,
    `Remove ${name}?`,
  );
}

/** Asked before a publisher that installed models depend on is blocked. */
export function blockQuestion(name: string, models: LocalModel[], roles: LocalModelRole[]): string | null {
  return lossQuestion(name, dependents(name, models), roles, `while ${name} is blocked`, `Block ${name}?`);
}

/** What the page says when a list was saved: what changed, and which models
 *  the change left without approval (the server's own list of them). */
export function publishersSavedSentence(
  change: "added" | "removed",
  list: PublisherList,
  name: string,
  answer: LocalModelsPublishersSaved,
): string {
  const did =
    list === "blocked"
      ? change === "added"
        ? `${name} is blocked now.`
        : `${name} is off the blocked list.`
      : change === "added"
        ? `${name} added to the ${LIST_WORDS[list].plural}.`
        : `${name} removed from the ${LIST_WORDS[list].plural}.`;
  const lost = answer.no_longer_approved;
  if (!lost.length) return did;
  const names = lost.map((entry) => nameOf(entry.repo_id));
  const one = lost.length === 1;
  const which = `${andList(names)} ${one ? "is" : "are"} no longer approved.`;
  const kept = lost.some((entry) => entry.roles.length)
    ? one
      ? " It keeps its role, but will not run until it is approved again."
      : " Those with a role keep it, but will not run until they are approved again."
    : "";
  return `${did} ${which}${kept}`;
}

export const TOKEN_STORED = "A token is stored.";
export const TOKEN_NONE = "None stored.";

/** The token's state, beside its field: never the token itself, which the
 *  page is never sent. */
export function tokenState(settings: LocalModelsSettings): { text: string; tone: "ok" | "bad" | null } {
  if (settings.hf_token_problem) return { text: settings.hf_token_problem, tone: "bad" };
  if (settings.hf_token_configured) return { text: TOKEN_STORED, tone: "ok" };
  return { text: TOKEN_NONE, tone: null };
}

export const TOKEN_REMOVE_QUESTION =
  "Remove the stored Hugging Face token? Models already downloaded keep working; " +
  "checking or downloading a gated model needs a token again.";

export function tokenSavedSentence(token: string): string {
  return token
    ? "Saved. A token is stored; it is only ever sent to Hugging Face."
    : "The token is removed. None stored.";
}

/** The kinds of work this page shows in their own place — a model's row, a
 *  role card, the software row, a Test — where they can also be cancelled.
 *  Work of any other kind (a later update's) is cancelled from Recent
 *  activity, so no job on this lane is ever out of reach. */
const SHOWN_ELSEWHERE: ReadonlySet<string> = new Set(["download", "place", "runtime_install", "test"]);

export function cancelInActivity(job: LocalModelJob): boolean {
  return isActiveJob(job) && !SHOWN_ELSEWHERE.has(job.kind);
}

/** How a job in Recent activity ended (or that it has not yet). */
export function activityState(job: LocalModelJob): {
  label: string;
  tone: "ok" | "warn" | "bad" | null;
  sentence: string;
} {
  switch (job.state) {
    case "done":
      return { label: "done", tone: "ok", sentence: job.message };
    case "failed":
      return { label: "failed", tone: "bad", sentence: job.message || "It failed." };
    case "cancelled":
      return { label: "cancelled", tone: null, sentence: job.message };
    case "running":
      return { label: job.cancelling ? "stopping" : "under way", tone: "warn", sentence: jobSentence(job) };
    case "waiting":
      return { label: "waiting", tone: "warn", sentence: jobSentence(job) };
    default:
      return { label: "queued", tone: null, sentence: jobSentence(job) };
  }
}

/** The moment Recent activity dates a job by: when it ended, else when it
 *  started, else when it was asked for. */
export function activityAt(job: LocalModelJob): string | null {
  return job.finished_at ?? job.started_at ?? job.created_at;
}
