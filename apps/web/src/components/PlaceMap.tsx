"use client";

// Three ways to draw the same places, in order of how much they tell an
// outsider:
//
//   maplibre — vector tiles from a style URL the operator chooses. Point it at
//              your own OpenMapTiles or Protomaps server and no third party
//              learns anything. This is the recommended basemap.
//   google   — Google's tiles. Every pan is a request telling Google roughly
//              where you are looking.
//   none     — a scatter drawn from the coordinates already on this machine.
//              No outbound request at all, and still enough to see how the
//              jobs sit relative to one another.
//
// All three render the same markers and fire the same onSelect, so the rest of
// the page does not know or care which is active.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { MapConfig, Place } from "@/lib/api";
import type { GMap, GMarker } from "@/types/google-maps";
import type { MlMap, MlMarker } from "@/types/maplibre";

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

/** Load MapLibre (script + stylesheet) once per page. */
function loadMapLibre(libraryUrl: string, stylesheetUrl: string): Promise<void> {
  if (window.maplibregl) return Promise.resolve();
  if (window.__framefoundMapLibreLoading) return window.__framefoundMapLibreLoading;

  window.__framefoundMapLibreLoading = new Promise<void>((resolve, reject) => {
    if (stylesheetUrl && !document.querySelector(`link[href="${stylesheetUrl}"]`)) {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = stylesheetUrl;
      document.head.appendChild(link);
    }
    const script = document.createElement("script");
    script.src = libraryUrl;
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("MapLibre failed to load"));
    document.head.appendChild(script);
  });
  return window.__framefoundMapLibreLoading;
}

export default function PlaceMap({
  places,
  config,
  selected,
  onSelect,
}: {
  places: Place[];
  config: MapConfig | null;
  selected: Place | null;
  onSelect: (place: Place) => void;
}) {
  const provider = config?.basemap_enabled ? config.provider : "none";
  const browserKey = provider === "google" ? (config?.browser_key ?? "") : "";
  const holder = useRef<HTMLDivElement>(null);
  const mapRef = useRef<GMap | null>(null);
  const markersRef = useRef<GMarker[]>([]);
  const mlRef = useRef<MlMap | null>(null);
  const mlMarkersRef = useRef<MlMarker[]>([]);
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

  // MapLibre: same markers, same onSelect, tiles from wherever the operator
  // pointed the style. Nothing here needs an API key.
  useEffect(() => {
    if (provider !== "maplibre" || !config?.style_url) return;
    let cancelled = false;

    loadMapLibre(config.library_url, config.stylesheet_url)
      .then(() => {
        if (cancelled || !holder.current || !window.maplibregl) return;
        mlRef.current ??= new window.maplibregl.Map({
          container: holder.current,
          style: config.style_url,
          center: [places[0]?.lon ?? 0, places[0]?.lat ?? 0],
          zoom: 9,
        });
        mlRef.current.addControl(new window.maplibregl.NavigationControl(), "top-right");
        mlRef.current.on("load", () => !cancelled && setReady(true));
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });

    return () => {
      cancelled = true;
    };
  }, [provider, config?.style_url, config?.library_url, config?.stylesheet_url, places]);

  useEffect(() => {
    const map = mlRef.current;
    if (provider !== "maplibre" || !ready || !map || !window.maplibregl || !places.length) return;
    const api = window.maplibregl;

    mlMarkersRef.current.forEach((marker) => marker.remove());
    mlMarkersRef.current = places.map((place) => {
      // A DOM element rather than the default pin, so the count is readable
      // and the marker matches the rest of the interface.
      const el = document.createElement("button");
      el.type = "button";
      el.className = "mapdot";
      el.textContent = String(place.asset_count);
      el.title = `${place.name} — ${place.asset_count} assets`;
      el.addEventListener("click", () => onSelect(place));
      return new api.Marker({ element: el }).setLngLat([place.lon, place.lat]).addTo(map);
    });

    const lons = places.map((p) => p.lon);
    const lats = places.map((p) => p.lat);
    map.fitBounds(
      [
        [Math.min(...lons), Math.min(...lats)],
        [Math.max(...lons), Math.max(...lats)],
      ],
      { padding: 56, maxZoom: 15 },
    );
  }, [provider, places, onSelect, ready]);

  useEffect(() => {
    if (provider === "maplibre" && selected && mlRef.current) {
      mlRef.current.flyTo({ center: [selected.lon, selected.lat] });
    }
  }, [provider, selected]);

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

  const hasBasemap = !failed && (browserKey || (provider === "maplibre" && config?.style_url));
  if (hasBasemap) {
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
          {provider === "maplibre"
            ? "The map library or tile style could not load — check the URLs on the Security page. Showing positions without a basemap."
            : "Google Maps could not load — check the key and its referrer restrictions. Showing positions without a basemap."}
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
          Relative positions, drawn from the coordinates themselves — nothing
          leaves this machine. For a real basemap, add a self-hosted tile style
          on the Security page; OpenMapTiles or Protomaps keeps it all on your
          network.
        </p>
      )}
    </div>
  );
}
