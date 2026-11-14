"use client";

// Look without leaving: an in-page viewer for one item of a list.
//
// When bulk selection reached the Tags review (2026-11-05) the picture became
// a select toggle, and the one way to open the footage went with it. "Is this
// Power Broom?" about a long auction clip can only be answered by playing it,
// and the route that was left — the filename — navigated away and threw the
// open queue and the selection away. This viewer opens over the page instead,
// so looking costs nothing the page was holding.
//
// It is a native modal <dialog>. The browser keeps Tab inside it, makes the
// page behind inert, and draws it in the top layer — a tile's `overflow:
// hidden` and hover `transform` would clip or re-anchor a fixed overlay
// rendered inside it. Mount it once, beside the grid, never inside a tile.
//
// The keys live here rather than in each page, so every page that mounts the
// viewer steps and answers the same way: ← / → step, Esc closes, and — only
// when the page passes the callbacks — Y / N answer and Space selects. A key
// used here is marked (preventDefault, as the Manage menu marks its Esc) and
// goes no further, so a page listening for the same keys does not act on the
// grid underneath as well.

import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type MouseEvent,
  type ReactNode,
  type RefObject,
} from "react";
import Link from "next/link";

import Thumb from "@/components/Thumb";
import { mediaStatus, mediaUrl, type MediaType } from "@/lib/api";
import { usePlayerSource } from "@/lib/hooks";
import { seekOnChange, type Moment } from "@/lib/seek";

// Sound, once the reviewer turns it on, stays on for the next clip: nobody
// listening to auction footage should have to unmute every one. Off to begin
// with, because a browser only starts a clip by itself when it is muted.
let soundOn = false;

export interface MediaPreviewProps {
  assetId: string;
  mediaType: MediaType;
  filename: string;
  /** Seconds into a clip to start at, for a list that knows the moment. A
   *  new value on the same clip moves the clip there. */
  startAt?: number;
  /** What the moment belongs to (a face, a shot). A new key at the same
   *  second moves the clip back to it, e.g. two faces found in one frame. */
  momentKey?: string;
  /** What is being asked about this item, e.g. "Is this Power Broom?". */
  heading?: string;
  /** A short line about this item, e.g. who suggested it. */
  detail?: string;
  /** Where this item is in the list, e.g. "3 of 41". */
  position?: string;
  /** What the last answer did; read out when it changes. */
  status?: string | null;
  /** Why the last answer was not saved. Shown as an error: the page's own
   *  message is behind the viewer, and an unnoticed failure reads as a Yes. */
  problem?: string | null;
  /** An answer is being saved: Yes and No wait for it. */
  busy?: boolean;
  onClose: () => void;
  /** Left arrow. Leave out at the first item. */
  onPrev?: () => void;
  /** Right arrow. Leave out at the last item. */
  onNext?: () => void;
  /** Y. The page saves the answer and decides what is shown next. */
  onYes?: () => void;
  /** N. */
  onNo?: () => void;
  yesLabel?: string;
  noLabel?: string;
  /** Space: add this item to, or take it out of, the page's selection. */
  onSelect?: () => void;
  selected?: boolean;
  /** Where focus goes on closing; by default, back to what opened the
   *  viewer. A page that steps through a list returns the tile of the item
   *  last on screen, so the reader comes back to where they were looking. */
  returnFocus?: () => HTMLElement | null;
  /** Further controls for the action row. */
  children?: ReactNode;
}

const NOT_PLAYABLE = "This clip cannot be played here right now.";

interface PlayerProps {
  assetId: string;
  startAt?: number;
  momentKey?: string;
  onFail: () => void;
}

function startWhereAsked(player: HTMLMediaElement, startAt?: number) {
  if (startAt && startAt > 0) player.currentTime = startAt;
}

/**
 * Moves a player that already holds its clip when the page asks for another
 * moment of it. A clip still loading needs nothing: `onLoadedMetadata` reads
 * the newest `startAt` when it fires. Another item is another player (the
 * stage is keyed by asset), which starts where asked by itself.
 */
function useFollowMoment(
  player: RefObject<HTMLMediaElement | null>,
  startAt: number | undefined,
  momentKey: string | undefined,
) {
  const shown = useRef<Moment>({ at: startAt, key: momentKey });
  useEffect(() => {
    const now = { at: startAt, key: momentKey };
    const to = seekOnChange(shown.current, now);
    shown.current = now;
    const el = player.current;
    if (to === null || !el || el.readyState < HTMLMediaElement.HAVE_METADATA) return;
    el.currentTime = to;
  }, [player, startAt, momentKey]);
}

// The two players are components of their own so that each is emptied the
// moment it leaves the stage — on stepping to the next item, and when it
// gives way to the "cannot be played" still. Stepping used to drop a playing
// <video> with its request still open; a queue answered at a clip a second
// ran the browser out of connections, and the viewer sat on "Saving…" behind
// streams nobody was watching (see usePlayerSource).

function ClipPlayer({ assetId, startAt, momentKey, onFail }: PlayerProps) {
  const player = usePlayerSource<HTMLVideoElement>(mediaUrl(assetId, "proxy"));
  useFollowMoment(player, startAt, momentKey);
  return (
    <video
      ref={player}
      controls
      autoPlay
      playsInline
      muted={!soundOn}
      preload="metadata"
      poster={mediaUrl(assetId, "poster")}
      onLoadedMetadata={(event) => startWhereAsked(event.currentTarget, startAt)}
      onVolumeChange={(event) => {
        soundOn = !event.currentTarget.muted && event.currentTarget.volume > 0;
      }}
      onError={onFail}
    >
      <track kind="captions" src={mediaUrl(assetId, "subtitle")} default />
    </video>
  );
}

function SoundPlayer({ assetId, startAt, momentKey, onFail }: PlayerProps) {
  const player = usePlayerSource<HTMLAudioElement>(mediaUrl(assetId, "proxy"));
  useFollowMoment(player, startAt, momentKey);
  return (
    <audio
      ref={player}
      controls
      onLoadedMetadata={(event) => startWhereAsked(event.currentTarget, startAt)}
      onError={onFail}
    />
  );
}

/**
 * The picture or the player for one item. Keyed by asset in the parent, so a
 * clip that failed to load does not leave its apology on the next one.
 */
function Stage({
  assetId,
  mediaType,
  filename,
  startAt,
  momentKey,
  fullPage,
}: {
  assetId: string;
  mediaType: MediaType;
  filename: string;
  startAt?: number;
  momentKey?: string;
  fullPage: string;
}) {
  const [missing, setMissing] = useState<string | null>(null);
  const [arrived, setArrived] = useState(false);
  // False once this item has left the stage. A player being emptied on its
  // way out, or a status that comes back after the reader has stepped on,
  // must not be reported as a clip that would not play.
  const onStage = useRef(true);
  useEffect(() => {
    onStage.current = true;
    return () => {
      onStage.current = false;
    };
  }, []);

  // The player only says "error". The server knows which of two different
  // things happened: no playable copy was ever made (a library with proxies
  // off, or a clip still in the queue), or the copies exist and the drive
  // that holds them is away. "It can be regenerated" is the wrong thing to
  // say about the second, so ask before saying anything specific.
  async function cannotPlay() {
    if (!onStage.current) return;
    setMissing(NOT_PLAYABLE);
    const status = await mediaStatus(assetId, "proxy");
    if (!onStage.current) return;
    if (status === 503) {
      setMissing(
        "The drive that holds the playable copies is not connected, so this clip cannot be played right now.",
      );
    } else if (status === 404) {
      setMissing("No playable copy of this clip has been made yet.");
    } else if (status >= 200 && status < 300) {
      // The file is there and was served; the player still refused it.
      setMissing("This browser could not play the clip.");
    }
    // Anything else (signed out, offline): the general sentence stands.
  }

  if (missing) {
    // Never a dead black box: the still that does exist, and a plain
    // sentence about what does not.
    const still =
      mediaType === "video" ? "poster" : mediaType === "audio" ? "waveform" : "thumbnail";
    return (
      <>
        <Thumb assetId={assetId} mediaType={mediaType} status="ready" kind={still} />
        <p className="preview-note" role="status">
          {missing}{" "}
          <Link href={fullPage} target="_blank" rel="noreferrer" prefetch={false}>
            Open the full page
          </Link>{" "}
          for everything known about it.
        </p>
      </>
    );
  }

  if (mediaType === "video") {
    return (
      <ClipPlayer
        assetId={assetId}
        startAt={startAt}
        momentKey={momentKey}
        onFail={cannotPlay}
      />
    );
  }

  if (mediaType === "audio") {
    return (
      <div className="preview-audio">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={mediaUrl(assetId, "waveform")}
          alt=""
          onError={(event) => {
            event.currentTarget.style.display = "none";
          }}
        />
        <SoundPlayer
          assetId={assetId}
          startAt={startAt}
          momentKey={momentKey}
          onFail={cannotPlay}
        />
      </div>
    );
  }

  return (
    <>
      {/* Over a slow link the large still takes a moment, and an empty
          stage reads as a picture that is not there. */}
      {!arrived && <span className="spinner" aria-hidden="true" />}
      {/* Served by our own session-guarded endpoint, so the Next image loader
          (which would proxy and re-encode it) is deliberately bypassed. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={mediaUrl(assetId, "preview")}
        alt={filename}
        onLoad={() => setArrived(true)}
        onError={() => setMissing("The large version of this picture is not available.")}
      />
    </>
  );
}

export default function MediaPreview(props: MediaPreviewProps) {
  const {
    assetId,
    mediaType,
    filename,
    startAt,
    momentKey,
    heading,
    detail,
    position,
    status,
    problem,
    busy = false,
    onClose,
    onPrev,
    onNext,
    onYes,
    onNo,
    yesLabel = "Yes",
    noLabel = "No",
    onSelect,
    selected = false,
    children,
  } = props;

  const dialogRef = useRef<HTMLDialogElement>(null);
  const titleRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  // The listeners below are bound once for the life of the viewer; the
  // page's callbacks change with every render and are read through this.
  // A layout effect, so a key pressed straight after an answer is saved
  // already sees the next clip's callbacks and not the last one's.
  const latest = useRef(props);
  useLayoutEffect(() => {
    latest.current = props;
  });
  // close() calls of our own (see the cleanup below), whose "close" events
  // must not be taken for the reader asking to close.
  const ownCloses = useRef(0);
  const pressedBackdrop = useRef(false);

  // Open as a modal, and on the way out put focus back where the reader
  // was. Removing an open dialog does not restore focus by itself, and a
  // keyboard user would be dropped at the top of the page.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!dialog.open) dialog.showModal();
    // Focus starts on the title, not the first button: Space then selects
    // rather than pressing whatever control happened to come first.
    titleRef.current?.focus();
    return () => {
      if (dialog.open) {
        ownCloses.current += 1;
        dialog.close();
      }
      const target = latest.current.returnFocus?.() ?? opener;
      if (target?.isConnected) target.focus();
    };
  }, []);

  // A clip that holds focus (after a click on its player) leaves the page
  // when the next one arrives, and focus would fall to the document with it.
  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog?.open && !dialog.contains(document.activeElement)) titleRef.current?.focus();
  }, [assetId]);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      // A clip playing full screen: Esc belongs to the browser, to leave it.
      if (document.fullscreenElement) return;
      const now = latest.current;
      const used = () => {
        event.preventDefault();
        event.stopPropagation();
      };
      const key = event.key.toLowerCase();
      if (key === "escape") {
        used();
        now.onClose();
        return;
      }
      const el = event.target instanceof HTMLElement ? event.target : null;
      if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
      // A focused player keeps its own keys: the arrows scrub and Space
      // pauses, which is half of what looking at footage is.
      const inPlayer = el !== null && /^(VIDEO|AUDIO)$/.test(el.tagName);
      if (key === "arrowleft" || key === "arrowright") {
        if (inPlayer) return;
        used();
        (key === "arrowleft" ? now.onPrev : now.onNext)?.();
      } else if (key === "y" || key === "n") {
        used();
        // A held key repeats, and each repeat would answer the next clip
        // as soon as the last answer was saved.
        if (event.repeat || now.busy) return;
        (key === "y" ? now.onYes : now.onNo)?.();
      } else if (key === " ") {
        // Space presses a focused button or link, as it does everywhere.
        if (inPlayer || el?.closest("button, a, summary")) return;
        if (!now.onSelect) return;
        used();
        if (!event.repeat) now.onSelect();
      }
    }
    // Capture, on the document: the keys must work wherever focus is inside
    // the viewer, and reach here before any page-level listener.
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, []);

  function onClick(event: MouseEvent<HTMLDialogElement>) {
    if (event.target === event.currentTarget) {
      // The dialog has no padding, so a click on the element itself is a
      // click on the backdrop. Only when the press began there too: letting
      // go of the player's scrubber outside the box must not close it.
      if (pressedBackdrop.current) onClose();
      return;
    }
    // A mouse click leaves focus on the button without showing a ring, so
    // the next Space — meant as "select" — would press it again: a click on
    // Yes, then Space, says Yes to the following clip. Focus goes back to
    // the title instead. Keyboard activation (detail 0) keeps focus where
    // the reader put it.
    if (event.detail > 0 && event.target instanceof Element && event.target.closest("button")) {
      titleRef.current?.focus();
    }
  }

  const fullPage =
    startAt && startAt > 0 ? `/assets/${assetId}?t=${Math.floor(startAt)}` : `/assets/${assetId}`;
  const about = [heading ? filename : null, position, detail].filter(Boolean).join(" · ");
  const keys = [
    (onPrev || onNext) && "← → step",
    (onYes || onNo) && "Y / N answer",
    onSelect && "Space selects",
    "Esc closes",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <dialog
      ref={dialogRef}
      className="preview"
      aria-labelledby={titleId}
      onCancel={(event) => {
        // Esc is handled above; this is any other request to close (the
        // back gesture on a phone). The page's state decides, not the DOM.
        event.preventDefault();
        onClose();
      }}
      onClose={() => {
        if (ownCloses.current > 0) ownCloses.current -= 1;
        else onClose();
      }}
      onMouseDown={(event) => {
        pressedBackdrop.current = event.target === event.currentTarget;
      }}
      onClick={onClick}
    >
      <header className="preview-head">
        <div className="preview-title" ref={titleRef} tabIndex={-1}>
          <h2 id={titleId}>{heading ?? filename}</h2>
          {/* Cut to one row on a wide screen; its own words as a title so
              the rest can be read on hover. A phone wraps it (globals.css),
              since touch has no hover. */}
          {about && (
            <p className="faint mono" title={about}>
              {about}
            </p>
          )}
        </div>
        {/* A new tab, so the page underneath keeps its queue and selection;
            not prefetched, or stepping through forty clips fetched forty. */}
        <Link
          className="preview-open"
          href={fullPage}
          target="_blank"
          rel="noreferrer"
          prefetch={false}
        >
          Open full page ↗
        </Link>
        <button type="button" className="btn" onClick={onClose} title="Esc">
          Close
        </button>
      </header>

      <div className="preview-stage" data-kind={mediaType}>
        <Stage
          key={assetId}
          assetId={assetId}
          mediaType={mediaType}
          filename={filename}
          startAt={startAt}
          momentKey={momentKey}
          fullPage={fullPage}
        />
      </div>

      <footer className="preview-bar">
        <div className="preview-actions">
          {onYes && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              onClick={onYes}
              title="Y"
            >
              {yesLabel}
            </button>
          )}
          {onNo && (
            <button type="button" className="btn" disabled={busy} onClick={onNo} title="N">
              {noLabel}
            </button>
          )}
          {onSelect && (
            <button
              type="button"
              className="btn"
              aria-pressed={selected}
              onClick={onSelect}
              title="Space"
            >
              {selected ? "✓ Selected" : "Select"}
            </button>
          )}
          {children}
        </div>
        {(onPrev || onNext) && (
          <div className="preview-steps">
            <button
              type="button"
              className="btn"
              disabled={!onPrev}
              onClick={onPrev}
              title="Left arrow"
            >
              ‹ Previous
            </button>
            <button
              type="button"
              className="btn"
              disabled={!onNext}
              onClick={onNext}
              title="Right arrow"
            >
              Next ›
            </button>
          </div>
        )}
        <p className="preview-foot faint">
          <span>
            <span role="status">{busy ? "Saving…" : problem ? null : status}</span>
            {problem && !busy && (
              <span role="alert" className="preview-problem">
                {problem}
              </span>
            )}
          </span>
          <span className="pointer-hint">{keys}</span>
        </p>
      </footer>
    </dialog>
  );
}
