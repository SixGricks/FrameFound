"use client";

// Small hooks shared by pages. Each exists because the same mistake was made
// on several pages at once (review, 2026-10).

import { useEffect, useRef, useState, type RefObject } from "react";

/** The site's own title: the root layout's, which Next writes back. */
export const SITE_TITLE = "FrameFound";

/** A page's tab title: "Cuts · FrameFound"; the site's own for none. */
export function tabTitle(title: string | null | undefined): string {
  return title ? `${title} · ${SITE_TITLE}` : SITE_TITLE;
}

/**
 * Whether the tab's title, as it now reads, is the page's title taken back,
 * to be written again. Only the site's own title (or none) counts: that is
 * what Next puts back when it applies the layout's title after a page has
 * set its own (seen on Search on a fresh load, and on a change of the query
 * string). Any other title is somebody's choice and is left alone, so two
 * pages that each want their own never take it from each other in turn.
 */
export function titleTakenBack(current: string, wanted: string): boolean {
  return current !== wanted && (current === SITE_TITLE || current.trim() === "");
}

/** The browser tab, history and bookmarks read "FrameFound" for every page
 *  until this existed; a listing, the editor and Processing were
 *  indistinguishable in the tab strip. While the page is up its title wins:
 *  when Next writes the site's own title back over it, it is written again. */
export function usePageTitle(title: string | null | undefined): void {
  useEffect(() => {
    const wanted = tabTitle(title);
    document.title = wanted;
    // Next changes the <title> element (its text, or the element itself) in
    // the head; watching the head sees either.
    const watcher = new MutationObserver(() => {
      if (titleTakenBack(document.title, wanted)) document.title = wanted;
    });
    watcher.observe(document.head, { subtree: true, childList: true, characterData: true });
    return () => {
      watcher.disconnect();
      document.title = SITE_TITLE;
    };
  }, [title]);
}

/**
 * Give a player its file, and take it back when the player goes. Put the
 * returned ref on a <video> or <audio> that has no `src` of its own.
 *
 * A player removed from the page is only paused. The browser keeps the
 * request for its file open until it gets round to discarding the player —
 * seconds later, or at the next garbage collection — and a review that
 * answers a clip every second or two leaves a stream open behind each one.
 * A browser opens six connections to a host over plain HTTP/1.1: after six
 * clips the next player, the thumbnails and the answer being saved all wait
 * behind streams nobody is watching, while the server goes on reading those
 * files off the drive. Emptying the player (no source, then `load()`) drops
 * the request at once.
 *
 * The source is set here, not as the element's `src` prop, because in
 * development React runs every effect twice (set up, clean up, set up): a
 * cleanup that emptied the player would leave it empty for good, since React
 * does not write an unchanged prop a second time.
 */
export function usePlayerSource<T extends HTMLMediaElement>(src: string): RefObject<T | null> {
  const player = useRef<T | null>(null);
  useEffect(() => {
    // Held here: by the time the cleanup runs the ref has been emptied.
    const el = player.current;
    if (!el) return;
    el.src = src;
    return () => {
      el.pause();
      el.removeAttribute("src");
      el.load();
    };
  }, [src]);
  return player;
}

/** Poll a request on an interval: nothing is asked while the tab is hidden
 *  (a page left open overnight polled all night), the tab's return refreshes
 *  at once, and an error clears on the next success instead of staying on
 *  screen after one failed tick. `load` must be a stable reference (an `api`
 *  method, not an inline arrow). */
export function usePoll<T>(
  load: () => Promise<T>,
  everyMs: number,
): { data: T | null; error: string | null } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    const tick = async () => {
      if (document.hidden) return;
      try {
        const next = await load();
        if (live) {
          setData(next);
          setError(null);
        }
      } catch (err) {
        if (live) setError(err instanceof Error ? err.message : "Request failed");
      }
    };
    tick();
    const timer = setInterval(tick, everyMs);
    const onVisible = () => {
      if (!document.hidden) tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      live = false;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load, everyMs]);

  return { data, error };
}
