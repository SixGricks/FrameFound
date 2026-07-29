"use client";

// Places: the shoots the catalogue knows the location of.
//
// No basemap. Fetching map tiles would send every coordinate in this archive
// to a third party, which is the opposite of what a self-hosted catalogue is
// for. The scatter shows the spatial relationship between jobs using only the
// coordinates already on this machine; the cards carry the detail.

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import { api, type NearbyAsset, type Place } from "@/lib/api";
import { shortDate } from "@/lib/format";

export default function PlacesPage() {
  const [places, setPlaces] = useState<Place[] | null>(null);
  const [includeInferred, setIncludeInferred] = useState(true);
  const [radiusKm, setRadiusKm] = useState(0.75);
  const [busy, setBusy] = useState(true);

  const [open, setOpen] = useState<Place | null>(null);
  const [openAssets, setOpenAssets] = useState<NearbyAsset[] | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setPlaces(await api.places(radiusKm, includeInferred));
    } finally {
      setBusy(false);
    }
  }, [radiusKm, includeInferred]);

  useEffect(() => {
    load().catch(() => setBusy(false));
  }, [load]);

  async function openPlace(place: Place) {
    setOpen(place);
    setOpenAssets(null);
    // A little past the cluster edge, so a member sitting exactly on the
    // boundary is not excluded by rounding.
    const radius = Math.max(place.radius_km * 1.15, 0.05);
    setOpenAssets(await api.assetsNear(place.lat, place.lon, radius));
  }

  const scatter = useMemo(() => {
    if (!places?.length) return null;
    const lats = places.map((p) => p.lat);
    const lons = places.map((p) => p.lon);
    const minLat = Math.min(...lats);
    const minLon = Math.min(...lons);
    const spanLat = Math.max(...lats) - minLat || 1;
    const spanLon = Math.max(...lons) - minLon || 1;
    const biggest = Math.max(...places.map((p) => p.asset_count));
    return places.map((p) => ({
      place: p,
      x: 6 + ((p.lon - minLon) / spanLon) * 88,
      // Inverted: north belongs at the top.
      y: 6 + (1 - (p.lat - minLat) / spanLat) * 88,
      r: 0.9 + (p.asset_count / biggest) * 2.6,
    }));
  }, [places]);

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Places</h2>
        {places && (
          <span className="faint mono">
            {places.length} places · {places.reduce((n, p) => n + p.asset_count, 0)} located
          </span>
        )}
      </div>

      <div className="toolbar">
        <select
          className="select"
          value={radiusKm}
          onChange={(e) => setRadiusKm(Number(e.target.value))}
        >
          <option value={0.25}>Group within 250 m</option>
          <option value={0.75}>Group within 750 m</option>
          <option value={2}>Group within 2 km</option>
          <option value={10}>Group within 10 km</option>
        </select>
        <label className="faint" style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <input
            type="checkbox"
            checked={includeInferred}
            onChange={(e) => setIncludeInferred(e.target.checked)}
          />
          Include inferred locations
        </label>
      </div>

      {busy && !places ? (
        <div className="empty">Working out where things were shot…</div>
      ) : !places?.length ? (
        <div className="empty">
          Nothing located yet. GPS comes from camera metadata, and can be inferred
          for cameras that were on the same job at the same time.
        </div>
      ) : (
        <>
          {scatter && scatter.length > 1 && (
            <div className="card" style={{ marginBottom: 18 }}>
              <svg
                viewBox="0 0 100 100"
                role="img"
                aria-label="Relative positions of each place"
                style={{ width: "100%", maxHeight: 280, display: "block" }}
              >
                {scatter.map(({ place, x, y, r }) => (
                  <circle
                    key={`${place.lat},${place.lon}`}
                    cx={x}
                    cy={y}
                    r={r}
                    fill="var(--amber)"
                    fillOpacity={open === place ? 0.95 : 0.5}
                    stroke="var(--amber)"
                    strokeWidth={0.3}
                    style={{ cursor: "pointer" }}
                    onClick={() => openPlace(place)}
                  >
                    <title>
                      {place.name} — {place.asset_count} assets
                    </title>
                  </circle>
                ))}
              </svg>
              <p className="faint" style={{ fontSize: "0.78rem", margin: "10px 0 0" }}>
                Relative positions, drawn from the coordinates themselves. No map
                tiles are fetched — that would hand your shoot locations to a
                third party.
              </p>
            </div>
          )}

          <div className="grid">
            {places.map((place) => (
              <button
                type="button"
                className="tile"
                key={`${place.lat},${place.lon}`}
                onClick={() => openPlace(place)}
                style={{ textAlign: "left", cursor: "pointer" }}
              >
                <div className="tile-frame">
                  {place.cover_asset_id ? (
                    <Thumb assetId={place.cover_asset_id} mediaType="image" status="ready" />
                  ) : (
                    <div className="placeholder">
                      <span>no preview yet</span>
                    </div>
                  )}
                </div>
                <div className="tile-meta">
                  <div className="tile-name">{place.name}</div>
                  <div className="tile-sub">
                    <span>
                      {place.asset_count} assets
                      {place.inferred_count > 0 && ` · ${place.inferred_count} inferred`}
                    </span>
                    <span>{shortDate(place.first_captured_at)}</span>
                  </div>
                </div>
              </button>
            ))}
          </div>

          {open && (
            <>
              <div className="sectionhead">
                <h2>{open.name}</h2>
                <span className="faint mono">
                  {open.lat.toFixed(5)}, {open.lon.toFixed(5)} · within{" "}
                  {(open.radius_km * 1000).toFixed(0)} m
                </span>
              </div>
              {!openAssets ? (
                <div className="empty">Loading…</div>
              ) : (
                <div className="grid">
                  {openAssets.map((asset) => (
                    <Link className="tile" key={asset.asset_id} href={`/assets/${asset.asset_id}`}>
                      <div className="tile-frame">
                        <Thumb
                          assetId={asset.asset_id}
                          mediaType={asset.media_type}
                          status="ready"
                        />
                        {asset.gps_source === "inferred" && (
                          <span className="tile-badge">inferred</span>
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
              )}
            </>
          )}
        </>
      )}
    </Shell>
  );
}
