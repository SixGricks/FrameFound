"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import PathMappingsEditor from "@/components/PathMappingsEditor";
import Shell from "@/components/Shell";
import { api, type Library } from "@/lib/api";
import { relativeTime } from "@/lib/format";

// How often a library is re-read. A scan of ~9,000 unchanged files takes
// seconds, and on a NAS nothing else notices new files: the watcher only
// hears changes made through this machine, not a shoot copied from a laptop.
const SCHEDULES: { minutes: number | null; label: string }[] = [
  { minutes: 60, label: "Rescan hourly" },
  { minutes: 360, label: "Every 6 hours" },
  { minutes: 1440, label: "Daily" },
  { minutes: null, label: "Manual only" },
];

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

  async function schedule(library: Library, minutes: number | null) {
    try {
      const updated = await api.updateLibrary(library.id, { scan_interval_minutes: minutes });
      setLibraries((all) =>
        all.map((lib) => (lib.id === updated.id ? { ...lib, ...updated, asset_count: lib.asset_count } : lib)),
      );
      setNotice(`${library.name}: ${SCHEDULES.find((s) => s.minutes === minutes)?.label ?? "saved"}`);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not change the schedule");
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
                <select
                  className="input"
                  style={{ width: "auto", padding: "4px 8px", fontSize: "0.82rem" }}
                  value={lib.scan_interval_minutes === null ? "" : String(lib.scan_interval_minutes)}
                  onChange={(e) => schedule(lib, e.target.value === "" ? null : Number(e.target.value))}
                  aria-label={`Rescan schedule for ${lib.name}`}
                  title="New files on a network share only appear after a scan"
                >
                  {SCHEDULES.map((s) => (
                    <option key={s.label} value={s.minutes === null ? "" : String(s.minutes)}>
                      {s.label}
                    </option>
                  ))}
                  {lib.scan_interval_minutes !== null &&
                    !SCHEDULES.some((s) => s.minutes === lib.scan_interval_minutes) && (
                      <option value={String(lib.scan_interval_minutes)}>
                        Every {lib.scan_interval_minutes} min
                      </option>
                    )}
                </select>
                <div style={{ marginLeft: "auto", display: "flex", gap: 9 }}>
                  <Link className="btn" href={`/browse?library=${lib.id}`}>
                    Browse
                  </Link>
                  <button className="btn" onClick={() => scan(lib)}>
                    Scan now
                  </button>
                </div>
              </div>
              <PathMappingsEditor libraryId={lib.id} libraryName={lib.name} />
            </div>
          ))}
        </div>
      )}
    </Shell>
  );
}
