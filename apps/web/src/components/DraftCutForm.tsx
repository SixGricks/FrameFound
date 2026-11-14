"use client";

// Draft a cut: the brief, the footage, the length — and one line saying who
// will draft it and when, before the button is pressed (an Editor that only
// fits overnight starts at 21:30, and finding that out after pressing Draft
// would read as a fault).
//
// Used on /cuts and on a cut's page ("Draft again": the page says which
// version the result follows, `revises`). Clips ticked in Search arrive on
// /cuts in its address and are given here as `selection`: footage the page
// holds, not the form, used instead of a folder for as long as it is given.
// The form only asks; the draft itself lives on the server and is shown by
// the page that holds this form, so leaving the page loses nothing.
//
// What was typed is kept when the server says no: its sentence is shown
// above the button and the form stays as it was.
//
// "Try again" hands the form everything a draft was asked for, and the form
// shows all of it: footage that is not a picked folder (clips named one by
// one, the chat's "folders matching") is named, with an x that goes back to
// the picker, and a draft that was to be the next version of a cut says so,
// with a way to make it a new cut instead. Neither is ever sent unseen.
//
// Clips held by id (from Search, or by Try again) are listed by name under
// their pill, each a link to the clip's own page: counted and nothing more,
// the footage about to be cut could not be seen or opened from here.

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import Autocomplete, { type Suggestion } from "@/components/Autocomplete";
import { ApiError, api, type CutDraft, type CutEditorStatus, type CutFolder } from "@/lib/api";
import {
  BRIEF_MAX,
  CLIPS_NAMED,
  LENGTHS,
  REVISED_GONE,
  TITLE_MAX,
  blankForm,
  clipRows,
  draftBody,
  editorLine,
  folderFromKey,
  folderHint,
  folderKey,
  folderLabel,
  folderReadiness,
  footageWords,
  refusalLink,
  revisedIsGone,
  revisesLine,
  type DraftForm,
} from "@/lib/cuts";
import { focusIfLost } from "@/lib/localModels";

/** A form to start from: "Try again" fills it in, a link names a folder.
 *  A new `key` replaces whatever is in the form. */
export interface DraftSeed {
  key: number;
  form: DraftForm;
  /** Bring the brief into view and give it the keyboard. */
  focus: boolean;
}

type Refusal = { text: string; link: { href: string; label: string } | null };

/** A folder as the form holds it: what the picker said of it, or only its
 *  place when it came from a link and has not been looked up yet. */
type Chosen = { library_id: string; path: string; found: CutFolder | null };

export default function DraftCutForm({
  editor,
  canDraft,
  readOnly,
  seed,
  selection,
  onUnselect,
  revises,
  askedFor,
  onDrafted,
  onGone,
}: {
  editor: CutEditorStatus;
  /** The server's can_draft: the Editor can take one and the account may ask. */
  canDraft: boolean;
  readOnly: boolean;
  seed: DraftSeed | null;
  /** Clips ticked in Search, used instead of a folder. `note` is said under
   *  them (ticked photographs, which the Editor is not given). */
  selection?: { assetIds: string[]; label: string; note?: string | null } | null;
  /** With `selection`: the x beside the clips was pressed — a folder is
   *  wanted instead — and the page that holds them should let them go.
   *  Without it the clips are fixed and there is no x. */
  onUnselect?: () => void;
  /** On a cut's page: the version the result is the next version after,
   *  whatever the form was filled in from. The page says which it is. Left
   *  out on the Cuts list, where the form says so itself (see `revised`). */
  revises?: string;
  /** On the Cuts list: the draft last asked for, while the page lists it as
   *  under way, and null once it is not. The sentence saying where to find
   *  it goes then. Left out on a cut's page, where the form closes. */
  askedFor?: string | null;
  onDrafted: (draft: CutDraft) => void | Promise<void>;
  /** With `revises`: the server says that version no longer exists, so the
   *  page should read the cut again. */
  onGone?: () => void | Promise<void>;
}) {
  const [form, setForm] = useState<DraftForm>(() => seed?.form ?? blankForm());
  const [chosen, setChosen] = useState<Chosen | null>(() => chosenFrom(seed?.form ?? null));
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<Refusal | null>(null);
  // "Asked. … is under In progress below", and the draft it is about.
  const [asked, setAsked] = useState<{ text: string; draftId: string } | null>(null);
  // Said under the footage field: nothing matches what was typed.
  const [footageSaid, setFootageSaid] = useState<string | null>(null);
  // The cut `form.revises` names, once looked up, and what became of it.
  const [revised, setRevised] = useState<{ id: string; title: string } | null>(null);
  const [revisedSaid, setRevisedSaid] = useState<string | null>(null);
  const brief = useRef<HTMLTextAreaElement | null>(null);
  const footage = useRef<HTMLDivElement | null>(null);
  // Every folder the picker has offered, by key, for its counts. Added to,
  // never replaced: the list on screen is the newest search's, but an
  // earlier, slower one can answer after it, and replacing the map with
  // that answer left the rows on screen unknown — a click on one did nothing.
  const offered = useRef(new Map<string, CutFolder>());
  // What each search answered, for Enter in the field.
  const listed = useRef(new Map<string, CutFolder[]>());
  const typed = useRef("");
  typed.current = query;
  // Counts folders chosen, so the keyboard can follow each one to its pill.
  const [picked, setPicked] = useState(0);
  const go = useRef<HTMLButtonElement | null>(null);

  // A new seed replaces the form: "Try again" on a failed draft, or a link
  // that names a folder.
  const seedKey = seed?.key ?? 0;
  const lastSeed = useRef(seedKey);
  useEffect(() => {
    if (!seed || seed.key === lastSeed.current) return;
    lastSeed.current = seed.key;
    setForm(seed.form);
    setChosen(chosenFrom(seed.form));
    setQuery("");
    setRefusal(null);
    setAsked(null);
    setFootageSaid(null);
    setRevised(null);
    setRevisedSaid(null);
    if (seed.focus) {
      brief.current?.scrollIntoView({ block: "center" });
      brief.current?.focus({ preventScroll: true });
    }
    // Only the key decides; the seed object is new on every render of the page.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedKey]);

  // The draft named in "Asked." has finished, failed or been cancelled: the
  // page says how it ended, and the sentence promising it is not left
  // beside that (or beside "0 under way").
  useEffect(() => {
    if (askedFor === undefined) return;
    setAsked((before) => (before && before.draftId !== askedFor ? null : before));
  }, [askedFor]);

  // A folder named by a link or by Try again has no counts yet: look it up,
  // so the form can say how much of it is ready. Not found is not an error —
  // the server says what it finds when Draft is pressed.
  const chosenKey = chosen ? folderKey(chosen) : null;
  const chosenFound = chosen?.found ?? null;
  useEffect(() => {
    if (!chosen || chosenFound) return;
    let live = true;
    const want = { library_id: chosen.library_id, path: chosen.path };
    api
      .cutFolders(want.path, 50)
      .then((list) => {
        const match = list.find((row) => folderKey(row) === folderKey(want));
        if (live && match) setChosen({ ...want, found: match });
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chosenKey, chosenFound]);

  // On the Cuts list no cut is on screen, so a form that would add a version
  // to one says which, and finds out first whether it is still there: a cut
  // id is kept on a draft after the cut is deleted, and every press of Draft
  // was then refused with "No such cut." in a form that named no cut.
  const lookUp = revises === undefined ? form.revises : null;
  useEffect(() => {
    if (!lookUp) return;
    let live = true;
    api
      .cut(lookUp)
      .then((detail) => {
        if (live) setRevised({ id: lookUp, title: detail.title || "Untitled cut" });
      })
      .catch((err: unknown) => {
        // Only "it is gone" changes anything; unreachable is said when Draft is pressed.
        if (!live || !(err instanceof ApiError) || err.status !== 404) return;
        setForm((before) => (before.revises === lookUp ? { ...before, revises: null } : before));
        setRevisedSaid(REVISED_GONE);
      });
    return () => {
      live = false;
    };
  }, [lookUp]);

  const fetchFolders = useCallback(async (q: string): Promise<Suggestion[]> => {
    const wanted = q.trim();
    const list = await api.cutFolders(wanted);
    for (const row of list) offered.current.set(folderKey(row), row);
    listed.current.set(wanted, list);
    return list.map((row) => ({ id: folderKey(row), label: folderLabel(row), hint: folderHint(row) }));
  }, []);

  function update(change: Partial<DraftForm>) {
    setForm((before) => ({ ...before, ...change }));
    setRefusal(null);
    setAsked(null);
  }

  /** A folder takes the place of whatever footage the form held. */
  function choose(place: { library_id: string; path: string }, found: CutFolder | null) {
    setChosen({ library_id: place.library_id, path: place.path, found });
    update({
      folder: { library_id: place.library_id, path: place.path },
      assetIds: null,
      matching: null,
      footageLabel: null,
    });
    setQuery("");
    setFootageSaid(null);
    setPicked((count) => count + 1);
  }

  // The field picked from is replaced by the folder's pill; without this the
  // keyboard falls to the top of the page. Done once the pill is drawn: a
  // pick by Enter comes back from a request, and a timer set then ran
  // before the pill was there.
  useEffect(() => {
    if (picked) footage.current?.querySelector<HTMLElement>(".cutform-unpick")?.focus();
  }, [picked]);

  function pick(suggestion: Suggestion) {
    // The row's own key says which folder it is; what the picker said of it
    // (its counts) is used when it is known and looked up when it is not.
    const row = offered.current.get(suggestion.id) ?? null;
    const place = row ?? folderFromKey(suggestion.id);
    if (place) choose(place, row);
  }

  function unpick() {
    setChosen(null);
    update({ folder: null, assetIds: null, matching: null, footageLabel: null });
    // The pill and its × are gone: the field that replaces them takes the
    // keyboard, so the next thing typed lands in it.
    window.setTimeout(() => footage.current?.querySelector("input")?.focus(), 0);
  }

  /**
   * Enter in the footage field with no row highlighted: the first folder
   * that matches what was typed. Asked of the server for exactly those
   * letters rather than read off the list on screen, which may still be an
   * earlier search's while the typist is ahead of it.
   */
  async function pickTyped() {
    const wanted = query.trim();
    if (!wanted) return;
    let rows = listed.current.get(wanted);
    if (!rows) {
      try {
        rows = await api.cutFolders(wanted);
      } catch {
        setFootageSaid("FrameFound could not be reached to look for that folder. Try again.");
        return;
      }
      for (const row of rows) offered.current.set(folderKey(row), row);
      listed.current.set(wanted, rows);
    }
    // Typed on since: that search is not this one's to answer.
    if (typed.current.trim() !== wanted) return;
    const first = rows[0];
    if (first) choose(first, first);
    else setFootageSaid(`No folder of video matches “${wanted}”.`);
  }

  function onFootageKey(event: React.KeyboardEvent<HTMLDivElement>) {
    // The picker handles Enter itself only on a highlighted row; otherwise
    // it lets the form submit, which elsewhere is how a new name is made.
    // There is no new folder to make here: Enter after typing a name used
    // to answer "Choose a folder of footage first." with the folder listed.
    if (event.key !== "Enter" || event.defaultPrevented || event.nativeEvent.isComposing) return;
    if (!(event.target instanceof HTMLInputElement)) return;
    event.preventDefault();
    if (!event.repeat) void pickTyped();
  }

  // The clips the page held have gone because their x was pressed: the
  // field that takes their place gets the keyboard, as it does after a
  // folder's x. Done when the page has let them go (it takes them out of
  // its address, which lands a moment after the press), and not when they
  // go for another reason — after Draft the keyboard belongs to the button.
  const toField = useRef(false);
  const selected = Boolean(selection);
  useEffect(() => {
    if (selected || !toField.current) return;
    toField.current = false;
    footage.current?.querySelector("input")?.focus();
  }, [selected]);

  function unselect() {
    toField.current = true;
    setRefusal(null);
    setAsked(null);
    onUnselect?.();
  }

  function newCut() {
    update({ revises: null });
    setRevisedSaid("The result will be a new cut.");
    // The button pressed goes with the line it was on.
    window.setTimeout(() => brief.current?.focus({ preventScroll: true }), 0);
  }

  const line = editorLine(editor);
  // Footage the form itself holds that is not a picked folder ("Try again").
  const held = selection ? null : footageWords(form);
  const readiness = !selection && !held && chosen?.found ? folderReadiness(chosen.found) : null;
  const blocked = !canDraft || Boolean(editor.why_not) || Boolean(readiness?.blocks);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy || blocked) return;
    const asking: DraftForm = {
      ...form,
      ...(selection ? { assetIds: selection.assetIds, folder: null, matching: null } : {}),
      ...(revises !== undefined ? { revises } : {}),
    };
    const made = draftBody(asking);
    if ("problem" in made) {
      setRefusal({ text: made.problem, link: null });
      return;
    }
    setBusy(true);
    setRefusal(null);
    setAsked(null);
    try {
      const draft = await api.draftCut(made.body);
      setAsked({
        text: `Asked. “${draft.title}” is under In progress below; you can leave this page.`,
        draftId: draft.id,
      });
      setForm({ ...blankForm(), folder: form.folder });
      setRevisedSaid(null);
      await onDrafted(draft);
    } catch (err) {
      if (err instanceof ApiError && revisedIsGone(err.status, asking.revises)) {
        // The server's "No such cut." names nothing the form shows.
        if (revises === undefined) {
          setForm((before) => ({ ...before, revises: null }));
          setRevisedSaid(REVISED_GONE);
          setRefusal({ text: `${REVISED_GONE} Nothing was asked: press Draft to ask for it.`, link: null });
        } else {
          setRefusal({
            text:
              "The version this was to follow has just been deleted, so nothing was asked. " +
              "Press Draft again once the page has caught up.",
            link: null,
          });
          void onGone?.();
        }
      } else if (err instanceof ApiError) {
        setRefusal({ text: err.message, link: refusalLink(err.status, err.message) });
      } else {
        setRefusal({
          text: "FrameFound could not be reached, so nothing was asked. Try again.",
          link: null,
        });
      }
    } finally {
      setBusy(false);
      // Draft is switched off while it asks, which drops the keyboard to the
      // top of the page; it comes back to the button, beside the sentence
      // that says what happened. (On a cut's page the form has gone by now,
      // and that page sends the keyboard on itself.)
      window.setTimeout(() => focusIfLost(go.current), 50);
    }
  }

  const explanation = (
    <p className="faint cutform-why">
      The Editor is a model running on this Mac. It reads your brief and a line describing each
      shot — it sees no pictures, and nothing leaves the building. What comes back is a draft:
      FrameFound checks every in and out point, and nothing reaches Premiere until you download
      it.
    </p>
  );

  if (readOnly) {
    return (
      <div className="card cutform">
        {explanation}
        <p className="faint" style={{ margin: "8px 0 0" }}>
          This account can look but not change anything.
        </p>
      </div>
    );
  }

  const lengthNumber = Number(form.lengthText);

  return (
    <form className="card cutform" onSubmit={submit} aria-label="Draft a cut">
      {revises === undefined && (
        // Always in the form (empty, it takes no room), so the line is read
        // out when Try again fills the form in and when it changes.
        <p className="cutform-revises" role="status">
          {form.revises ? (
            <>
              {revisesLine(revised?.id === form.revises ? revised.title : null)}{" "}
              <button type="button" className="btn" onClick={newCut}>
                Make it a new cut instead
              </button>
            </>
          ) : (
            revisedSaid
          )}
        </p>
      )}
      <label className="field" style={{ marginTop: 0 }}>
        <span>What should the video be?</span>
        <textarea
          ref={brief}
          className="input cutform-brief"
          rows={3}
          maxLength={BRIEF_MAX}
          value={form.brief}
          placeholder="Thirty seconds of smooth aerials at golden hour, opening on the clubhouse."
          onChange={(event) => update({ brief: event.target.value })}
        />
      </label>

      <div className="field" ref={footage} onKeyDown={onFootageKey}>
        <span id="cutform-footage">Footage</span>
        {selection ? (
          <>
            {onUnselect ? (
              <div className="cutform-chosen">
                <span className="pill cutform-folder">
                  The {selection.label}
                  <button
                    type="button"
                    className="cutform-unpick"
                    aria-label="Choose a folder instead"
                    title="Choose a folder instead (Back brings these clips back)"
                    onClick={unselect}
                  >
                    ×
                  </button>
                </span>
              </div>
            ) : (
              <p style={{ margin: 0 }}>The {selection.label}</p>
            )}
            <ClipList assetIds={selection.assetIds} />
            {selection.note && <p className="faint cutform-note">{selection.note}</p>}
          </>
        ) : held ? (
          <>
            <div className="cutform-chosen">
              <span className="pill cutform-folder">
                {held}
                <button
                  type="button"
                  className="cutform-unpick"
                  aria-label="Choose a folder instead"
                  title="Choose a folder instead"
                  onClick={unpick}
                >
                  ×
                </button>
              </span>
            </div>
            {form.assetIds && form.assetIds.length > 0 && <ClipList assetIds={form.assetIds} />}
            {form.matching && (
              <p className="faint cutform-note">
                Every folder with these letters in its name, as the draft was asked for.
              </p>
            )}
          </>
        ) : chosen ? (
          <div className="cutform-chosen">
            <span className="pill cutform-folder">
              {chosen.found ? folderLabel(chosen.found) : chosen.path}
              {chosen.found && <small className="faint">{folderHint(chosen.found)}</small>}
              <button
                type="button"
                className="cutform-unpick"
                aria-label="Choose another folder"
                onClick={unpick}
              >
                ×
              </button>
            </span>
          </div>
        ) : (
          <div>
            <Autocomplete
              value={query}
              onChange={(next) => {
                setQuery(next);
                setFootageSaid(null);
              }}
              onPick={pick}
              fetcher={fetchFolders}
              minChars={0}
              placeholder="Type part of a folder's name — or pick one shot recently"
              ariaLabel="Footage: a folder of video"
            />
            {footageSaid && (
              <p className="cutform-note" role="alert">
                {footageSaid}
              </p>
            )}
          </div>
        )}
        {readiness && (
          <p className={readiness.blocks ? "cutform-note" : "faint cutform-note"} role={readiness.blocks ? "alert" : undefined}>
            {readiness.text}
            {readiness.blocks && (
              <>
                {" "}
                <Link href="/processing">Processing →</Link>
              </>
            )}
          </p>
        )}
      </div>

      <fieldset className="field cutform-lengths">
        <legend>Length</legend>
        <div className="hintrow" style={{ marginTop: 0 }}>
          {LENGTHS.map((seconds) => (
            <button
              key={seconds}
              type="button"
              className="hint"
              aria-pressed={lengthNumber === seconds}
              onClick={() => update({ lengthText: String(seconds) })}
            >
              {seconds} s
            </button>
          ))}
          <label className="cutform-seconds">
            <span>or</span>
            <input
              className="input"
              type="number"
              inputMode="decimal"
              min={5}
              max={600}
              step="any"
              value={form.lengthText}
              aria-label="Length in seconds"
              onChange={(event) => update({ lengthText: event.target.value })}
            />
            <span>seconds</span>
          </label>
        </div>
      </fieldset>

      <details className="disclosure cutform-more">
        <summary>Shot length and used footage</summary>
        <div className="cutform-pair">
          <label className="field">
            <span>Shortest shot (seconds)</span>
            <input
              className="input"
              type="number"
              inputMode="decimal"
              min={0.5}
              max={30}
              step="any"
              value={form.shortestText}
              onChange={(event) => update({ shortestText: event.target.value })}
            />
          </label>
          <label className="field">
            <span>Longest shot (seconds)</span>
            <input
              className="input"
              type="number"
              inputMode="decimal"
              min={1}
              max={60}
              step="any"
              value={form.longestText}
              onChange={(event) => update({ longestText: event.target.value })}
            />
          </label>
        </div>
        <label className="field" data-layout="row">
          <input
            type="checkbox"
            checked={form.excludeUsed}
            onChange={(event) => update({ excludeUsed: event.target.checked })}
          />
          <span>Leave out shots an earlier video already used</span>
        </label>
      </details>

      <label className="field">
        <span>Title</span>
        <input
          className="input"
          maxLength={TITLE_MAX}
          value={form.title}
          placeholder="Named after the brief if left empty"
          onChange={(event) => update({ title: event.target.value })}
        />
      </label>

      {line.tone === "alert" ? (
        <div className="card cutform-editor" role="alert" style={{ borderColor: "var(--ember)" }}>
          {line.text} <Link href="/models#roles">AI models →</Link>
        </div>
      ) : (
        <p className={line.tone === "ok" ? "faint cutform-editor" : "cutform-editor"}>
          {line.pill && (
            <span className="pill" data-tone="warn">
              {line.pill}
            </span>
          )}{" "}
          {line.text}
          {line.tone === "wait" && (
            <>
              {" "}
              <Link href={editor.paused ? "/models#start" : "/models#sharing"}>AI models →</Link>
            </>
          )}
        </p>
      )}

      {refusal && (
        <div className="card cutform-refusal" role="alert">
          {refusal.text}
          {refusal.link && (
            <>
              {" "}
              <Link href={refusal.link.href}>{refusal.link.label} →</Link>
            </>
          )}
        </div>
      )}
      {asked && (
        <p className="cutform-asked" role="status">
          {asked.text}
        </p>
      )}

      <div className="cutform-go">
        <button ref={go} type="submit" className="btn btn-primary" disabled={busy || blocked}>
          {busy ? "Asking…" : "Draft"}
        </button>
      </div>
      {explanation}
    </form>
  );
}

/**
 * The clips a form holds by id, by name, each linking to its own page (the
 * same tab: Back brings the form back). Names are read with the clip's own
 * page's request, for the first `CLIPS_NAMED` only; the rest are counted.
 */
function ClipList({ assetIds }: { assetIds: string[] }) {
  const [names, setNames] = useState<ReadonlyMap<string, string | null>>(() => new Map());
  const wanted = assetIds.slice(0, CLIPS_NAMED).join(",");
  useEffect(() => {
    let live = true;
    for (const id of wanted ? wanted.split(",") : []) {
      api
        .asset(id)
        .then(
          (detail) => detail.filename,
          () => null,
        )
        .then((name) => {
          if (live) setNames((before) => new Map(before).set(id, name));
        });
    }
    return () => {
      live = false;
    };
  }, [wanted]);
  const { rows, more } = clipRows(assetIds, names);
  return (
    <ul className="cutform-clips" aria-label="The clips">
      {rows.map((row) => (
        <li key={row.id}>
          <Link href={row.href} className={row.named ? "mono" : "faint"} title="Open this clip's own page">
            {row.label}
          </Link>
        </li>
      ))}
      {more && <li className="faint">{more}</li>}
    </ul>
  );
}

function chosenFrom(form: DraftForm | null): Chosen | null {
  if (!form?.folder) return null;
  return { library_id: form.folder.library_id, path: form.folder.path, found: null };
}
