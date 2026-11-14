"use client";

// One version's shot list: a row per piece, in timeline order — its picture,
// the clip, in and out, what the shot is, and why it is there.
//
// The picture is the way to the footage. A plain click opens the viewer over
// the page at the piece's in point, and ← → step through the cut in order; a
// modified or middle click opens the clip's own page at that moment in a new
// tab, as everywhere else. Nothing on a row competes with that click: there
// is no selection here, and a draft is judged by watching it, not by reading
// about it.
//
// A clip that is off the share still opens: its page is still there and its
// playable copy usually is too, and the viewer says so itself when it is
// not. Only a clip deleted from the catalogue has nothing to open; its row
// stays, greyed, so the cut still reads as the cut that was made.
//
// The note ("why it is there") is a model's words on a local draft and on
// Claude's cut, and a marker's on an imported timeline. It is shown as text.

import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

import LookFrame from "@/components/LookFrame";
import MediaPreview from "@/components/MediaPreview";
import Thumb from "@/components/Thumb";
import type { CutPiece, CutVersion } from "@/lib/api";
import {
  canLook,
  goneSentence,
  momentHref,
  noteLead,
  pieceDetail,
  pieceHeading,
  pieceSteps,
  secondsText,
  shotWords,
  spanText,
  totalLine,
} from "@/lib/cuts";

export default function CutShots({
  version,
  lookPrefix = "piece",
  badge,
}: {
  version: CutVersion;
  /** Written before each piece's position in `data-look`, for focus to come
   *  back to the picture the viewer was last on. A page that shows two
   *  versions at once gives each list its own. */
  lookPrefix?: string;
  /** Drawn after a piece's name, e.g. what changed between two versions. */
  badge?: (piece: CutPiece) => ReactNode;
}) {
  const [viewing, setViewing] = useState<number | null>(null);
  const pieces = version.pieces;

  const viewed =
    viewing === null ? null : (pieces.find((piece) => piece.position === viewing && canLook(piece)) ?? null);
  const steps = viewed ? pieceSteps(pieces, viewed.position) : null;
  const lead = noteLead(version.source);

  function focusAfterViewing(): HTMLElement | null {
    return viewed
      ? document.querySelector<HTMLElement>(`[data-look="${lookPrefix}-${viewed.position}"]`)
      : null;
  }

  if (pieces.length === 0) {
    return <div className="empty">This version has no shots.</div>;
  }

  return (
    <>
      <ol className="cutshots">
        {pieces.map((piece, index) => {
          const number = index + 1;
          const span = spanText(piece.in_s, piece.out_s);
          const words = shotWords(piece.shot);
          const gone = goneSentence(piece);
          const open = canLook(piece) && piece.asset_id !== null;
          return (
            <li key={piece.position} className="cutshot" data-gone={gone ? "true" : undefined}>
              <span className="cutshot-n mono" aria-hidden="true">
                {number}
              </span>
              <div className="cutshot-pic">
                {open && piece.asset_id ? (
                  <LookFrame
                    assetId={piece.asset_id}
                    mediaType={piece.media_type}
                    // What a screen reader hears: two pieces of one clip are
                    // two different things to look at.
                    filename={`shot ${number}, ${piece.clip}, ${span}`}
                    focusKey={`${lookPrefix}-${piece.position}`}
                    hint="click to watch from its in point"
                    onLook={() => setViewing(piece.position)}
                    picture={<PieceStill piece={piece} />}
                    href={momentHref(piece.asset_id, piece.in_s)}
                  />
                ) : (
                  <div className="tile-frame">
                    <span className="placeholder">clip deleted</span>
                  </div>
                )}
              </div>
              <div className="cutshot-body">
                <p className="cutshot-name">
                  <span className="compare-sr">Shot {number}: </span>
                  {open && piece.asset_id ? (
                    <Link href={momentHref(piece.asset_id, piece.in_s)} prefetch={false} className="cutshot-link">
                      {piece.clip}
                    </Link>
                  ) : (
                    <span>{piece.clip}</span>
                  )}
                  {badge?.(piece)}
                </p>
                <p className="cutshot-times">
                  <span className="tc" title="In and out, in seconds from the clip's start">
                    {span}
                  </span>
                  <span className="tc" title="How long it runs in the cut">
                    {secondsText(piece.length_s)}
                  </span>
                </p>
                {words && <p className="faint cutshot-what">{words}</p>}
                {piece.note && (
                  <p className="cutshot-note" data-made={version.source}>
                    <span className="cutshot-lead">{lead}</span> {piece.note}
                  </p>
                )}
                {gone && <p className="cutshot-gone">{gone}</p>}
              </div>
            </li>
          );
        })}
      </ol>
      <p className="faint cutshot-total">{totalLine(version.duration_s, version.draft?.target_s)}</p>

      {viewed && viewed.asset_id && steps && (
        // Mounted once, after the list — never inside a row.
        <MediaPreview
          assetId={viewed.asset_id}
          mediaType={viewed.media_type}
          filename={viewed.filename}
          startAt={viewed.media_type === "image" ? undefined : viewed.in_s}
          // Two pieces of one clip in a row: stepping between them moves the
          // clip to the next in point, even at the same second.
          momentKey={`${version.id}:${viewed.position}`}
          heading={pieceHeading(viewed, steps.at, steps.count)}
          detail={pieceDetail(viewed)}
          onClose={() => setViewing(null)}
          onPrev={steps.prev ? () => setViewing(steps.prev?.position ?? null) : undefined}
          onNext={steps.next ? () => setViewing(steps.next?.position ?? null) : undefined}
          returnFocus={focusAfterViewing}
        >
          {viewed.note && (
            <p className="cutshot-viewer-note">
              <span className="cutshot-lead">{lead}</span> {viewed.note}
            </p>
          )}
        </MediaPreview>
      )}
    </>
  );
}

/**
 * A piece's picture: the still nearest the middle of its shot, which the
 * server serves from what is already on disk. When there is none (a clip
 * whose stills were never made, or whose drive is away), the clip's own
 * thumbnail, with Thumb's honest "No preview" when that is missing too.
 */
function PieceStill({ piece }: { piece: CutPiece }) {
  const [failed, setFailed] = useState(false);
  // Another version's piece at the same place in the list is another picture.
  useEffect(() => {
    setFailed(false);
  }, [piece.still_url]);

  if (piece.still_url && !failed) {
    return (
      // Served by our own session-guarded route, as every picture in the app.
      // eslint-disable-next-line @next/next/no-img-element
      <img src={piece.still_url} alt="" loading="lazy" decoding="async" onError={() => setFailed(true)} />
    );
  }
  if (piece.asset_id) {
    return <Thumb assetId={piece.asset_id} mediaType={piece.media_type} status="ready" />;
  }
  return <span className="placeholder">no picture</span>;
}
