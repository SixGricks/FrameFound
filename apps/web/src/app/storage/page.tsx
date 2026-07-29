"use client";

// Storage: what is mounted, what each drive is for, and how to add another.
//
// Adding a drive is the one action in the app that reaches a privileged
// helper, so the page is explicit about what it is doing and never hides the
// manual path — if the helper is not running, the fstab line is still offered.

import { useCallback, useEffect, useState } from "react";

import Shell from "@/components/Shell";
import { api, type Mount, type StorageReport } from "@/lib/api";

const ROLE_TONE: Record<string, string> = { media: "ok", cache: "warn" };

function gb(value: number | null): string {
  if (value === null) return "—";
  return value >= 1024 ? `${(value / 1024).toFixed(1)} TB` : `${value.toFixed(0)} GB`;
}

export default function StoragePage() {
  const [report, setReport] = useState<StorageReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fstab, setFstab] = useState<string>("");

  const [form, setForm] = useState({
    protocol: "cifs",
    server: "",
    share: "",
    name: "",
    purpose: "media",
    username: "",
    password: "",
    library_name: "",
    create_library: true,
  });

  const load = useCallback(async () => {
    try {
      setReport(await api.storage());
    } catch {
      setError("Could not read storage.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));

  async function addDrive() {
    setBusy(true);
    setError(null);
    setNote(null);
    setFstab("");
    try {
      const result = await api.addDrive(form);
      if (!result.ok) {
        setError(result.detail || "The drive could not be mounted.");
        return;
      }
      setNote(
        result.library_id
          ? `Mounted at ${result.target} and a library was created. A scan has started.`
          : `Mounted at ${result.target}.`,
      );
      setFstab(result.fstab_line);
      setForm((f) => ({ ...f, server: "", share: "", name: "", username: "", password: "" }));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The drive could not be added.");
    } finally {
      setBusy(false);
    }
  }

  async function unmount(mount: Mount) {
    if (!confirm(`Unmount ${mount.path}? Media on it becomes unavailable until remounted.`)) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await api.unmountDrive(mount.path);
      if (!result.ok) setError(result.detail || "Could not unmount.");
      else setNote(`Unmounted ${mount.path}.`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not unmount.");
    } finally {
      setBusy(false);
    }
  }

  const canSubmit = form.server.trim() && form.share.trim() && form.name.trim() && !busy;

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Storage</h2>
        {report && (
          <span className="faint mono">
            {report.mounts.length} mounted
          </span>
        )}
      </div>

      {error && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
          {error}
        </div>
      )}
      {note && (
        <div className="card" role="status" style={{ borderColor: "var(--amber-line)" }}>
          {note}
          {fstab && (
            <>
              <p className="faint" style={{ marginBottom: 6 }}>
                This mount is live now but will not survive a host reboot. Add
                this line to <code>/etc/fstab</code> on the host to keep it:
              </p>
              <div className="pathbox">
                <span>{fstab}</span>
                <button
                  className="btn"
                  style={{ padding: "3px 10px" }}
                  onClick={() => navigator.clipboard?.writeText(fstab)}
                >
                  Copy
                </button>
              </div>
            </>
          )}
        </div>
      )}

      <div className="tablewrap card" style={{ padding: 0, marginTop: 14 }}>
        <table className="table">
          <caption className="faint" style={{ captionSide: "top", padding: "12px 12px 0", textAlign: "left" }}>
            Drives visible to FrameFound
          </caption>
          <thead>
            <tr>
              <th scope="col">Path</th>
              <th scope="col">Type</th>
              <th scope="col">Role</th>
              <th scope="col">Size</th>
              <th scope="col">Free</th>
              <th scope="col">Access</th>
              <th scope="col" />
            </tr>
          </thead>
          <tbody>
            {report?.mounts.map((mount) => (
              <tr key={mount.path}>
                <td className="mono" style={{ fontSize: "0.78rem" }}>
                  {mount.path}
                  {mount.library_name && (
                    <div className="faint">
                      {mount.library_name}
                      {mount.asset_count != null && ` · ${mount.asset_count} assets`}
                    </div>
                  )}
                </td>
                <td className="faint">{mount.fstype}</td>
                <td>
                  <span className="pill" data-tone={ROLE_TONE[mount.role]}>
                    {mount.role}
                  </span>
                </td>
                <td className="mono">{gb(mount.total_gb)}</td>
                <td className="mono">{gb(mount.free_gb)}</td>
                <td className="faint">{mount.writable ? "read/write" : "read-only"}</td>
                <td>
                  {mount.is_network && (
                    <button
                      className="btn"
                      style={{ padding: "4px 10px" }}
                      disabled={busy}
                      onClick={() => unmount(mount)}
                    >
                      Unmount
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {!report?.mounts.length && (
              <tr>
                <td colSpan={7} className="faint">
                  Nothing mounted yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="sectionhead">
        <h2>Add a network drive</h2>
      </div>

      <div className="card">
        <p className="faint" style={{ marginTop: 0, fontSize: "0.86rem" }}>
          A <strong>media drive</strong> holds originals and is always mounted
          read-only — FrameFound never writes to your footage. A{" "}
          <strong>cache drive</strong> is writable and holds thumbnails and
          proxies, which is how you keep generated files off the system disk.
        </p>

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
            gap: "0 16px",
          }}
        >
          <label className="field">
            <span>Use this drive for</span>
            <select
              className="select"
              value={form.purpose}
              onChange={(e) => set({ purpose: e.target.value })}
            >
              <option value="media">Media library (read-only)</option>
              <option value="cache">Thumbnails &amp; proxies (writable)</option>
            </select>
          </label>

          <label className="field">
            <span>Protocol</span>
            <select
              className="select"
              value={form.protocol}
              onChange={(e) => set({ protocol: e.target.value })}
            >
              <option value="cifs">SMB / Windows share</option>
              <option value="nfs">NFS</option>
            </select>
          </label>

          <label className="field">
            <span>Server</span>
            <input
              className="input"
              value={form.server}
              placeholder="192.168.1.157"
              onChange={(e) => set({ server: e.target.value })}
            />
          </label>

          <label className="field">
            <span>Share name</span>
            <input
              className="input"
              value={form.share}
              placeholder="GELCO"
              onChange={(e) => set({ share: e.target.value })}
            />
          </label>

          <label className="field">
            <span>Folder name here</span>
            <input
              className="input"
              value={form.name}
              placeholder="gelco"
              onChange={(e) => set({ name: e.target.value })}
            />
            <small className="faint">
              Mounts at /mnt/{form.purpose === "media" ? "media" : "cache"}/
              {form.name || "…"}
            </small>
          </label>

          {form.protocol === "cifs" && (
            <>
              <label className="field">
                <span>Username</span>
                <input
                  className="input"
                  autoComplete="off"
                  value={form.username}
                  onChange={(e) => set({ username: e.target.value })}
                />
              </label>
              <label className="field">
                <span>Password</span>
                <input
                  className="input"
                  type="password"
                  autoComplete="new-password"
                  value={form.password}
                  onChange={(e) => set({ password: e.target.value })}
                />
                <small className="faint">
                  Written to a temporary 0600 file for the mount, then deleted.
                  Never passed on a command line.
                </small>
              </label>
            </>
          )}
        </div>

        {form.purpose === "media" && (
          <>
            <label className="field" data-layout="row">
              <input
                type="checkbox"
                checked={form.create_library}
                onChange={(e) => set({ create_library: e.target.checked })}
              />
              <span>Create a library and scan it straight away</span>
            </label>
            {form.create_library && (
              <label className="field">
                <span>Library name</span>
                <input
                  className="input"
                  value={form.library_name}
                  placeholder={form.name || "Same as the folder name"}
                  onChange={(e) => set({ library_name: e.target.value })}
                />
              </label>
            )}
          </>
        )}

        <div className="toolbar" style={{ marginTop: 18, marginBottom: 0 }}>
          <button className="btn btn-primary" disabled={!canSubmit} onClick={addDrive}>
            {busy ? "Working…" : "Mount drive"}
          </button>
          <span className="faint" style={{ fontSize: "0.82rem" }}>
            Needs the storage profile running:{" "}
            <code className="mono">docker compose --profile storage up -d</code>
          </span>
        </div>
      </div>
    </Shell>
  );
}
