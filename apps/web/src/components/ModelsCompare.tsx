"use client";

// The Compare card on the AI models page: what Compare is for, the three
// newest comparisons and the way in. It reads the list itself (the page's own
// overview does not carry comparisons), quickly while one runs and slowly
// otherwise; a failed read leaves the way in where it is.
//
// The page draws nothing but one card where models cannot run, so this card
// is only ever mounted where they can. There, `ModelsCompareSaved` adds one
// line under that card when comparisons made on a Mac are in the catalogue:
// they can still be read, and the AI models page is the only way to them
// from the menu.

import Link from "next/link";
import { useEffect, useState } from "react";

import { ApiError, api, type CompareOverview } from "@/lib/api";
import { decisionText, listEvery, statePill, summaryLine } from "@/lib/compare";
import { rereads } from "@/lib/localModels";

const SHOWN = 3;

export default function ModelsCompare() {
  const [overview, setOverview] = useState<CompareOverview | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    const reader = rereads<CompareOverview>({
      read: () => api.compareOverview(),
      answered: (next) => {
        setOverview(next);
        setProblem(null);
      },
      failed: (err) =>
        setProblem(
          err instanceof ApiError
            ? err.message
            : "The saved comparisons could not be read just now. FrameFound tries again shortly.",
        ),
      every: listEvery,
      hidden: () => document.hidden,
      later: (run, ms) => window.setTimeout(run, ms),
      cancel: (handle) => window.clearTimeout(handle as number),
    });
    void reader.now();
    const onShow = () => {
      if (!document.hidden) void reader.now();
    };
    document.addEventListener("visibilitychange", onShow);
    return () => {
      document.removeEventListener("visibilitychange", onShow);
      reader.stop();
    };
  }, []);

  const newest = overview?.comparisons.slice(0, SHOWN) ?? [];
  const none = !!overview && overview.comparisons.length === 0;

  return (
    <div className="card">
      <p style={{ margin: 0 }}>
        Before a model is trusted with your catalogue, run two or more on the same shots and see
        what each says, beside what you already decided. Nothing a model says here is saved to the
        catalogue.
      </p>

      {problem && (
        <p role="alert" style={{ margin: "12px 0 0", color: "var(--ember)" }}>
          {problem}
        </p>
      )}

      {none && (
        <p className="faint" style={{ margin: "12px 0 0" }}>
          No comparisons yet.
        </p>
      )}

      {newest.length > 0 && (
        <ul className="models-compare-list">
          {newest.map((one) => {
            const pill = statePill(one.state);
            const decided = decisionText(one);
            return (
              <li key={one.id}>
                <Link href={`/models/compare/${one.id}`} className="models-compare-name">
                  {one.name}
                </Link>
                <span className="faint mono">{summaryLine(one)}</span>
                <span className="pill" data-tone={pill.tone ?? undefined}>
                  {pill.label}
                </span>
                {decided && <span className="muted">{decided}</span>}
              </li>
            );
          })}
        </ul>
      )}

      <div style={{ marginTop: 14 }}>
        <Link href="/models/compare" className={none ? "btn btn-primary" : "btn"}>
          Compare models →
        </Link>
      </div>
    </div>
  );
}

/**
 * Under the "Models run only on a Mac" card: the way to the comparisons made
 * on a Mac, which this server can show but not change. Nothing at all when
 * there are none (or they could not be read): the card says everything then.
 */
export function ModelsCompareSaved() {
  const [count, setCount] = useState(0);

  useEffect(() => {
    let live = true;
    api
      .compareOverview()
      .then((overview) => {
        if (live) setCount(overview.comparisons.length);
      })
      .catch(() => {
        // Only a way in; the page is whole without it.
      });
    return () => {
      live = false;
    };
  }, []);

  if (!count) return null;
  return (
    <p style={{ margin: "12px 0 0" }}>
      {count === 1
        ? "One comparison was made on a Mac. It can still be read here, not changed."
        : `${count} comparisons were made on a Mac. They can still be read here, not changed.`}{" "}
      <Link href="/models/compare" className="compare-link">
        Compare models →
      </Link>
    </p>
  );
}
