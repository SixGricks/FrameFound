// The chat panel's state and everything that changes it.
//
// The panel is one thing for the whole app, while pages (and the Shell
// round them) are thrown away on every navigation: a conversation kept in
// a component would be lost, and its answer cut off, by clicking a menu
// item. So the state lives outside React, in the store this file makes;
// the panel only draws it. A turn that is running keeps being read into
// the store whichever page is showing, and with the panel closed.
//
// What the store promises:
//
// - One conversation is open at a time. Its `view` is what to draw (see
//   chatReducer.ts); everything else in the snapshot is the panel's own
//   (is it open, what is typed, is it still connected).
// - Closing a stream never stops a turn, so losing one is not the end of
//   it: the store picks the turn up again from the event it had reached,
//   and falls back to asking for the stored conversation.
// - Work that was overtaken — an answer for a conversation since left, a
//   retry for a turn since finished — changes nothing. Every piece of
//   asynchronous work carries the ticket it started under and stops when
//   the ticket has moved on.
// - A failure is reported with where it happened (`problem.where`), so the
//   panel can show it beside the control that was used.
//
// Everything outside (the API, the clock, the tab's memory, the page
// context) comes in as `ChatEffects`, so the whole of this is tested with
// `npm test` and no browser (tests/chatSession.test.mjs). The one that the
// app uses is made in chatStore.ts.

import type {
  ChatContext,
  ChatConversation,
  ChatDecision,
  ChatEvent,
  ChatStatus,
  ChatSummary,
} from "./api";
import type { ChatView } from "./chatReducer";
import { emptyView, expecting, reduce, viewOf, withSent, withoutSent } from "./chatReducer";

type OnEvent = (event: ChatEvent) => void;

/** The outside world, as the store uses it. The three streams resolve and
 *  reject as `eventStream` in api.ts describes; a rejection that carries a
 *  numeric `status` is the server saying no, and its message is shown. */
export interface ChatEffects {
  status(): Promise<ChatStatus>;
  conversations(): Promise<ChatSummary[]>;
  create(pictures: boolean): Promise<ChatConversation>;
  read(id: string): Promise<ChatConversation>;
  setPictures(id: string, pictures: boolean): Promise<ChatSummary>;
  remove(id: string): Promise<void>;
  stop(id: string): Promise<ChatSummary>;
  send(
    id: string,
    body: { text: string; context: ChatContext | null },
    onEvent: OnEvent,
    signal: AbortSignal,
  ): Promise<number>;
  answer(id: string, decisions: ChatDecision[], onEvent: OnEvent, signal: AbortSignal): Promise<number>;
  follow(id: string, after: number, onEvent: OnEvent, signal: AbortSignal): Promise<number>;
  /** What is on screen right now, to go with a message. */
  context(): ChatContext | null;
  wait(ms: number): Promise<void>;
  /** A few words kept for this tab, so a reload comes back to the same
   *  conversation. `remember(null)` forgets. */
  recall(): string | null;
  remember(value: string | null): void;
}

/** Where a failure happened, so it can be shown where the action was taken. */
export interface ChatProblem {
  where: "status" | "list" | "open" | "send" | "confirm" | "stop" | "delete" | "pictures";
  message: string;
}

export interface ChatSnapshot {
  /** Whose conversations these are; null when nobody is signed in. */
  user: string | null;
  /** The panel is showing. */
  open: boolean;
  /** What is typed and not sent. Kept while the panel is closed. */
  draft: string;
  /** Whether chat can be used; null until asked. */
  status: ChatStatus | null;
  /** Earlier conversations, newest first; null until asked. */
  conversations: ChatSummary[] | null;
  /** The open conversation, as drawn. `view.id` null = a new one. */
  view: ChatView;
  /** The open conversation is being fetched. */
  opening: boolean;
  /**
   * How the running turn is being heard:
   * - `live`: its events are arriving (or the request has just been made);
   * - `reconnecting`: the connection dropped and is being made again —
   *   the turn itself carries on meanwhile;
   * - `watching`: not listening; asking now and then whether the turn is
   *   over (a turn the server lost, or one a Stop could not reach);
   * - `idle`: no turn is running.
   */
  link: "idle" | "live" | "reconnecting" | "watching";
  /** Stop was pressed and the turn has not ended yet. */
  stopping: boolean;
  /** False once the server has turned out not to have a Stop. */
  canStop: boolean;
  problem: ChatProblem | null;
}

export interface ChatStore {
  /** For `useSyncExternalStore`: the same object until something changes. */
  get(): ChatSnapshot;
  subscribe(listener: () => void): () => void;
  /**
   * Say who is signed in. The first call for a person reopens the
   * conversation this tab had open and, if the panel was left open, loads
   * the status and the list (a closed panel asks nothing of them until it
   * is opened); a call for the same person again does nothing. Must be called before anything else
   * does anything: with nobody signed in every action is a no-op, so the
   * sign-in page never makes a request that would bounce it to itself.
   */
  signIn(user: string): void;
  /** Forget everything, here and in the tab's memory: the next person to
   *  sign in on this tab must not find the last one's conversation. */
  signOut(): void;
  setOpen(open: boolean): void;
  setDraft(text: string): void;
  refreshStatus(): Promise<void>;
  refreshList(): Promise<void>;
  /** Open an earlier conversation, and follow its turn if one is running. */
  openConversation(id: string): Promise<void>;
  /** Start afresh. Nothing is created on the server until a message is sent. */
  newConversation(): void;
  /**
   * Send a message and read the turn it starts. `context` undefined sends
   * what is on screen now; null sends none. Resolves true when the server
   * took the message, false when it did not (the text is put back in the
   * draft and `problem.where` is "send").
   */
  send(text: string, options?: { context?: ChatContext | null }): Promise<boolean>;
  /** Answer calls that are waiting for a yes or no. */
  confirm(decisions: ChatDecision[]): Promise<boolean>;
  /** Stop the running turn. The turn's own `done` is what ends it here. */
  stop(): Promise<void>;
  /** Delete a conversation. The panel asks first; this does not. */
  remove(id: string): Promise<boolean>;
  /** The pictures switch of the open conversation. */
  setPictures(on: boolean): Promise<void>;
  dismissProblem(): void;
}

/** Waits between attempts to pick a turn up again, the last one repeating. */
export const RETRY_MS = [1000, 2000, 4000, 8000, 15000];
/** How often a turn that cannot be followed is asked after. */
export const WATCH_MS = 4000;
/** How long after a Stop the turn's own ending is waited for before asking. */
export const STOP_GRACE_MS = 3000;

export const UNREACHED = "FrameFound could not be reached. Check the connection and try again.";
export const NO_STOP =
  "Stopping is not available on this server yet. Claude is still working; " +
  "its answer will appear here when it has finished.";
/** What every chat route answers for a conversation that is not the caller's. */
const NO_SUCH = "No such conversation";

type Opener = (onEvent: OnEvent, signal: AbortSignal) => Promise<number>;
type Heard =
  | { kind: "done" | "dropped" | "nothing" | "aborted" | "unreached"; heard: boolean }
  | { kind: "refused"; heard: false; status: number; message: string };
interface Refusal {
  /** 0 when the server was not reached at all. */
  status: number;
  message: string;
}

function initial(): ChatSnapshot {
  return {
    user: null,
    open: false,
    draft: "",
    status: null,
    conversations: null,
    view: emptyView(),
    opening: false,
    link: "idle",
    stopping: false,
    canStop: true,
    problem: null,
  };
}

function statusOf(err: unknown): number {
  const status = (err as { status?: unknown } | null)?.status;
  return typeof status === "number" ? status : 0;
}

/** The server's sentence when it gave one; ours when it was never reached
 *  (a browser's "Failed to fetch" tells the reader nothing). */
function sentence(err: unknown, fallback = UNREACHED): string {
  return statusOf(err) > 0 && err instanceof Error && err.message ? err.message : fallback;
}

function summaryOf(stored: ChatConversation): ChatSummary {
  return {
    id: stored.id,
    title: stored.title,
    model: stored.model,
    pictures: stored.pictures,
    state: stored.state,
    spent_usd: stored.spent_usd,
    created_at: stored.created_at,
    updated_at: stored.updated_at,
  };
}

function lastSeq(view: ChatView): number {
  return view.messages.reduce((most, message) => Math.max(most, message.seq ?? 0), 0);
}

export function createChatStore(fx: ChatEffects): ChatStore {
  let state = initial();
  const listeners = new Set<() => void>();
  // Moves on whenever the open conversation changes or a new turn begins.
  let ticket = 0;
  let listening: AbortController | null = null;
  let remembered = "";
  // The pictures switch: changes go to the server one at a time (see
  // setPictures). `pictures.saved` is what the server last said for
  // `pictures.id`, which is what the switch goes back to if a change fails.
  let pictures: { id: string | null; saved: boolean } = { id: null, saved: true };
  let picturesAsked = 0;
  let picturesBusy = 0;
  let picturesLine: Promise<void> = Promise.resolve();

  const stale = (mine: number): boolean => mine !== ticket;

  function set(patch: Partial<ChatSnapshot>): void {
    state = { ...state, ...patch };
    keep();
    for (const listener of [...listeners]) listener();
  }

  /** Which conversation was open, and the unsent text, for after a reload. */
  function keep(): void {
    if (state.user === null) return;
    const value = JSON.stringify({
      user: state.user,
      open: state.open,
      conversation: state.view.id,
      draft: state.draft,
    });
    if (value === remembered) return;
    remembered = value;
    try {
      fx.remember(value);
    } catch {
      // A full or forbidden store: the panel works without its memory.
    }
  }

  /** Stop listening to whatever was being followed and take a new ticket. */
  function claim(): number {
    listening?.abort();
    listening = null;
    ticket += 1;
    return ticket;
  }

  /** Report a background failure without talking over a problem the
   *  reader is still looking at from something they did. */
  function mention(where: ChatProblem["where"], message: string): void {
    if (state.problem === null || state.problem.where === where) {
      set({ problem: { where, message } });
    }
  }

  function settle(where: ChatProblem["where"]): ChatProblem | null {
    return state.problem?.where === where ? null : state.problem;
  }

  function listed(summary: ChatSummary, first = false): ChatSummary[] | null {
    const all = state.conversations;
    if (all === null) return null;
    if (all.some((one) => one.id === summary.id)) {
      return all.map((one) => (one.id === summary.id ? summary : one));
    }
    return first ? [summary, ...all] : all;
  }

  // --- hearing a turn --------------------------------------------------------

  function receive(event: ChatEvent): void {
    const patch: Partial<ChatSnapshot> = { view: reduce(state.view, event), link: "live" };
    if (event.event === "usage" && state.status !== null) {
      // The month's total is everyone's and every feature's; the turn has
      // just said what it now is.
      const { month_usd: month, budget_usd: budget } = event.data;
      patch.status = {
        ...state.status,
        spent_this_month_usd: typeof month === "number" ? month : state.status.spent_this_month_usd,
        budget_usd: typeof budget === "number" ? budget : state.status.budget_usd,
      };
    }
    if (event.event === "done") {
      patch.link = "idle";
      patch.stopping = false;
    }
    set(patch);
  }

  /**
   * Read one stream to its end and say how it ended.
   *
   * `held`: the request carries something the reader typed (a message), and
   * until the server has said a first word about it, nothing the reader
   * does here may cut it off. Leaving for another conversation used to
   * abort it there, and a message that had not yet reached the server was
   * then neither sent nor anywhere on screen. A held request is let go only
   * once it has been heard from; if by then this view has been left, it is
   * closed at that point (closing a stream never stops a turn).
   */
  async function listen(mine: number, open: Opener, held = false): Promise<Heard> {
    const controller = new AbortController();
    if (!held) listening = controller;
    let heard = false;
    let done = false;
    try {
      const status = await open((event) => {
        if (done) return;
        if (stale(mine)) {
          controller.abort();
          return;
        }
        if (!heard && held) listening = controller;
        heard = true;
        receive(event);
        if (event.event === "done") {
          done = true;
          // Nothing comes after `done`. Not waiting for the connection to
          // close means a proxy that holds it open cannot hold the panel.
          controller.abort();
        }
      }, controller.signal);
      if (done) return { kind: "done", heard };
      return { kind: status === 204 ? "nothing" : "dropped", heard };
    } catch (err) {
      if (done) return { kind: "done", heard };
      if (controller.signal.aborted) return { kind: "aborted", heard };
      const status = statusOf(err);
      if (status > 0) return { kind: "refused", heard: false, status, message: sentence(err) };
      return { kind: "unreached", heard };
    } finally {
      if (listening === controller) listening = null;
    }
  }

  /**
   * Hear a turn to its end: first through `first` (the request that starts
   * it; null to join one already running), then, for as long as it takes,
   * by asking for its events from the one after the last this view has.
   *
   * Returns the server's refusal of `first`, when it refused; null once the
   * turn has ended, been handed to another way of following, or been left.
   */
  async function follow(mine: number, first: Opener | null, held = false): Promise<Refusal | null> {
    let open = first;
    let misses = 0;
    for (;;) {
      const id = state.view.id;
      if (id === null) return null;
      const after = state.view.events;
      if (open === null && after > 0) {
        // Part-way through a turn. The server answers `events?after=` with
        // whichever turn it holds NOW, and while this tab was away another
        // tab or a phone may have started a new one: its events from `after`
        // on, spliced into this half-drawn answer, would be a transcript of
        // nothing that happened. Only pick up the turn this view is drawing.
        const same = await sameTurn(mine, id);
        if (same === "left") return null;
        if (same === "unknown") {
          misses += 1;
          set({ link: "reconnecting" });
          await fx.wait(RETRY_MS[Math.min(misses, RETRY_MS.length - 1)] ?? 15000);
          if (stale(mine)) return null;
          continue;
        }
      }
      const rest: Opener = (onEvent, signal) => fx.follow(id, after, onEvent, signal);
      const heard = await listen(mine, open ?? rest, open !== null && held);
      if (stale(mine) || heard.kind === "aborted") return null;
      if (heard.kind === "done") {
        void refreshList();
        // A stopped answer is kept as far as it got, and only the server
        // knows exactly what that is (a call Claude was still writing is
        // not kept; a tool that was allowed to finish is). Draw what it
        // stored, so the screen and a reload say the same thing.
        if (state.view.ended?.reason === "stopped") await reread(mine);
        return null;
      }
      if (open !== null) {
        if (heard.kind === "refused") return { status: heard.status, message: heard.message };
        if (heard.kind === "unreached") return { status: 0, message: UNREACHED };
      }
      // The server holds no turn for this conversation any more (it ended
      // long ago, or went with a restart), or the conversation is gone:
      // what is stored is all there is.
      if (heard.kind === "nothing" || (heard.kind === "refused" && heard.status === 404)) {
        await reread(mine);
        return null;
      }
      // Dropped, unreachable, or a server that is restarting. The turn
      // does not need us; keep trying to hear the rest of it.
      open = null;
      misses = heard.heard ? 0 : misses + 1;
      set({ link: "reconnecting" });
      await fx.wait(RETRY_MS[Math.min(misses, RETRY_MS.length - 1)] ?? 15000);
      if (stale(mine)) return null;
    }
  }

  /**
   * Ask now and then whether a turn that cannot be followed has ended.
   * `detached` = the reader asked to stop listening (the server has no
   * Stop): what is on screen stays as it is until the turn is over, even
   * though the server could still replay it.
   */
  async function watch(mine: number, detached: boolean): Promise<void> {
    set({ link: "watching" });
    for (;;) {
      await fx.wait(WATCH_MS);
      if (stale(mine)) return;
      const id = state.view.id;
      if (id === null) return;
      let stored: ChatConversation;
      try {
        stored = await fx.read(id);
      } catch (err) {
        if (stale(mine)) return;
        if (statusOf(err) === 404) {
          gone(id, sentence(err));
          return;
        }
        continue;
      }
      if (stale(mine)) return;
      if (stored.state === "running" && (detached || !stored.run?.active)) continue;
      adopt(mine, stored);
      if (stored.state !== "running") void refreshList();
      return;
    }
  }

  /** Draw a stored conversation, and follow its turn if it has one. */
  function adopt(mine: number, stored: ChatConversation): void {
    const view = viewOf(stored);
    set({
      view,
      opening: false,
      stopping: false,
      link: !view.running ? "idle" : view.stranded ? "watching" : "live",
      conversations: listed(summaryOf(stored)) ?? state.conversations,
    });
    if (view.stranded) void watch(mine, false);
    else if (view.running) void follow(mine, null);
  }

  /** Replace what is drawn with what the server has stored. False when it
   *  could not be read. */
  async function reread(mine: number): Promise<boolean> {
    const id = state.view.id;
    if (id === null) return false;
    let stored: ChatConversation;
    try {
      stored = await fx.read(id);
    } catch (err) {
      if (stale(mine)) return false;
      if (statusOf(err) === 404) gone(id, sentence(err));
      // Not knowing must not look like "finished": if a turn was running,
      // keep asking until the server answers.
      else if (state.view.running) void watch(mine, false);
      return false;
    }
    if (stale(mine)) return false;
    adopt(mine, stored);
    return true;
  }

  /**
   * Whether the turn the server holds for a conversation is the one this
   * view was drawing (`view.turn`, from its `start`). "yes": go on from the
   * event reached. "left": it is not — what is stored has been drawn
   * instead, and its own turn followed from the start if one runs — or the
   * view has been left. "unknown": the server could not be asked.
   */
  async function sameTurn(mine: number, id: string): Promise<"yes" | "left" | "unknown"> {
    let stored: ChatConversation;
    try {
      stored = await fx.read(id);
    } catch (err) {
      if (stale(mine)) return "left";
      if (statusOf(err) === 404) {
        gone(id, sentence(err));
        return "left";
      }
      return "unknown";
    }
    if (stale(mine)) return "left";
    const turn = state.view.turn;
    if (turn !== null && stored.run !== null && stored.run.seq === turn) return "yes";
    adopt(mine, stored);
    return "left";
  }

  /** The open conversation does not exist any more. */
  function gone(id: string, message: string): void {
    claim();
    set({
      view: emptyView(state.view.pictures),
      opening: false,
      stopping: false,
      link: "idle",
      conversations: state.conversations?.filter((one) => one.id !== id) ?? null,
      problem: { where: "open", message },
    });
  }

  // --- what the panel calls ----------------------------------------------------

  async function refreshStatus(): Promise<void> {
    const who = state.user;
    if (who === null) return;
    try {
      const status = await fx.status();
      if (state.user === who) set({ status, problem: settle("status") });
    } catch (err) {
      if (state.user === who) mention("status", sentence(err));
    }
  }

  async function refreshList(): Promise<void> {
    const who = state.user;
    if (who === null) return;
    try {
      const conversations = await fx.conversations();
      if (state.user === who) set({ conversations, problem: settle("list") });
    } catch (err) {
      if (state.user === who) mention("list", sentence(err));
    }
  }

  async function openConversation(id: string): Promise<void> {
    if (state.user === null) return;
    // Already open and being heard: reopening would only cut it off and
    // read it again.
    if (id === state.view.id && state.view.running && state.link === "live") return;
    const mine = claim();
    const known = state.conversations?.find((one) => one.id === id);
    set({
      view: {
        ...emptyView(known?.pictures ?? true),
        id,
        title: known?.title ?? "",
        model: known?.model ?? "",
      },
      opening: true,
      stopping: false,
      link: "idle",
      problem: null,
    });
    let stored: ChatConversation;
    try {
      stored = await fx.read(id);
    } catch (err) {
      if (stale(mine)) return;
      if (statusOf(err) === 404) {
        gone(id, sentence(err));
        return;
      }
      set({ view: emptyView(), opening: false, problem: { where: "open", message: sentence(err) } });
      return;
    }
    if (stale(mine)) return;
    adopt(mine, stored);
  }

  function newConversation(): void {
    if (state.user === null) return;
    claim();
    set({
      // The pictures choice carries over: someone who switched pictures
      // off does not expect a new conversation to switch them back on.
      view: emptyView(state.view.pictures),
      opening: false,
      stopping: false,
      link: "idle",
      problem: null,
    });
  }

  /** Undo an optimistic send: the message comes off the screen and goes
   *  back in the box, unless something else has been typed there since. */
  function takeBack(typed: string, message: string): void {
    set({
      view: withoutSent(state.view),
      draft: state.draft === "" ? typed : state.draft,
      link: "idle",
      problem: { where: "send", message },
    });
  }

  /**
   * A message that was on its way when the reader went to another
   * conversation, and that the server never took: its words go back in the
   * box (if the box is empty) rather than vanish with the view they were
   * drawn in.
   */
  function handBack(who: string, typed: string, why: string): void {
    if (state.user !== who || state.draft !== "") return;
    set({
      draft: typed,
      problem: state.problem ?? {
        where: "send",
        message: `Your last message was not sent (${why.replace(/\.$/, "")}). It is back in the box.`,
      },
    });
  }

  async function send(text: string, options: { context?: ChatContext | null } = {}): Promise<boolean> {
    const typed = text.trim();
    const who = state.user;
    if (who === null || typed === "" || state.view.running || state.opening) return false;
    const mine = claim();
    const context = options.context === undefined ? fx.context() : options.context;
    const before = lastSeq(state.view);
    set({
      view: withSent(state.view, typed, context),
      draft: state.draft.trim() === typed ? "" : state.draft,
      stopping: false,
      link: "live",
      problem: null,
    });

    let id = state.view.id;
    if (id === null) {
      let made: ChatConversation;
      try {
        made = await fx.create(state.view.pictures);
      } catch (err) {
        if (!stale(mine)) takeBack(typed, sentence(err));
        else handBack(who, typed, sentence(err));
        return false;
      }
      if (stale(mine)) {
        // Left while the conversation was being made: it exists now (empty),
        // so list it, and the message, never sent, goes back in the box.
        if (state.user === who) {
          set({ conversations: listed(summaryOf(made), true) });
          handBack(who, typed, "you left that conversation before it was sent");
        }
        return false;
      }
      id = made.id;
      set({
        view: { ...state.view, id, model: made.model, pictures: made.pictures },
        conversations: listed(summaryOf(made), true),
      });
    }

    const conversation = id;
    // Whether the server said `start` for it: then it has the message,
    // whatever becomes of this view.
    let took = false;
    let refused: string | null = null;
    const refusal = await follow(
      mine,
      (onEvent, signal) =>
        fx
          .send(
            conversation,
            { text: typed, context },
            (event) => {
              if (event.event === "start") took = true;
              onEvent(event);
            },
            signal,
          )
          .catch((err: unknown) => {
            refused = sentence(err);
            throw err;
          }),
      true,
    );
    if (stale(mine)) {
      if (!took) handBack(who, typed, refused ?? UNREACHED);
      return took;
    }
    if (refusal === null) return true;
    takeBack(typed, refusal.message);
    // 409: a turn is running there after all (another tab, or one this tab
    // lost track of) — go and follow it. 404: it was deleted elsewhere.
    // 0: no answer came back, which is not the same as the message not
    // arriving — look, rather than let it be sent twice.
    if (refusal.status === 409 || refusal.status === 404 || refusal.status === 0) {
      const read = await reread(mine);
      const arrived =
        read && state.view.messages.some((one) => one.role === "user" && (one.seq ?? 0) > before);
      if (refusal.status === 0 && arrived) {
        set({ draft: state.draft === typed ? "" : state.draft, problem: settle("send") });
        return true;
      }
    }
    return false;
  }

  async function confirm(decisions: ChatDecision[]): Promise<boolean> {
    const id = state.view.id;
    if (state.user === null || id === null || decisions.length === 0) return false;
    if (state.view.running || state.opening) return false;
    const mine = claim();
    const before = state.view;
    set({ view: expecting(before), stopping: false, link: "live", problem: null });
    const refusal = await follow(mine, (onEvent, signal) => fx.answer(id, decisions, onEvent, signal));
    if (refusal === null) return !stale(mine);
    if (stale(mine)) return false;
    set({ view: before, link: "idle", problem: { where: "confirm", message: refusal.message } });
    // "No longer waiting", "still working", or no answer at all: what is
    // drawn as waiting may not be what is waiting. Read-only (403) and
    // not-installed (503) change nothing worth re-reading.
    if (refusal.status !== 403 && refusal.status !== 503) await reread(mine);
    return false;
  }

  async function stop(): Promise<void> {
    const id = state.view.id;
    if (id === null || !state.view.running || state.view.stranded) return;
    if (state.stopping || state.link === "watching") return;
    // Not before the turn has said `start`. Until then there is nothing on
    // the server to stop: the route answers "nothing running", the turn
    // starts a moment later and runs to the end at full price, and the
    // re-read that followed cut the very request carrying the message.
    if (!state.view.begun) return;
    const mine = ticket;
    set({ stopping: true, problem: null });
    let after: ChatSummary;
    try {
      after = await fx.stop(id);
    } catch (err) {
      if (stale(mine)) return;
      const status = statusOf(err);
      const said = sentence(err);
      if (status === 404 && said === NO_SUCH) {
        gone(id, said);
        return;
      }
      if (status === 404 || status === 405) {
        // A server from before Stop existed. Closing the stream stops
        // nothing, and to go on showing an answer the reader tried to
        // stop would be worse: stop listening, say so, and show what was
        // stored once the turn is over.
        set({ stopping: false, canStop: false, problem: { where: "stop", message: NO_STOP } });
        if (state.view.running) void watch(claim(), true);
        return;
      }
      set({ stopping: false, problem: { where: "stop", message: said } });
      return;
    }
    // The turn says `done` (reason: stopped) down the stream being read,
    // and that is what ends it here. A tool that had already started is
    // left to finish (up to three minutes), so the server can answer that
    // the turn is still running: then the stream is still the way to hear
    // the end, and "Stopping…" stays up until it comes. Asking for the
    // stored conversation now would cut that stream and draw the turn
    // afresh as if nobody had pressed Stop.
    if (after.state === "running") return;
    // It has ended. If its `done` does not arrive — the stream had already
    // been lost — ask what became of the turn.
    await fx.wait(STOP_GRACE_MS);
    if (stale(mine) || !state.view.running) return;
    await reread(claim());
  }

  async function remove(id: string): Promise<boolean> {
    if (state.user === null) return false;
    try {
      await fx.remove(id);
    } catch (err) {
      // Already gone is what was asked for.
      if (statusOf(err) !== 404) {
        set({ problem: { where: "delete", message: sentence(err) } });
        return false;
      }
    }
    const conversations = state.conversations?.filter((one) => one.id !== id) ?? null;
    if (state.view.id !== id) {
      set({ conversations, problem: settle("delete") });
      return true;
    }
    claim();
    set({
      conversations,
      view: emptyView(state.view.pictures),
      opening: false,
      stopping: false,
      link: "idle",
      problem: null,
    });
    return true;
  }

  async function setPictures(on: boolean): Promise<void> {
    const who = state.user;
    if (who === null) return;
    const id = state.view.id;
    // With nothing on its way, the switch shows what the server has.
    if (id !== null && (picturesBusy === 0 || pictures.id !== id)) {
      pictures = { id, saved: state.view.pictures };
    }
    set({ view: { ...state.view, pictures: on }, problem: settle("pictures") });
    // A conversation not yet created takes the choice with it when it is.
    if (id === null) return;
    // One change at a time, the latest wanted value last. Two quick clicks
    // used to send two requests at once; the server could keep the first
    // while the second's answer arrived last, and the switch then said
    // pictures were not sent while they were. This switch's whole job is to
    // say truly what leaves the building.
    const mine = ++picturesAsked;
    picturesBusy += 1;
    const turn = picturesLine.then(async () => {
      // Overtaken before it was sent: the later change says what is wanted.
      if (mine !== picturesAsked) return;
      let summary: ChatSummary;
      try {
        summary = await fx.setPictures(id, on);
      } catch (err) {
        if (mine !== picturesAsked || state.user !== who) return;
        const patch: Partial<ChatSnapshot> = { problem: { where: "pictures", message: sentence(err) } };
        // The switch must show what is true: what the server last kept.
        if (state.view.id === id && pictures.id === id) patch.view = { ...state.view, pictures: pictures.saved };
        set(patch);
        return;
      }
      if (state.user !== who) return;
      if (pictures.id === id) pictures = { id, saved: summary.pictures };
      const patch: Partial<ChatSnapshot> = { conversations: listed(summary) ?? state.conversations };
      if (state.view.id === id && mine === picturesAsked) {
        patch.view = { ...state.view, pictures: summary.pictures };
      }
      set(patch);
    });
    picturesLine = turn.catch(() => undefined).finally(() => {
      picturesBusy -= 1;
    });
    await turn;
  }

  function signIn(user: string): void {
    if (state.user === user) return;
    claim();
    let kept: { user?: unknown; open?: unknown; conversation?: unknown; draft?: unknown } | null = null;
    try {
      const parsed: unknown = JSON.parse(fx.recall() ?? "null");
      if (parsed !== null && typeof parsed === "object") kept = parsed;
    } catch {
      // Unreadable: start with nothing remembered.
    }
    // Only this person's: a tab is not always used by one person.
    if (kept?.user !== user) kept = null;
    remembered = "";
    state = {
      ...initial(),
      user,
      open: kept?.open === true,
      draft: typeof kept?.draft === "string" ? kept.draft : "",
    };
    set({});
    // Only with the panel showing. Every page load signs in, and asking
    // whether chat can be used is not free on the server (the first time,
    // it loads the Claude libraries): an install where nobody has opened
    // the panel must not pay for it on every tab. Opening the panel asks.
    if (state.open) {
      void refreshStatus();
      void refreshList();
    }
    // A reload in the middle of a turn rejoins it, open or closed.
    if (typeof kept?.conversation === "string") void openConversation(kept.conversation);
  }

  function signOut(): void {
    if (state.user === null) return;
    claim();
    remembered = "";
    try {
      fx.remember(null);
    } catch {
      // Nothing to forget, then.
    }
    state = initial();
    for (const listener of [...listeners]) listener();
  }

  return {
    get: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    signIn,
    signOut,
    setOpen(open) {
      if (open === state.open) return;
      set({ open });
      // Opening is when it matters whether a key has been added, or a
      // conversation started on another device, since last time.
      if (open) {
        void refreshStatus();
        void refreshList();
      }
    },
    setDraft(text) {
      if (text !== state.draft) set({ draft: text });
    },
    refreshStatus,
    refreshList,
    openConversation,
    newConversation,
    send,
    confirm,
    stop,
    remove,
    setPictures,
    dismissProblem() {
      if (state.problem !== null) set({ problem: null });
    },
  };
}
