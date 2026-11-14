"use client";

// A clip's shots, in the clip's order: a picture of each, its times, how the
// camera moved, how steady it is and the light — what the motion analysis
// measured — and, where a local model has looked at a shot, its score and
// what it said.
//
// The picture and the times are one button that plays the shot in the page's
// own player, so the list is a way into the footage and never a dead end:
// "is the third shot really that steady?" is answered by pressing it. The
// row of the shot the player is in is marked, as the scene strip marks its
// frame.
//
// The measured columns are there on every install. The last two (score, and
// what the model saw) are a model's opinion, drawn only where one can look
// or one already has (`withLooks`), so a server that cannot run models shows
// measurements and nothing that reads as a gap.

import { useState } from "react";

import { MadeUpPill, ScorePill } from "@/components/LookNote";
import { mediaUrl, type ShotRow } from "@/lib/api";
import { timecode } from "@/lib/format";
import {
  NOT_LOOKED_AT,
  NO_SHOT_PICTURE_NOTE,
  OPINION_NOTE,
  STEADINESS_NOTE,
  lookSaidBy,
  shotAt,
  shotMove,
  shotButtonWords,
  shotPictures,
  steadinessText,
} from "@/lib/looks";

// The server answers /media/{id}/frame?ts= for sampled frames only; a shot's
// own middle still has no address yet (see `shotPictures`). Asking for it
// would be a 404 on every row, so a row shows the sampled frame nearest the
// shot's middle. Set this once the server serves the still itself.
const SHOT_STILLS_SERVED = false;

function ShotPicture({
  assetId,
  shot,
  frames,
}: {
  assetId: string;
  shot: ShotRow;
  frames: ReadonlyArray<{ ts_ms: number }>;
}) {
  // Which pictures did not load, by what was asked for, so the next one is
  // tried and a picture that failed is not asked for again.
  const [failed, setFailed] = useState<readonly string[]>([]);
  const kind = shotPictures(shot, frames, SHOT_STILLS_SERVED).find((k) => !failed.includes(k));
  if (!kind) {
    return (
      <span className="shot-nopicture" title={NO_SHOT_PICTURE_NOTE}>
        no picture
      </span>
    );
  }
  return (
    // Served by our own session-guarded endpoint, as Thumb's pictures are.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={mediaUrl(assetId, kind)}
      alt=""
      loading="lazy"
      decoding="async"
      onError={() => setFailed((already) => (already.includes(kind) ? already : [...already, kind]))}
    />
  );
}

export default function ShotList({
  assetId,
  shots,
  frames = [],
  playheadMs,
  onSeek,
  withLooks,
  playable = true,
}: {
  assetId: string;
  /** In the clip's order, as the server lists them. */
  shots: ShotRow[];
  /** The clip's sampled frames (the scene strip's), for the rows' pictures. */
  frames?: ReadonlyArray<{ ts_ms: number }>;
  /** Where the player is; null until it has said. */
  playheadMs: number | null;
  /** Play the clip from this second, in the page's player. */
  onSeek: (seconds: number) => void;
  /** Draw the model's two columns. */
  withLooks: boolean;
  /** The clip can play on this page. False when its reason stands where the
   *  player would be: a row's button then shows that, and says so. */
  playable?: boolean;
}) {
  const playing = shotAt(shots, playheadMs);
  // Who the captions are from, once each: a clip looked at over two nights
  // by two builds says both.
  const said: string[] = [];
  for (const shot of shots) {
    if (!shot.look) continue;
    const line = lookSaidBy(shot.look);
    if (line && !said.includes(line)) said.push(line);
  }
  const madeUp = shots.some((shot) => shot.look?.demo);

  return (
    <div className="card shotlist-card">
      <table className="table shotlist">
        <thead>
          <tr>
            <th scope="col">Shot</th>
            <th scope="col">Move</th>
            <th scope="col" title={STEADINESS_NOTE}>
              Steadiness
            </th>
            <th scope="col">Light</th>
            {withLooks && (
              <>
                <th scope="col">AI score</th>
                <th scope="col">What the model saw</th>
              </>
            )}
          </tr>
        </thead>
        <tbody>
          {shots.map((shot, index) => {
            const from = timecode(shot.start_ms);
            const to = timecode(shot.end_ms);
            const look = shot.look;
            const words = shotButtonWords(`shot ${index + 1}`, from, to, playable);
            return (
              <tr key={shot.idx} data-active={playing === shot.idx || undefined}>
                <td className="shot-when">
                  <button
                    type="button"
                    className="shot-seek"
                    onClick={() => onSeek(shot.start_ms / 1000)}
                    aria-label={words.label}
                    aria-current={playing === shot.idx ? "true" : undefined}
                    title={words.title}
                  >
                    <span className="shot-still">
                      <ShotPicture assetId={assetId} shot={shot} frames={frames} />
                    </span>
                    <span className="tc">
                      {from}–{to}
                    </span>
                  </button>
                </td>
                <td data-label="Move">{shotMove(shot)}</td>
                <td data-label="Steadiness" className="mono" title={STEADINESS_NOTE}>
                  {steadinessText(shot.smooth)}
                </td>
                <td data-label="Light">{shot.light ?? "—"}</td>
                {withLooks && (
                  <>
                    <td className="shot-score">
                      {look && <ScorePill score={look.score} />}
                      {look?.demo && <MadeUpPill />}
                    </td>
                    <td className="shot-said">
                      {look ? (
                        <>
                          {look.caption && <div>{look.caption}</div>}
                          {look.reason && <div className="muted shot-reason">{look.reason}</div>}
                        </>
                      ) : (
                        <span className="faint">{NOT_LOOKED_AT}</span>
                      )}
                    </td>
                  </>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
      {said.length > 0 && (
        <p className="shotlist-said faint">
          <span className="mono">{said.join("; ")}</span>
          {". "}
          {OPINION_NOTE}
          {madeUp && " In this demonstration the captions and scores are made up."}
        </p>
      )}
    </div>
  );
}
