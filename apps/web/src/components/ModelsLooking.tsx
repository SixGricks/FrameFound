"use client";

// What the Looker does: the two things an administrator can set about how a
// model on this Mac looks at footage, and one line saying where its work
// shows up.
//
// Both settings are the server's (`GET /looks/status`), saved the moment
// they are changed (`PUT /looks/settings`, one field at a time) and read by
// the Looker before every photograph and shot, so a change applies from the
// next one on and nothing already looked at is looked at again. There is no
// Save button because there is nothing to compose: one tick, one choice.
//
// A change shows at once and is sent in the order it was made. The controls
// stay enabled while a save is out: a control disabled under the keyboard
// drops focus to the top of a long page. If the server says no, its sentence
// appears here in the card and the control goes back to what the server has.
//
// The card reads for itself rather than from the page's overview: these
// settings belong to Looking, which the overview knows nothing about.

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { api, type LookSettings, type LookStatus } from "@/lib/api";
import {
  STILLS_CHOICES,
  STILLS_NOTE,
  lookSettingSaved,
  reviewsUnderWayText,
  waitingOnTagsText,
} from "@/lib/looks";

type Note = { tone: "alert" | "status"; text: string };

/** Why the two controls cannot be changed, for whoever is not an administrator
 *  (the page says the same once, at its top). */
const ADMIN_ONLY = "Only an administrator can change this.";

/** How often the line of counts is read again while the page is open. */
const READ_EVERY_MS = 15_000;

export default function ModelsLooking() {
  const [status, setStatus] = useState<LookStatus | null>(null);
  const [reviews, setReviews] = useState<ReadonlyArray<{ state: string }>>([]);
  // What the controls show while a change is on its way to the server.
  const [draft, setDraft] = useState<LookSettings | null>(null);
  const [note, setNote] = useState<Note | null>(null);
  // Only the newest read draws: one asked for before a save must not put
  // the old value back after it.
  const asked = useRef(0);
  const live = useRef(true);
  // Saves go out one after another, in the order the changes were made.
  const queue = useRef<Promise<void>>(Promise.resolve());
  const unsaved = useRef(0);

  const read = useCallback(async () => {
    const mine = ++asked.current;
    try {
      const [nextStatus, activity] = await Promise.all([api.lookStatus(), api.lookReviews()]);
      if (mine !== asked.current || !live.current) return;
      setStatus(nextStatus);
      setReviews(activity.reviews);
    } catch {
      // What was last read stays. Before any read worked there is no card:
      // a server from before Looking has none of this to show.
    }
  }, []);

  useEffect(() => {
    live.current = true;
    const tick = () => {
      // Not over a change still being saved: the answer would be read
      // again the moment the save lands anyway.
      if (!document.hidden && unsaved.current === 0) void read();
    };
    tick();
    const timer = window.setInterval(tick, READ_EVERY_MS);
    document.addEventListener("visibilitychange", tick);
    return () => {
      live.current = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [read]);

  if (!status?.available) return null;
  const settings = draft ?? status.settings;
  const manage = status.can_manage;

  function change(patch: Partial<LookSettings>) {
    if (!manage || !status) return;
    setNote(null);
    setDraft((before) => ({ ...(before ?? status.settings), ...patch }));
    unsaved.current += 1;
    queue.current = queue.current.then(async () => {
      try {
        const saved = await api.updateLookSettings(patch);
        if (!live.current) return;
        setStatus((now) => (now ? { ...now, settings: saved } : now));
        setNote({ tone: "status", text: lookSettingSaved(patch) });
      } catch (err) {
        if (!live.current) return;
        setNote({
          tone: "alert",
          text:
            err instanceof Error && err.message
              ? `Not saved: ${err.message}`
              : "Not saved. Try again.",
        });
      } finally {
        unsaved.current -= 1;
        if (unsaved.current === 0 && live.current) {
          // The server's values show again: what was just saved, or, after
          // a refusal, what was there before.
          setDraft(null);
          void read();
        }
      }
    });
  }

  return (
    <div className="card models-looking">
      <h3 style={{ margin: 0 }}>What the Looker does</h3>
      <p className="faint" style={{ margin: "4px 0 0", fontSize: "0.88rem" }}>
        For each photograph and each shot of a clip it writes a caption, gives a score and suggests
        tags. These two choices apply to what it looks at from now on; nothing already looked at is
        looked at again.
      </p>

      <label className="field" data-layout="row">
        <input
          type="checkbox"
          checked={settings.new_tags}
          disabled={!manage}
          title={manage ? undefined : ADMIN_ONLY}
          onChange={(event) => change({ new_tags: event.target.checked })}
        />
        <span>
          Let it propose new tags (not just ones you already use)
          <span className="faint models-looking-hint">
            A new tag appears on Tags with its suggestions waiting; nothing is tagged until you
            answer them.
          </span>
        </span>
      </label>

      <label className="field models-looking-stills">
        Pictures shown for each shot
        <select
          className="select"
          value={settings.stills_per_shot}
          disabled={!manage}
          title={manage ? undefined : ADMIN_ONLY}
          onChange={(event) => change({ stills_per_shot: Number(event.target.value) })}
        >
          {STILLS_CHOICES.map((choice) => (
            <option key={choice.value} value={choice.value}>
              {choice.label}
            </option>
          ))}
        </select>
        <span className="faint">{STILLS_NOTE}</span>
      </label>

      {/* Present before it has anything to say, so the sentence is read out. */}
      <div role={note?.tone === "alert" ? "alert" : "status"}>
        {note && (
          <p className="models-save-note" data-tone={note.tone === "alert" ? "bad" : "ok"}>
            {note.text}
          </p>
        )}
      </div>

      <p className="faint models-looking-where">
        <Link href="/tags">{waitingOnTagsText(status.pending_suggestions)}</Link> ·{" "}
        {reviewsUnderWayText(reviews)} — <Link href="/processing">see Processing</Link>.
      </p>
    </div>
  );
}
