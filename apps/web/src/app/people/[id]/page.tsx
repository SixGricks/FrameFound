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
//
// And one for looking. A 104 px crop is usually enough to say yes or no, and
// when it is not — a profile, a child among cousins — the answer is in the
// rest of the picture, which this page could not show: only confirmed faces
// linked anywhere, and that link left the page and took the selection with
// it. The magnifier on a face opens the photograph or clip it was found in,
// in the viewer (MediaPreview), over the page; a clip starts at the moment
// the face was seen. The viewer steps through the grid, answers the face on
// screen, or adds it to the selection; nothing selected is lost by looking.
//
// The answer buttons are pinned to the foot of the screen while anything is
// selected (and Y / N / Esc work), as on Tags. They used to sit above their
// grid: after "down to here" two hundred faces into a queue they were several
// screens away.

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import Autocomplete from "@/components/Autocomplete";
import FaceCrop from "@/components/FaceCrop";
import MediaPreview from "@/components/MediaPreview";
import Shell from "@/components/Shell";
import { setChatContext } from "@/lib/chatContext";
import { timecode } from "@/lib/format";
import { usePageTitle } from "@/lib/hooks";
import { typingInto } from "@/lib/keys";
import { count as countOf } from "@/lib/storage";
import { api, type FaceRef, type PersonDetail } from "@/lib/api";

// The id-taking endpoints accept at most this many per call.
const ID_BATCH = 500;

// The two grids that are answered, and the third that is only looked at.
type Answered = "pending" | "found";
type Which = Answered | "confirmed";
type Viewing = { list: Which; faceId: string };

/** `current` without the ids that are no longer `alive`; the same set when
 *  nothing left, so an unchanged selection does not redraw the page. */
function only(current: Set<string>, alive: Set<string>): Set<string> {
  const next = new Set([...current].filter((id) => alive.has(id)));
  return next.size === current.size ? current : next;
}

export default function PersonPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const personId = params.id;

  const [person, setPerson] = useState<PersonDetail | null>(null);
  const [suggested, setSuggested] = useState<FaceRef[]>([]);
  usePageTitle(person?.name || "People");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [picked, setPicked] = useState<Set<string>>(new Set());
  // The last face clicked in each grid, which a shift-click extends from.
  // One per grid: with a single anchor, a shift-click in "Found elsewhere"
  // ran from wherever the last click in "To review" happened to be. Face
  // ids, not positions — answered faces leave and every index after shifts.
  const [anchors, setAnchors] = useState<Record<Answered, string | null>>({
    pending: null,
    found: null,
  });
  // The grid selected in last: the one — the only one — Y / N / Esc act on.
  const [active, setActive] = useState<Answered>("pending");
  const [busy, setBusy] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const renameButton = useRef<HTMLButtonElement>(null);
  const wasRenaming = useRef(false);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // The face whose picture is open in the viewer. Each face says whether it
  // is in a photograph or a clip, and when in the clip; the page used to ask
  // the server about every asset before its picture could open.
  const [viewing, setViewing] = useState<Viewing | null>(null);
  const pageHead = useRef<HTMLHeadingElement>(null);
  const pendingHead = useRef<HTMLHeadingElement>(null);
  const foundHead = useRef<HTMLHeadingElement>(null);

  /**
   * Read the person again. `thenView` is where the viewer goes if the face
   * it is showing has left its grid (it was just answered) — decided here,
   * with the new lists, so no render has the viewer on a face that is gone.
   */
  const load = useCallback(
    async (thenView: Viewing | null = null) => {
      try {
        const data = await api.person(personId);
        const found = data.named ? await api.suggestions(personId) : [];
        const alive: Record<Which, Set<string>> = {
          pending: new Set(
            data.faces.filter((f) => f.source === "detected").map((f) => f.face_id),
          ),
          found: new Set(found.map((f) => f.face_id)),
          confirmed: new Set(
            data.faces.filter((f) => f.source === "confirmed").map((f) => f.face_id),
          ),
        };
        setPerson(data);
        setDraft(data.named ? data.name : "");
        setSuggested(found);
        // Only the faces that have gone leave a selection. Every save used
        // to empty both — one Yes in "Found elsewhere" threw away a line
        // drawn in "To review".
        setSelected((current) => only(current, alive.pending));
        setPicked((current) => only(current, alive.found));
        setAnchors((current) => ({
          pending: current.pending && alive.pending.has(current.pending) ? current.pending : null,
          found: current.found && alive.found.has(current.found) ? current.found : null,
        }));
        setViewing((current) => {
          if (!current || alive[current.list].has(current.faceId)) return current;
          return thenView && alive[thenView.list].has(thenView.faceId) ? thenView : null;
        });
      } catch {
        setError("Could not load this person.");
      }
    },
    [personId],
  );

  useEffect(() => {
    load();
  }, [load]);

  const pending = person?.faces.filter((f) => f.source === "detected") ?? [];
  const confirmed = person?.faces.filter((f) => f.source === "confirmed") ?? [];
  const lists: Record<Which, FaceRef[]> = { pending, found: suggested, confirmed };

  /** Select `index`, or with shift everything from this grid's last click. */
  function choose(which: Answered, index: number, shift: boolean) {
    const list = lists[which];
    const target = list[index];
    if (!target) return;
    const setter = which === "pending" ? setSelected : setPicked;
    const anchor = anchors[which];
    const anchored = anchor ? list.findIndex((f) => f.face_id === anchor) : -1;
    const from = shift && anchored >= 0 ? anchored : index;
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
    setAnchors((current) => ({ ...current, [which]: target.face_id }));
    setActive(which);
  }

  /** Everything from the top of the ranking down to and including `index`. */
  function chooseDownTo(which: Answered, index: number) {
    const list = lists[which];
    const setter = which === "pending" ? setSelected : setPicked;
    setter(new Set(list.slice(0, index + 1).map((f) => f.face_id)));
    setAnchors((current) => ({ ...current, [which]: list[index]?.face_id ?? null }));
    setActive(which);
  }

  function clear(which: Answered) {
    (which === "pending" ? setSelected : setPicked)(new Set());
    setAnchors((current) => ({ ...current, [which]: null }));
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

  /** Save something, say what it did, and read the person again (which
   *  takes the answered faces out of the selections). `thenView` is asked,
   *  once the save is through, where the viewer should go next. */
  async function act(what: () => Promise<string>, thenView?: () => Viewing | null) {
    setBusy(true);
    setError(null);
    try {
      setNotice(await what());
      await load(thenView?.() ?? null);
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

  /** Open the picture a face came from, in the viewer. */
  function look(list: Which, face: FaceRef) {
    setViewing({ list, faceId: face.face_id });
  }

  function closeViewer() {
    setViewing(null);
  }

  // The face in the viewer, looked up in its grid as the grid is now: one
  // that has been answered is not found, and the viewer is not drawn.
  const viewList = viewing ? lists[viewing.list] : [];
  const viewAt = viewing ? viewList.findIndex((f) => f.face_id === viewing.faceId) : -1;
  const viewed = viewAt >= 0 ? viewList[viewAt] : undefined;
  const looking = viewing !== null && viewed !== undefined;
  // Where a clip starts: the moment the face was seen in it.
  const viewedMoment =
    viewed?.media_type === "video" && viewed.ts_ms != null ? viewed.ts_ms : null;

  /** Move the viewer along the grid it was opened from. */
  function step(by: number) {
    const target = viewList[viewAt + by];
    if (viewing && target) look(viewing.list, target);
  }

  /** Answer the one face in the viewer, and show the next. */
  async function answerViewed(yes: boolean) {
    if (!viewing || !viewed || busy || viewing.list === "confirmed") return;
    const which = viewing.list;
    const id = viewed.face_id;
    // Picked before the save: once this face leaves the grid every index
    // after it shifts. The next one down; at the end, the one above.
    const next = viewList[viewAt + 1] ?? viewList[viewAt - 1];
    await act(
      async () => {
        if (which === "pending") {
          if (yes) await api.confirmBulk(personId, { faceIds: [id] });
          else await api.rejectFaces(personId, [id]);
        } else if (yes) {
          await api.acceptSuggestions(personId, { faceIds: [id] });
        } else {
          await api.rejectSuggestions(personId, [id]);
        }
        if (which === "pending") return yes ? "1 confirmed." : "1 set aside.";
        return yes ? `1 added to ${person?.name}.` : "1 ruled out — it stays wherever it was.";
      },
      () => (next ? { list: which, faceId: next.face_id } : null),
    );
  }

  /** Where focus goes when the viewer closes: the face last on screen, or
   *  the head of its grid when that face has been answered and is gone. */
  function focusAfterViewing(): HTMLElement | null {
    const tile = viewed
      ? document.querySelector<HTMLElement>(`[data-look="${viewed.face_id}"]`)
      : null;
    if (tile) return tile;
    // The last face of a grid takes the grid, heading and all, with it.
    const head = viewing?.list === "found" ? foundHead.current : pendingHead.current;
    return head ?? pageHead.current;
  }

  const sizes: Record<Answered, number> = { pending: selected.size, found: picked.size };
  const anySelected = sizes.pending > 0 || sizes.found > 0;
  // The selection the keys answer: the one in the grid selected in last, and
  // no other. They used to fall through to the other grid when that one had
  // nothing selected — so with faces ticked in "Found elsewhere" to rule out
  // later, a Y in "To review" saved, emptied its selection, and a second Y
  // (a double tap, the next press in rhythm) added every one of those faces
  // to this person. The other grid's row keeps its buttons; selecting in
  // that grid makes it the one the keys answer.
  //
  // And none at all while the name field is open. Y and N are letters: a
  // name typed with focus anywhere but the field ("Nancy", "Mary") answered
  // the whole selection, and a rejected face is never offered again.
  const keyed: Answered | null = !renaming && sizes[active] ? active : null;
  // The grid the viewer answers and selects in; none for a confirmed face,
  // which is only looked at.
  const answering: Answered | null =
    viewing && viewing.list !== "confirmed" ? viewing.list : null;

  // For the chat panel: who this is and which pictures are ticked. A face
  // id means nothing to Claude's tools, so the selection is sent as the
  // photographs and clips the ticked faces are in (two faces in one
  // photograph are one item). Joined into one string so the effect runs
  // when the selection changes, not on every render.
  const personName = person === null ? null : person.name || "";
  const tickedItems = Array.from(
    new Set(
      [
        ...(person?.faces ?? []).filter((face) => selected.has(face.face_id)),
        ...suggested.filter((face) => picked.has(face.face_id)),
      ].map((face) => face.asset_id),
    ),
  ).join(" ");
  useEffect(
    () =>
      setChatContext({
        ...(personName === null
          ? {}
          : { title: personName ? `People: ${personName}` : "People: someone not named yet" }),
        selection: tickedItems ? tickedItems.split(" ") : [],
      }),
    [personName, tickedItems],
  );

  // The name field takes focus as it opens (it used not to: the button that
  // opened it had gone, focus fell to the page, and the name was typed at
  // the shortcuts below). When it closes, focus goes back to the button
  // rather than to the top of the page.
  useEffect(() => {
    if (wasRenaming.current && !renaming) renameButton.current?.focus();
    wasRenaming.current = renaming;
  }, [renaming]);

  // Y / N answer the selection and Esc clears it, as on Tags — the hands
  // stay where they are while working down a long queue. Bound afresh on
  // every render: it reads the selections as they are now.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      // The viewer is open: the keys are about the face on screen and the
      // viewer handles them. Left to run, Y would answer the selection
      // behind it as well.
      if (looking || !keyed || busy) return;
      if (typingInto(event.target)) return;
      const el = event.target instanceof Element ? event.target : null;
      if (event.metaKey || event.ctrlKey || event.altKey || event.repeat) return;
      // A key the page's menu already used (Esc closing Manage), or one
      // pressed while focus is in the top bar or the chat panel, is not
      // meant for the grid. (The panel stops its own keys too; this holds
      // if it ever does not.)
      if (
        event.defaultPrevented ||
        el?.closest('.topbar, [role="menu"], [role="listbox"], [data-chat-panel]')
      )
        return;
      const key = event.key.toLowerCase();
      if (key === "escape") {
        clear(keyed);
      } else if (key === "y") {
        event.preventDefault();
        if (keyed === "pending") judge("confirm");
        else judgeSuggestions("accept");
      } else if (key === "n") {
        event.preventDefault();
        if (keyed === "pending") judge("reject");
        else judgeSuggestions("reject");
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

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

  function grid(which: Answered, chosen: Set<string>) {
    const list = lists[which];
    return (
      <div className="facegrid">
        {list.map((face, index) => (
          <div key={face.face_id} className="facepick-wrap">
            <button
              type="button"
              className="facepick"
              data-selected={chosen.has(face.face_id)}
              aria-pressed={chosen.has(face.face_id)}
              onClick={(event) => choose(which, index, event.shiftKey)}
              title={`${face.filename} — click to select, shift-click for a range`}
            >
              <FaceCrop face={face} size={104} />
              {face.similarity != null && (
                <span className="faint mono" style={{ fontSize: "0.66rem" }}>
                  {Math.round(face.similarity * 100)}%
                </span>
              )}
            </button>
            {/* A sibling of the face, not inside it: the face is a button,
                and a click on this must not select it. */}
            <button
              type="button"
              className="facepick-look"
              data-look={face.face_id}
              aria-haspopup="dialog"
              aria-label={`See the whole picture this face is from, ${face.filename}`}
              title="See the whole picture this face is from"
              onClick={() => look(which, face)}
            >
              <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
                <circle cx="6.8" cy="6.8" r="4.3" fill="none" stroke="currentColor" strokeWidth="1.6" />
                <path d="M10.2 10.2 14 14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
              </svg>
            </button>
            <button
              type="button"
              className="facepick-downto"
              onClick={() => chooseDownTo(which, index)}
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

  /** One grid's answers, as a row of the bar at the foot of the screen. */
  function answerRow(which: Answered) {
    const count = sizes[which];
    const hasKeys = keyed === which;
    // No row has the keys, and not because a name is being typed: this
    // selection was made before the reader went to work in the other grid.
    // Said, so that a Y that does nothing is not taken for a fault.
    const keysElsewhere = keyed === null && !renaming;
    return (
      <div
        key={which}
        className="toolbar"
        role="group"
        aria-label={which === "pending" ? "Answer the faces to review" : "Answer the faces found elsewhere"}
      >
        <span className="pill" data-tone="warn" aria-live="polite">
          {busy
            ? "Saving…"
            : `${count} selected in ${which === "pending" ? "To review" : "Found elsewhere"}`}
        </span>
        <button
          className="btn btn-primary"
          disabled={busy}
          onClick={() => (which === "pending" ? judge("confirm") : judgeSuggestions("accept"))}
          title={hasKeys ? "Y" : undefined}
        >
          {which === "pending" ? `Yes, this is ${them}` : `Yes, add to ${them}`}
        </button>
        <button
          className="btn"
          disabled={busy}
          onClick={() => (which === "pending" ? judge("reject") : judgeSuggestions("reject"))}
          title={hasKeys ? "N" : undefined}
        >
          No, not them
        </button>
        <button className="btn" onClick={() => clear(which)} title={hasKeys ? "Esc" : undefined}>
          Clear
        </button>
        {hasKeys && (
          <span className="faint pointer-hint" style={{ fontSize: "0.76rem" }}>
            Y / N answer these · Esc clears
          </span>
        )}
        {keysElsewhere && (
          <span className="faint pointer-hint" style={{ fontSize: "0.76rem" }}>
            Use these buttons — Y / N answer the grid you last selected in
          </span>
        )}
      </div>
    );
  }

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2 ref={pageHead} tabIndex={-1}>
          {person?.name ?? "Loading…"}
        </h2>
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
                // The reader pressed "Name them" to type a name: the field
                // must be where the typing goes.
                autoFocus
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
          <button ref={renameButton} className="btn" onClick={() => setRenaming(true)}>
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

      {pending.length > 0 && (
        <>
          <div className="sectionhead">
            <h2 ref={pendingHead} tabIndex={-1}>
              To review
            </h2>
            <span className="faint mono">
              best match first
              {person && person.pending_count > pending.length
                ? ` · showing ${pending.length} of ${person.pending_count}`
                : ""}
            </span>
          </div>
          <div className="toolbar">
            <button
              className="btn"
              disabled={busy}
              onClick={() => chooseDownTo("pending", pending.length - 1)}
            >
              Select all {person?.pending_count ?? pending.length}
            </button>
            <span className="faint" style={{ fontSize: "0.8rem" }}>
              click a face to select
              <span className="pointer-hint">, shift-click for a range</span> · the magnifier
              shows the whole picture
            </span>
          </div>
          {grid("pending", selected)}
        </>
      )}

      {suggested.length > 0 && (
        <>
          <div className="sectionhead">
            <h2 ref={foundHead} tabIndex={-1}>
              Found elsewhere
            </h2>
            <span className="faint mono">
              from unnamed faces across the catalogue · saying no leaves them where they are
            </span>
          </div>
          {grid("found", picked)}
        </>
      )}

      {confirmed.length > 0 && (
        <>
          <div className="sectionhead">
            <h2>Confirmed</h2>
            <span className="faint mono">{countOf(confirmed.length, "face")} · click one to see its picture</span>
          </div>
          <div className="facegrid">
            {confirmed.map((face) => (
              // Still a link to the asset's page — a modified or middle
              // click opens it in a new tab as before — but a plain click
              // opens the viewer: followed in this tab, the link took the
              // selections in the grids above with it.
              <Link
                key={face.face_id}
                href={`/assets/${face.asset_id}`}
                prefetch={false}
                data-look={face.face_id}
                aria-haspopup="dialog"
                aria-label={`Look at ${face.filename}`}
                title={`${face.filename} — click to look`}
                onClick={(event) => {
                  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                  event.preventDefault();
                  look("confirmed", face);
                }}
              >
                <FaceCrop face={face} size={104} />
              </Link>
            ))}
          </div>
        </>
      )}

      {person && !pending.length && !confirmed.length && !suggested.length && (
        <div className="empty">No faces in this group any more.</div>
      )}

      {anySelected && (
        // Pinned to the foot of the screen while anything is selected, as on
        // Tags: never several screens above the face just clicked. Each
        // grid's answers are its own row — shown for as long as that grid
        // has a selection, whichever grid the keys are answering; the grid
        // selected in last comes first.
        <div className="tagreview-bar reviewbar-rows" role="toolbar" aria-label="Answer the selection">
          {(active === "found" ? (["found", "pending"] as const) : (["pending", "found"] as const))
            .filter((which) => sizes[which] > 0)
            .map((which) => answerRow(which))}
        </div>
      )}

      {viewing && viewed && (
        // Mounted once, beside the grids — never inside a face tile.
        <MediaPreview
          assetId={viewed.asset_id}
          mediaType={viewed.media_type}
          filename={viewed.filename}
          startAt={viewedMoment != null ? viewedMoment / 1000 : undefined}
          // Two faces of one clip side by side: stepping between them moves
          // the clip to the new face's moment, even one found in the same frame.
          momentKey={viewed.face_id}
          heading={
            viewing.list === "confirmed"
              ? (person?.name ?? undefined)
              : person?.named
                ? `Is this ${person.name}?`
                : "Is this the same person?"
          }
          detail={
            [
              viewed.similarity != null && viewing.list !== "confirmed"
                ? `${Math.round(viewed.similarity * 100)}% similar`
                : null,
              viewed.media_type !== "video"
                ? null
                : viewedMoment != null
                  ? `starts where the face is seen, ${timecode(viewedMoment)}`
                  : "the face appears somewhere in this clip",
            ]
              .filter(Boolean)
              .join(" · ") || undefined
          }
          position={`${viewAt + 1} of ${viewList.length}`}
          // What the last answer did — nothing to say under a confirmed
          // face, which is not being asked about.
          status={answering ? notice : null}
          problem={error}
          busy={busy}
          onClose={closeViewer}
          onPrev={viewAt > 0 ? () => step(-1) : undefined}
          onNext={viewAt < viewList.length - 1 ? () => step(1) : undefined}
          onYes={answering ? () => answerViewed(true) : undefined}
          onNo={answering ? () => answerViewed(false) : undefined}
          yesLabel={answering === "found" ? `Yes, add to ${them}` : `Yes, this is ${them}`}
          noLabel="No, not them"
          onSelect={answering ? () => choose(answering, viewAt, false) : undefined}
          selected={
            answering !== null &&
            (answering === "pending" ? selected : picked).has(viewed.face_id)
          }
          returnFocus={focusAfterViewing}
        >
          {/* A group photograph holds a dozen faces; this is the one being
              asked about. */}
          <span className="preview-face">
            <FaceCrop face={viewed} size={44} />
            <span>this face</span>
          </span>
        </MediaPreview>
      )}
    </Shell>
  );
}
