"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import Shell from "@/components/Shell";
import { api, type Library } from "@/lib/api";
import { relativeTime } from "@/lib/format";

export default function LibrariesPage() {
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);

  async function load() {
    setLibraries(await api.libraries());
    setBusy(false);
  }

  useEffect(() => {
    load().catch(() => setBusy(false));
  }, []);

  async function scan(library: Library) {
    try {
      await api.scanLibrary(library.id);
      setNotice(`Scan queued for ${library.name}`);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not start scan");
    }
    setTimeout(() => setNotice(null), 3500);
  }

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Libraries</h2>
        {notice && <span className="faint">{notice}</span>}
      </div>

      {busy ? (
        <div className="empty">Loading…</div>
      ) : libraries.length === 0 ? (
        <div className="empty">No libraries yet.</div>
      ) : (
        <div style={{ display: "grid", gap: 14 }}>
          {libraries.map((lib) => (
            <div className="card" key={lib.id}>
              <div
                style={{
                  display: "flex",
                  gap: 14,
                  alignItems: "center",
                  flexWrap: "wrap",
                }}
              >
                <div style={{ minWidth: 0, flex: 1 }}>
                  <h2 style={{ fontSize: "1.25rem" }}>{lib.name}</h2>
                  <p className="faint mono" style={{ fontSize: "0.78rem", margin: "4px 0 0" }}>
                    {lib.root_path}
                  </p>
                </div>
                <div style={{ display: "flex", gap: 7, flexWrap: "wrap" }}>
                  {lib.read_only && <span className="pill" data-tone="ok">read only</span>}
                  {lib.watcher_enabled && <span className="pill">watching</span>}
                  {lib.generate_proxies ? (
                    <span className="pill">proxies {lib.proxy_resolution}p</span>
                  ) : (
                    <span className="pill">proxies off</span>
                  )}
                  {lib.transcribe_enabled && <span className="pill">transcribe</span>}
                  {!lib.enabled && <span className="pill" data-tone="bad">disabled</span>}
                </div>
              </div>

              <div
                style={{
                  display: "flex",
                  gap: 18,
                  alignItems: "center",
                  marginTop: 16,
                  flexWrap: "wrap",
                }}
              >
                <span className="mono" style={{ fontSize: "1.35rem" }}>
                  {lib.asset_count.toLocaleString()}
                  <span className="faint" style={{ fontSize: "0.8rem" }}> assets</span>
                </span>
                <span className="faint" style={{ fontSize: "0.82rem" }}>
                  last scan {relativeTime(lib.last_scan_at)}
                </span>
                <div style={{ marginLeft: "auto", display: "flex", gap: 9 }}>
                  <Link className="btn" href={`/browse?library=${lib.id}`}>
                    Browse
                  </Link>
                  <button className="btn" onClick={() => scan(lib)}>
                    Scan now
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </Shell>
  );
}
