"use client";

// Asset detail: proxy playback with a synchronised, clickable transcript.
// Arriving from a search hit deep-links to the moment via ?t=<seconds>.

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";

import Shell from "@/components/Shell";
import ClipLookCard from "@/components/ClipLookCard";
import FacesInPhoto from "@/components/FacesInPhoto";
import LookNote from "@/components/LookNote";
import ShotList from "@/components/ShotList";
import TagEditor from "@/components/TagEditor";
import Thumb from "@/components/Thumb";
import { setChatContext } from "@/lib/chatContext";
import { copyText } from "@/lib/clipboard";
import { draftFolderHref } from "@/lib/cuts";
import { usePageTitle } from "@/lib/hooks";
import {
  ApiError,
  api,
  mediaStatus,
  mediaUrl,
  subtitlesUrl,
  type AssetDetail,
  type SceneFrame,
  type ShotRow,
  type Transcript,
  type VisualHit,
} from "@/lib/api";
import { bytes, duration, resolution, shortDate, timecode } from "@/lib/format";
import {
  INCOMPLETE_NOTE,
  NOT_LOOKED_AT,
  NO_SHOTS_NOTE,
  folderOf,
  lookAbout,
  reviewHref,
  shotButtonWords,
  shotsCountText,
  showsLookCard,
} from "@/lib/looks";

import { NOT_PLAYABLE, cannotPlayText, missingFor } from "./playable";

// Said where the transcript would be, so "nothing here" is never ambiguous.
const TRANSCRIPT_NOTES: Partial<Record<AssetDetail["transcript_state"], string>> = {
  queued: "Transcript on its way — speech recognition has this clip in its queue.",
  failed: "Transcription failed for this clip. Reprocess it to try again.",
  off: "Transcription is turned off for this library.",
  // A finished run that heard nothing (see _transcript_state).
  no_speech: "No speech was heard in this clip.",
};

function DetailPage() {
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  const assetId = params.id;
  const startAt = Number(search.get("t") ?? 0);

  const videoRef = useRef<HTMLVideoElement>(null);
  const cueRefs = useRef<Record<number, HTMLButtonElement | null>>({});
  const [asset, setAsset] = useState<AssetDetail | null>(null);
  // By its file name, as the tab strip and history should show it; the site's
  // own title until the file is read.
  usePageTitle(asset?.filename);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [scenes, setScenes] = useState<SceneFrame[]>([]);
  // Which clip the sampled frames above have been answered for (found or
  // not). The shot list waits for it: its rows' pictures are chosen from
  // those frames, and drawn before they are known every row would say it has
  // no picture and then change.
  const [scenesFor, setScenesFor] = useState<string | null>(null);
  // A clip's shots, kept with the clip they are of: this page is reused for
  // the next clip, and one clip's shots must never sit under another's
  // player. `rows` is null when they could not be read (`problem` says why;
  // a server from before the shot list says nothing and has no section).
  const [shots, setShots] = useState<{
    of: string;
    rows: ShotRow[] | null;
    problem: string | null;
  } | null>(null);
  // Whether a model on this install can look at footage. False until the
  // server says so, and on a server from before local models.
  const [canLook, setCanLook] = useState(false);
  const [similar, setSimilar] = useState<VisualHit[]>([]);
  const [playhead, setPlayhead] = useState(0);
  // Which item the player has reported a position for. Until it has, the
  // moment in the address (?t=) is the best word on where it is.
  const [heardFor, setHeardFor] = useState<string | null>(null);
  const [activeCue, setActiveCue] = useState(-1);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Why the player gave up, kept with the clip it is about (see playable.ts).
  // Until it was here the page showed the poster and live-looking controls
  // for a clip with no playable copy, and a shot's button moved the playhead
  // over nothing, while Browse's viewer said plainly what was missing.
  const [unplayable, setUnplayable] = useState<{ of: string; text: string } | null>(null);
  // The clip on screen, for an answer from the server that comes back after
  // the reader has gone on to the next one.
  const onScreen = useRef(assetId);
  useEffect(() => {
    onScreen.current = assetId;
  }, [assetId]);
  // The poster or waveform shown in the player's place, so a shot's button
  // still brings the reason into view.
  const stageRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .asset(assetId)
      .then(setAsset)
      .catch((err) => setError(err instanceof Error ? err.message : "Could not load asset"));
    api
      .transcript(assetId)
      .then(setTranscript)
      .catch(() => setTranscript(null)); // no transcript is a normal state
    api
      .scenes(assetId)
      .then(setScenes)
      .catch(() => setScenes([]))
      .finally(() => setScenesFor(assetId));
    api.similar(assetId).then(setSimilar).catch(() => setSimilar([]));
  }, [assetId]);

  useEffect(() => {
    let live = true;
    api
      .lookStatus()
      .then((status) => {
        if (live) setCanLook(status.available);
      })
      .catch(() => undefined); // an older server: the page is as it was
    return () => {
      live = false;
    };
  }, []);

  // Only a clip has shots, so only a clip's page asks.
  const shotsOf = asset?.id === assetId && asset.media_type === "video" ? assetId : null;
  useEffect(() => {
    if (!shotsOf) return;
    let live = true;
    api
      .assetShots(shotsOf)
      .then((rows) => {
        if (live) setShots({ of: shotsOf, rows, problem: null });
      })
      .catch((err) => {
        if (!live) return;
        const absent = err instanceof ApiError && err.status === 404;
        setShots({
          of: shotsOf,
          rows: null,
          problem: absent ? null : err instanceof Error ? err.message : "The server did not answer.",
        });
      });
    return () => {
      live = false;
    };
  }, [shotsOf]);

  const seek = useCallback((seconds: number) => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = seconds;
    video.play().catch(() => undefined);
  }, []);

  useEffect(() => {
    if (startAt > 0 && videoRef.current) seek(startAt);
  }, [startAt, seek, asset]);

  // Play a shot from the shot list or from the card beside the player. Both
  // sit well below or beside it, so the player is brought back into view: a
  // clip that starts playing off screen looks like a button that did
  // nothing. Nothing moves when it is already in view, and with reduced
  // motion asked for the page jumps rather than glides, as the transcript
  // does.
  const playFrom = useCallback(
    (seconds: number) => {
      seek(seconds);
      const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      // A clip that cannot play has its reason where the player was: that
      // is what the button should show, not nothing.
      const stage = videoRef.current ?? stageRef.current;
      stage?.scrollIntoView({ block: "nearest", behavior: calm ? "auto" : "smooth" });
    },
    [seek],
  );

  // The player only says "error"; the server knows which of the different
  // things happened (see playable.ts), so ask before saying anything
  // specific, as the in-page viewer does.
  async function cannotPlay() {
    const of = assetId;
    if (onScreen.current !== of) return;
    setUnplayable({ of, text: NOT_PLAYABLE });
    const status = await mediaStatus(of, "proxy");
    if (onScreen.current !== of) return;
    setUnplayable({ of, text: cannotPlayText(status) });
  }

  function onTimeUpdate() {
    const video = videoRef.current;
    if (!video) return;
    setPlayhead(video.currentTime * 1000);
    setHeardFor(assetId);
    if (!transcript) return;
    const ms = video.currentTime * 1000;
    const index = transcript.segments.findIndex(
      (seg) => ms >= seg.start_ms && ms < seg.end_ms,
    );
    if (index !== activeCue) {
      setActiveCue(index);
      // The transcript follows the player; with reduced motion asked for it
      // jumps instead of gliding (the global rule stops CSS motion only).
      const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      cueRefs.current[index]?.scrollIntoView({ block: "nearest", behavior: calm ? "auto" : "smooth" });
    }
  }

  async function copyPath() {
    if (!asset) return;
    const copied = await copyText(asset.relative_path);
    setNotice(copied ? "Path copied" : "Couldn't copy — select the path and copy it by hand");
    setTimeout(() => setNotice(null), 2200);
  }

  async function reprocess() {
    try {
      await api.reprocess(assetId);
      setNotice("Reprocessing queued");
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not queue reprocessing");
    }
    setTimeout(() => setNotice(null), 3000);
  }

  // For the chat panel: the file by name and folder (the address has only
  // its id), and where the player is, so "this moment" means the frame on
  // screen. Whole seconds, so it is published once a second while a clip
  // plays rather than four times. Once the player has said where it is,
  // that is published even at 0:00: a page opened at ?t=45 and dragged
  // back to the start is at the start, and leaving `at_s` out let the
  // address's 45 seconds stand.
  const filename = asset?.filename ?? "";
  const libraryId = asset?.library_id ?? "";
  const folder = asset ? asset.relative_path.split("/").slice(0, -1).join("/") : "";
  const second = heardFor === assetId ? Math.floor(playhead / 1000) : null;
  useEffect(
    () =>
      setChatContext({
        filename,
        folder,
        library_id: libraryId,
        ...(second !== null ? { at_s: second } : {}),
      }),
    [filename, folder, libraryId, second],
  );

  if (error) return <div className="empty">{error}</div>;
  if (!asset) return <div className="empty">Loading…</div>;

  const isVideo = asset.media_type === "video";
  const isAudio = asset.media_type === "audio";
  const cannotPlayNow = missingFor(unplayable, asset.id);
  // The look as the server may not have sent it: a server from before looks
  // has no such field.
  const look = asset.look ?? null;
  const about = look ? lookAbout(asset.media_type, look) : null;
  // The best shot's button plays it or, where the clip cannot play here,
  // shows why, and says which.
  const bestShot =
    about?.kind === "shot" && about.startMs !== null && about.endMs !== null
      ? shotButtonWords(
          "the best shot",
          timecode(about.startMs),
          timecode(about.endMs),
          !cannotPlayNow,
        )
      : null;
  const clipShots = shots?.of === asset.id ? shots : null;
  const shotRows = clipShots?.rows ?? null;
  // The model's two columns: where a model can look, or one already has.
  const withLooks = canLook || (shotRows ?? []).some((shot) => shot.look);
  const reviewFolder = reviewHref({
    library_id: asset.library_id,
    folder: folderOf(asset.relative_path),
  });
  // "Draft a cut from this folder": for a video, where models can run, and
  // not for a clip at the top of its library (a draft is of a folder).
  const clipFolder = folderOf(asset.relative_path);
  const draftHref = isVideo && canLook ? draftFolderHref(asset.library_id, clipFolder) : null;

  return (
    <>
      <div style={{ marginBottom: 18 }}>
        <p className="eyebrow">{asset.media_type}</p>
        <h1 style={{ fontSize: "1.9rem", marginTop: 6 }}>{asset.title ?? asset.filename}</h1>
      </div>

      <div className="detail">
        <div>
          {isVideo && cannotPlayNow && (
            // Never a dead player: the still that does exist, and a plain
            // sentence about what does not.
            <div ref={stageRef} className="player player-missing">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={mediaUrl(assetId, "poster")}
                alt=""
                onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
              />
              <p className="preview-note" role="status">
                {cannotPlayNow}
              </p>
            </div>
          )}
          {isVideo && !cannotPlayNow && (
            <video
              ref={videoRef}
              className="player"
              controls
              preload="metadata"
              poster={mediaUrl(assetId, "poster")}
              src={mediaUrl(assetId, "proxy")}
              onTimeUpdate={onTimeUpdate}
              onError={cannotPlay}
            >
              <track kind="captions" src={mediaUrl(assetId, "subtitle")} default />
            </video>
          )}
          {isAudio && (
            <div ref={stageRef} className="card" style={{ padding: 18 }}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={mediaUrl(assetId, "waveform")}
                alt=""
                style={{ width: "100%", borderRadius: 8, display: "block" }}
                onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
              />
              {cannotPlayNow ? (
                <p role="status" style={{ margin: "14px 0 0" }}>
                  {cannotPlayNow}
                </p>
              ) : (
                <audio
                  ref={videoRef as unknown as React.RefObject<HTMLAudioElement>}
                  controls
                  src={mediaUrl(assetId, "proxy")}
                  onTimeUpdate={onTimeUpdate}
                  onError={cannotPlay}
                  style={{ width: "100%", marginTop: 14 }}
                />
              )}
            </div>
          )}
          {!isVideo && !isAudio && (
            <>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img className="stillframe" src={mediaUrl(assetId, "preview")} alt={asset.filename} />
              {/* The develop editor has a "← Asset" button for exactly this
                  arrival, and nothing here led to it: the only way in was
                  through a listing, so a photograph in no listing could not
                  be edited at all. */}
              <div className="toolbar" style={{ margin: "12px 0 0" }}>
                <Link
                  className="btn"
                  href={`/edit/${asset.id}`}
                  title="Open this photograph in the develop editor"
                >
                  Edit this photograph
                </Link>
                <span className="faint" style={{ fontSize: "0.8rem" }}>
                  Light, colour and straightening. The original file is never changed.
                </span>
              </div>
            </>
          )}

          {scenes.length > 1 && (
            <div className="strip" aria-label="Scene thumbnails">
              {scenes.map((frame) => (
                <button
                  key={frame.ts_ms}
                  className="strip-cell"
                  data-scene={frame.is_scene_change}
                  data-active={
                    playhead >= frame.ts_ms && playhead < frame.ts_ms + 5000
                  }
                  onClick={() => seek(frame.ts_ms / 1000)}
                  title={
                    frame.is_scene_change
                      ? `Scene ${frame.scene_number} · ${timecode(frame.ts_ms)}`
                      : timecode(frame.ts_ms)
                  }
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={frame.url} alt="" loading="lazy" decoding="async" />
                  <time>{timecode(frame.ts_ms)}</time>
                </button>
              ))}
            </div>
          )}

          {/* Under the player and its scenes, as "Edit this photograph" is
              under a photograph, and one line tall: the transcript below
              keeps its place near the player. */}
          {draftHref && (
            <div className="toolbar" style={{ margin: "12px 0 0" }}>
              <Link
                className="btn"
                href={draftHref}
                title="Opens the form on the Cuts page with this folder chosen; nothing is drafted until you press Draft there"
              >
                Draft a cut from this folder
              </Link>
              <span className="faint" style={{ fontSize: "0.8rem", overflowWrap: "anywhere" }}>
                The local Editor drafts a rough cut from the clips in {clipFolder}.
              </span>
            </div>
          )}

          {transcript && (
            <section style={{ marginTop: 26 }}>
              <div className="sectionhead" style={{ marginTop: 0 }}>
                <h2>Transcript</h2>
                <span className="faint mono">
                  {/* Rows backfilled for clips heard before empty runs were
                      recorded carry no language; skip the empty field. */}
                  {transcript.segment_count} cues ·{" "}
                  {transcript.language && `${transcript.language} · `}
                  {transcript.model_name}
                  {transcript.segment_count > 0 && (
                    <>
                      {" · "}
                      <a href={subtitlesUrl(asset.id, "srt")} download>
                        SRT
                      </a>{" "}
                      <a href={subtitlesUrl(asset.id, "vtt")} download>
                        VTT
                      </a>
                    </>
                  )}
                </span>
              </div>
              {transcript.segment_count === 0 && (
                // The server's state, not the empty row, says what this
                // means: a placeholder left by the old code (migration 0027)
                // reads "queued", because the sweep listens to it again.
                <p className="muted">
                  {TRANSCRIPT_NOTES[asset.transcript_state] ?? TRANSCRIPT_NOTES.no_speech}
                </p>
              )}
              <div className="transcript">
                {transcript.segments.map((seg, index) => (
                  <button
                    key={`${seg.start_ms}-${index}`}
                    ref={(el) => {
                      cueRefs.current[index] = el;
                    }}
                    className="cue"
                    data-active={index === activeCue}
                    onClick={() => seek(seg.start_ms / 1000)}
                  >
                    <time>{timecode(seg.start_ms)}</time>
                    <span>{seg.text}</span>
                  </button>
                ))}
              </div>
            </section>
          )}
          {!transcript && TRANSCRIPT_NOTES[asset.transcript_state] && (
            <p className="muted" style={{ marginTop: 26 }}>
              {TRANSCRIPT_NOTES[asset.transcript_state]}
            </p>
          )}

          {/* After the player, its scenes and the transcript: the footage and
              what is said in it stay together. Above the transcript, the
              card (several hundred pixels tall) pushed it down, and the
              transcript's follow-the-player scrolling then scrolled the
              whole page past the card at every cue, taking the video off
              screen. A photograph has its own editor (the button above), so
              no card. */}
          {/* Keyed by the clip: the folder's list opens the next clip on
              this same page, and nothing of the last one's card (an open
              Adjust, its sliders) may carry over to it. */}
          {isVideo && (
            <ClipLookCard key={asset.id} assetId={asset.id} hasPlayer={!cannotPlayNow} />
          )}

          {/* Last in the column, so the player, its scenes, the transcript
              and the clip card are exactly where they were. Every row plays
              its shot in the player above and brings it back into view. */}
          {isVideo && clipShots && (shotRows !== null || clipShots.problem) && (
            <section id="shots" className="shots" aria-labelledby="shots-head">
              <div className="sectionhead">
                <h2 id="shots-head">Shots</h2>
                {shotRows !== null && shotRows.length > 0 && (
                  <span className="faint mono">{shotsCountText(shotRows, withLooks)}</span>
                )}
              </div>
              {clipShots.problem && (
                <p className="muted">Could not read this clip&apos;s shots: {clipShots.problem}</p>
              )}
              {shotRows !== null && shotRows.length === 0 && (
                <p className="muted">{NO_SHOTS_NOTE}</p>
              )}
              {shotRows !== null && shotRows.length > 0 && scenesFor === asset.id && (
                <ShotList
                  assetId={asset.id}
                  shots={shotRows}
                  frames={scenes}
                  playheadMs={heardFor === asset.id ? playhead : null}
                  onSeek={playFrom}
                  withLooks={withLooks}
                  playable={!cannotPlayNow}
                />
              )}
            </section>
          )}
        </div>

        <aside style={{ display: "grid", gap: 16 }}>
          <FacesInPhoto assetId={asset.id} />
          <TagEditor assetId={asset.id} />
          {showsLookCard(asset, canLook) && (
            <div className="card lookcard">
              <p className="eyebrow" style={{ marginBottom: 12 }}>
                What the local model saw
              </p>
              {look && about ? (
                <>
                  {about.kind === "shot" && about.startMs !== null && about.endMs !== null && (
                    <p className="lookcard-about">
                      Best shot (
                      <button
                        type="button"
                        className="linkish mono"
                        onClick={() => playFrom((about.startMs ?? 0) / 1000)}
                        aria-label={bestShot?.label}
                        title={bestShot?.title}
                      >
                        {timecode(about.startMs)}–{timecode(about.endMs)}
                      </button>
                      )
                    </p>
                  )}
                  {about.kind === "clip" && (
                    <p className="lookcard-about">The whole clip, judged from one still.</p>
                  )}
                  <LookNote look={look} />
                  {isVideo && !look.complete && (
                    <p className="lookcard-more muted">
                      {INCOMPLETE_NOTE}
                      {canLook && (
                        <>
                          {" "}
                          <Link href={reviewFolder}>Review its folder</Link>
                        </>
                      )}
                    </p>
                  )}
                  {isVideo && shotRows !== null && shotRows.length > 0 && (
                    <p className="lookcard-more">
                      <a href="#shots">
                        {shotRows.length === 1 ? "Its one shot" : `All ${shotRows.length} shots`} ↓
                      </a>
                    </p>
                  )}
                </>
              ) : (
                <p className="muted" style={{ margin: 0 }}>
                  {NOT_LOOKED_AT}{" "}
                  <Link
                    href={reviewFolder}
                    title="Opens the folder in Browse, where Review with AI looks through it"
                  >
                    Review its folder
                  </Link>
                </p>
              )}
            </div>
          )}
          <div className="card">
            <p className="eyebrow" style={{ marginBottom: 12 }}>
              Original file
            </p>
            <div className="pathbox">
              <span>{asset.relative_path}</span>
              <button className="btn" style={{ padding: "4px 10px" }} onClick={copyPath}>
                Copy
              </button>
            </div>
            <p className="faint" style={{ fontSize: "0.78rem", marginTop: 10, marginBottom: 0 }}>
              FrameFound never modifies originals.
            </p>
          </div>

          <div className="card">
            <p className="eyebrow" style={{ marginBottom: 12 }}>
              Details
            </p>
            <dl className="kv">
              <dt>Size</dt>
              <dd>{bytes(asset.size_bytes)}</dd>
              {asset.duration_s !== null && (
                <>
                  <dt>Duration</dt>
                  <dd className="mono">{duration(asset.duration_s)}</dd>
                </>
              )}
              <dt>Resolution</dt>
              <dd className="mono">{resolution(asset.width, asset.height)}</dd>
              {asset.fps !== null && (
                <>
                  <dt>Frame rate</dt>
                  <dd className="mono">{asset.fps} fps</dd>
                </>
              )}
              {asset.video_codec && (
                <>
                  <dt>Video</dt>
                  <dd className="mono">{asset.video_codec}</dd>
                </>
              )}
              {asset.audio_codec && (
                <>
                  <dt>Audio</dt>
                  <dd className="mono">
                    {asset.audio_codec}
                    {asset.channels ? ` · ${asset.channels}ch` : ""}
                  </dd>
                </>
              )}
              <dt>Captured</dt>
              <dd>{shortDate(asset.captured_at)}</dd>
              <dt>Indexed</dt>
              <dd>{shortDate(asset.first_indexed_at)}</dd>
              <dt>Status</dt>
              <dd>
                <span
                  className="pill"
                  data-tone={
                    asset.processing_status === "ready"
                      ? "ok"
                      : asset.processing_status.includes("fail")
                        ? "bad"
                        : "warn"
                  }
                >
                  {asset.processing_status}
                </span>
              </dd>
            </dl>
          </div>

          {(asset.camera_make || asset.camera_model || asset.lens || asset.iso) && (
            <div className="card">
              <p className="eyebrow" style={{ marginBottom: 12 }}>
                Capture
              </p>
              <dl className="kv">
                {asset.camera_make && (
                  <>
                    <dt>Camera</dt>
                    <dd>
                      {asset.camera_make} {asset.camera_model ?? ""}
                    </dd>
                  </>
                )}
                {asset.lens && (
                  <>
                    <dt>Lens</dt>
                    <dd>{asset.lens}</dd>
                  </>
                )}
                {asset.focal_length_mm && (
                  <>
                    <dt>Focal</dt>
                    <dd className="mono">{asset.focal_length_mm}mm</dd>
                  </>
                )}
                {asset.aperture_f && (
                  <>
                    <dt>Aperture</dt>
                    <dd className="mono">ƒ/{asset.aperture_f}</dd>
                  </>
                )}
                {asset.shutter_speed && (
                  <>
                    <dt>Shutter</dt>
                    <dd className="mono">{asset.shutter_speed}</dd>
                  </>
                )}
                {asset.iso && (
                  <>
                    <dt>ISO</dt>
                    <dd className="mono">{asset.iso}</dd>
                  </>
                )}
                {asset.gps_lat !== null && asset.gps_lon !== null && (
                  <>
                    <dt>Location</dt>
                    <dd className="mono">
                      {asset.gps_lat.toFixed(4)}, {asset.gps_lon.toFixed(4)}
                    </dd>
                  </>
                )}
              </dl>
            </div>
          )}

          {similar.length > 0 && (
            <div className="card">
              <p className="eyebrow" style={{ marginBottom: 12 }}>
                Visually similar
              </p>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
                {similar.slice(0, 6).map((hit) => (
                  <Link
                    key={hit.asset_id}
                    href={`/assets/${hit.asset_id}`}
                    className="tile"
                    title={hit.filename}
                  >
                    <div className="tile-frame">
                      <Thumb
                        assetId={hit.asset_id}
                        mediaType={hit.media_type}
                        status="ready"
                      />
                    </div>
                  </Link>
                ))}
              </div>
            </div>
          )}

          <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <button className="btn" onClick={reprocess}>
              Reprocess
            </button>
            {notice && <span className="faint">{notice}</span>}
          </div>
        </aside>
      </div>
    </>
  );
}

export default function Page() {
  return (
    <Shell>
      <Suspense fallback={<div className="empty">Loading…</div>}>
        <DetailPage />
      </Suspense>
    </Shell>
  );
}
