"use client";

import { useEffect, useState } from "react";

import Shell from "@/components/Shell";
import { api, type HealthReport } from "@/lib/api";

const TONE: Record<string, string> = { ok: "ok", error: "bad", unconfigured: "warn" };

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
        </div>
      )}
    </Shell>
  );
}
