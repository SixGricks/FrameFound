"use client";

// Recent activity: the models' own work list — everything under way, then
// the last fifteen that ended — with what each was, how it ended, who asked
// for it and when.
//
// Each kind of work this page knows is shown and cancelled in its own place
// (a download on its model's row, a copy on its role card, an install on the
// software row, a test in its panel); here it is only listed, so there is one
// Cancel for it, not two. Work of a kind the page has no place for yet (a
// later update's) gets its bar and Cancel here, so nothing on this lane is
// ever out of reach.

import { type LocalModelsOverview } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import {
  activityAt,
  activityState,
  aheadOf,
  cancelInActivity,
  whenText,
  type ModelsAct,
} from "@/lib/localModels";
import ModelsProgress from "@/components/ModelsProgress";

export default function ModelsActivity({
  overview,
  act,
  busy,
}: {
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
}) {
  const { jobs } = overview;
  if (!jobs.length) return <div className="empty">Nothing yet.</div>;
  return (
    <div className="tablewrap card" style={{ padding: 0 }}>
      <table className="table models-activity">
        <thead>
          <tr>
            <th>What</th>
            <th>How it ended</th>
            <th>Who</th>
            <th>When</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => {
            const state = activityState(job);
            const at = activityAt(job);
            return (
              <tr key={job.id}>
                <td style={{ minWidth: 180 }}>{job.title}</td>
                <td style={{ minWidth: 220 }}>
                  {cancelInActivity(job) ? (
                    <ModelsProgress
                      job={job}
                      ahead={aheadOf(job, jobs)}
                      where="activity"
                      act={act}
                      canManage={overview.can_manage}
                      busy={busy}
                    />
                  ) : (
                    <>
                      <span className="pill" data-tone={state.tone ?? undefined}>
                        {state.label}
                      </span>
                      {state.sentence && (
                        <p
                          className="models-row-sentence"
                          data-tone={job.state === "failed" ? "bad" : undefined}
                        >
                          {state.sentence}
                        </p>
                      )}
                    </>
                  )}
                  {/* The technical part (a path, the last lines a program
                      printed) reaches administrators only: the server leaves
                      it out for everyone else. */}
                  {job.detail && job.state === "failed" && (
                    <details className="disclosure" style={{ marginTop: 6, fontSize: "0.84rem" }}>
                      <summary>Details</summary>
                      <pre className="pathbox models-lastlines">{job.detail}</pre>
                    </details>
                  )}
                </td>
                <td className="faint" style={{ overflowWrap: "anywhere" }}>
                  {job.requested_by ?? "FrameFound"}
                </td>
                <td className="faint" style={{ whiteSpace: "nowrap" }} title={whenText(at) || undefined}>
                  {at ? relativeTime(at) : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
