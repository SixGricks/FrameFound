"use client";

// Small hooks shared by pages. Each exists because the same mistake was made
// on several pages at once (review, 2026-10).

import { useEffect, useState } from "react";

/** The browser tab, history and bookmarks read "FrameFound" for every page
 *  until this existed; a listing, the editor and Processing were
 *  indistinguishable in the tab strip. */
export function usePageTitle(title: string | null | undefined): void {
  useEffect(() => {
    document.title = title ? `${title} · FrameFound` : "FrameFound";
    return () => {
      document.title = "FrameFound";
    };
  }, [title]);
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
