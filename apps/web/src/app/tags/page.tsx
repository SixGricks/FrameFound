"use client";

// Tags: what the system has learned, and the queue of things it wants checked.
//
// Reviewing suggestions in bulk is much faster than opening assets one at a
// time, and every answer — yes or no — feeds straight back into the match. The
// threshold and its reason are shown because an operator who sees a strange
// suggestion deserves to know why it cleared the bar.

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import { api, type PendingSuggestion, type TagSummary } from "@/lib/api";
import { shortDate } from "@/lib/format";

export default function TagsPage() {
  const [tags, setTags] = useState<TagSummary[] | null>(null);
  const [open, setOpen] = useState<TagSummary | null>(null);
  const [pending, setPending] = useState<PendingSuggestion[] | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setTags(await api.tags());
    } catch {
      setTags([]);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

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
