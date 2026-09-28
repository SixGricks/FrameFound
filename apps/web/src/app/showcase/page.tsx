"use client";

// Showcase: the best photographs, one place at a time.
//
// Built for GELCO's calendar — the best finished work of each course, a few
// crew and construction shots, a company group photo — and general on
// purpose: subject, libraries and places are settings. The server ranks
// (quality, light, finished-not-in-progress, print size, no people or
// machines, one place at a time by folder and GPS); this page is where a
// person chooses. Each place offers options in a carousel under the pick.
//
// The result goes into a listing — a new one, or one already started, so a
// finished-work search, a crew search and a group-photo search make one
// shortlist. Its files are named by place ("ledgerock-03-fall-drone"), and
// Lightroom's "Import FrameFound listings…" brings it in under those names.

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import {
  api,
  mediaUrl,
  type Library,
  type ListingSummary,
  type ShowcaseKind,
  type ShowcaseListingPick,
  type ShowcasePick,
  type ShowcasePlace,
  type ShowcasePlaceCount,
  type ShowcaseRequest,
} from "@/lib/api";

const DEFAULTS: Omit<ShowcaseRequest, "library_ids" | "places"> = {
  subject: "golf course",
  avoid: "",
  count: 20,
  alternates: 3,
  orientation: "landscape",
  min_megapixels: 12,
  allow_people: false,
  kind: "finished",
};

const KINDS: { value: ShowcaseKind; label: string }[] = [
  { value: "finished", label: "The finished work" },
  { value: "crew", label: "Crew, in a scenic setting" },
  { value: "construction", label: "Construction, the dramatic kind" },
  { value: "group", label: "A company group photo" },
];

function describe(pick: ShowcasePick): string {
  const when = pick.captured_at
    ? new Date(pick.captured_at).toLocaleDateString(undefined, { month: "short", year: "numeric" })
    : "undated";
  return `${pick.megapixels} MP · ${pick.width}×${pick.height} · ${when}`;
}

// One place: the pick large and whole, and every candidate in a carousel
// beneath it. The alternates used to sit beside the pick, and a thumbnail
// wider than its box spilled over them; below, nothing competes with the
// photograph being judged.
function PlaceCard({
  place,
  rank,
  current,
  on,
  onPick,
  onToggle,
}: {
  place: ShowcasePlace;
  rank: number;
  current: number;
  on: boolean;
  onPick: (index: number) => void;
  onToggle: (on: boolean) => void;
}) {
  const pick = place.picks[current];
  const strip = useRef<HTMLDivElement>(null);
  const count = place.picks.length;

  // Stepping with the arrows keeps the chosen one in view in the strip —
  // by scrolling the strip sideways only. scrollIntoView would also scroll
  // the page, and every card runs this on mount: the page opened at the
  // bottom.
  useEffect(() => {
    const box = strip.current;
    const cell = box?.querySelector<HTMLElement>('[data-active="true"]');
    if (!box || !cell) return;
    const outer = box.getBoundingClientRect();
    const inner = cell.getBoundingClientRect();
    if (inner.left < outer.left) {
      box.scrollBy({ left: inner.left - outer.left - 4, behavior: "smooth" });
    } else if (inner.right > outer.right) {
      box.scrollBy({ left: inner.right - outer.right + 4, behavior: "smooth" });
    }
  }, [current]);

  return (
    <div className="card showcase-place" data-off={!on}>
      <div style={{ display: "flex", gap: 10, alignItems: "baseline", flexWrap: "wrap" }}>
        <input
          type="checkbox"
          checked={on}
          onChange={(e) => onToggle(e.target.checked)}
          aria-label={`Include ${place.label}`}
        />
        <strong>
          {rank + 1}. {place.label}
        </strong>
        {pick && <span className="faint mono">{describe(pick)}</span>}
        {pick?.season && <span className="pill">{pick.season}</span>}
        {pick?.source && <span className="pill">{pick.source}</span>}
        <span className="faint mono" style={{ marginLeft: "auto" }}>
          {count === 1 ? "1 option" : `${current + 1} of ${count}`}
        </span>
      </div>
      {pick && (
        <a
          className="showcase-main"
          // The photograph's own shape: no letterbox bars, and no jump as it loads.
          style={
            pick.width && pick.height
              ? { aspectRatio: `${pick.width} / ${pick.height}` }
              : undefined
          }
          href={mediaUrl(pick.asset_id, "preview")}
          target="_blank"
          rel="noreferrer"
          title={`Open larger — ${pick.relative_path}`}
        >
          <Thumb assetId={pick.asset_id} mediaType="image" status="ready" kind="preview" />
        </a>
      )}
      {count > 1 && (
        <div className="showcase-carousel-row">
          <button
            type="button"
            className="btn showcase-step"
            onClick={() => onPick((current - 1 + count) % count)}
            aria-label={`Previous photograph of ${place.label}`}
          >
            ‹
          </button>
          <div className="showcase-carousel" ref={strip}>
            {place.picks.map((alt, index) => (
              <button
                key={alt.asset_id}
                type="button"
                className="showcase-alt"
                data-active={index === current}
                aria-pressed={index === current}
                onClick={() => onPick(index)}
                title={index === current ? "The pick" : `Use this one — ${alt.relative_path}`}
              >
                <Thumb assetId={alt.asset_id} mediaType="image" status="ready" />
                <span className="showcase-alt-label">
                  {index === 0 ? "Best" : `#${index + 1}`}
                  {index === current ? " · chosen" : ""}
                </span>
              </button>
            ))}
          </div>
          <button
            type="button"
            className="btn showcase-step"
            onClick={() => onPick((current + 1) % count)}
            aria-label={`Next photograph of ${place.label}`}
          >
            ›
          </button>
        </div>
      )}
    </div>
  );
}

export default function ShowcasePage() {
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [chosenLibraries, setChosenLibraries] = useState<Set<string>>(new Set());
  const [placeOptions, setPlaceOptions] = useState<ShowcasePlaceCount[]>([]);
  // Labels of the places to search; empty = every place.
  const [onlyPlaces, setOnlyPlaces] = useState<Set<string>>(new Set());
  const [form, setForm] = useState(DEFAULTS);
  const [places, setPlaces] = useState<ShowcasePlace[] | null>(null);
  // What the results on screen were searched for — the form may have moved on.
  const [resultKind, setResultKind] = useState<ShowcaseKind>("finished");
  const [considered, setConsidered] = useState(0);
  // Per place: which option is the pick, and whether the place is in.
  const [pickIndex, setPickIndex] = useState<Record<string, number>>({});
  const [included, setIncluded] = useState<Record<string, boolean>>({});
  const [listings, setListings] = useState<ListingSummary[]>([]);
  // "new", or the id of a listing to add to.
  const [target, setTarget] = useState("new");
  const [name, setName] = useState("GELCO calendar candidates");
  const [saved, setSaved] = useState<{ id: string; name: string; added: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .libraries()
      .then((libs) => {
        setLibraries(libs);
        // GELCO's request is why this page exists; start there if present.
        const gelco = libs.find((l) => l.name.toLowerCase().includes("gelco"));
        setChosenLibraries(new Set(gelco ? [gelco.id] : libs.map((l) => l.id)));
      })
      .catch(() => setLibraries([]));
    api
      .listings()
      .then(setListings)
      .catch(() => setListings([]));
  }, []);

  // The places the chosen libraries hold, for "only these places".
  useEffect(() => {
    if (!chosenLibraries.size) {
      setPlaceOptions([]);
      return;
    }
    api
      .showcasePlaces([...chosenLibraries])
      .then((found) => {
        setPlaceOptions(found);
        // Keep only choices that still exist.
        setOnlyPlaces((prev) => new Set([...prev].filter((l) => found.some((p) => p.label === l))));
      })
      .catch(() => setPlaceOptions([]));
  }, [chosenLibraries]);

  async function search() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.showcase({
        ...form,
        library_ids: [...chosenLibraries],
        places: [...onlyPlaces],
      });
      setPlaces(res.places);
      setResultKind(form.kind);
      setConsidered(res.considered);
      setPickIndex(Object.fromEntries(res.places.map((p) => [p.key, 0])));
      setIncluded(Object.fromEntries(res.places.map((p) => [p.key, true])));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not rank the photographs");
    } finally {
      setBusy(false);
    }
  }

  const chosenPlaces = useMemo(() => (places ?? []).filter((p) => included[p.key]), [
    places,
    included,
  ]);
  const selected = useMemo(
    () =>
      chosenPlaces
        .map((p) => ({ place: p.label, pick: p.picks[pickIndex[p.key] ?? 0] }))
        .filter((s): s is { place: string; pick: ShowcasePick } => Boolean(s.pick)),
    [chosenPlaces, pickIndex],
  );
  const everyOption = useMemo(
    () => chosenPlaces.flatMap((p) => p.picks.map((pick) => ({ place: p.label, pick }))),
    [chosenPlaces],
  );

  async function save(which: { place: string; pick: ShowcasePick }[]) {
    if (!which.length) return;
    setBusy(true);
    setError(null);
    try {
      const picks: ShowcaseListingPick[] = which.map(({ place, pick }) => ({
        asset_id: pick.asset_id,
        place,
        kind: resultKind,
        season: pick.season,
        source: pick.source,
      }));
      const listingName =
        target === "new" ? name.trim() || "Showcase" : listings.find((l) => l.id === target)?.name;
      const res = await api.showcaseListing(
        target === "new" ? { name: listingName ?? "Showcase" } : { listing_id: target },
        picks,
      );
      setSaved({ id: res.listing_id, name: listingName ?? "the listing", added: res.added });
      // The next search adds to the same listing.
      setTarget(res.listing_id);
      setListings(await api.listings());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save to the listing");
    } finally {
      setBusy(false);
    }
  }

  function toggleLibrary(id: string) {
    setChosenLibraries((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function togglePlace(label: string) {
    setOnlyPlaces((current) => {
      const next = new Set(current);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  }

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Showcase</h2>
        <span className="faint mono">the best of the work, one place at a time</span>
      </div>

      {error && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
          {error}
        </div>
      )}

      <div className="card">
        <div className="toolbar" style={{ marginTop: 0, flexWrap: "wrap" }}>
          {libraries.map((lib) => (
            <label key={lib.id} className="faint" style={{ display: "flex", gap: 6 }}>
              <input
                type="checkbox"
                checked={chosenLibraries.has(lib.id)}
                onChange={() => toggleLibrary(lib.id)}
              />
              {lib.name}
            </label>
          ))}
        </div>
        <div className="toolbar" style={{ flexWrap: "wrap" }}>
          <select
            className="select"
            value={form.kind}
            onChange={(e) => setForm({ ...form, kind: e.target.value as ShowcaseKind })}
            aria-label="Looking for"
          >
            {KINDS.map((k) => (
              <option key={k.value} value={k.value}>
                Looking for: {k.label}
              </option>
            ))}
          </select>
          <label className="faint" style={{ fontSize: "0.8rem" }}>
            The finished work is a
            <input
              className="input"
              style={{ marginLeft: 6, width: 170 }}
              value={form.subject}
              onChange={(e) => setForm({ ...form, subject: e.target.value })}
              aria-label="Subject"
            />
          </label>
          <input
            className="input"
            style={{ flex: 1, minWidth: 180 }}
            placeholder="Also avoid (comma-separated), e.g. winter, cart paths"
            value={form.avoid}
            onChange={(e) => setForm({ ...form, avoid: e.target.value })}
            aria-label="Avoid"
          />
        </div>
        {placeOptions.length > 0 && (
          <details className="showcase-places">
            <summary className="faint" style={{ fontSize: "0.8rem", cursor: "pointer" }}>
              {onlyPlaces.size
                ? `Only these places: ${onlyPlaces.size} of ${placeOptions.length}`
                : `All ${placeOptions.length} places — choose some`}
            </summary>
            <div className="toolbar" style={{ flexWrap: "wrap", gap: "4px 14px" }}>
              <button type="button" className="btn" onClick={() => setOnlyPlaces(new Set())}>
                All places
              </button>
              {placeOptions.map((p) => (
                <label
                  key={p.key}
                  className="faint"
                  style={{ display: "flex", gap: 6, fontSize: "0.8rem" }}
                >
                  <input
                    type="checkbox"
                    checked={onlyPlaces.has(p.label)}
                    onChange={() => togglePlace(p.label)}
                  />
                  {p.label} <span className="mono">({p.photos})</span>
                </label>
              ))}
            </div>
            <p className="faint" style={{ fontSize: "0.75rem", margin: "6px 0 0" }}>
              Photographs in collection folders (Photo Exports, social posts) are placed by GPS
              and count toward their place.
            </p>
          </details>
        )}
        <div className="toolbar" style={{ flexWrap: "wrap" }}>
          <label className="faint" style={{ fontSize: "0.8rem" }}>
            Places
            <input
              className="input"
              type="number"
              min={1}
              max={60}
              style={{ marginLeft: 6, width: 70 }}
              value={form.count}
              onChange={(e) => setForm({ ...form, count: Number(e.target.value) || 14 })}
            />
          </label>
          <label
            className="faint"
            style={{ fontSize: "0.8rem" }}
            title="Up to 20. A place with fewer good photographs offers fewer — its best is always shown"
          >
            Options per place
            <input
              className="input"
              type="number"
              min={1}
              max={20}
              style={{ marginLeft: 6, width: 60 }}
              value={form.alternates}
              onChange={(e) =>
                setForm({
                  ...form,
                  alternates: Math.min(20, Math.max(1, Number(e.target.value) || 3)),
                })
              }
            />
          </label>
          <select
            className="select"
            value={form.orientation}
            onChange={(e) =>
              setForm({ ...form, orientation: e.target.value as ShowcaseRequest["orientation"] })
            }
            aria-label="Orientation"
          >
            <option value="landscape">Landscape only</option>
            <option value="portrait">Portrait only</option>
            <option value="any">Any orientation</option>
          </select>
          <select
            className="select"
            value={form.min_megapixels}
            onChange={(e) => setForm({ ...form, min_megapixels: Number(e.target.value) })}
            aria-label="Minimum size"
          >
            <option value={8}>≥ 8 MP (small prints)</option>
            <option value={12}>≥ 12 MP (calendar page)</option>
            <option value={20}>≥ 20 MP (large prints)</option>
          </select>
          {form.kind === "finished" && (
            <label className="faint" style={{ display: "flex", gap: 6, fontSize: "0.8rem" }}>
              <input
                type="checkbox"
                checked={form.allow_people}
                onChange={(e) => setForm({ ...form, allow_people: e.target.checked })}
              />
              Allow people in frame
            </label>
          )}
          <button
            className="btn btn-primary"
            disabled={busy || !chosenLibraries.size}
            onClick={search}
          >
            {busy && !places ? "Ranking…" : "Find the best"}
          </button>
        </div>
      </div>

      {places && (
        <>
          <div className="sectionhead">
            <h2>
              {places.length} places · {selected.length} chosen · {everyOption.length} options
            </h2>
            <span className="faint mono">
              from {considered.toLocaleString()} photographs — choose from the strip under each
              photograph
            </span>
          </div>
          {places.length === 0 && (
            <div className="empty">
              Nothing passed — try allowing any orientation, a smaller minimum size, or a
              different subject.
            </div>
          )}
          <div className="showcase-grid">
            {places.map((place, rank) => (
              <PlaceCard
                key={place.key}
                place={place}
                rank={rank}
                current={pickIndex[place.key] ?? 0}
                on={Boolean(included[place.key])}
                onPick={(index) => setPickIndex((prev) => ({ ...prev, [place.key]: index }))}
                onToggle={(on) => setIncluded((prev) => ({ ...prev, [place.key]: on }))}
              />
            ))}
          </div>

          <div className="card" style={{ position: "sticky", bottom: 8, marginTop: 12 }}>
            <div className="toolbar" style={{ marginTop: 0, flexWrap: "wrap" }}>
              <select
                className="select"
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                aria-label="Save to"
              >
                <option value="new">Save to a new listing…</option>
                {listings.map((l) => (
                  <option key={l.id} value={l.id}>
                    Add to {l.name} ({l.item_count})
                  </option>
                ))}
              </select>
              {target === "new" && (
                <input
                  className="input"
                  style={{ flex: 1, minWidth: 200 }}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  aria-label="Listing name"
                />
              )}
              <button
                className="btn"
                disabled={busy || !selected.length}
                onClick={() => save(selected)}
                title="The chosen photograph of each place"
              >
                Add the chosen {selected.length}
              </button>
              <button
                className="btn btn-primary"
                disabled={busy || !everyOption.length}
                onClick={() => save(everyOption)}
                title="Every option of every place that is ticked"
              >
                Add every option ({everyOption.length})
              </button>
            </div>
            {saved ? (
              <span className="faint" style={{ fontSize: "0.75rem" }}>
                Added {saved.added} to <Link href={`/listings/${saved.id}`}>{saved.name}</Link>.
                Search again (crew, construction, a group photo) and add to the same listing. In
                Lightroom Classic: File → Plug-in Extras → Import FrameFound listings…, with “Copy
                them into a folder” ticked, brings the files in named by place.
              </span>
            ) : (
              <span className="faint" style={{ fontSize: "0.75rem" }}>
                Files are named by place — ledgerock-03-fall-drone — in the listing&apos;s export
                and in Lightroom.
              </span>
            )}
          </div>
        </>
      )}
    </Shell>
  );
}
