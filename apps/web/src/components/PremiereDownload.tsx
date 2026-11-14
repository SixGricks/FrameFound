"use client";

// Download for Premiere: the row of things that can be done with one version
// of a cut, led by this computer's path profile and the download itself, with
// what the file said underneath.
//
// A cut is kept as pieces, not as a file, so the same version downloads for
// one computer today and another tomorrow with only the paths changed. The
// profile is remembered under the key Search's "Send to Premiere" uses: it is
// the same question (which computer is this?) and should be answered once.
//
// Nothing here decides what happened to a clip. The server's sentence is
// shown first and unchanged; the clips it left out or wrote offline are
// named after it. The first download of a local draft is when its shots
// start to count as used, and the page says so — before that a draft marks
// nothing, so three drafts of one folder do not use half of it up.

import { useEffect, useRef, useState, type ReactNode } from "react";

import { api } from "@/lib/api";
import { profilesWord, sendLines, sentElsewhere, type ProfilesRead } from "@/lib/cuts";

/** Search's key (app/page.tsx): one answer to "which computer is this?". */
const PROFILE_KEY = "framefound.premiereProfile";

function readProfile(): string {
  try {
    return window.localStorage.getItem(PROFILE_KEY) ?? "";
  } catch {
    return "";
  }
}

/** Long enough for any browser to have taken the file from the page. */
const KEEP_FILE_MS = 60_000;

export default function PremiereDownload({
  cutId,
  what = "another version",
  firstOfDraft,
  canSend,
  busy = false,
  onSent,
  children,
}: {
  /** The version to download. */
  cutId: string;
  /** What to call it ("version 2"), for a download that finishes after
   *  another version has been put on screen. */
  what?: string;
  /** A local draft nobody has downloaded yet. */
  firstOfDraft: boolean;
  /** False for an account that can look but not change anything: a download
   *  marks the cut as sent, which is a change. */
  canSend: boolean;
  /** Another action on the page is under way. */
  busy?: boolean;
  /** After a download: the page reads the cut again (it is now "downloaded"). */
  onSent: () => void | Promise<void>;
  /** The page's other actions for this version, in the same row. */
  children?: ReactNode;
}) {
  const [profiles, setProfiles] = useState<string[]>([]);
  // Whether the server has answered: an empty list said "No path profiles
  // yet" before it had, on every cut whose computer has one.
  const [profilesRead, setProfilesRead] = useState<ProfilesRead>("reading");
  const [profile, setProfile] = useState("");
  const [sending, setSending] = useState(false);
  const [lines, setLines] = useState<string[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  // The version on screen now, for a download that was started on another.
  const onScreen = useRef(cutId);
  onScreen.current = cutId;

  useEffect(() => {
    if (!canSend) return;
    let live = true;
    api
      .pathProfiles()
      .then((rows) => {
        if (!live) return;
        const names = Array.from(new Set(rows.map((row) => row.profile_name))).sort();
        setProfiles(names);
        setProfilesRead("read");
        const saved = readProfile();
        setProfile(names.includes(saved) ? saved : (names[0] ?? ""));
      })
      // No list: the file is still written, with the server's own paths, and
      // its sentence says which clips import offline. The list says so too.
      .catch(() => {
        if (live) setProfilesRead("failed");
      });
    return () => {
      live = false;
    };
  }, [canSend]);

  // What one version's file said is not said of another.
  useEffect(() => {
    setLines([]);
    setProblem(null);
  }, [cutId]);

  async function download() {
    if (sending) return;
    // The version this download is of, whatever is on screen when it ends:
    // the version buttons stay usable while the file is built.
    const started = { id: cutId, what, firstOfDraft };
    setSending(true);
    setLines([]);
    setProblem(null);
    try {
      // Only a profile that was chosen from the list is remembered. With the
      // list not read (yet, or at all) the value is empty, and storing that
      // under the key Search shares threw away the computer's own choice:
      // the next visit fell back to the first profile by name, and the file
      // was written with another computer's paths.
      if (profiles.includes(profile)) {
        try {
          window.localStorage.setItem(PROFILE_KEY, profile);
        } catch {
          // Remembering the profile is a convenience; private windows refuse it.
        }
      }
      const sent = await api.sendCut(started.id, profile);
      const url = URL.createObjectURL(new Blob([sent.xml], { type: "application/xml" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = sent.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // Given back once the browser has taken it; at once can cancel the
      // save (Safari reads the file after the click returns), and by then
      // the server has marked the cut downloaded and its shots as used.
      window.setTimeout(() => URL.revokeObjectURL(url), KEEP_FILE_MS);
      // What one version's file said is not said of another.
      setLines(
        onScreen.current === started.id ? sendLines(sent, started.firstOfDraft) : sentElsewhere(sent, started.what),
      );
      await onSent();
    } catch (err) {
      const why =
        err instanceof Error && err.message
          ? err.message
          : "The file could not be made. Nothing was downloaded. Try again.";
      setProblem(onScreen.current === started.id ? why : `The file for ${started.what} was not downloaded. ${why}`);
    } finally {
      setSending(false);
    }
  }

  const placeholder = profilesWord(profilesRead, profiles.length);
  // Download waits for the list: pressed while it was being read, the file
  // was written with the server's paths instead of this computer's.
  const waiting = profilesRead === "reading";

  return (
    <>
      <div className="toolbar cut-actions">
        {canSend && (
          <>
            <select
              className="select"
              value={profile}
              onChange={(event) => setProfile(event.target.value)}
              aria-label="This computer's path profile"
              title="Paths in the file are written for this computer"
            >
              {placeholder !== null && <option value="">{placeholder}</option>}
              {profiles.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn-primary"
              disabled={sending || busy || waiting}
              onClick={() => void download()}
              title="A Premiere sequence with every shot at its in and out point"
            >
              {sending ? "Building…" : "Download for Premiere"}
            </button>
          </>
        )}
        {children}
      </div>
      {problem && (
        <div className="card cutsend-problem" role="alert">
          {problem}
        </div>
      )}
      {/* Always in the page, so what the file said is read out when it arrives. */}
      <div className="faint cutsend-said" role="status">
        {lines.map((line) => (
          <p key={line}>{line}</p>
        ))}
      </div>
    </>
  );
}
