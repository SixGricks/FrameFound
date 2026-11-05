"use client";

// Search — the product's centre of gravity. Transcript hits lead because
// "the moment someone said X" is the thing no folder structure can answer.

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import Shell from "@/components/Shell";
import { usePageTitle } from "@/lib/hooks";
import Thumb from "@/components/Thumb";
import {
  api,
  type Library,
  type MediaType,
  type SearchResponse,
  type VisualStatus,
} from "@/lib/api";
import { highlight, shortDate, timecode } from "@/lib/format";

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
};

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

  useEffect(() => {
    api.libraries().then(setLibraries).catch(() => undefined);
  }, []);

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
        if (ticket === latest.current) setResults(response);
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
  const transcriptHits = results?.transcript_hits ?? [];
  const filenameHits = results?.filename_hits ?? [];
  const visualHits = results?.visual_hits ?? [];
  const tagHits = results?.tag_hits ?? [];
  // A server older than visual_status has only the boolean, which meant
  // "still indexing".
  const visualStatus: VisualStatus | null = results
    ? (results.visual_status ?? (results.visual_available ? "ready" : "indexing"))
    : null;
  // tagHits counts toward 'found something', or a purely tag-matched query
  // would render the empty state under a full grid of results.
  const nothing =
    hasResults &&
    transcriptHits.length === 0 &&
    filenameHits.length === 0 &&
    visualHits.length === 0 &&
    tagHits.length === 0;

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
          {bestMatches.map((hit, index) => {
            const at = hit.ts_ms ?? 0;
            // The matched frame, not the poster: the drone shot is at 0:42,
            // not wherever the clip happens to open.
            const kind =
              hit.media_type === "video" && hit.frame_ms ? `frame?ts=${hit.frame_ms}` : "thumbnail";
            return (
              <Link
                key={hit.asset_id}
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
                  </div>
                  {hit.snippet && (
                    <p className="hit-quote">
                      {highlight(hit.snippet, results?.query ?? "").map(([chunk, hot], i) =>
                        hot ? <mark key={i}>{chunk}</mark> : <span key={i}>{chunk}</span>,
                      )}
                    </p>
                  )}
                </div>
              </Link>
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
