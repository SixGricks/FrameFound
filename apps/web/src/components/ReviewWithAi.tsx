"use client";

// Review with AI: ask a local model on the Mac to look through a folder.
//
// The dialog says what a review of this folder would take before anyone
// starts one: what has not been looked at, what has, what has to be made
// ready first, which model, about how long. Then Start now, or Tonight.
//
// Opening it only reads. A review begins when a person presses Start now or
// Tonight and at no other moment: not on open, not when the model or a box
// changes, not when the dialog is opened again. Once one is running the
// dialog follows it, and closing loses nothing: the review lives on the
// server, and opening the dialog again on the same folder shows the same
// review.
//
// Every sentence about what may or may not happen is the server's own, shown
// as it wrote it: why models cannot run here, why a model may not start now,
// why a start was refused, how a review is waiting and how it ended. The
// page only counts and words what the preview says is there.
//
// A native modal <dialog>, as the viewer is: the browser keeps Tab inside it
// and the page behind inert. Mount it once per page, never inside a tile.

import { useEffect, useId, useRef, useState, type MouseEvent, type ReactNode } from "react";
import Link from "next/link";

import { api, type LookPreview, type LookReview, type LookStatus } from "@/lib/api";
import { INSTALL_PROGRAM } from "@/lib/localModelsCompare";
import {
  CANCEL_QUESTION,
  NO_REDO_MEMORY,
  alreadyText,
  anythingToReview,
  estimateText,
  failedBeforeText,
  firstText,
  modelChoiceText,
  preparingText,
  previewKinds,
  programMissingText,
  progressText,
  redoShown,
  redoText,
  rememberRedo,
  reviewActive,
  reviewLine,
  reviewStateWord,
  reviewTone,
  reviewWayOut,
  scopeText,
  skippedText,
  startBlock,
  stillsOnlyWhere,
  suggestedText,
  todoText,
  tonightBlock,
  tonightLabel,
  type RedoMemory,
} from "@/lib/looks";

/** How often a review that is still going is read again while the dialog is open. */
const FOLLOW_EVERY_MS = 3000;

export interface ReviewTarget {
  libraryId: string;
  libraryName: string;
  /** "" is the whole library. */
  folder: string;
}

export interface ReviewWithAiProps {
  /** The folder to ask about; null keeps the dialog closed. */
  target: ReviewTarget | null;
  /** `touched` is true when a review was started, followed or stopped while
   *  the dialog was open, so what the page shows of the folder (how much of
   *  it has been looked at, the scores) may be out of date. */
  onClose: (touched: boolean) => void;
  /** The control to focus on the way out; without it, whatever had focus
   *  when the dialog opened (a browser that does not focus a clicked button
   *  would otherwise drop the reader at the top of the page). */
  returnFocus?: () => HTMLElement | null;
}

export default function ReviewWithAi({ target, onClose, returnFocus }: ReviewWithAiProps) {
  if (!target) return null;
  // Keyed by the folder: another folder is another dialog, with none of the
  // last one's choices or its review.
  return (
    <ReviewDialog
      key={`${target.libraryId}\n${target.folder}`}
      target={target}
      onClose={onClose}
      returnFocus={returnFocus}
    />
  );
}

const said = (err: unknown, otherwise: string) => (err instanceof Error ? err.message : otherwise);

function ReviewDialog({
  target,
  onClose,
  returnFocus,
}: {
  target: ReviewTarget;
  onClose: (touched: boolean) => void;
  returnFocus?: () => HTMLElement | null;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const keepRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const modelFieldId = useId();
  const startWhyId = useId();
  const tonightWhyId = useId();
  const programWhyId = useId();

  const [status, setStatus] = useState<LookStatus | null>(null);
  const [preview, setPreview] = useState<LookPreview | null>(null);
  // Why the folder could not be read at all (never shown as "nothing here").
  const [problem, setProblem] = useState<string | null>(null);
  // A preview is being asked for again (the model or the redo box changed).
  const [asking, setAsking] = useState(true);
  // The choices. null is the Looker, whichever model holds the role when the
  // review runs.
  const [modelId, setModelId] = useState<string | null>(null);
  const [redo, setRedo] = useState(false);
  const [prepare, setPrepare] = useState(true);
  // Bumped to ask for the preview again with nothing else changed.
  const [round, setRound] = useState(0);
  // The review being followed: one already running for this folder when the
  // dialog opened, or the one just started here.
  const [review, setReview] = useState<LookReview | null>(null);
  // The server's sentence for a start or a stop it refused.
  const [refusal, setRefusal] = useState<string | null>(null);
  const [sending, setSending] = useState<"now" | "tonight" | "cancel" | null>(null);
  // "Cancel review" was pressed and the question is on screen.
  const [confirming, setConfirming] = useState(false);
  const [followProblem, setFollowProblem] = useState<string | null>(null);
  // What ticking "redo" adds, as last counted without it (in items, and by
  // kind). Kept so the box still says how many while it is ticked, whatever
  // the count reads then — and forgotten once a review has ended, since that
  // review looked at what it counted (`backToFolder`).
  const others = useRef<RedoMemory>(NO_REDO_MEMORY);
  const touched = useRef(false);

  // The page's callbacks change with every render; the effects below are
  // bound once and read them through this.
  const latest = useRef({ onClose, returnFocus });
  useEffect(() => {
    latest.current = { onClose, returnFocus };
  });
  const close = () => latest.current.onClose(touched.current);
  // close() calls of our own (the cleanup below), whose "close" events must
  // not be taken for the reader asking to close.
  const ownCloses = useRef(0);
  const pressedBackdrop = useRef(false);

  // Open as a modal, and on the way out put focus back on the button that
  // opened it: removing an open dialog does not restore focus by itself.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!dialog.open) dialog.showModal();
    // The title, not the first control: what loads under it arrives a
    // moment later, and focus on a button that then moves is no place to be.
    titleRef.current?.focus();
    return () => {
      if (dialog.open) {
        ownCloses.current += 1;
        dialog.close();
      }
      const back = latest.current.returnFocus?.() ?? opener;
      if (back?.isConnected) back.focus();
    };
  }, []);

  // Whether models can run here, who may start a review, the models to
  // choose from. Asked once: none of it changes while a dialog is open.
  useEffect(() => {
    let live = true;
    api
      .lookStatus()
      .then((found) => {
        if (live) setStatus(found);
      })
      .catch((err) => {
        if (live) setProblem(said(err, "The server did not answer."));
      });
    return () => {
      live = false;
    };
  }, []);

  // What a review of this folder would take, with the model and the redo box
  // as they stand. Reading only: nothing here starts anything.
  useEffect(() => {
    let live = true;
    setAsking(true);
    api
      .lookPreview(target.libraryId, target.folder, modelId, redo)
      .then((found) => {
        if (!live) return;
        others.current = rememberRedo(redo, others.current, found);
        setPreview(found);
        setProblem(null);
        // A review of this folder is already running: follow it. One that
        // is being followed is not replaced by this older reading of it.
        if (found.active) setReview((now) => now ?? found.active);
      })
      .catch((err) => {
        if (live) setProblem(said(err, "The server did not answer."));
      })
      .finally(() => {
        if (live) setAsking(false);
      });
    return () => {
      live = false;
    };
  }, [target.libraryId, target.folder, modelId, redo, round]);

  // Back to what is true of the folder now, after a review: only what is
  // new. Redo is unticked and what it would add is counted afresh: the
  // review just looked at what was remembered, and "Also look again at the
  // 6 photographs …" under a ticked box outlived it, while the server (and a
  // fresh dialog) said nothing here was looked at by another model.
  const backToFolder = () => {
    others.current = NO_REDO_MEMORY;
    setRedo(false);
    setReview(null);
    setRound((n) => n + 1);
  };

  // Follow a review while it is still going. The list of reviews is what is
  // read: it settles a review whose work ended some other way (cancelled on
  // the AI models page, say), and it still has the review once it has
  // finished, with its closing sentence.
  const following = review !== null && reviewActive(review) ? review.id : null;
  useEffect(() => {
    if (!following) return;
    let live = true;
    const read = async () => {
      // Nothing is asked while the tab is hidden; its return reads at once.
      if (document.hidden) return;
      try {
        const listed = await api.lookReviews();
        if (!live) return;
        const found = listed.reviews.find((one) => one.id === following);
        setFollowProblem(null);
        if (found) {
          setReview(found);
        } else {
          // No longer listed (its library was removed, or twenty newer
          // reviews have ended since): back to what is true of the folder.
          backToFolder();
        }
      } catch (err) {
        if (live) setFollowProblem(said(err, "The server did not answer."));
      }
    };
    const timer = setInterval(read, FOLLOW_EVERY_MS);
    const onVisible = () => {
      if (!document.hidden) void read();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      live = false;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [following]);

  // A review that was seen here may have changed what the page behind shows.
  useEffect(() => {
    if (review) touched.current = true;
  }, [review]);

  // Where the keyboard is after the buttons change. The button that was
  // pressed goes when the form gives way to the review (and the other way
  // round), and focus would fall out of the dialog's controls with it.
  const view = review ? (reviewActive(review) ? "running" : "ended") : "asking";
  const backToCancel = useRef(false);
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog?.open) return;
    if (confirming) {
      // The question is asked with focus on "No, keep going": Enter held a
      // moment too long on "Cancel review" must not answer it.
      keepRef.current?.focus();
      return;
    }
    if (backToCancel.current) {
      // "No, keep going": back on the button the question came from.
      backToCancel.current = false;
      if (cancelRef.current) {
        cancelRef.current.focus();
        return;
      }
    }
    // Otherwise the title takes it, so the next Tab starts from the top.
    const active = document.activeElement;
    if (!active || active === document.body || active === dialog || !dialog.contains(active)) {
      titleRef.current?.focus();
    }
  }, [view, confirming]);

  async function start(when: "now" | "tonight") {
    if (sending) return;
    setSending(when);
    setRefusal(null);
    try {
      const started = await api.startReview({
        library_id: target.libraryId,
        folder: target.folder,
        when,
        model_id: modelId,
        redo,
        prepare,
      });
      touched.current = true;
      setReview(started);
    } catch (err) {
      // The server's sentence, as it wrote it. What it refused may also have
      // changed what is true here (a review started from another tab), so
      // the folder is read again.
      setRefusal(said(err, "The review could not be started."));
      setRound((n) => n + 1);
    } finally {
      setSending(null);
    }
  }

  async function stop() {
    if (!review || sending) return;
    setSending("cancel");
    setRefusal(null);
    try {
      setReview(await api.cancelReview(review.id));
    } catch (err) {
      setRefusal(said(err, "The review could not be stopped."));
      // "It has already finished": show how.
      try {
        const listed = await api.lookReviews();
        const found = listed.reviews.find((one) => one.id === review.id);
        if (found) setReview(found);
      } catch {
        // The sentence above stands; the next reading will say the rest.
      }
    } finally {
      setSending(null);
      setConfirming(false);
    }
  }

  function onClick(event: MouseEvent<HTMLDialogElement>) {
    // The dialog has no padding, so a click on the element itself is a click
    // on the backdrop. Only when the press began there too.
    if (event.target === event.currentTarget && pressedBackdrop.current) close();
  }

  // Why models cannot run on this install, in the server's sentence; null
  // where they can.
  const unavailableWhy =
    preview && !preview.available
      ? (preview.reason ?? status?.reason ?? "Local AI models cannot run here.")
      : status && !status.available
        ? (status.reason ?? "Local AI models cannot run here.")
        : null;
  const loading = !problem && (status === null || preview === null);
  const canManage = status?.can_manage ?? false;

  const scope = scopeText(
    preview?.library_name ?? target.libraryName,
    preview?.folder ?? target.folder,
    // Where models cannot run nothing was counted: the path alone.
    preview?.available ? preview.folders_inside : null,
  );

  let body: ReactNode;
  let actions: ReactNode = null;
  if (problem && (status === null || preview === null)) {
    // A failed request is not an empty folder; it is its own message.
    body = (
      <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
        Could not read this folder: {problem}
      </div>
    );
  } else if (loading || !status || !preview) {
    body = <p className="faint">Reading this folder…</p>;
  } else if (unavailableWhy) {
    // Where models cannot run: the one sentence, and Close. Nothing else.
    body = <p>{unavailableWhy}</p>;
  } else if (review) {
    const active = reviewActive(review);
    const percent = review.total > 0 ? Math.min(100, (review.done / review.total) * 100) : 0;
    const suggested = suggestedText(review);
    const preparing = preparingText(review.preparing);
    const wayOut = reviewWayOut(review);
    body = (
      <>
        <Demo line={preview.reason} />
        <div className="review-state">
          <span className="pill" data-tone={reviewTone(review.state)} role="status">
            {reviewStateWord(review.state)}
          </span>
          <span className="faint mono">{review.model_name}</span>
        </div>
        {review.total > 0 && (
          <div>
            <div
              className="bar"
              role="progressbar"
              aria-label="Looked at"
              aria-valuemin={0}
              aria-valuemax={review.total}
              // Never past the end, even for a row an older build counted
              // past its total (the words then say the count alone).
              aria-valuenow={Math.min(review.done, review.total)}
            >
              <i style={{ width: `${percent}%` }} />
            </div>
            <p className="faint mono review-count">
              {progressText(review)} looked at
              {review.failed > 0 &&
                `; ${review.failed.toLocaleString("en-US")} could not be`}
            </p>
          </div>
        )}
        {/* The server's sentence. Read out when it is how the review ended,
            not for every file's name on the way. */}
        <p role={active ? undefined : "status"}>{reviewLine(review)}</p>
        {/* Paused, or stopped by the model: the way out is on another page. */}
        {wayOut && (
          <p className="faint">
            {wayOut.text} <Link href={wayOut.href}>{wayOut.link}</Link>.
            {review.state === "failed" && " Then start the review again: what was looked at is kept."}
          </p>
        )}
        {preparing && <p className="faint">{preparing}</p>}
        {suggested && (
          <p>
            {suggested} <Link href="/tags">Answer them on Tags</Link>
          </p>
        )}
        {/* A review that ended with clips judged from one still: where the
            reason is, so the cause can be fixed before the folder is
            reviewed again (the way out of that ending). */}
        {!active && stillsOnlyWhere(review) && (
          <p className="faint">
            {stillsOnlyWhere(review)} <Link href="/processing">Open Processing</Link>
          </p>
        )}
        {active && (
          <p className="faint">
            You can close this: the review carries on without it.{" "}
            <Link href="/processing">Follow it on Processing</Link>
          </p>
        )}
        {followProblem && (
          <p className="faint" role="alert">
            Could not read how it is going: {followProblem}
          </p>
        )}
      </>
    );
    actions = !active ? (
      <button
        type="button"
        className="btn"
        onClick={() => {
          setRefusal(null);
          backToFolder();
        }}
      >
        See what is left here
      </button>
    ) : !review.can_cancel ? (
      <span className="faint">Only an administrator can stop a review.</span>
    ) : confirming ? (
      <>
        <span className="review-question">{CANCEL_QUESTION}</span>
        <button
          type="button"
          className="btn"
          ref={keepRef}
          disabled={sending !== null}
          onClick={() => {
            backToCancel.current = true;
            setConfirming(false);
          }}
        >
          No, keep going
        </button>
        <button type="button" className="btn" disabled={sending !== null} onClick={stop}>
          {sending === "cancel" ? "Stopping…" : "Yes, stop it"}
        </button>
      </>
    ) : (
      <button type="button" className="btn" ref={cancelRef} onClick={() => setConfirming(true)}>
        Cancel review
      </button>
    );
  } else if (!preview.model) {
    body = (
      <>
        <Demo line={preview.reason} />
        <p>
          {preview.no_looker ?? status.looker?.problem ?? "No model has the Looker role yet."}
        </p>
        <p>
          <Link href="/models">Open the AI models page</Link>, then come back here.
        </p>
      </>
    );
  } else {
    const needed = firstText(preview, prepare);
    const left = skippedText(preview.skipped);
    const tried = failedBeforeText(preview.failed_before);
    const something = anythingToReview(preview);
    const startWhy = startBlock(preview);
    // Nothing to do is said once, beside Start now; Tonight is off with it.
    const tonightWhy = tonightBlock(preview);
    const tonightOff = tonightWhy !== null || !something;
    const offersPrepare = preview.first.need_copy + preview.first.need_shots > 0;
    // What was looked at, and what redo adds, by kind. While redo is ticked
    // the preview may no longer count what it adds: the larger count is kept.
    const kinds = previewKinds(preview);
    const counted: RedoMemory = { items: preview.already.by_another_model, kinds: kinds.byAnother };
    const another = redoShown(redo, others.current, counted);
    const byAnother = another.items;
    // The model chosen (the Looker by default) cannot be used: its program
    // is not installed here. Said beside the choice, with where to install
    // it; Start and Tonight are off with it, as the server would refuse.
    const programWhy = programMissingText(preview.model);
    const chosen = preview.model.id;
    // What the select shows: the choice as made, not the model of the
    // preview on screen, which is the last one's until the new one arrives.
    const selected = modelId ?? status.models.find((one) => one.is_looker)?.id ?? chosen;
    body = (
      <>
        <Demo line={preview.reason} />
        <div className="review-rows" aria-busy={asking}>
          <div className="review-row">
            <div className="review-label">{redo ? "To look at" : "Not looked at yet"}</div>
            <div>
              <p>{todoText(preview)}</p>
              {tried && <p className="faint">{tried}</p>}
            </div>
          </div>
          {preview.already.items > 0 && (
            <div className="review-row">
              <div className="review-label">Already looked at</div>
              <div>
                <p>{alreadyText(preview.already, kinds.already)}</p>
              </div>
            </div>
          )}
          {needed.length > 0 && (
            <div className="review-row">
              <div className="review-label">Needed first</div>
              <div>
                {needed.map((line) => (
                  <p key={line}>{line}</p>
                ))}
              </div>
            </div>
          )}
          {left && (
            <div className="review-row">
              <div className="review-label">Left out</div>
              <div>
                <p>{left}</p>
              </div>
            </div>
          )}
          <div className="review-row">
            {status.models.length > 1 ? (
              <label className="review-label" htmlFor={modelFieldId}>
                Model
              </label>
            ) : (
              <div className="review-label">Model</div>
            )}
            <div>
              {status.models.length > 1 ? (
                <select
                  id={modelFieldId}
                  className="select"
                  value={selected}
                  aria-describedby={programWhy ? programWhyId : undefined}
                  onChange={(event) => {
                    const picked = status.models.find((one) => one.id === event.target.value);
                    // The Looker is asked for by its role, so a review
                    // follows the role if it moves before the review runs.
                    setModelId(picked && !picked.is_looker ? picked.id : null);
                    // "Redo" is about the model chosen: another model,
                    // another answer to what it would add.
                    setRedo(false);
                  }}
                >
                  {/* A model no longer among the choices but still the one
                      the preview is about keeps its line. */}
                  {!status.models.some((one) => one.id === selected) && (
                    <option value={selected}>{preview.model.name}</option>
                  )}
                  {status.models.map((one) => (
                    <option key={one.id} value={one.id}>
                      {modelChoiceText(one)}
                      {one.is_looker ? " (the Looker)" : ""}
                    </option>
                  ))}
                </select>
              ) : (
                <p>
                  {modelChoiceText(
                    status.models.find((one) => one.id === chosen) ?? {
                      name: preview.model.name,
                      memory_gb: Number.NaN,
                      fits: preview.model.fits,
                      program_missing: preview.model.program_missing,
                    },
                  )}
                </p>
              )}
              {programWhy && (
                <p className="faint" id={programWhyId}>
                  {programWhy} <Link href={INSTALL_PROGRAM.href}>{INSTALL_PROGRAM.label}</Link>
                </p>
              )}
            </div>
          </div>
          {something && (
            <div className="review-row">
              <div className="review-label">How long</div>
              <div>
                <p>{estimateText(preview.estimate)}</p>
              </div>
            </div>
          )}
        </div>

        <label className="review-check">
          <input
            type="checkbox"
            checked={redo}
            // Never while it is ticked: it must always be possible to untick.
            disabled={!redo && byAnother <= 0}
            onChange={(event) => setRedo(event.target.checked)}
          />
          <span>
            Redo with the current model
            <span className="faint">{redoText(byAnother, another.kinds)}</span>
          </span>
        </label>
        {offersPrepare && (
          <label className="review-check">
            <input
              type="checkbox"
              checked={prepare}
              onChange={(event) => setPrepare(event.target.checked)}
            />
            <span>
              Make clips ready first
              <span className="faint">
                Makes a playable copy of each clip and finds its shots, so every shot is looked
                at. Unticked, a clip that is not ready is judged from one still.
              </span>
            </span>
          </label>
        )}
      </>
    );
    // Why a button is off is said once, beside the buttons, in the bar that
    // does not scroll away. One sentence when both are off for one reason.
    const tonightSays = tonightWhy !== null && tonightWhy !== startWhy && something;
    // A missing program is already said beside the model choice: not twice.
    const startSaid = startWhy !== null && startWhy !== programWhy ? startWhy : null;
    const startWhyRef = startWhy === null ? undefined : startSaid ? startWhyId : programWhyId;
    actions = canManage ? (
      <>
        {preview.paused && (
          // Beside the buttons: it is about what pressing one will do.
          <p className="review-why">Local AI is paused. This will wait until it is resumed.</p>
        )}
        {startSaid && (
          <p className="review-why" id={startWhyId}>
            {tonightOff && !tonightSays ? "" : "Start now: "}
            {startSaid}
          </p>
        )}
        {tonightSays && (
          <p className="review-why" id={tonightWhyId}>
            Tonight: {tonightWhy}
          </p>
        )}
        <button
          type="button"
          className="btn btn-primary"
          disabled={sending !== null || startWhy !== null}
          aria-describedby={startWhyRef}
          onClick={() => start("now")}
        >
          {sending === "now" ? "Starting…" : "Start now"}
        </button>
        <button
          type="button"
          className="btn"
          disabled={sending !== null || tonightOff}
          aria-describedby={
            tonightSays ? tonightWhyId : tonightOff ? startWhyRef : undefined
          }
          onClick={() => start("tonight")}
        >
          {sending === "tonight" ? "Starting…" : tonightLabel(preview.tonight)}
        </button>
      </>
    ) : (
      <span className="faint">Only an administrator can start a review.</span>
    );
  }

  return (
    <dialog
      ref={dialogRef}
      className="preview review"
      aria-labelledby={titleId}
      onCancel={(event) => {
        // Esc, or a phone's back gesture. The page's state decides whether
        // the dialog is there, not the DOM.
        event.preventDefault();
        close();
      }}
      onClose={() => {
        if (ownCloses.current > 0) ownCloses.current -= 1;
        else close();
      }}
      onMouseDown={(event) => {
        pressedBackdrop.current = event.target === event.currentTarget;
      }}
      onClick={onClick}
    >
      <header className="preview-head">
        <div className="preview-title" ref={titleRef} tabIndex={-1}>
          <h2 id={titleId}>Review with AI</h2>
          <p className="faint">{scope}</p>
        </div>
        <button
          type="button"
          className="btn review-x"
          onClick={close}
          aria-label="Close"
          title="Close (Esc)"
        >
          ×
        </button>
      </header>
      <div className="review-body">
        {refusal && (
          // What the server refused, in its words.
          <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
            {refusal}
          </div>
        )}
        {body}
      </div>
      <footer className="preview-bar review-bar">
        {actions}
        <button type="button" className="btn review-close" onClick={close}>
          Close
        </button>
      </footer>
    </dialog>
  );
}

/** In the demonstration, said before anything else: what a review writes
 *  there is made up. The sentence is the server's. */
function Demo({ line }: { line: string | null }) {
  if (!line) return null;
  return <p className="review-note">{line}</p>;
}
