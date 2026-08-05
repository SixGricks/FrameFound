"use client";

// Contact-sheet browse. Defaults to "ready to view" because the first real
// session surfaced thousands of still-processing assets as blank tiles —
// the state is now filterable and always labelled rather than hidden.

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import { api, type AssetSummary, type Library } from "@/lib/api";
import { duration, resolution, shortDate } from "@/lib/format";

const PAGE_SIZE = 60;

function BrowsePage() {
  const params = useSearchParams();
  const router = useRouter();
  // Arriving from a tag hit or a Places card: the filter comes in on the URL so
  // the view is linkable and survives a reload.
  const [tag, setTag] = useState(params.get("tag") ?? "");
  const [includeSuggested, setIncludeSuggested] = useState(false);
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [libraryId, setLibraryId] = useState("");
  const [mediaType, setMediaType] = useState("");
  const [previewable, setPreviewable] = useState(true);
  const [sort, setSort] = useState("recent");
  const [page, setPage] = useState(1);

  const [items, setItems] = useState<AssetSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [busy, setBusy] = useState(true);

  useEffect(() => {
    api.libraries().then(setLibraries).catch(() => undefined);
  }, []);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const data = await api.assets({
        library_id: libraryId || undefined,
        media_type: mediaType || undefined,
        previewable: previewable || undefined,
        tag: tag || undefined,
        include_suggested_tags: tag && includeSuggested ? true : undefined,
        sort,
        page,
        page_size: PAGE_SIZE,
      });
      setItems(data.items);
      setTotal(data.total);
    } finally {
      setBusy(false);
    }
  }, [libraryId, mediaType, previewable, tag, includeSuggested, sort, page]);

  useEffect(() => {
    load();
  }, [load]);

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const reset = <T,>(setter: (v: T) => void) => (value: T) => {
    setter(value);
    setPage(1);
  };

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
              onClick={() => {
                setTag("");
                setPage(1);
              }}
            >
              ×
            </button>
          </span>
          <label className="faint" style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input
              type="checkbox"
              checked={includeSuggested}
              onChange={(e) => {
                setIncludeSuggested(e.target.checked);
                setPage(1);
              }}
            />
            Include suggestions I haven&rsquo;t reviewed
          </label>
        </div>
      )}

      <div className="toolbar">
        <select
          className="select"
          value={libraryId}
          onChange={(e) => reset(setLibraryId)(e.target.value)}
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
          onChange={(e) => reset(setMediaType)(e.target.value)}
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
          onChange={(e) => reset(setSort)(e.target.value)}
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
            onChange={(e) => reset(setPreviewable)(e.target.checked)}
            style={{ accentColor: "var(--amber)" }}
          />
          Ready to view
        </label>

        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
          <span className="faint mono" style={{ fontSize: "0.78rem" }}>
            {page} / {pages}
          </span>
          <button className="btn" disabled={page <= 1} onClick={() => setPage(page - 1)}>
            ←
          </button>
          <button className="btn" disabled={page >= pages} onClick={() => setPage(page + 1)}>
            →
          </button>
        </div>
      </div>

      {busy && items.length === 0 ? (
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
