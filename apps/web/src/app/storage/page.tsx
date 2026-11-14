"use client";

// Storage: what is mounted, what each drive is for, how to add another — and
// where the bulky files FrameFound makes are kept (components/BulkStorage.tsx).
//
// Adding a drive is the one action in the app that reaches a privileged
// helper, so the page is explicit about what it is doing and never hides the
// manual path — if the helper is not running, the fstab line is still offered.
//
// That half only means something where the server can see and add mounts:
// the containers. On the Mac install drives are connected in Finder, and the
// page used to offer a Docker command and an /etc/fstab line there all the
// same. The server now says which it is (`drive_mounting`), and where it
// cannot mount the page shows its sentence in place of the table and the form.

import { useCallback, useEffect, useState } from "react";

import BulkStorage from "@/components/BulkStorage";
import Shell from "@/components/Shell";
import { api, type Mount, type StorageReport } from "@/lib/api";
import { copyText } from "@/lib/clipboard";
import { usePageTitle } from "@/lib/hooks";
import { diskSize, gbToBytes } from "@/lib/storage";

const ROLE_TONE: Record<string, string> = { media: "ok", cache: "warn" };

// One way of writing a size on this page: a drive must not read "14.6 TB"
// in this table and "14,901.2 GB" in the list of kinds under it.
const gb = (value: number | null): string => diskSize(gbToBytes(value));

export default function StoragePage() {
  const [report, setReport] = useState<StorageReport | null>(null);
  // The drives could not be read at all. The form is still offered then, as
  // it always was: whether this server can mount is not known either way.
  const [unread, setUnread] = useState(false);
  const [busy, setBusy] = useState(false);
  // About the drives table (a failed read, an unmount): shown above it.
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // About "Mount drive": shown beside that button, at the foot of the page.
  // It used to share the cards at the top, which was near enough while only
  // the table lay between; with the bulky-files section in between as well,
  // the reason a mount failed, and the /etc/fstab line to copy after one that
  // worked, appeared more than a screen above the button and out of sight.
  const [addNote, setAddNote] = useState<string | null>(null);
  const [addError, setAddError] = useState<string | null>(null);
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
      setUnread(false);
    } catch {
      setUnread(true);
      setError("Could not read storage.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  usePageTitle("Storage");

  const set = (patch: Partial<typeof form>) => setForm((f) => ({ ...f, ...patch }));

  async function addDrive() {
    setBusy(true);
    setError(null);
    setNote(null);
    setAddError(null);
    setAddNote(null);
    setFstab("");
    try {
      const result = await api.addDrive(form);
      if (!result.ok) {
        setAddError(result.detail || "The drive could not be mounted.");
        return;
      }
      setAddNote(
        result.library_id
          ? `Mounted at ${result.target} and a library was created. A scan has started.`
          : `Mounted at ${result.target}.`,
      );
      setFstab(result.fstab_line);
      setForm((f) => ({ ...f, server: "", share: "", name: "", username: "", password: "" }));
      await load();
    } catch (err) {
      setAddError(err instanceof Error ? err.message : "The drive could not be added.");
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
  // Nothing about drives is drawn until the server has said which kind of
  // install this is: on a Mac the form would otherwise flash up and vanish.
  const mounting = report ? report.drive_mounting !== false : unread;

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Storage</h2>
        {report && mounting && (
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
        </div>
      )}

      {report && !mounting && (
        <p className="muted" style={{ margin: "0 0 4px", maxWidth: "78ch" }}>
          {report.hint}
        </p>
      )}

      {mounting && (
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
              {report && !report.mounts.length && (
                <tr>
                  <td colSpan={7} className="faint">
                    Nothing mounted yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      <BulkStorage />

      {mounting && (
        <>
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

            {addError && (
              <p role="alert" style={{ color: "var(--ember)", margin: "16px 0 0" }}>
                {addError}
              </p>
            )}
            {addNote && (
              <div
                role="status"
                style={{
                  marginTop: 16,
                  padding: "12px 14px",
                  border: "1px solid var(--amber-line)",
                  borderRadius: "var(--radius)",
                }}
              >
                {addNote}
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
                        onClick={() => void copyText(fstab)}
                      >
                        Copy
                      </button>
                    </div>
                  </>
                )}
              </div>
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
        </>
      )}
    </Shell>
  );
}
