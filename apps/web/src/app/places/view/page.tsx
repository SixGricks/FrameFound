"use client";

// One place, browsed like a library: same tiles, same filters, same paging as
// /browse. Reached from a marker or a card on /places.
//
// A place has no stable id — clusters are recomputed on demand — so the URL
// carries the coordinate and radius that define it. That also makes the view
// linkable and survives a re-cluster, which an index would not.

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import { api, type NearbyAsset } from "@/lib/api";
import { shortDate } from "@/lib/format";

const PAGE_SIZE = 60;

function PlaceView() {
  const params = useSearchParams();
  const lat = Number(params.get("lat"));
  const lon = Number(params.get("lon"));
  const radius = Number(params.get("radius")) || 0.5;
  const name = params.get("name") ?? "Place";

  const [assets, setAssets] = useState<NearbyAsset[] | null>(null);
  const [mediaType, setMediaType] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [sort, setSort] = useState("distance");
  const [page, setPage] = useState(1);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      setError("That link is missing a coordinate.");
      return;
    }
    try {
      setAssets(await api.assetsNear(lat, lon, radius));
    } catch {
      setError("Could not load this place.");
    }
  }, [lat, lon, radius]);

  useEffect(() => {
    load();
  }, [load]);

  // Filtering and sorting happen here rather than server-side: a place holds
  // hundreds of assets, not thousands, and the round trip is already done.
  const shown = useMemo(() => {
    if (!assets) return [];
    let rows = assets;
    if (mediaType) rows = rows.filter((a) => a.media_type === mediaType);
    if (sourceFilter === "inferred") rows = rows.filter((a) => a.gps_source === "inferred");
    if (sourceFilter === "exif") rows = rows.filter((a) => a.gps_source !== "inferred");

    const sorted = [...rows];
    if (sort === "distance") sorted.sort((a, b) => a.distance_km - b.distance_km);
    if (sort === "captured") {
      sorted.sort((a, b) => (b.captured_at ?? "").localeCompare(a.captured_at ?? ""));
    }
    if (sort === "name") sorted.sort((a, b) => a.filename.localeCompare(b.filename));
    return sorted;
  }, [assets, mediaType, sourceFilter, sort]);

  const pageCount = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  const visible = shown.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const inferred = assets?.filter((a) => a.gps_source === "inferred").length ?? 0;

  useEffect(() => {
    setPage(1);
  }, [mediaType, sourceFilter, sort]);

  if (error) {
    return (
      <>
        <div className="empty">{error}</div>
        <Link className="btn" href="/places">
          Back to places
        </Link>
      </>
    );
  }

  return (
    <>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>{name}</h2>
        <span className="faint mono">
          {lat.toFixed(5)}, {lon.toFixed(5)} · within {(radius * 1000).toFixed(0)} m
        </span>
      </div>

      <div className="toolbar">
        <Link className="btn" href="/places" style={{ padding: "6px 12px" }}>
          ← All places
        </Link>
        <select className="select" value={mediaType} onChange={(e) => setMediaType(e.target.value)}>
          <option value="">Everything</option>
          <option value="image">Images</option>
          <option value="video">Video</option>
          <option value="audio">Audio</option>
        </select>
        <select
          className="select"
          value={sourceFilter}
          onChange={(e) => setSourceFilter(e.target.value)}
        >
          <option value="">Any position source</option>
          <option value="exif">Camera GPS only</option>
          <option value="inferred">Inferred only{inferred ? ` (${inferred})` : ""}</option>
        </select>
        <select className="select" value={sort} onChange={(e) => setSort(e.target.value)}>
          <option value="distance">Closest first</option>
          <option value="captured">Newest first</option>
          <option value="name">By name</option>
        </select>
        {assets && (
          <span className="faint mono">
            {shown.length} of {assets.length}
          </span>
        )}
      </div>

      {!assets ? (
        <div className="empty">Loading…</div>
      ) : !visible.length ? (
        <div className="empty">Nothing here matches those filters.</div>
      ) : (
        <>
          <div className="grid">
            {visible.map((asset, index) => (
              <Link
                className="tile"
                key={asset.asset_id}
                href={`/assets/${asset.asset_id}`}
                title={asset.filename}
                style={{ animationDelay: `${Math.min(index, 20) * 18}ms` }}
              >
                <div className="tile-frame">
                  <Thumb assetId={asset.asset_id} mediaType={asset.media_type} status="ready" />
                  {asset.gps_source === "inferred" && (
                    <span className="tile-badge">
                      inferred
                      {asset.gps_confidence != null &&
                        ` ${(asset.gps_confidence * 100).toFixed(0)}%`}
                    </span>
                  )}
                </div>
                <div className="tile-meta">
                  <div className="tile-name">{asset.filename}</div>
                  <div className="tile-sub">
                    <span>{asset.distance_km.toFixed(2)} km</span>
                    <span>{shortDate(asset.captured_at)}</span>
                  </div>
                </div>
              </Link>
            ))}
          </div>

          {pageCount > 1 && (
            <div className="toolbar" style={{ justifyContent: "center", marginTop: 18 }}>
              <button
                className="btn"
                disabled={page === 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                Previous
              </button>
              <span className="faint mono">
                {page} / {pageCount}
              </span>
              <button
                className="btn"
                disabled={page === pageCount}
                onClick={() => setPage((p) => Math.min(pageCount, p + 1))}
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </>
  );
}

export default function PlaceViewPage() {
  return (
    <Shell>
      <Suspense fallback={<div className="empty">Loading…</div>}>
        <PlaceView />
      </Suspense>
    </Shell>
  );
}
