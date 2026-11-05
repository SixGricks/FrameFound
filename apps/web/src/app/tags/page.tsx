"use client";

// Tags: what the system has learned, and the queue of things it wants checked.
//
// Reviewing suggestions in bulk is much faster than opening assets one at a
// time, and every answer — yes or no — feeds straight back into the match. The
// threshold and its reason are shown because an operator who sees a strange
// suggestion deserves to know why it cleared the bar.
//
// Suggestions now come from three places — the CLIP learner, the Claude
// reviewer on the server, and Claude through the MCP connector — and each tile
// says which, because a second opinion is weighed differently from a match.

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import {
  api,
  type ClaudeReviewStatus,
  type PendingSuggestion,
  type TagSummary,
} from "@/lib/api";
import { relativeTime, shortDate } from "@/lib/format";

const ORIGIN_LABEL: Record<string, string> = {
  clip: "match",
  claude: "Claude",
  mcp: "Claude (chat)",
};

function money(value: number): string {
  return `$${value.toFixed(2)}`;
}

export default function TagsPage() {
  const [tags, setTags] = useState<TagSummary[] | null>(null);
  const [open, setOpen] = useState<TagSummary | null>(null);
  const [pending, setPending] = useState<PendingSuggestion[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [review, setReview] = useState<ClaudeReviewStatus | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setTags(await api.tags());
    } catch {
      setTags([]);
    }
    try {
      setReview(await api.claudeReviewStatus());
    } catch {
      setReview(null);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // A run in progress: refresh its numbers until it finishes.
  useEffect(() => {
    if (review?.last.state !== "running") return;
    const timer = setInterval(() => {
      api.claudeReviewStatus().then(setReview).catch(() => undefined);
    }, 5000);
    return () => clearInterval(timer);
  }, [review?.last.state]);

  const openTag = useCallback(async (tag: TagSummary) => {
    setOpen(tag);
    setPending(null);
    setPending(await api.pendingForTag(tag.id));
  }, []);

  async function decide(tagId: string, assetId: string, accept: boolean) {
    setBusy(true);
    try {
      await api.decideAssetTag(assetId, tagId, accept);
      // Drop it from the queue rather than refetching — the operator is
      // working through a list and should not lose their place.
      setPending((rows) => (rows ?? []).filter((r) => r.asset_id !== assetId));
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function startReview() {
    setBusy(true);
    setNotice(null);
    try {
      const result = await api.startClaudeReview({});
      setNotice(result.message);
      await load();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not start the review");
    } finally {
      setBusy(false);
    }
  }

  const running = review?.last.state === "running";
  const toReview = review ? Math.min(review.untagged, review.batch) : 0;

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Tags</h2>
        {tags && (
          <span className="faint mono">
            {tags.length} tags ·{" "}
            {tags.reduce((n, t) => n + t.pending_count, 0)} waiting for review
          </span>
        )}
      </div>

      {review && (
        <div className="card">
          <div style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap" }}>
            <div style={{ flex: 1, minWidth: 260 }}>
              <strong>A second opinion from Claude</strong>
              <p className="faint" style={{ margin: "4px 0 0", fontSize: "0.86rem" }}>
                {review.configured ? (
                  <>
                    {review.untagged} picture{review.untagged === 1 ? "" : "s"} nobody has
                    tagged · {money(review.spent_this_month_usd)} of {money(review.budget_usd)}{" "}
                    used this month · {review.model}
                    {review.auto ? " · runs nightly" : ""}
                  </>
                ) : (
                  <>
                    Add an Anthropic key under Security and Claude can look at untagged
                    pictures and propose tags, under a monthly budget you set.
                  </>
                )}
              </p>
              {review.last.state !== "idle" && (
                <p className="faint mono" style={{ margin: "6px 0 0", fontSize: "0.78rem" }}>
                  last run {running ? "running" : review.last.state}
                  {review.last.finished_at && !running
                    ? ` ${relativeTime(review.last.finished_at)}`
                    : ""}
                  : {review.last.reviewed} reviewed, {review.last.suggested} suggestions
                  {review.last.new_tags ? ` (${review.last.new_tags} new tags)` : ""},{" "}
                  {money(review.last.spent_usd)}
                  {review.last.message ? ` — ${review.last.message}` : ""}
                </p>
              )}
              {notice && (
                <p className="faint" style={{ margin: "6px 0 0", fontSize: "0.82rem" }}>
                  {notice}
                </p>
              )}
            </div>
            {review.configured && (
              <button
                className="btn btn-primary"
                disabled={busy || running || toReview === 0 || review.remaining_usd <= 0}
                onClick={startReview}
                title={
                  review.remaining_usd <= 0
                    ? "This month's budget is used up — raise it under Security"
                    : undefined
                }
              >
                {running ? "Reviewing…" : `Review ${toReview} with Claude`}
              </button>
            )}
          </div>
        </div>
      )}

      {!tags ? (
        <div className="empty">Loading…</div>
      ) : !tags.length ? (
        <div className="empty">
          No tags yet. Open a video, add a tag like <strong>Power Broom</strong>,
          and FrameFound will start looking for it in everything else.
        </div>
      ) : (
        <div className="tablewrap card" style={{ padding: 0 }}>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Tag</th>
                <th scope="col">Tagged</th>
                <th scope="col">To review</th>
                <th scope="col">Match bar</th>
                <th scope="col">Learned</th>
                <th scope="col" />
              </tr>
            </thead>
            <tbody>
              {tags.map((tag) => (
                <tr key={tag.id}>
                  <td>
                    <strong>{tag.name}</strong>
                  </td>
                  <td className="mono">{tag.asset_count}</td>
                  <td>
                    {tag.pending_count > 0 ? (
                      <span className="pill" data-tone="warn">
                        {tag.pending_count}
                      </span>
                    ) : (
                      <span className="faint">—</span>
                    )}
                  </td>
                  <td className="mono" title={tag.threshold_reason}>
                    {tag.threshold != null ? tag.threshold.toFixed(3) : "—"}
                    {tag.threshold_reason && (
                      <div className="faint" style={{ fontSize: "0.72rem" }}>
                        {tag.threshold_reason}
                      </div>
                    )}
                  </td>
                  <td className="faint">
                    {tag.learned_at ? shortDate(tag.learned_at) : "not yet"}
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {tag.pending_count > 0 && (
                      <button
                        className="btn"
                        style={{ padding: "4px 10px" }}
                        onClick={() => openTag(tag)}
                      >
                        Review
                      </button>
                    )}{" "}
                    <button
                      className="btn"
                      style={{ padding: "4px 10px" }}
                      onClick={() => api.relearnTag(tag.id)}
                      title="Re-run learning — useful after new media is indexed"
                    >
                      Re-learn
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open && (
        <>
          <div className="sectionhead">
            <h2>Is this {open.name}?</h2>
            <span className="faint">
              {open.example_count} confirmed example
              {open.example_count === 1 ? "" : "s"} so far — every answer here
              sharpens the next round
            </span>
          </div>
          {!pending ? (
            <div className="empty">Loading…</div>
          ) : !pending.length ? (
            <div className="empty">Nothing left to review for this tag.</div>
          ) : (
            <div className="grid">
              {pending.map((row) => (
                <div className="tile" key={row.asset_id}>
                  <Link href={`/assets/${row.asset_id}`} className="tile-frame">
                    <Thumb assetId={row.asset_id} mediaType={row.media_type} status="ready" />
                    {row.confidence != null && (
                      <span className="tile-badge">
                        {(row.confidence * 100).toFixed(0)}%
                      </span>
                    )}
                  </Link>
                  <div className="tile-meta">
                    <div className="tile-name">{row.filename}</div>
                    <div className="faint" style={{ fontSize: "0.74rem" }}>
                      suggested by {ORIGIN_LABEL[row.origin] ?? row.origin}
                    </div>
                    <div className="toolbar" style={{ margin: "8px 0 0", gap: 6 }}>
                      <button
                        className="btn btn-primary"
                        style={{ padding: "4px 12px" }}
                        disabled={busy}
                        onClick={() => decide(open.id, row.asset_id, true)}
                      >
                        Yes
                      </button>
                      <button
                        className="btn"
                        style={{ padding: "4px 12px" }}
                        disabled={busy}
                        onClick={() => decide(open.id, row.asset_id, false)}
                      >
                        No
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </Shell>
  );
}
