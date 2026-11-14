"use client";

// Tags: what the system has learned, and the queue of things it wants checked.
//
// Reviewing suggestions in bulk is much faster than opening assets one at a
// time, and every answer — yes or no — feeds straight back into the match. The
// threshold and its reason are shown because an operator who sees a strange
// suggestion deserves to know why it cleared the bar.
//
// Suggestions now come from three places — the CLIP learner, the Claude
// reviewer on the server, and Claude through the MCP connector — and each tile
// says which, because a second opinion is weighed differently from a match.
//
// Bulk review works as it does for faces (people/[id]): the queue is ranked
// strongest first, so there is usually one line between yes and no, and the
// operator's job is to draw it — "down to here", shift-click for a range,
// the tick for the odd one — then answer the selection once (or press Y / N).
// One request answers them all and the tag re-learns once; answering sixty
// tiles one at a time used to queue sixty full-library re-learns.
//
// The picture opens the footage. Bulk review first shipped (2026-11-05) with
// the picture as the select toggle, on the assumption above that nobody needs
// to open an asset to answer — and "is this Power Broom?" about a long
// auction clip can only be answered by playing it. The one route left, the
// filename, left the page and took the open queue and the selection with it.
// A click on the picture now opens the viewer over the grid (TagViewer, the
// shared MediaPreview): it plays the clip, steps through the queue, and answers or selects from
// there, and nothing on this page is lost by looking. Selecting has its own
// control, the tick in the picture's corner.
//
// A fourth source since 2026-11-13: a model on this Mac (origin "local"),
// from a folder review or from new footage in a library switched on for it.
// Its suggestions are answered exactly as the others are — same tile, same
// tick, same keys, same viewer. Two things differ, because it was shown one
// shot of a clip rather than the clip: the tile's picture is that shot, and
// the viewer starts the clip there, so "is this a loading dock?" is asked
// about the seconds the model saw and not about the clip's first frame.

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import Shell from "@/components/Shell";
import TagViewer from "@/components/TagViewer";
import Thumb from "@/components/Thumb";
import {
  api,
  mediaUrl,
  type ClaudeReviewStatus,
  type LookStatus,
  type PendingSuggestion,
  type TagSummary,
} from "@/lib/api";
import { setChatContext } from "@/lib/chatContext";
import { relativeTime, shortDate, timecode } from "@/lib/format";
import { typingInto } from "@/lib/keys";
import {
  LOCAL_SUGGESTIONS_FROM,
  localWaitingText,
  momentPicture,
  momentSeconds,
  suggestedBy,
} from "@/lib/looks";
import { usePageTitle } from "@/lib/hooks";
import { count as countOf } from "@/lib/storage";

const ORIGIN_LABEL: Record<string, string> = {
  clip: "match",
  claude: "Claude",
  mcp: "Claude (chat)",
  local: "local model",
};

function money(value: number): string {
  return `$${value.toFixed(2)}`;
}

/** " · at 00:08" for a suggestion about a moment of a clip; "" for the rest. */
function momentNote(row: PendingSuggestion): string {
  return row.at_ms != null && momentPicture(row) ? ` · at ${timecode(row.at_ms)}` : "";
}

/**
 * The line under a tile: "suggested by match", as it has always read, and
 * for a local model "suggested by local model · at 00:08", with the model's
 * name as the tooltip and "(made up)" when the demonstration invented it.
 */
function SuggestedBy({ row }: { row: PendingSuggestion }) {
  const who = suggestedBy(row, ORIGIN_LABEL);
  return (
    <>
      suggested by {who.title ? <span title={who.title}>{who.label}</span> : who.label}
      {momentNote(row)}
    </>
  );
}

/**
 * A suggestion's picture. One about a moment of a clip shows that moment.
 * When that picture cannot be had (no frame was kept at that moment, or its
 * drive is away) the tile shows what it has always shown, the clip's own
 * thumbnail: a clip that has a picture must never read "No preview" because
 * a better one was asked for.
 */
function SuggestionPicture({ row }: { row: PendingSuggestion }) {
  const moment = momentPicture(row);
  // Which moment failed, so another moment of the same clip is tried afresh.
  const [failed, setFailed] = useState<string | null>(null);
  if (moment && failed !== moment) {
    return (
      // Served by our own session-guarded endpoint, as Thumb's pictures are.
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={mediaUrl(row.asset_id, moment)}
        alt=""
        loading="lazy"
        decoding="async"
        onError={() => setFailed(moment)}
      />
    );
  }
  return <Thumb assetId={row.asset_id} mediaType={row.media_type} status="ready" />;
}

export default function TagsPage() {
  usePageTitle("Tags");
  const [tags, setTags] = useState<TagSummary[] | null>(null);
  const [open, setOpen] = useState<TagSummary | null>(null);
  const [pending, setPending] = useState<PendingSuggestion[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [review, setReview] = useState<ClaudeReviewStatus | null>(null);
  // Whether a model on this Mac can suggest tags, and how many of its
  // suggestions wait. Null until the server says (and on a server from
  // before local models): the local card is then not drawn at all.
  const [local, setLocal] = useState<LookStatus | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Bulk review: the chosen asset ids, and the last tile clicked (the
  // anchor a shift-click extends from) — kept as an asset id, because
  // answered tiles leave the grid and every index after them shifts.
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [anchor, setAnchor] = useState<string | null>(null);
  const [answer, setAnswer] = useState<string | null>(null);
  // Whether `answer` is a failure to save: shown as an error here, and in
  // the viewer, which covers this page's own message.
  const [answerFailed, setAnswerFailed] = useState(false);
  // The suggestion open in the viewer — an asset id for the reason `anchor`
  // is one: an answered tile leaves the queue and every index after it
  // shifts.
  const [viewing, setViewing] = useState<string | null>(null);
  // What a row's button last did (Re-learn, a queue that would not open),
  // shown in that row: the table can be several screens long, and a banner
  // above it would be out of sight. Re-learn used to fire its request and
  // show nothing — a press looked dead, and a failure was an unhandled
  // rejection nobody saw.
  const [rowNote, setRowNote] = useState<{ tagId: string; ok: boolean; text: string } | null>(
    null,
  );
  const [relearning, setRelearning] = useState<string | null>(null);
  // A tag whose re-learn was just asked for, and the "Learned" time it had.
  const [watching, setWatching] = useState<{
    id: string;
    before: string | null;
    until: number;
  } | null>(null);
  // Counts the openings of a queue, to bring each one into view (below).
  const [opened, setOpened] = useState(0);
  const reviewHead = useRef<HTMLDivElement>(null);
  const reviewTitle = useRef<HTMLHeadingElement>(null);
  // Which opening of the review a response belongs to. A slow answer or
  // queue fetch for one tag must not land on the next tag opened — nor on
  // the same tag opened again (A, then B, then A).
  const opening = useRef(0);
  const localAsked = useRef(0);

  const load = useCallback(async () => {
    // Not waited for: every answer waits on this load before its tiles leave
    // the grid, and a count on a card is not worth a moment of that. A read
    // that fails leaves the card as it was, and only the newest one draws.
    const mine = ++localAsked.current;
    api
      .lookStatus()
      .then((next) => {
        if (mine === localAsked.current) setLocal(next);
      })
      .catch(() => undefined);
    try {
      setTags(await api.tags());
    } catch {
      setTags([]);
    }
    try {
      setReview(await api.claudeReviewStatus());
    } catch {
      setReview(null);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Keep the open tag's numbers ("N confirmed examples so far") in step
  // with the table after answers land; it was a snapshot from opening.
  useEffect(() => {
    setOpen((current) =>
      current ? (tags?.find((t) => t.id === current.id) ?? current) : null,
    );
  }, [tags]);

  // A run in progress: refresh its numbers until it finishes.
  useEffect(() => {
    if (review?.last.state !== "running") return;
    const timer = setInterval(() => {
      api.claudeReviewStatus().then(setReview).catch(() => undefined);
    }, 5000);
    return () => clearInterval(timer);
  }, [review?.last.state]);

  // After Re-learn, re-read the table until the tag's "Learned" time moves,
  // so the result arrives by itself rather than after a reload. The learner
  // commits that time and its new suggestions together, so the count read
  // with it is the finished one. It gives up after a minute and a half and
  // says so: the learner may be busy, and the server accepts the request
  // even when its queue is down.
  useEffect(() => {
    if (!watching) return;
    let live = true;
    const timer = setInterval(async () => {
      if (document.hidden) return;
      let rows: TagSummary[];
      try {
        rows = await api.tags();
      } catch {
        return;
      }
      if (!live) return;
      setTags(rows);
      const now = rows.find((t) => t.id === watching.id);
      if (!now) {
        setWatching(null);
      } else if (now.learned_at !== watching.before) {
        setWatching(null);
        setRowNote({
          tagId: now.id,
          ok: true,
          text: `Re-learned. ${
            now.pending_count
              ? `${now.pending_count} waiting for review.`
              : "Nothing new to review."
          }`,
        });
      } else if (Date.now() > watching.until) {
        setWatching(null);
        setRowNote({
          tagId: now.id,
          ok: true,
          text: "Not finished yet. It carries on by itself; the result shows here the next time this page is opened.",
        });
      }
    }, 4000);
    return () => {
      live = false;
      clearInterval(timer);
    };
  }, [watching]);

  const openTag = useCallback(async (tag: TagSummary) => {
    const ticket = ++opening.current;
    setOpen(tag);
    setPending(null);
    setChosen(new Set());
    setAnchor(null);
    setAnswer(null);
    setAnswerFailed(false);
    setViewing(null);
    setOpened((count) => count + 1);
    try {
      const rows = await api.pendingForTag(tag.id);
      if (ticket === opening.current) setPending(rows);
    } catch (err) {
      // Left unhandled, a failed fetch sat on "Loading…" for ever.
      if (ticket !== opening.current) return;
      setOpen(null);
      setRowNote({
        tagId: tag.id,
        ok: false,
        text: `Could not open the queue${err instanceof Error ? `: ${err.message}` : ""}`,
      });
    }
  }, []);

  // Review opens the queue below a tag table that can run to several
  // screens, so the press looked as if it had done nothing. Bring the queue
  // into view, and move focus to it so the keyboard carries on from the
  // queue rather than from the button far above it. It scrolls twice: at
  // once, and again when the tiles arrive — while the queue is still
  // "Loading…" the page is too short for its heading to reach the top, so
  // the first scroll can only go part of the way.
  const loaded = pending !== null;
  const focusedFor = useRef(0);
  useEffect(() => {
    if (!opened) return;
    const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    reviewHead.current?.scrollIntoView({ block: "start", behavior: calm ? "auto" : "smooth" });
    if (focusedFor.current !== opened) {
      focusedFor.current = opened;
      reviewTitle.current?.focus({ preventScroll: true });
    }
  }, [opened, loaded]);

  async function relearn(tag: TagSummary) {
    setRelearning(tag.id);
    setRowNote(null);
    try {
      await api.relearnTag(tag.id);
      setRowNote({
        tagId: tag.id,
        ok: true,
        text: "Re-learning. It takes a minute or two; this row updates when it is done.",
      });
      setWatching({ id: tag.id, before: tag.learned_at, until: Date.now() + 90_000 });
    } catch (err) {
      setRowNote({
        tagId: tag.id,
        ok: false,
        text: `Could not start re-learning${err instanceof Error ? `: ${err.message}` : ""}`,
      });
    } finally {
      setRelearning(null);
    }
  }

  /**
   * Answer suggestions of the open tag: `accept` are it, `reject` are not.
   * One request for any number, then the answered tiles leave the queue
   * rather than the queue being refetched — the operator is working down a
   * list and should not lose their place. `thenView` is the suggestion the
   * viewer moves to if the one it is showing was among those answered.
   */
  const decide = useCallback(
    async (accept: string[], reject: string[], thenView: string | null = null) => {
      if (!open || (!accept.length && !reject.length)) return;
      const ticket = opening.current;
      setBusy(true);
      setAnswer(null);
      setAnswerFailed(false);
      try {
        const result = await api.decideTags(open.id, { accept, reject });
        await load();
        // Another tag (or this one, afresh) was opened while this was
        // saving: the answers are recorded, but this grid is not theirs.
        if (ticket !== opening.current) return;
        const done = new Set([...accept, ...reject]);
        setPending((rows) => (rows ?? []).filter((r) => !done.has(r.asset_id)));
        // Only the answered tiles leave the selection: a single Yes on one
        // tile must not throw away a bulk selection being built elsewhere.
        setChosen(
          (current) => new Set([...current].filter((id) => !done.has(id))),
        );
        setAnchor((current) => (current && done.has(current) ? null : current));
        // The viewer follows its clip out of the queue: on to the one the
        // caller named, or shut. Set here, together with the queue, so no
        // render has the viewer pointing at a tile that has gone.
        setViewing((current) => (current && done.has(current) ? thenView : current));
        const parts = [];
        if (result.accepted) parts.push(`${result.accepted} confirmed`);
        if (result.rejected) parts.push(`${result.rejected} rejected`);
        if (result.skipped)
          parts.push(`${result.skipped} already answered elsewhere`);
        setAnswer(
          `${parts.join(", ") || "Nothing changed"}. ${open.name} is re-learning.`,
        );
      } catch (err) {
        // Shown after "Not saved:", here and in the viewer.
        setAnswer(err instanceof Error ? err.message : "the answers could not be sent");
        setAnswerFailed(true);
      } finally {
        setBusy(false);
      }
    },
    [open, load],
  );

  /** Toggle one tile, or a range from the last click when shift is held. */
  function choose(index: number, shift: boolean) {
    const list = pending ?? [];
    const target = list[index];
    if (!target) return;
    const anchored = anchor ? list.findIndex((r) => r.asset_id === anchor) : -1;
    const from = shift && anchored >= 0 ? anchored : index;
    setChosen((current) => {
      const next = new Set(current);
      const [lo, hi] = from <= index ? [from, index] : [index, from];
      const adding = !next.has(target.asset_id);
      for (let i = lo; i <= hi; i += 1) {
        const row = list[i];
        if (!row) continue;
        if (adding) next.add(row.asset_id);
        else next.delete(row.asset_id);
      }
      return next;
    });
    setAnchor(target.asset_id);
  }

  /** Everything from the strongest suggestion down to and including `index`. */
  function chooseDownTo(index: number) {
    setChosen(
      new Set((pending ?? []).slice(0, index + 1).map((r) => r.asset_id)),
    );
    setAnchor((pending ?? [])[index]?.asset_id ?? null);
  }

  const selectedIds = (pending ?? [])
    .filter((r) => chosen.has(r.asset_id))
    .map((r) => r.asset_id);
  const otherIds = (pending ?? [])
    .filter((r) => !chosen.has(r.asset_id))
    .map((r) => r.asset_id);

  // For the chat panel: the queue that is open and what is ticked in it,
  // so "are these really silos?" means these. Joined into one string for
  // the effect, so it runs when the selection changes and not on every
  // render (the array is new each time).
  const queueTag = open?.name ?? "";
  const waiting = pending?.length ?? null;
  const ticked = selectedIds.join(" ");
  useEffect(() => {
    if (!queueTag) return;
    return setChatContext({
      title:
        waiting === null
          ? `Tags — reviewing “${queueTag}”`
          : `Tags — reviewing “${queueTag}”, ${waiting} suggestion${waiting === 1 ? "" : "s"} waiting`,
      tag: queueTag,
      selection: ticked ? ticked.split(" ") : [],
    });
  }, [queueTag, waiting, ticked]);

  // The suggestion in the viewer, looked up in the queue as it is now: one
  // that has since been answered is simply not found, and the viewer is not
  // drawn, rather than left open on a tile that has gone.
  const viewAt = viewing && pending ? pending.findIndex((r) => r.asset_id === viewing) : -1;
  const viewed = viewAt >= 0 ? pending?.[viewAt] : undefined;
  const looking = viewed !== undefined;

  /** Move the viewer up or down the queue. */
  function step(by: number) {
    const target = pending?.[viewAt + by];
    if (target) setViewing(target.asset_id);
  }

  /** Answer the suggestion in the viewer, and show the next one. */
  function answerViewed(accept: boolean) {
    if (!viewed || busy) return;
    // Picked before the save: once this row leaves the queue every index
    // after it shifts. The next one down; at the end, the one above.
    const next = pending?.[viewAt + 1] ?? pending?.[viewAt - 1];
    const id = viewed.asset_id;
    decide(accept ? [id] : [], accept ? [] : [id], next?.asset_id ?? null);
  }

  /**
   * Where focus goes when the viewer closes: the picture of the suggestion
   * last on screen, so the reader is back where they were looking — or, when
   * that tile was answered and has gone, the head of the queue.
   */
  function focusAfterViewing(): HTMLElement | null {
    const tile = viewed
      ? document.querySelector<HTMLElement>(`[data-look="${viewed.asset_id}"]`)
      : null;
    return tile ?? reviewTitle.current;
  }

  // Y / N / Esc answer or clear the selection — the hands stay on the
  // mouse and the keyboard while working down a long queue.
  useEffect(() => {
    if (!open) return;
    function onKey(event: KeyboardEvent) {
      // The viewer is open: the keys are about the clip on screen and the
      // viewer handles them. Left to run, Y would answer the selection
      // behind it as well, and the Esc that closes it would also clear
      // the selection.
      if (looking) return;
      // Typing is not a shortcut — but a tick box is not typing. This used
      // to skip every <input>, and the tick that selects a tile is one: a
      // click on it leaves focus there, so "tick a few, press Y" did nothing
      // until the reader clicked somewhere else.
      if (typingInto(event.target)) return;
      const el = event.target instanceof Element ? event.target : null;
      // A held key repeats. Esc held a moment too long closed the viewer
      // (its first press) and then, the viewer gone, cleared the selection
      // behind it with the repeats; a held Y answered the next selection
      // made.
      if (event.metaKey || event.ctrlKey || event.altKey || event.repeat || busy) return;
      // A key the page's menu already used (Esc closing Manage), or one
      // pressed while focus is in the top bar or the chat panel, is not
      // meant for the grid: Y on the panel's Approve button must not also
      // answer forty tiles. (The panel stops its own keys too; this holds
      // if it ever does not.)
      if (event.defaultPrevented || el?.closest('.topbar, [role="menu"], [data-chat-panel]'))
        return;
      const key = event.key.toLowerCase();
      if (key === "escape") {
        setChosen(new Set());
        setAnchor(null);
      } else if (key === "y" && selectedIds.length) {
        event.preventDefault();
        decide(selectedIds, []);
      } else if (key === "n" && selectedIds.length) {
        event.preventDefault();
        decide([], selectedIds);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, selectedIds, decide, looking]);

  async function startReview() {
    setBusy(true);
    setNotice(null);
    try {
      const result = await api.startClaudeReview({});
      setNotice(result.message);
      await load();
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not start the review");
    } finally {
      setBusy(false);
    }
  }

  const running = review?.last.state === "running";
  const toReview = review ? Math.min(review.untagged, review.batch) : 0;
  // How the tag table's links to Browse open. With a queue open this page is
  // holding things a link followed in this tab would throw away — the queue,
  // and whatever is ticked in it — so they open beside it, as the queue's
  // own links do. With nothing open there is nothing to lose, and a link
  // behaves as a link.
  const aside = open ? ({ target: "_blank", rel: "noreferrer" } as const) : {};

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Tags</h2>
        {tags && (
          <span className="faint mono">
            {countOf(tags.length, "tag")} ·{" "}
            {tags.reduce((n, t) => n + t.pending_count, 0)} waiting for review
          </span>
        )}
      </div>

      {review && (
        <div className="card">
          <div style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap" }}>
            <div style={{ flex: 1, minWidth: 260 }}>
              <strong>A second opinion from Claude</strong>
              <p className="faint" style={{ margin: "4px 0 0", fontSize: "0.86rem" }}>
                {review.configured ? (
                  <>
                    {review.untagged} picture{review.untagged === 1 ? "" : "s"} nobody has
                    tagged · {money(review.spent_this_month_usd)} of {money(review.budget_usd)}{" "}
                    used this month · {review.model}
                    {review.auto ? " · runs nightly" : ""}
                  </>
                ) : (
                  <>
                    Add an Anthropic key under Security and Claude can look at untagged
                    pictures and propose tags, under a monthly budget you set.
                  </>
                )}
              </p>
              {review.last.state !== "idle" && (
                <p className="faint mono" style={{ margin: "6px 0 0", fontSize: "0.78rem" }}>
                  last run {running ? "running" : review.last.state}
                  {review.last.finished_at && !running
                    ? ` ${relativeTime(review.last.finished_at)}`
                    : ""}
                  : {review.last.reviewed} reviewed, {review.last.suggested} suggestions
                  {review.last.new_tags ? ` (${review.last.new_tags} new tags)` : ""},{" "}
                  {money(review.last.spent_usd)}
                  {review.last.message ? ` — ${review.last.message}` : ""}
                </p>
              )}
              {notice && (
                <p className="faint" style={{ margin: "6px 0 0", fontSize: "0.82rem" }}>
                  {notice}
                </p>
              )}
            </div>
            {review.configured && (
              <button
                className="btn btn-primary"
                disabled={busy || running || toReview === 0 || review.remaining_usd <= 0}
                onClick={startReview}
                title={
                  review.remaining_usd <= 0
                    ? "This month's budget is used up — raise it under Security"
                    : undefined
                }
              >
                {running ? "Reviewing…" : `Review ${toReview} with Claude`}
              </button>
            )}
          </div>
        </div>
      )}

      {/* Shown where a model on this Mac can suggest tags, or where some of
          its suggestions still wait (a catalogue brought over from the Mac).
          Anywhere else there is nothing to say and the page is as it was. */}
      {local && (local.available || local.pending_suggestions > 0) && (
        <div className="card" style={{ marginTop: review ? 12 : undefined, marginBottom: 14 }}>
          <div style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap" }}>
            <div style={{ flex: 1, minWidth: 260 }}>
              <strong>Suggestions from the local model</strong>
              <p className="faint" style={{ margin: "4px 0 0", fontSize: "0.86rem" }}>
                {localWaitingText(local.pending_suggestions)} {LOCAL_SUGGESTIONS_FROM}
                {local.mode === "demo" && " In this demonstration they are made up."}
              </p>
            </div>
            {/* In a new tab while a queue is open, as the table's links are. */}
            <Link className="btn" href="/browse?view=folders" {...aside}>
              Browse › Folders
            </Link>
            <Link className="btn" href="/models" {...aside}>
              AI models
            </Link>
          </div>
        </div>
      )}

      {!tags ? (
        <div className="empty">Loading…</div>
      ) : !tags.length ? (
        <div className="empty">
          No tags yet. Open a video, add a tag like <strong>Power Broom</strong>,
          and FrameFound will start looking for it in everything else.
        </div>
      ) : (
        <div className="tablewrap card" style={{ padding: 0 }}>
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Tag</th>
                <th scope="col">Tagged</th>
                <th scope="col">To review</th>
                <th scope="col">Match bar</th>
                <th scope="col">Learned</th>
                <th scope="col" />
              </tr>
            </thead>
            <tbody>
              {tags.map((tag) => (
                <tr key={tag.id}>
                  {/* Both lead to Browse filtered by the tag. They were plain
                      text, and after a bulk Yes there was no way from here
                      to see what had just been tagged. While a queue is open
                      they open a new tab (`aside`): followed in this one,
                      they took the queue and a half-built selection with
                      them, and Back brought neither home. */}
                  <td>
                    <Link
                      className="linkish"
                      href={`/browse?tag=${encodeURIComponent(tag.slug)}`}
                      title={`Browse everything tagged ${tag.name}${
                        open ? ", in a new tab" : ""
                      }`}
                      {...aside}
                    >
                      <strong>{tag.name}</strong>
                    </Link>
                  </td>
                  <td className="mono">
                    {tag.asset_count > 0 ? (
                      <Link
                        className="linkish"
                        href={`/browse?tag=${encodeURIComponent(tag.slug)}`}
                        aria-label={`${tag.asset_count} tagged ${tag.name} — browse them${
                          open ? " in a new tab" : ""
                        }`}
                        {...aside}
                      >
                        {tag.asset_count}
                      </Link>
                    ) : (
                      0
                    )}
                  </td>
                  <td>
                    {tag.pending_count > 0 ? (
                      <span className="pill" data-tone="warn">
                        {tag.pending_count}
                      </span>
                    ) : (
                      <span className="faint">—</span>
                    )}
                  </td>
                  <td className="mono" title={tag.threshold_reason}>
                    {tag.threshold != null ? tag.threshold.toFixed(3) : "—"}
                    {tag.threshold_reason && (
                      <div className="faint" style={{ fontSize: "0.72rem" }}>
                        {tag.threshold_reason}
                      </div>
                    )}
                  </td>
                  <td className="faint">
                    {tag.learned_at ? shortDate(tag.learned_at) : "not yet"}
                  </td>
                  <td>
                    <div style={{ whiteSpace: "nowrap" }}>
                      {tag.pending_count > 0 && (
                        <button
                          className="btn"
                          style={{ padding: "4px 10px" }}
                          onClick={() => openTag(tag)}
                        >
                          Review
                        </button>
                      )}{" "}
                      <button
                        className="btn"
                        style={{ padding: "4px 10px" }}
                        disabled={relearning === tag.id || watching?.id === tag.id}
                        onClick={() => relearn(tag)}
                        title="Re-run learning — useful after new media is indexed"
                      >
                        {relearning === tag.id || watching?.id === tag.id
                          ? "Re-learning…"
                          : "Re-learn"}
                      </button>
                    </div>
                    {rowNote?.tagId === tag.id && (
                      <div
                        role={rowNote.ok ? "status" : "alert"}
                        className={rowNote.ok ? "faint" : undefined}
                        style={{
                          marginTop: 6,
                          maxWidth: 260,
                          fontSize: "0.76rem",
                          color: rowNote.ok ? undefined : "var(--ember)",
                        }}
                      >
                        {rowNote.text}
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open && (
        <>
          <div className="sectionhead tagreview-head" ref={reviewHead}>
            <h2 ref={reviewTitle} tabIndex={-1}>
              Is this {open.name}?
            </h2>
            <span className="faint">
              {/* The way to see what has just been tagged — in a new tab, so
                  the queue and the selection stay as they are. */}
              {open.asset_count > 0 ? (
                <Link
                  className="linkish"
                  href={`/browse?tag=${encodeURIComponent(open.slug)}`}
                  target="_blank"
                  rel="noreferrer"
                  title={`Browse everything tagged ${open.name}, in a new tab`}
                >
                  {open.asset_count} confirmed example
                  {open.asset_count === 1 ? "" : "s"}
                </Link>
              ) : (
                "No confirmed examples"
              )}{" "}
              so far — every answer here sharpens the next round
            </span>
          </div>
          {answer && (
            <div
              className="card"
              role={answerFailed ? "alert" : "status"}
              style={{
                marginBottom: 14,
                borderColor: answerFailed ? "var(--ember)" : undefined,
              }}
            >
              {answerFailed ? `Not saved: ${answer}` : answer}
            </div>
          )}
          {!pending ? (
            <div className="empty">Loading…</div>
          ) : !pending.length ? (
            <div className="empty">Nothing left to review for this tag.</div>
          ) : (
            <>
              <div className="toolbar">
                <button
                  className="btn"
                  disabled={busy}
                  onClick={() => chooseDownTo(pending.length - 1)}
                >
                  Select all {pending.length}
                </button>
                <span className="faint" style={{ fontSize: "0.8rem" }}>
                  strongest first · click a picture to look · tick to select
                  <span className="pointer-hint">
                    , shift-click for a range
                  </span>{" "}
                  · Y / N answers the selection
                </span>
              </div>
              <div className="grid">
                {pending.map((row, index) => (
                  <div
                    className="tile tagpick"
                    key={row.asset_id}
                    data-selected={chosen.has(row.asset_id)}
                  >
                    {/* Picture and tick are siblings: a checkbox inside the
                        picture's button is invalid, and its clicks would
                        open the viewer. */}
                    <div className="tagpick-stage">
                      <button
                        type="button"
                        className="tile-frame tagpick-frame"
                        data-look={row.asset_id}
                        aria-haspopup="dialog"
                        aria-label={`Look at ${row.filename}`}
                        onClick={(event) =>
                          event.shiftKey ? choose(index, true) : setViewing(row.asset_id)
                        }
                        title={`${row.filename} — click to look, shift-click to select a range`}
                      >
                        <SuggestionPicture row={row} />
                        {row.media_type !== "image" && (
                          <span className="tagpick-play" aria-hidden="true" />
                        )}
                        {row.confidence != null && (
                          <span className="tile-badge">
                            {(row.confidence * 100).toFixed(0)}%
                          </span>
                        )}
                      </button>
                      <label className="tagpick-check" title="Select">
                        <input
                          type="checkbox"
                          checked={chosen.has(row.asset_id)}
                          aria-label={`Select ${row.filename}`}
                          // A checkbox's change rides on its click, so the
                          // shift key is there to be read: shift-tick is a
                          // range, like shift-click on the picture.
                          onChange={(event) =>
                            choose(index, (event.nativeEvent as MouseEvent).shiftKey === true)
                          }
                        />
                      </label>
                      <button
                        type="button"
                        className="tagpick-downto"
                        onClick={() => chooseDownTo(index)}
                        title="Select this one and every stronger suggestion above it"
                      >
                        ↑ down to here
                      </button>
                    </div>
                    <div className="tile-meta">
                      <div className="tile-name">
                        {/* A new tab: followed in this one, the link took
                            the open queue and the selection with it. */}
                        <Link
                          href={`/assets/${row.asset_id}`}
                          target="_blank"
                          rel="noreferrer"
                          prefetch={false}
                          title="Open the full page in a new tab"
                        >
                          {row.filename}
                        </Link>
                      </div>
                      <div className="faint" style={{ fontSize: "0.74rem" }}>
                        <SuggestedBy row={row} />
                      </div>
                      <div
                        className="toolbar"
                        style={{ margin: "8px 0 0", gap: 6 }}
                      >
                        <button
                          className="btn btn-primary"
                          style={{ padding: "4px 12px" }}
                          disabled={busy}
                          onClick={() => decide([row.asset_id], [])}
                        >
                          Yes
                        </button>
                        <button
                          className="btn"
                          style={{ padding: "4px 12px" }}
                          disabled={busy}
                          onClick={() => decide([], [row.asset_id])}
                        >
                          No
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
              {selectedIds.length > 0 && (
                // Pinned to the foot of the screen while anything is chosen:
                // never under the sticky header, and in thumb's reach on a phone.
                <div
                  className="toolbar tagreview-bar"
                  role="toolbar"
                  aria-label="Answer the selection"
                >
                  <>
                    <span className="pill" data-tone="warn" aria-live="polite">
                      {busy ? "Saving…" : `${selectedIds.length} selected`}
                    </span>
                    <button
                      className="btn btn-primary"
                      disabled={busy}
                      onClick={() => decide(selectedIds, [])}
                      title="Y"
                    >
                      Yes, {selectedIds.length === 1 ? "it is" : "these are"}{" "}
                      {open.name}
                    </button>
                    <button
                      className="btn"
                      disabled={busy}
                      onClick={() => decide([], selectedIds)}
                      title="N"
                    >
                      No
                    </button>
                    {otherIds.length > 0 && (
                      <button
                        className="btn"
                        disabled={busy}
                        onClick={() => decide(selectedIds, otherIds)}
                        title="Answer the whole queue at once: the selected tiles are it, every other tile shown is not"
                      >
                        Yes to these, no to the other {otherIds.length}
                      </button>
                    )}
                    <button
                      className="btn"
                      onClick={() => {
                        setChosen(new Set());
                        setAnchor(null);
                      }}
                      title="Esc"
                    >
                      Clear
                    </button>
                  </>
                </div>
              )}
              {viewed && (
                // Mounted once, beside the grid — never inside a tile, whose
                // `overflow: hidden` and hover transform would clip it.
                <TagViewer
                  assetId={viewed.asset_id}
                  mediaType={viewed.media_type}
                  filename={viewed.filename}
                  // A local model was shown one shot: the clip starts there.
                  startAt={momentSeconds(viewed)}
                  heading={`Is this ${open.name}?`}
                  // The model's name is said here in words: a tile's tooltip
                  // is no use on a touch screen. TagViewer lets this line
                  // wrap so all of it shows on a phone; the name still goes
                  // last, the least needed if a window ever cuts it short.
                  detail={`suggested by ${suggestedBy(viewed, ORIGIN_LABEL).label}${momentNote(
                    viewed,
                  )}${viewed.confidence != null ? ` · ${(viewed.confidence * 100).toFixed(0)}%` : ""}${
                    viewed.origin === "local" && viewed.model ? ` · ${viewed.model}` : ""
                  }`}
                  position={`${viewAt + 1} of ${pending.length}`}
                  status={answerFailed ? null : answer}
                  problem={answerFailed && answer ? `Not saved: ${answer}` : null}
                  busy={busy}
                  onClose={() => setViewing(null)}
                  onPrev={viewAt > 0 ? () => step(-1) : undefined}
                  onNext={viewAt < pending.length - 1 ? () => step(1) : undefined}
                  onYes={() => answerViewed(true)}
                  onNo={() => answerViewed(false)}
                  yesLabel={`Yes, it is ${open.name}`}
                  onSelect={() => choose(viewAt, false)}
                  selected={chosen.has(viewed.asset_id)}
                  returnFocus={focusAfterViewing}
                />
              )}
            </>
          )}
        </>
      )}
    </Shell>
  );
}
