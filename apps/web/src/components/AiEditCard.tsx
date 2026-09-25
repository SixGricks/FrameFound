"use client";

// AI photo editing settings — the Anthropic key that powers the recipe-picker.
//
// Same contract as the maps keys: sealed at rest, presence-only in the API,
// and nothing leaves the machine until the operator presses the button on a
// listing that says send. This card is where that trade is made explicit.

import { useEffect, useState } from "react";

import { api, type AiEditSettings } from "@/lib/api";

// Measured in the Sep 2026 bake-off against what shipped (docs/ai-editing-models.md):
// colour distance from the final across 7 shoots, and the API's own token counts.
const MODELS = [
  {
    id: "claude-opus-5-5",
    label: "Opus 5.5 — best tone judgment of the models · ≈ $0.011/photo",
  },
  {
    id: "claude-sonnet-5",
    label: "Sonnet 5 — enough for naming and straightening · ≈ $0.007/photo",
  },
  {
    id: "claude-fable-5-1",
    label: "Fable 5.1 — no better than Opus, often overloaded · ≈ $0.032/photo",
  },
];

export default function AiEditCard() {
  const [settings, setSettings] = useState<AiEditSettings | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    api.aiEditSettings().then(setSettings).catch(() => setSettings(null));
  }, []);

  async function save(patch: { api_key?: string; enabled?: boolean; model?: string }) {
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
        {settings.configured && (
          <div className="toolbar" style={{ marginTop: 8 }}>
            <label className="faint" htmlFor="ai-model" style={{ fontSize: "0.8rem" }}>
              Model
            </label>
            <select
              id="ai-model"
              className="select"
              style={{ flex: 1, minWidth: 240 }}
              value={settings.model}
              disabled={busy}
              onChange={(e) => save({ model: e.target.value })}
            >
              {!MODELS.some((m) => m.id === settings.model) && (
                <option value={settings.model}>{settings.model}</option>
              )}
              {MODELS.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
          </div>
        )}
        <p className="faint" style={{ fontSize: "0.8rem", marginBottom: 0 }}>
          {settings.look_examples > 0
            ? `Learned look installed — ${settings.look_examples} shipped photos. Auto-edit takes ` +
              `its tone from how your most similar past photos were edited (locally, free); ` +
              `the model above straightens and names each photo.`
            : "No learned look installed — auto-edit takes its tone from the model, or the " +
              "preset without a key."}
        </p>
        {message && (
          <p className="faint mono" style={{ marginBottom: 0 }}>
            {message}
          </p>
        )}
      </div>
    </>
  );
}
