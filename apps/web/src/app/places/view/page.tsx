"use client";

// One place, browsed like a library: same tiles, same filters, same paging as
// /browse. Reached from a marker or a card on /places.
//
// A place has no stable id — clusters are recomputed on demand — so the URL
// carries the coordinate and radius that define it. That also makes the view
// linkable and survives a re-cluster, which an index would not.
//
// The filters, the sort and the page live in the URL beside it, as they do
// on /browse. They were component state: opening a photograph on page 3 of
// "video, newest first" and pressing Back landed on page 1 of everything,
// closest first.

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import { api, type NearbyAsset } from "@/lib/api";
import { setChatContext } from "@/lib/chatContext";
import { shortDate } from "@/lib/format";
import { usePageTitle } from "@/lib/hooks";

const PAGE_SIZE = 60;

// What the URL may ask for. Anything else (a hand-edited link) falls back to
// the first, rather than a select showing one thing while the grid filters
// by another.
const MEDIA = ["", "image", "video", "audio"];
const SOURCES = ["", "exif", "inferred"];
const SORTS = ["distance", "captured", "captured_asc", "name"];

function oneOf(allowed: string[], value: string | null): string {
  return value !== null && allowed.includes(value) ? value : (allowed[0] ?? "");
}

// The place last shown, so Back draws it at once — and the browser has a
// page tall enough to restore the scroll position to — while it is fetched
// again.
let lastShown: { key: string; assets: NearbyAsset[] } | null = null;

function PlaceView() {
  const params = useSearchParams();
  const router = useRouter();
  const lat = Number(params.get("lat"));
  const lon = Number(params.get("lon"));
  const radius = Number(params.get("radius")) || 0.5;
  const name = params.get("name") ?? "Place";
  usePageTitle(name);
  const placeKey = `${lat},${lon},${radius}`;

  const mediaType = oneOf(MEDIA, params.get("media"));
  const sourceFilter = oneOf(SOURCES, params.get("source"));
  const sort = oneOf(SORTS, params.get("sort"));
  const askedPage = Math.max(1, Number(params.get("page") ?? "1") || 1);

  const [assets, setAssets] = useState<NearbyAsset[] | null>(
    lastShown?.key === placeKey ? lastShown.assets : null,
  );
  const [error, setError] = useState<string | null>(null);

  // The query the URL is about to have. `router.replace` lands a moment
  // later, and a second change made before it did was built on the old
  // query and undid the first (seen: a media filter lost to a sort chosen
  // straight after it).
  const query = useRef(params.toString());
  useEffect(() => {
    query.current = params.toString();
  }, [params]);

  /** Change the view in the URL. A new filter or sort starts at page 1. */
  const update = useCallback(
    (changes: Record<string, string | null>, keepPage = false) => {
      const usp = new URLSearchParams(query.current);
      for (const [key, value] of Object.entries(changes)) {
        if (value === null || value === "") usp.delete(key);
        else usp.set(key, value);
      }
      if (!keepPage) usp.delete("page");
      query.current = usp.toString();
      router.replace(`/places/view?${query.current}`, { scroll: false });
    },
    [router],
  );

  const load = useCallback(async () => {
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
      setError("That link is missing a coordinate.");
      return;
    }
    try {
      const found = await api.assetsNear(lat, lon, radius);
      lastShown = { key: placeKey, assets: found };
      setAssets(found);
    } catch {
      setError("Could not load this place.");
    }
  }, [lat, lon, radius, placeKey]);

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
    if (sort === "captured_asc") {
      sorted.sort((a, b) =>
        (a.captured_at ?? "9999").localeCompare(b.captured_at ?? "9999"),
      );
    }
    if (sort === "name") sorted.sort((a, b) => a.filename.localeCompare(b.filename));
    return sorted;
  }, [assets, mediaType, sourceFilter, sort]);

  const pageCount = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  // A link to page 9 of a place that now has four pages shows the last one,
  // not an empty grid.
  const page = Math.min(askedPage, pageCount);
  /** One page forward or back — from the page the URL is about to show, so
   *  two quick presses of Next move two pages. */
  const turn = (by: number) => {
    const asked = Number(new URLSearchParams(query.current).get("page") ?? "1") || 1;
    const n = Math.min(pageCount, Math.max(1, Math.min(asked, pageCount) + by));
    update({ page: n <= 1 ? null : String(n) }, true);
  };
  const visible = shown.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const inferred = assets?.filter((a) => a.gps_source === "inferred").length ?? 0;

  // For the chat panel: which place, where it is, and what the grid shows.
  // A place has no id the tools know, so it goes in words.
  const where = Number.isFinite(lat) && Number.isFinite(lon)
    ? `at ${lat.toFixed(5)}, ${lon.toFixed(5)}, within ${(radius * 1000).toFixed(0)} m`
    : "";
  const showing =
    assets === null
      ? ""
      : `${shown.length} item${shown.length === 1 ? "" : "s"} shown, page ${page} of ${pageCount}`;
  const detail = [where, showing].filter(Boolean).join("; ");
  useEffect(
    () => setChatContext({ title: `Place “${name}”${detail ? ` — ${detail}` : ""}` }),
    [name, detail],
  );

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
        <select
          className="select"
          value={mediaType}
          onChange={(e) => update({ media: e.target.value })}
          aria-label="Media type"
        >
          <option value="">Everything</option>
          <option value="image">Images</option>
          <option value="video">Video</option>
          <option value="audio">Audio</option>
        </select>
        <select
          className="select"
          value={sourceFilter}
          onChange={(e) => update({ source: e.target.value })}
          aria-label="Where the position came from"
        >
          <option value="">Any position source</option>
          <option value="exif">Camera GPS only</option>
          <option value="inferred">Inferred only{inferred ? ` (${inferred})` : ""}</option>
        </select>
        <select
          className="select"
          value={sort}
          onChange={(e) => update({ sort: e.target.value === "distance" ? null : e.target.value })}
          aria-label="Sort"
        >
          <option value="distance">Closest first</option>
          <option value="captured">Newest first</option>
          <option value="captured_asc">Oldest first</option>
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
              <button className="btn" disabled={page === 1} onClick={() => turn(-1)}>
                Previous
              </button>
              <span className="faint mono">
                {page} / {pageCount}
              </span>
              <button className="btn" disabled={page === pageCount} onClick={() => turn(1)}>
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
