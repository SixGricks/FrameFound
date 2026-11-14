"use client";

// One piece of local-AI work as a bar, a sentence and Cancel — the same on a
// model's row, a role card, the software row and Getting started.
//
// Everything shown comes from the job as the server last read it: the page
// keeps nothing about a run in the tab, so leaving and coming back finds the
// same bar. A job that moves no bytes (an install, a test) reports no total,
// and it gets a spinner and its own sentence rather than a bar that would
// have to be invented.

import { api, type LocalModelJob } from "@/lib/api";
import { jobPercent, jobSentence, type ModelsAct, type ModelsSectionId } from "@/lib/localModels";

export default function ModelsProgress({
  job,
  ahead = null,
  where,
  act,
  canManage,
  busy,
  sentence: said,
}: {
  job: LocalModelJob;
  /** The title of the job running now, for one waiting its turn. */
  ahead?: string | null;
  /** The section a refused Cancel is reported in. */
  where: ModelsSectionId;
  act: ModelsAct;
  canManage: boolean;
  busy: boolean;
  /** The line to show instead of the job's own (a test says what is most
   *  likely taking the time). */
  sentence?: string;
}) {
  const share = job.state === "running" ? jobPercent(job) : null;
  const moving = job.state === "running" || job.state === "queued" || job.cancelling;
  const sentence = said ?? jobSentence(job, ahead);
  return (
    <div className="models-progress">
      {share !== null && (
        <div
          className="bar"
          role="progressbar"
          aria-label={job.title}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={share}
          aria-valuetext={sentence}
        >
          <i style={{ width: `${share}%` }} />
        </div>
      )}
      <div className="models-progress-line">
        {share === null && moving && <span className="spinner" aria-hidden="true" />}
        <span className="faint">{sentence}</span>
        {canManage && job.can_cancel && !job.cancelling && (
          <button
            type="button"
            className="btn"
            style={{ padding: "3px 10px" }}
            aria-label={`Cancel: ${job.title}`}
            disabled={busy}
            onClick={() =>
              act(where, () => api.cancelModelJob(job.id), `${job.title}: cancelling.`)
            }
          >
            Cancel
          </button>
        )}
      </div>
    </div>
  );
}
