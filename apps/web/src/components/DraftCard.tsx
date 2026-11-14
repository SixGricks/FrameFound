"use client";

// One draft, as a sentence and its buttons. Every word of where it stands is
// the server's (`draft.message`), read again by the page that lists it; the
// card keeps nothing of its own, so it reads the same after a reload or on
// another computer.
//
// There is no progress bar: one model answer has no percentage, and the
// page does not invent one. What the Editor wrote when its answer could not
// be used is shown as text, never as HTML — a model's words are not markup.

import Link from "next/link";

import type { CutDraft } from "@/lib/api";
import { askedLine, draftSentence, isActive, offerLine } from "@/lib/cuts";

export default function DraftCard({
  draft,
  busy,
  canTryAgain,
  claudeProblem,
  onCancel,
  onClear,
  onTryAgain,
  onAskClaude,
}: {
  draft: CutDraft;
  busy: boolean;
  /** The account may ask for drafts (Try again only fills the form in). */
  canTryAgain: boolean;
  /** Why the chat panel would not answer, or null when it would. */
  claudeProblem: string | null;
  onCancel: (draft: CutDraft) => void;
  onClear: (draft: CutDraft) => void;
  onTryAgain: (draft: CutDraft) => void;
  onAskClaude: (draft: CutDraft) => void;
}) {
  const active = isActive(draft);
  const sentence = draftSentence(draft);
  const offer = active ? offerLine(draft) : null;
  const headingId = `draft-${draft.id}`;
  const ended = draft.state === "failed" || draft.state === "cancelled";

  return (
    <article className="card draftcard" data-state={draft.state} aria-labelledby={headingId}>
      <div className="draftcard-head">
        <h3 id={headingId}>{draft.title}</h3>
        {draft.demo && (
          <span className="pill" data-tone="warn">
            demo
          </span>
        )}
        {draft.origin === "chat" && <span className="pill">from the chat</span>}
      </div>
      <p className="faint mono draftcard-what">
        {draft.scope.label} · {askedLine(draft)}
        {draft.model_name ? ` · ${draft.model_name}` : ""}
        {draft.requested_by ? ` · asked by ${draft.requested_by}` : ""}
      </p>

      <p
        className="draftcard-says"
        data-tone={draft.state === "failed" ? "bad" : undefined}
        role={draft.state === "failed" ? undefined : "status"}
      >
        {draft.state === "running" && !draft.stopping && <span className="spinner" aria-hidden="true" />}
        {sentence}
        {draft.state === "waiting" && " You can leave this page."}
      </p>
      {offer && <p className="faint draftcard-offer">{offer}</p>}

      {draft.state === "failed" && draft.answer_text && (
        <details className="disclosure draftcard-more">
          <summary>What the Editor wrote</summary>
          <pre className="answerbox">{draft.answer_text}</pre>
        </details>
      )}
      {draft.problems.length > 0 && (
        <details className="disclosure draftcard-more">
          <summary>What the check found ({draft.problems.length})</summary>
          <ul className="draftcard-problems">
            {draft.problems.map((problem, index) => (
              <li key={index}>{problem}</li>
            ))}
          </ul>
        </details>
      )}

      <div className="toolbar draftcard-actions">
        {draft.state === "done" && draft.cut_id && (
          <Link className="btn btn-primary" href={`/cuts/${draft.cut_id}`}>
            Open the draft
          </Link>
        )}
        {draft.can_cancel && (
          <button type="button" className="btn" disabled={busy} onClick={() => onCancel(draft)}>
            Cancel
          </button>
        )}
        {draft.state === "waiting" && (
          // Where a pause is lifted, the window shared and the server's state
          // shown: whichever of those the sentence names, it is that page.
          <Link className="btn" href="/models">
            AI models
          </Link>
        )}
        {ended && canTryAgain && (
          <button type="button" className="btn" disabled={busy} onClick={() => onTryAgain(draft)}>
            Try again
          </button>
        )}
        {draft.state === "failed" && (
          <button
            type="button"
            className="btn"
            disabled={busy || claudeProblem !== null}
            title={claudeProblem ?? "Opens the chat panel with the brief and the footage written out. Nothing is sent until you press Send."}
            onClick={() => onAskClaude(draft)}
          >
            Ask Claude instead
          </button>
        )}
        {draft.can_clear && (
          <button type="button" className="btn" disabled={busy} onClick={() => onClear(draft)}>
            Clear
          </button>
        )}
      </div>
      {draft.state === "failed" && claudeProblem && <p className="faint draftcard-offer">{claudeProblem}</p>}
    </article>
  );
}
