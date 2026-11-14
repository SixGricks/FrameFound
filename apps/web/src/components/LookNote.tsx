"use client";

// What a local model said of a photograph or of one shot: its score, its
// caption, why it scored it so, and who said it.
//
// It is an opinion and every part of it is labelled as one. The score is
// "AI 82", never a bare number beside a file; the last line names the model,
// the build that answered, the program that ran it and the wording of the
// question, so an answer from last month's build is not read as this
// month's; and a look the demonstration invented carries a "made up" pill,
// because a made-up caption under a real photograph reads as a real one.
//
// The caption and the reason are a model's text. They are rendered as text
// and nothing else: never as markup, never as part of an address.

import type { AssetLook } from "@/lib/api";
import {
  MADE_UP_NOTE,
  OPINION_NOTE,
  lookSaidBy,
  scoreLabel,
  scoreTone,
  utcMoment,
} from "@/lib/looks";

/**
 * A local model's 0–100 score as the pill every page shows ("AI 82"), read
 * out as "AI score 82 of 100". Renders nothing without a score: a file
 * nothing has looked at, a look that gave none, a server from before looks.
 */
export function ScorePill({ score }: { score: number | null | undefined }) {
  if (typeof score !== "number" || !Number.isFinite(score)) return null;
  return (
    <span
      className="scorepill"
      data-tone={scoreTone(score)}
      role="img"
      aria-label={scoreLabel(score)}
      title={`${scoreLabel(score)} — a local model's opinion of how usable this is`}
    >
      AI {score}
    </span>
  );
}

/** Beside a look the demonstration invented. */
export function MadeUpPill() {
  return (
    <span className="pill" data-tone="warn" title={MADE_UP_NOTE}>
      made up
    </span>
  );
}

/** The day a look was made, short: "12 Nov", with the year once it is not
 *  this one. Empty when the server's time cannot be read. */
function dayOf(lookedAt: string): string {
  const at = utcMoment(lookedAt);
  if (Number.isNaN(at)) return "";
  const when = new Date(at);
  const thisYear = when.getFullYear() === new Date().getFullYear();
  return when.toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
    ...(thisYear ? {} : { year: "numeric" }),
  });
}

export default function LookNote({ look }: { look: AssetLook | null }) {
  if (!look) return null;
  return (
    <div className="looknote">
      {(look.score !== null || look.demo) && (
        <div className="looknote-top">
          <ScorePill score={look.score} />
          {look.demo && <MadeUpPill />}
        </div>
      )}
      {look.caption && <p className="looknote-caption">{look.caption}</p>}
      {look.reason && <p className="looknote-reason muted">{look.reason}</p>}
      <p
        className="looknote-said faint mono"
        title={`${look.model}${look.model_revision ? ` at ${look.model_revision}` : ""}`}
      >
        {lookSaidBy(look, dayOf(look.looked_at))}
      </p>
      <p className="looknote-opinion faint">{OPINION_NOTE}</p>
    </div>
  );
}
