"use client";

// The figures of one comparison, side by side: a label column and one column
// per model, in the order the models were added. It scrolls sideways when
// the models do not fit; under 900 px each model is its own card with the
// labels repeated (the CSS does that from the same markup).
//
// No column is marked best and none is ordered by a figure. Every figure is
// a count of agreement with what a person already decided, said in words;
// every state is the server's own sentence. The buttons are the column's
// ways out — Cancel, Carry on, Try again, Try the failed ones again, Take it
// out — each shown only when the server says it can be pressed.

import Link from "next/link";

import { api, type Comparison, type ComparisonEntry } from "@/lib/api";
import {
  buildLine,
  fitsPill,
  formSentence,
  FIGURES_LATER,
  memorySentence,
  overNote,
  removeQuestion,
  roleWords,
  scoreSentences,
  statusOf,
  tagSentences,
  timeSentences,
} from "@/lib/compare";
import { INSTALL_PROGRAM, columnLink } from "@/lib/localModelsCompare";

export type CompareAct = (work: () => Promise<unknown>, done?: string | null) => Promise<boolean>;

/**
 * The mark on a demonstration model's name, figures and answers (design 8.3:
 * "a demo pill on every model name, every figure and every answer"). The
 * banner at the top of the page scrolls away and the viewer covers it; this
 * goes wherever the made-up output does.
 */
export function DemoPill() {
  return (
    <span className="pill compare-demo" data-tone="warn" title="A demonstration model: nothing it says is real.">
      demo
    </span>
  );
}

const ROWS = [
  "Model",
  "Status",
  "Answered in the form asked",
  "Tags you had decided",
  "Scores against the cuts",
  "Time",
  "Memory",
] as const;

export default function CompareMeasures({
  comparison,
  act,
  busy,
}: {
  comparison: Comparison;
  act: CompareAct;
  busy: boolean;
}) {
  const entries = comparison.entries;
  if (!entries.length) {
    return (
      <div className="empty">
        No model is in this comparison any more.
        {comparison.can_add_models ? " Add one above to compare it on this sample." : ""}
      </div>
    );
  }
  return (
    <>
      <div className="tablewrap compare-figures">
        <div
          className="comparecols"
          style={{ ["--compare-models" as string]: entries.length }}
          aria-label="Figures for each model, in the order they were added"
        >
          <div className="comparecol comparecol-labels" aria-hidden="true">
            {ROWS.map((label, row) => (
              <div key={label} className="comparecell comparecell-label" data-row={row}>
                {row === 0 ? "" : label}
              </div>
            ))}
          </div>
          {entries.map((entry) => (
            <Column key={entry.id} comparison={comparison} entry={entry} act={act} busy={busy} />
          ))}
        </div>
      </div>
      {overNote(comparison).map((line) => (
        <p key={line} className="faint compare-overnote">
          {line}
        </p>
      ))}
    </>
  );
}

function Column({
  comparison,
  entry,
  act,
  busy,
}: {
  comparison: Comparison;
  entry: ComparisonEntry;
  act: CompareAct;
  busy: boolean;
}) {
  const done = entry.state === "done";
  const status = statusOf(entry);
  const tags = tagSentences(entry.measures.tags, {
    done,
    truth: comparison.counts.footage_with_tags > 0,
  });
  const scores = scoreSentences(entry.measures.scores, {
    done,
    what: comparison.what,
    used: comparison.counts.used,
    unused: comparison.counts.unused,
  });
  const fits = entry.installed ? fitsPill(entry.fits) : null;
  // Stopped by its program: the name leads to where that is installed.
  const nameLink = columnLink(entry);
  const id = comparison.id;
  // Every figure cell carries the cell's own label (seen under 900 px) and,
  // for a demonstration model, the demo mark.
  const label = (text: string) => (
    <>
      <span className="comparecell-name">{text}</span>
      {entry.demo && text !== "Status" && <DemoPill />}
    </>
  );

  return (
    <div className="comparecol" role="group" aria-labelledby={`compare-col-${entry.id}`}>
      <div className="comparecell comparecell-head" data-row={0}>
        <h3 id={`compare-col-${entry.id}`} className="comparecol-name">
          {entry.installed ? (
            <Link
              href={nameLink}
              className="compare-link"
              title={
                nameLink === INSTALL_PROGRAM.href
                  ? "AI models › Software that runs models, to install its program"
                  : "Its row on AI models"
              }
            >
              {entry.name}
            </Link>
          ) : (
            entry.name
          )}
        </h3>
        <div className="faint mono compare-build">{buildLine(entry)}</div>
        <div className="compare-model-pills">
          {roleWords(entry.roles).map((role) => (
            <span key={role} className="pill" data-tone="ok" title={`It is the ${role} now.`}>
              {role}
            </span>
          ))}
          {!entry.installed && (
            <span className="pill" title="Its answers and figures are kept; it cannot be carried on.">
              no longer installed
            </span>
          )}
          {entry.demo && <DemoPill />}
        </div>
      </div>

      <div className="comparecell" data-row={1}>
        {label("Status")}
        {status.progress && (
          <div
            className="bar"
            role="progressbar"
            aria-label={`${entry.name}: items asked`}
            aria-valuemin={0}
            aria-valuemax={status.progress.max}
            aria-valuenow={status.progress.value}
            aria-valuetext={status.text}
          >
            <i style={{ width: `${status.progress.percent}%` }} />
          </div>
        )}
        <p className="compare-said" data-tone={status.tone === "ok" ? undefined : (status.tone ?? undefined)}>
          {status.text}
        </p>
        {status.sofar && <p className="compare-said faint">{status.sofar}</p>}
        {entry.state === "failed" && entry.detail && (
          <details className="disclosure compare-detail">
            <summary>Details</summary>
            <pre className="pathbox models-lastlines">{entry.detail}</pre>
          </details>
        )}
        <div className="compare-buttons">
          {entry.can_cancel && (
            <button
              type="button"
              className="btn"
              disabled={busy}
              aria-label={`Cancel ${entry.name}`}
              onClick={() => act(() => api.cancelComparisonModel(id, entry.id))}
            >
              Cancel
            </button>
          )}
          {entry.can_carry_on && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              aria-label={`${entry.state === "failed" ? "Try again" : "Carry on"}: ${entry.name}`}
              onClick={() => act(() => api.runComparisonModel(id, entry.id, false))}
            >
              {entry.state === "failed" ? "Try again" : "Carry on"}
            </button>
          )}
          {entry.can_remove && (
            <button
              type="button"
              className="btn compare-remove"
              disabled={busy}
              aria-label={`Take ${entry.name} out of this comparison`}
              onClick={() => {
                if (!window.confirm(removeQuestion(entry, comparison.verdict))) return;
                void act(
                  () => api.removeComparisonModel(id, entry.id),
                  `${entry.name} was taken out of this comparison.`,
                );
              }}
            >
              Take it out of this comparison
            </button>
          )}
        </div>
      </div>

      <div className="comparecell" data-row={2}>
        {label("Answered in the form asked")}
        <p className="compare-said">{formSentence(entry.measures.form)}</p>
        {entry.can_retry_failed && (
          <div className="compare-buttons">
            <button
              type="button"
              className="btn"
              disabled={busy}
              aria-label={`Try the failed ones again: ${entry.name}`}
              onClick={() => act(() => api.runComparisonModel(id, entry.id, true))}
            >
              Try the failed ones again
            </button>
          </div>
        )}
      </div>

      <div className="comparecell" data-row={3}>
        {label("Tags you had decided")}
        {tags ? (
          <>
            {tags.lines.map((line) => (
              <p key={line} className="compare-said">
                {line}
              </p>
            ))}
            {tags.surer.length > 0 && (
              <details className="disclosure compare-detail">
                <summary>Only the ones it was surer of</summary>
                {tags.surer.map((floor) => (
                  <div key={floor.label} className="compare-floor">
                    <strong>{floor.label}</strong>
                    {floor.lines.map((line) => (
                      <p key={line} className="compare-said">
                        {line}
                      </p>
                    ))}
                  </div>
                ))}
              </details>
            )}
          </>
        ) : (
          <p className="compare-said faint">{FIGURES_LATER}</p>
        )}
      </div>

      <div className="comparecell" data-row={4}>
        {label("Scores against the cuts")}
        {scores ? (
          scores.map((line) => (
            <p key={line} className="compare-said">
              {line}
            </p>
          ))
        ) : (
          <p className="compare-said faint">{FIGURES_LATER}</p>
        )}
      </div>

      <div className="comparecell" data-row={5}>
        {label("Time")}
        {timeSentences(entry, entry.measures).map((line) => (
          <p key={line} className="compare-said">
            {line}
          </p>
        ))}
      </div>

      <div className="comparecell" data-row={6}>
        {label("Memory")}
        <p className="compare-said">{memorySentence(entry)}</p>
        {fits && (
          <div className="compare-model-pills">
            <span className="pill" data-tone={fits.tone ?? undefined} title={fits.title}>
              {fits.label}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
