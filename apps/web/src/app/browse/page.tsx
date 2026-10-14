"use client";

// Contact-sheet browse. Defaults to "ready to view" because the first real
// session surfaced thousands of still-processing assets as blank tiles —
// the state is now filterable and always labelled rather than hidden.
//
// Every filter, the sort and the page live in the URL. Pressing Back from an
// asset used to land on page 1 of all libraries, recently added, whatever had
// been chosen; and the Libraries page's "Browse" link (`?library=…`) was
// ignored. The URL is the state, so a view is linkable, survives a reload and
// comes back as it was.

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import { api, type AssetSummary, type Library } from "@/lib/api";
import { duration, resolution, shortDate } from "@/lib/format";
import { usePageTitle } from "@/lib/hooks";

const PAGE_SIZE = 60;

// The last page shown, so Back renders it at once (and the browser has a
// height to restore the scroll position to) while it is re-fetched.
let lastShown: { key: string; items: AssetSummary[]; total: number } | null = null;

function BrowsePage() {
  const params = useSearchParams();
  const router = useRouter();
  usePageTitle("Browse");

  const tag = params.get("tag") ?? "";
  const includeSuggested = params.get("suggested") === "1";
  const libraryId = params.get("library") ?? "";
  const mediaType = params.get("media") ?? "";
  const previewable = params.get("all") !== "1";
  const sort = params.get("sort") ?? "recent";
  const page = Math.max(1, Number(params.get("page") ?? "1") || 1);
  const key = params.toString();

  const [libraries, setLibraries] = useState<Library[]>([]);
  const [items, setItems] = useState<AssetSummary[]>(lastShown?.key === key ? lastShown.items : []);
  const [total, setTotal] = useState(lastShown?.key === key ? lastShown.total : 0);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.libraries().then(setLibraries).catch(() => undefined);
  }, []);

  /** Change filters in the URL. A new filter starts at page 1. */
  const update = useCallback(
    (changes: Record<string, string | null>, keepPage = false) => {
      const usp = new URLSearchParams(params.toString());
      for (const [name, value] of Object.entries(changes)) {
        if (value === null || value === "") usp.delete(name);
        else usp.set(name, value);
      }
      if (!keepPage) usp.delete("page");
      const s = usp.toString();
      router.replace(s ? `/browse?${s}` : "/browse", { scroll: false });
    },
    [params, router],
  );

  useEffect(() => {
    let live = true;
    setBusy(true);
    api
      .assets({
        library_id: libraryId || undefined,
        media_type: mediaType || undefined,
        previewable: previewable || undefined,
        tag: tag || undefined,
        include_suggested_tags: tag && includeSuggested ? true : undefined,
        sort,
        page,
        page_size: PAGE_SIZE,
      })
      .then((data) => {
        if (!live) return;
        lastShown = { key, items: data.items, total: data.total };
        setItems(data.items);
        setTotal(data.total);
        setError(null);
      })
      .catch((err) => {
        // A failed request is not "no assets match"; it is its own message.
        if (live) setError(err instanceof Error ? err.message : "Could not load assets");
      })
      .finally(() => {
        if (live) setBusy(false);
      });
    return () => {
      live = false;
    };
  }, [key, libraryId, mediaType, previewable, tag, includeSuggested, sort, page]);

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const setPage = (n: number) => update({ page: n <= 1 ? null : String(n) }, true);

  return (
    <>
      <div className="sectionhead">
        <h2>Browse</h2>
        <span className="faint mono">{total.toLocaleString()} assets</span>
      </div>

      {tag && (
        <div className="toolbar">
          <span className="pill" data-tone="ok">
            tag: {tag}
            <button
              className="chipx"
              aria-label="Clear the tag filter"
              onClick={() => update({ tag: null, suggested: null })}
            >
              ×
            </button>
          </span>
          <label className="faint" style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input
              type="checkbox"
              checked={includeSuggested}
              onChange={(e) => update({ suggested: e.target.checked ? "1" : null })}
            />
            Include suggestions I haven&rsquo;t reviewed
          </label>
        </div>
      )}

      <div className="toolbar">
        <select
          className="select"
          value={libraryId}
          onChange={(e) => update({ library: e.target.value })}
          aria-label="Library"
        >
          <option value="">All libraries</option>
          {libraries.map((lib) => (
            <option key={lib.id} value={lib.id}>
              {lib.name} ({lib.asset_count.toLocaleString()})
            </option>
          ))}
        </select>

        <select
          className="select"
          value={mediaType}
          onChange={(e) => update({ media: e.target.value })}
          aria-label="Media type"
        >
          <option value="">All media</option>
          <option value="video">Video</option>
          <option value="image">Images</option>
          <option value="audio">Audio</option>
        </select>

        <select
          className="select"
          value={sort}
          onChange={(e) => update({ sort: e.target.value === "recent" ? null : e.target.value })}
          aria-label="Sort"
        >
          <option value="recent">Recently added</option>
          <option value="captured">Capture date (newest)</option>
          <option value="captured_asc">Capture date (oldest)</option>
          <option value="name">Filename</option>
          <option value="size">Largest first</option>
        </select>

        <label
          className="pill"
          style={{ cursor: "pointer", gap: 7 }}
          data-tone={previewable ? "warn" : undefined}
        >
          <input
            type="checkbox"
            checked={previewable}
            onChange={(e) => update({ all: e.target.checked ? null : "1" })}
            style={{ accentColor: "var(--amber)" }}
          />
          Ready to view
        </label>

        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
          <span className="faint mono" style={{ fontSize: "0.78rem" }}>
            {page} / {pages}
          </span>
          <button
            className="btn"
            disabled={page <= 1}
            onClick={() => setPage(page - 1)}
            aria-label="Previous page"
          >
            ←
          </button>
          <button
            className="btn"
            disabled={page >= pages}
            onClick={() => setPage(page + 1)}
            aria-label="Next page"
          >
            →
          </button>
        </div>
      </div>

      {error ? (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
          Could not load this page: {error}
        </div>
      ) : busy && items.length === 0 ? (
        <div className="empty">Loading…</div>
      ) : items.length === 0 ? (
        <div className="empty">
          No assets match these filters.
          {previewable && " Try turning off “Ready to view” to include items still processing."}
        </div>
      ) : (
        <div className="grid">
          {items.map((asset, index) => (
            <Link
              key={asset.id}
              href={`/assets/${asset.id}`}
              className="tile"
              style={{ animationDelay: `${Math.min(index, 20) * 18}ms` }}
              title={asset.relative_path}
            >
              <div className="tile-frame">
                <Thumb
                  assetId={asset.id}
                  mediaType={asset.media_type}
                  status={asset.processing_status}
                />
                {asset.media_type === "video" && asset.duration_s !== null && (
                  <span className="tile-badge">{duration(asset.duration_s)}</span>
                )}
                {asset.media_type === "audio" && <span className="tile-badge">audio</span>}
                {asset.media_type === "image" && asset.relative_path.includes("/") && (
                  <button
                    type="button"
                    className="tile-badge"
                    style={{ right: "auto", left: 6, cursor: "pointer", border: 0 }}
                    title={`Start a listing from this shoot folder: ${asset.relative_path.slice(0, asset.relative_path.lastIndexOf("/"))}`}
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      const folder = asset.relative_path.slice(
                        0,
                        asset.relative_path.lastIndexOf("/"),
                      );
                      router.push(
                        `/listings?folder=${encodeURIComponent(folder)}&library=${asset.library_id}`,
                      );
                    }}
                  >
                    ＋ listing
                  </button>
                )}
              </div>
              <div className="tile-meta">
                <div className="tile-name">{asset.filename}</div>
                <div className="tile-sub">
                  <span>{resolution(asset.width, asset.height)}</span>
                  <span>{shortDate(asset.captured_at ?? asset.mtime)}</span>
                </div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </>
  );
}

export default function Page() {
  return (
    <Shell>
      <Suspense fallback={<div className="empty">Loading…</div>}>
        <BrowsePage />
      </Suspense>
    </Shell>
  );
}
