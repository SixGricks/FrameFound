"use client";

// One person: confirm, reject, rename, forget, and search for more of them.
//
// Unreviewed faces come first because the review queue is the reason to open
// this page, and within that queue they are ranked by how much they look like
// this person. That ranking is what makes bulk review possible: a ranked grid
// has one boundary between yes and no, so the operator's whole job is to find
// it and draw a line — not to click 295 faces one at a time.
//
// Three gestures, in the order they cost the operator least:
//   1. "Down to here"  — everything from the top to this face is them.
//   2. Shift-click     — a range, for when the ranking gets one stretch wrong.
//   3. Click           — a single face, for the tail where it is genuinely mixed.

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import Autocomplete from "@/components/Autocomplete";
import FaceCrop from "@/components/FaceCrop";
import Shell from "@/components/Shell";
import { api, type FaceRef, type PersonDetail } from "@/lib/api";

// The id-taking endpoints accept at most this many per call.
const ID_BATCH = 500;

export default function PersonPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const personId = params.id;

  const [person, setPerson] = useState<PersonDetail | null>(null);
  const [suggested, setSuggested] = useState<FaceRef[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [anchor, setAnchor] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.person(personId);
      setPerson(data);
      setDraft(data.named ? data.name : "");
      setSuggested(data.named ? await api.suggestions(personId) : []);
    } catch {
      setError("Could not load this person.");
    }
  }, [personId]);

  useEffect(() => {
    load();
  }, [load]);

  const pending = person?.faces.filter((f) => f.source === "detected") ?? [];
  const confirmed = person?.faces.filter((f) => f.source === "confirmed") ?? [];

  /** Select through to `index`, or from the last click if shift is held. */
  function choose(list: FaceRef[], index: number, shift: boolean, isPending: boolean) {
    const target = list[index];
    if (!target) return;
    const setter = isPending ? setSelected : setPicked;
    const from = shift && anchor !== null ? anchor : index;
    setter((current) => {
      const next = new Set(current);
      const [lo, hi] = from <= index ? [from, index] : [index, from];
      const adding = !next.has(target.face_id);
      for (let i = lo; i <= hi; i += 1) {
        const face = list[i];
        if (!face) continue;
        if (adding) next.add(face.face_id);
        else next.delete(face.face_id);
      }
      return next;
    });
    setAnchor(index);
  }

  /** Everything from the top of the ranking down to and including `index`. */
  function chooseDownTo(list: FaceRef[], index: number, isPending: boolean) {
    const setter = isPending ? setSelected : setPicked;
    setter(new Set(list.slice(0, index + 1).map((f) => f.face_id)));
    setAnchor(index);
  }

  /**
   * A contiguous run from the top of `list`, or -1.
   *
   * This is what decides whether a confirmation can be expressed as "and
   * everything at least this similar" rather than as a list of ids — and that
   * matters because the page holds at most 600 faces while the queue may hold
   * more. Confirming a prefix by threshold settles the ones off-screen too,
   * which is the difference between clearing a backlog and denting it.
   */
  function prefixLength(list: FaceRef[], chosen: Set<string>): number {
    if (!chosen.size || chosen.size > list.length) return -1;
    for (let i = 0; i < list.length; i += 1) {
      const face = list[i];
      if (!face || chosen.has(face.face_id) !== i < chosen.size) return -1;
    }
    return chosen.size;
  }

  async function inBatches(ids: string[], run: (batch: string[]) => Promise<unknown>) {
    for (let i = 0; i < ids.length; i += ID_BATCH) {
      await run(ids.slice(i, i + ID_BATCH));
    }
  }

  async function act(what: () => Promise<string>) {
    setBusy(true);
    setError(null);
    try {
      setNotice(await what());
      setSelected(new Set());
      setPicked(new Set());
      setAnchor(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save that");
    } finally {
      setBusy(false);
    }
  }

  function judge(verdict: "confirm" | "reject") {
    if (!selected.size) return;
    const ids = [...selected];
    return act(async () => {
      if (verdict === "reject") {
        await inBatches(ids, (batch) => api.rejectFaces(personId, batch));
        return `${ids.length} set aside.`;
      }
      // Prefer the threshold form when the selection is a clean prefix and
      // there is more queue than the page is showing.
      //
      // Only when the boundary face actually has a score. An unscored face
      // would send a bar of 0, which every pending face clears — so drawing a
      // line under ten faces would silently confirm all of them. Falling back
      // to ids is merely slower; getting this wrong attributes hundreds of
      // photographs to the wrong person with no record of what was agreed.
      const run = prefixLength(pending, selected);
      const line = run > 0 ? pending[run - 1]?.similarity : null;
      const beyond = (person?.pending_count ?? 0) > pending.length;
      if (line != null && line > 0 && (beyond || ids.length > ID_BATCH)) {
        const { confirmed: n } = await api.confirmBulk(personId, { minSimilarity: line });
        return `${n} confirmed — everything ${Math.round(line * 100)}% and above.`;
      }
      await inBatches(ids, (batch) => api.confirmBulk(personId, { faceIds: batch }));
      return `${ids.length} confirmed.`;
    });
  }

  function judgeSuggestions(verdict: "accept" | "reject") {
    if (!picked.size) return;
    const ids = [...picked];
    return act(async () => {
      if (verdict === "reject") {
        await inBatches(ids, (batch) => api.rejectSuggestions(personId, batch));
        return `${ids.length} ruled out — they stay wherever they were.`;
      }
      await inBatches(ids, (batch) => api.acceptSuggestions(personId, { faceIds: batch }));
      return `${ids.length} added to ${person?.name}.`;
    });
  }

  function discover() {
    return act(async () => {
      const { found, searched, threshold } = await api.discoverMore(personId);
      const pool = searched.toLocaleString();
      return found
        ? `Found ${found} possible matches among ${pool} unnamed faces, down to ` +
            `${Math.round(threshold * 100)}% similar.`
        : `Nothing new above ${Math.round(threshold * 100)}% among ${pool} unnamed ` +
            `faces. Confirming more sharpens the match — try again after.`;
    });
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

  async function mergeInto(targetId: string) {
    // No confirmation dialog. Picking a specific person from a list and
    // pressing "Merge into" is already an unambiguous statement of intent —
    // asking again treats a deliberate choice as if it might have been a slip,
    // and merging is the single most common correction there is, so the tax is
    // paid constantly.
    setBusy(true);
    try {
      await api.mergePeople(targetId, personId);
      // This person no longer exists, so there is no page left to show.
      router.push(`/people/${targetId}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not merge those two");
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

  function grid(list: FaceRef[], chosen: Set<string>, isPending: boolean) {
    return (
      <div className="facegrid">
        {list.map((face, index) => (
          <div key={face.face_id} className="facepick-wrap">
            <button
              type="button"
              className="facepick"
              data-selected={chosen.has(face.face_id)}
              onClick={(event) => choose(list, index, event.shiftKey, isPending)}
              title={`${face.filename} — shift-click to select a range`}
            >
              <FaceCrop face={face} size={104} />
              {face.similarity != null && (
                <span className="faint mono" style={{ fontSize: "0.66rem" }}>
                  {Math.round(face.similarity * 100)}%
                </span>
              )}
            </button>
            <button
              type="button"
              className="facepick-downto"
              onClick={() => chooseDownTo(list, index, isPending)}
              title="Select this face and every better match above it"
            >
              ↑ down to here
            </button>
          </div>
        ))}
      </div>
    );
  }

  const them = person?.named ? person.name : "them";

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>{person?.name ?? "Loading…"}</h2>
        {person && (
          <span className="faint mono">
            {person.confirmed_count} confirmed
            {person.pending_count > 0 && ` · ${person.pending_count} to review`}
            {person.suggestion_count > 0 && ` · ${person.suggestion_count} found`}
          </span>
        )}
      </div>

      {error && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
          {error}
        </div>
      )}
      {notice && !error && (
        <div className="card" role="status">
          {notice}
        </div>
      )}

      <div className="toolbar">
        <Link className="btn" href="/people" style={{ padding: "6px 12px" }}>
          ← All people
        </Link>
        {renaming ? (
          <>
            <div style={{ flex: 1, minWidth: 220 }}>
              <Autocomplete
                value={draft}
                onChange={setDraft}
                ariaLabel="Name this person"
                placeholder="Their name"
                disabled={busy}
                // Clustering routinely makes several groups for one person — a
                // different haircut, a decade, a bad angle. Showing the
                // existing "Dad" while you type "Dad" is the only moment the
                // operator has the context to say it is the same person.
                fetcher={async (q) => {
                  const found = await api.suggestNames(q, personId);
                  return found.map((p) => ({
                    id: p.id,
                    label: p.name,
                    hint: p.confirmed_count
                      ? `${p.confirmed_count} confirmed`
                      : "no confirmed faces",
                    exact: p.exact,
                  }));
                }}
                // Picking only fills the field. Merging is a separate,
                // explicit button — one is naming, the other destroys a
                // grouping, and they should not be the same gesture.
                onPick={(pick) => setDraft(pick.label)}
                renderExtra={(pick) => (
                  <button
                    type="button"
                    className="btn"
                    disabled={busy}
                    onClick={() => mergeInto(pick.id)}
                  >
                    Merge into
                  </button>
                )}
              />
            </div>
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
        {person?.named && (
          <button
            className="btn"
            disabled={busy || !person.confirmed_count}
            onClick={discover}
            title={
              person.confirmed_count
                ? "Compare every unnamed face in the catalogue against this person"
                : "Confirm a few faces first — there is nothing to match against yet"
            }
          >
            {busy ? "Working…" : "Find more of them"}
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
          <button className="btn btn-primary" disabled={busy} onClick={() => judge("confirm")}>
            Yes, this is {them}
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
            <span className="faint mono">
              best match first · shift-click for a range
              {person && person.pending_count > pending.length
                ? ` · showing ${pending.length} of ${person.pending_count}`
                : ""}
            </span>
          </div>
          <div className="toolbar">
            <button
              className="btn"
              disabled={busy}
              onClick={() => chooseDownTo(pending, pending.length - 1, true)}
            >
              Select all {person?.pending_count ?? pending.length}
            </button>
          </div>
          {grid(pending, selected, true)}
        </>
      )}

      {picked.size > 0 && (
        <div className="toolbar">
          <span className="pill" data-tone="warn">
            {picked.size} selected
          </span>
          <button
            className="btn btn-primary"
            disabled={busy}
            onClick={() => judgeSuggestions("accept")}
          >
            Yes, add to {them}
          </button>
          <button className="btn" disabled={busy} onClick={() => judgeSuggestions("reject")}>
            No, not them
          </button>
          <button className="btn" onClick={() => setPicked(new Set())}>
            Clear
          </button>
        </div>
      )}

      {suggested.length > 0 && (
        <>
          <div className="sectionhead">
            <h2>Found elsewhere</h2>
            <span className="faint mono">
              from unnamed faces across the catalogue · saying no leaves them where they are
            </span>
          </div>
          {grid(suggested, picked, false)}
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

      {person && !pending.length && !confirmed.length && !suggested.length && (
        <div className="empty">No faces in this group any more.</div>
      )}
    </Shell>
  );
}
