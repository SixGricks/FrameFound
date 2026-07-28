"use client";

// Processing dashboard. Deliberately plain-language: queue names become
// "what it does", and a failure feed is always visible rather than buried
// in container logs.

import { useEffect, useState } from "react";

import Shell from "@/components/Shell";
import { api, type ProcessingReport } from "@/lib/api";
import { relativeTime } from "@/lib/format";

const QUEUE_LABELS: Record<string, string> = {
  visuals: "Thumbnails & posters",
  metadata: "Reading file details",
  media: "Video previews",
  transcribe: "Transcribing speech",
  vision: "Visual analysis",
  default: "Legacy queue",
};

const TASK_LABELS: Record<string, string> = {
  extract_metadata: "Read details",
  generate_derivatives: "Thumbnails",
  generate_proxy: "Video preview",
  transcribe_asset: "Transcribe",
};

export default function ProcessingPage() {
  const [report, setReport] = useState<ProcessingReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .processing()
        .then((data) => !cancelled && setReport(data))
        .catch((err) => !cancelled && setError(err.message));
    load();
    const timer = setInterval(load, 5000); // live view while work drains
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Processing</h2>
        <span className="faint mono">refreshes every 5s</span>
      </div>

      {error && <div className="empty">{error}</div>}
      {!report && !error && <div className="empty">Loading…</div>}

      {report && (
        <>
          <div className="statgrid">
            {Object.entries(report.queue_depths)
              .filter(([name, depth]) => name !== "default" || depth > 0)
              .map(([name, depth]) => (
                <div className="stat" key={name}>
                  <p className="eyebrow">{QUEUE_LABELS[name] ?? name}</p>
                  <div
                    className="stat-value"
                    data-tone={depth < 0 ? "bad" : depth > 0 ? "busy" : undefined}
                  >
                    {depth < 0 ? "—" : depth.toLocaleString()}
                  </div>
                  <p className="faint" style={{ fontSize: "0.76rem", margin: 0 }}>
                    {depth < 0 ? "queue unreachable" : depth === 0 ? "idle" : "waiting"}
                  </p>
                </div>
              ))}
          </div>

          <div className="sectionhead">
            <h2>Media state</h2>
          </div>
          <div className="statgrid">
            {Object.entries(report.assets_by_status).map(([status, count]) => (
              <div className="stat" key={status}>
                <p className="eyebrow">{status.replace(/_/g, " ")}</p>
                <div className="stat-value">{count.toLocaleString()}</div>
              </div>
            ))}
          </div>

          <div className="sectionhead">
            <h2>Generated files</h2>
            <span className="faint mono">
              {Object.entries(report.jobs_last_hour)
                .map(([status, count]) => `${count} ${status}`)
                .join(" · ") || "no recent jobs"}
            </span>
          </div>
          <div className="card" style={{ padding: 0, overflowX: "auto" }}>
            <table className="table">
              <thead>
                <tr>
                  <th>Kind</th>
                  <th>Ready</th>
                  <th>Failed</th>
                  <th style={{ width: "45%" }}>Completion</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(report.derivatives).map(([kind, states]) => {
                  const ready = states.ready ?? 0;
                  const failed = states.failed ?? 0;
                  const total = Object.values(states).reduce((a, b) => a + b, 0);
                  const pct = total ? Math.round((ready / total) * 100) : 0;
                  return (
                    <tr key={kind}>
                      <td style={{ textTransform: "capitalize" }}>{kind}</td>
                      <td className="mono">{ready.toLocaleString()}</td>
                      <td className="mono" style={{ color: failed ? "var(--ember)" : undefined }}>
                        {failed || "—"}
                      </td>
                      <td>
                        <div className="bar">
                          <i style={{ width: `${pct}%` }} />
                        </div>
                      </td>
                    </tr>
                  );
                })}
                {Object.keys(report.derivatives).length === 0 && (
                  <tr>
                    <td colSpan={4} className="faint">
                      Nothing generated yet.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          {report.recent_failures.length > 0 && (
            <>
              <div className="sectionhead">
                <h2>Recent failures</h2>
                <span className="faint mono">originals are never affected</span>
              </div>
              <div className="card" style={{ padding: 0, overflowX: "auto" }}>
                <table className="table">
                  <thead>
                    <tr>
                      <th>Task</th>
                      <th>When</th>
                      <th>Reason</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {report.recent_failures.map((job) => (
                      <tr key={job.id}>
                        <td>{TASK_LABELS[job.task_name] ?? job.task_name}</td>
                        <td className="faint mono">{relativeTime(job.started_at)}</td>
                        <td className="muted">{job.error ?? "—"}</td>
                        <td>
                          {job.asset_id && (
                            <a className="navlink" href={`/assets/${job.asset_id}`}>
                              View
                            </a>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </>
      )}
    </Shell>
  );
}
