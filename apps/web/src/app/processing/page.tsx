"use client";

// Processing dashboard. Deliberately plain-language: queue names become
// "what it does", and a failure feed is always visible rather than buried
// in container logs.

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import Shell from "@/components/Shell";
import { ApiError, api, type LookActivity, type LookReview, type ProcessingReport } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { usePageTitle, usePoll } from "@/lib/hooks";
import { focusIfLost } from "@/lib/localModels";
import {
  CANCEL_QUESTION,
  ingestStopped,
  ingestText,
  preparingText,
  progressText,
  reviewHref,
  reviewLine,
  reviewPercent,
  reviewPlace,
  reviewStateWord,
  reviewTone,
  reviewWayOut,
  suggestedText,
} from "@/lib/looks";

// One entry per queue the server reports (QUEUES in api/v1/system.py). A
// queue without one was shown under its internal name: `frames` — where
// frames are sampled for visual search and camera motion is measured, the
// slowest stage there is — read "frames" on the page built to say what is
// happening in plain words. One with no entry yet now falls back to
// `plainName`, which is readable but not an explanation: add the entry.
const QUEUE_LABELS: Record<string, string> = {
  visuals: "Thumbnails & posters",
  metadata: "Reading file details",
  frames: "Sampling frames & motion",
  media: "Video previews",
  exports: "Exports & renders",
  transcribe: "Transcribing speech",
  vision: "Visual analysis",
  // Only reported where local models can run (the Mac install): downloads,
  // tests and other work of the models on this Mac, one at a time.
  local_ai: "Local AI models",
  default: "Legacy queue",
};

// One entry per task the server records a job for (every name passed to
// `_with_asset` in processing/tasks.py). Two were missing: a clip whose
// visual indexing failed — a model file not there, a drive away — was
// listed under "embed_frames".
const TASK_LABELS: Record<string, string> = {
  extract_metadata: "Read details",
  generate_derivatives: "Thumbnails",
  generate_proxy: "Video preview",
  sample_frames: "Sample frames",
  embed_frames: "Visual search index",
  transcribe_asset: "Transcribe",
  analyze_motion: "Shot motion",
  describe_shots: "Shot descriptors",
  // Reads every byte of one file, on request, to tell a true duplicate.
  verify_content_hash: "Fingerprint the whole file",
};

/** A queue or task this page has no words for yet — one added on the server
 *  since — still reads as words rather than as an identifier. */
function plainName(internal: string): string {
  const words = internal.replace(/[_-]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : internal;
}

/**
 * Looking (local AI): the folder reviews somebody asked a model on this Mac
 * for, and the libraries whose new footage is looked at as it arrives.
 *
 * These are not rows of the media pipeline above (they are the local-AI
 * lane's), so they have their own request, on the page's own five seconds.
 * Nothing is drawn where models cannot run, where nobody has asked for a
 * review, or on a server from before reviews existed: the page then reads
 * exactly as it did.
 *
 * A review is the server's from start to finish. The bar, the sentence and
 * whether it can be stopped are read off it each time, so a review started
 * in the dialog, from the chat panel or by another administrator shows here
 * the same, and one left running overnight is here in the morning with how
 * it ended.
 */
function Looking() {
  const [activity, setActivity] = useState<LookActivity | null>(null);
  const [cancelling, setCancelling] = useState<string | null>(null);
  // Why a review could not be stopped, in its own row: "It has already
  // finished." belongs beside the review it is about.
  const [refused, setRefused] = useState<{ id: string; text: string } | null>(null);
  // Only the newest answer draws: an answer asked for before Cancel was
  // pressed must not put the review back to "Looking" after it.
  const asked = useRef(0);
  // A server with no such route (older than reviews) is asked once.
  const absent = useRef(false);
  const rows = useRef<HTMLTableSectionElement>(null);

  const read = useCallback(async () => {
    if (absent.current) return;
    const mine = ++asked.current;
    try {
      const next = await api.lookReviews();
      if (mine === asked.current) setActivity(next);
    } catch (err) {
      // What was last read stays: the page's own line above says when the
      // server cannot be reached, and a bar that vanished for one missed
      // answer would read as a review that had gone.
      if (err instanceof ApiError && err.status === 404) absent.current = true;
    }
  }, []);

  useEffect(() => {
    const tick = () => {
      if (!document.hidden) void read();
    };
    tick();
    const timer = setInterval(tick, 5000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      asked.current += 1;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [read]);

  async function cancel(review: LookReview) {
    if (!window.confirm(CANCEL_QUESTION)) return;
    setCancelling(review.id);
    setRefused(null);
    try {
      await api.cancelReview(review.id);
    } catch (err) {
      setRefused({
        id: review.id,
        text:
          err instanceof Error && err.message
            ? err.message
            : "The review could not be stopped. Try again.",
      });
    }
    // Read now rather than at the next tick: the row says how it ended.
    await read();
    setCancelling(null);
    // The button pressed was disabled while this ran, and it goes altogether
    // once the review has ended. The keyboard carries on from the button if
    // the review is still there to stop (it was refused), else from the
    // row's link, instead of from the top of the page.
    requestAnimationFrame(() => {
      const row = rows.current?.querySelector<HTMLElement>(`[data-review="${review.id}"]`);
      focusIfLost(
        row?.querySelector<HTMLElement>("button:not(:disabled)") ??
          row?.querySelector<HTMLElement>("a"),
      );
    });
  }

  if (!activity?.available) return null;
  const { reviews, ingest } = activity;
  if (reviews.length === 0 && ingest.length === 0) return null;
  const madeUp = reviews.some((review) => review.demo);

  return (
    <>
      <div className="sectionhead">
        <h2>Looking (local AI)</h2>
        <span className="faint mono">
          {madeUp ? "demonstration: the answers are made up" : "a model on this Mac"}
        </span>
      </div>

      {ingest.length > 0 && (
        <div className="card looking-ingest">
          {ingest.map((line) => (
            <p key={line.library_id} data-stopped={ingestStopped(line) || undefined}>
              {ingestText(line)}
            </p>
          ))}
        </div>
      )}

      {reviews.length > 0 && (
        <div className="card" style={{ padding: 0, overflowX: "auto" }}>
          <table className="table looking-reviews">
            <thead>
              <tr>
                <th scope="col">Folder</th>
                <th scope="col">Model</th>
                <th scope="col">State</th>
                <th scope="col">Progress</th>
                <th scope="col" aria-label="Stop" />
              </tr>
            </thead>
            <tbody ref={rows}>
              {reviews.map((review) => {
                const stopping = cancelling === review.id;
                const suggested = suggestedText(review);
                const preparing = preparingText(review.preparing);
                const wayOut = reviewWayOut(review);
                const place = reviewPlace(review);
                return (
                  <tr key={review.id} data-review={review.id}>
                    <td>
                      {/* To the footage: the folder in Browse, where its
                          pictures open and "Review with AI" is. Not
                          prefetched: the list is re-read every five seconds. */}
                      <Link
                        className="linkish"
                        href={reviewHref(review)}
                        prefetch={false}
                        title={`Open ${place} in Browse`}
                      >
                        {place}
                      </Link>
                      <div className="faint looking-asked">
                        {review.requested_by ? `asked by ${review.requested_by}` : "asked"}
                        {review.created_at ? ` · ${relativeTime(review.created_at)}` : ""}
                      </div>
                    </td>
                    <td className="mono looking-model">{review.model_name || "—"}</td>
                    <td>
                      <span
                        className="pill"
                        data-tone={stopping ? undefined : reviewTone(review.state)}
                      >
                        {stopping ? "Cancelling…" : reviewStateWord(review.state)}
                      </span>
                    </td>
                    <td className="looking-progress">
                      {review.total > 0 && (
                        <>
                          <div
                            className="bar"
                            role="progressbar"
                            aria-label={`Looked at, ${place}`}
                            aria-valuemin={0}
                            aria-valuemax={review.total}
                            aria-valuenow={Math.min(review.done, review.total)}
                          >
                            <i style={{ width: `${reviewPercent(review)}%` }} />
                          </div>
                          <div className="faint mono looking-count">
                            {progressText(review)} looked at
                            {review.failed > 0 &&
                              `; ${review.failed.toLocaleString("en-US")} could not be`}
                          </div>
                        </>
                      )}
                      <div className="muted">{reviewLine(review)}</div>
                      {/* Paused, or stopped by the model: where that is put
                          right, since it is not on this page. */}
                      {wayOut && (
                        <div className="faint">
                          {wayOut.text}{" "}
                          <Link className="linkish" href={wayOut.href} prefetch={false}>
                            {wayOut.link}
                          </Link>
                          .
                        </div>
                      )}
                      {preparing && <div className="faint">{preparing}</div>}
                      {suggested && (
                        <div>
                          {suggested}{" "}
                          <Link className="linkish" href="/tags" prefetch={false}>
                            Answer them on Tags
                          </Link>
                        </div>
                      )}
                      {refused?.id === review.id && (
                        <div role="alert" style={{ color: "var(--ember)" }}>
                          {refused.text}
                        </div>
                      )}
                    </td>
                    <td>
                      {review.can_cancel && (
                        <button
                          type="button"
                          className="btn"
                          disabled={cancelling !== null}
                          aria-label={`Cancel the review of ${place}`}
                          onClick={() => cancel(review)}
                        >
                          Cancel
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

export default function ProcessingPage() {
  // Live view while work drains. A hidden tab stops asking, and one failed
  // tick no longer leaves its error on screen after the next one succeeds.
  const { data: report, error } = usePoll<ProcessingReport>(api.processing, 5000);
  usePageTitle("Processing");

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Processing</h2>
        <span className="faint mono">refreshes every 5s</span>
      </div>

      {error && <div className="empty">{error}</div>}
      {!report && !error && <div className="empty">Loading…</div>}

      {report && (
        <>
          <div className="statgrid">
            {Object.entries(report.queue_depths)
              .filter(([name, depth]) => name !== "default" || depth > 0)
              .map(([name, depth]) => (
                <div className="stat" key={name}>
                  <p className="eyebrow">{QUEUE_LABELS[name] ?? plainName(name)}</p>
                  <div
                    className="stat-value"
                    data-tone={depth < 0 ? "bad" : depth > 0 ? "busy" : undefined}
                  >
                    {depth < 0 ? "—" : depth.toLocaleString()}
                  </div>
                  <p className="faint" style={{ fontSize: "0.76rem", margin: 0 }}>
                    {depth < 0 ? "queue unreachable" : depth === 0 ? "idle" : "waiting"}
                  </p>
                </div>
              ))}
          </div>

          <div className="sectionhead">
            <h2>Media state</h2>
          </div>
          <div className="statgrid">
            {Object.entries(report.assets_by_status).map(([status, count]) => (
              <div className="stat" key={status}>
                <p className="eyebrow">{status.replace(/_/g, " ")}</p>
                <div className="stat-value">{count.toLocaleString()}</div>
              </div>
            ))}
          </div>

          {Object.keys(report.workers_last_hour ?? {}).length > 1 && (
            <>
              <div className="sectionhead">
                <h2>Machines</h2>
                <span className="faint mono">jobs in the last hour</span>
              </div>
              <div className="statgrid">
                {Object.entries(report.workers_last_hour ?? {}).map(([worker, states]) => {
                  const total = Object.values(states).reduce((a, b) => a + b, 0);
                  return (
                    <div className="stat" key={worker}>
                      <p className="eyebrow">{worker}</p>
                      <div className="stat-value">{total.toLocaleString()}</div>
                      <p className="faint" style={{ fontSize: "0.76rem", margin: 0 }}>
                        {Object.entries(states)
                          .map(([status, count]) => `${count} ${status}`)
                          .join(" · ")}
                      </p>
                    </div>
                  );
                })}
              </div>
            </>
          )}

          <Looking />

          <div className="sectionhead">
            <h2>Generated files</h2>
            <span className="faint mono">
              {Object.entries(report.jobs_last_hour)
                .map(([status, count]) => `${count} ${status}`)
                .join(" · ") || "no recent jobs"}
            </span>
          </div>
          <div className="card" style={{ padding: 0, overflowX: "auto" }}>
            <table className="table">
              <thead>
                <tr>
                  <th>Kind</th>
                  <th>Ready</th>
                  <th>Failed</th>
                  <th style={{ width: "45%" }}>Completion</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(report.derivatives).map(([kind, states]) => {
                  const ready = states.ready ?? 0;
                  const failed = states.failed ?? 0;
                  const total = Object.values(states).reduce((a, b) => a + b, 0);
                  const pct = total ? Math.round((ready / total) * 100) : 0;
                  return (
                    <tr key={kind}>
                      <td style={{ textTransform: "capitalize" }}>{kind}</td>
                      <td className="mono">{ready.toLocaleString()}</td>
                      <td className="mono" style={{ color: failed ? "var(--ember)" : undefined }}>
                        {failed || "—"}
                      </td>
                      <td>
                        <div className="bar">
                          <i style={{ width: `${pct}%` }} />
                        </div>
                      </td>
                    </tr>
                  );
                })}
                {Object.keys(report.derivatives).length === 0 && (
                  <tr>
                    <td colSpan={4} className="faint">
                      Nothing generated yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {report.recent_failures.length > 0 && (
            <>
              <div className="sectionhead">
                <h2>Recent failures</h2>
                <span className="faint mono">originals are never affected</span>
              </div>
              <div className="card" style={{ padding: 0, overflowX: "auto" }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th>Task</th>
                      <th>When</th>
                      <th>Reason</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {report.recent_failures.map((job) => (
                      <tr key={job.id}>
                        <td>{TASK_LABELS[job.task_name] ?? plainName(job.task_name)}</td>
                        <td className="faint mono">
                          {relativeTime(job.started_at)}
                          {job.worker ? ` · ${job.worker}` : ""}
                        </td>
                        <td className="muted">{job.error ?? "—"}</td>
                        <td>
                          {job.asset_id && (
                            // A Link, as everywhere else: the bare <a> reloaded
                            // the whole app to show one asset. Not prefetched —
                            // the list is re-read every five seconds.
                            <Link
                              className="navlink"
                              href={`/assets/${job.asset_id}`}
                              prefetch={false}
                            >
                              View
                            </Link>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}
    </Shell>
  );
}
