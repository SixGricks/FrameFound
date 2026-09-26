"use client";

// Google Drive organizer credentials — a service account, not an OAuth grant.
//
// The operator creates the account in Google Cloud, pastes its JSON key here
// (sealed at rest like every other key), and then shares Drive folders with
// the account's email address exactly like sharing with a person. FrameFound
// can only ever see what was explicitly shared, and un-sharing revokes it.

import { useEffect, useState } from "react";

import { api, type GdriveSettings } from "@/lib/api";

export default function GdriveCard() {
  const [settings, setSettings] = useState<GdriveSettings | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [folders, setFolders] = useState<string | null>(null);

  useEffect(() => {
    api.gdriveSettings().then(setSettings).catch(() => setSettings(null));
  }, []);

  async function save(patch: {
    service_account_json?: string;
    enabled?: boolean;
    training_folders?: string[];
  }) {
    setBusy(true);
    setMessage(null);
    try {
      const updated = await api.updateGdriveSettings(patch);
      setSettings(updated);
      setDraft("");
      setMessage(
        patch.service_account_json === ""
          ? "Service account removed."
          : patch.service_account_json
            ? "Saved — the key is sealed and will not be shown again."
            : "Saved.",
      );
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Could not save");
    } finally {
      setBusy(false);
    }
  }

  if (!settings) return null;

  return (
    <>
      <div className="sectionhead">
        <h2>Google Drive organizer</h2>
        <span className="faint mono">
          {settings.configured ? "service account configured" : "not configured"}
        </span>
      </div>
      <div className="card">
        <p className="faint" style={{ marginTop: 0 }}>
          Lets the Drive page sort listing folders: classify, rename in place,
          undo from a manifest. Create a service account in Google Cloud
          (IAM &amp; Admin → Service Accounts → Keys → JSON), paste the
          downloaded key file below, then share each property folder with the
          account&apos;s email. Only shared folders are visible to it.
        </p>
        {settings.configured && (
          <p className="mono" style={{ userSelect: "all" }}>
            Share folders with: <strong>{settings.client_email}</strong>
          </p>
        )}
        <textarea
          className="input"
          style={{ width: "100%", minHeight: 96, fontFamily: "var(--mono, monospace)" }}
          placeholder={
            settings.configured
              ? "Paste a new key file to replace the stored one…"
              : '{ "type": "service_account", … } — paste the JSON key file'
          }
          value={draft}
          disabled={busy}
          onChange={(e) => setDraft(e.target.value)}
          aria-label="Service account JSON key"
        />
        <div className="toolbar" style={{ marginTop: 8 }}>
          <button
            className="btn btn-primary"
            disabled={busy || !draft.trim()}
            onClick={() => save({ service_account_json: draft.trim() })}
          >
            Save service account
          </button>
          {settings.configured && (
            <button
              className="btn"
              disabled={busy}
              onClick={() => save({ service_account_json: "" })}
            >
              Remove
            </button>
          )}
          {settings.configured && (
            <button
              className="btn"
              disabled={busy}
              onClick={() => save({ enabled: !settings.enabled })}
            >
              {settings.enabled ? "Disable" : "Enable"}
            </button>
          )}
        </div>
        {settings.configured && (
          <>
            <p className="faint" style={{ marginBottom: 4 }}>
              <strong>Training pairs.</strong> Drive folders whose shoot subfolders hold
              finished photos (MLS, Edited, Fotello Edited) — e.g. the Intel Auctions / 2026
              folder. Each night FrameFound pairs those finals with their originals on the NAS,
              read-only, to learn your editing look. One link or id per line.
            </p>
            <textarea
              className="input"
              style={{ width: "100%", minHeight: 60, fontFamily: "var(--mono, monospace)" }}
              value={folders ?? settings.training_folder_ids.join("\n")}
              disabled={busy}
              onChange={(e) => setFolders(e.target.value)}
              aria-label="Drive folders to collect training pairs from"
            />
            <button
              className="btn"
              style={{ marginTop: 6 }}
              disabled={busy || folders === null}
              onClick={async () => {
                await save({
                  training_folders: (folders ?? "").split("\n").map((f) => f.trim()).filter(Boolean),
                });
                setFolders(null);
              }}
            >
              Save training folders
            </button>
          </>
        )}
        {message && (
          <p className="faint mono" style={{ marginBottom: 0 }}>
            {message}
          </p>
        )}
      </div>
    </>
  );
}
