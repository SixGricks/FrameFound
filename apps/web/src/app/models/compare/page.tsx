"use client";

// Compare models: build a new comparison, and the comparisons saved so far.
//
// The server decides what this page is (`mode`), never the browser. Where
// models cannot run it is AI models' one card, and any comparison already
// saved (a database that came from a Mac) is still listed and opens
// read-only. In demonstration mode a banner says nothing here is real.
//
// One request draws the page (`api.compareOverview()`), read again every
// 5 s while a comparison runs and every 30 s otherwise, never while the tab
// is hidden. The builder keeps what the owner chose across those reads: a
// read only refreshes the models offered and the list below.

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import CompareBuilder from "@/components/CompareBuilder";
import Shell from "@/components/Shell";
import { ApiError, api, type CompareOverview, type ComparisonSummary } from "@/lib/api";
import { decisionText, listEvery, sampleText, statePill } from "@/lib/compare";
import { usePageTitle } from "@/lib/hooks";
import { leadSentence, rereads, whenText, type Rereader } from "@/lib/localModels";

export default function CompareModelsPage() {
  usePageTitle("Compare models");
  const [overview, setOverview] = useState<CompareOverview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const reader = useRef<Rereader<CompareOverview> | null>(null);

  useEffect(() => {
    const own = rereads<CompareOverview>({
      read: () => api.compareOverview(),
      answered: (next) => {
        setOverview(next);
        setLoadError(null);
      },
      failed: (err) =>
        // The last good answer stays on screen beneath the sentence.
        setLoadError(
          err instanceof ApiError
            ? err.message
            : "FrameFound could not be reached to read the comparisons. It tries again shortly.",
        ),
      every: listEvery,
      hidden: () => document.hidden,
      later: (run, ms) => window.setTimeout(run, ms),
      cancel: (handle) => window.clearTimeout(handle as number),
    });
    reader.current = own;
    void own.now();
    const onShow = () => {
      if (!document.hidden) void own.now();
    };
    document.addEventListener("visibilitychange", onShow);
    return () => {
      document.removeEventListener("visibilitychange", onShow);
      own.stop();
      if (reader.current === own) reader.current = null;
    };
  }, []);

  const heading = (
    <div className="sectionhead" style={{ marginTop: 0, flexDirection: "column", alignItems: "flex-start", gap: 4 }}>
      <Link href="/models" className="compare-back">
        ‹ AI models
      </Link>
      <h2>Compare models</h2>
    </div>
  );

  if (!overview) {
    return (
      <Shell>
        {heading}
        {loadError ? (
          <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
            {loadError}
          </div>
        ) : (
          <div className="empty">Loading…</div>
        )}
      </Shell>
    );
  }

  const error = loadError && (
    <div className="card" role="alert" style={{ borderColor: "var(--ember)", marginTop: 14 }}>
      {loadError}
    </div>
  );

  if (overview.mode === "unavailable") {
    return (
      <Shell>
        {heading}
        {error}
        <div className="card" role="status">
          <p style={{ margin: 0 }}>
            <strong>Models run only on a Mac.</strong>{" "}
            {overview.note ??
              "This FrameFound runs in containers on a Linux server, where the software that runs these models does not exist. Everything else works as before."}
          </p>
          <p className="muted" style={{ margin: "8px 0 0" }}>
            When FrameFound is installed on the Mac mini, this is where models are compared on
            your own footage.
          </p>
        </div>
        {overview.comparisons.length > 0 && <Saved comparisons={overview.comparisons} readOnly />}
      </Shell>
    );
  }

  const [demoLead, demoRest] = leadSentence(overview.note ?? "Demonstration only.");

  return (
    <Shell>
      {heading}
      {overview.mode === "demo" && (
        <div className="demo-banner" role="status">
          <strong>{demoLead}</strong> {demoRest} It is here so Compare can be looked at on a
          development machine.
        </div>
      )}
      {error}
      {!overview.can_manage && (
        <p className="faint" style={{ margin: "12px 0 0" }}>
          Only an administrator can change anything here.
        </p>
      )}

      <section id="new" className="secsection">
        <div className="sectionhead">
          <h2>New comparison</h2>
          <span className="faint mono">question {overview.prompt_version}</span>
        </div>
        <CompareBuilder overview={overview} />
      </section>

      <Saved comparisons={overview.comparisons} readOnly={false} />
    </Shell>
  );
}

function Saved({ comparisons, readOnly }: { comparisons: ComparisonSummary[]; readOnly: boolean }) {
  return (
    <section id="saved" className="secsection">
      <div className="sectionhead">
        <h2>Saved comparisons</h2>
        {comparisons.length > 0 && (
          <span className="faint mono">
            {comparisons.length === 1 ? "1 comparison" : `${comparisons.length} comparisons`}
          </span>
        )}
      </div>
      {readOnly && (
        <p className="faint" style={{ margin: "0 0 12px" }}>
          These were made on a Mac. They can be read here, not changed.
        </p>
      )}
      {comparisons.length === 0 ? (
        <div className="empty">Nothing compared yet.</div>
      ) : (
        <div className="tablewrap card" style={{ padding: 0 }}>
          <table className="table compare-saved">
            <thead>
              <tr>
                <th>Name</th>
                <th>Sample</th>
                <th>Models</th>
                <th>State</th>
                <th>Decided</th>
                <th>When</th>
                <th>By</th>
              </tr>
            </thead>
            <tbody>
              {comparisons.map((one) => {
                const pill = statePill(one.state);
                return (
                  <tr key={one.id}>
                    <td>
                      <Link href={`/models/compare/${one.id}`} className="compare-link">
                        {one.name}
                      </Link>
                      {!one.current && (
                        <>
                          {" "}
                          <span
                            className="pill faint"
                            title="Made with an earlier version of the question. It can be read but not added to."
                          >
                            earlier question
                          </span>
                        </>
                      )}
                    </td>
                    <td>
                      {sampleText(one)}
                      <div className="faint compare-label">{one.source_label}</div>
                    </td>
                    <td>
                      {one.models.length ? one.models.map((entry) => entry.name).join(", ") : "—"}
                    </td>
                    <td>
                      <span className="pill" data-tone={pill.tone ?? undefined}>
                        {pill.label}
                      </span>
                    </td>
                    <td>{decisionText(one) ?? <span className="faint">not yet</span>}</td>
                    <td className="mono">{whenText(one.created_at)}</td>
                    <td>{one.created_by ?? <span className="faint">—</span>}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
