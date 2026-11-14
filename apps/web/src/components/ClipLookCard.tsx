"use client";

// A clip's look: three versions of the same moment side by side — the local
// model's suggestion, the look learned from what the operator chose before,
// and the clip as it was shot — to choose one by eye and take it to Premiere
// as a LUT.
//
// Self-loading by `assetId`, like TagEditor. It sits on a video's page after
// the player, the scene strip and the transcript, and replaces nothing
// there: those stay together as they were, and its own picture buttons open
// a larger view inside the card, not a dialog over the page.
//
// No look is put first. The three tiles always come in the server's order,
// every "Use this look" is the same plain button, and nothing here shows a
// colour difference or a score: a clip that measured close looked far worse
// by eye on 901 Smyrna Rd (docs/ai-editing-models.md).
//
// A request to the local model is the server's, never the tab's: the card
// shows what each read of the clip says (`suggest.job`), reads again while
// one is on its way, and so shows the same thing after the page is left and
// opened again, or opened in another tab.

import Link from "next/link";
import {
  Fragment,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
  type SyntheticEvent,
} from "react";

import {
  api,
  clipLookPreviewUrl,
  clipLutUrl,
  type ClipLookCandidate,
  type ClipLookJob,
  type ClipLookSize,
  type ClipLookSource,
  type ClipLooks,
} from "@/lib/api";
import {
  ADJUST_WAIT_MS,
  CARD_LINE,
  FLIP_LABELS,
  flipNote,
  LUT_IN_PREMIERE,
  LUT_SIZES,
  momentsLine,
  NO_LUT_LINE,
  NO_SLIDERS,
  SLIDERS,
  adjustChoice,
  adjustStart,
  cancelSaid,
  cannotCarry,
  chosenApart,
  chosenLine,
  chosenTile,
  fileNameOf,
  flipTo,
  folderAskedLine,
  folderEvery,
  folderLine,
  folderListTitle,
  folderPill,
  folderSuggestLabel,
  folderZipBlocked,
  isActive,
  jobLine,
  learnedFrom,
  momentButtonLabel,
  momentLabel,
  needsLooker,
  needsLut,
  provenanceLine,
  readAgainIn,
  shownMoment,
  sliderFigure,
  sliderRows,
  slidersBody,
  sooner,
  suggestEnded,
  suggestLabel,
  tileTitle,
  watchFolder,
  whiteBalanceLine,
  type FolderWatch,
  type Sliders,
} from "@/lib/clipLooks";
import { focusIfLost, rereads, type Rereader } from "@/lib/localModels";

const DEMO_LINE = "Demonstration only. Suggestions on this card are made up, not made by a model.";

// Where focus goes back to when the control that was pressed has gone: the
// part of the card it was in, and inside the model's tile the control that
// took its place (Suggest became Cancel, Cancel became Ask again).
const FOCUS_HOMES = ".looktile, .lookflip, .looks-adjust, .looks-foot, .looks-folder";
const ASK_BUTTON = ".looks-ask button:not([disabled])";

export default function ClipLookCard({
  assetId,
  hasPlayer = true,
}: {
  assetId: string;
  /** The page has a player for this clip. False when it cannot play here
   *  (the poster and the reason stand in its place). */
  hasPlayer?: boolean;
}) {
  const uid = useId();
  const [looks, setLooks] = useState<ClipLooks | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // The still the tiles show; null is the reference the server measured on.
  const [moment, setMoment] = useState<number | null>(null);
  const [size, setSize] = useState<ClipLookSize>(33);
  // The look shown large, or null for the three tiles.
  const [flip, setFlip] = useState<ClipLookSource | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [said, setSaid] = useState<string | null>(null);
  const sectionRef = useRef<HTMLElement>(null);
  const flipRef = useRef<HTMLDivElement>(null);
  // The picture that opened the flip view, so Back puts focus where it was.
  const openedFrom = useRef<ClipLookSource | null>(null);
  const tileButtons = useRef<Partial<Record<ClipLookSource, HTMLButtonElement | null>>>({});
  // The tile pressed last (its picture, the flip view, Use this look): where
  // Adjust starts.
  const pressed = useRef<ClipLookSource | null>(null);

  // Reading the clip. `previous` is the last answer drawn, so a suggestion
  // that arrives while the card watches is announced; `folderWatch` keeps
  // the card reading slowly after the folder's Suggest.
  const reader = useRef<Rereader<ClipLooks> | null>(null);
  const previous = useRef<ClipLooks | null>(null);
  const folderWatch = useRef<FolderWatch | null>(null);
  // A picture that could not be made (the proxy store away, a grab that
  // failed) is tried again when this moves on: when the tab is shown again,
  // after a press, and when the clip reads again after a read that failed.
  // Not at every read: while a request moves the card reads every 2.5 s.
  const [pictureTry, setPictureTry] = useState(0);
  const readFailed = useRef(false);

  useEffect(() => {
    previous.current = null;
    folderWatch.current = null;
    setLooks(null);
    setMoment(null);
    setFlip(null);
    const own = rereads<ClipLooks>({
      read: () => api.clipLooks(assetId),
      answered: (next) => {
        if (readFailed.current) {
          readFailed.current = false;
          setPictureTry((count) => count + 1);
        }
        const ended = suggestEnded(previous.current, next);
        previous.current = next;
        setLooks(next);
        setLoadError(null);
        if (ended) {
          setProblem(null);
          setSaid(ended);
        }
      },
      failed: (err) => {
        readFailed.current = true;
        setLoadError(err instanceof Error ? err.message : "Could not load this clip's looks.");
      },
      // Quickly while this clip's request moves, slowly while the folder's
      // answers are still to come, and not at all otherwise: every press
      // reads the card again at once.
      every: (latest, failed) =>
        sooner(readAgainIn(latest?.suggest.job, failed), folderEvery(folderWatch.current, latest?.folder)),
      hidden: () => document.hidden,
      later: (run, ms) => window.setTimeout(run, ms),
      cancel: (handle) => window.clearTimeout(handle as number),
    });
    reader.current = own;
    void own.now();
    const onShow = () => {
      if (document.hidden) return;
      setPictureTry((count) => count + 1);
      void own.now();
    };
    document.addEventListener("visibilitychange", onShow);
    return () => {
      document.removeEventListener("visibilitychange", onShow);
      own.stop();
      if (reader.current === own) reader.current = null;
    };
  }, [assetId]);

  function readNow() {
    void reader.current?.now();
  }

  // Focus follows the flip view, after React has drawn it: in the view, the
  // button of the look on show, so ← and → carry on from wherever they
  // left it; out of it, the picture that opened it, so the keyboard is not
  // dropped to the top of the page.
  const returnTo = useRef<ClipLookSource | null>(null);
  useEffect(() => {
    if (flip) {
      flipRef.current?.querySelector<HTMLButtonElement>('button[aria-pressed="true"]')?.focus();
      return;
    }
    const back = returnTo.current;
    returnTo.current = null;
    if (back) tileButtons.current[back]?.focus();
  }, [flip]);

  // A pressed button is disabled while its request runs, and may be gone
  // when it ends (Suggest becomes Cancel; Use this look becomes "In use"):
  // the browser then drops focus to the page's top. Once the press has
  // settled, focus goes back to the button, or to what replaced it.
  const focusHome = useRef<{ el: HTMLElement; box: HTMLElement | null } | null>(null);
  const [settled, setSettled] = useState(0);
  useEffect(() => {
    const home = focusHome.current;
    focusHome.current = null;
    if (!home) return;
    const { el } = home;
    if (el.isConnected && !(el instanceof HTMLButtonElement && el.disabled)) {
      focusIfLost(el);
      return;
    }
    const box = home.box?.isConnected ? home.box : sectionRef.current;
    // In the flip view (Cancel pressed there, and gone with the request),
    // the look on show, so the arrows still work.
    const look = box?.matches(".lookflip") ? box.querySelector<HTMLElement>('button[aria-pressed="true"]') : null;
    focusIfLost(box?.querySelector<HTMLElement>(ASK_BUTTON) ?? look ?? box);
  }, [settled]);

  // The same when a read, not a press, changes the request under the
  // keyboard (Cancel goes when the suggestion arrives).
  const job = looks?.suggest.job ?? null;
  const jobKey = !looks ? null : isActive(job) ? "active" : job?.state === "failed" ? "failed" : "idle";
  const lastJobKey = useRef<string | null>(null);
  useEffect(() => {
    const before = lastJobKey.current;
    lastJobKey.current = jobKey;
    if (before === null || jobKey === null || before === jobKey) return;
    const tile = sectionRef.current?.querySelector<HTMLElement>('.looktile[data-source="model"]');
    if (tile) {
      focusIfLost(tile.querySelector<HTMLElement>(ASK_BUTTON) ?? tile);
      return;
    }
    // In the flip view the request's line sits under its buttons; when it
    // goes, the look on show keeps the keyboard.
    const view = flipRef.current;
    if (view) {
      focusIfLost(
        view.querySelector<HTMLElement>(ASK_BUTTON) ??
          view.querySelector<HTMLElement>('button[aria-pressed="true"]') ??
          view,
      );
    }
  }, [jobKey]);

  /**
   * Run one press: busy while it runs, the server's sentence if it refuses,
   * and in every case the card read again at once, so what is shown is what
   * the server holds.
   */
  async function act<T>(action: () => Promise<T>, after: (answer: T) => string | null) {
    const active = document.activeElement;
    focusHome.current =
      active instanceof HTMLElement && sectionRef.current?.contains(active)
        ? { el: active, box: active.closest<HTMLElement>(FOCUS_HOMES) }
        : null;
    setBusy(true);
    setProblem(null);
    setSaid(null);
    try {
      setSaid(after(await action()));
    } catch (err) {
      setProblem(err instanceof Error ? err.message : "That did not work.");
    } finally {
      setBusy(false);
      setSettled((count) => count + 1);
      setPictureTry((count) => count + 1);
      readNow();
    }
  }

  /** Draw an answer that is the whole card (a choice made or cleared). */
  function show(answer: ClipLooks) {
    previous.current = answer;
    setLooks(answer);
  }

  /** Draw the request a press started or stopped, before the next read. It
   *  is also what the next read is compared with, so a suggestion that
   *  lands before that read is still announced. */
  function withJob(next: ClipLookJob) {
    setLooks((current) => {
      if (!current) return current;
      const drawn = { ...current, suggest: { ...current.suggest, job: next } };
      previous.current = drawn;
      return drawn;
    });
  }

  function closeFlip() {
    returnTo.current = openedFrom.current;
    setFlip(null);
  }

  // ---- the folder's zip -------------------------------------------------------------

  // Asked for rather than linked: whether the server makes it depends on
  // which choices change anything, which the page cannot see, and a link
  // would save its refusal as the zip.
  const [zipping, setZipping] = useState(false);
  const [zipSaid, setZipSaid] = useState<{ text: string; refused: boolean } | null>(null);

  async function downloadFolder(at: ClipLookSize) {
    if (zipping) return;
    setZipping(true);
    setZipSaid({ text: "Making the zip…", refused: false });
    try {
      const { blob, disposition } = await api.folderLooksFile(assetId, at);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = fileNameOf(disposition, "FrameFound looks.zip");
      document.body.appendChild(link);
      link.click();
      link.remove();
      // Given back once the browser has taken it; at once can cancel it.
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
      setZipSaid({ text: `Downloaded ${link.download}.`, refused: false });
    } catch (err) {
      setZipSaid({ text: err instanceof Error ? err.message : "The zip could not be made.", refused: true });
    } finally {
      setZipping(false);
    }
  }

  // ---- Adjust ------------------------------------------------------------------------

  const [adjustOpen, setAdjustOpen] = useState(false);
  const [start, setStart] = useState<{ from: string; sliders: Sliders }>({ from: "No change", sliders: NO_SLIDERS });
  const [sliders, setSliders] = useState<Sliders>(NO_SLIDERS);
  const [yours, setYours] = useState<string | null>(null);
  const [making, setMaking] = useState(false);
  const [yoursProblem, setYoursProblem] = useState<string | null>(null);
  // Pictures can land out of order; the ticket makes the newest one win,
  // as in the editor.
  const yoursTicket = useRef(0);
  const yoursUrl = useRef<string | null>(null);

  const ts = looks ? shownMoment(looks, moment) : null;
  const canAdjust = !!looks && !looks.problem && ts !== null;

  // "Yours": the still through the sliders, asked for a moment after the
  // last move rather than at every step of a drag.
  useEffect(() => {
    if (!adjustOpen || !canAdjust) return;
    const mine = ++yoursTicket.current;
    setMaking(true);
    const timer = window.setTimeout(async () => {
      try {
        const blob = await api.clipLookPreview(assetId, slidersBody(sliders), ts, size);
        if (mine !== yoursTicket.current) return;
        const url = URL.createObjectURL(blob);
        if (yoursUrl.current) URL.revokeObjectURL(yoursUrl.current);
        yoursUrl.current = url;
        setYours(url);
        setYoursProblem(null);
      } catch (err) {
        if (mine === yoursTicket.current) {
          setYoursProblem(err instanceof Error ? err.message : "This picture could not be made.");
        }
      } finally {
        if (mine === yoursTicket.current) setMaking(false);
      }
    }, ADJUST_WAIT_MS);
    return () => window.clearTimeout(timer);
  }, [adjustOpen, canAdjust, assetId, sliders, ts, size]);

  // The last picture's memory is given back when the card goes.
  useEffect(
    () => () => {
      yoursTicket.current += 1;
      if (yoursUrl.current) URL.revokeObjectURL(yoursUrl.current);
      yoursUrl.current = null;
    },
    [],
  );

  function onAdjustToggle(event: SyntheticEvent<HTMLDetailsElement>) {
    const open = event.currentTarget.open;
    if (open && !adjustOpen && looks) {
      const from = adjustStart(looks.candidates, pressed.current, looks.chosen);
      setStart(from);
      setSliders(from.sliders);
    }
    setAdjustOpen(open);
  }

  // ---- what is drawn -----------------------------------------------------------------

  if (loadError && !looks) {
    return (
      <section className="looks" ref={sectionRef}>
        <div className="sectionhead looks-head">
          <h2>Look</h2>
        </div>
        <div className="card" role="alert">
          <p style={{ margin: 0 }}>{loadError}</p>
          <button className="btn" style={{ marginTop: 12 }} onClick={readNow}>
            Try again
          </button>
        </div>
      </section>
    );
  }

  if (!looks) {
    return (
      <section className="looks" ref={sectionRef}>
        <div className="sectionhead looks-head">
          <h2>Look</h2>
        </div>
        <div className="empty">Loading…</div>
      </section>
    );
  }

  const showable = looks.candidates.filter((candidate) => candidate.available).map((c) => c.source);
  const chosen = looks.chosen;
  const chosenOne = chosenTile(looks.candidates, chosen);
  const noneVersion = looks.candidates.find((c) => c.source === "none")?.version ?? "";
  const wbLine = whiteBalanceLine(looks.candidates);
  const shown = flip ? looks.candidates.find((c) => c.source === flip) : undefined;
  const suggest = looks.suggest;
  const folder = looks.folder;
  // Where the sentence saying why Suggest is refused is on screen: in the
  // model's tile while the tiles show. The folder's Suggest points to it
  // there, and says it itself when the tiles are not shown.
  const noteId = `${uid}-ask-note`;
  const noteInTile = !looks.problem && !flip;

  function picture(candidate: ClipLookCandidate) {
    return clipLookPreviewUrl(assetId, candidate.source, ts, size, candidate.version);
  }

  function titleOf(source: ClipLookSource): string {
    return tileTitle(looks?.candidates.find((c) => c.source === source) ?? { source, basis: null });
  }

  function choose(source: ClipLookSource) {
    pressed.current = source;
    act(
      () => api.chooseClipLook(assetId, source),
      (answer) => {
        show(answer);
        return `Chosen: ${titleOf(source)}.`;
      },
    );
  }

  function askModel() {
    act(
      () => api.suggestClipLook(assetId),
      (answer) => {
        withJob(answer.job);
        return null;
      },
    );
  }

  function cancelAsking() {
    act(
      () => api.cancelClipLook(assetId),
      (answer) => {
        // One being stopped is drawn as such, and its end is said by the
        // read that sees it end. One that was waiting or queued has ended
        // already and is said now: no later read shows it. One that
        // finished first is not drawn, so the read after this still sees
        // it end and says what arrived.
        if (isActive(answer.job) || answer.job.state === "cancelled") withJob(answer.job);
        return cancelSaid(answer.job);
      },
    );
  }

  function chooseMine() {
    const choice = adjustChoice(sliders);
    act(
      () => api.chooseClipLook(assetId, choice.source, choice.recipe),
      (answer) => {
        show(answer);
        return choice.source === "custom" ? "Chosen: your own version." : "Chosen: No change (no slider was moved).";
      },
    );
  }

  function askFolder() {
    const before = folder;
    act(
      () => api.suggestFolderLooks(assetId),
      (answer) => {
        folderWatch.current = watchFolder(before, answer.queued);
        return folderAskedLine(answer);
      },
    );
  }

  function onFlipKey(event: KeyboardEvent<HTMLDivElement>) {
    if (!flip) return;
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      event.preventDefault();
      const next = flipTo(showable, flip, event.key === "ArrowRight" ? 1 : -1);
      pressed.current = next;
      setFlip(next);
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeFlip();
    }
  }

  // The request while it is on its way: what it is doing, and Cancel. In
  // the model's tile, and in the flip view, which hides the tiles: a look
  // shown large must not hide that the model is still at work.
  function jobArea(extra?: string): ReactNode {
    if (!isActive(job)) return null;
    return (
      <div className={`looks-ask looks-job${extra ? ` ${extra}` : ""}`}>
        <span className="spinner" aria-hidden="true" />
        <span role="status">{jobLine(job)}</span>
        <button
          className="btn"
          disabled={busy || job.cancelling || !job.can_cancel}
          onClick={cancelAsking}
        >
          Cancel
        </button>
      </div>
    );
  }

  // The model's tile: Suggest a look / Ask again, the request while it is
  // on its way with Cancel, or why it failed with Suggest again.
  function askArea(model: ClipLookCandidate): ReactNode {
    if (isActive(job)) return jobArea();
    const refused = !suggest.ok;
    const button = (label: string) => (
      <button
        className="btn"
        disabled={busy || refused}
        aria-describedby={refused ? noteId : undefined}
        onClick={askModel}
      >
        {label}
      </button>
    );
    const why = (
      <>
        {/* The tile's empty box already says it when there is no suggestion. */}
        {refused && model.available && (
          <span id={noteId} className="faint">
            {suggest.note}
          </span>
        )}
        {needsLooker(suggest) && <Link href="/models">Open AI models</Link>}
      </>
    );
    if (job?.state === "failed") {
      return (
        <div className="looks-ask">
          <div className="card looks-failed" role="alert">
            <p>{jobLine(job)}</p>
            {job.detail && (
              <details>
                <summary>Details</summary>
                <pre className="mono">{job.detail}</pre>
              </details>
            )}
            <div className="toolbar looks-ask-bar">
              {button("Suggest again")}
              {why}
            </div>
          </div>
        </div>
      );
    }
    return (
      <div className="looks-ask">
        <div className="toolbar looks-ask-bar">
          {button(suggestLabel(model.available))}
          {why}
        </div>
      </div>
    );
  }

  const folderAsk = folderSuggestLabel(folder);
  const zipBlocked = folderZipBlocked(folder);

  return (
    <section className="looks" aria-label="Look" ref={sectionRef}>
      <div className="sectionhead looks-head">
        <h2>Look</h2>
      </div>
      <p className="faint looks-line">{CARD_LINE}</p>

      {suggest.mode === "demo" && <div className="demo-banner">{DEMO_LINE}</div>}

      {looks.problem ? (
        <div className="empty looks-empty">{looks.problem}</div>
      ) : (
        <>
          {looks.stills.length > 1 && (
            <>
              {/* It looks like the page's scene strip, but moves the looks,
                  not the player: it says so, and is drawn apart
                  (looks-moments). */}
              <p id={`${uid}-moments`} className="faint looks-line">
                {momentsLine(hasPlayer)}
              </p>
              <div className="strip looks-moments" role="group" aria-labelledby={`${uid}-moments`}>
                {looks.stills.map((still) => (
                  <button
                    key={still.ts_ms}
                    className="strip-cell"
                    data-active={still.ts_ms === ts}
                    aria-pressed={still.ts_ms === ts}
                    aria-label={momentButtonLabel(still.ts_ms)}
                    title={momentButtonLabel(still.ts_ms)}
                    onClick={() => setMoment(still.ts_ms)}
                  >
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={clipLookPreviewUrl(assetId, "none", still.ts_ms, size, noneVersion)}
                      alt=""
                      loading="lazy"
                      decoding="async"
                    />
                    <time>{momentLabel(still.ts_ms)}</time>
                  </button>
                ))}
              </div>
            </>
          )}
          {wbLine && <p className="faint looks-line">{wbLine}</p>}

          {flip && shown ? (
            <div
              ref={flipRef}
              className="lookflip"
              role="group"
              aria-label={`${tileTitle(shown)}, large. Left and right arrows change the look.`}
              onKeyDown={onFlipKey}
            >
              <div className="toolbar lookflip-bar">
                {looks.candidates.map((candidate) => (
                  <button
                    key={candidate.source}
                    className="btn"
                    aria-pressed={candidate.source === flip}
                    disabled={!candidate.available}
                    title={candidate.available ? undefined : candidate.note ?? undefined}
                    onClick={() => {
                      pressed.current = candidate.source;
                      setFlip(candidate.source);
                    }}
                  >
                    {FLIP_LABELS[candidate.source]}
                  </button>
                ))}
                <button className="btn lookflip-back" onClick={closeFlip}>
                  Back to all three
                </button>
              </div>
              {jobArea("lookflip-job")}
              <LookPicture
                src={picture(shown)}
                alt={`${tileTitle(shown)} at ${momentLabel(ts ?? 0)}`}
                again={pictureTry}
                canRetry
              />
              <div className="toolbar lookflip-foot">
                <span className="faint">{flipNote(hasPlayer)}</span>
                {chosenOne === shown.source ? (
                  <span className="pill" data-tone="ok">
                    chosen
                  </span>
                ) : (
                  <button className="btn" disabled={busy} onClick={() => choose(shown.source)}>
                    Use this look
                  </button>
                )}
              </div>
            </div>
          ) : (
            <div className="lookgrid">
              {looks.candidates.map((candidate) => (
                <LookTile
                  key={candidate.source}
                  candidate={candidate}
                  src={picture(candidate)}
                  momentText={momentLabel(ts ?? 0)}
                  chosen={chosenOne === candidate.source}
                  // A suggestion nobody has answered yet is marked as the
                  // folder's list marks it.
                  waiting={candidate.source === "model" && candidate.available && !chosen}
                  busy={busy}
                  // No change, and a look that moves nothing, need no LUT: the
                  // server refuses the file, and the link would save that.
                  lutHref={
                    candidate.source === "none" || !needsLut(candidate.recipe)
                      ? null
                      : clipLutUrl(assetId, candidate.source, size)
                  }
                  again={pictureTry}
                  noteId={candidate.source === "model" && !suggest.ok ? noteId : undefined}
                  ask={candidate.source === "model" ? askArea(candidate) : null}
                  pictureRef={(el) => {
                    tileButtons.current[candidate.source] = el;
                  }}
                  onLook={() => {
                    pressed.current = candidate.source;
                    openedFrom.current = candidate.source;
                    setFlip(candidate.source);
                  }}
                  onUse={() => choose(candidate.source)}
                />
              ))}
            </div>
          )}

          {canAdjust && (
            <details className="disclosure looks-adjust" open={adjustOpen} onToggle={onAdjustToggle}>
              <summary>Adjust before using</summary>
              <p className="faint looks-line">
                Started from: {start.from}. The sliders a LUT can carry; the picture follows them.
              </p>
              <div className="looks-adjust-body">
                <figure className="looks-yours">
                  {yours ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={yours} alt={`Your version at ${momentLabel(ts ?? 0)}`} />
                  ) : (
                    <span className="looktile-nopicture faint">
                      {yoursProblem ?? "Making the picture…"}
                    </span>
                  )}
                  <figcaption className="faint">
                    Yours{yours && making ? " · updating…" : ""}
                  </figcaption>
                  {yours && yoursProblem && (
                    <p className="looks-said" role="alert">
                      {yoursProblem}
                    </p>
                  )}
                </figure>
                <div className="looks-sliders">
                  {SLIDERS.map((spec) => {
                    const id = `${uid}-slider-${spec.key}`;
                    const figure = sliderFigure(spec.key, sliders[spec.key]);
                    return (
                      <div key={spec.key} className="looks-slider">
                        <label htmlFor={id}>{spec.label}</label>
                        <output htmlFor={id} className="mono faint">
                          {figure}
                        </output>
                        <input
                          id={id}
                          type="range"
                          min={spec.min}
                          max={spec.max}
                          step={spec.step}
                          value={sliders[spec.key]}
                          aria-valuetext={figure}
                          onChange={(event) => {
                            const value = Number(event.target.value);
                            setSliders((current) => ({ ...current, [spec.key]: value }));
                          }}
                        />
                      </div>
                    );
                  })}
                </div>
              </div>
              <div className="toolbar looks-adjust-bar">
                <button className="btn" disabled={busy} onClick={chooseMine}>
                  Use my version
                </button>
                <button
                  className="btn"
                  title={`Back to ${start.from}`}
                  onClick={() => setSliders(start.sliders)}
                >
                  Reset
                </button>
              </div>
            </details>
          )}
        </>
      )}

      {(problem || said || loadError) && (
        <p className={problem ? "looks-said" : "faint looks-said"} role={problem ? "alert" : "status"}>
          {problem ?? said ?? loadError}
        </p>
      )}

      <div className="looks-foot">
        {chosen ? (
          <div className="looks-chosen">
            {!chosenOne && !looks.problem && (
              <figure className="looks-chosen-picture">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={clipLookPreviewUrl(assetId, "chosen", ts, size, chosen.version)}
                  alt={`The chosen look at ${momentLabel(ts ?? 0)}`}
                />
                <figcaption className="faint">{chosenApart(chosen.source)}</figcaption>
              </figure>
            )}
            <div>
              <p style={{ margin: 0 }}>
                {chosenOne ? `${titleOf(chosenOne)} · ` : ""}
                {chosenLine(chosen)}
              </p>
              {!chosenOne && chosen.in_words && <p className="faint looks-line">{chosen.in_words}</p>}
              {chosen.demo && (
                <span className="pill" data-tone="warn">
                  demo
                </span>
              )}
            </div>
          </div>
        ) : (
          <p className="faint" style={{ margin: 0 }}>
            No look chosen for this clip yet.
          </p>
        )}
        <div className="toolbar looks-foot-actions">
          {chosen && (
            <button
              className="btn"
              disabled={busy}
              onClick={() =>
                act(
                  () => api.clearClipLook(assetId),
                  (answer) => {
                    show(answer);
                    return "The choice is cleared.";
                  },
                )
              }
            >
              Clear the choice
            </button>
          )}
          {chosen && needsLut(chosen.recipe) && (
            <a className="btn" href={clipLutUrl(assetId, "chosen", size)} download>
              Download LUT (.cube)
            </a>
          )}
          {chosen && chosen.source !== "none" && !needsLut(chosen.recipe) && (
            <span className="faint">{NO_LUT_LINE}</span>
          )}
          <label className="looks-size">
            <span className="faint">LUT size</span>
            <select
              className="select"
              value={size}
              onChange={(event) => setSize(Number(event.target.value) === 65 ? 65 : 33)}
            >
              {LUT_SIZES.map((option) => (
                <option key={option.size} value={option.size}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <details className="disclosure looks-premiere">
        <summary>Using the LUT in Premiere</summary>
        {LUT_IN_PREMIERE.map((paragraph) => (
          <p key={paragraph.slice(0, 24)} className="muted">
            {paragraph}
          </p>
        ))}
      </details>

      <div className="looks-folder">
        <p>{folderLine(folder)}</p>
        <div className="toolbar looks-folder-bar">
          {folderAsk && (
            <button
              className="btn"
              disabled={busy || !suggest.ok}
              aria-describedby={!suggest.ok ? (noteInTile ? noteId : `${uid}-folder-note`) : undefined}
              onClick={askFolder}
            >
              {folderAsk}
            </button>
          )}
          <button
            className="btn"
            disabled={!!zipBlocked}
            // Not disabled while the zip is made: a disabled button loses
            // the keyboard's focus to the top of the page.
            aria-disabled={zipping || undefined}
            aria-describedby={zipBlocked || zipSaid ? `${uid}-zip-note` : undefined}
            onClick={() => void downloadFolder(size)}
          >
            Download this folder&apos;s looks (.zip)
          </button>
          {zipBlocked ? (
            <span id={`${uid}-zip-note`} className="faint">
              {zipBlocked}
            </span>
          ) : (
            zipSaid && (
              <span
                id={`${uid}-zip-note`}
                className={zipSaid.refused ? undefined : "faint"}
                role={zipSaid.refused ? "alert" : "status"}
              >
                {zipSaid.text}
              </span>
            )
          )}
        </div>
        {folderAsk && !suggest.ok && !noteInTile && (
          <p id={`${uid}-folder-note`} className="faint looks-line">
            {suggest.note}
          </p>
        )}
        {folder.others.length > 0 && (
          <details className="disclosure looks-folder-clips">
            <summary>{folderListTitle(folder)}</summary>
            <ul className="looks-folder-list">
              {folder.others.map((clip) => {
                const pill = folderPill(clip.state);
                return (
                  <li key={clip.asset_id}>
                    <Link href={`/assets/${clip.asset_id}`}>{clip.filename}</Link>
                    <span className="pill" data-tone={pill.tone ?? undefined}>
                      {pill.label}
                    </span>
                  </li>
                );
              })}
            </ul>
          </details>
        )}
      </div>
    </section>
  );
}

function LookTile({
  candidate,
  src,
  momentText,
  chosen,
  waiting,
  busy,
  lutHref,
  again,
  noteId,
  ask,
  pictureRef,
  onLook,
  onUse,
}: {
  candidate: ClipLookCandidate;
  src: string;
  momentText: string;
  chosen: boolean;
  waiting: boolean;
  busy: boolean;
  /** Null for No change, and for a look that changes nothing. */
  lutHref: string | null;
  /** Moves on when a picture that could not be made should be tried again. */
  again: number;
  /** The id of the sentence saying why Suggest is refused, when this tile's
   *  empty box is where it is said. */
  noteId?: string;
  /** The model's tile only: asking for a suggestion. */
  ask: ReactNode;
  pictureRef: (el: HTMLButtonElement | null) => void;
  onLook: () => void;
  onUse: () => void;
}) {
  const title = tileTitle(candidate);
  const from = learnedFrom(candidate);
  const lost = cannotCarry(candidate.left_out);
  const rows = sliderRows(candidate.recipe);
  const made = candidate.source === "model" ? provenanceLine(candidate.provenance) : "";
  const marked = folderPill("suggested");

  return (
    <article
      className="looktile"
      data-source={candidate.source}
      data-chosen={chosen}
      data-off={!candidate.available}
    >
      <div className="looktile-head">
        <h3>{title}</h3>
        {chosen && (
          <span className="pill" data-tone="ok">
            chosen
          </span>
        )}
        {waiting && (
          <span className="pill" data-tone={marked.tone ?? undefined}>
            {marked.label}
          </span>
        )}
        {candidate.demo && (
          <span className="pill" data-tone="warn">
            demo
          </span>
        )}
      </div>
      {from && <p className="faint looktile-from">{from}</p>}

      {candidate.available ? (
        <button
          ref={pictureRef}
          type="button"
          className="looktile-picture"
          title="Look at it larger"
          aria-label={`${title}: look at it larger`}
          onClick={onLook}
        >
          <LookPicture src={src} alt={`${title} at ${momentText}`} again={again} />
        </button>
      ) : (
        <div className="empty looktile-off" id={noteId}>
          {candidate.note ?? "Not available for this clip."}
        </div>
      )}

      {candidate.available && (
        <div className="looktile-words">
          {candidate.source === "none" ? (
            <p>The clip as it was shot.</p>
          ) : (
            candidate.in_words && <p>{candidate.in_words}</p>
          )}
          {candidate.reason && <p>&ldquo;{candidate.reason}&rdquo;</p>}
          {candidate.source === "learned" && candidate.note && <p className="faint">{candidate.note}</p>}
          {lost && <p className="faint">{lost}</p>}
          {made && <p className="faint mono looktile-made">{made}</p>}
        </div>
      )}

      {candidate.available && (
        <div className="toolbar looktile-actions">
          <button className="btn" disabled={busy || chosen} onClick={onUse}>
            {chosen ? "In use" : "Use this look"}
          </button>
          {lutHref ? (
            <a className="btn" href={lutHref} download>
              Download LUT (.cube)
            </a>
          ) : (
            candidate.source !== "none" && <span className="faint">{NO_LUT_LINE}</span>
          )}
        </div>
      )}

      {ask}

      {candidate.available && rows.length > 0 && (
        <details className="looktile-numbers">
          <summary>Show the numbers</summary>
          <dl className="kv">
            {rows.map((row) => (
              <Fragment key={row.key}>
                <dt>{row.label}</dt>
                <dd className="mono">{row.value}</dd>
              </Fragment>
            ))}
          </dl>
        </details>
      )}
    </article>
  );
}

/** A look's picture. A still that cannot be made (gone from the proxy, the
 *  look changed under it) says so in its place rather than showing a broken
 *  image. It is tried again at a new address, or when `again` moves on (the
 *  card was read again, the tab shown again): a share that was away when the
 *  page opened must not leave the pictures broken until a reload. Where the
 *  picture is not itself inside a button (`canRetry`), its place also
 *  offers "Try again". */
function LookPicture({
  src,
  alt,
  again,
  canRetry = false,
}: {
  src: string;
  alt: string;
  again: number;
  canRetry?: boolean;
}) {
  const [failed, setFailed] = useState<{ src: string; again: number } | null>(null);
  if (failed && failed.src === src && failed.again === again) {
    return (
      <span className="looktile-nopicture faint">
        This picture could not be made.
        {canRetry && (
          <button
            type="button"
            className="btn"
            onClick={(event) => {
              // The button goes with the failure; the look on show keeps
              // the keyboard rather than the top of the page.
              const home = event.currentTarget
                .closest(".lookflip")
                ?.querySelector<HTMLElement>('button[aria-pressed="true"]');
              setFailed(null);
              home?.focus();
            }}
          >
            Try again
          </button>
        )}
      </span>
    );
  }
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={src} alt={alt} decoding="async" onError={() => setFailed({ src, again })} />;
}
