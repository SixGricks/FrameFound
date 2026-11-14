"use client";

// Sharing this Mac: how much memory a model may use by day and overnight,
// when overnight is, and how long a model stays loaded with nothing asking.
// The Mac is also the Adobe machine, so by day a model gets only what
// Premiere and Photoshop can spare; a bigger one waits for the night.
//
// What is typed is kept as text until Save, field by field, so the page's
// regular re-read does not overwrite a half-typed figure, and a field left
// alone keeps showing the server's value. Save sends only what changed. When
// the server says no, its sentence appears beside Save, where the owner is
// looking, rather than at the top of the section.

import { useEffect, useRef, useState } from "react";

import { ApiError, api, type LocalModelsOverview, type LocalModelsSettingsSaved } from "@/lib/api";
import {
  focusIfLost,
  gbFigure,
  sharingPatch,
  sharingSavedSentence,
  sharingValue,
  type ModelsAct,
  type SharingDrafts,
  type SharingField,
} from "@/lib/localModels";

type Note = { tone: "alert" | "status"; text: string };

export default function ModelsSharing({
  overview,
  act,
  busy,
}: {
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
}) {
  const { settings, machine } = overview;
  const [drafts, setDrafts] = useState<SharingDrafts>({});
  const [note, setNote] = useState<Note | null>(null);
  // "Put back" removes itself (it is there only while something changed),
  // so the keyboard goes to the first field, where the values came back.
  const [putBack, setPutBack] = useState(0);
  const form = useRef<HTMLFormElement>(null);
  const plan = sharingPatch(drafts, settings);
  const dirty = Object.keys(drafts).length > 0;

  useEffect(() => {
    if (putBack) focusIfLost(form.current?.querySelector<HTMLInputElement>("input"));
  }, [putBack]);

  const value = (field: SharingField) => drafts[field] ?? sharingValue(settings, field);
  function edit(field: SharingField, text: string) {
    setDrafts((before) => ({ ...before, [field]: text }));
    setNote(null);
  }

  function save(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (plan.problem) {
      setNote({ tone: "alert", text: plan.problem });
      return;
    }
    if (!plan.changed.length) {
      setDrafts({});
      setNote({ tone: "status", text: "Nothing changed." });
      return;
    }
    const { patch, changed } = plan;
    setNote(null);
    act(
      "sharing",
      // The server's refusal goes beside Save; only a failure to reach it at
      // all is left to the page's own line at the top of the section.
      async (): Promise<LocalModelsSettingsSaved | null> => {
        try {
          return await api.updateLocalModelSettings(patch);
        } catch (err) {
          if (!(err instanceof ApiError)) throw err;
          setNote({ tone: "alert", text: err.message });
          return null;
        }
      },
      (saved) => {
        if (saved) {
          setDrafts({});
          setNote({ tone: "status", text: sharingSavedSentence(changed) });
        }
        return null;
      },
    );
  }

  const intro = (
    <p style={{ margin: 0 }}>
      By day a model may use up to this much memory, so Premiere and Photoshop keep the rest. A
      model that needs more waits for the overnight window.
      {machine.memory_gb != null && ` This Mac has ${gbFigure(Math.round(machine.memory_gb))}.`}
    </p>
  );
  const footnote = (
    <p className="faint" style={{ margin: "12px 0 0", fontSize: "0.86rem" }}>
      The limit decides what may start; it does not stop a model that grows.
    </p>
  );

  if (!overview.can_manage) {
    const minutes = settings.idle_unload_minutes;
    return (
      <div className="card">
        {intro}
        <dl className="kv" style={{ marginTop: 12 }}>
          <dt>By day</dt>
          <dd>{gbFigure(settings.day_budget_gb)}</dd>
          <dt>Overnight</dt>
          <dd>
            {gbFigure(settings.night_budget_gb)}, {settings.night_start}–{settings.night_end}
          </dd>
          <dt>Unload after</dt>
          <dd>
            {minutes} minute{minutes === 1 ? "" : "s"} with nothing asked
          </dd>
        </dl>
        {footnote}
      </div>
    );
  }

  return (
    <form ref={form} className="card" onSubmit={save} noValidate>
      {intro}
      <div className="models-sharing-fields">
        <label className="field">
          Daytime memory (GB)
          <input
            className="input"
            type="number"
            inputMode="decimal"
            min={1}
            max={512}
            step="any"
            value={value("day_budget_gb")}
            onChange={(event) => edit("day_budget_gb", event.target.value)}
          />
        </label>
        <label className="field">
          Overnight memory (GB)
          <input
            className="input"
            type="number"
            inputMode="decimal"
            min={1}
            max={512}
            step="any"
            value={value("night_budget_gb")}
            onChange={(event) => edit("night_budget_gb", event.target.value)}
          />
        </label>
        <label className="field">
          Overnight from
          <input
            className="input"
            type="time"
            step={60}
            value={value("night_start")}
            onChange={(event) => edit("night_start", event.target.value)}
          />
        </label>
        <label className="field">
          to
          <input
            className="input"
            type="time"
            step={60}
            value={value("night_end")}
            onChange={(event) => edit("night_end", event.target.value)}
          />
        </label>
        <label className="field">
          Unload a model after (minutes idle)
          <input
            className="input"
            type="number"
            inputMode="numeric"
            min={1}
            max={120}
            step={1}
            value={value("idle_unload_minutes")}
            onChange={(event) => edit("idle_unload_minutes", event.target.value)}
          />
        </label>
      </div>
      <p className="faint" style={{ margin: "8px 0 0", fontSize: "0.84rem" }}>
        Times are on this Mac&apos;s clock. A window that ends earlier than it starts runs past
        midnight.
      </p>
      <div className="models-save">
        <button type="submit" className="btn btn-primary" disabled={busy || !dirty}>
          Save
        </button>
        {dirty && (
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => {
              setDrafts({});
              setNote(null);
              setPutBack((count) => count + 1);
            }}
          >
            Put back
          </button>
        )}
        {note && (
          <span
            role={note.tone}
            className="models-save-note"
            data-tone={note.tone === "alert" ? "bad" : "ok"}
          >
            {note.text}
          </span>
        )}
      </div>
      {footnote}
    </form>
  );
}
