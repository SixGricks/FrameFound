"use client";

// M3 proof-of-concept browser: thumbnail grid + click-to-play proxies.
// Media requests ride the session cookie (same-origin); the polished search
// experience replaces this in M6.

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

type Library = { id: string; name: string; asset_count: number };
type Asset = {
  id: string;
  filename: string;
  media_type: "image" | "video" | "audio";
  duration_s: number | null;
  processing_status: string;
};
type AssetPage = { items: Asset[]; total: number; page: number; page_size: number };

export default function BrowsePage() {
  const router = useRouter();
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [libraryId, setLibraryId] = useState<string>("");
  const [assets, setAssets] = useState<Asset[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [playing, setPlaying] = useState<Asset | null>(null);

  useEffect(() => {
    fetch("/api/v1/auth/me").then((r) => {
      if (r.status === 401) router.push("/login");
    });
    fetch("/api/v1/libraries")
      .then((r) => (r.ok ? r.json() : []))
      .then(setLibraries);
  }, [router]);

  const loadAssets = useCallback(async () => {
    const params = new URLSearchParams({ page: String(page), page_size: "60" });
    if (libraryId) params.set("library_id", libraryId);
    const resp = await fetch(`/api/v1/assets?${params}`);
    if (resp.ok) {
      const data: AssetPage = await resp.json();
      setAssets(data.items);
      setTotal(data.total);
    }
  }, [libraryId, page]);

  useEffect(() => {
    loadAssets();
  }, [loadAssets]);

  const pages = Math.max(1, Math.ceil(total / 60));

  return (
    <main style={styles.page}>
      <header style={styles.header}>
        <strong style={{ fontSize: 18 }}>FrameFound</strong>
        <select
          style={styles.select}
          value={libraryId}
          onChange={(e) => {
            setLibraryId(e.target.value);
            setPage(1);
          }}
        >
          <option value="">All libraries ({total})</option>
          {libraries.map((lib) => (
            <option key={lib.id} value={lib.id}>
              {lib.name} ({lib.asset_count})
            </option>
          ))}
        </select>
        <span style={{ color: "#8b95a5", marginLeft: "auto" }}>
          page {page}/{pages}
        </span>
        <button style={styles.pageBtn} disabled={page <= 1} onClick={() => setPage(page - 1)}>
          ‹
        </button>
        <button
          style={styles.pageBtn}
          disabled={page >= pages}
          onClick={() => setPage(page + 1)}
        >
          ›
        </button>
      </header>

      <div style={styles.grid}>
        {assets.map((asset) => (
          <figure
            key={asset.id}
            style={styles.cell}
            onClick={() => asset.media_type === "video" && setPlaying(asset)}
            title={asset.filename}
          >
            {/* Thumbnails 404 until generated; the broken-image state is hidden. */}
            <img
              src={`/api/v1/media/${asset.id}/thumbnail`}
              alt={asset.filename}
              style={styles.thumb}
              loading="lazy"
              onError={(e) => {
                (e.target as HTMLImageElement).style.visibility = "hidden";
              }}
            />
            {asset.media_type === "video" && <span style={styles.badge}>▶</span>}
            <figcaption style={styles.caption}>{asset.filename}</figcaption>
          </figure>
        ))}
      </div>

      {playing && (
        <div style={styles.overlay} onClick={() => setPlaying(null)}>
          <div style={{ width: "min(1100px, 94vw)" }} onClick={(e) => e.stopPropagation()}>
            <p style={{ color: "#e8ecf3", margin: "0 0 8px" }}>{playing.filename}</p>
            <video
              src={`/api/v1/media/${playing.id}/proxy`}
              poster={`/api/v1/media/${playing.id}/poster`}
              controls
              autoPlay
              style={{ width: "100%", borderRadius: 8, background: "#000" }}
            />
          </div>
        </div>
      )}
    </main>
  );
}

const styles: Record<string, React.CSSProperties> = {
  page: {
    minHeight: "100dvh",
    background: "#0f1420",
    color: "#e8ecf3",
    fontFamily: "system-ui, sans-serif",
  },
  header: {
    display: "flex",
    alignItems: "center",
    gap: 12,
    padding: "14px 20px",
    borderBottom: "1px solid #1f2940",
    position: "sticky",
    top: 0,
    background: "#0f1420",
  },
  select: {
    background: "#161d2e",
    color: "#e8ecf3",
    border: "1px solid #2a3550",
    borderRadius: 8,
    padding: "6px 10px",
  },
  pageBtn: {
    background: "#161d2e",
    color: "#e8ecf3",
    border: "1px solid #2a3550",
    borderRadius: 8,
    padding: "4px 12px",
    cursor: "pointer",
  },
  grid: {
    display: "grid",
    gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
    gap: 10,
    padding: 20,
  },
  cell: {
    margin: 0,
    background: "#161d2e",
    borderRadius: 10,
    overflow: "hidden",
    cursor: "pointer",
    position: "relative",
  },
  thumb: { width: "100%", aspectRatio: "16/10", objectFit: "cover", display: "block" },
  badge: {
    position: "absolute",
    top: 8,
    right: 8,
    background: "rgba(0,0,0,.6)",
    borderRadius: 999,
    padding: "2px 8px",
    fontSize: 12,
  },
  caption: {
    padding: "6px 8px",
    fontSize: 12,
    color: "#8b95a5",
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
  },
  overlay: {
    position: "fixed",
    inset: 0,
    background: "rgba(4,8,16,.85)",
    display: "grid",
    placeItems: "center",
    zIndex: 10,
  },
};
