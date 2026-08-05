"use client";

// AI photo editing settings — the Anthropic key that powers the recipe-picker.
//
// Same contract as the maps keys: sealed at rest, presence-only in the API,
// and nothing leaves the machine until the operator presses the button on a
// listing that says send. This card is where that trade is made explicit.

import { useEffect, useState } from "react";

import { api, type AiEditSettings } from "@/lib/api";

export default function AiEditCard() {
  const [settings, setSettings] = useState<AiEditSettings | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    api.aiEditSettings().then(setSettings).catch(() => setSettings(null));
  }, []);

  async function save(patch: { api_key?: string; enabled?: boolean }) {
    setBusy(true);
    setMessage(null);
    try {
      const updated = await api.updateAiEditSettings(patch);
      setSettings(updated);
      setDraft("");
      setMessage(
        patch.api_key === ""
          ? "Key removed."
          : patch.api_key
            ? "Key saved — it is sealed and will not be shown again."
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
        <h2>AI photo editing</h2>
        <span className="faint mono">
          {settings.configured ? `key configured · ${settings.model}` : "not configured"}
        </span>
      </div>
      <div className="card">
        <p className="faint" style={{ marginTop: 0 }}>
          Powers “AI auto-edit” on listings: a small preview of each photograph
          goes to the Claude API, slider values come back, and the
          full-resolution render happens on this machine. Photos leave only
          when you press that button. The key is sealed at rest and never shown
          again after saving.
        </p>
        <div className="toolbar" style={{ marginTop: 8 }}>
          <input
            className="input"
            type="password"
            style={{ flex: 1, minWidth: 240 }}
            placeholder={
              settings.configured ? "Replace the stored key…" : "sk-ant-… Anthropic API key"
            }
            value={draft}
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            aria-label="Anthropic API key"
          />
          <button
            className="btn btn-primary"
            disabled={busy || !draft.trim()}
            onClick={() => save({ api_key: draft.trim() })}
          >
            Save key
          </button>
          {settings.configured && (
            <button className="btn" disabled={busy} onClick={() => save({ api_key: "" })}>
              Remove key
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
        {message && (
          <p className="faint mono" style={{ marginBottom: 0 }}>
            {message}
          </p>
        )}
      </div>
    </>
  );
}
