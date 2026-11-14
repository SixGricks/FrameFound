"use client";

// Two versions of one cut, side by side, the older on the left: what a
// second pass changed — the Editor drafting again, or Claude reviewing a
// local draft and building its own.
//
// Each side is the same shot list the cut's page shows (CutShots), so every
// picture still opens the footage at the piece's in point and ← → still step
// through that version. What Compare adds is a pill on each row saying what
// became of the shot in the other version, and one sentence above that
// counts them. Two pieces are "the same shot" when they are the same clip
// and mostly the same stretch of it (`compareVersions`); nothing here reads
// what a model wrote about a shot.
//
// Which two is in the address (`?v=2&with=1`), so a reload or a pasted link
// compares the same pair. With more than two versions each side has a
// select; a side only offers versions that keep the older one on the left.

import { useMemo } from "react";

import CutShots from "@/components/CutShots";
import type { CutPiece, CutVersion } from "@/lib/api";
import {
  compareVersions,
  markPills,
  secondsText,
  sourceLabel,
  trimNote,
  versionButton,
  type PieceMark,
} from "@/lib/cuts";

function Marks({
  mark,
  side,
  otherVersion,
}: {
  mark: PieceMark | undefined;
  side: "older" | "newer";
  otherVersion: number;
}) {
  if (!mark) return null;
  const was = trimNote(mark, side, otherVersion);
  return (
    <>
      {markPills(mark, otherVersion).map((pill) => (
        <span key={pill.text} className="pill" data-tone={pill.tone ?? undefined} title={pill.title}>
          {pill.text}
        </span>
      ))}
      {was && <span className="faint mono cutcompare-was">{was}</span>}
    </>
  );
}

export default function CutCompare({
  versions,
  older,
  newer,
  onChoose,
}: {
  /** Every version of the cut, oldest first. */
  versions: CutVersion[];
  older: CutVersion;
  newer: CutVersion;
  /** Another version was picked for one side. */
  onChoose: (side: "older" | "newer", version: CutVersion) => void;
}) {
  const compared = useMemo(() => compareVersions(older, newer), [older, newer]);
  const choosing = versions.length > 2;

  function column(side: "older" | "newer") {
    const version = side === "older" ? older : newer;
    const other = side === "older" ? newer : older;
    const marks = side === "older" ? compared.older : compared.newer;
    // The older stays on the left: this side offers only what is older than
    // the right-hand version, or newer than the left-hand one.
    const offered = versions.filter((each) =>
      side === "older" ? each.version < other.version : each.version > other.version,
    );
    const headingId = `cutcompare-${side}`;
    return (
      <section className="cutcompare-side" aria-labelledby={headingId}>
        <div className="cutcompare-head">
          <h3 id={headingId}>
            Version {version.version} · {version.label || sourceLabel(version.source)}
          </h3>
          <span className="faint mono">
            {version.pieces.length} {version.pieces.length === 1 ? "shot" : "shots"} ·{" "}
            {secondsText(version.duration_s)}
          </span>
          {choosing && (
            <select
              className="select"
              value={version.id}
              aria-label={side === "older" ? "The older version to compare" : "The newer version to compare"}
              onChange={(event) => {
                const picked = versions.find((each) => each.id === event.target.value);
                if (picked) onChoose(side, picked);
              }}
            >
              {offered.map((each) => (
                <option key={each.id} value={each.id}>
                  {versionButton(each)}
                </option>
              ))}
            </select>
          )}
        </div>
        {/* Keyed by version: a viewer open on one version's shot does not
            carry over when another version is chosen for this side. */}
        <CutShots
          key={version.id}
          version={version}
          lookPrefix={side}
          badge={(piece: CutPiece) => (
            <Marks mark={marks.get(piece.position)} side={side} otherVersion={other.version} />
          )}
        />
      </section>
    );
  }

  return (
    <>
      <p className="cutcompare-says" role="status">
        {compared.sentence}
      </p>
      <p className="faint cutcompare-key">
        A shot counts as the same when it is the same clip and mostly the same stretch of it. Every
        picture opens the footage, as in the shot list.
      </p>
      <div className="cutcompare">
        {column("older")}
        {column("newer")}
      </div>
    </>
  );
}
