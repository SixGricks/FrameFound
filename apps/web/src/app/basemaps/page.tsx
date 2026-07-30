"use client";

// Basemaps: downloading the map itself, so the maps work offline.
//
// The important thing this page communicates is that a basemap is *one file*
// in the data directory. There is no tile server to run and nothing to keep
// alive, and once a region is here nothing about the map leaves the network.
//
// Extraction takes minutes and reports nothing while it runs — pmtiles has no
// progress output to relay — so the page polls and says plainly that it is
// working rather than showing a bar it would have to invent.

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import Shell from "@/components/Shell";
import { api, type Basemap, type BasemapList } from "@/lib/api";

// While something is extracting, re-check often enough to feel live without
// hammering an endpoint that stats a directory.
const POLL_MS = 5000;

export default function BasemapsPage() {
  const [data, setData] = useState<BasemapList | null>(null);
  const [busy, setBusy] = useState<string>("");
  const [error, setError] = useState<string>("");
  // Names we asked for that are not on disk yet. Extraction writes to a .part
  // file, so "not installed but requested" is the only signal available.
  const [extracting, setExtracting] = useState<string[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.basemaps();
      setData(next);
      setExtracting((names) =>
        names.filter((n) => !next.basemaps.some((b) => b.name === n && b.installed)),
      );
    } catch {
      setError("Could not read the basemap directory.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Poll only while something is actually in flight.
  useEffect(() => {
    if (!extracting.length) return;
    timer.current = setTimeout(load, POLL_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [extracting, data, load]);

  async function download(map: Basemap) {
    setBusy(map.name);
    setError("");
    try {
      await api.downloadBasemap(map.name);
      setExtracting((names) => [...new Set([...names, map.name])]);
    } catch {
      setError(`Could not start the download for ${map.label}.`);
    } finally {
      setBusy("");
    }
  }

  async function remove(map: Basemap) {
    setBusy(map.name);
    setError("");
    try {
      await api.deleteBasemap(map.name);
      await load();
    } catch {
      setError(`Could not delete ${map.label}.`);
    } finally {
      setBusy("");
    }
  }

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Basemaps</h2>
        {data && (
          <span className="faint mono">
            {data.installed_count} installed
          </span>
        )}
      </div>

      <p className="faint" style={{ maxWidth: "60ch" }}>
        {data?.note ??
          "A basemap is one file served straight out of your data directory."}{" "}
        <Link href="/places">Places</Link> uses it to draw the map.
      </p>

      {error && <div className="empty">{error}</div>}

      {!data ? (
        <div className="empty">Loading…</div>
      ) : (
        <div className="tablewrap card" style={{ padding: 0 }}>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Region</th>
                <th scope="col">Size</th>
                <th scope="col">Status</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {data.basemaps.map((map) => {
                const working = extracting.includes(map.name);
                return (
                  <tr key={map.name}>
                    <th scope="row" style={{ fontWeight: 500 }}>
                      {map.label}
                    </th>
                    <td className="mono faint">
                      {map.installed && map.size_gb != null
                        ? `${map.size_gb} GB`
                        : map.approx_gb != null
                          ? `~${map.approx_gb} GB`
                          : "—"}
                    </td>
                    <td>
                      {map.installed ? (
                        <span className="pill" data-tone="ok">
                          Installed
                        </span>
                      ) : working ? (
                        <span className="pill">Extracting…</span>
                      ) : (
                        <span className="faint">Not downloaded</span>
                      )}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      {map.installed ? (
                        <button
                          className="btn"
                          disabled={busy === map.name}
                          onClick={() => remove(map)}
                        >
                          Delete
                        </button>
                      ) : (
                        <button
                          className="btn"
                          disabled={busy === map.name || working}
                          onClick={() => download(map)}
                        >
                          {working ? "Extracting…" : "Download"}
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {extracting.length > 0 && (
        <p className="faint" style={{ maxWidth: "60ch" }}>
          Extraction pulls just your bounding box out of a 125 GB planet archive
          over range requests, so it takes several minutes and reports nothing
          until it finishes. You can leave this page — it carries on in the
          background.
        </p>
      )}

      <h3>How this works</h3>
      <p className="faint" style={{ maxWidth: "60ch" }}>
        Each region is a single PMTiles archive. FrameFound serves byte ranges
        out of it using the same code that scrubs video, so there is no tile
        server, no extra container and nothing else to monitor. Deleting a
        basemap is safe — it can always be downloaded again, and nothing else
        depends on it.
      </p>
    </Shell>
  );
}
