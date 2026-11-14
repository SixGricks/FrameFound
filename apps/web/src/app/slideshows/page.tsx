"use client";

// Slideshows: propose a selection, look at it, then render.
//
// The review step is the point. FrameFound picks well enough to save an hour
// of scrolling and not well enough to be trusted unseen — a themed selection
// will occasionally include someone the operator would rather leave out, and
// there has to be a moment where that can be caught. So the proposal is shown
// as thumbnails that can be removed individually, and nothing is written until
// "Render" is pressed.
//
// Looking has to be possible for that to mean anything. The proposal used to
// be 212 px thumbnails and nothing else — who is in the third row cannot be
// read at that size — and a finished slideshow could only be downloaded. A
// photograph now opens in the viewer (MediaPreview), which steps through the
// proposal and can remove the one on screen; and a rendered slideshow plays
// here, under its row, as well as downloading.

import { Fragment, useCallback, useEffect, useRef, useState } from "react";

import LookFrame from "@/components/LookFrame";
import MediaPreview from "@/components/MediaPreview";
import Shell from "@/components/Shell";
import {
  api,
  type ProposedSlide,
  type Slideshow,
  type SlideshowProposal,
  type SlideshowTheme,
} from "@/lib/api";
import { shortDate } from "@/lib/format";
import { usePageTitle, usePlayerSource } from "@/lib/hooks";
import { urlStatus } from "@/lib/probe";

import { renderRefused } from "./slideshowWords";

// While something is rendering, poll often enough that the slide counter
// visibly moves — a piece takes a few seconds.
const POLL_MS = 4000;

/**
 * Why a rendered slideshow would not play, from the status the server gives
 * for its file. The player only says "error", and one sentence for every
 * case sent the operator the wrong way: with the drive that holds the
 * renders merely away, "re-render it if the file has gone" had them press
 * Re-render — which takes Watch and Download off a video that is perfectly
 * good, and then fails, because the drive it would write to is the one that
 * is away.
 */
function whyUnplayable(status: number): string {
  if (status === 503) {
    return (
      "The drive that holds the rendered slideshows is not connected, so this one cannot be " +
      "played or downloaded right now. The video is still on that drive — nothing needs " +
      "re-rendering. It will play again when the drive is back."
    );
  }
  if (status === 404) {
    return "The rendered video is no longer on the drive. Re-render to make it again.";
  }
  if (status >= 200 && status < 300) {
    // The file is there and was served; the player still refused it.
    return "This browser could not play the video. Download it to watch it.";
  }
  // Signed out, offline, the server down: nothing specific can be said.
  return "This slideshow cannot be played here right now.";
}

/**
 * A rendered slideshow playing under its row. A component of its own so that
 * the player is emptied when it closes (usePlayerSource): a <video> taken
 * off the page keeps its request for the file open, and a rendered slideshow
 * is the largest file this page serves.
 */
function InlinePlayer({
  src,
  label,
  onFail,
}: {
  src: string;
  label: string;
  onFail: () => void;
}) {
  const player = usePlayerSource<HTMLVideoElement>(src);
  return (
    <video
      ref={player}
      className="player inline-player"
      controls
      autoPlay
      playsInline
      preload="metadata"
      aria-label={label}
      onError={onFail}
    />
  );
}

export default function SlideshowsPage() {
  usePageTitle("Slideshows");
  const [themes, setThemes] = useState<SlideshowTheme[]>([]);
  const [theme, setTheme] = useState("plain");
  const [title, setTitle] = useState("");
  const [targetCount, setTargetCount] = useState(40);
  const [proposal, setProposal] = useState<SlideshowProposal | null>(null);
  const [chosen, setChosen] = useState<ProposedSlide[]>([]);
  const [shows, setShows] = useState<Slideshow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // The proposed photograph open in the viewer — its asset id, not its
  // place in the list: removing one shifts every index after it.
  const [viewing, setViewing] = useState<string | null>(null);
  // The rendered slideshow playing on the page, and the one that would not
  // (with the reason, once the server has been asked for it).
  const [watching, setWatching] = useState<string | null>(null);
  const [unplayable, setUnplayable] = useState<{ id: string; text: string } | null>(null);
  // The slideshow a Re-render or Delete is being sent for, and the one whose
  // request was refused, with the server's reason. Said in that row: the
  // list can be long, and the page's own message is at the top.
  const [acting, setActing] = useState<string | null>(null);
  const [rowProblem, setRowProblem] = useState<{ id: string; text: string } | null>(null);
  const proposalHead = useRef<HTMLHeadingElement>(null);
  // Render's refusal, said beside the button rather than at the top of the
  // page, and where focus goes after pressing it: the button disables while
  // the request is out, which dropped focus to the page.
  const [renderProblem, setRenderProblem] = useState("");
  const renderButton = useRef<HTMLButtonElement>(null);
  const renderedHead = useRef<HTMLHeadingElement>(null);
  const focusAfterRender = useRef<"button" | "rendered" | null>(null);
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

  // Once the button is enabled again: back on Render after a refusal, or on
  // the Rendered list, where the new row is, once it was taken.
  useEffect(() => {
    if (busy || !focusAfterRender.current) return;
    const where = focusAfterRender.current;
    focusAfterRender.current = null;
    (where === "button" ? renderButton.current : renderedHead.current)?.focus();
  }, [busy]);

  async function propose() {
    setBusy(true);
    setError("");
    setRenderProblem("");
    setProposal(null);
    setViewing(null);
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
    setRenderProblem("");
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
      setViewing(null);
      setTitle("");
      focusAfterRender.current = "rendered";
      await loadShows();
    } catch (err) {
      setRenderProblem(renderRefused(err instanceof Error ? err.message : null));
      focusAfterRender.current = "button";
      // The list as the server has it, so the count above is never stale.
      await loadShows();
    } finally {
      setBusy(false);
    }
  }

  const renderable = chosen.filter((s) => s.has_preview).length;

  /** Take one photograph out of the proposal. */
  function removeSlide(assetId: string) {
    // The viewer, if it is showing this one, moves on rather than closing:
    // to the next photograph, or at the end to the one before. Chosen from
    // the list as it is now — once the row is gone the indexes have shifted.
    const at = chosen.findIndex((s) => s.asset_id === assetId);
    const next = chosen[at + 1] ?? chosen[at - 1];
    setChosen((rows) => rows.filter((r) => r.asset_id !== assetId));
    setViewing((current) => (current === assetId ? (next?.asset_id ?? null) : current));
  }

  /** The inline player gave up on `show`: find out why, and say that. */
  async function cannotPlay(show: Slideshow) {
    setUnplayable({ id: show.id, text: whyUnplayable(0) });
    if (!show.video_url) return;
    const status = await urlStatus(show.video_url);
    // Only if this is still the one that would not play: the player may
    // have been closed, or another opened, while the server was asked.
    setUnplayable((current) =>
      current?.id === show.id ? { id: show.id, text: whyUnplayable(status) } : current,
    );
  }

  /**
   * Re-render or delete one slideshow. Both used to await the request with
   * nothing around it, after closing the player: with the drive that holds
   * the renders away the server refuses either (503, with a sentence saying
   * so), and the operator saw the player shut, the row stay, and no word of
   * why. The player now closes only once the request has gone through, and
   * a refusal is shown in the row with the server's reason.
   */
  async function change(show: Slideshow, how: "rerender" | "delete") {
    setActing(show.id);
    setRowProblem(null);
    try {
      if (how === "rerender") await api.rerenderSlideshow(show.id);
      else await api.deleteSlideshow(show.id);
      // Closed before the list is read again: left open through a
      // re-render, the player would come back and start by itself when the
      // new video was ready.
      setWatching((current) => (current === show.id ? null : current));
      await loadShows();
    } catch (err) {
      const why = err instanceof Error ? err.message : "the server could not be reached";
      setRowProblem({
        id: show.id,
        text: `${how === "rerender" ? "Not re-rendered" : "Not deleted"}: ${why}`,
      });
    } finally {
      setActing(null);
    }
  }

  // The photograph in the viewer, looked up in the proposal as it is now, so
  // one that has been removed is simply not found and the viewer is not
  // drawn.
  const viewAt = proposal && viewing ? chosen.findIndex((s) => s.asset_id === viewing) : -1;
  const viewed = viewAt >= 0 ? chosen[viewAt] : undefined;

  /** Where focus goes when the viewer closes: the tile last on screen, or
   *  the head of the proposal when that tile was removed. */
  function focusAfterViewing(): HTMLElement | null {
    const tile = viewed
      ? document.querySelector<HTMLElement>(`[data-look="${viewed.asset_id}"]`)
      : null;
    return tile ?? proposalHead.current;
  }

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

      {error && (
        <div className="empty" role="alert">
          {error}
        </div>
      )}

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
            <h3 ref={proposalHead} tabIndex={-1}>
              Proposed — {chosen.length} photographs
            </h3>
            <span className="faint mono">
              {proposal.dropped_duplicates} near-duplicates dropped · click a photograph to
              see it large
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
                <LookFrame
                  assetId={slide.asset_id}
                  mediaType={slide.media_type}
                  filename={slide.filename}
                  onLook={() => setViewing(slide.asset_id)}
                />
                <figcaption className="tile-meta">
                  <span className="tile-name">{slide.filename}</span>
                  <span className="tile-sub">
                    {slide.captured_at ? shortDate(slide.captured_at) : "no date"}
                    {!slide.has_preview && " · no preview"}
                  </span>
                  <button className="btn" onClick={() => removeSlide(slide.asset_id)}>
                    Remove
                  </button>
                </figcaption>
              </figure>
            ))}
          </div>

          <div className="toolbar">
            <button
              ref={renderButton}
              className="btn btn-primary"
              disabled={busy || renderable === 0}
              onClick={render}
            >
              Render {renderable} photographs
            </button>
            <button
              className="btn"
              onClick={() => {
                setProposal(null);
                setViewing(null);
              }}
            >
              Discard
            </button>
          </div>
          {renderProblem && (
            <p className="faint" role="alert">
              {renderProblem}
            </p>
          )}
          {renderable < chosen.length && (
            <p className="faint">
              {chosen.length - renderable} have no preview image yet and will be
              left out. Wait for processing to catch up if you want them in.
            </p>
          )}
          {viewed && (
            // Mounted once, beside the grid — never inside a tile, whose
            // `overflow: hidden` and hover transform would clip it.
            <MediaPreview
              assetId={viewed.asset_id}
              mediaType={viewed.media_type}
              filename={viewed.filename}
              detail={[
                viewed.captured_at ? shortDate(viewed.captured_at) : "no date",
                viewed.people.length ? `with ${viewed.people.join(", ")}` : null,
              ]
                .filter(Boolean)
                .join(" · ")}
              position={`${viewAt + 1} of ${chosen.length}`}
              status={
                viewed.has_preview
                  ? null
                  : "No preview image yet, so this one will be left out of the render."
              }
              onClose={() => setViewing(null)}
              onPrev={
                viewAt > 0 ? () => setViewing(chosen[viewAt - 1]?.asset_id ?? null) : undefined
              }
              onNext={
                viewAt < chosen.length - 1
                  ? () => setViewing(chosen[viewAt + 1]?.asset_id ?? null)
                  : undefined
              }
              returnFocus={focusAfterViewing}
            >
              <button
                type="button"
                className="btn"
                onClick={() => removeSlide(viewed.asset_id)}
              >
                Remove from the slideshow
              </button>
            </MediaPreview>
          )}
        </>
      )}

      <h3 ref={renderedHead} tabIndex={-1}>
        Rendered
      </h3>
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
                <Fragment key={show.id}>
                  <tr>
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
                        // Watching used to mean downloading the file and
                        // finding it again. It plays in the row underneath.
                        <button
                          className="btn"
                          aria-expanded={watching === show.id}
                          onClick={() => {
                            setUnplayable(null);
                            setWatching((current) => (current === show.id ? null : show.id));
                          }}
                        >
                          {watching === show.id ? "Close player" : "Watch"}
                        </button>
                      )}{" "}
                      {show.video_url && (
                        <a className="btn" href={show.video_url} download>
                          Download
                        </a>
                      )}{" "}
                      <button
                        className="btn"
                        disabled={show.status === "rendering" || acting === show.id}
                        onClick={() => change(show, "rerender")}
                      >
                        Re-render
                      </button>{" "}
                      <button
                        className="btn"
                        disabled={acting === show.id}
                        onClick={() => {
                          if (!window.confirm("Delete this slideshow and its rendered video?")) return;
                          change(show, "delete");
                        }}
                      >
                        Delete
                      </button>
                      {rowProblem?.id === show.id && (
                        <div
                          role="alert"
                          style={{
                            maxWidth: 380,
                            margin: "6px 0 0 auto",
                            fontSize: "0.8rem",
                            color: "var(--ember)",
                          }}
                        >
                          {rowProblem.text}
                        </div>
                      )}
                    </td>
                  </tr>
                  {watching === show.id && show.video_url && (
                    <tr>
                      <td colSpan={5}>
                        {unplayable?.id === show.id ? (
                          <p className="faint" role="alert" style={{ margin: 0 }}>
                            {unplayable.text}
                          </p>
                        ) : (
                          // The list is re-read while anything renders; the
                          // row keeps its key, so playback is not interrupted.
                          <InlinePlayer
                            src={show.video_url}
                            label={`${show.title}, the rendered slideshow`}
                            onFail={() => cannotPlay(show)}
                          />
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
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
