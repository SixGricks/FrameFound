"use client";

// The Google Drive organizer: paste a folder link, see the whole rename plan,
// approve it, and keep an undo.
//
// Nothing is written until "Rename" is pressed, and what gets written is
// exactly the visible plan — rows can be dropped first, and the numbering
// closes ranks. Undo reads the manifest FrameFound left in the folder.

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";

import {
  api,
  type GdrivePreview,
  type GdriveRename,
  type GdriveSettings,
} from "@/lib/api";

function renumber(renames: GdriveRename[]): GdriveRename[] {
  return renames.map((entry, index) => ({
    ...entry,
    new_name: `${String(index + 1).padStart(2, "0")}${entry.new_name.slice(
      entry.new_name.indexOf(" - "),
    )}`,
  }));
}

export default function DrivePage() {
  const [settings, setSettings] = useState<GdriveSettings | null>(null);
  const [folder, setFolder] = useState("");
  const [preview, setPreview] = useState<GdrivePreview | null>(null);
  const [dropped, setDropped] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<"preview" | "apply" | "undo" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => {
    api.gdriveSettings().then(setSettings).catch(() => setSettings(null));
  }, []);

  const plan = useMemo(
    () =>
      preview
        ? renumber(preview.renames.filter((entry) => !dropped.has(entry.file_id)))
        : [],
    [preview, dropped],
  );

  async function runPreview() {
    setBusy("preview");
    setError(null);
    setDone(null);
    setPreview(null);
    setDropped(new Set());
    try {
      setPreview(await api.gdrivePreview(folder.trim()));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Preview failed");
    } finally {
      setBusy(null);
    }
  }

  async function runApply() {
    if (!preview || plan.length === 0) return;
    setBusy("apply");
    setError(null);
    try {
      const result = await api.gdriveApply(preview.folder_id, plan);
      setDone(
        result.failed.length
          ? `Renamed ${result.renamed}; ${result.failed.length} failed — Drive said no. Preview again to see the current state.`
          : `Renamed ${result.renamed} files. A manifest in the folder records the change — Undo reverses it.`,
      );
      setPreview(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Rename failed");
    } finally {
      setBusy(null);
    }
  }

  async function runUndo() {
    setBusy("undo");
    setError(null);
    setDone(null);
    setPreview(null);
    try {
      const result = await api.gdriveUndo(folder.trim());
      setDone(
        result.failed.length
          ? `Restored ${result.restored}; ${result.failed.length} failed.`
          : `Restored ${result.restored} original names.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Undo failed");
    } finally {
      setBusy(null);
    }
  }

  function toggle(fileId: string) {
    setDropped((current) => {
      const next = new Set(current);
      if (next.has(fileId)) next.delete(fileId);
      else next.add(fileId);
      return next;
    });
  }

  return (
    <div>
      <div className="sectionhead">
        <h1>Drive organizer</h1>
        {settings?.configured && (
          <span className="faint mono">shared with {settings.client_email}</span>
        )}
      </div>

      {settings && !settings.configured && (
        <div className="card">
          <p style={{ marginTop: 0 }}>
            Not connected yet. Add a Google service account on the{" "}
            <Link href="/security">Security page</Link>, then share each
            property folder in Drive with the account&apos;s email address. Only
            folders you share are visible to it.
          </p>
        </div>
      )}

      <div className="card">
        <p className="faint" style={{ marginTop: 0 }}>
          Paste a Drive folder link. FrameFound classifies every photo — from
          the catalogue when the shoot is on the NAS, otherwise from a small
          Drive thumbnail — and proposes gallery-order names like{" "}
          <span className="mono">01 - Front exterior - IMG_1724.jpg</span>.
          Nothing is renamed until you approve the plan, and a manifest in the
          folder makes it one click to undo.
        </p>
        <div className="toolbar">
          <input
            className="input"
            style={{ flex: 1, minWidth: 280 }}
            placeholder="https://drive.google.com/drive/folders/…"
            value={folder}
            disabled={busy !== null}
            onChange={(e) => setFolder(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && folder.trim() && !busy) void runPreview();
            }}
            aria-label="Drive folder link"
          />
          <button
            className="btn btn-primary"
            disabled={busy !== null || !folder.trim()}
            onClick={() => void runPreview()}
          >
            {busy === "preview" ? "Analyzing…" : "Preview"}
          </button>
          <button
            className="btn"
            disabled={busy !== null || !folder.trim()}
            onClick={() => void runUndo()}
            title="Restore original names from the manifest FrameFound left in the folder"
          >
            {busy === "undo" ? "Undoing…" : "Undo last organize"}
          </button>
        </div>
        {busy === "preview" && (
          <p className="faint mono">
            Listing the folder and classifying… big folders can take a minute.
          </p>
        )}
        {error && (
          <p className="mono" style={{ color: "var(--bad, #e5484d)" }}>
            {error}
          </p>
        )}
        {done && <p className="mono">{done}</p>}
      </div>

      {preview && (
        <>
          <div className="sectionhead">
            <h2>{preview.folder_name}</h2>
            <span className="faint mono">
              {plan.length} of {preview.total} will be renamed ·{" "}
              {preview.from_catalogue} from catalogue · {preview.from_thumbnail}{" "}
              from thumbnails
            </span>
          </div>
          <div className="card">
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 32 }} />
                  <th>Current name</th>
                  <th>New name</th>
                  <th>Room</th>
                </tr>
              </thead>
              <tbody>
                {preview.renames.map((entry) => {
                  const skippedRow = dropped.has(entry.file_id);
                  const planned = plan.find((p) => p.file_id === entry.file_id);
                  return (
                    <tr
                      key={entry.file_id}
                      style={skippedRow ? { opacity: 0.45 } : undefined}
                    >
                      <td>
                        <button
                          className="btn"
                          style={{ padding: "2px 8px" }}
                          title={skippedRow ? "Include in the plan" : "Leave this file as is"}
                          onClick={() => toggle(entry.file_id)}
                        >
                          {skippedRow ? "+" : "✕"}
                        </button>
                      </td>
                      <td className="mono">{entry.old_name}</td>
                      <td className="mono">
                        {skippedRow ? "— left as is —" : planned?.new_name}
                      </td>
                      <td>
                        <span
                          className="pill"
                          data-tone={entry.source === "catalogue" ? "ok" : undefined}
                          title={
                            entry.source === "catalogue"
                              ? "Classified from the NAS catalogue — no pixels left the machine"
                              : "Classified from the Drive thumbnail"
                          }
                        >
                          {entry.room_label}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {preview.skipped.length > 0 && (
              <p className="faint" style={{ marginBottom: 0 }}>
                Left untouched:{" "}
                {preview.skipped
                  .map((s) => `${s.name} (${s.reason})`)
                  .join(", ")}
              </p>
            )}
            <div className="toolbar" style={{ marginTop: 12 }}>
              <button
                className="btn btn-primary"
                disabled={busy !== null || plan.length === 0}
                onClick={() => void runApply()}
              >
                {busy === "apply"
                  ? "Renaming…"
                  : `Rename ${plan.length} file${plan.length === 1 ? "" : "s"} in Drive`}
              </button>
              <span className="faint">
                Renames happen in place — no copies, originals recoverable via
                Undo.
              </span>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
