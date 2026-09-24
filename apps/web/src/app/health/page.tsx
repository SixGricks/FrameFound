"use client";

import { useEffect, useState } from "react";

import Shell from "@/components/Shell";
import { api, type HealthReport } from "@/lib/api";
import { relativeTime } from "@/lib/format";

const TONE: Record<string, string> = {
  ok: "ok",
  error: "bad",
  unconfigured: "warn",
  low: "warn",
  full: "bad",
  unreachable: "bad",
  stale: "warn",
  missing: "bad",
};

export default function HealthPage() {
  const [health, setHealth] = useState<HealthReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () => api.health().then(setHealth).catch((e) => setError(e.message));
    load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, []);

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>System</h2>
        {health && <span className="faint mono">v{health.version}</span>}
      </div>

      {error && <div className="empty">{error}</div>}
      {!health && !error && <div className="empty">Loading…</div>}

      {health && (
        <div className="statgrid">
          <div className="stat">
            <p className="eyebrow">Catalog database</p>
            <div style={{ marginTop: 10 }}>
              <span className="pill" data-tone={TONE[health.database.status]}>
                {health.database.status}
              </span>
            </div>
            {health.database.detail && (
              <p className="faint" style={{ fontSize: "0.78rem" }}>{health.database.detail}</p>
            )}
          </div>

          <div className="stat">
            <p className="eyebrow">Job queue</p>
            <div style={{ marginTop: 10 }}>
              <span className="pill" data-tone={TONE[health.queue.status]}>
                {health.queue.status}
              </span>
            </div>
            {health.queue.detail && (
              <p className="faint" style={{ fontSize: "0.78rem" }}>{health.queue.detail}</p>
            )}
          </div>

          <div className="stat">
            <p className="eyebrow">Free space for previews</p>
            <div
              className="stat-value"
              data-tone={
                health.data_dir_free_gb !== null && health.data_dir_free_gb < 10 ? "bad" : undefined
              }
            >
              {health.data_dir_free_gb === null ? "—" : `${health.data_dir_free_gb} GB`}
            </div>
          </div>

          <div className="stat">
            <p className="eyebrow">Catalogue backup</p>
            <div style={{ marginTop: 10 }}>
              <span className="pill" data-tone={TONE[health.backup.status]}>
                {health.backup.status === "ok"
                  ? `last ${relativeTime(health.backup.last_backup_at)}`
                  : health.backup.status}
              </span>
            </div>
            {health.backup.detail && (
              <p className="faint" style={{ fontSize: "0.78rem" }}>{health.backup.detail}</p>
            )}
          </div>
        </div>
      )}

      {health && (
        <>
          {/* Every disk and share FrameFound depends on. Built server-side
              long ago and never shown — the three unmounted Intel libraries
              reported "unreachable" here, unseen, for 39 days. */}
          <div className="sectionhead">
            <h2>Storage</h2>
          </div>
          <table className="table">
            <thead>
              <tr>
                <th>Volume</th>
                <th>Status</th>
                <th>Free</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {health.volumes.map((volume) => (
                <tr key={`${volume.label}:${volume.path}`}>
                  <td>
                    {volume.label}
                    <div className="faint mono" style={{ fontSize: "0.72rem" }}>
                      {volume.path}
                    </div>
                  </td>
                  <td>
                    <span className="pill" data-tone={TONE[volume.status]}>
                      {volume.status}
                    </span>
                  </td>
                  <td className="mono">
                    {volume.status === "unreachable"
                      ? "—"
                      : `${volume.free_gb} of ${volume.total_gb} GB`}
                  </td>
                  <td className="faint" style={{ fontSize: "0.78rem" }}>
                    {volume.detail}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Shell>
  );
}
