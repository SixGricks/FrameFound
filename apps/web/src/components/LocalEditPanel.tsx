"use client";

// The local model on a listing: asking it about the photographs (in the
// rail, under Auto-edit), and choosing what of its suggestions to apply (a
// list above the gallery).
//
// Asking changes nothing. The model proposes, for each photograph, how to
// straighten it, whether the sky is worth replacing, and a caption and file
// name; they wait here until the operator ticks the ones to use and presses
// Apply, which is an ordinary auto-edit run of mode "local" — the page's own
// run handling ("Editing 12/34…", "✓ edited" on the tiles) takes over from
// there. A large correction starts unticked: it is the one to look at first.
//
// The way to the photograph is kept: each row's "As shot" picture opens the
// page's viewer (LookFrame, as the gallery's tiles do) and the tick is a
// separate control, so looking never ticks or unticks anything. Colour is
// judged by eye: nothing here shows a difference, a score or a rank.
//
// What is shown is the server's, read again while the model is at work: a
// request outlives the page, so leaving and coming back shows it as it is.
// The page reads once (`useLocalEdit`) and hands the same reader to the
// rail's block and the list, so both always show the same state.

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type ChangeEvent,
} from "react";

import LookFrame from "@/components/LookFrame";
import {
  api,
  localEditPreviewUrl,
  type ClipLookJob,
  type LocalApplyOptions,
  type LocalEdit,
  type LocalEditSuggestion,
} from "@/lib/api";
import {
  APPLY_WAITS_FOR_ASK,
  APPLY_WAITS_FOR_RUN,
  ASK_EXPLAINED,
  ASK_OR_LINE,
  ASK_STOPPED_LINE,
  LISTING_DEMO_LINE,
  NO_TICKS,
  PANEL_TITLE,
  SKY_WHERE,
  TICK_ONE,
  applyLabel,
  askEnded,
  askFocus,
  askHoldsList,
  askLabel,
  askedLine,
  dismissConfirm,
  dismissedLine,
  dismissedOf,
  isActive,
  jobLine,
  localEditLine,
  madeAtShown,
  mergeTicks,
  missingLine,
  panelShown,
  proposalPicture,
  readAgainIn,
  skyChoiceLine,
  skyText,
  straightenText,
  tickRange,
  waitingSuggestions,
  type AskFocusSeen,
  type Ticks,
} from "@/lib/clipLooks";
import { focusIfLost, rereads, type Rereader } from "@/lib/localModels";

// ---- reading ------------------------------------------------------------------------

export interface LocalEditReader {
  /** The last answer; null until the first one. */
  edit: LocalEdit | null;
  loadError: string | null;
  /** Read again at once (after a press, or when an Apply run ends). */
  readNow: () => void;
  /** Draw an answer that is the whole state (Dismiss answers with one). */
  show: (edit: LocalEdit) => void;
  /** Draw the request a press started or stopped, before the next read. */
  withJob: (job: ClipLookJob) => void;
  /** The list's ticks (listing item ids). */
  ticks: Ticks;
  setTicked: (ticked: string[]) => void;
  /** The last thing said about asking (asked, stopped, finished) or about
   *  the list (dismissed): said in the rail, which stays when the list goes. */
  said: string | null;
  say: (sentence: string | null) => void;
}

/** One reader of a listing's local-model suggestions, for the page to hand to
 *  the rail's block, the list and the viewer. */
export function useLocalEdit(listingId: string): LocalEditReader {
  const [edit, setEdit] = useState<LocalEdit | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [ticks, setTicks] = useState<Ticks>(NO_TICKS);
  const [said, say] = useState<string | null>(null);
  const reader = useRef<Rereader<LocalEdit> | null>(null);
  // The last answer drawn, so a request that ends while the page watches is
  // announced once.
  const previous = useRef<LocalEdit | null>(null);

  const draw = useCallback((next: LocalEdit) => {
    const ended = askEnded(previous.current?.job, next.job);
    previous.current = next;
    setEdit(next);
    setTicks((current) => mergeTicks(current, next.suggestions));
    if (ended) say(ended);
  }, []);

  useEffect(() => {
    previous.current = null;
    setEdit(null);
    setTicks(NO_TICKS);
    const own = rereads<LocalEdit>({
      read: () => api.localEdit(listingId),
      answered: (next) => {
        setLoadError(null);
        draw(next);
      },
      failed: (err) =>
        setLoadError(err instanceof Error ? err.message : "Could not read the local model's suggestions."),
      // Every 2.5 s while the model is at work, slowly while its request
      // waits for a time far off, and not at all otherwise: every press
      // reads again at once.
      every: (latest, failed) => readAgainIn(latest?.job, failed),
      hidden: () => document.hidden,
      later: (run, ms) => window.setTimeout(run, ms),
      cancel: (handle) => window.clearTimeout(handle as number),
    });
    reader.current = own;
    void own.now();
    const onShow = () => {
      if (!document.hidden) void own.now();
    };
    document.addEventListener("visibilitychange", onShow);
    return () => {
      document.removeEventListener("visibilitychange", onShow);
      own.stop();
      if (reader.current === own) reader.current = null;
    };
  }, [listingId, draw]);

  const readNow = useCallback(() => void reader.current?.now(), []);
  const withJob = useCallback((job: ClipLookJob) => {
    setEdit((current) => {
      if (!current) return current;
      const drawn = { ...current, job };
      previous.current = drawn;
      return drawn;
    });
  }, []);
  const setTicked = useCallback(
    (ticked: string[]) => setTicks((current) => ({ ...current, ticked })),
    [],
  );

  return { edit, loadError, readNow, show: draw, withJob, ticks, setTicked, said, say };
}

/** Ask the model: draws the request at once and says how many it will look at. */
async function askModel(listingId: string, reader: LocalEditReader, redo: boolean): Promise<void> {
  const answer = await api.askLocalEdit(listingId, redo);
  reader.withJob(answer.job);
  reader.say(askedLine(answer.to_look_at));
}

// ---- the rail: asking ---------------------------------------------------------------

/**
 * Under the Auto-edit button and its line, which stay as they were: Ask the
 * local model, how far it has got and Cancel while it looks, and "look at
 * all of them again" once it has suggested anything.
 */
export function LocalEditAsk({ listingId, reader }: { listingId: string; reader: LocalEditReader }) {
  const uid = useId();
  const noteId = `${uid}-note`;
  const { edit } = reader;
  const job = edit?.job ?? null;
  const active = isActive(job);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [redo, setRedo] = useState(false);
  const askRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  // The Ask button is disabled while a press runs and while the model
  // looks, and Cancel goes when it stops: either way the browser drops
  // focus to the top of the page. Once a press here has settled, or the
  // request that press started has ended, focus goes to whichever of the
  // two is there to press (`askFocus`). Never on the first answer: the rail
  // is drawn before it arrives, and a request waiting for the night window
  // used to put Cancel under the keyboard on every visit, where Space —
  // meant to scroll — cancelled it.
  const [settled, setSettled] = useState(0);
  // What the last run of the effect saw: it acts on a change, so a second
  // run with nothing changed (React runs it twice on mount in development)
  // takes nothing. Forgotten while there is no answer (another listing).
  const seen = useRef<AskFocusSeen | null>(null);
  const loaded = edit !== null;
  useEffect(() => {
    if (!loaded) {
      seen.current = null;
      return;
    }
    const { focus, seen: now } = askFocus(seen.current, active, settled);
    seen.current = now;
    if (focus) focusIfLost(focus === "cancel" ? cancelRef.current : askRef.current);
  }, [loaded, active, settled]);

  async function ask() {
    setBusy(true);
    setProblem(null);
    reader.say(null);
    try {
      await askModel(listingId, reader, redo);
      setRedo(false);
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "The local model could not be asked.");
    } finally {
      setBusy(false);
      setSettled((count) => count + 1);
      reader.readNow();
    }
  }

  async function cancel() {
    setBusy(true);
    setProblem(null);
    try {
      const answer = await api.cancelLocalEdit(listingId);
      reader.withJob(answer.job);
      // A request that had not started ends at once; one under way ends
      // when the photograph in hand is done, and the read that sees it gone
      // says so.
      if (answer.job.state === "cancelled") reader.say(ASK_STOPPED_LINE);
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "It could not be stopped.");
    } finally {
      setBusy(false);
      setSettled((count) => count + 1);
      reader.readNow();
    }
  }

  const blocked = !edit || !edit.ok || edit.photographs === 0;
  return (
    <div className="localedit-ask">
      <p className="faint localedit-or">{ASK_OR_LINE}</p>
      <button
        ref={askRef}
        type="button"
        className="btn"
        style={{ width: "100%" }}
        disabled={busy || active || blocked}
        aria-describedby={edit && !edit.ok && edit.note ? noteId : undefined}
        onClick={ask}
      >
        {askLabel(edit ?? { job: null, suggestions: [] })}
      </button>
      {active && (
        <div className="localedit-job" role="status">
          <span className="spinner" aria-hidden="true" />
          <span>{jobLine(job)}</span>
          <button
            ref={cancelRef}
            type="button"
            className="linkish"
            disabled={busy || job.cancelling || !job.can_cancel}
            onClick={cancel}
          >
            Cancel
          </button>
        </div>
      )}
      {!active && edit?.ok && edit.suggestions.length > 0 && (
        <label className="faint localedit-redo">
          <input type="checkbox" checked={redo} onChange={(event) => setRedo(event.target.checked)} />
          look at all of them again
        </label>
      )}
      {edit && !edit.ok && edit.note && (
        <span id={noteId} className="faint localedit-note">
          {edit.note}
        </span>
      )}
      <span className="faint localedit-note">{ASK_EXPLAINED}</span>
      {job?.state === "failed" && (
        <div className="localedit-problem" role="alert">
          {jobLine(job)}
        </div>
      )}
      {problem && (
        <div className="localedit-problem" role="alert">
          {problem}
        </div>
      )}
      {!edit && reader.loadError && (
        <div className="localedit-problem" role="alert">
          {reader.loadError}
        </div>
      )}
      <div className="localedit-said" role="status">
        {reader.said}
      </div>
    </div>
  );
}

// ---- the list: choosing -------------------------------------------------------------

/** The suggested picture: the server's render, or a sentence when it could
 *  not be made (the file is away), never a broken image. */
function ProposalPicture({ src, filename }: { src: string; filename: string }) {
  const [broken, setBroken] = useState<string | null>(null);
  if (broken === src) {
    return <div className="localedit-nopic faint">This picture could not be made.</div>;
  }
  return (
    // A link, so the suggestion can be seen larger (it is made 1024 px wide).
    <a
      href={src}
      target="_blank"
      rel="noopener"
      aria-label={`The suggested version of ${filename}, larger, in a new tab`}
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={src} alt="" loading="lazy" onError={() => setBroken(src)} />
    </a>
  );
}

/**
 * "What the local model suggests", above the gallery. Shown while something
 * waits for a decision, the model is at work, or the last run left
 * photographs out.
 */
export default function LocalEditPanel({
  listingId,
  reader,
  skyName,
  running,
  applying,
  onApply,
  onLook,
}: {
  listingId: string;
  reader: LocalEditReader;
  /** The sky picked under Auto-edit, as its picker names it; "" for none. */
  skyName: string;
  /** An auto-edit run is live on this listing: Apply waits for it. */
  running: boolean;
  /** When the live run is this listing's local Apply, how far it has got
   *  (said beside Apply instead of "Apply waits for it"); else null. */
  applying: string | null;
  /** Start the run (the page's: it shows the run's progress and its end).
   *  Throws with the server's sentence when refused. */
  onApply: (itemIds: string[], assetIds: string[], options: Omit<LocalApplyOptions, "item_ids">) => Promise<void>;
  /** Open the page's viewer on this photograph. */
  onLook: (assetId: string) => void;
}) {
  const uid = useId();
  const headRef = useRef<HTMLHeadingElement>(null);
  const [straighten, setStraighten] = useState(true);
  const [names, setNames] = useState(true);
  const [sky, setSky] = useState<"suggested" | "all">("suggested");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  // The server's sentence when the last Apply left photographs whose
  // suggestion had been made again after the list was read; "" for none.
  const [left, setLeft] = useState("");
  // The last tick, which a shift-click extends from.
  const anchor = useRef<string | null>(null);

  const { edit, ticks } = reader;
  const waiting: LocalEditSuggestion[] = edit ? waitingSuggestions(edit.suggestions) : [];
  const order = waiting.map((row) => row.item_id);
  const ticked = new Set(ticks.ticked);
  const tickedCount = order.filter((id) => ticked.has(id)).length;
  const job = edit?.job ?? null;
  const active = isActive(job);
  const holds = askHoldsList(job);
  const missing = missingLine(edit?.last_run);

  // Apply is disabled while its run goes, and Dismiss takes the rows (and
  // perhaps the whole list) away: once a press here has settled, a keyboard
  // left on nothing goes to the heading — or, when the list has gone, to
  // the rail's Ask — not to the top of the page.
  const [settled, setSettled] = useState(0);
  useEffect(() => {
    if (!settled) return;
    focusIfLost(headRef.current ?? document.querySelector<HTMLElement>(".localedit-ask .btn"));
  }, [settled]);

  if (!edit || !panelShown(edit)) return null;

  function tick(itemId: string, on: boolean, shift: boolean) {
    reader.setTicked(tickRange(order, ticks.ticked, shift ? anchor.current : null, itemId, on));
    anchor.current = itemId;
  }

  async function apply() {
    const chosen = waiting.filter((row) => ticked.has(row.item_id));
    if (!chosen.length) return;
    setBusy(true);
    setProblem(null);
    setLeft("");
    reader.say(null);
    try {
      // With what each row looked like when it was ticked: one suggested
      // again since is left waiting, and the server says so in a sentence
      // (or refuses with it, 409, when that was every one ticked).
      await onApply(
        chosen.map((row) => row.item_id),
        chosen.map((row) => row.asset_id),
        {
          straighten,
          names,
          sky,
          made_at: madeAtShown(chosen),
          heard: (answer) => setLeft(answer.note ?? ""),
        },
      );
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "The suggestions could not be applied.");
    } finally {
      setBusy(false);
      setSettled((count) => count + 1);
      reader.readNow();
    }
  }

  async function dismiss() {
    // Every row of the list, ticked or not, and making them again is a fresh
    // run of the model: one slip beside Apply must not lose them unasked.
    if (!window.confirm(dismissConfirm(order.length))) return;
    // The rows on screen, by name: with none named the server puts aside
    // every row still waiting, one written since the last read included.
    const sent = order;
    setBusy(true);
    setProblem(null);
    setLeft("");
    reader.say(null);
    try {
      const answer = await api.dismissLocalEdit(listingId, sent);
      reader.show(answer);
      reader.say(dismissedLine(dismissedOf(sent, answer.suggestions)));
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "The suggestions could not be dismissed.");
    } finally {
      setBusy(false);
      setSettled((count) => count + 1);
      reader.readNow();
    }
  }

  async function askAgain() {
    setBusy(true);
    setProblem(null);
    setLeft("");
    reader.say(null);
    try {
      await askModel(listingId, reader, false);
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "The local model could not be asked.");
    } finally {
      setBusy(false);
      reader.readNow();
    }
  }

  const tickHint = `${uid}-tick`;
  const waitsId = `${uid}-waits`;
  const waitsWhy = running || holds ? waitsId : undefined;
  return (
    <section className="card localedit" aria-labelledby={`${uid}-head`}>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2 id={`${uid}-head`} ref={headRef} tabIndex={-1}>
          {PANEL_TITLE}
        </h2>
        <span className="faint mono">{localEditLine(edit)}</span>
      </div>
      {edit.mode === "demo" && <div className="demo-banner">{LISTING_DEMO_LINE}</div>}
      {active && (
        // The rail's Cancel stops it; here it is only said how far it has got.
        <p className="localedit-job">
          <span className="spinner" aria-hidden="true" />
          <span>{jobLine(job)}</span>
        </p>
      )}

      {waiting.length > 0 && (
        <>
          <div className="toolbar localedit-uses">
            <span className="faint">Apply will use:</span>
            <label>
              <input
                type="checkbox"
                checked={straighten}
                onChange={(event: ChangeEvent<HTMLInputElement>) => setStraighten(event.target.checked)}
              />{" "}
              Straighten as suggested
            </label>
            <label>
              <input
                type="checkbox"
                checked={names}
                onChange={(event: ChangeEvent<HTMLInputElement>) => setNames(event.target.checked)}
              />{" "}
              Use the suggested names
            </label>
            <span>{skyChoiceLine(skyName)}</span>
            {skyName && (
              <select
                className="select"
                aria-label="Where the sky goes"
                value={sky}
                onChange={(event) => setSky(event.target.value === "all" ? "all" : "suggested")}
              >
                {SKY_WHERE.map((choice) => (
                  <option key={choice.value} value={choice.value}>
                    {choice.label}
                  </option>
                ))}
              </select>
            )}
          </div>
          <div className="toolbar localedit-ticks">
            {/* Never disabled: pressing one would drop the keyboard to the
                top of the page the moment it had done its work. */}
            <button type="button" className="btn" onClick={() => reader.setTicked(order)}>
              Tick all
            </button>
            <button type="button" className="btn" onClick={() => reader.setTicked([])}>
              Untick all
            </button>
            <span className="faint">
              Click a picture to look at it · tick to apply
              <span className="pointer-hint">, shift-click a tick for a range</span>
            </span>
          </div>
          <ul className="proposals">
            {waiting.map((row) => {
              const src = proposalPicture(localEditPreviewUrl(listingId, row.item_id, straighten), row.made_at);
              return (
                <li key={row.item_id} data-large={row.large || undefined}>
                  <input
                    type="checkbox"
                    className="localedit-tick"
                    checked={ticked.has(row.item_id)}
                    aria-label={`Apply to ${row.filename}`}
                    // A checkbox's change rides on its click, so the shift
                    // key is there to be read.
                    onChange={(event) =>
                      tick(row.item_id, event.target.checked, (event.nativeEvent as MouseEvent).shiftKey === true)
                    }
                  />
                  <figure className="localedit-pic">
                    <LookFrame
                      assetId={row.asset_id}
                      mediaType="image"
                      filename={row.filename}
                      focusKey={`proposals:${row.asset_id}`}
                      onLook={() => onLook(row.asset_id)}
                    />
                    <figcaption className="faint">As shot</figcaption>
                  </figure>
                  <figure className="localedit-pic">
                    <ProposalPicture src={src} filename={row.filename} />
                    <figcaption className="faint">Suggested</figcaption>
                  </figure>
                  <div className="localedit-words">
                    <div className="mono localedit-file">{row.filename}</div>
                    <div>
                      {straightenText(row.rotate, row.keystone)}
                      {row.large && (
                        <>
                          {" "}
                          <span className="pill" data-tone="warn">
                            large — look first
                          </span>
                        </>
                      )}
                    </div>
                    <div>{skyText(row.sky_worth_replacing)}</div>
                    {(row.caption || row.slug) && (
                      <div className="localedit-name">
                        {row.caption && <span className="mono">{row.caption}</span>}
                        {row.slug && <span className="mono faint">{row.slug}</span>}
                        {row.name_is_yours && <span className="faint">your own name is kept</span>}
                      </div>
                    )}
                    {row.note && <div className="faint">{row.note}</div>}
                    {row.demo && (
                      <span className="pill" title="Made up for the demonstration, not by a model">
                        demo
                      </span>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
          <div className="toolbar localedit-foot">
            <button
              type="button"
              className="btn btn-primary"
              // Not while the model looks (`askHoldsList`): a suggestion asked
              // for again can replace a ticked one before the run reads it,
              // and Apply sends only which rows — it would write the one
              // nobody looked at.
              disabled={busy || running || holds || tickedCount === 0}
              aria-describedby={waitsWhy ?? (tickedCount === 0 ? tickHint : undefined)}
              onClick={apply}
            >
              {applyLabel(tickedCount)}
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy || running || holds}
              aria-describedby={waitsWhy}
              onClick={dismiss}
            >
              Dismiss the suggestions
            </button>
            {tickedCount === 0 && (
              <span id={tickHint} className="faint">
                {TICK_ONE}
              </span>
            )}
            {(running || holds) && (
              <span id={waitsId} className="faint">
                {running ? (applying ?? APPLY_WAITS_FOR_RUN) : APPLY_WAITS_FOR_ASK}
              </span>
            )}
          </div>
        </>
      )}

      {missing && (
        <p className="faint localedit-missing">
          {missing}{" "}
          <button
            type="button"
            className="linkish"
            disabled={busy || active || !edit.ok}
            onClick={askAgain}
          >
            Ask again
          </button>
        </p>
      )}
      {/* Always in the page, so the sentence is read out when it arrives. */}
      <p className="localedit-said localedit-left" role="status">
        {left}
      </p>
      {problem && (
        <div className="localedit-problem" role="alert">
          {problem}
        </div>
      )}
    </section>
  );
}
