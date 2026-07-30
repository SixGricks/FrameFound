"use client";

// Tags on an asset, and the correction loop.
//
// Two kinds of chip, deliberately distinguishable at a glance:
//   confirmed — the operator said so. Solid, with a remove control.
//   suggested — the system thinks so. Outlined, with accept and reject.
//
// The distinction is the feature. A system that presented its guesses as facts
// would be untrustworthy the first time it was wrong; one that asks gets
// corrected, and every correction makes the next guess better.

import { useCallback, useEffect, useState } from "react";

import { api, type AssetTag } from "@/lib/api";

export default function TagEditor({ assetId }: { assetId: string }) {
  const [tags, setTags] = useState<AssetTag[] | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setTags(await api.assetTags(assetId));
    } catch {
      setTags([]);
    }
  }, [assetId]);

  useEffect(() => {
    load();
  }, [load]);

  async function run(action: () => Promise<AssetTag[] | void>, message?: string) {
    setBusy(true);
    setNote(null);
    try {
      const result = await action();
      if (Array.isArray(result)) setTags(result);
      else await load();
      if (message) {
        setNote(message);
        setTimeout(() => setNote(null), 3500);
      }
    } catch (err) {
      setNote(err instanceof Error ? err.message : "That did not work");
    } finally {
      setBusy(false);
    }
  }

  const confirmed = tags?.filter((t) => t.source !== "suggested") ?? [];
  const suggested = tags?.filter((t) => t.source === "suggested") ?? [];

  return (
    <div className="card">
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: 10,
          marginBottom: 10,
          flexWrap: "wrap",
        }}
      >
        <strong>Tags</strong>
        {note && (
          <span className="faint" role="status">
            {note}
          </span>
        )}
      </div>

      <div style={{ display: "flex", flexWrap: "wrap", gap: 7, marginBottom: 12 }}>
        {confirmed.map((tag) => (
          <span className="pill" data-tone="ok" key={tag.tag_id}>
            {tag.name}
            <button
              className="chipx"
              aria-label={`Remove ${tag.name}`}
              title={`Remove ${tag.name}`}
              disabled={busy}
              onClick={() =>
                run(
                  () => api.removeAssetTag(assetId, tag.tag_id),
                  `Removed ${tag.name} — it will not be suggested here again`,
                )
              }
            >
              ×
            </button>
          </span>
        ))}
        {!confirmed.length && !suggested.length && tags && (
          <span className="faint" style={{ fontSize: "0.84rem" }}>
            No tags yet. Add one and FrameFound will look for it everywhere else.
          </span>
        )}
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          const name = draft.trim();
          if (!name) return;
          setDraft("");
          run(
            () => api.addAssetTag(assetId, name),
            `Added ${name} — looking for it in your other media`,
          );
        }}
        style={{ display: "flex", gap: 8 }}
      >
        <input
          className="input"
          style={{ flex: 1 }}
          placeholder="Add a tag — e.g. Power Broom"
          value={draft}
          maxLength={120}
          onChange={(e) => setDraft(e.target.value)}
          aria-label="Add a tag"
        />
        <button className="btn btn-primary" disabled={busy || !draft.trim()}>
          Add
        </button>
      </form>

      {suggested.length > 0 && (
        <>
          <div
            className="eyebrow"
            style={{ marginTop: 18, marginBottom: 8, display: "block" }}
          >
            Suggested — is this right?
          </div>
          <div style={{ display: "grid", gap: 7 }}>
            {suggested.map((tag) => (
              <div
                key={tag.tag_id}
                style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}
              >
                <span className="pill">{tag.name}</span>
                {tag.confidence != null && (
                  <span className="faint mono" style={{ fontSize: "0.72rem" }}>
                    {(tag.confidence * 100).toFixed(0)}% match
                  </span>
                )}
                <button
                  className="btn"
                  style={{ padding: "3px 11px", marginLeft: "auto" }}
                  disabled={busy}
                  onClick={() =>
                    run(
                      () => api.decideAssetTag(assetId, tag.tag_id, true),
                      `${tag.name} confirmed — that improves future matches`,
                    )
                  }
                >
                  Yes
                </button>
                <button
                  className="btn"
                  style={{ padding: "3px 11px" }}
                  disabled={busy}
                  onClick={() =>
                    run(
                      () => api.decideAssetTag(assetId, tag.tag_id, false),
                      `${tag.name} rejected — the match will be tightened`,
                    )
                  }
                >
                  No
                </button>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
