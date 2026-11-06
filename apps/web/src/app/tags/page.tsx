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
//
// Bulk review works as it does for faces (people/[id]): the queue is ranked
// strongest first, so there is usually one line between yes and no, and the
// operator's job is to draw it — "down to here", shift-click for a range,
// click for the odd one — then answer the selection once (or press Y / N).
// One request answers them all and the tag re-learns once; answering sixty
// tiles one at a time used to queue sixty full-library re-learns.

import { useCallback, useEffect, useRef, useState } from "react";
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
  // Bulk review: the chosen asset ids, and the last tile clicked (the
  // anchor a shift-click extends from) — kept as an asset id, because
  // answered tiles leave the grid and every index after them shifts.
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [anchor, setAnchor] = useState<string | null>(null);
  const [answer, setAnswer] = useState<string | null>(null);
  // Which opening of the review a response belongs to. A slow answer or
  // queue fetch for one tag must not land on the next tag opened — nor on
  // the same tag opened again (A, then B, then A).
  const opening = useRef(0);

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

  // Keep the open tag's numbers ("N confirmed examples so far") in step
  // with the table after answers land; it was a snapshot from opening.
  useEffect(() => {
    setOpen((current) =>
      current ? (tags?.find((t) => t.id === current.id) ?? current) : null,
    );
  }, [tags]);

  // A run in progress: refresh its numbers until it finishes.
  useEffect(() => {
    if (review?.last.state !== "running") return;
    const timer = setInterval(() => {
      api.claudeReviewStatus().then(setReview).catch(() => undefined);
    }, 5000);
    return () => clearInterval(timer);
  }, [review?.last.state]);

  const openTag = useCallback(async (tag: TagSummary) => {
    const ticket = ++opening.current;
    setOpen(tag);
    setPending(null);
    setChosen(new Set());
    setAnchor(null);
    setAnswer(null);
    const rows = await api.pendingForTag(tag.id);
    if (ticket === opening.current) setPending(rows);
  }, []);

  /**
   * Answer suggestions of the open tag: `accept` are it, `reject` are not.
   * One request for any number, then the answered tiles leave the queue
   * rather than the queue being refetched — the operator is working down a
   * list and should not lose their place.
   */
  const decide = useCallback(
    async (accept: string[], reject: string[]) => {
      if (!open || (!accept.length && !reject.length)) return;
      const ticket = opening.current;
      setBusy(true);
      setAnswer(null);
      try {
        const result = await api.decideTags(open.id, { accept, reject });
        await load();
        // Another tag (or this one, afresh) was opened while this was
        // saving: the answers are recorded, but this grid is not theirs.
        if (ticket !== opening.current) return;
        const done = new Set([...accept, ...reject]);
        setPending((rows) => (rows ?? []).filter((r) => !done.has(r.asset_id)));
        // Only the answered tiles leave the selection: a single Yes on one
        // tile must not throw away a bulk selection being built elsewhere.
        setChosen(
          (current) => new Set([...current].filter((id) => !done.has(id))),
        );
        setAnchor((current) => (current && done.has(current) ? null : current));
        const parts = [];
        if (result.accepted) parts.push(`${result.accepted} confirmed`);
        if (result.rejected) parts.push(`${result.rejected} rejected`);
        if (result.skipped)
          parts.push(`${result.skipped} already answered elsewhere`);
        setAnswer(
          `${parts.join(", ") || "Nothing changed"}. ${open.name} is re-learning.`,
        );
      } catch (err) {
        setAnswer(
          err instanceof Error ? err.message : "Could not save those answers",
        );
      } finally {
        setBusy(false);
      }
    },
    [open, load],
  );

  /** Toggle one tile, or a range from the last click when shift is held. */
  function choose(index: number, shift: boolean) {
    const list = pending ?? [];
    const target = list[index];
    if (!target) return;
    const anchored = anchor ? list.findIndex((r) => r.asset_id === anchor) : -1;
    const from = shift && anchored >= 0 ? anchored : index;
    setChosen((current) => {
      const next = new Set(current);
      const [lo, hi] = from <= index ? [from, index] : [index, from];
      const adding = !next.has(target.asset_id);
      for (let i = lo; i <= hi; i += 1) {
        const row = list[i];
        if (!row) continue;
        if (adding) next.add(row.asset_id);
        else next.delete(row.asset_id);
      }
      return next;
    });
    setAnchor(target.asset_id);
  }

  /** Everything from the strongest suggestion down to and including `index`. */
  function chooseDownTo(index: number) {
    setChosen(
      new Set((pending ?? []).slice(0, index + 1).map((r) => r.asset_id)),
    );
    setAnchor((pending ?? [])[index]?.asset_id ?? null);
  }

  const selectedIds = (pending ?? [])
    .filter((r) => chosen.has(r.asset_id))
    .map((r) => r.asset_id);
  const otherIds = (pending ?? [])
    .filter((r) => !chosen.has(r.asset_id))
    .map((r) => r.asset_id);

  // Y / N / Esc answer or clear the selection — the hands stay on the
  // mouse and the keyboard while working down a long queue.
  useEffect(() => {
    if (!open) return;
    function onKey(event: KeyboardEvent) {
      const el = event.target as HTMLElement | null;
      if (
        el &&
        (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))
      )
        return;
      if (event.metaKey || event.ctrlKey || event.altKey || busy) return;
      // A key the page's menu already used (Esc closing Manage), or one
      // pressed while focus is in the top bar, is not meant for the grid.
      if (event.defaultPrevented || el?.closest('.topbar, [role="menu"]'))
        return;
      const key = event.key.toLowerCase();
      if (key === "escape") {
        setChosen(new Set());
        setAnchor(null);
      } else if (key === "y" && selectedIds.length) {
        event.preventDefault();
        decide(selectedIds, []);
      } else if (key === "n" && selectedIds.length) {
        event.preventDefault();
        decide([], selectedIds);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, selectedIds, decide]);

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
              {open.asset_count} confirmed example
              {open.asset_count === 1 ? "" : "s"} so far — every answer here
              sharpens the next round
            </span>
          </div>
          {answer && (
            <div className="card" role="status">
              {answer}
            </div>
          )}
          {!pending ? (
            <div className="empty">Loading…</div>
          ) : !pending.length ? (
            <div className="empty">Nothing left to review for this tag.</div>
          ) : (
            <>
              <div className="toolbar">
                <button
                  className="btn"
                  disabled={busy}
                  onClick={() => chooseDownTo(pending.length - 1)}
                >
                  Select all {pending.length}
                </button>
                <span className="faint" style={{ fontSize: "0.8rem" }}>
                  strongest first · click to select
                  <span className="pointer-hint">
                    , shift-click for a range
                  </span>{" "}
                  · Y / N answers the selection
                </span>
              </div>
              <div className="grid">
                {pending.map((row, index) => (
                  <div
                    className="tile tagpick"
                    key={row.asset_id}
                    data-selected={chosen.has(row.asset_id)}
                  >
                    <button
                      type="button"
                      className="tile-frame tagpick-frame"
                      aria-pressed={chosen.has(row.asset_id)}
                      aria-label={`Select ${row.filename}`}
                      onClick={(event) => choose(index, event.shiftKey)}
                      title={`${row.filename} — shift-click to select a range`}
                    >
                      <Thumb
                        assetId={row.asset_id}
                        mediaType={row.media_type}
                        status="ready"
                      />
                      {row.confidence != null && (
                        <span className="tile-badge">
                          {(row.confidence * 100).toFixed(0)}%
                        </span>
                      )}
                    </button>
                    <button
                      type="button"
                      className="tagpick-downto"
                      onClick={() => chooseDownTo(index)}
                      title="Select this one and every stronger suggestion above it"
                    >
                      ↑ down to here
                    </button>
                    <div className="tile-meta">
                      <div className="tile-name">
                        <Link href={`/assets/${row.asset_id}`} title="Open">
                          {row.filename}
                        </Link>
                      </div>
                      <div className="faint" style={{ fontSize: "0.74rem" }}>
                        suggested by {ORIGIN_LABEL[row.origin] ?? row.origin}
                      </div>
                      <div
                        className="toolbar"
                        style={{ margin: "8px 0 0", gap: 6 }}
                      >
                        <button
                          className="btn btn-primary"
                          style={{ padding: "4px 12px" }}
                          disabled={busy}
                          onClick={() => decide([row.asset_id], [])}
                        >
                          Yes
                        </button>
                        <button
                          className="btn"
                          style={{ padding: "4px 12px" }}
                          disabled={busy}
                          onClick={() => decide([], [row.asset_id])}
                        >
                          No
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
              {selectedIds.length > 0 && (
                // Pinned to the foot of the screen while anything is chosen:
                // never under the sticky header, and in thumb's reach on a phone.
                <div
                  className="toolbar tagreview-bar"
                  role="toolbar"
                  aria-label="Answer the selection"
                >
                  <>
                    <span className="pill" data-tone="warn" aria-live="polite">
                      {busy ? "Saving…" : `${selectedIds.length} selected`}
                    </span>
                    <button
                      className="btn btn-primary"
                      disabled={busy}
                      onClick={() => decide(selectedIds, [])}
                      title="Y"
                    >
                      Yes, {selectedIds.length === 1 ? "it is" : "these are"}{" "}
                      {open.name}
                    </button>
                    <button
                      className="btn"
                      disabled={busy}
                      onClick={() => decide([], selectedIds)}
                      title="N"
                    >
                      No
                    </button>
                    {otherIds.length > 0 && (
                      <button
                        className="btn"
                        disabled={busy}
                        onClick={() => decide(selectedIds, otherIds)}
                        title="Answer the whole queue at once: the selected tiles are it, every other tile shown is not"
                      >
                        Yes to these, no to the other {otherIds.length}
                      </button>
                    )}
                    <button
                      className="btn"
                      onClick={() => {
                        setChosen(new Set());
                        setAnchor(null);
                      }}
                      title="Esc"
                    >
                      Clear
                    </button>
                  </>
                </div>
              )}
            </>
          )}
        </>
      )}
    </Shell>
  );
}
