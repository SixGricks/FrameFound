"use client";

// Asset detail: proxy playback with a synchronised, clickable transcript.
// Arriving from a search hit deep-links to the moment via ?t=<seconds>.

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";

import Shell from "@/components/Shell";
import FacesInPhoto from "@/components/FacesInPhoto";
import TagEditor from "@/components/TagEditor";
import Thumb from "@/components/Thumb";
import {
  api,
  mediaUrl,
  type AssetDetail,
  type SceneFrame,
  type Transcript,
  type VisualHit,
} from "@/lib/api";
import { bytes, duration, resolution, shortDate, timecode } from "@/lib/format";

function DetailPage() {
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  const assetId = params.id;
  const startAt = Number(search.get("t") ?? 0);

  const videoRef = useRef<HTMLVideoElement>(null);
  const cueRefs = useRef<Record<number, HTMLButtonElement | null>>({});
  const [asset, setAsset] = useState<AssetDetail | null>(null);
  const [transcript, setTranscript] = useState<Transcript | null>(null);
  const [scenes, setScenes] = useState<SceneFrame[]>([]);
  const [similar, setSimilar] = useState<VisualHit[]>([]);
  const [playhead, setPlayhead] = useState(0);
  const [activeCue, setActiveCue] = useState(-1);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .asset(assetId)
      .then(setAsset)
      .catch((err) => setError(err instanceof Error ? err.message : "Could not load asset"));
    api
      .transcript(assetId)
      .then(setTranscript)
      .catch(() => setTranscript(null)); // no transcript is a normal state
    api.scenes(assetId).then(setScenes).catch(() => setScenes([]));
    api.similar(assetId).then(setSimilar).catch(() => setSimilar([]));
  }, [assetId]);

  const seek = useCallback((seconds: number) => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = seconds;
    video.play().catch(() => undefined);
  }, []);

  useEffect(() => {
    if (startAt > 0 && videoRef.current) seek(startAt);
  }, [startAt, seek, asset]);

  function onTimeUpdate() {
    const video = videoRef.current;
    if (!video) return;
    setPlayhead(video.currentTime * 1000);
    if (!transcript) return;
    const ms = video.currentTime * 1000;
    const index = transcript.segments.findIndex(
      (seg) => ms >= seg.start_ms && ms < seg.end_ms,
    );
    if (index !== activeCue) {
      setActiveCue(index);
      cueRefs.current[index]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }

  async function copyPath() {
    if (!asset) return;
    await navigator.clipboard.writeText(asset.relative_path);
    setNotice("Path copied");
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

  if (error) return <div className="empty">{error}</div>;
  if (!asset) return <div className="empty">Loading…</div>;

  const isVideo = asset.media_type === "video";
  const isAudio = asset.media_type === "audio";

  return (
    <>
      <div style={{ marginBottom: 18 }}>
        <p className="eyebrow">{asset.media_type}</p>
        <h1 style={{ fontSize: "1.9rem", marginTop: 6 }}>{asset.title ?? asset.filename}</h1>
      </div>

      <div className="detail">
        <div>
          {isVideo && (
            <video
              ref={videoRef}
              className="player"
              controls
              preload="metadata"
              poster={mediaUrl(assetId, "poster")}
              src={mediaUrl(assetId, "proxy")}
              onTimeUpdate={onTimeUpdate}
            >
              <track kind="captions" src={mediaUrl(assetId, "subtitle")} default />
            </video>
          )}
          {isAudio && (
            <div className="card" style={{ padding: 18 }}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={mediaUrl(assetId, "waveform")}
                alt=""
                style={{ width: "100%", borderRadius: 8, display: "block" }}
                onError={(e) => ((e.target as HTMLImageElement).style.display = "none")}
              />
              <audio
                ref={videoRef as unknown as React.RefObject<HTMLAudioElement>}
                controls
                src={mediaUrl(assetId, "proxy")}
                onTimeUpdate={onTimeUpdate}
                style={{ width: "100%", marginTop: 14 }}
              />
            </div>
          )}
          {!isVideo && !isAudio && (
            // eslint-disable-next-line @next/next/no-img-element
            <img className="stillframe" src={mediaUrl(assetId, "preview")} alt={asset.filename} />
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

          {transcript && (
            <section style={{ marginTop: 26 }}>
              <div className="sectionhead" style={{ marginTop: 0 }}>
                <h2>Transcript</h2>
                <span className="faint mono">
                  {transcript.segment_count} cues · {transcript.language} ·{" "}
                  {transcript.model_name}
                </span>
              </div>
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
        </div>

        <aside style={{ display: "grid", gap: 16 }}>
          <FacesInPhoto assetId={asset.id} />
          <TagEditor assetId={asset.id} />
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
