"use client";

// Search — the product's centre of gravity. Transcript hits lead because
// "the moment someone said X" is the thing no folder structure can answer.

import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import { api, type Library, type SearchResponse } from "@/lib/api";
import { highlight, shortDate, timecode } from "@/lib/format";

const EXAMPLES = [
  '"starting bid"',
  "settlement will be on or before",
  "drone",
  "auction preview",
];

function SearchPage() {
  const router = useRouter();
  const params = useSearchParams();
  const initial = params.get("q") ?? "";

  const [term, setTerm] = useState(initial);
  const [libraryId, setLibraryId] = useState(params.get("library") ?? "");
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.libraries().then(setLibraries).catch(() => undefined);
  }, []);

  const run = useCallback(
    async (q: string, lib: string) => {
      if (q.trim().length < 2) return;
      setBusy(true);
      setError(null);
      try {
        setResults(await api.search(q.trim(), lib || undefined));
      } catch (err) {
        setError(err instanceof Error ? err.message : "Search failed");
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (initial) run(initial, params.get("library") ?? "");
  }, [initial, params, run]);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const usp = new URLSearchParams();
    usp.set("q", term.trim());
    if (libraryId) usp.set("library", libraryId);
    router.push(`/?${usp}`);
    run(term, libraryId);
  }

  const hasResults = results !== null;
  const transcriptHits = results?.transcript_hits ?? [];
  const filenameHits = results?.filename_hits ?? [];
  const visualHits = results?.visual_hits ?? [];
  const nothing =
    hasResults &&
    transcriptHits.length === 0 &&
    filenameHits.length === 0 &&
    visualHits.length === 0;

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
                  run(example, libraryId);
                  router.push(`/?q=${encodeURIComponent(example)}`);
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

      {hasResults && !results?.visual_available && (
        <p className="faint" style={{ marginTop: 26, fontSize: "0.84rem" }}>
          Visual search is still indexing your library — results will improve as it completes.
        </p>
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
