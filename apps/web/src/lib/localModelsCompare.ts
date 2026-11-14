// The compare pages' way out for a model held back: where on the AI models
// page the thing that holds it back is put right. The sentence itself is
// always the server's (`reason`, or a column's `message`); this only adds
// the link beside it.

import type { CompareModelChoice, ComparisonEntry } from "./api";

export interface FixLink {
  href: string;
  label: string;
}

/** Where to install the program a model runs on. */
export const INSTALL_PROGRAM: FixLink = {
  href: "/models#software",
  label: "AI models › Software that runs models",
};

/**
 * The link shown after the reason a model cannot be ticked; null when there
 * is nothing on another page to do about it. A comparison runs the model
 * now, so one whose program is not installed is held back (the server
 * refuses it too) and sent to Software that runs models — before, it could
 * be ticked and its turn stopped at once.
 */
export function fixLink(model: Pick<CompareModelChoice, "can_compare" | "program_missing">): FixLink | null {
  if (model.can_compare) return null;
  return model.program_missing ? INSTALL_PROGRAM : null;
}

/** The sentences a model's turn stops with because of its program: what the
 *  server's `program_missing` (comparisons.missing_programs → ask._running,
 *  step 5) stands for, so a column and the builder send the owner to the
 *  same place. Word for word from apps/server/framefound/local_models:
 *  ask.py ("… is not installed. Install it under Software that runs models
 *  …", and the damaged version record it puts in place of a path),
 *  runtimes.py ("The version recorded as in use (…) is missing or
 *  incomplete on disk.") and kinds.py ("… is recorded but its program is
 *  missing. Install … again under Software that runs models …"). Held to
 *  those files by tests/models-e2-final-qa.test.mjs. */
const PROGRAM_SENTENCE =
  /Software that runs models|record of which version is in use is damaged|recorded as in use \(.*\) is missing or incomplete on disk/;

/**
 * Where a comparison column's model name links: its row on the AI models
 * page, or Software that runs models when what stopped it is its program
 * (removed after the comparison started). The column has no
 * `program_missing` of its own, so the server's sentence is what says so.
 */
export function columnLink(entry: Pick<ComparisonEntry, "message">): string {
  return PROGRAM_SENTENCE.test(entry.message) ? INSTALL_PROGRAM.href : "/models#models";
}
