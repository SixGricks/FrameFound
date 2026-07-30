"use client";

// Where each workstation mounts one library.
//
// The catalogue stores `/media/gelco/...`. A Windows edit bay sees `Z:\`, a Mac
// sees `/Volumes/GELCO`. Without a profile the editing panels can still stream
// a proxy but cannot hand the editor the real file, so this is the setting that
// decides whether Premiere and Lightroom are genuinely useful.
//
// Every row shows a worked example, because a prefix looks right far more often
// than it is right — and the symptom of a wrong one is a project full of
// offline media, which reads as a broken server rather than a typo here.

import { useCallback, useEffect, useState } from "react";

import { api, type PathMapping, type PathMappingInput } from "@/lib/api";

const PLATFORMS: PathMappingInput["platform"][] = ["windows", "macos", "linux"];

const PLACEHOLDER: Record<string, string> = {
  windows: "Z:\\ or \\\\nas\\media",
  macos: "/Volumes/GELCO",
  linux: "/mnt/gelco",
};

export default function PathMappingsEditor({
  libraryId,
  libraryName,
}: {
  libraryId: string;
  libraryName: string;
}) {
  const [rows, setRows] = useState<PathMappingInput[]>([]);
  const [saved, setSaved] = useState<PathMapping[]>([]);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    try {
      const found = await api.pathMappings(libraryId);
      setSaved(found);
      setRows(
        found.map((m) => ({
          profile_name: m.profile_name,
          platform: m.platform,
          mapped_prefix: m.mapped_prefix,
        })),
      );
    } catch {
      setSaved([]);
    }
  }, [libraryId]);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  async function save() {
    setBusy(true);
    setNote("");
    try {
      const cleaned = rows.filter((r) => r.profile_name.trim() && r.mapped_prefix.trim());
      const result = await api.replacePathMappings(libraryId, cleaned);
      setSaved(result);
      setNote(`Saved ${result.length} profile${result.length === 1 ? "" : "s"}.`);
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Could not save those");
    } finally {
      setBusy(false);
    }
  }

  function update(index: number, patch: Partial<PathMappingInput>) {
    setRows((current) => current.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  }

  return (
    <details open={open} onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="faint">
        Workstation paths{saved.length ? ` (${saved.length})` : ""}
      </summary>

      <p className="faint" style={{ maxWidth: "60ch" }}>
        How other machines reach <strong>{libraryName}</strong>. The Premiere
        panel and Lightroom plugin use these to hand the editor a real file
        instead of a server path.
      </p>

      {rows.map((row, index) => (
        <div className="card" key={index} style={{ padding: 10 }}>
          <div className="field">
            <label htmlFor={`pm-name-${libraryId}-${index}`}>Profile name</label>
            <input
              id={`pm-name-${libraryId}-${index}`}
              className="input"
              value={row.profile_name}
              placeholder="Edit bay iMac"
              onChange={(e) => update(index, { profile_name: e.target.value })}
            />
          </div>
          <div className="field">
            <label htmlFor={`pm-plat-${libraryId}-${index}`}>Platform</label>
            <select
              id={`pm-plat-${libraryId}-${index}`}
              className="select"
              value={row.platform}
              onChange={(e) =>
                update(index, { platform: e.target.value as PathMappingInput["platform"] })
              }
            >
              {PLATFORMS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor={`pm-prefix-${libraryId}-${index}`}>Mounted at</label>
            <input
              id={`pm-prefix-${libraryId}-${index}`}
              className="input mono"
              value={row.mapped_prefix}
              placeholder={PLACEHOLDER[row.platform]}
              onChange={(e) => update(index, { mapped_prefix: e.target.value })}
            />
          </div>
          {saved[index]?.example && (
            <p className="faint mono" style={{ fontSize: "0.74rem", overflowWrap: "anywhere" }}>
              e.g. {saved[index].example}
            </p>
          )}
          <button
            className="btn"
            onClick={() => setRows((current) => current.filter((_, i) => i !== index))}
          >
            Remove
          </button>
        </div>
      ))}

      <div className="toolbar">
        <button
          className="btn"
          onClick={() =>
            setRows((current) => [
              ...current,
              { profile_name: "", platform: "windows", mapped_prefix: "" },
            ])
          }
        >
          Add a workstation
        </button>
        <button className="btn btn-primary" disabled={busy} onClick={save}>
          {busy ? "Saving…" : "Save"}
        </button>
        {note && <span className="faint">{note}</span>}
      </div>
    </details>
  );
}
