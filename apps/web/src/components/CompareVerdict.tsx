"use client";

// "What did you decide?" — the owner's conclusion, recorded on the
// comparison so the next step is built on something measured.
//
// One radio per column (in the order added, never by a figure), "None is
// good enough yet" and "Not decided", and a note. Save sends only what
// changed. Saving changes nothing else: a model becomes the Looker only by
// the separate button, which asks first and goes through AI models' own
// role route, whose sentence is shown when it says no. When that route asks
// a question of its own (a llama.cpp model whose column has not finished
// with answers has shown nobody how it looks), the question is put to the
// owner exactly as the AI models page puts it, and a yes sends it again.

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { ApiError, api, type Comparison } from "@/lib/api";
import {
  NONE_GOOD,
  NOT_DECIDED,
  choiceOf,
  decidedLine,
  lookerOffer,
  lookerQuestion,
  verdictChange,
  verdictWords,
  type VerdictChoice,
} from "@/lib/compare";
import { DECLINED, NOTHING_CHANGED, focusIfLost, roleGivenSentence, sendAsking } from "@/lib/localModels";

import type { CompareAct } from "./CompareMeasures";

const NOTE_CHARS = 2000;

/** What is said after "Make it the Looker", and what follows the sentence:
 *  the way to the copy on AI models, or to the column the question was about. */
type Said = { text: string; tone: "status" | "alert"; then: "copy" | "column" | null };

/**
 * Models as a role's sentence takes them, named as Compare names them. A
 * column's name and the Compare overview's already carry the build
 * ("demo-vision-gguf Q4_K_M"); the role route names a model after its
 * repository alone, so "demo-vision-gguf is still the Looker" could be either
 * build. No build is handed on beside the name: the sentence would say it
 * twice.
 */
function named(models: readonly { id: string | null; name: string }[]) {
  return models.flatMap((one) => (one.id ? [{ id: one.id, name: one.name, variant: "", variant_label: "" }] : []));
}

export default function CompareVerdict({
  comparison,
  act,
  busy,
}: {
  comparison: Comparison;
  act: CompareAct;
  busy: boolean;
}) {
  const saved = comparison.verdict;
  const savedChoice = choiceOf(saved);
  const [choice, setChoice] = useState<VerdictChoice>(savedChoice);
  const [note, setNote] = useState(saved.note);
  // What the form was last filled from. A newer saved decision (another tab,
  // a column taken out) refills it only while the owner has not changed it.
  const filledFrom = useRef({ choice: savedChoice, note: saved.note });
  const [looker, setLooker] = useState<Said | null>(null);

  // "Make it the Looker" is switched off while its request is out, and a
  // switched-off button gives the keyboard up; once the role is given the
  // button is not drawn at all. When the press has settled, focus goes back
  // to the button, or to what was said where the button has gone, so the
  // next Tab reaches "See its column" and not the top of the page.
  const lookerButton = useRef<HTMLButtonElement | null>(null);
  const lookerSaid = useRef<HTMLDivElement | null>(null);
  const refocus = useRef(false);
  useEffect(() => {
    if (busy || !refocus.current) return;
    refocus.current = false;
    focusIfLost(lookerButton.current ?? lookerSaid.current);
  }, [busy]);

  useEffect(() => {
    const before = filledFrom.current;
    if (before.choice === savedChoice && before.note === saved.note) return;
    setChoice((now) => (now === before.choice ? savedChoice : now));
    setNote((now) => (now === before.note ? saved.note : now));
    filledFrom.current = { choice: savedChoice, note: saved.note };
  }, [savedChoice, saved.note]);

  const change = verdictChange(choice, note, saved);
  const tooLong = note.length > NOTE_CHARS;
  const decided = decidedLine(saved);
  const offer = comparison.can_manage ? lookerOffer(comparison) : null;

  async function save() {
    if (!change || tooLong) return;
    await act(() => api.updateComparison(comparison.id, change), "Saved.");
  }

  async function makeLooker() {
    if (!offer?.model_id) return;
    // Who has the role now, for the question: the Compare overview lists
    // every installed model with the roles it holds. The same read names the
    // models for the sentence afterwards; this comparison's own columns stand
    // in for it when it cannot be read.
    let current: string | null = null;
    let names = named(comparison.entries.map((entry) => ({ id: entry.model_id, name: entry.name })));
    try {
      const overview = await api.compareOverview();
      current = overview.models.find((model) => model.roles.includes("looker") && model.id !== offer.model_id)?.name ?? null;
      names = [...named(overview.models), ...names];
    } catch {
      // The question is asked without the other name; the role route itself
      // says no in its own words if anything is wrong.
    }
    if (!window.confirm(lookerQuestion(offer.name, current))) return;
    setLooker(null);
    refocus.current = true;
    const modelId = offer.model_id;
    await act(async () => {
      try {
        const answer = await sendAsking(
          (yes) => api.setModelRole("looker", modelId, yes),
          (question) => window.confirm(question),
        );
        if (answer === DECLINED) {
          // Only a column that finished with answers stops the question, and
          // this model's column is on this page.
          setLooker({
            text: `${NOTHING_CHANGED} FrameFound stops asking once ${offer.name} has finished its column here with answers.`,
            tone: "status",
            then: "column",
          });
          return;
        }
        setLooker({
          text: roleGivenSentence(answer, offer.name, names),
          tone: "status",
          then: answer.pending_model_id ? "copy" : null,
        });
      } catch (err) {
        setLooker({
          text: err instanceof ApiError ? err.message : "FrameFound could not be reached. Try again.",
          tone: "alert",
          then: null,
        });
      }
    });
  }

  return (
    <div className="card compare-verdict">
      <p className="muted" style={{ marginTop: 0 }}>
        The figures say how far each model agrees with decisions you already made; the answers
        show what it would write. Record what you concluded, so the next step is built on
        something measured.
      </p>

      {comparison.can_manage ? (
        <>
          <fieldset className="compare-verdict-choices">
            <legend className="compare-label-strong">Your decision</legend>
            {comparison.entries.map((entry) => (
              <label key={entry.id} className="compare-tick">
                <input
                  type="radio"
                  name="compare-verdict"
                  value={`entry:${entry.id}`}
                  checked={choice === `entry:${entry.id}`}
                  disabled={busy}
                  onChange={() => setChoice(`entry:${entry.id}`)}
                />
                <span>
                  <strong>{entry.name}</strong> looks
                  {!entry.installed && <span className="faint"> (no longer installed)</span>}
                </span>
              </label>
            ))}
            <label className="compare-tick">
              <input
                type="radio"
                name="compare-verdict"
                value="none"
                checked={choice === "none"}
                disabled={busy}
                onChange={() => setChoice("none")}
              />
              <span>{NONE_GOOD}</span>
            </label>
            <label className="compare-tick">
              <input
                type="radio"
                name="compare-verdict"
                value="open"
                checked={choice === "open"}
                disabled={busy}
                onChange={() => setChoice("open")}
              />
              <span>{NOT_DECIDED}</span>
            </label>
          </fieldset>

          <label className="compare-note">
            <span className="compare-label-strong">Why — in your own words</span>
            <textarea
              className="input"
              rows={3}
              value={note}
              disabled={busy}
              aria-describedby={tooLong ? "compare-note-long" : undefined}
              onChange={(event) => setNote(event.target.value)}
            />
          </label>
          {tooLong && (
            <p id="compare-note-long" className="compare-said" data-tone="bad">
              Keep the note under 2,000 characters.
            </p>
          )}

          <div className="compare-start">
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || !change || tooLong}
              title={!change ? "Nothing has changed since it was saved." : undefined}
              onClick={() => void save()}
            >
              Save
            </button>
            {decided && <span className="faint">{decided}</span>}
          </div>
        </>
      ) : (
        <div>
          <p style={{ margin: 0 }}>
            <strong>{verdictWords(comparison)}</strong>
            {decided && <span className="faint"> — {decided}</span>}
          </p>
          {saved.note && <p className="compare-verdict-note">{saved.note}</p>}
        </div>
      )}

      {offer && (
        <div className="compare-start">
          <button
            ref={lookerButton}
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => void makeLooker()}
          >
            Make it the Looker
          </button>
          <span className="faint">{offer.name} is not the Looker now.</span>
        </div>
      )}
      {looker && (
        <div
          ref={lookerSaid}
          className="card"
          role={looker.tone}
          style={{ borderColor: looker.tone === "alert" ? "var(--ember)" : "var(--sage)", margin: "12px 0 0" }}
        >
          {looker.text}
          {looker.then === "copy" && (
            <>
              {" "}
              <Link href="/models#roles" className="compare-link">
                Follow the copy on AI models
              </Link>
            </>
          )}
          {looker.then === "column" && (
            <>
              {" "}
              <a href="#figures" className="compare-link">
                See its column
              </a>
            </>
          )}
        </div>
      )}

      <p className="faint" style={{ margin: "12px 0 0" }}>
        Saving a decision changes nothing by itself.
      </p>
    </div>
  );
}
