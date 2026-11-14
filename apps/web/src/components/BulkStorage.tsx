"use client";

// Where the bulky files live, and moving them, without a terminal.
//
// Most of what FrameFound makes is small and stays beside the catalogue. A
// few kinds are large and read rarely — video proxies above all — and each
// of those can live on a bigger drive: in the one "bulk folder", or in a
// folder of its own. This section of the Storage page shows where each kind
// is and whether that place is connected, names the bulk folder, moves one
// kind, follows the move, and removes the old copies afterwards.
//
// Three decisions shape it:
//
// - The move belongs to the server, not to this tab. A copy of forty
//   gigabytes to a NAS takes an hour; the page asks how it is going every
//   two seconds while one runs and shows the same move to whoever opens the
//   page, so it can be closed and come back to.
// - A question from the server is asked in the page, beside the folder it is
//   about, and nothing is made in that folder until the answer is yes. The
//   usual one: the folder is on this machine's own disk — which is what a
//   share that is not connected looks like.
// - Nothing here deletes without saying which folder is emptied and that it
//   cannot be undone, and the files in use are never the ones removed.

import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import Link from "next/link";

import {
  ApiError,
  api,
  type BulkFolderResult,
  type KindStatus,
  type MoveStatus,
  type StorageKinds,
} from "@/lib/api";
import { relativeTime } from "@/lib/format";
import {
  REMOVAL_WAIT_MS,
  WHERE_WORDS,
  comesBack,
  copiedWords,
  count,
  diskSize,
  gbToBytes,
  kindsSummary,
  layoutBehind,
  leftBehind,
  looksLikeMac,
  mayStillBeRemoving,
  moveProgress,
  noAnswer,
  phaseWords,
  refusal,
  stillAbout,
  suggestedTarget,
  things,
  type OldCopiesSaid,
} from "@/lib/storage";

// How often a running move is asked about. It is one row read on the server.
const MOVE_POLL_MS = 2000;
// A move started from a terminal reports no progress here, only that it is
// running. Asked about no faster than the server re-measures the folders.
const BUSY_POLL_MS = 10_000;
// The finished move the owner has put away, so it is not shown on every visit.
const SEEN_KEY = "framefound.storageMoveSeen";
// A removal of old copies this browser asked for and has had no answer to
// yet, one entry per kind: `{folder, at}`. The server gives no sign that one
// is running, so this is all a page opened again in the middle can go by.
const REMOVING_KEY = "framefound.storageRemoving.";

/** The sentence for a request that failed: the server's own when it gave
 *  one, otherwise what was being tried and that the server did not answer. */
function said(err: unknown, fallback: string): string {
  if (err instanceof ApiError) return refusal(err.status, err.message, fallback);
  return `${fallback} The server did not answer.`;
}

/** A removal that was asked for and not answered: which folder, and when. */
type Unanswered = { folder: string; at: number };

function recallRemoval(kind: string): Unanswered | null {
  try {
    const kept = JSON.parse(window.localStorage.getItem(REMOVING_KEY + kind) ?? "null");
    return kept && typeof kept.folder === "string" && typeof kept.at === "number" ? kept : null;
  } catch {
    return null;
  }
}

function noteRemoval(kind: string, started: Unanswered | null) {
  try {
    if (started) window.localStorage.setItem(REMOVING_KEY + kind, JSON.stringify(started));
    else window.localStorage.removeItem(REMOVING_KEY + kind);
  } catch {
    /* private browsing: a page opened again mid-removal offers the button */
  }
}

/** Whether focus is nowhere: what is left when the control that had it is
 *  taken off the page or disabled. Focus somewhere else was put there by the
 *  reader and is left alone. */
function focusLost(within?: Element | null): boolean {
  const at = document.activeElement;
  return !at || at === document.body || (!!within && within.contains(at));
}

function listed(names: string[]): string {
  return new Intl.ListFormat("en", { style: "long", type: "conjunction" }).format(names);
}

/**
 * A yes-or-no question asked in the page, in place of the buttons that led
 * to it. Focus moves to the question itself, not to either answer, so a
 * second press of Enter cannot say yes to something that was not read.
 */
function Question({
  text,
  yes,
  no,
  danger,
  busy = false,
  onYes,
  onNo,
}: {
  text: string;
  yes: string;
  no: string;
  /** The yes is a deletion. */
  danger?: boolean;
  /** The answer has been sent and the server has not replied yet. */
  busy?: boolean;
  onYes: () => void;
  onNo: () => void;
}) {
  const id = useId();
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    box.current?.focus();
  }, []);
  return (
    <div
      className="storeask"
      role="group"
      aria-labelledby={id}
      tabIndex={-1}
      ref={box}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !busy) {
          event.stopPropagation();
          onNo();
        }
      }}
    >
      <p id={id}>{text}</p>
      <div className="toolbar">
        <button
          type="button"
          className={danger ? "btn" : "btn btn-primary"}
          style={danger ? { borderColor: "var(--ember)", color: "var(--ember)" } : undefined}
          disabled={busy}
          onClick={onYes}
        >
          {busy ? "Working…" : yes}
        </button>
        <button type="button" className="btn" disabled={busy} onClick={onNo}>
          {no}
        </button>
      </div>
    </div>
  );
}

/**
 * Put focus back in the folder field when the form is the reader's again and
 * focus has nowhere to be: a question went away without the form going with
 * it (the answer was no, or yes was refused), or the folder was refused
 * outright. Done after the render, because the field is disabled while a
 * question stands and while the server checks the folder: a disabled field
 * cannot take focus, and the browser drops focus from one that had it. After
 * "that is not a full path" the correction was typed into nothing, and Esc no
 * longer closed the move form, which only hears keys from inside itself.
 */
function useBackToField(question: string | null, busy: boolean, error: string | null) {
  const field = useRef<HTMLInputElement>(null);
  const asked = useRef(false);
  useEffect(() => {
    if (question) asked.current = true;
    else if (asked.current) {
      asked.current = false;
      field.current?.focus();
    }
  }, [question]);
  useEffect(() => {
    if (busy || !error) return;
    if (focusLost(field.current?.form)) field.current?.focus();
  }, [busy, error]);
  return field;
}

/** The move in progress, or the last one and how it ended. */
function MoveCard({
  move,
  oldPath,
  isAdmin,
  pollError,
  focusOnArrival,
  onChange,
  onPutAway,
}: {
  move: MoveStatus;
  /** Where this kind's old copies still are, when the move left some. */
  oldPath: string | null;
  isAdmin: boolean;
  pollError: string | null;
  focusOnArrival: boolean;
  onChange: (next: MoveStatus | null) => void;
  onPutAway: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);

  // A move is started from a line further down the page; the card that
  // follows it appears up here, possibly off screen.
  useEffect(() => {
    if (!focusOnArrival) return;
    heading.current?.scrollIntoView({ block: "center" });
    heading.current?.focus({ preventScroll: true });
  }, [focusOnArrival, move.id]);

  async function cancel() {
    setBusy(true);
    setError(null);
    try {
      onChange(await api.cancelStorageMove());
    } catch (err) {
      setError(said(err, "The move could not be cancelled."));
    } finally {
      setBusy(false);
    }
  }

  const words = phaseWords(move);
  const progress = moveProgress(move);
  const copied = copiedWords(move);
  const name = things(move.label);
  const stopped = move.phase === "failed" || move.phase === "cancelled";
  // What follows the server's reason for a move that stopped: that nothing
  // was changed (its sentence for a cancel already says so), how far it had
  // got, and that asking again carries on.
  const after = [
    !move.error?.includes("Nothing was changed") &&
      (move.source
        ? `Nothing was changed: ${name} still live in ${move.source}.`
        : "Nothing was changed."),
    progress.counts,
    move.phase === "failed" &&
      "Start the move again when the cause is fixed: it carries on from what was copied.",
  ].filter((part): part is string => !!part);

  return (
    <div
      className="card storemove"
      style={{
        borderColor: move.active
          ? "var(--amber-line)"
          : move.phase === "failed"
            ? "var(--ember)"
            : undefined,
      }}
    >
      <div className="storemove-head">
        <h3 ref={heading} tabIndex={-1}>
          {move.active ? `Moving ${name}` : `Last move: ${name}`}
        </h3>
        <span className="pill" data-tone={words.tone}>
          {move.active && <span className="spinner" aria-hidden="true" />}
          {move.cancelling ? "Cancelling" : words.short}
        </span>
      </div>

      <dl className="kv">
        <dt>From</dt>
        <dd className="mono">
          {move.source ?? "nowhere (there was no folder before)"}
          {move.mode === "empty" && move.source && (
            <span className="faint"> (started empty: nothing is copied)</span>
          )}
        </dd>
        <dt>To</dt>
        <dd className="mono">{move.target}</dd>
      </dl>

      {/* Only the sentence is announced: it changes a handful of times in a
          move, the figures every two seconds. For a move that stopped, the
          sentence is the server's reason. */}
      <p
        aria-live="polite"
        style={{
          margin: "14px 0 0",
          color: move.phase === "failed" ? "var(--ember)" : undefined,
        }}
      >
        {move.cancelling
          ? "Stopping. Nothing is switched, and what was copied so far is kept."
          : (stopped && move.error) || words.sentence}
        {move.phase === "done" && move.mode === "empty" && (
          <> It was started empty, so {comesBack(move.kind)}.</>
        )}
      </p>

      {progress.percent !== null && (
        <div
          className="bar"
          role="progressbar"
          aria-label={`Moving ${name}`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress.percent}
        >
          <i style={{ width: `${progress.percent}%` }} />
        </div>
      )}
      {progress.counts && !stopped && (
        <p className="faint mono" style={{ margin: "8px 0 0", fontSize: "0.78rem" }}>
          {progress.counts}
        </p>
      )}

      {stopped && after.length > 0 && <p style={{ margin: "8px 0 0" }}>{after.join(" ")}</p>}

      {move.phase === "done" && move.mode === "copy" && (copied || oldPath) && (
        <p className="muted" style={{ margin: "8px 0 0", fontSize: "0.88rem" }}>
          {copied && <>{copied} </>}
          {oldPath && (
            <>
              The old copies are still in <span className="mono">{oldPath}</span>. When you have
              seen that everything is where it should be, free that space with{" "}
              <strong>Remove old copies</strong> on the {name} line below.
            </>
          )}
        </p>
      )}

      {error && (
        <p role="alert" style={{ color: "var(--ember)", margin: "10px 0 0" }}>
          {error}
        </p>
      )}
      {pollError && move.active && (
        <p role="status" className="faint" style={{ margin: "10px 0 0" }}>
          {pollError} The move itself carries on; this page keeps asking.
        </p>
      )}

      <div className="toolbar" style={{ margin: "14px 0 0" }}>
        {move.active && isAdmin && (
          <button
            type="button"
            className="btn"
            disabled={busy || move.cancelling}
            onClick={cancel}
          >
            {move.cancelling || busy ? "Cancelling…" : "Cancel the move"}
          </button>
        )}
        {!move.active && (
          <button type="button" className="btn" onClick={onPutAway}>
            Put this away
          </button>
        )}
        <span className="faint" style={{ fontSize: "0.8rem" }}>
          Started by {move.requested_by}, {relativeTime(move.requested_at)}
          {move.finished_at && ` · ended ${relativeTime(move.finished_at)}`}
          {move.active && " · you can leave this page; the move carries on"}
        </span>
      </div>
    </div>
  );
}

/** Naming the one folder for everything bulky. */
function BulkFolder({
  data,
  isAdmin,
  mac,
  onSet,
  onRefused,
}: {
  data: StorageKinds;
  isAdmin: boolean;
  mac: boolean;
  onSet: (result: BulkFolderResult) => void;
  onRefused: () => void;
}) {
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [question, setQuestion] = useState<string | null>(null);
  const field = useBackToField(question, busy, error);
  const hintId = useId();

  async function send(confirm: boolean) {
    setBusy(true);
    setError(null);
    try {
      const result = await api.setBulkFolder(path.trim(), confirm);
      setQuestion(null);
      setPath("");
      onSet(result);
    } catch (err) {
      if (err instanceof ApiError && err.status === 428) {
        // A question, not a refusal. Nothing has been made in the folder.
        setQuestion(err.message);
      } else {
        setQuestion(null);
        setError(said(err, "The bulk folder could not be set."));
        if (err instanceof ApiError && err.status === 409) onRefused();
      }
    } finally {
      setBusy(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (path.trim() && !busy && !question) void send(false);
  }

  const form = (
    <form onSubmit={submit}>
      <label className="field" style={{ marginTop: 10 }}>
        <span>{data.bulk_root ? "The new bulk folder" : "Folder for bulk storage"}</span>
        <input
          ref={field}
          className="input mono"
          value={path}
          // Never filled in for the owner: an example they did not type must
          // not be one click from becoming where everything is written.
          placeholder={mac ? "/Volumes/Photos/Framefound" : undefined}
          aria-describedby={hintId}
          autoComplete="off"
          spellCheck={false}
          disabled={busy || !!question || !data.can_move}
          onChange={(event) => {
            setPath(event.target.value);
            setError(null);
          }}
        />
        <small className="faint" id={hintId}>
          The full path of a folder on the big drive, as this server sees it. The drive has to be
          connected; FrameFound makes the last folder of the path if it is not there yet.
        </small>
      </label>
      {error && (
        <p role="alert" style={{ color: "var(--ember)", margin: "10px 0 0" }}>
          {error}
        </p>
      )}
      {question ? (
        <Question
          text={question}
          yes="Yes, go ahead"
          no="No, go back"
          busy={busy}
          onYes={() => void send(true)}
          onNo={() => setQuestion(null)}
        />
      ) : (
        <div className="toolbar" style={{ margin: "14px 0 0" }}>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={busy || !path.trim() || !data.can_move}
          >
            {busy ? "Checking the folder…" : "Use this folder"}
          </button>
        </div>
      )}
    </form>
  );

  return (
    <div className="card" style={{ marginTop: 14 }}>
      <div className="storemove-head">
        <h3>The bulk folder</h3>
        <span className="pill" data-tone={data.bulk_root ? "ok" : undefined}>
          {data.bulk_root ? "Set" : "Not set"}
        </span>
      </div>
      {data.bulk_root ? (
        <div className="pathbox">
          <span title={data.bulk_root}>{data.bulk_root}</span>
        </div>
      ) : (
        <p style={{ margin: 0 }}>
          No bulk folder has been named, so everything below is kept on this machine unless it has
          a folder of its own.
        </p>
      )}
      <p className="faint" style={{ margin: "10px 0 0", fontSize: "0.86rem" }}>
        One folder on a big drive — a NAS share or an external drive — for everything bulky. Each
        kind below gets a folder of its own inside it. <strong>Naming it moves nothing:</strong> a
        kind with no files yet starts there straight away, and a kind that already has files stays
        where it is, still found, until you move it.
      </p>
      {isAdmin &&
        (data.bulk_root ? (
          <details className="disclosure" style={{ marginTop: 12 }}>
            <summary>Use a different folder</summary>
            {form}
          </details>
        ) : (
          form
        ))}
    </div>
  );
}

/** The form under a kind's line: where to move it, and whether to copy. */
function MoveForm({
  kind,
  mac,
  onStarted,
  onClose,
  onRefused,
}: {
  kind: KindStatus;
  mac: boolean;
  onStarted: (move: MoveStatus) => void;
  onClose: () => void;
  onRefused: () => void;
}) {
  const [target, setTarget] = useState(() => suggestedTarget(kind));
  const [empty, setEmpty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [question, setQuestion] = useState<string | null>(null);
  const field = useBackToField(question, busy, error);
  const hintId = useId();
  const name = things(kind.label);

  useEffect(() => {
    field.current?.focus();
  }, [field]);

  async function send(confirm: boolean) {
    setBusy(true);
    setError(null);
    try {
      onStarted(await api.moveKind(kind.kind, target.trim(), empty ? "empty" : "copy", confirm));
    } catch (err) {
      if (err instanceof ApiError && err.status === 428) {
        setQuestion(err.message);
      } else {
        setQuestion(null);
        setError(said(err, "The move could not be started."));
        // What the server has to show has changed: somebody else's move, or
        // this one recorded as not started because the queue is down.
        if (err instanceof ApiError && (err.status === 409 || err.status === 503)) onRefused();
      }
    } finally {
      setBusy(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (target.trim() && !busy && !question) void send(false);
  }

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === "Escape" && !busy) {
      event.stopPropagation();
      onClose();
    }
  }

  // Places the server already knows, offered as one press each.
  const fills = [
    { label: "its place in the bulk folder", path: kind.bulk_path },
    {
      label: "back on this machine",
      path: kind.where === "local" ? null : kind.default_path,
    },
  ].filter(
    (fill): fill is { label: string; path: string } => !!fill.path && fill.path !== target.trim(),
  );

  return (
    <form className="storeform" onSubmit={submit} onKeyDown={onKeyDown}>
      {kind.path && !kind.connected ? (
        // Saying "FrameFound copies them" first would promise what cannot
        // happen: there is nothing to copy from.
        <p style={{ margin: 0, fontSize: "0.86rem" }}>
          The folder the {name} live in is not connected, so they cannot be copied out of it.
          Connect the drive and they are back as they were. If it is gone for good, tick{" "}
          <strong>Start it empty</strong> — the folder can be the same one, on a replacement
          drive.
        </p>
      ) : (
        <p className="faint" style={{ margin: 0, fontSize: "0.86rem" }}>
          {kind.path
            ? `FrameFound copies the ${name} to the new folder while everything keeps working, ` +
              "checks that they all arrived, then switches over. Nothing is deleted: the old " +
              "copies stay where they are until you remove them."
            : "From the next backup on, a copy of each one is kept in this folder as well as on " +
              "this machine."}
        </p>
      )}

      <label className="field">
        <span>{kind.path ? `New folder for ${name}` : "Folder for the second copy"}</span>
        <input
          ref={field}
          className="input mono"
          value={target}
          placeholder={mac ? `/Volumes/Photos/Framefound/${kind.kind}` : undefined}
          aria-describedby={hintId}
          autoComplete="off"
          spellCheck={false}
          disabled={busy || !!question}
          onChange={(event) => {
            setTarget(event.target.value);
            setError(null);
          }}
        />
        <small className="faint" id={hintId}>
          The full path as this server sees it. Its drive has to be connected; FrameFound makes
          the last folder of the path if it is not there yet.
          {fills.map((fill) => (
            <span key={fill.label}>
              {" "}
              <button
                type="button"
                className="linkish"
                disabled={busy || !!question}
                onClick={() => {
                  setTarget(fill.path);
                  setError(null);
                }}
              >
                Fill in {fill.label}
              </button>
            </span>
          ))}
        </small>
      </label>

      {kind.path && (
        <label className="field" data-layout="row">
          <input
            type="checkbox"
            checked={empty}
            disabled={busy || !!question}
            onChange={(event) => setEmpty(event.target.checked)}
          />
          <span>
            <strong>Start it empty.</strong> For a drive that was lost: nothing is copied,
            FrameFound switches to the new folder straight away, and {comesBack(kind.kind)}.
          </span>
        </label>
      )}

      {error && (
        <p role="alert" style={{ color: "var(--ember)", margin: "12px 0 0" }}>
          {error}
        </p>
      )}
      {question ? (
        <Question
          text={question}
          yes="Yes, go ahead"
          no="No, go back"
          busy={busy}
          onYes={() => void send(true)}
          onNo={() => setQuestion(null)}
        />
      ) : (
        <div className="toolbar" style={{ margin: "14px 0 0" }}>
          <button type="submit" className="btn btn-primary" disabled={busy || !target.trim()}>
            {busy
              ? "Checking the folder…"
              : !kind.path
                ? "Keep a copy here"
                : empty
                  ? "Switch to this folder, empty"
                  : "Start the move"}
          </button>
          <button type="button" className="btn" disabled={busy} onClick={onClose}>
            Cancel
          </button>
        </div>
      )}
    </form>
  );
}

/** One kind: where it is, how much, whether it is connected, what can be done. */
function KindRow({
  kind,
  isAdmin,
  canChange,
  mac,
  beingMoved,
  open,
  onOpen,
  onClose,
  onStarted,
  onReload,
}: {
  kind: KindStatus;
  isAdmin: boolean;
  /** An administrator, and nothing is being moved right now. */
  canChange: boolean;
  mac: boolean;
  /** The move in progress is of this kind. */
  beingMoved: boolean;
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
  onStarted: (move: MoveStatus) => void;
  onReload: () => void;
}) {
  // Removing old copies. Kept here, not in a child, because the line that
  // offers it goes when the last old copy does and the result must outlast it.
  const [asking, setAsking] = useState<"copies" | "rest" | null>(null);
  const [removing, setRemoving] = useState(false);
  const [removed, setRemoved] = useState<OldCopiesSaid | null>(null);
  const [cleanError, setCleanError] = useState<string | null>(null);
  // A removal the server was asked for and never answered. It sees one
  // through whatever happens to the connection, so it may well be deleting
  // still. Also what a page opened again in the middle of one starts from.
  const [unanswered, setUnanswered] = useState<Unanswered | null>(null);
  // The folder an unanswered removal turned out to have emptied.
  const [gone, setGone] = useState<string | null>(null);
  // "Make the missing proxies again" (the proxies line only): what the server
  // said, or why it could not be asked.
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildSaid, setRebuildSaid] = useState<{ ok: boolean; text: string } | null>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const copiesButton = useRef<HTMLButtonElement>(null);
  const restButton = useRef<HTMLButtonElement>(null);
  const outcome = useRef<HTMLDivElement>(null);
  // Where focus goes when the question standing on this line goes away.
  const backTo = useRef<"copies" | "rest" | "outcome" | null>(null);
  const name = things(kind.label);

  // A question takes focus when it appears, and the button that opened it is
  // off the page for as long as it stands. When it goes, focus goes back to
  // that button (the answer was no) or to what the line says next (it was
  // yes). It used to be left on nothing, which is the top of the page: a
  // header and a whole section of Tab presses away from this line.
  useEffect(() => {
    if (asking || !backTo.current) return;
    const to = backTo.current;
    backTo.current = null;
    if (!focusLost()) return;
    const target =
      to === "outcome"
        ? outcome.current
        : to === "rest"
          ? restButton.current
          : copiesButton.current;
    (target ?? copiesButton.current ?? opener.current)?.focus();
  }, [asking]);

  // What this line remembers about old copies was said about the two folders
  // it showed at the time. After another move they are two other folders, and
  // the request to remove names neither: the server removes from whichever
  // folder it has on record. A remembered "3 files were left alone" once
  // offered "Remove those 3 too…" for a folder it had never looked at, with
  // the has-a-copy check off. So a change of folder withdraws the question
  // and drops what no longer holds; the line starts again from its first step.
  const shown = useRef({ old: kind.old_path, path: kind.path });
  useEffect(() => {
    const was = shown.current;
    shown.current = { old: kind.old_path, path: kind.path };
    if (was.old === kind.old_path && was.path === kind.path) return;
    backTo.current = "copies";
    setAsking(null);
    setRemoved((kept) =>
      kept && was.path === kind.path && stillAbout(kept, kind.old_path) ? kept : null,
    );
  }, [kind.old_path, kind.path]);

  // A move of this kind has started: its old copies are about to be another
  // folder's, and nothing said about the last ones will hold.
  useEffect(() => {
    if (!beingMoved) return;
    backTo.current = "copies";
    setAsking(null);
    setRemoved(null);
    setCleanError(null);
    setGone(null);
  }, [beingMoved]);

  // A page opened again while a removal asked for from this browser may
  // still be running: the server has no way of saying so, and the button
  // used to be offered as if nothing was happening.
  const recalled = useRef(false);
  useEffect(() => {
    if (recalled.current) return;
    recalled.current = true;
    const started = recallRemoval(kind.kind);
    if (!started) return;
    if (mayStillBeRemoving(started, kind.old_path, Date.now())) setUnanswered(started);
    else noteRemoval(kind.kind, null);
  }, [kind.kind, kind.old_path]);

  // Follow an unanswered removal to its end: look again now and then, and
  // stop when its folder is no longer on record as holding old copies (it
  // finished and nothing was left) or when it has had long enough, after
  // which the line offers the button again.
  useEffect(() => {
    if (!unanswered) return;
    if (unanswered.folder !== kind.old_path) {
      noteRemoval(kind.kind, null);
      setUnanswered(null);
      if (kind.old_path === null) setGone(unanswered.folder);
      return;
    }
    const look = setInterval(() => {
      if (!document.hidden) onReload();
    }, BUSY_POLL_MS);
    const enough = setTimeout(
      () => {
        noteRemoval(kind.kind, null);
        setUnanswered(null);
      },
      Math.max(0, unanswered.at + REMOVAL_WAIT_MS - Date.now()),
    );
    return () => {
      clearInterval(look);
      clearTimeout(enough);
    };
  }, [unanswered, kind.kind, kind.old_path, onReload]);

  async function remove(everything: boolean) {
    const asked = { old: kind.old_path, current: kind.path };
    if (!asked.old) return;
    // The question has been answered; what shows now is that it is working.
    backTo.current = "outcome";
    setAsking(null);
    setRemoving(true);
    setCleanError(null);
    setGone(null);
    try {
      // The yes was for the folders the question named. Somebody else, or a
      // terminal, may have moved this kind since this line was last read, so
      // look once more before asking for anything to be deleted.
      let now: KindStatus | undefined;
      try {
        now = (await api.storageKinds()).kinds.find((row) => row.kind === kind.kind);
      } catch (err) {
        setCleanError(
          said(err, "Could not check where the old copies are just now, so nothing was removed."),
        );
        return;
      }
      if (!now || now.old_path !== asked.old || now.path !== asked.current) {
        setRemoved(null);
        setCleanError(
          `Where ${name} and their old copies are has changed since this was asked, so ` +
            "nothing was removed. This line now shows where they are.",
        );
        return;
      }
      noteRemoval(kind.kind, { folder: asked.old, at: Date.now() });
      try {
        const result = await api.removeOldCopies(kind.kind, everything, asked.old);
        noteRemoval(kind.kind, null);
        setRemoved({ ...result, everything });
      } catch (err) {
        const status = err instanceof ApiError ? err.status : null;
        if (noAnswer(status, err instanceof Error ? err.message : "")) {
          // Not a refusal: the request went and no answer came back. The
          // server removes the files all the same, so "could not be removed"
          // would be false, and offering the button again at once started a
          // second removal over the first.
          const started = { folder: asked.old, at: Date.now() };
          noteRemoval(kind.kind, started);
          setUnanswered(started);
        } else {
          noteRemoval(kind.kind, null);
          setCleanError(said(err, "The old copies could not be removed."));
        }
      }
    } finally {
      setRemoving(false);
      onReload();
    }
  }

  async function rebuild() {
    setRebuilding(true);
    setRebuildSaid(null);
    try {
      const result = await api.rebuildProxies();
      setRebuildSaid({
        ok: true,
        text:
          result.queued === 0
            ? "Every video that should have a proxy has one. Nothing needed making."
            : `${count(result.queued, "proxy", "proxies")} queued to be made. They appear as the worker gets to them.`,
      });
    } catch (err) {
      setRebuildSaid({ ok: false, text: said(err, "The missing proxies could not be queued.") });
    } finally {
      setRebuilding(false);
    }
  }

  function answerNo() {
    backTo.current = asking === "rest" ? "rest" : "copies";
    setAsking(null);
  }

  function close() {
    onClose();
    // Back to the button that opened the form, not to the top of the page.
    setTimeout(() => opener.current?.focus(), 0);
  }

  const away = !kind.connected && kind.where !== "none";
  // Only what is still about the folder this line shows.
  const result = removed && !beingMoved && stillAbout(removed, kind.old_path) ? removed : null;
  // The first pass left files that have no copy. What is offered next is the
  // second, explicit step for those, not the first one over again.
  const leftovers = leftBehind(result, kind.old_path);
  const waiting = !!unanswered && unanswered.folder === kind.old_path;

  return (
    <li className="storekind">
      <div className="storekind-what">
        <h3>{kind.label}</h3>
        <div className="storekind-pills">
          <span className="pill">{WHERE_WORDS[kind.where]}</span>
          {away && (
            <span className="pill" data-tone="bad">
              {kind.where === "local" ? "Not available" : "Not connected"}
            </span>
          )}
          {kind.connected && kind.where !== "local" && (
            <span className="pill" data-tone="ok">
              Connected
            </span>
          )}
        </div>
        <p className="faint">{kind.description}</p>
      </div>

      <dl className="kv storekind-facts">
        <dt>Folder</dt>
        <dd className={kind.path ? "mono" : "faint"}>
          {kind.path ?? "No folder chosen, so no second copy is kept."}
        </dd>
        <dt>Holds</dt>
        <dd>
          {kind.files === null
            ? kind.where === "none"
              ? "—"
              : "Not known while its drive is away"
            : kind.files === 0
              ? "Nothing yet"
              : `${count(kind.files, "file")} · ${diskSize(kind.size_bytes)}`}
        </dd>
        <dt>Free space</dt>
        <dd>
          {kind.free_gb === null
            ? "—"
            : `${diskSize(gbToBytes(kind.free_gb))} free of ${diskSize(gbToBytes(kind.total_gb))}`}
        </dd>
      </dl>

      <div className="storekind-do">
        {kind.movable && isAdmin && (
          <button
            ref={opener}
            type="button"
            className="btn"
            aria-expanded={open}
            disabled={!canChange && !open}
            onClick={open ? close : onOpen}
          >
            {kind.path ? "Move" : "Choose a folder"}
          </button>
        )}
        {kind.kind === "grabs" && <span className="faint">Always on this machine</span>}
        {kind.kind === "proxies" && isAdmin && (
          <button
            type="button"
            className="btn"
            disabled={rebuilding || beingMoved || away}
            title={
              away
                ? "The proxy folder is not connected"
                : "Queue a proxy for every video that should have one and does not"
            }
            onClick={() => void rebuild()}
          >
            {rebuilding ? "Looking…" : "Make the missing proxies again"}
          </button>
        )}
      </div>
      {rebuildSaid && (
        <p
          className="storekind-wide"
          role={rebuildSaid.ok ? "status" : "alert"}
          style={rebuildSaid.ok ? undefined : { color: "var(--ember)" }}
        >
          {rebuildSaid.text}
        </p>
      )}

      {kind.problem && (
        <p className="storekind-wide" role="status" style={{ color: "var(--ember)" }}>
          {kind.problem}
          {kind.where !== "local" && (
            <span className="faint"> Only {name} are affected; everything else carries on.</span>
          )}
        </p>
      )}

      {kind.old_path && !asking && !removing && !waiting && leftovers === 0 && (
        <div className="storekind-wide storekind-old">
          <span>
            Old copies from the last move are still in{" "}
            <span className="mono">{kind.old_path}</span>.
          </span>
          {isAdmin && (
            <button
              ref={copiesButton}
              type="button"
              className="btn"
              style={{ padding: "4px 10px" }}
              disabled={beingMoved}
              onClick={() => {
                setCleanError(null);
                setGone(null);
                setAsking("copies");
              }}
            >
              Remove old copies
            </button>
          )}
        </div>
      )}
      {asking === "copies" && kind.old_path && (
        <div className="storekind-wide">
          <Question
            text={
              `This deletes the old copies of ${name} in ${kind.old_path}. Only files that ` +
              `have a copy in the folder now in use (${kind.path ?? "none"}) are removed, and ` +
              "the files in use are not touched. It cannot be undone."
            }
            yes="Remove old copies"
            no="Keep them"
            danger
            onYes={() => void remove(false)}
            onNo={answerNo}
          />
        </div>
      )}
      {/* Everything the line says back about old copies, in one box that stays
          on the page from the yes until the answer: focus waits in it. */}
      {(removing || waiting || cleanError || gone || (result && !asking)) && (
        <div
          className="storekind-wide storekind-said"
          role="group"
          aria-label={`Old copies of ${name}`}
          tabIndex={-1}
          ref={outcome}
        >
          {removing && (
            <p className="storekind-old" role="status">
              <span className="spinner" aria-hidden="true" />
              <span>
                Removing the old copies. On a network drive this can take a few minutes.
              </span>
            </p>
          )}
          {waiting && !removing && unanswered && (
            <p className="storekind-old" role="status">
              <span className="spinner" aria-hidden="true" />
              <span>
                The answer about removing the old copies in{" "}
                <span className="mono">{unanswered.folder}</span> never reached this page. That
                does not mean it failed: the server carries on by itself, and on a network drive
                it can take a few minutes. This page keeps looking and will say when they are
                gone.
              </span>
            </p>
          )}
          {cleanError && (
            <p role="alert" style={{ color: "var(--ember)" }}>
              {cleanError}
            </p>
          )}
          {gone && (
            <p role="status">
              The old copies in <span className="mono">{gone}</span> have been removed.
            </p>
          )}
          {result && !asking && !removing && (
            <div role="status">
              <p>
                {result.files === 0
                  ? `Nothing was removed from ${result.folder}.`
                  : `Removed ${
                      result.everything
                        ? count(result.files, "file")
                        : count(result.files, "old copy", "old copies")
                    } (${diskSize(result.bytes)}) from ${result.folder}.`}{" "}
                {result.kept > 0 &&
                  (result.everything
                    ? `${count(result.kept, "file")} could not be removed and ` +
                      `${result.kept === 1 ? "is" : "are"} still there.`
                    : `${count(result.kept, "file")} there ${result.kept === 1 ? "has" : "have"} ` +
                      "no copy in the folder now in use, so " +
                      `${result.kept === 1 ? "it was" : "they were"} left alone.`)}
              </p>
              {leftovers > 0 && isAdmin && !waiting && (
                <button
                  ref={restButton}
                  type="button"
                  className="btn"
                  style={{ padding: "4px 10px", marginTop: 8 }}
                  disabled={beingMoved}
                  onClick={() => setAsking("rest")}
                >
                  Remove {leftovers === 1 ? "that one" : `those ${leftovers.toLocaleString("en-US")}`}{" "}
                  too…
                </button>
              )}
            </div>
          )}
        </div>
      )}
      {/* Asked about the folder this line shows now, never about one it
          remembers: it is the folder the server will empty. */}
      {asking === "rest" && leftovers > 0 && kind.old_path && (
        <div className="storekind-wide">
          <Question
            text={
              `This empties ${kind.old_path} of ${name}. The ${count(leftovers, "file")} left ` +
              `there ${leftovers === 1 ? "has" : "have"} no copy in the folder now in use ` +
              `(${kind.path ?? "none"}), so deleting ${leftovers === 1 ? "it" : "them"} deletes ` +
              "the only copy FrameFound knows of. It cannot be undone."
            }
            yes={leftovers === 1 ? "Delete it" : "Delete them"}
            no={leftovers === 1 ? "Keep it" : "Keep them"}
            danger
            onYes={() => void remove(true)}
            onNo={answerNo}
          />
        </div>
      )}

      {open && (
        <div className="storekind-wide">
          <MoveForm
            kind={kind}
            mac={mac}
            onStarted={onStarted}
            onClose={close}
            onRefused={onReload}
          />
        </div>
      )}
    </li>
  );
}

export default function BulkStorage() {
  const [data, setData] = useState<StorageKinds | null>(null);
  // Null until the server has said who is looking: nobody is told they
  // cannot change anything on the strength of an answer not yet in.
  const [role, setRole] = useState<string | null>(null);
  // Asked, and no answer: said on the page, because without a role there are
  // no buttons, and a page with no buttons and no reason reads as broken.
  const [roleUnread, setRoleUnread] = useState(false);
  const roleKnown = useRef(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  // The freshest word on the move: from the last full read, or from the
  // two-second question while one is running.
  const [move, setMove] = useState<MoveStatus | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [openKind, setOpenKind] = useState<string | null>(null);
  const [putAway, setPutAway] = useState<string | null>(null);
  const [justStarted, setJustStarted] = useState<string | null>(null);
  // A read can take ten seconds when a drive is slow to answer; a later one
  // must not be overwritten by an earlier one that comes back after it.
  const ticket = useRef(0);
  // The finished move whose answer came with the list of kinds from before
  // it finished, and was read once more for that reason. Once per move: a
  // kind moved again since, from a terminal, would otherwise be re-read for
  // ever.
  const readAgainFor = useRef<string | null>(null);

  // Who is looking decides which buttons exist. The server refuses a write
  // from anyone else whatever the page shows.
  const askRole = useCallback(async () => {
    try {
      const me = await api.me();
      roleKnown.current = true;
      setRole(me.role);
      setRoleUnread(false);
    } catch {
      if (!roleKnown.current) setRoleUnread(true);
    }
  }, []);

  const load = useCallback(async () => {
    // One failed answer about the account used to leave an administrator
    // with no buttons for the life of the page, a running move's Cancel
    // among them. Every read of the page asks again until it is known.
    if (!roleKnown.current) void askRole();
    const mine = ++ticket.current;
    try {
      let next = await api.storageKinds();
      if (mine !== ticket.current) return;
      if (next.move && layoutBehind(next) && readAgainFor.current !== next.move.id) {
        // "Finished" above a line still on its old folder, and "not
        // connected" if that folder was a lost drive: it read as a move that
        // had failed. Show what came, and read the lines again.
        readAgainFor.current = next.move.id;
        setData(next);
        setMove(next.move);
        next = await api.storageKinds();
        if (mine !== ticket.current) return;
      }
      setData(next);
      setMove(next.move);
      setLoadError(null);
    } catch (err) {
      if (mine !== ticket.current) return;
      setLoadError(said(err, "Could not read where the bulky files live."));
    }
  }, [askRole]);

  useEffect(() => {
    void load();
    try {
      setPutAway(window.localStorage.getItem(SEEN_KEY));
    } catch {
      /* private browsing: the last move is simply shown again */
    }
  }, [load]);

  // Follow a running move. Only while one runs, and not while the tab is
  // hidden; coming back to the tab asks at once.
  const active = move?.active ?? false;
  useEffect(() => {
    if (!active) return;
    let live = true;
    let asking = false;
    const tick = async () => {
      if (document.hidden || asking) return;
      asking = true;
      try {
        const next = await api.storageMove();
        if (!live) return;
        setPollError(null);
        setMove(next);
      } catch {
        // Whatever went wrong with the question, the answer is the same.
        if (live) setPollError("Could not read how the move is going just now.");
      } finally {
        asking = false;
      }
    };
    const timer = setInterval(tick, MOVE_POLL_MS);
    const onVisible = () => {
      if (!document.hidden) void tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      live = false;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [active]);

  // A move has ended: where everything lives may have changed, so the lines
  // are read again, whoever noticed. It used to be only the two-second
  // question above that did this. When a full read was first to see the end
  // (one is made the moment a move starts, and a move started empty is over
  // in under a second) the two-second question stopped without having seen
  // it, and the lines stayed as they were before the switch.
  const wasActive = useRef(false);
  useEffect(() => {
    if (wasActive.current && !active) void load();
    wasActive.current = active;
  }, [active, load]);

  const reload = useCallback(() => void load(), [load]);

  // Coming back to a tab left open: somebody else, or a terminal, may have
  // moved something since. (A running move is asked about by the effect
  // above; this is everything else on the page.)
  useEffect(() => {
    const onVisible = () => {
      if (!document.hidden) void load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [load]);

  // A move started from a terminal: all the page knows is that it runs, so
  // it looks again now and then to find out when it has stopped.
  const busyElsewhere = !!data?.busy;
  useEffect(() => {
    if (!busyElsewhere) return;
    const timer = setInterval(() => {
      if (!document.hidden) void load();
    }, BUSY_POLL_MS);
    return () => clearInterval(timer);
  }, [busyElsewhere, load]);

  function started(next: MoveStatus) {
    setOpenKind(null);
    setNotice(null);
    setMove(next);
    setJustStarted(next.id);
    void load();
  }

  function bulkSet(result: BulkFolderResult) {
    const staying = result.staying.map((kind, at) => {
      const label = data?.kinds.find((row) => row.kind === kind)?.label ?? kind;
      // The first one starts the sentence.
      return at === 0 ? label : things(label);
    });
    setNotice(
      staying.length
        ? `${result.bulk_root} is now the bulk folder. Nothing was moved. ${listed(staying)} ` +
            `already ${staying.length === 1 ? "has" : "have"} files, so ` +
            `${staying.length === 1 ? "it stays" : "they stay"} where ` +
            `${staying.length === 1 ? "it is" : "they are"} until you move ` +
            `${staying.length === 1 ? "it" : "them"}, below. A kind with no files yet starts in ` +
            "the bulk folder from now on."
        : `${result.bulk_root} is now the bulk folder. Nothing had to be moved: every kind with ` +
            "no files yet starts there from now on.",
    );
    void load();
  }

  function hideMove(id: string) {
    setPutAway(id);
    try {
      window.localStorage.setItem(SEEN_KEY, id);
    } catch {
      /* it comes back on the next visit; nothing is lost */
    }
  }

  const summary = data ? kindsSummary(data.kinds) : null;
  const mac = looksLikeMac(
    data ? [data.bulk_root, ...data.kinds.flatMap((kind) => [kind.path, kind.default_path])] : [],
  );
  const isAdmin = role === "admin";
  const canChange = isAdmin && !!data?.can_move && !active;
  const showMove = move && (move.active || move.id !== putAway);

  return (
    <>
      <div className="sectionhead">
        <h2>Where the bulky files live</h2>
        {summary && (
          <span className="faint mono" style={summary.bad ? { color: "var(--ember)" } : undefined}>
            {summary.text}
          </span>
        )}
      </div>

      <p className="faint" style={{ margin: "0 0 14px", fontSize: "0.9rem", maxWidth: "78ch" }}>
        Most of what FrameFound makes is small and stays on this machine, where it is fast. These
        kinds are large and seldom read, so each can live on a bigger drive instead. Your originals
        are never moved or changed by anything on this page.
      </p>

      {loadError && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)", marginBottom: 14 }}>
          {loadError}{" "}
          <button type="button" className="linkish" onClick={() => void load()}>
            Try again
          </button>
        </div>
      )}

      {role === null && roleUnread && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)", marginBottom: 14 }}>
          Could not check whether this account may change anything here, so the buttons for
          moving, cancelling and removing are not shown.{" "}
          <button type="button" className="linkish" onClick={() => void askRole()}>
            Try again
          </button>
        </div>
      )}

      {!data && !loadError && <div className="empty">Reading where everything lives…</div>}

      {data && (
        <>
          {data.problem && (
            <div
              className="card"
              role="alert"
              style={{ borderColor: "var(--ember)", marginBottom: 14 }}
            >
              {data.problem} Nothing can be moved until this is put right.
            </div>
          )}
          {data.busy && (
            <div
              className="card"
              role="status"
              style={{ borderColor: "var(--amber-line)", marginBottom: 14 }}
            >
              {data.busy} It was not started from this page, so its progress is not shown here;
              this page will notice when it has finished.
            </div>
          )}
          {showMove && move && (
            <MoveCard
              move={move}
              oldPath={data.kinds.find((kind) => kind.kind === move.kind)?.old_path ?? null}
              isAdmin={isAdmin}
              pollError={pollError}
              focusOnArrival={justStarted === move.id}
              onChange={setMove}
              onPutAway={() => hideMove(move.id)}
            />
          )}
          {notice && (
            <div
              className="card"
              role="status"
              style={{ borderColor: "var(--sage)", marginTop: 14 }}
            >
              {notice}
            </div>
          )}

          <BulkFolder
            data={data}
            isAdmin={isAdmin}
            mac={mac}
            onSet={bulkSet}
            onRefused={reload}
          />

          {role !== null && !isAdmin && (
            <p className="faint" style={{ margin: "14px 0 0", fontSize: "0.86rem" }}>
              You can see where everything lives. Changing it needs an administrator account.
            </p>
          )}
          {isAdmin && active && (
            <p className="faint" style={{ margin: "14px 0 0", fontSize: "0.86rem" }}>
              One move runs at a time. The others can be moved when this one has finished or been
              cancelled.
            </p>
          )}

          <ul className="card storekinds" aria-label="Kinds of bulky file">
            {data.kinds.map((kind) => (
              <KindRow
                key={kind.kind}
                kind={kind}
                isAdmin={isAdmin}
                canChange={canChange}
                mac={mac}
                beingMoved={active && move?.kind === kind.kind}
                open={openKind === kind.kind}
                onOpen={() => setOpenKind(kind.kind)}
                onClose={() => setOpenKind(null)}
                onStarted={started}
                onReload={reload}
              />
            ))}
          </ul>

          <p className="faint" style={{ margin: "12px 0 0", fontSize: "0.82rem" }}>
            Whether each drive is reachable right now is also on the{" "}
            <Link href="/health" style={{ color: "var(--amber)", textDecoration: "underline" }}>
              System page
            </Link>
            , and a drive that goes away raises a line at the top of every page.
          </p>
        </>
      )}
    </>
  );
}
