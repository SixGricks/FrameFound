"use client";

// Search — the product's centre of gravity. Transcript hits lead because
// "the moment someone said X" is the thing no folder structure can answer.

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import Shell from "@/components/Shell";
import { setChatContext } from "@/lib/chatContext";
import { usePageTitle } from "@/lib/hooks";
import { ScorePill } from "@/components/LookNote";
import Thumb from "@/components/Thumb";
import {
  api,
  type FusedResult,
  type Library,
  type MediaType,
  type SearchResponse,
  type SelectMoment,
  type VisualStatus,
} from "@/lib/api";
import {
  draftTickedHref,
  keepTicks,
  profilesWord,
  ticksBack,
  ticksKey,
  type ProfilesRead,
} from "@/lib/cuts";
import { highlight, shortDate, timecode } from "@/lib/format";
import { captionShown } from "@/lib/looks";

const EXAMPLES = [
  '"starting bid"',
  "settlement will be on or before",
  "drone",
  "auction preview",
];

const MEDIA_TYPES: { value: MediaType; label: string }[] = [
  { value: "image", label: "Photos" },
  { value: "video", label: "Video" },
  { value: "audio", label: "Audio" },
];

function asMediaType(value: string | null): MediaType | "" {
  return value === "image" || value === "video" || value === "audio" ? value : "";
}

// Why a best match matched, in the operator's words. A tag is their own
// judgement; a suggestion and a CLIP opinion are labelled as what they are.
const REASONS: Record<string, { label: string; tone?: "ok" | "warn" }> = {
  tag: { label: "tagged", tone: "ok" },
  suggested_tag: { label: "suggested tag", tone: "warn" },
  visual: { label: "looks like" },
  transcript: { label: "said" },
  name: { label: "name" },
  // A local model's sentence about the picture held the words. An opinion,
  // so it is named as the AI's and carries no tone of its own.
  caption: { label: "AI caption" },
};

const PROFILE_KEY = "framefound.premiereProfile";

// The ticks of the searches last shown, for the way back: "Draft a cut…" and
// every row's link leave this page, and Back ran the search again with
// nothing ticked. Kept while the tab is open; a reload starts afresh.
const keptTicks = new Map<string, string[]>();

/** A ticked best match as a moment for the selects reel. The server widens
 * it: a quote to its whole sentence, a picture to its detected shot. */
function momentFor(hit: FusedResult, query: string): SelectMoment {
  // Found by what a local model said it shows, and not by how it looks.
  const byCaption = hit.reasons.includes("caption") && !hit.reasons.includes("visual");
  if (hit.media_type === "image") {
    if (!hit.tag && byCaption) return { id: hit.asset_id, reason: `AI caption: ${query}` };
    return { id: hit.asset_id, reason: hit.tag ? `tag: ${hit.tag}` : `visual: ${query}` };
  }
  if (hit.reasons.includes("transcript") && hit.ts_ms !== null) {
    return { id: hit.asset_id, at_s: hit.ts_ms / 1000, kind: "speech" };
  }
  if (hit.reasons.includes("visual") && hit.frame_ms !== null) {
    return {
      id: hit.asset_id,
      at_s: hit.frame_ms / 1000,
      kind: "visual",
      reason: `visual: ${query}`,
    };
  }
  if (byCaption && hit.ts_ms !== null) {
    // `ts_ms` is the middle of the shot whose caption matched; the server
    // widens it to that shot, as it does a matched frame. Without this a
    // clip found by its caption alone went to Premiere at 0:00, labelled as
    // a file-name match. (The same order as the server's own
    // moments-from-a-search: words, picture, caption, tag, name.)
    return {
      id: hit.asset_id,
      at_s: hit.ts_ms / 1000,
      kind: "visual",
      reason: `AI caption: ${query}`,
    };
  }
  if (hit.tag) {
    return {
      id: hit.asset_id,
      at_s: (hit.frame_ms ?? 0) / 1000,
      kind: "tag",
      reason: `tag: ${hit.tag}`,
    };
  }
  return { id: hit.asset_id, at_s: 0, kind: "name", reason: `name: ${query}` };
}

function readProfile(): string {
  try {
    return window.localStorage.getItem(PROFILE_KEY) ?? "";
  } catch {
    return "";
  }
}

function SearchPage() {
  const router = useRouter();
  const params = useSearchParams();
  const initial = params.get("q") ?? "";
  usePageTitle(initial ? `Search: ${initial}` : "Search");

  const [term, setTerm] = useState(initial);
  const [libraryId, setLibraryId] = useState(params.get("library") ?? "");
  const [mediaType, setMediaType] = useState<MediaType | "">(asMediaType(params.get("type")));
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Send to Premiere: the ticked rows, this computer's path profile, and
  // what the last send said (files it could not map, files offline).
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [profiles, setProfiles] = useState<string[]>([]);
  // Whether the server has answered: "No path profiles yet" was said while
  // the list was still being read (lib/cuts.ts profilesWord).
  const [profilesRead, setProfilesRead] = useState<ProfilesRead>("reading");
  const [profile, setProfile] = useState("");
  const [byDate, setByDate] = useState(false);
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState<string | null>(null);
  // Whether a local model can draft a cut on this install: only there is
  // "Draft a cut…" offered. False until the server says so, and on the
  // container server and one from before local models, where the bar is
  // exactly as it was.
  const [canDraft, setCanDraft] = useState(false);

  useEffect(() => {
    api
      .lookStatus()
      .then((status) => setCanDraft(status.available))
      .catch(() => undefined);
    api.libraries().then(setLibraries).catch(() => undefined);
    api
      .pathProfiles()
      .then((rows) => {
        const names = Array.from(new Set(rows.map((row) => row.profile_name))).sort();
        setProfiles(names);
        setProfilesRead("read");
        const saved = readProfile();
        setProfile(names.includes(saved) ? saved : (names[0] ?? ""));
      })
      // No list: the file is still built, with the server's own paths.
      .catch(() => setProfilesRead("failed"));
  }, []);

  // Which search the results on screen answer (its words and filters), so
  // their ticks are kept under it. Set with the results; "" for none.
  const shownFor = useRef("");

  // A new result set starts with nothing ticked, unless it is a search
  // ticked earlier in this tab: then its ticks come back with it.
  useEffect(() => {
    setPicked(
      results
        ? ticksBack(keptTicks, shownFor.current, results.results.map((hit) => hit.asset_id))
        : new Set(),
    );
    setSent(null);
  }, [results]);

  /** Tick or untick rows, keeping the ticks for the way back. Only a
   *  person's ticking is kept; results arriving never overwrite it. */
  function choose(next: Set<string>) {
    setPicked(next);
    keepTicks(keptTicks, shownFor.current, next);
  }

  function toggle(assetId: string) {
    const next = new Set(picked);
    if (next.has(assetId)) next.delete(assetId);
    else next.add(assetId);
    choose(next);
  }

  async function sendToPremiere() {
    if (!results) return;
    const rows = results.results.filter((hit) => picked.has(hit.asset_id));
    if (rows.length === 0) return;
    setSending(true);
    setSent(null);
    try {
      try {
        window.localStorage.setItem(PROFILE_KEY, profile);
      } catch {
        // Remembering the profile is a convenience; private windows refuse it.
      }
      const made = await api.selects({
        title: results.query,
        profile,
        query: results.query,
        moments: rows.map((hit) => momentFor(hit, results.query)),
        order: byDate ? "captured" : "rank",
      });
      const url = URL.createObjectURL(new Blob([made.xml], { type: "application/xml" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = made.filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
      setSent(`${made.filename}: ${made.note} In Premiere, File → Import.`);
    } catch (err) {
      setSent(err instanceof Error ? err.message : "Could not build the selects");
    } finally {
      setSending(false);
    }
  }

  // Only the newest request may write to the page. Once the results follow
  // the URL (below), a slow "drone" response landing after Back to `/` would
  // put its results back under the cleared box.
  const latest = useRef(0);

  const run = useCallback(
    async (q: string, lib: string, type: MediaType | "") => {
      if (q.trim().length < 2) return;
      const ticket = ++latest.current;
      setBusy(true);
      setError(null);
      try {
        const response = await api.search(q.trim(), lib || undefined, type || undefined);
        if (ticket === latest.current) {
          shownFor.current = ticksKey(q, lib, type);
          setResults(response);
        }
      } catch (err) {
        if (ticket === latest.current) {
          setError(err instanceof Error ? err.message : "Search failed");
        }
      } finally {
        if (ticket === latest.current) setBusy(false);
      }
    },
    [],
  );

  // The URL is the one trigger. Submitting used to run the search and then
  // push the URL, whose change ran it again: the server's heaviest request,
  // twice per query.
  const urlLibrary = params.get("library") ?? "";
  const urlType = asMediaType(params.get("type"));
  useEffect(() => {
    if (initial) {
      run(initial, urlLibrary, urlType);
      return;
    }
    // Back to the bare `/` cleared the box (the effect below) but left the
    // last query's results on screen: an empty search above "Best matches"
    // for a term that appeared nowhere. No query in the URL is the landing
    // page, so the results go too, and any request still in flight is void.
    latest.current++;
    shownFor.current = "";
    setResults(null);
    setError(null);
    setBusy(false);
  }, [initial, urlLibrary, urlType, run]);

  // The controls follow the URL too. They were read from it once, at mount:
  // after Back from `?type=video` the search ran over all media under a
  // select still reading "Video", and the next submit put the filter back.
  useEffect(() => {
    setTerm(initial);
    setLibraryId(urlLibrary);
    setMediaType(urlType);
  }, [initial, urlLibrary, urlType]);

  // For the chat panel: the library by name (the address has only its id)
  // and the rows ticked for Premiere, so "these" in a message means them.
  // The search itself is in the address. Joined into one string so the
  // effect runs when the ticks change, not on every render.
  const libraryName = libraries.find((library) => library.id === urlLibrary)?.name ?? "";
  const ticked = Array.from(picked).join(" ");
  useEffect(
    () =>
      setChatContext({
        library: libraryName,
        selection: ticked ? ticked.split(" ") : [],
      }),
    [libraryName, ticked],
  );

  // Every way into a search carries the filters on screen. An example hint
  // pushed `?q=` alone and searched everything while "Video" was selected.
  function searchUrl(q: string) {
    const usp = new URLSearchParams();
    usp.set("q", q.trim());
    if (libraryId) usp.set("library", libraryId);
    if (mediaType) usp.set("type", mediaType);
    return `/?${usp}`;
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    router.push(searchUrl(term));
  }

  const hasResults = results !== null;
  const bestMatches = results?.results ?? [];
  // A select is a picture on V1: audio-only files are not offered.
  const selectable = bestMatches.filter((hit) => hit.media_type !== "audio");
  // "Draft a cut…": the Cuts page's form, with the ticked clips as its
  // footage. Null while no clip is ticked (photographs alone are not footage
  // for a cut), and then the button is off.
  const draftHref = canDraft
    ? draftTickedHref(
        bestMatches.filter((hit) => picked.has(hit.asset_id)),
        results?.query ?? "",
      )
    : null;
  const transcriptHits = results?.transcript_hits ?? [];
  const filenameHits = results?.filename_hits ?? [];
  const visualHits = results?.visual_hits ?? [];
  const tagHits = results?.tag_hits ?? [];
  // What a local model said files show. Absent on a server from before
  // captions, and empty until something has been looked at.
  const captionHits = results?.caption_hits ?? [];
  // A server older than visual_status has only the boolean, which meant
  // "still indexing".
  const visualStatus: VisualStatus | null = results
    ? (results.visual_status ?? (results.visual_available ? "ready" : "indexing"))
    : null;
  // tagHits counts toward 'found something', or a purely tag-matched query
  // would render the empty state under a full grid of results. The same
  // goes for captions: "building" matched thirteen photographs by what a
  // model said of them and nothing else, and the page said "Nothing
  // matched" above all thirteen.
  const nothing =
    hasResults &&
    transcriptHits.length === 0 &&
    filenameHits.length === 0 &&
    visualHits.length === 0 &&
    tagHits.length === 0 &&
    captionHits.length === 0;

  const profilesPlaceholder = profilesWord(profilesRead, profiles.length);

  return (
    <>
      <div className="searchwrap" data-compact={hasResults}>
        {!hasResults && (
          <>
            <p className="eyebrow">Self-hosted media catalog</p>
            <h1 style={{ marginTop: 10 }}>
              Find the moment,
              <br />
              not the folder.
            </h1>
          </>
        )}
        <form className="searchbar" onSubmit={submit}>
          <input
            className="input"
            value={term}
            onChange={(e) => setTerm(e.target.value)}
            placeholder="Search spoken words, filenames, folders…"
            autoFocus
            aria-label="Search"
          />
          <select
            className="select"
            value={libraryId}
            onChange={(e) => setLibraryId(e.target.value)}
            aria-label="Library"
          >
            <option value="">All libraries</option>
            {libraries.map((lib) => (
              <option key={lib.id} value={lib.id}>
                {lib.name}
              </option>
            ))}
          </select>
          <select
            className="select"
            value={mediaType}
            onChange={(e) => setMediaType(asMediaType(e.target.value))}
            aria-label="Media type"
          >
            <option value="">All media</option>
            {MEDIA_TYPES.map((type) => (
              <option key={type.value} value={type.value}>
                {type.label}
              </option>
            ))}
          </select>
          <button className="btn btn-primary" disabled={busy || term.trim().length < 2}>
            {busy ? "Searching…" : "Search"}
          </button>
        </form>

        {!hasResults && (
          <div className="hintrow">
            {EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                className="hint"
                onClick={() => {
                  setTerm(example);
                  router.push(searchUrl(example));
                }}
              >
                {example}
              </button>
            ))}
          </div>
        )}
        {error && (
          <p style={{ color: "var(--ember)", marginTop: 14 }}>{error}</p>
        )}
      </div>

      {nothing && (
        <div className="empty" style={{ marginTop: 34 }}>
          Nothing matched <strong>{results?.query}</strong>. Transcripts only cover media that
          has finished processing.
        </div>
      )}

      {bestMatches.length > 0 && (
        <section>
          <div className="sectionhead">
            <h2>Best matches</h2>
            <span className="faint mono">tags, pictures, words and names together</span>
          </div>
          <div className="selectbar">
            <label className="faint" style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={picked.size > 0 && picked.size === selectable.length}
                onChange={(e) =>
                  choose(
                    e.target.checked ? new Set(selectable.map((hit) => hit.asset_id)) : new Set(),
                  )
                }
              />
              {picked.size ? `${picked.size} selected` : "Select all"}
            </label>
            <select
              className="select"
              value={profile}
              onChange={(e) => setProfile(e.target.value)}
              aria-label="This computer's path profile"
              title="Paths in the file are written for this computer"
            >
              {profilesPlaceholder !== null && <option value="">{profilesPlaceholder}</option>}
              {profiles.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
            <label className="faint" style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={byDate}
                onChange={(e) => setByDate(e.target.checked)}
              />
              In shooting order
            </label>
            {/* Send waits for the list: pressed while it was read, the file
                was written with the server's paths instead of this computer's. */}
            <button
              type="button"
              className="btn btn-primary"
              disabled={picked.size === 0 || sending || profilesRead === "reading"}
              onClick={sendToPremiere}
              title="A bin with a marker on each moment, and a Selects sequence with handles"
            >
              {sending ? "Building…" : "Send to Premiere"}
            </button>
            {/* After the bar's own controls, which are all still here. Selects
                are a bin for a person to cut from; this asks the local Editor
                for a first cut of the same ticked clips. A link, so it can be
                opened beside the search; Back from the form finds the same
                rows still ticked (`keptTicks`). */}
            {canDraft &&
              (draftHref ? (
                <Link
                  className="btn"
                  href={draftHref}
                  prefetch={false}
                  title="A first cut of the ticked clips, drafted by the local Editor. Opens the form on the Cuts page; nothing is drafted until you press Draft there"
                >
                  Draft a cut…
                </Link>
              ) : (
                <button
                  type="button"
                  className="btn"
                  disabled
                  title="Tick at least one video clip first: the local Editor drafts a cut from video"
                >
                  Draft a cut…
                </button>
              ))}
          </div>
          {sent && (
            <p className="faint" style={{ margin: "0 0 12px", fontSize: "0.86rem" }}>
              {sent}
            </p>
          )}
          {bestMatches.map((hit, index) => {
            const at = hit.ts_ms ?? 0;
            // The matched frame, not the poster: the drone shot is at 0:42,
            // not wherever the clip happens to open.
            const kind =
              hit.media_type === "video" && hit.frame_ms ? `frame?ts=${hit.frame_ms}` : "thumbnail";
            const caption = captionShown(hit);
            return (
              <div key={hit.asset_id} className="hit-row">
                <label className="hit-pick" title="Select for Premiere">
                  <input
                    type="checkbox"
                    checked={picked.has(hit.asset_id)}
                    onChange={() => toggle(hit.asset_id)}
                    aria-label={`Select ${hit.filename}`}
                    disabled={hit.media_type === "audio"}
                  />
                </label>
                <Link
                className="hit"
                href={`/assets/${hit.asset_id}${at > 0 ? `?t=${Math.floor(at / 1000)}` : ""}`}
                style={{ animationDelay: `${Math.min(index, 12) * 28}ms` }}
              >
                <div className="hit-thumb">
                  <Thumb
                    assetId={hit.asset_id}
                    mediaType={hit.media_type}
                    status="ready"
                    kind={kind}
                  />
                </div>
                <div className="hit-body">
                  <div
                    style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}
                  >
                    {at > 0 && <span className="tc">{timecode(at)}</span>}
                    <span className="hit-file">{hit.filename}</span>
                    {hit.reasons.map((reason) => (
                      <span key={reason} className="pill" data-tone={REASONS[reason]?.tone}>
                        {REASONS[reason]?.label ?? reason}
                        {(reason === "tag" || reason === "suggested_tag") && hit.tag
                          ? `: ${hit.tag}`
                          : ""}
                      </span>
                    ))}
                    {/* Where a local model has looked at it: its score, an
                        opinion, labelled as the AI's. */}
                    <ScorePill score={hit.look_score} />
                  </div>
                  {hit.snippet && (
                    <p className="hit-quote">
                      {highlight(hit.snippet, results?.query ?? "").map(([chunk, hot], i) =>
                        hot ? <mark key={i}>{chunk}</mark> : <span key={i}>{chunk}</span>,
                      )}
                    </p>
                  )}
                  {caption && (
                    // Where a quote would be, when there is none: the
                    // model's sentence that matched. Text only, and faint,
                    // so it is not read as something that was said.
                    <p className="hit-quote hit-caption" title="What a local model said this shows">
                      {highlight(caption, results?.query ?? "").map(([chunk, hot], i) =>
                        hot ? <mark key={i}>{chunk}</mark> : <span key={i}>{chunk}</span>,
                      )}
                    </p>
                  )}
                </div>
              </Link>
              </div>
            );
          })}
        </section>
      )}

      {visualStatus === "indexing" && (
        <p className="faint" style={{ marginTop: 26, fontSize: "0.84rem" }}>
          Visual search is still indexing your library — results will improve as it completes.
        </p>
      )}
      {/* Not a wait: a missing or broken image model never fixes itself, and
          telling the operator it was "still indexing" had them wait for it. */}
      {visualStatus === "unavailable" && (
        <p style={{ color: "var(--ember)", marginTop: 26, fontSize: "0.84rem" }}>
          Visual search is unavailable — the server could not run its image model, so these
          results match tags, spoken words and names only. Check the server log.
        </p>
      )}

      {/* The same results by kind of match. Folded away under the fused
          list; open when there is no fused list to show (an older server). */}
      {hasResults && !nothing && (
        <details className="disclosure" open={bestMatches.length === 0} style={{ marginTop: 30 }}>
          <summary>By kind of match</summary>

          {tagHits.length > 0 && (
            <section>
              <div className="sectionhead">
                <h2>Tagged</h2>
                <span className="faint mono">
                  you said these are {tagHits[0]?.tag_name.toLowerCase()}
                </span>
                <Link
                  className="navlink"
                  style={{ marginLeft: "auto" }}
                  href={`/browse?tag=${tagHits[0]?.tag_slug}`}
                >
                  Browse all →
                </Link>
              </div>
              <div className="grid">
                {tagHits.map((hit, index) => (
                  <Link
                    key={`${hit.asset_id}-${hit.tag_slug}`}
                    href={`/assets/${hit.asset_id}`}
                    className="tile"
                    style={{ animationDelay: `${Math.min(index, 16) * 22}ms` }}
                    title={hit.filename}
                  >
                    <div className="tile-frame">
                      <Thumb assetId={hit.asset_id} mediaType={hit.media_type} status="ready" />
                      {/* A suggestion is a claim, not a decision — label it. */}
                      {!hit.confirmed && <span className="tile-badge">suggested</span>}
                    </div>
                    <div className="tile-meta">
                      <div className="tile-name">{hit.filename}</div>
                      <div className="tile-sub">
                        <span>{hit.tag_name}</span>
                        <span>{hit.confirmed ? "confirmed" : "unreviewed"}</span>
                      </div>
                    </div>
                  </Link>
                ))}
              </div>
            </section>
          )}

          {visualHits.length > 0 && (
            <section>
              <div className="sectionhead">
                <h2>Looks like this</h2>
                <span className="faint mono">matched by image, not by name</span>
              </div>
              <div className="grid">
                {visualHits.map((hit, index) => (
                  <Link
                    key={`${hit.asset_id}-${hit.ts_ms}`}
                    href={`/assets/${hit.asset_id}${hit.ts_ms ? `?t=${Math.floor(hit.ts_ms / 1000)}` : ""}`}
                    className="tile"
                    style={{ animationDelay: `${Math.min(index, 16) * 22}ms` }}
                    title={hit.filename}
                  >
                    <div className="tile-frame">
                      <Thumb assetId={hit.asset_id} mediaType={hit.media_type} status="ready" />
                      <span className="tile-badge">{Math.round(hit.similarity * 100)}%</span>
                    </div>
                    <div className="tile-meta">
                      <div className="tile-name">{hit.filename}</div>
                      <div className="tile-sub">
                        <span>{hit.media_type}</span>
                        {hit.ts_ms > 0 && <span>{timecode(hit.ts_ms)}</span>}
                      </div>
                    </div>
                  </Link>
                ))}
              </div>
            </section>
          )}

          {transcriptHits.length > 0 && (
            <section>
              <div className="sectionhead">
                <h2>Spoken words</h2>
                <span className="faint mono">{transcriptHits.length} moments</span>
              </div>
              {transcriptHits.map((hit, index) => (
                <Link
                  key={`${hit.asset_id}-${hit.start_ms}`}
                  className="hit"
                  href={`/assets/${hit.asset_id}?t=${Math.floor(hit.start_ms / 1000)}`}
                  style={{ animationDelay: `${Math.min(index, 12) * 28}ms` }}
                >
                  <div className="hit-thumb">
                    <Thumb assetId={hit.asset_id} mediaType={hit.media_type} status="ready" />
                  </div>
                  <div className="hit-body">
                    <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                      <span className="tc">{timecode(hit.start_ms)}</span>
                      <span className="hit-file">{hit.filename}</span>
                    </div>
                    <p className="hit-quote">
                      {highlight(hit.text, results?.query ?? "").map(([chunk, hot], i) =>
                        hot ? <mark key={i}>{chunk}</mark> : <span key={i}>{chunk}</span>,
                      )}
                    </p>
                  </div>
                </Link>
              ))}
            </section>
          )}

          {captionHits.length > 0 && (
            <section>
              <div className="sectionhead">
                <h2>AI captions</h2>
                <span className="faint mono">what a local model said these show</span>
              </div>
              {captionHits.map((hit, index) => (
                <Link
                  key={`${hit.asset_id}-${hit.ts_ms ?? "whole"}`}
                  className="hit"
                  href={`/assets/${hit.asset_id}${hit.ts_ms ? `?t=${Math.floor(hit.ts_ms / 1000)}` : ""}`}
                  style={{ animationDelay: `${Math.min(index, 12) * 28}ms` }}
                >
                  <div className="hit-thumb">
                    <Thumb assetId={hit.asset_id} mediaType={hit.media_type} status="ready" />
                  </div>
                  <div className="hit-body">
                    <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                      {hit.ts_ms !== null && hit.ts_ms > 0 && (
                        <span className="tc">{timecode(hit.ts_ms)}</span>
                      )}
                      <span className="hit-file">{hit.filename}</span>
                    </div>
                    <p className="hit-quote hit-caption">
                      {highlight(hit.caption, results?.query ?? "").map(([chunk, hot], i) =>
                        hot ? <mark key={i}>{chunk}</mark> : <span key={i}>{chunk}</span>,
                      )}
                    </p>
                  </div>
                </Link>
              ))}
            </section>
          )}

          {filenameHits.length > 0 && (
            <section>
              <div className="sectionhead">
                <h2>Files and folders</h2>
                <span className="faint mono">{filenameHits.length} matches</span>
              </div>
              <div className="grid">
                {filenameHits.map((hit, index) => (
                  <Link
                    key={hit.asset_id}
                    href={`/assets/${hit.asset_id}`}
                    className="tile"
                    style={{ animationDelay: `${Math.min(index, 16) * 22}ms` }}
                  >
                    <div className="tile-frame">
                      <Thumb assetId={hit.asset_id} mediaType={hit.media_type} status="ready" />
                    </div>
                    <div className="tile-meta">
                      <div className="tile-name">{hit.filename}</div>
                      <div className="tile-sub">
                        <span>{hit.media_type}</span>
                        <span>{shortDate(hit.captured_at)}</span>
                      </div>
                    </div>
                  </Link>
                ))}
              </div>
            </section>
          )}
        </details>
      )}
    </>
  );
}

export default function Page() {
  return (
    <Shell>
      <Suspense fallback={<div className="searchwrap" />}>
        <SearchPage />
      </Suspense>
    </Shell>
  );
}
