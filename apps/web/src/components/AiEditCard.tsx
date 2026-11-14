"use client";

// AI settings — the Anthropic key that powers the recipe-picker, the tag
// reviewer and the chat panel, and the monthly ceiling on what all of them
// together may spend.
//
// Same contract as the maps keys: sealed at rest, presence-only in the API,
// and nothing leaves the machine until the operator presses a button that
// says send (or turns the nightly review on, which is the same decision made
// once). This card is where that trade is made explicit, and where the bill
// is capped: the budget is enforced from a ledger of what every call actually
// cost, before the call that would cross it.

import { useEffect, useState } from "react";

import { api, type AiEditSettings } from "@/lib/api";
import { modelName } from "@/lib/chatWords";

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

// The reviewer names what is in a thumbnail; it is not a tone judgement, and
// the cheapest current model does it. Estimates from the price sheet at
// ~1,800 input and 400 output tokens a picture (ai/spend.py).
const REVIEW_MODELS = [
  { id: "claude-haiku-5-5", label: "Haiku 5.5 — ≈ $0.0004/picture (recommended)" },
  { id: "claude-sonnet-5-5", label: "Sonnet 5.5 — ≈ $0.008/picture" },
  { id: "claude-opus-5-5", label: "Opus 5.5 — ≈ $0.015/picture" },
];

// A word on the chat models people will weigh; the list itself is the
// server's. Chat sends the whole conversation with every request, so the
// difference in price between models shows more here than per photo.
// Relative prices from the price sheet (ai/spend.py), Nov 2026.
const CHAT_NOTES: Record<string, string> = {
  "claude-opus-5-5": "the best at multi-step work (recommended)",
  "claude-sonnet-5-5": "about half the price of Opus 5.5",
  "claude-haiku-5-5": "quickest and cheapest, for simple look-ups",
  "claude-fable-5-1": "about twice the price of Opus 5.5",
};

function money(value: number): string {
  return `$${value.toFixed(2)}`;
}

export default function AiEditCard() {
  const [settings, setSettings] = useState<AiEditSettings | null>(null);
  const [draft, setDraft] = useState("");
  const [budgetDraft, setBudgetDraft] = useState<string | null>(null);
  const [batchDraft, setBatchDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    api.aiEditSettings().then(setSettings).catch(() => setSettings(null));
  }, []);

  async function save(patch: Parameters<typeof api.updateAiEditSettings>[0]) {
    setBusy(true);
    setMessage(null);
    try {
      const updated = await api.updateAiEditSettings(patch);
      setSettings(updated);
      setDraft("");
      setBudgetDraft(null);
      setBatchDraft(null);
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

  // The server offers only models it has a price for (a model it cannot
  // price would spend outside the budget); an older server offers none.
  const chatModels = settings.chat_models ?? [];
  const spent = settings.spent_this_month_usd;
  const budget = settings.monthly_budget_usd;
  const used = budget > 0 ? Math.min(1, spent / budget) : 1;
  const budgetTone = used >= 1 ? "bad" : used >= 0.8 ? "warn" : "ok";

  return (
    <>
      <div className="sectionhead">
        <h2>Claude: photo editing, tag review and chat</h2>
        <span className="faint mono">
          {settings.configured ? `key configured · ${settings.model}` : "not configured"}
        </span>
      </div>
      <div className="card">
        <p className="faint" style={{ marginTop: 0 }}>
          Powers “AI auto-edit” on listings and “Review with Claude” on Tags: a
          small preview of each photograph goes to the Claude API, slider values
          or tag suggestions come back, and everything else happens on this
          machine. Photos leave only when you press those buttons (or turn the
          nightly review on). The same key powers “Ask Claude”, the chat panel
          beside every page. The key is sealed at rest and never shown again
          after saving.
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
              Editing model
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
        <p className="faint" style={{ fontSize: "0.8rem" }}>
          {settings.look_examples > 0
            ? `Learned look installed — ${settings.look_examples} shipped photos. Auto-edit takes ` +
              `its tone from how your most similar past photos were edited (locally, free); ` +
              `the model above straightens and names each photo.`
            : "No learned look installed — auto-edit takes its tone from the model, or the " +
              "preset without a key."}
        </p>

        {/* ---- the cap ---- */}
        <div className="sectionhead" style={{ marginTop: 14 }}>
          <h3 style={{ margin: 0 }}>Monthly budget</h3>
          <span className="pill" data-tone={budgetTone}>
            {money(spent)} of {money(budget)} used
          </span>
        </div>
        <p className="faint" style={{ fontSize: "0.82rem", marginTop: 4 }}>
          The ceiling on everything FrameFound asks the Claude API this calendar
          month — auto-edit, naming, the tag reviewer and the chat panel
          together. A run stops <em>before</em> the call that would cross it and
          says so; in the chat panel, Claude stops answering until next month or
          until you raise it. Set it to 0 to pause all calls without removing
          the key.
        </p>
        <div className="toolbar">
          <span className="faint" style={{ fontSize: "0.8rem" }}>
            $
          </span>
          <input
            className="input mono"
            type="number"
            min={0}
            step={0.5}
            style={{ width: 120 }}
            value={budgetDraft ?? String(budget)}
            disabled={busy}
            onChange={(e) => setBudgetDraft(e.target.value)}
            aria-label="Monthly AI budget in dollars"
          />
          <button
            className="btn"
            disabled={busy || budgetDraft === null || budgetDraft === String(budget)}
            onClick={() => {
              const value = Number(budgetDraft);
              if (Number.isFinite(value) && value >= 0) save({ monthly_budget_usd: value });
            }}
          >
            Save budget
          </button>
        </div>

        {/* ---- the reviewer ---- */}
        {settings.configured && (
          <>
            <div className="sectionhead" style={{ marginTop: 14 }}>
              <h3 style={{ margin: 0 }}>Tag reviewer</h3>
              <span className="faint mono">{settings.review_model}</span>
            </div>
            <p className="faint" style={{ fontSize: "0.82rem", marginTop: 4 }}>
              Claude looks at the thumbnail of pictures nobody has tagged and
              proposes tags from your vocabulary, plus new ones it is sure of.
              They arrive as suggestions on the Tags page; your yes or no is what
              trains the match. Run it from Tags, or let it run nightly.
            </p>
            <div className="toolbar">
              <label className="faint" htmlFor="review-model" style={{ fontSize: "0.8rem" }}>
                Model
              </label>
              <select
                id="review-model"
                className="select"
                style={{ flex: 1, minWidth: 240 }}
                value={settings.review_model}
                disabled={busy}
                onChange={(e) => save({ review_model: e.target.value })}
              >
                {!REVIEW_MODELS.some((m) => m.id === settings.review_model) && (
                  <option value={settings.review_model}>{settings.review_model}</option>
                )}
                {REVIEW_MODELS.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="toolbar" style={{ marginTop: 8 }}>
              <label className="field" style={{ flexDirection: "row", gap: 8, margin: 0 }}>
                <input
                  type="checkbox"
                  checked={settings.review_auto}
                  disabled={busy}
                  onChange={(e) => save({ review_auto: e.target.checked })}
                />
                <span>Review new untagged media every night</span>
              </label>
              <label className="faint" htmlFor="review-batch" style={{ fontSize: "0.8rem" }}>
                pictures per run
              </label>
              <input
                id="review-batch"
                className="input mono"
                type="number"
                min={1}
                max={500}
                style={{ width: 90 }}
                value={batchDraft ?? String(settings.review_batch)}
                disabled={busy}
                onChange={(e) => setBatchDraft(e.target.value)}
                onBlur={() => {
                  const value = Number(batchDraft);
                  if (batchDraft !== null && Number.isInteger(value) && value >= 1) {
                    save({ review_batch: value });
                  }
                }}
              />
            </div>
          </>
        )}

        {/* ---- the chat panel ---- */}
        {settings.configured && chatModels.length > 0 && (
          <>
            <div className="sectionhead" style={{ marginTop: 14 }}>
              <h3 style={{ margin: 0 }}>Chat panel</h3>
              <span className="faint mono">{settings.chat_model}</span>
            </div>
            <p className="faint" style={{ fontSize: "0.82rem", marginTop: 4 }}>
              “Ask Claude”, beside every page: Claude can search the catalogue,
              look at pictures and propose changes, and asks before changing
              anything. What you type and which page you are on go to the Claude
              API; pictures go only while a conversation’s pictures switch is
              on. This model is used for new conversations — each conversation
              keeps the one it began with.
            </p>
            <div className="toolbar">
              <label className="faint" htmlFor="chat-model" style={{ fontSize: "0.8rem" }}>
                Chat model
              </label>
              <select
                id="chat-model"
                className="select"
                style={{ flex: 1, minWidth: 240 }}
                value={settings.chat_model}
                disabled={busy}
                onChange={(e) => save({ chat_model: e.target.value })}
              >
                {!chatModels.includes(settings.chat_model) && (
                  <option value={settings.chat_model}>{modelName(settings.chat_model)}</option>
                )}
                {chatModels.map((id) => (
                  <option key={id} value={id}>
                    {modelName(id)}
                    {CHAT_NOTES[id] ? ` — ${CHAT_NOTES[id]}` : ""}
                  </option>
                ))}
              </select>
            </div>
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
