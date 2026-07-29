"use client";

// Duplicates. This page shows what it found and where the copies are; it
// never deletes anything. On an archive full of render copies and Premiere
// auto-saves, the number that matters is reclaimable space.

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import Shell from "@/components/Shell";
import { api, type DuplicateReport } from "@/lib/api";
import { bytes, shortDate } from "@/lib/format";

export default function DuplicatesPage() {
  const [kind, setKind] = useState<"identical" | "similar">("identical");
  const [minSizeMb, setMinSizeMb] = useState(10);
  const [report, setReport] = useState<DuplicateReport | null>(null);
  const [busy, setBusy] = useState(true);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setReport(await api.duplicates(kind, minSizeMb));
    } finally {
      setBusy(false);
    }
  }, [kind, minSizeMb]);

  useEffect(() => {
    load().catch(() => setBusy(false));
  }, [load]);

  async function verify(assetIds: string[]) {
    const result = await api.verifyDuplicates(assetIds);
    setNotice(
      result.queued
        ? `Checking ${result.queued} files byte for byte — refresh shortly.`
        : "Processing queue is unavailable.",
    );
    setTimeout(() => setNotice(null), 5000);
  }

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Duplicates</h2>
        {report && (
          <span className="faint mono">
            {bytes(report.total_reclaimable_bytes)} reclaimable across{" "}
            {report.total_groups} groups
          </span>
        )}
      </div>

      <div className="toolbar">
        <select
          className="select"
          value={kind}
          onChange={(e) => setKind(e.target.value as "identical" | "similar")}
        >
          <option value="identical">Identical copies</option>
          <option value="similar">Looks the same (re-encodes, exports)</option>
        </select>
        <select
          className="select"
          value={minSizeMb}
          onChange={(e) => setMinSizeMb(Number(e.target.value))}
        >
          <option value={1}>Larger than 1 MB</option>
          <option value={10}>Larger than 10 MB</option>
          <option value={100}>Larger than 100 MB</option>
          <option value={1000}>Larger than 1 GB</option>
        </select>
        {notice && <span className="faint">{notice}</span>}
      </div>

      {report?.note && (
        <p className="faint" style={{ fontSize: "0.84rem", marginTop: 0 }}>
          {report.note}
        </p>
      )}

      {busy && !report ? (
        <div className="empty">Looking for duplicates…</div>
      ) : !report?.groups.length ? (
        <div className="empty">
          No duplicates found at this size. Nothing to clean up.
        </div>
      ) : (
        <div style={{ display: "grid", gap: 14 }}>
          {report.groups.map((group) => (
            <div className="card" key={group.key}>
              <div
                style={{
                  display: "flex",
                  gap: 12,
                  alignItems: "baseline",
                  flexWrap: "wrap",
                  marginBottom: 12,
                }}
              >
                <strong style={{ fontSize: "1.05rem" }}>
                  {group.count} copies · {bytes(group.size_bytes)} each
                </strong>
                <span className="pill" data-tone="warn">
                  {bytes(group.reclaimable_bytes)} reclaimable
                </span>
                {group.kind === "identical" && (
                  <button
                    className="btn"
                    style={{ marginLeft: "auto", padding: "4px 12px" }}
                    onClick={() => verify(group.members.map((m) => m.asset_id))}
                  >
                    Verify byte for byte
                  </button>
                )}
              </div>

              <table className="table">
                <tbody>
                  {group.members.map((member) => (
                    <tr key={member.asset_id}>
                      <td className="mono" style={{ fontSize: "0.78rem" }}>
                        {member.relative_path}
                      </td>
                      <td className="faint" style={{ whiteSpace: "nowrap" }}>
                        {bytes(member.size_bytes)}
                      </td>
                      <td className="faint" style={{ whiteSpace: "nowrap" }}>
                        {shortDate(member.mtime)}
                      </td>
                      <td>
                        {member.content_hash_verified ? (
                          <span className="pill" data-tone="ok">verified</span>
                        ) : (
                          <span className="pill">unverified</span>
                        )}
                      </td>
                      <td>
                        <Link className="navlink" href={`/assets/${member.asset_id}`}>
                          View
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      )}
    </Shell>
  );
}
