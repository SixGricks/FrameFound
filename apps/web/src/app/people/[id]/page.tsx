"use client";

// One person: confirm, reject, rename, forget.
//
// Unreviewed faces come first because the review queue is the reason to open
// this page. Confirming is one click on the face, not a form — the whole cost
// of confirm-before-it-counts is clicks, so they should be cheap.

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import FaceCrop from "@/components/FaceCrop";
import Shell from "@/components/Shell";
import { api, type PersonDetail } from "@/lib/api";

export default function PersonPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const personId = params.id;

  const [person, setPerson] = useState<PersonDetail | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.person(personId);
      setPerson(data);
      setDraft(data.named ? data.name : "");
    } catch {
      setError("Could not load this person.");
    }
  }, [personId]);

  useEffect(() => {
    load();
  }, [load]);

  function toggle(faceId: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(faceId)) next.delete(faceId);
      else next.add(faceId);
      return next;
    });
  }

  async function judge(verdict: "confirm" | "reject") {
    if (!selected.size) return;
    setBusy(true);
    try {
      const ids = [...selected];
      if (verdict === "confirm") await api.confirmFaces(personId, ids);
      else await api.rejectFaces(personId, ids);
      setSelected(new Set());
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save that");
    } finally {
      setBusy(false);
    }
  }

  async function rename() {
    if (!draft.trim()) return;
    setBusy(true);
    try {
      await api.namePerson(personId, draft.trim());
      setRenaming(false);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not rename");
    } finally {
      setBusy(false);
    }
  }

  async function forget() {
    if (
      !confirm(
        "Forget this person entirely? Their name, the grouping, and the face " +
          "data are all deleted. This cannot be undone.",
      )
    ) {
      return;
    }
    setBusy(true);
    await api.forgetPerson(personId).catch(() => undefined);
    router.push("/people");
  }

  if (error && !person) {
    return (
      <Shell>
        <div className="empty">{error}</div>
        <Link className="btn" href="/people">
          Back to people
        </Link>
      </Shell>
    );
  }

  const pending = person?.faces.filter((f) => f.source === "detected") ?? [];
  const confirmed = person?.faces.filter((f) => f.source === "confirmed") ?? [];

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>{person?.name ?? "Loading…"}</h2>
        {person && (
          <span className="faint mono">
            {person.confirmed_count} confirmed
            {person.pending_count > 0 && ` · ${person.pending_count} to review`}
          </span>
        )}
      </div>

      {error && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
          {error}
        </div>
      )}

      <div className="toolbar">
        <Link className="btn" href="/people" style={{ padding: "6px 12px" }}>
          ← All people
        </Link>
        {renaming ? (
          <>
            <input
              className="input"
              autoFocus
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && rename()}
            />
            <button className="btn btn-primary" disabled={busy} onClick={rename}>
              Save
            </button>
            <button className="btn" onClick={() => setRenaming(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button className="btn" onClick={() => setRenaming(true)}>
            {person?.named ? "Rename" : "Name them"}
          </button>
        )}
        <button
          className="btn"
          style={{ marginLeft: "auto", borderColor: "var(--ember)", color: "var(--ember)" }}
          disabled={busy}
          onClick={forget}
        >
          Forget this person
        </button>
      </div>

      {selected.size > 0 && (
        <div className="toolbar">
          <span className="pill" data-tone="warn">
            {selected.size} selected
          </span>
          <button
            className="btn btn-primary"
            disabled={busy}
            onClick={() => judge("confirm")}
          >
            Yes, this is {person?.named ? person.name : "them"}
          </button>
          <button className="btn" disabled={busy} onClick={() => judge("reject")}>
            No, not them
          </button>
          <button className="btn" onClick={() => setSelected(new Set())}>
            Clear
          </button>
        </div>
      )}

      {pending.length > 0 && (
        <>
          <div className="sectionhead">
            <h2>To review</h2>
            <span className="faint mono">tap a face to select it</span>
          </div>
          <div className="facegrid">
            {pending.map((face) => (
              <button
                type="button"
                key={face.face_id}
                className="facepick"
                data-selected={selected.has(face.face_id)}
                onClick={() => toggle(face.face_id)}
                title={face.filename}
              >
                <FaceCrop face={face} size={104} />
                {face.similarity != null && (
                  <span className="faint mono" style={{ fontSize: "0.66rem" }}>
                    {Math.round(face.similarity * 100)}%
                  </span>
                )}
              </button>
            ))}
          </div>
        </>
      )}

      {confirmed.length > 0 && (
        <>
          <div className="sectionhead">
            <h2>Confirmed</h2>
            <span className="faint mono">{confirmed.length} faces</span>
          </div>
          <div className="facegrid">
            {confirmed.map((face) => (
              <Link key={face.face_id} href={`/assets/${face.asset_id}`} title={face.filename}>
                <FaceCrop face={face} size={104} />
              </Link>
            ))}
          </div>
        </>
      )}

      {person && !pending.length && !confirmed.length && (
        <div className="empty">No faces in this group any more.</div>
      )}
    </Shell>
  );
}
