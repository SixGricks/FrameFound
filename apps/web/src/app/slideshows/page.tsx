"use client";

// Slideshows: propose a selection, look at it, then render.
//
// The review step is the point. FrameFound picks well enough to save an hour
// of scrolling and not well enough to be trusted unseen — a themed selection
// will occasionally include someone the operator would rather leave out, and
// there has to be a moment where that can be caught. So the proposal is shown
// as thumbnails that can be removed individually, and nothing is written until
// "Render" is pressed.

import { useCallback, useEffect, useRef, useState } from "react";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import {
  api,
  type ProposedSlide,
  type Slideshow,
  type SlideshowProposal,
  type SlideshowTheme,
} from "@/lib/api";
import { shortDate } from "@/lib/format";

// While something is rendering, poll often enough that the slide counter
// visibly moves — a piece takes a few seconds.
const POLL_MS = 4000;

export default function SlideshowsPage() {
  const [themes, setThemes] = useState<SlideshowTheme[]>([]);
  const [theme, setTheme] = useState("plain");
  const [title, setTitle] = useState("");
  const [targetCount, setTargetCount] = useState(40);
  const [proposal, setProposal] = useState<SlideshowProposal | null>(null);
  const [chosen, setChosen] = useState<ProposedSlide[]>([]);
  const [shows, setShows] = useState<Slideshow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadShows = useCallback(async () => {
    try {
      setShows(await api.slideshows());
    } catch {
      setShows([]);
    }
  }, []);

  useEffect(() => {
    api.slideshowThemes().then(setThemes).catch(() => setThemes([]));
    loadShows();
  }, [loadShows]);

  // Poll only while something is actually in flight.
  const working = (shows ?? []).some(
    (s) => s.status === "rendering" || s.status === "pending",
  );
  useEffect(() => {
    if (!working) return;
    timer.current = setTimeout(loadShows, POLL_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [working, shows, loadShows]);

  async function propose() {
    setBusy(true);
    setError("");
    setProposal(null);
    try {
      const result = await api.proposeSlideshow({
        theme,
        target_count: targetCount,
      });
      setProposal(result);
      setChosen(result.slides);
    } catch {
      setError(
        "Nothing could be proposed. The photographs need visual indexing first — " +
          "check Processing.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function render() {
    setBusy(true);
    setError("");
    try {
      await api.createSlideshow({
        title,
        theme,
        // Only the ones that can actually be rendered. Sending a photograph
        // with no preview would queue a render that fails several minutes
        // later, which is a worse way to learn the same thing.
        asset_ids: chosen.filter((s) => s.has_preview).map((s) => s.asset_id),
      });
      setProposal(null);
      setChosen([]);
      setTitle("");
      await loadShows();
    } catch {
      setError("The render could not be queued.");
    } finally {
      setBusy(false);
    }
  }

  const renderable = chosen.filter((s) => s.has_preview).length;

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Slideshows</h2>
        {shows && <span className="faint mono">{shows.length} made</span>}
      </div>

      <p className="faint" style={{ maxWidth: "62ch" }}>
        FrameFound picks photographs that fit a theme, drops the near-duplicates
        from a burst, and makes sure the people you name actually appear. You
        look at what it chose before anything is rendered.
      </p>

      {error && <div className="empty">{error}</div>}

      <div className="card">
        <div className="field">
          <label htmlFor="ss-title">Title</label>
          <input
            id="ss-title"
            className="input"
            value={title}
            placeholder="Rainforest Falls VBS"
            onChange={(e) => setTitle(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="ss-theme">Theme</label>
          <select
            id="ss-theme"
            className="select"
            value={theme}
            onChange={(e) => setTheme(e.target.value)}
          >
            {themes.map((t) => (
              <option key={t.slug} value={t.slug}>
                {t.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="ss-count">How many photographs</label>
          <input
            id="ss-count"
            className="input"
            type="number"
            min={1}
            max={300}
            value={targetCount}
            onChange={(e) => setTargetCount(Number(e.target.value) || 1)}
          />
        </div>
        <button className="btn btn-primary" disabled={busy} onClick={propose}>
          {busy ? "Choosing…" : "Choose photographs"}
        </button>
      </div>

      {proposal && (
        <>
          <div className="sectionhead">
            <h3>Proposed — {chosen.length} photographs</h3>
            <span className="faint mono">
              {proposal.dropped_duplicates} near-duplicates dropped
            </span>
          </div>
          <p className="faint">{proposal.note}</p>
          {proposal.people_missing.length > 0 && (
            <p className="faint">
              {proposal.people_missing.length} of the people you asked for do not
              appear in any of these photographs.
            </p>
          )}

          <div className="grid">
            {chosen.map((slide) => (
              <figure className="tile" key={slide.asset_id}>
                <div className="tile-frame">
                  <Thumb
                    assetId={slide.asset_id}
                    mediaType={slide.media_type}
                    status="ready"
                  />
                </div>
                <figcaption className="tile-meta">
                  <span className="tile-name">{slide.filename}</span>
                  <span className="tile-sub">
                    {slide.captured_at ? shortDate(slide.captured_at) : "no date"}
                    {!slide.has_preview && " · no preview"}
                  </span>
                  <button
                    className="btn"
                    onClick={() =>
                      setChosen((rows) =>
                        rows.filter((r) => r.asset_id !== slide.asset_id),
                      )
                    }
                  >
                    Remove
                  </button>
                </figcaption>
              </figure>
            ))}
          </div>

          <div className="toolbar">
            <button
              className="btn btn-primary"
              disabled={busy || renderable === 0}
              onClick={render}
            >
              Render {renderable} photographs
            </button>
            <button className="btn" onClick={() => setProposal(null)}>
              Discard
            </button>
          </div>
          {renderable < chosen.length && (
            <p className="faint">
              {chosen.length - renderable} have no preview image yet and will be
              left out. Wait for processing to catch up if you want them in.
            </p>
          )}
        </>
      )}

      <h3>Rendered</h3>
      {!shows ? (
        <div className="empty">Loading…</div>
      ) : !shows.length ? (
        <div className="empty">
          Nothing rendered yet. Choose a theme above to make your first one.
        </div>
      ) : (
        <div className="tablewrap card" style={{ padding: 0 }}>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Title</th>
                <th scope="col">Photos</th>
                <th scope="col">Length</th>
                <th scope="col">Status</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {shows.map((show) => (
                <tr key={show.id}>
                  <th scope="row" style={{ fontWeight: 500 }}>
                    {show.title}
                    <span className="faint mono" style={{ display: "block" }}>
                      {shortDate(show.created_at)}
                    </span>
                  </th>
                  <td className="mono faint">{show.slide_count}</td>
                  <td className="mono faint">
                    {show.duration_seconds
                      ? `${Math.round(show.duration_seconds)}s`
                      : "—"}
                    {show.size_mb ? ` · ${show.size_mb} MB` : ""}
                  </td>
                  <td>
                    {show.status === "ready" && (
                      <span className="pill" data-tone="ok">
                        ready
                      </span>
                    )}
                    {show.status === "rendering" && (
                      <span className="pill">
                        {show.segments_done}/{show.slide_count}
                      </span>
                    )}
                    {show.status === "pending" && <span className="pill">queued</span>}
                    {show.status === "failed" && (
                      <span className="pill" data-tone="bad" title={show.error ?? ""}>
                        failed
                      </span>
                    )}
                  </td>
                  <td style={{ textAlign: "right" }}>
                    {show.video_url && (
                      <a className="btn" href={show.video_url} download>
                        Download
                      </a>
                    )}{" "}
                    <button
                      className="btn"
                      disabled={show.status === "rendering"}
                      onClick={async () => {
                        await api.rerenderSlideshow(show.id);
                        await loadShows();
                      }}
                    >
                      Re-render
                    </button>{" "}
                    <button
                      className="btn"
                      onClick={async () => {
                        await api.deleteSlideshow(show.id);
                        await loadShows();
                      }}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {shows?.some((s) => s.status === "failed") && (
        <p className="faint" style={{ maxWidth: "62ch" }}>
          A failed render keeps its reason — hover the badge. The most common
          cause is a photograph whose preview has not been generated yet.
        </p>
      )}
    </Shell>
  );
}
