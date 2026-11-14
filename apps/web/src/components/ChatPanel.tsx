"use client";

// The chat panel: Claude, beside whatever page is open.
//
// It is mounted once, from app/layout.tsx, and wraps every page. Each page
// renders its own Shell, so anything inside a page is thrown away on
// navigation; this is not. The conversation itself lives further out still,
// in the module store (lib/chatStore.ts), so a turn that is running keeps
// being read while pages change underneath and while the panel is closed.
// This file only draws that store and passes on what the reader does.
//
// Two shapes, by the width of the window:
//
// - Wide (1200 px and up): a column down the right-hand side, under the
//   site header. The page is narrowed to make room for it rather than
//   covered, so a listing's rail or a queue's answer bar is still there to
//   use with the panel open. Nothing about the page is locked.
// - Narrower: a sheet over the page, full height (full width on a phone).
//   The page behind is made inert while it is up — it cannot be seen, so it
//   must not be reachable with Tab either — and gets its focus back on close.
//
// Closed, the panel is not in the page at all: no element, no listener, no
// request. The one thing it adds to a closed page is the "Ask Claude" button
// (ChatButton, below), which the Shell puts in the header.
//
// Keys. Tags and People answer a selection with bare Y / N / Esc, listening
// on the window. A key pressed inside the panel belongs to the panel, so the
// panel stops it there: Y on a focused Approve button must not also say Yes
// to forty tiles behind it. The look-without-leaving viewer is a modal in the
// top layer and takes its keys first (capture, on the document), so the two
// never compete; while it is open the panel is inert like everything else.
// Three more ways a key used to get past the panel to the page, closed here:
// focus falling to the page's body when the focused control went away
// (`keepFocus`, and the panel's window listener for whatever is left), the
// panel's own viewers being outside the panel's element (they sit in a
// [data-chat-panel] wrapper of their own), and the plain picture dialog
// only hearing keys while focus was inside it (`Lightbox`).
//
// Claude's text is drawn from the tree lib/chatMarkdown.ts makes of it, with
// ordinary elements. It is never handed to the browser as HTML.
//
// A page can ask Claude about something of its own (`askClaude` in
// lib/chatStore.ts; a cut's "Ask Claude to review"). The panel then opens on
// a new conversation with what the page attached shown above the message
// box: what it is, a way to read all of it, what pressing Send will do and
// cost, and an x to take it away. Asking sends nothing. Send sends what was
// typed, a blank line, and the attachment; the message it becomes is long,
// so a long message of the reader's own is shown with its start and the rest
// folded.

import {
  Fragment,
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import MediaPreview from "@/components/MediaPreview";
import {
  api,
  chatArtifactUrl,
  chatBlobUrl,
  mediaUrl,
  type ChatContext,
  type ChatDecision,
  type ChatPending,
  type ChatPicture,
  type ChatSummary,
  type MediaType,
} from "@/lib/api";
import { knowLibraries, pageContext, subscribeChatContext } from "@/lib/chatContext";
import { markdownLite, offsite, plainText, type Block, type Inline } from "@/lib/chatMarkdown";
import type { ViewChip, ViewMessage } from "@/lib/chatReducer";
import type { ChatSnapshot } from "@/lib/chatSession";
import {
  chat,
  chatAsk,
  forgetAskedFrom,
  takeAskedFrom,
  useChat,
  useChatAsked,
  useChatAttachment,
} from "@/lib/chatStore";
import {
  askDetails,
  cannotSend,
  chipWords,
  contextWords,
  endWords,
  gate,
  inputLines,
  itemName,
  modelName,
  money,
  pictureAsset,
  spendWords,
  spoken,
  stateWords,
  titleOf,
  workingWords,
  type AskItems,
} from "@/lib/chatWords";
import { attachedMessage, enterInBox, foldMessage, sendingLine } from "@/lib/cuts";
import { relativeTime } from "@/lib/format";

const PANEL_ID = "chat-panel";
const FIELD_ID = "chat-message";
/** The same width the stylesheet switches shape at (globals.css). */
const WIDE = "(min-width: 1200px)";

// Somewhere to start, for a first look at an empty panel. Pressing one puts
// it in the box; it is not sent until the reader sends it.
const STARTERS = [
  "What can you help me with here?",
  "Which tag suggestions are waiting for me to review?",
  "Find smooth drone shots from the last month",
];

// Set by the button and Esc, read once by the panel as it appears. A panel
// that is open because the tab was reloaded with it open must not take
// focus: on a phone that would raise the keyboard on every page load.
let focusOnOpen = false;

// How many of the pages' asks (lib/chatStore.ts `askClaude`) the panel has
// already answered by showing the conversation and moving the keyboard to
// the box. Kept here, not in the panel, so closing and opening the panel
// with the same attachment still above the box moves nothing.
let asksAnswered = 0;

function opener(): HTMLElement | null {
  return document.querySelector<HTMLElement>("[data-chat-open]");
}

function openPanel(): void {
  focusOnOpen = true;
  // Opened from the header: that is where a close goes back to, whatever
  // page button opened the panel last time.
  forgetAskedFrom();
  chat.setOpen(true);
}

function closePanel(): void {
  chat.setOpen(false);
  // After the page has been drawn without the panel: while a sheet is up
  // the page (and the button in its header) is inert and cannot take focus.
  // Back to the page's button that opened the panel ("Ask Claude to
  // review"), when one did and it is still there; else the header's.
  window.setTimeout(() => (takeAskedFrom() ?? opener())?.focus(), 0);
}

function subscribeWide(listener: () => void): () => void {
  const query = window.matchMedia(WIDE);
  query.addEventListener("change", listener);
  return () => query.removeEventListener("change", listener);
}

function useWide(): boolean {
  return useSyncExternalStore(
    subscribeWide,
    () => window.matchMedia(WIDE).matches,
    () => false,
  );
}

// Whether the signed-in account is an administrator, asked once per person.
// Only an administrator can add a key or switch Claude on, so only one is
// offered the way to the page where that is done. (/auth/me is one of the
// few requests that is safe to make with no session.)
let knownRole: { user: string; admin: boolean } | null = null;

function useIsAdmin(user: string | null): boolean {
  const [admin, setAdmin] = useState(knownRole !== null && knownRole.user === user && knownRole.admin);
  useEffect(() => {
    if (user === null) return;
    if (knownRole?.user === user) {
      setAdmin(knownRole.admin);
      return;
    }
    let live = true;
    api
      .me()
      .then((me) => {
        if (me.id !== user) return;
        knownRole = { user, admin: me.role === "admin" };
        if (live) setAdmin(knownRole.admin);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [user]);
  return admin;
}

/**
 * The "Ask Claude" button, for the site header. It says by itself whether
 * the panel is open, and whether Claude is busy or waiting on the reader
 * while it is closed — a turn carries on with the panel shut, and its
 * question would otherwise wait unseen.
 */
export function ChatButton() {
  const s = useChat();
  if (s.user === null) return null;
  const waiting = !s.view.running && s.view.pending.length > 0;
  const note = s.view.running
    ? "Claude is answering"
    : waiting
      ? "Claude is waiting for your yes or no"
      : null;
  return (
    <button
      type="button"
      className="navlink chatopen"
      data-chat-open=""
      data-active={s.open}
      aria-expanded={s.open}
      aria-controls={s.open ? PANEL_ID : undefined}
      title={s.open ? "Close the chat with Claude" : "Open the chat with Claude"}
      onClick={() => {
        if (!s.open) {
          openPanel();
          return;
        }
        // Closed from this button: the keyboard stays on it, whichever
        // button of the page opened the panel.
        forgetAskedFrom();
        closePanel();
      }}
    >
      <span className="chatopen-mark" aria-hidden="true" />
      Ask Claude
      {note && (
        <span className="chatopen-note" data-kind={s.view.running ? "busy" : "ask"}>
          <span className="chat-sr">({note})</span>
        </span>
      )}
    </button>
  );
}

// --- Claude's words ----------------------------------------------------------

function Runs({ runs, onLeave }: { runs: Inline[]; onLeave: () => void }) {
  return (
    <>
      {runs.map((run, at) => {
        switch (run.kind) {
          case "code":
            return <code key={at}>{run.text}</code>;
          case "bold":
            return (
              <strong key={at}>
                <Runs runs={run.children} onLeave={onLeave} />
              </strong>
            );
          case "italic":
            return (
              <em key={at}>
                <Runs runs={run.children} onLeave={onLeave} />
              </em>
            );
          case "link":
            // A page of this app opens in place: the panel stays, so "open
            // that clip" puts the clip beside the conversation about it.
            // Anything else gets a tab of its own and is told nothing about
            // where it was linked from.
            return run.internal ? (
              <Link key={at} href={run.href} prefetch={false} onClick={onLeave}>
                {run.text}
              </Link>
            ) : (
              <a key={at} href={run.href} target="_blank" rel="noreferrer">
                {run.text}
                <Away href={run.href} text={run.text} />
              </a>
            );
          default:
            return <Fragment key={at}>{run.text}</Fragment>;
        }
      })}
    </>
  );
}

/** Where a link that leaves the app goes, beside its words (see `offsite`):
 *  part of the link, so a screen reader says it too. */
function Away({ href, text }: { href: string; text: string }) {
  const where = offsite(href, text, typeof window === "undefined" ? "" : window.location.origin);
  return where ? <span className="faint"> ({where} ↗)</span> : null;
}

function Lines({ lines, onLeave }: { lines: Inline[][]; onLeave: () => void }) {
  return (
    <>
      {lines.map((line, at) => (
        <Fragment key={at}>
          {at > 0 && <br />}
          <Runs runs={line} onLeave={onLeave} />
        </Fragment>
      ))}
    </>
  );
}

function BlockView({ block, onLeave }: { block: Block; onLeave: () => void }) {
  switch (block.kind) {
    case "heading":
      return (
        <h3>
          <Runs runs={block.text} onLeave={onLeave} />
        </h3>
      );
    case "list":
      // Each item carries its own marker, as written: a numbered list inside
      // a bulleted one, or one that starts at 4, reads as Claude wrote it.
      return (
        <ul className="chat-list" role="list">
          {block.items.map((item, at) => (
            <li key={at} style={{ marginLeft: item.depth * 16 }}>
              <span aria-hidden={item.marker === "•"}>{item.marker}</span>
              <span>
                <Lines lines={item.lines} onLeave={onLeave} />
              </span>
            </li>
          ))}
        </ul>
      );
    case "code":
      return <pre>{block.text}</pre>;
    case "rule":
      return <hr />;
    default:
      return (
        <p>
          <Lines lines={block.lines} onLeave={onLeave} />
        </p>
      );
  }
}

function Answer({ text, onLeave }: { text: string; onLeave: () => void }) {
  const blocks = useMemo(
    () => markdownLite(text, typeof window === "undefined" ? "" : window.location.origin),
    [text],
  );
  return (
    <div className="chat-answer">
      {blocks.map((block, at) => (
        <BlockView key={at} block={block} onLeave={onLeave} />
      ))}
    </div>
  );
}

// --- what Claude looked up -----------------------------------------------------

type OnPicture = (chip: ViewChip, picture: ChatPicture) => void;
/** Open a catalogue item in the viewer the rest of the app uses. */
type OnItem = (assetId: string) => void;

/** How many items of one request are shown to be looked at; the rest are
 *  counted. A request to decide two hundred tags stays a card. */
const ITEMS_SHOWN = 24;

function ItemThumb({ id, onItem }: { id: string; onItem: OnItem }) {
  const [failed, setFailed] = useState(false);
  const name = itemName(id);
  return (
    <button type="button" className="chat-picture" title={`Look at ${name}`} onClick={() => onItem(id)}>
      {failed ? (
        <span className="chat-item-name">{name}</span>
      ) : (
        /* eslint-disable-next-line @next/next/no-img-element -- our own guarded endpoint */
        <img
          src={mediaUrl(id, "thumbnail")}
          alt={`${name}. Look at it.`}
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
        />
      )}
    </button>
  );
}

/**
 * The items a request would change, each one there to be opened before the
 * answer is given: the app always lets the reviewer look at the footage
 * they are deciding on, and a yes to a dozen clips must not be given blind.
 * An item Claude named only by its short id cannot be opened from here and
 * is shown by that name.
 */
function AskItemsView({ groups, onItem }: { groups: AskItems[]; onItem: OnItem }) {
  return (
    <>
      {groups.map((one) => {
        const more = one.ids.length - ITEMS_SHOWN;
        return (
          <div className="chat-ask-items" key={one.name}>
            <p className="faint">
              {one.name} {one.ids.length.toLocaleString("en-US")} {one.ids.length === 1 ? "item" : "items"}
              {one.openable.length > 0 ? " (open one to look at it):" : ":"}
            </p>
            <div className="chat-pictures">
              {one.ids.slice(0, ITEMS_SHOWN).map((id) =>
                one.openable.includes(id) ? (
                  <ItemThumb key={id} id={id} onItem={onItem} />
                ) : (
                  <span key={id} className="chat-item-name faint">
                    {itemName(id)}
                  </span>
                ),
              )}
              {more > 0 && <span className="chat-item-name faint">and {more.toLocaleString("en-US")} more</span>}
            </div>
          </div>
        );
      })}
    </>
  );
}

function Picture({
  conversation,
  chip,
  picture,
  label,
  onPicture,
}: {
  conversation: string;
  chip: ViewChip;
  picture: ChatPicture;
  label: string;
  onPicture: OnPicture;
}) {
  const [gone, setGone] = useState(false);
  if (gone) {
    // Deleted with its conversation's files, or the data folder is away.
    return <span className="chat-picture-gone faint">This picture is no longer stored.</span>;
  }
  return (
    <button
      type="button"
      className="chat-picture"
      title="Show this picture larger"
      onClick={() => onPicture(chip, picture)}
    >
      {/* Served by our own session-guarded endpoint; the Next image loader
          would proxy and re-encode it, and cannot send the session. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={chatBlobUrl(conversation, picture.blob)}
        alt={`Picture: ${label}. Show it larger.`}
        loading="lazy"
        decoding="async"
        onError={() => setGone(true)}
      />
    </button>
  );
}

function Chip({
  chip,
  conversation,
  onPicture,
  onItem,
}: {
  chip: ViewChip;
  conversation: string | null;
  onPicture: OnPicture;
  onItem: OnItem;
}) {
  const words = chipWords(chip);
  // A change that asked first is described as its card was.
  const asked = chip.needs_confirm ? askDetails(chip.name, chip.input) : null;
  const lines = asked ? asked.lines : inputLines(chip.input);
  const items = asked?.items ?? [];
  const mark =
    words.tone === "busy" ? (
      <span className="spinner" />
    ) : (
      { ok: "✓", bad: "!", ask: "?", off: "–" }[words.tone]
    );
  const line = (
    <>
      <span className="chat-chip-mark" aria-hidden="true">
        {mark}
      </span>
      <span>{words.text}</span>
    </>
  );
  const more = lines.length > 0 || items.length > 0 || chip.summary !== "";
  return (
    <div className="chat-tool">
      {more ? (
        <details className="chat-chip" data-tone={words.tone}>
          <summary>
            {line}
            <span className="chat-chip-open" aria-hidden="true">
              ▾
            </span>
          </summary>
          <div className="chat-chip-more">
            {lines.length > 0 && (
              <dl className="chat-kv">
                {lines.map((one) => (
                  <Fragment key={one.name}>
                    <dt>{one.name}</dt>
                    <dd>{one.value}</dd>
                  </Fragment>
                ))}
              </dl>
            )}
            {items.length > 0 && <AskItemsView groups={items} onItem={onItem} />}
            {chip.summary !== "" && <p>{chip.summary}</p>}
          </div>
        </details>
      ) : (
        <div className="chat-chip" data-tone={words.tone}>
          <div className="chat-chip-line">{line}</div>
        </div>
      )}
      {/* Why it did not work is the part worth reading without a click. */}
      {chip.state === "failed" && chip.summary !== "" && <p className="chat-chip-why">{chip.summary}</p>}
      {conversation !== null && chip.images.length > 0 && (
        <div className="chat-pictures">
          {chip.images.map((picture) => (
            <Picture
              key={picture.blob}
              conversation={conversation}
              chip={chip}
              picture={picture}
              label={words.text}
              onPicture={onPicture}
            />
          ))}
        </div>
      )}
      {conversation !== null && chip.artifact && (
        <a
          className="chat-file"
          href={chatArtifactUrl(conversation, chip.id)}
          download={chip.artifact.name}
        >
          <span aria-hidden="true">↓</span>
          <span>Download {chip.artifact.name}</span>
        </a>
      )}
    </div>
  );
}

// One message. Memoised on the message object: the store keeps every message
// that did not change the very same object, so while an answer streams only
// the answer being written is drawn again (and read through the parser).
const Message = memo(function Message({
  message,
  conversation,
  onPicture,
  onItem,
  onLeave,
}: {
  message: ViewMessage;
  conversation: string | null;
  onPicture: OnPicture;
  onItem: OnItem;
  onLeave: () => void;
}) {
  if (message.role === "user") {
    const from = contextWords(message.context);
    // A message with a cut's review request attached runs to thousands of
    // characters of shot lines: its start is shown and the rest opens.
    const folded = foldMessage(message.text);
    return (
      <div className="chat-msg" data-role="user">
        <span className="chat-sr">You:</span>
        {folded ? (
          <div className="chat-bubble chat-bubble-long">
            <p>{folded.head}</p>
            <details>
              <summary>
                The rest of this message ({folded.rest.length.toLocaleString("en-US")} characters)
              </summary>
              <p>{folded.rest}</p>
            </details>
          </div>
        ) : (
          <p className="chat-bubble">{message.text}</p>
        )}
        {(from.length > 0 || message.sending) && (
          <p className="chat-from faint">
            {message.sending ? "Sending…" : `Sent from ${from.join(" · ")}`}
          </p>
        )}
      </div>
    );
  }
  if (message.text === "" && message.tools.length === 0) return null;
  return (
    <div className="chat-msg" data-role="assistant">
      <span className="chat-sr">Claude:</span>
      {message.text !== "" && <Answer text={message.text} onLeave={onLeave} />}
      {message.tools.length > 0 && (
        <div className="chat-tools">
          {message.tools.map((chip) => (
            <Chip key={chip.id} chip={chip} conversation={conversation} onPicture={onPicture} onItem={onItem} />
          ))}
        </div>
      )}
    </div>
  );
});

// --- a picture, larger -----------------------------------------------------------

/**
 * A picture with no catalogue item behind it that the panel can name (a
 * contact sheet, or a preview asked for by a short id): simply the picture,
 * larger. The same dialog as the viewer the rest of the app uses, without
 * the parts that need an item.
 */
function Lightbox({ src, title, onClose }: { src: string; title: string; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const closing = useRef(onClose);
  closing.current = onClose;
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const from = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    if (!dialog.open) dialog.showModal();
    closeRef.current?.focus();
    // Every key while this is up is for it, wherever focus is. A listener
    // on the dialog only hears keys while focus is inside it, and a click
    // on the picture drops focus to the page's body: the N pressed next
    // went to Tags, which declined its whole selection behind this modal.
    // Capture, on the document, as the app's own viewer does.
    function onKey(event: globalThis.KeyboardEvent) {
      event.stopPropagation();
      if (event.key === "Escape") {
        event.preventDefault();
        closing.current();
      }
    }
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      if (dialog.open) dialog.close();
      if (from?.isConnected) from.focus();
    };
  }, []);
  return (
    <dialog
      ref={dialogRef}
      className="preview"
      aria-label={title}
      // A click on the picture or the heading keeps focus here.
      tabIndex={-1}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <header className="preview-head">
        <div className="preview-title">
          <h2>{title}</h2>
        </div>
        <button type="button" className="btn" ref={closeRef} onClick={onClose} title="Esc">
          Close
        </button>
      </header>
      <div className="preview-stage">
        {/* eslint-disable-next-line @next/next/no-img-element -- our own guarded endpoint */}
        <img src={src} alt={title} />
      </div>
    </dialog>
  );
}

type Looking =
  | {
      kind: "asset";
      assetId: string;
      mediaType: MediaType;
      filename: string;
      startAt?: number;
      /** The line under its name: why it is being shown. */
      detail: string;
    }
  | { kind: "picture"; src: string; title: string };

// --- changes ask first --------------------------------------------------------------

function AskCard({
  pending,
  disabled,
  problem,
  onAnswer,
  onItem,
}: {
  pending: ChatPending[];
  /** Why the buttons are off, when they are. */
  disabled: string | null;
  problem: string | null;
  onAnswer: (decisions: ChatDecision[]) => void;
  onItem: OnItem;
}) {
  const all = (approve: boolean) =>
    onAnswer(pending.map((call) => ({ tool_use_id: call.id, approve })));
  return (
    <section className="chat-ask" aria-label="Claude is asking before it changes anything">
      <p className="eyebrow">Claude is asking first</p>
      {pending.map((call) => {
        // In words, with the items there to be opened: never parameter
        // names and 36-character ids (see askDetails).
        const { lines, items } = askDetails(call.name, call.input);
        return (
          <div className="chat-ask-one" key={call.id}>
            <p className="chat-ask-what">{call.summary}</p>
            {lines.length > 0 && (
              <dl className="chat-kv">
                {lines.map((one) => (
                  <Fragment key={one.name}>
                    <dt>{one.name}</dt>
                    <dd>{one.value}</dd>
                  </Fragment>
                ))}
              </dl>
            )}
            {items.length > 0 && <AskItemsView groups={items} onItem={onItem} />}
            <div className="chat-ask-do">
              <button
                type="button"
                className="btn btn-primary"
                disabled={disabled !== null}
                onClick={() => onAnswer([{ tool_use_id: call.id, approve: true }])}
              >
                Approve
              </button>
              <button
                type="button"
                className="btn"
                disabled={disabled !== null}
                onClick={() => onAnswer([{ tool_use_id: call.id, approve: false }])}
              >
                Decline
              </button>
            </div>
          </div>
        );
      })}
      {pending.length > 1 && (
        <div className="chat-ask-do chat-ask-all">
          <button type="button" className="btn" disabled={disabled !== null} onClick={() => all(true)}>
            Approve all {pending.length}
          </button>
          <button type="button" className="btn" disabled={disabled !== null} onClick={() => all(false)}>
            Decline all
          </button>
        </div>
      )}
      <p className="faint">
        {disabled ??
          "Nothing changes until you answer. Sending a new message instead counts as declining."}
      </p>
      {problem && (
        <p className="chat-problem" role="alert">
          {problem}
        </p>
      )}
    </section>
  );
}

// --- earlier conversations ---------------------------------------------------------

function Conversations({
  s,
  onOpen,
  onFocusLost,
}: {
  s: ChatSnapshot;
  onOpen: (id: string) => void;
  /** The focused Delete has gone with its question: put focus somewhere. */
  onFocusLost: () => void;
}) {
  const [asking, setAsking] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  // Which row the last refusal to delete belongs to.
  const [refused, setRefused] = useState<string | null>(null);
  const askRef = useRef<HTMLDivElement>(null);
  const rowsRef = useRef<HTMLUListElement>(null);

  useEffect(() => {
    if (asking !== null) askRef.current?.focus();
  }, [asking]);

  /** "Keep it": the question goes, and focus goes back to the button that
   *  asked it rather than falling off the panel. */
  function keep(id: string) {
    setAsking(null);
    window.setTimeout(() => rowsRef.current?.querySelector<HTMLElement>(`[data-delete="${id}"]`)?.focus(), 0);
  }

  async function remove(one: ChatSummary) {
    setAsking(null);
    setDeleting(one.id);
    setRefused(null);
    onFocusLost();
    const done = await chat.remove(one.id);
    setDeleting(null);
    if (!done) setRefused(one.id);
    onFocusLost();
  }

  if (s.conversations === null) {
    return s.problem?.where === "list" ? (
      <div className="chat-note">
        <p className="chat-problem" role="alert">
          {s.problem.message}
        </p>
        <button type="button" className="btn" onClick={() => void chat.refreshList()}>
          Try again
        </button>
      </div>
    ) : (
      <p className="chat-note faint">Loading your conversations…</p>
    );
  }
  if (s.conversations.length === 0) {
    return (
      <p className="chat-note faint">
        No earlier conversations yet. They are kept here, and only you can see yours.
      </p>
    );
  }
  return (
    <>
      {s.problem?.where === "list" && (
        <p className="chat-problem" role="alert">
          {s.problem.message}
        </p>
      )}
      <ul className="chat-convos" ref={rowsRef}>
        {s.conversations.map((one) => {
          const doing = stateWords(one.state);
          return (
            <li key={one.id} className="chat-convo" data-current={one.id === s.view.id}>
              {asking === one.id ? (
                <div
                  className="chat-convo-ask"
                  ref={askRef}
                  tabIndex={-1}
                  role="group"
                  aria-label={`Delete “${titleOf(one.title)}”?`}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") {
                      // Esc here means "keep it", not "close the panel".
                      event.preventDefault();
                      keep(one.id);
                    }
                  }}
                >
                  <p>
                    Delete “{titleOf(one.title)}”? Its messages, the pictures Claude was shown
                    and any files it made are removed for good.
                  </p>
                  <div className="chat-ask-do">
                    <button
                      type="button"
                      className="btn"
                      style={{ borderColor: "var(--ember)", color: "var(--ember)" }}
                      onClick={() => void remove(one)}
                    >
                      Delete
                    </button>
                    <button type="button" className="btn" onClick={() => keep(one.id)}>
                      Keep it
                    </button>
                  </div>
                </div>
              ) : (
                <>
                  <button
                    type="button"
                    className="chat-convo-open"
                    aria-current={one.id === s.view.id ? "true" : undefined}
                    onClick={() => onOpen(one.id)}
                  >
                    <span className="chat-convo-title">{titleOf(one.title)}</span>
                    <span className="faint">
                      {[relativeTime(one.updated_at), doing, one.spent_usd > 0 ? money(one.spent_usd) : null]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  </button>
                  <button
                    type="button"
                    className="btn chat-convo-del"
                    data-delete={one.id}
                    disabled={deleting === one.id}
                    aria-label={`Delete “${titleOf(one.title)}”`}
                    onClick={() => {
                      setRefused(null);
                      setAsking(one.id);
                    }}
                  >
                    {deleting === one.id ? "Deleting…" : "Delete"}
                  </button>
                </>
              )}
              {refused === one.id && s.problem?.where === "delete" && (
                <p className="chat-problem chat-convo-why" role="alert">
                  {s.problem.message}
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </>
  );
}

// --- the panel -----------------------------------------------------------------------

function sameContext(a: ChatContext | null, b: ChatContext | null): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** What is on screen now, as it would be sent; kept current for the "this
 *  page" line. (What is actually sent is read again by the store at the
 *  moment of sending.) */
function usePageHere(pathname: string, draft: string): [ChatContext | null, () => void] {
  const [here, setHere] = useState<ChatContext | null>(null);
  const refresh = useCallback(() => {
    const now = pageContext({
      pathname: window.location.pathname,
      search: window.location.search,
      title: document.title,
    });
    setHere((was) => (sameContext(was, now) ? was : now));
  }, []);
  useEffect(() => {
    refresh();
    // A page names itself, and publishes its selection, in effects of its
    // own, some of them after its data has arrived.
    const stop = subscribeChatContext(refresh);
    const watcher = new MutationObserver(refresh);
    watcher.observe(document.head, { subtree: true, childList: true, characterData: true });
    window.addEventListener("popstate", refresh);
    return () => {
      stop();
      watcher.disconnect();
      window.removeEventListener("popstate", refresh);
    };
  }, [refresh]);
  // A filter that only changes the query string tells nobody; whoever is
  // about to send is typing, so look again as they do.
  useEffect(refresh, [refresh, pathname, draft]);
  // The libraries' names, read each time the panel opens, so a page that
  // knows a library only by its id (a clip's page, `?library=` in the
  // address) still sends its name: Claude's requests to confirm then say
  // "GELCO", not an id. Without them the id goes alone, as it did before.
  useEffect(() => {
    let live = true;
    api
      .libraries()
      .then((libraries) => {
        if (live) knowLibraries(libraries);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);
  return [here, refresh];
}

function Panel({ s, wide, pathname }: { s: ChatSnapshot; wide: boolean; pathname: string }) {
  const { view } = s;
  const isAdmin = useIsAdmin(s.user);
  const [listing, setListing] = useState(false);
  const [skipPage, setSkipPage] = useState(false);
  const [looking, setLooking] = useState<Looking | null>(null);
  const [said, setSaid] = useState("");
  const fieldRef = useRef<HTMLTextAreaElement>(null);
  const titleRef = useRef<HTMLDivElement>(null);
  const logRef = useRef<HTMLDivElement>(null);
  // Follow the answer down the page while the reader is at the foot of the
  // conversation; leave them be once they scroll up to read something.
  const following = useRef(true);
  const [here, lookAgain] = usePageHere(pathname, s.draft);
  // What a page attached to the next message (a cut's review request), if
  // anything. It goes with what is typed, so it counts towards the length
  // and is enough by itself for Send.
  const attached = useChatAttachment();
  const asked = useChatAsked();
  const whole = attached ? attachedMessage(s.draft, attached.text) : s.draft;

  const blocked = gate(s.status, isAdmin);
  const why = cannotSend({ view, opening: s.opening, draft: whole });
  const ready = blocked === null && s.status !== null && why === null && whole.trim() !== "";
  const working = workingWords(view, s.link, s.stopping);
  const ended = view.running ? null : endWords(view.ended);
  const spend = spendWords(view.spent_usd, s.status);
  const model = view.model || s.status?.model || "";
  const fresh = view.id === null && view.messages.length === 0;
  // Not before the turn has said `start`: until then there is nothing on
  // the server to stop, and a Stop pressed then was quietly ignored while
  // the turn ran to the end (chatSession.stop says more).
  const canStop = view.running && view.begun && !view.stranded && s.canStop && s.link !== "watching";

  const focusField = useCallback(() => {
    // The box when there is one; otherwise the panel's title, so a screen
    // reader starts at the top and reads why there is no box.
    window.setTimeout(() => (fieldRef.current ?? titleRef.current)?.focus(), 0);
  }, []);

  /**
   * After a control that had focus goes away or is switched off (Approve
   * once answered, Stop while stopping, Dismiss, the "this page" x, a
   * conversation's Delete), put focus back in the panel. The browser drops
   * it on the page's body, and the Esc or N pressed next went to the page:
   * Tags cleared, or declined, its whole selection.
   */
  const asideRef = useRef<HTMLElement>(null);
  const keepFocus = useCallback(() => {
    window.setTimeout(() => {
      const on = document.activeElement;
      const lost =
        on === null ||
        on === document.body ||
        !on.isConnected ||
        (on instanceof HTMLButtonElement && on.disabled);
      if (lost) (fieldRef.current ?? titleRef.current ?? asideRef.current)?.focus();
    }, 0);
  }, []);

  // And whatever else lets focus fall out of the panel: a key that reaches
  // the page's body while the reader was last in the panel (or while the
  // sheet covers the page, which is inert but still hears window keys) is
  // the panel's. The pages listen on the window too; this listener is the
  // window's first, in capture.
  const lastHere = useRef(false);
  useEffect(() => {
    function where(event: Event) {
      const aside = asideRef.current;
      lastHere.current = aside !== null && event.target instanceof Node && aside.contains(event.target);
    }
    function onKey(event: globalThis.KeyboardEvent) {
      const loose = event.target === document.body || event.target === document.documentElement;
      if (!loose || event.isComposing) return;
      // A viewer is up and takes its own keys.
      if (document.querySelector("dialog[open]")) return;
      // Beside the page, after a click on the page: the key is the page's.
      if (wide && !lastHere.current) return;
      event.stopPropagation();
      asideRef.current?.focus();
      if (event.key === "Escape") {
        event.preventDefault();
        closePanel();
      }
    }
    document.addEventListener("focusin", where);
    document.addEventListener("pointerdown", where, true);
    window.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("focusin", where);
      document.removeEventListener("pointerdown", where, true);
      window.removeEventListener("keydown", onKey, true);
    };
  }, [wide]);

  // On opening, focus goes to the message box. The first time the panel is
  // opened the server has not yet said whether there is to be a box, so
  // focus waits on the title and moves down when the box arrives, unless
  // the reader has gone somewhere else in the meantime.
  const boxWanted = useRef(false);
  const hasBox = s.status !== null && blocked === null && !listing;
  useEffect(() => {
    if (focusOnOpen) {
      focusOnOpen = false;
      boxWanted.current = !hasBox;
      focusField();
      return;
    }
    if (!boxWanted.current || !hasBox) return;
    boxWanted.current = false;
    const on = document.activeElement;
    if (on === titleRef.current || on === document.body) fieldRef.current?.focus();
  }, [focusField, hasBox]);
  // A page has just asked (a cut's "Ask Claude to review"): the reader
  // pressed a button for this, so show the conversation (not the list of
  // earlier ones), say what was attached, and put the keyboard in the box —
  // or on the title, above the sentence that says why there is no box,
  // until there is one.
  const canHaveBox = s.status !== null && blocked === null;
  useEffect(() => {
    if (asked <= asksAnswered) return;
    asksAnswered = asked;
    setListing(false);
    if (attached) setSaid(`Attached: ${attached.label}. Nothing is sent until you press Send.`);
    boxWanted.current = !canHaveBox;
    focusField();
  }, [asked, attached, canHaveBox, focusField]);
  // And the other way: a box taken away while the reader was in it (the
  // key was removed, or the account turned out to be read-only) would drop
  // focus on the floor. It goes to the title, above the sentence that says
  // why the box went.
  const inBox = useRef(false);
  useEffect(() => {
    if (hasBox || !inBox.current) return;
    inBox.current = false;
    if (!listing) titleRef.current?.focus();
  }, [hasBox, listing]);

  // A sheet covers the page; the page must not scroll behind it.
  useEffect(() => {
    if (wide) return;
    const was = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = was;
    };
  }, [wide]);

  // "Without this page" is for one message, about the page it was said on.
  useEffect(() => setSkipPage(false), [pathname]);

  useEffect(() => {
    following.current = true;
  }, [view.id, listing]);

  useLayoutEffect(() => {
    const log = logRef.current;
    if (log && following.current) log.scrollTop = log.scrollHeight;
  }, [view.messages, view.pending, view.running, view.ended, s.opening, s.problem, listing]);

  // The box grows with what is typed, up to a few lines.
  useLayoutEffect(() => {
    const field = fieldRef.current;
    if (!field) return;
    field.style.height = "auto";
    field.style.height = `${Math.min(field.scrollHeight + 2, 168)}px`;
  }, [s.draft, listing, blocked]);

  // Read out how a turn ended, once, when it ends: never the words as they
  // arrive, which would be a screen reader talking over itself for a minute.
  const wasRunning = useRef(view.running);
  useEffect(() => {
    if (wasRunning.current && !view.running && view.ended) {
      const reason = view.ended.reason;
      if (reason === "end") {
        const last = [...view.messages].reverse().find((one) => one.role === "assistant");
        setSaid(spoken(plainText(last?.text ?? "")));
      } else if (reason === "awaiting_confirmation") {
        setSaid(
          `Claude is asking before it changes anything: ${view.pending.map((call) => call.summary).join("; ")}`,
        );
      } else if (reason === "stopped") {
        setSaid("Stopped.");
      } else {
        // Shown as an alert in the conversation, which is read out by itself.
        setSaid("");
      }
    }
    wasRunning.current = view.running;
  }, [view.running, view.ended, view.messages, view.pending]);

  const leave = useCallback(() => {
    // A link to a page of the app: on a sheet the page is behind the panel,
    // so get out of its way.
    if (!wide) chat.setOpen(false);
  }, [wide]);

  const openPicture = useCallback<OnPicture>(
    (chip, picture) => {
      if (view.id === null) return;
      const plain: Looking = {
        kind: "picture",
        src: chatBlobUrl(view.id, picture.blob),
        title: chipWords(chip).text,
      };
      const item = pictureAsset(chip);
      if (!item) {
        setLooking(plain);
        return;
      }
      // The viewer needs to know whether this is a clip or a still, which
      // the conversation does not say. If the item cannot be read (deleted
      // since, or its library is away) the picture Claude saw still can be.
      api
        .asset(item.assetId)
        .then((asset) =>
          setLooking({
            kind: "asset",
            assetId: asset.id,
            mediaType: asset.media_type,
            filename: asset.filename,
            startAt: item.startAt,
            detail: "Claude looked at this",
          }),
        )
        .catch(() => setLooking(plain));
    },
    [view.id],
  );

  // An item a request would change, opened from its card to be looked at
  // before answering. If it cannot be read, its thumbnail, larger.
  const openItem = useCallback<OnItem>((assetId) => {
    api
      .asset(assetId)
      .then((asset) =>
        setLooking({
          kind: "asset",
          assetId: asset.id,
          mediaType: asset.media_type,
          filename: asset.filename,
          detail: "Claude asks to change this",
        }),
      )
      .catch(() => setLooking({ kind: "picture", src: mediaUrl(assetId, "thumbnail"), title: itemName(assetId) }));
  }, []);

  function submit(event?: FormEvent) {
    event?.preventDefault();
    if (!ready) return;
    following.current = true;
    // With nothing attached this is `chat.send` and nothing else.
    void chatAsk.send(s.draft, skipPage ? { context: null } : undefined);
    setSkipPage(false);
  }

  function onFieldKey(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter is a new line. Not while a word is still
    // being composed (Japanese, Chinese, Korean input ends one with Enter),
    // and never as the repeat of a held key: an attachment alone is enough
    // to send, so an Enter held on the page's button would otherwise send
    // it the moment the keyboard arrived here (`enterInBox`).
    const does = enterInBox({
      key: event.key,
      shiftKey: event.shiftKey,
      repeat: event.repeat,
      composing: event.nativeEvent.isComposing,
    });
    if (does === "leave") return;
    event.preventDefault();
    if (does === "send") submit();
  }

  function onKey(event: KeyboardEvent<HTMLElement>) {
    // See the note on keys at the top of this file.
    event.stopPropagation();
    if (event.key !== "Escape" || event.defaultPrevented || event.nativeEvent.isComposing) return;
    // Marked as used, like the Manage menu's: a page listening for Esc
    // (Tags clears its selection on it) leaves this one alone.
    event.preventDefault();
    closePanel();
  }

  function openConversation(id: string) {
    setListing(false);
    void chat.openConversation(id);
    focusField();
  }

  function startNew() {
    setListing(false);
    chat.newConversation();
    focusField();
  }

  const pageWords = contextWords(here);
  const confirmOff = view.running
    ? "Working on your last answer…"
    : blocked !== null
      ? blocked.sentence
      : null;

  return (
    <>
      <aside
        ref={asideRef}
        id={PANEL_ID}
        className="chatpanel"
        data-chat-panel=""
        role={wide ? "complementary" : "dialog"}
        aria-modal={wide ? undefined : true}
        aria-labelledby="chat-heading"
        // A click on a blank part of the panel (on Claude's text, say) must
        // leave focus in the panel. Otherwise it falls to the page's body,
        // and the Esc pressed next to close the panel goes to the page
        // instead: Tags cleared its selection and the panel stayed open.
        tabIndex={-1}
        onKeyDown={onKey}
      >
        <header className="chat-head">
          <div className="chat-title" ref={titleRef} tabIndex={-1}>
            <h2 id="chat-heading">Ask Claude</h2>
            <p className="faint">{listing ? "Earlier conversations" : titleOf(view.title)}</p>
          </div>
          <button
            type="button"
            className="btn"
            onClick={startNew}
            disabled={fresh && !listing}
            title="Start a new conversation"
          >
            New
          </button>
          <button
            type="button"
            className="btn"
            aria-pressed={listing}
            onClick={() => {
              const next = !listing;
              setListing(next);
              if (next) void chat.refreshList();
            }}
            title="Your earlier conversations"
          >
            Earlier
          </button>
          <button type="button" className="btn" onClick={closePanel} title="Esc">
            Close
          </button>
        </header>

        {listing ? (
          <div className="chat-log" tabIndex={0} role="region" aria-label="Earlier conversations">
            <Conversations s={s} onOpen={openConversation} onFocusLost={keepFocus} />
          </div>
        ) : (
          <>
            <div className="chat-meta">
              <p className="faint mono">
                {[modelName(model), spend.here, spend.month].filter(Boolean).join(" · ")}
              </p>
              <label className="chat-switch">
                <input
                  type="checkbox"
                  checked={view.pictures}
                  disabled={s.status !== null && !s.status.can_chat}
                  onChange={(event) => void chat.setPictures(event.target.checked)}
                />
                <span>
                  Let Claude see pictures
                  <small className="faint">
                    {view.pictures
                      ? "A small copy of each picture Claude asks to look at is sent to Anthropic."
                      : "Pictures Claude asks for are shown to you here and not sent; it gets their file names and tags."}
                  </small>
                </span>
              </label>
              {s.problem?.where === "pictures" && (
                <p className="chat-problem" role="alert">
                  {s.problem.message}
                </p>
              )}
            </div>

            <div
              className="chat-log"
              ref={logRef}
              tabIndex={0}
              role="region"
              aria-label="Conversation"
              onScroll={(event) => {
                const log = event.currentTarget;
                following.current = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
              }}
              // A picture takes its height when it arrives, which is after
              // the conversation was scrolled to its foot.
              onLoadCapture={(event) => {
                const log = event.currentTarget;
                if (following.current) log.scrollTop = log.scrollHeight;
              }}
            >
              {s.problem?.where === "open" && (
                // The server's sentence alone ("No such conversation") reads
                // as a fault with the panel; it is usually a conversation
                // that was deleted in another tab or on another device.
                <p className="chat-problem" role="alert">
                  The conversation that was open here could not be opened ({s.problem.message}).{" "}
                  <button
                    type="button"
                    className="linkish"
                    onClick={() => {
                      chat.dismissProblem();
                      keepFocus();
                    }}
                  >
                    Dismiss
                  </button>
                </p>
              )}
              {s.opening && <p className="chat-note faint">Opening the conversation…</p>}
              {fresh && !s.opening && (
                <div className="chat-welcome">
                  <p>
                    Ask about anything in the catalogue: find footage, look at a picture, read
                    what was said in a clip, check tags, build a rough cut for Premiere.
                  </p>
                  <p className="faint">
                    Claude can see what this page shows. It never touches an original, and a
                    change to your tags always asks you first.
                  </p>
                  {blocked === null && s.status !== null && (
                    <div className="hintrow">
                      {STARTERS.map((starter) => (
                        <button
                          key={starter}
                          type="button"
                          className="hint"
                          onClick={() => {
                            chat.setDraft(starter);
                            focusField();
                          }}
                        >
                          {starter}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              )}
              {view.messages.map((message) => (
                <Message
                  key={message.key}
                  message={message}
                  conversation={view.id}
                  onPicture={openPicture}
                  onItem={openItem}
                  onLeave={leave}
                />
              ))}
              {view.pending.length > 0 && (
                <AskCard
                  pending={view.pending}
                  disabled={confirmOff}
                  problem={s.problem?.where === "confirm" ? s.problem.message : null}
                  onAnswer={(decisions) => {
                    following.current = true;
                    void chat.confirm(decisions);
                    keepFocus();
                  }}
                  onItem={openItem}
                />
              )}
              {view.pending.length === 0 && s.problem?.where === "confirm" && (
                // The usual refusal is a request answered meanwhile in
                // another tab or on another device: the card has gone with
                // it, and the reason must not go too, or the click seems to
                // have done nothing.
                <p className="chat-problem" role="alert">
                  Your answer could not be given ({s.problem.message.replace(/\.$/, "")}).{" "}
                  <button
                    type="button"
                    className="linkish"
                    onClick={() => {
                      chat.dismissProblem();
                      keepFocus();
                    }}
                  >
                    Dismiss
                  </button>
                </p>
              )}
              {working && (
                <p className="chat-working">
                  <span className="spinner" aria-hidden="true" />
                  <span>{working}</span>
                </p>
              )}
              {ended && (
                <p
                  className="chat-end"
                  data-tone={ended.tone}
                  role={ended.tone === "bad" ? "alert" : undefined}
                >
                  {ended.text}
                </p>
              )}
            </div>

            {/* With something attached the foot can be taller than the panel
                (a short window, a phone on its side): the attachment then
                gives way and scrolls, and the box and Send stay in reach. */}
            <footer className="chat-foot" data-attached={attached ? "" : undefined}>
              {s.status === null ? (
                s.problem?.where === "status" ? (
                  <div className="chat-gate" data-fault="">
                    <p role="alert">{s.problem.message}</p>
                    <button type="button" className="btn" onClick={() => void chat.refreshStatus()}>
                      Try again
                    </button>
                  </div>
                ) : (
                  <p className="chat-note faint">Checking whether Claude can be used here…</p>
                )
              ) : blocked !== null ? (
                <div className="chat-gate" data-fault={blocked.fault ? "" : undefined} role="status">
                  <p>{blocked.sentence}</p>
                  {blocked.fix && (
                    <Link href={blocked.fix.href} onClick={leave}>
                      {blocked.fix.label} →
                    </Link>
                  )}
                </div>
              ) : (
                <form className="chat-composer" onSubmit={submit}>
                  <div className="chat-here">
                    {skipPage ? (
                      <>
                        <span>The next message will not say which page you are on.</span>
                        <button
                          type="button"
                          className="linkish"
                          onClick={() => {
                            setSkipPage(false);
                            keepFocus();
                          }}
                        >
                          Include it
                        </button>
                      </>
                    ) : (
                      pageWords.length > 0 && (
                        <>
                          <span className="pill">This page</span>
                          <span className="chat-here-what">{pageWords.join(" · ")}</span>
                          <button
                            type="button"
                            className="chipx"
                            title="Leave this page out of the next message"
                            aria-label="Leave this page out of the next message"
                            onClick={() => {
                              setSkipPage(true);
                              keepFocus();
                            }}
                          >
                            ×
                          </button>
                        </>
                      )
                    )}
                  </div>
                  {attached && (
                    <div className="chat-attached" role="group" aria-label="Attached to your next message">
                      <div className="chat-attached-what">
                        <span className="pill">Attached</span>
                        <span>{attached.label}</span>
                        <button
                          type="button"
                          className="chipx"
                          title="Remove the attachment. Nothing has been sent."
                          aria-label={`Remove the attachment: ${attached.label}`}
                          onClick={() => {
                            chatAsk.drop();
                            keepFocus();
                          }}
                        >
                          ×
                        </button>
                      </div>
                      <details className="chat-attached-read">
                        <summary>Read what will be sent</summary>
                        {/* The server's words and file names, as text. Focusable so
                            it can be scrolled from the keyboard. */}
                        <pre tabIndex={0} aria-label="The attachment, as it will be sent">
                          {attached.text}
                        </pre>
                      </details>
                      <p id="chat-attached-cost" className="faint">
                        {sendingLine(
                          view.pictures,
                          s.status ? money(s.status.spent_this_month_usd) : null,
                          s.status ? money(s.status.budget_usd) : null,
                        )}
                      </p>
                    </div>
                  )}
                  <div className="chat-compose">
                    <textarea
                      id={FIELD_ID}
                      ref={fieldRef}
                      className="input"
                      rows={1}
                      value={s.draft}
                      // Short enough for one line of the box at any width:
                      // a placeholder that wraps is cut off by a one-row box.
                      placeholder={attached ? "Add a note, or just press Send." : "Ask about your footage…"}
                      aria-label="Message to Claude"
                      aria-describedby={attached ? "chat-attached-cost chat-why" : "chat-why"}
                      onChange={(event) => chat.setDraft(event.target.value)}
                      onKeyDown={onFieldKey}
                      onFocus={() => {
                        inBox.current = true;
                        lookAgain();
                      }}
                      onBlur={() => {
                        inBox.current = false;
                      }}
                    />
                    {canStop ? (
                      <button
                        type="button"
                        className="btn"
                        disabled={s.stopping}
                        onClick={() => {
                          void chat.stop();
                          keepFocus();
                        }}
                      >
                        {s.stopping ? "Stopping…" : "Stop"}
                      </button>
                    ) : (
                      <button type="submit" className="btn btn-primary" disabled={!ready}>
                        Send
                      </button>
                    )}
                  </div>
                  <p id="chat-why" className="chat-why faint">
                    {why ??
                      (view.pending.length > 0
                        ? "Sending a message now answers No to the request above."
                        : "Enter sends · Shift+Enter starts a new line")}
                  </p>
                  {(s.problem?.where === "send" || s.problem?.where === "stop") && (
                    <p className="chat-problem" role="alert">
                      {s.problem.message}
                    </p>
                  )}
                </form>
              )}
            </footer>
          </>
        )}
        <p className="chat-sr" role="status" aria-live="polite">
          {said}
        </p>
      </aside>

      {/* Part of the panel, though drawn over everything: the pages' key
          handlers skip keys from inside [data-chat-panel], and a clip from
          here played full screen (where the viewer lets keys go) must not
          answer Tags' selection with N. */}
      <div data-chat-panel="" style={{ display: "contents" }}>
        {looking?.kind === "asset" && (
          <MediaPreview
            assetId={looking.assetId}
            mediaType={looking.mediaType}
            filename={looking.filename}
            startAt={looking.startAt}
            detail={looking.detail}
            onClose={() => setLooking(null)}
          />
        )}
        {looking?.kind === "picture" && (
          <Lightbox src={looking.src} title={looking.title} onClose={() => setLooking(null)} />
        )}
      </div>
    </>
  );
}

/**
 * Wraps every page (app/layout.tsx) and draws the panel beside it. The
 * wrapper is what lets an open panel make room for itself on a wide window
 * and put the page out of reach under a sheet on a narrow one; with the
 * panel closed it is a plain block that changes nothing.
 */
export default function ChatPanel({ children }: { children: ReactNode }) {
  const s = useChat();
  const pathname = usePathname();
  const wide = useWide();
  // Nobody signed in means no panel: the sign-in page must not ask the
  // server for anything it would be bounced back to itself for.
  const shown = s.open && s.user !== null && pathname !== "/login";
  const sheet = shown && !wide;
  return (
    <>
      <div className="chatframe" data-chat={shown ? "open" : undefined} inert={sheet}>
        {children}
      </div>
      {sheet && <div className="chat-backdrop" onClick={closePanel} />}
      {shown && <Panel s={s} wide={wide} pathname={pathname} />}
    </>
  );
}
