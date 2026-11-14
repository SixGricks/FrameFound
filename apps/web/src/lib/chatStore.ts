"use client";

// The app's one chat: the store the panel draws from, wired to the real
// API, the real clock and this tab's memory.
//
// It is made here, at module level, on purpose. Every page renders its own
// Shell, so everything inside a page is thrown away on navigation; a module
// is loaded once per tab and stays. A turn that is running when the reader
// clicks to another page goes on being read into this store, and the panel
// (mounted once, from app/layout.tsx) picks up where it was.
//
// All of the behaviour is in chatSession.ts, where it is tested; this file
// only supplies the outside world. Nothing here runs a request by being
// imported: the store does nothing until `chat.signIn(user.id)` is called,
// which is what keeps the sign-in page from asking for something it would
// be bounced back to itself for (a 401 outside /auth/ redirects to /login).

import { useSyncExternalStore } from "react";

import { api } from "./api";
import { pageContext } from "./chatContext";
import { createChatStore, type ChatSnapshot } from "./chatSession";
import { createChatAsk, type ChatAttachment } from "./cuts";

// Session storage, not local: which conversation is open is a fact about
// this tab, and two tabs may each have their own.
const MEMORY_KEY = "framefound.chat";

export const chat = createChatStore({
  status: api.chatStatus,
  conversations: api.chatConversations,
  create: api.createChatConversation,
  read: api.chatConversation,
  setPictures: api.setChatPictures,
  remove: api.deleteChatConversation,
  stop: api.stopChat,
  send: api.sendChatMessage,
  answer: api.answerChat,
  follow: api.followChat,
  // Read when a message is sent, not when the panel opened: the selection
  // may have changed since. The address bar is read directly rather than
  // through useSearchParams, which would need a Suspense boundary round
  // every statically rendered page the panel sits on.
  context: () =>
    typeof window === "undefined"
      ? null
      : pageContext({
          pathname: window.location.pathname,
          search: window.location.search,
          title: document.title,
        }),
  wait: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  recall: () => {
    try {
      return window.sessionStorage.getItem(MEMORY_KEY);
    } catch {
      return null;
    }
  },
  remember: (value) => {
    try {
      if (value === null) window.sessionStorage.removeItem(MEMORY_KEY);
      else window.sessionStorage.setItem(MEMORY_KEY, value);
    } catch {
      // Private browsing, or storage switched off: the panel works, it
      // just does not come back to the same conversation after a reload.
    }
  },
});

/** The chat's state, for a component to draw. Change it through `chat`. */
export function useChat(): ChatSnapshot {
  return useSyncExternalStore(chat.subscribe, chat.get, chat.get);
}

// --- a page asks Claude about something of its own ---------------------------------
//
// A page can open the panel on a new conversation with something attached
// to the next message: a cut's page attaches the request to review that cut.
// The attachment is held here, beside the store, and shown above the message
// box. NOTHING is sent by asking: it goes when the reader presses Send, as
// what they typed, a blank line, and the attachment's text. Until then it
// can be read and removed.
//
// It is kept for the tab's life but not across a reload (a reload shows the
// panel as it was, without the attachment; the page's button attaches it
// again), and it goes when whoever was signed in signs out.
//
// How it is held, sent and handed back is `createChatAsk` in lib/cuts.ts,
// which imports nothing and is tested against a pretend server
// (tests/cuts-ask.test.mjs); this file only makes the app's one.

/** The app's one attachment, beside the app's one chat. */
export const chatAsk = createChatAsk(chat);

// The page's button that last opened the panel, so that closing the panel
// gives the keyboard back to it. The panel used to hand it to the header's
// "Ask Claude" button whatever had opened it: a keyboard user who pressed
// "Ask Claude to review", read the request and pressed Esc was sent to the
// top of the page.
let askedFrom: HTMLElement | null = null;

/**
 * Open the chat panel on a new conversation, ready to ask Claude about
 * something of the page's: `text` written in the box (after anything the
 * reader had typed there and not sent), `attachment` above it. Nothing is
 * sent until the reader presses Send.
 *
 * `from` is the button that was pressed, when the caller did something
 * slow first (a request) and the button may have been switched off, and so
 * lost focus, meanwhile; otherwise it is whatever has the keyboard now.
 */
export function askClaude(
  request: { text?: string; attachment?: ChatAttachment },
  from?: HTMLElement | null,
): boolean {
  const active = typeof document === "undefined" ? null : document.activeElement;
  const pressed = from ?? (active instanceof HTMLElement && active !== document.body ? active : null);
  const asked = chatAsk.ask(request);
  if (asked) askedFrom = pressed;
  return asked;
}

/**
 * Where the keyboard goes when the panel closes after a page's button
 * opened it: that button, if it is still on the page and can be pressed.
 * Null otherwise (the header's own button is then the place). Asked once:
 * the next close, after the panel was opened some other way, is not this
 * button's.
 */
export function takeAskedFrom(): HTMLElement | null {
  const from = askedFrom;
  askedFrom = null;
  if (!from || !from.isConnected) return null;
  if ((from instanceof HTMLButtonElement || from instanceof HTMLInputElement) && from.disabled) return null;
  return from;
}

/** The panel was opened from its own button: a close goes back there. */
export function forgetAskedFrom(): void {
  askedFrom = null;
}

/** What is attached to the next message, for the panel to draw. */
export function useChatAttachment(): ChatAttachment | null {
  return useSyncExternalStore(chatAsk.subscribe, chatAsk.get, chatAsk.get);
}

/** How many times a page has asked (see `ChatAsk.asked`). */
export function useChatAsked(): number {
  return useSyncExternalStore(chatAsk.subscribe, chatAsk.asked, chatAsk.asked);
}
