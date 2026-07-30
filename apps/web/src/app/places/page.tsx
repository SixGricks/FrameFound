"use client";

// Places: the shoots the catalogue knows the location of.
//
// The map is a Google basemap when a key is configured, and a locally drawn
// scatter otherwise — see PlaceMap. Clicking a place opens it as a library
// view rather than expanding inline, so it can be linked and paged.

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import PlaceMap from "@/components/PlaceMap";
import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import { api, type MapConfig, type Place } from "@/lib/api";
import { shortDate } from "@/lib/format";

function placeHref(place: Place): string {
  // A little past the cluster edge so a member sitting exactly on the
  // boundary is not excluded by rounding.
  const radius = Math.max(place.radius_km * 1.15, 0.05).toFixed(3);
  const params = new URLSearchParams({
    lat: String(place.lat),
    lon: String(place.lon),
    radius,
    name: place.name,
  });
  return `/places/view?${params}`;
}

export default function PlacesPage() {
  const router = useRouter();
  const [places, setPlaces] = useState<Place[] | null>(null);
  const [mapConfig, setMapConfig] = useState<MapConfig | null>(null);
  const [includeInferred, setIncludeInferred] = useState(true);
  const [radiusKm, setRadiusKm] = useState(0.75);
  const [hovered, setHovered] = useState<Place | null>(null);
  const [busy, setBusy] = useState(true);

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

  useEffect(() => {
    api
      .mapConfig()
      .then(setMapConfig)
      .catch(() => setMapConfig(null));
  }, []);

  const open = useCallback((place: Place) => router.push(placeHref(place)), [router]);

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
          <div style={{ marginBottom: 18 }}>
            <PlaceMap
              places={places}
              config={mapConfig}
              selected={hovered}
              onSelect={open}
            />
          </div>

          <div className="grid">
            {places.map((place) => (
              <Link
                className="tile"
                key={`${place.lat},${place.lon}`}
                href={placeHref(place)}
                onMouseEnter={() => setHovered(place)}
                onMouseLeave={() => setHovered(null)}
              >
                <div className="tile-frame">
                  {place.cover_asset_id ? (
                    <Thumb assetId={place.cover_asset_id} mediaType="image" status="ready" />
                  ) : (
                    <div className="placeholder">
                      <span>no preview yet</span>
                    </div>
                  )}
                  {place.named_from === "geocode" && (
                    <span className="tile-badge">from map</span>
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
              </Link>
            ))}
          </div>
        </>
      )}
    </Shell>
  );
}
