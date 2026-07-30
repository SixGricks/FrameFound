"use client";

// People: face clusters, naming, and review.
//
// Nothing here asserts an identity the operator has not agreed to. An unnamed
// cluster says "Unnamed person" rather than guessing, and a suggestion on a
// named person is labelled as unreviewed until it is confirmed. That is the
// whole design — a face database that is confidently wrong is worse than one
// that asks.

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import FaceCrop from "@/components/FaceCrop";
import Shell from "@/components/Shell";
import { api, type FaceSettings, type PersonSummary } from "@/lib/api";

export default function PeoplePage() {
  const [people, setPeople] = useState<PersonSummary[] | null>(null);
  const [settings, setSettings] = useState<FaceSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [naming, setNaming] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [list, config] = await Promise.all([
      api.people().catch(() => []),
      api.faceSettings().catch(() => null),
    ]);
    setPeople(list);
    setSettings(config);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function saveName(personId: string) {
    if (!draft.trim()) return;
    setBusy(true);
    try {
      await api.namePerson(personId, draft.trim());
      setNaming(null);
      setDraft("");
      await load();
    } catch (err) {
      setNote(err instanceof Error ? err.message : "Could not save that name");
      setTimeout(() => setNote(null), 6000);
    } finally {
      setBusy(false);
    }
  }

  async function toggleEnabled(enabled: boolean) {
    setBusy(true);
    try {
      setSettings(await api.updateFaceSettings({ enabled }));
    } finally {
      setBusy(false);
    }
  }

  const named = people?.filter((p) => p.named) ?? [];
  const unnamed = people?.filter((p) => !p.named) ?? [];
  const pending = people?.reduce((n, p) => n + p.pending_count, 0) ?? 0;

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>People</h2>
        {settings && (
          <span className="faint mono">
            {settings.people_count} groups · {settings.faces_count} faces
            {pending > 0 && ` · ${pending} to review`}
          </span>
        )}
      </div>

      {note && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
          {note}
        </div>
      )}

      {settings && !settings.enabled && (
        <div className="card" style={{ borderColor: "var(--amber-line)" }}>
          <strong>Face recognition is off.</strong>
          <p className="faint" style={{ margin: "6px 0 10px", fontSize: "0.86rem" }}>
            No new faces are being detected. The people you have already named
            are kept — turning this off does not delete them.
          </p>
          <button className="btn btn-primary" disabled={busy} onClick={() => toggleEnabled(true)}>
            Turn it back on
          </button>
        </div>
      )}

      {people && people.length === 0 && settings?.enabled && (
        <div className="empty">
          No faces grouped yet. Detection runs after frames are sampled and
          embedded, so this fills in as the library processes.
        </div>
      )}

      {unnamed.length > 0 && (
        <>
          <div className="sectionhead">
            <h2>Who is this?</h2>
            <span className="faint mono">{unnamed.length} groups waiting for a name</span>
          </div>
          <div className="grid">
            {unnamed.map((person) => (
              <div className="tile" key={person.id}>
                <div className="tile-frame">
                  {person.cover ? (
                    <FaceCrop face={person.cover} size={220} />
                  ) : (
                    <div className="placeholder">
                      <span>no preview</span>
                    </div>
                  )}
                  <span className="tile-badge">{person.pending_count} faces</span>
                </div>
                <div className="tile-meta">
                  {naming === person.id ? (
                    <div style={{ display: "grid", gap: 6 }}>
                      <input
                        className="input"
                        autoFocus
                        placeholder="Their name"
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") saveName(person.id);
                          if (e.key === "Escape") setNaming(null);
                        }}
                      />
                      <div style={{ display: "flex", gap: 6 }}>
                        <button
                          className="btn btn-primary"
                          style={{ padding: "5px 12px" }}
                          disabled={busy || !draft.trim()}
                          onClick={() => saveName(person.id)}
                        >
                          Save
                        </button>
                        <button
                          className="btn"
                          style={{ padding: "5px 12px" }}
                          onClick={() => setNaming(null)}
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : (
                    <>
                      <div className="tile-name faint">Unnamed person</div>
                      <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
                        <button
                          className="btn"
                          style={{ padding: "5px 12px" }}
                          onClick={() => {
                            setNaming(person.id);
                            setDraft("");
                          }}
                        >
                          Name them
                        </button>
                        <Link
                          className="btn"
                          style={{ padding: "5px 12px" }}
                          href={`/people/${person.id}`}
                        >
                          Review
                        </Link>
                      </div>
                    </>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {named.length > 0 && (
        <>
          <div className="sectionhead">
            <h2>Named</h2>
            <span className="faint mono">{named.length} people</span>
          </div>
          <div className="grid">
            {named.map((person) => (
              <Link className="tile" key={person.id} href={`/people/${person.id}`}>
                <div className="tile-frame">
                  {person.cover ? (
                    <FaceCrop face={person.cover} size={220} />
                  ) : (
                    <div className="placeholder">
                      <span>no preview</span>
                    </div>
                  )}
                  {person.pending_count > 0 && (
                    <span className="tile-badge">{person.pending_count} to review</span>
                  )}
                </div>
                <div className="tile-meta">
                  <div className="tile-name">{person.name}</div>
                  <div className="tile-sub">
                    <span>{person.confirmed_count} confirmed</span>
                  </div>
                </div>
              </Link>
            ))}
          </div>
        </>
      )}

      {settings?.enabled && (
        <>
          <div className="sectionhead">
            <h2>Settings</h2>
          </div>
          <div className="card">
            <p className="faint" style={{ marginTop: 0, fontSize: "0.86rem" }}>
              Faces are grouped and named on this machine only. There is no
              pre-trained identity set and no external lookup — every name here
              was typed by you, and no face crop is stored.
            </p>
            <label className="field" data-layout="row">
              <input
                type="checkbox"
                checked={settings.enabled}
                disabled={busy}
                onChange={(e) => toggleEnabled(e.target.checked)}
              />
              <span>
                Detect and group faces
                <span className="faint">
                  {" "}
                  — turning this off stops detection but keeps the names you
                  have given
                </span>
              </span>
            </label>
          </div>
        </>
      )}
    </Shell>
  );
}
