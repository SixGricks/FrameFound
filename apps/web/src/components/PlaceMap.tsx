"use client";

// Two ways to draw the same places.
//
// With a Google Maps key configured, a real basemap — which means tile
// requests to Google that reveal roughly where the operator is looking. That
// is a deliberate opt-in, off by default, configured on the Security page.
//
// Without one, a scatter drawn from the coordinates themselves. No outbound
// request, no third party, and still enough to see how the jobs sit relative
// to each other.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Place } from "@/lib/api";
import type { GMap, GMarker } from "@/types/google-maps";

/** Load the Maps JS API once per page, however many components ask for it. */
function loadMaps(key: string): Promise<void> {
  if (window.google?.maps) return Promise.resolve();
  if (window.__framefoundMapsLoading) return window.__framefoundMapsLoading;

  window.__framefoundMapsLoading = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(key)}&v=weekly`;
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("Google Maps failed to load"));
    document.head.appendChild(script);
  });
  return window.__framefoundMapsLoading;
}

export default function PlaceMap({
  places,
  browserKey,
  selected,
  onSelect,
}: {
  places: Place[];
  browserKey: string;
  selected: Place | null;
  onSelect: (place: Place) => void;
}) {
  const holder = useRef<HTMLDivElement>(null);
  const mapRef = useRef<GMap | null>(null);
  const markersRef = useRef<GMarker[]>([]);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!browserKey) return;
    let cancelled = false;

    loadMaps(browserKey)
      .then(() => {
        if (cancelled || !holder.current || !window.google) return;
        mapRef.current ??= new window.google.maps.Map(holder.current, {
          // Aerial: these are properties and job sites, not street routes.
          mapTypeId: "hybrid",
          streetViewControl: false,
          mapTypeControl: true,
          fullscreenControl: false,
        });
        setReady(true);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });

    return () => {
      cancelled = true;
    };
  }, [browserKey]);

  // Markers are rebuilt whenever the clustering changes — which happens when
  // the radius or the inferred filter moves.
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map || !window.google || !places.length) return;
    const api = window.google.maps;

    markersRef.current.forEach((marker) => marker.setMap(null));
    markersRef.current = places.map((place) => {
      const marker = new api.Marker({
        position: { lat: place.lat, lng: place.lon },
        map,
        title: `${place.name} — ${place.asset_count} assets`,
        label: {
          text: String(place.asset_count),
          color: "#12100e",
          fontSize: "11px",
          fontWeight: "700",
        },
      });
      marker.addListener("click", () => onSelect(place));
      return marker;
    });

    const bounds = new api.LatLngBounds();
    places.forEach((place) => bounds.extend({ lat: place.lat, lng: place.lon }));
    map.fitBounds(bounds, 48);
  }, [places, onSelect, ready]);

  // Centre on a place picked from the list, without resetting the zoom the
  // operator has chosen.
  useEffect(() => {
    if (selected && mapRef.current) {
      mapRef.current.panTo({ lat: selected.lat, lng: selected.lon });
    }
  }, [selected]);

  const scatter = useMemo(() => {
    if (!places.length) return [];
    const lats = places.map((p) => p.lat);
    const lons = places.map((p) => p.lon);
    const minLat = Math.min(...lats);
    const minLon = Math.min(...lons);
    const spanLat = Math.max(...lats) - minLat || 1;
    const spanLon = Math.max(...lons) - minLon || 1;
    const biggest = Math.max(...places.map((p) => p.asset_count));
    return places.map((place) => ({
      place,
      x: 6 + ((place.lon - minLon) / spanLon) * 88,
      y: 6 + (1 - (place.lat - minLat) / spanLat) * 88, // north at the top
      r: 0.9 + (place.asset_count / biggest) * 2.6,
    }));
  }, [places]);

  const select = useCallback((place: Place) => onSelect(place), [onSelect]);

  if (browserKey && !failed) {
    return (
      <div className="card" style={{ padding: 0, overflow: "hidden" }}>
        <div ref={holder} style={{ width: "100%", height: 380 }} />
      </div>
    );
  }

  return (
    <div className="card">
      {failed && (
        <p className="faint" style={{ marginTop: 0, fontSize: "0.84rem" }}>
          Google Maps could not load — check the key and its referrer
          restrictions. Showing positions without a basemap.
        </p>
      )}
      <svg
        viewBox="0 0 100 100"
        role="img"
        aria-label="Relative positions of each place"
        style={{ width: "100%", maxHeight: 300, display: "block" }}
      >
        {scatter.map(({ place, x, y, r }) => (
          <circle
            key={`${place.lat},${place.lon}`}
            cx={x}
            cy={y}
            r={r}
            fill="var(--amber)"
            fillOpacity={selected === place ? 0.95 : 0.5}
            stroke="var(--amber)"
            strokeWidth={0.3}
            style={{ cursor: "pointer" }}
            onClick={() => select(place)}
          >
            <title>
              {place.name} — {place.asset_count} assets
            </title>
          </circle>
        ))}
      </svg>
      {!failed && (
        <p className="faint" style={{ fontSize: "0.78rem", margin: "10px 0 0" }}>
          Relative positions, drawn from the coordinates themselves. Add a
          Google Maps key on the Security page for a real basemap — that sends
          tile requests to Google.
        </p>
      )}
    </div>
  );
}
