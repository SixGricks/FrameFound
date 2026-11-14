"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";

import PathMappingsEditor from "@/components/PathMappingsEditor";
import ReviewWithAi, { type ReviewTarget } from "@/components/ReviewWithAi";
import Shell from "@/components/Shell";
import { api, type Library, type LookStatus } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { ingestNotes } from "@/lib/looks";
import { usePageTitle } from "@/lib/hooks";

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
  usePageTitle("Libraries");
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  // Whether a local model can look at anything on this install, and who may
  // say so. null until the server has answered, and for good on a server
  // too old to be asked: then nothing about looking is shown.
  const [looking, setLooking] = useState<LookStatus | null>(null);
  // "Look at new footage with AI", per library: the box as just ticked while
  // it is being saved, and the server's sentence when the save was refused.
  const [lookSaving, setLookSaving] = useState<Record<string, boolean>>({});
  const [lookProblem, setLookProblem] = useState<Record<string, string>>({});
  // The library the "Review with AI" dialog is open on, and the button that
  // opened it (focus goes back there).
  const [reviewing, setReviewing] = useState<ReviewTarget | null>(null);
  const reviewOpener = useRef<HTMLElement | null>(null);

  async function load() {
    setLibraries(await api.libraries());
    setBusy(false);
  }

  useEffect(() => {
    load().catch(() => setBusy(false));
    api
      .lookStatus()
      .then(setLooking)
      .catch(() => undefined);
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

  /** Switch looking at a library's new footage on or off. The box shows the
   *  new state at once; a save the server refuses puts it back and leaves
   *  the server's sentence in the card. */
  async function lookAtNew(library: Library, on: boolean) {
    // One save at a time for a library. The box is not disabled meanwhile:
    // disabled, it dropped the keyboard's focus to the top of the page, and
    // the Space meant to untick it again went nowhere. A press that lands
    // while a save is out is left unanswered; the box shows what was asked.
    if (library.id in lookSaving) return;
    const without = <T,>(all: Record<string, T>) =>
      Object.fromEntries(Object.entries(all).filter(([id]) => id !== library.id));
    setLookSaving((all) => ({ ...all, [library.id]: on }));
    setLookProblem(without);
    try {
      const updated = await api.updateLibrary(library.id, { look_at_ingest: on });
      setLibraries((all) =>
        all.map((lib) =>
          lib.id === updated.id ? { ...lib, ...updated, asset_count: lib.asset_count } : lib,
        ),
      );
    } catch (err) {
      setLookProblem((all) => ({
        ...all,
        [library.id]: err instanceof Error ? err.message : "Could not save this.",
      }));
    } finally {
      setLookSaving(without);
    }
  }

  /** Why the box cannot be ticked, or null when it can. Unticking is never
   *  refused, so a box that is on is never disabled: it could not be turned
   *  off again where models have stopped being able to run. */
  function lookBlocked(library: Library): string | null {
    if (!looking || library.look_at_ingest) return null;
    if (!looking.available) return looking.reason ?? "Local AI models cannot run here.";
    return looking.can_manage ? null : "Only an administrator can change this.";
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
                  {looking && (
                    <label
                      className="pill lib-look"
                      data-tone={(lookSaving[lib.id] ?? lib.look_at_ingest) ? "warn" : undefined}
                      title={lookBlocked(lib) ?? undefined}
                    >
                      <input
                        type="checkbox"
                        checked={lookSaving[lib.id] ?? lib.look_at_ingest}
                        disabled={lookBlocked(lib) !== null}
                        aria-busy={lib.id in lookSaving || undefined}
                        aria-label={`Look at new footage in ${lib.name} with AI`}
                        onChange={(e) => lookAtNew(lib, e.target.checked)}
                      />
                      Look at new footage with AI
                    </label>
                  )}
                </div>
              </div>
              {/* What the box covers, where it can be ticked or is on; and
                  why it will not do what was hoped, once it is on. */}
              {looking &&
                (looking.available || lib.look_at_ingest) &&
                ingestNotes({
                  on: lookSaving[lib.id] ?? lib.look_at_ingest,
                  makesCopies: lib.generate_proxies,
                  // Not known where models cannot run: say nothing of it.
                  hasLooker: !looking.available || Boolean(looking.looker?.model_id),
                }).map((line) => (
                  <p className="faint lib-looknote" key={line}>
                    {line}
                  </p>
                ))}
              {lookProblem[lib.id] && (
                <p className="lib-looknote" role="alert" style={{ color: "var(--ember)" }}>
                  {lookProblem[lib.id]}
                </p>
              )}

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
                <div style={{ marginLeft: "auto", display: "flex", gap: 9, flexWrap: "wrap" }}>
                  <Link className="btn" href={`/browse?library=${lib.id}`}>
                    Browse
                  </Link>
                  <Link
                    className="btn"
                    href={`/browse?view=folders&library=${lib.id}`}
                    title={`Walk ${lib.name} folder by folder`}
                  >
                    Folders
                  </Link>
                  {looking && (
                    <button
                      type="button"
                      className="btn"
                      title={`See what it would take for a local model to look through ${lib.name}`}
                      onClick={(event) => {
                        reviewOpener.current = event.currentTarget;
                        setReviewing({ libraryId: lib.id, libraryName: lib.name, folder: "" });
                      }}
                    >
                      Review with AI
                    </button>
                  )}
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

      {/* Mounted once, for whichever library's button was pressed. Opening
          it only reads; a review starts when Start is pressed inside it. */}
      <ReviewWithAi
        target={reviewing}
        returnFocus={() => reviewOpener.current}
        onClose={() => setReviewing(null)}
      />
    </Shell>
  );
}
