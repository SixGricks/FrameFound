"use client";

// Showcase: the best finished photographs, one per place.
//
// Built for GELCO's calendar — fourteen masterpiece photographs from
// fourteen courses — and general on purpose: subject and libraries are
// settings. The server ranks (quality, finished-not-in-progress, print size,
// no people, one per place by folder and GPS); this page is where a person
// chooses. Each place offers alternates; clicking one makes it the pick.
// The chosen set becomes a listing, and the listing's export is what gets
// sent: full-size files named after each place, a photo index, contact
// sheets to choose from.

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import {
  api,
  mediaUrl,
  type Library,
  type ShowcasePick,
  type ShowcasePlace,
  type ShowcaseRequest,
} from "@/lib/api";

const DEFAULTS: Omit<ShowcaseRequest, "library_ids"> = {
  subject: "golf course",
  avoid: "",
  count: 20,
  alternates: 3,
  orientation: "landscape",
  min_megapixels: 12,
  allow_people: false,
};

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
  const router = useRouter();
  const [libraries, setLibraries] = useState<Library[]>([]);
  const [chosenLibraries, setChosenLibraries] = useState<Set<string>>(new Set());
  const [form, setForm] = useState(DEFAULTS);
  const [places, setPlaces] = useState<ShowcasePlace[] | null>(null);
  const [considered, setConsidered] = useState(0);
  // Per place: which alternate is the pick, and whether the place is in.
  const [pickIndex, setPickIndex] = useState<Record<string, number>>({});
  const [included, setIncluded] = useState<Record<string, boolean>>({});
  const [name, setName] = useState("GELCO calendar candidates");
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
  }, []);

  async function search() {
    setBusy(true);
    setError(null);
    try {
      const res = await api.showcase({ ...form, library_ids: [...chosenLibraries] });
      setPlaces(res.places);
      setConsidered(res.considered);
      setPickIndex(Object.fromEntries(res.places.map((p) => [p.key, 0])));
      setIncluded(Object.fromEntries(res.places.map((p) => [p.key, true])));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not rank the photographs");
    } finally {
      setBusy(false);
    }
  }

  const selected = useMemo(
    () =>
      (places ?? [])
        .filter((p) => included[p.key])
        .map((p) => ({ place: p.label, pick: p.picks[pickIndex[p.key] ?? 0] }))
        .filter((s): s is { place: string; pick: ShowcasePick } => Boolean(s.pick)),
    [places, included, pickIndex],
  );

  async function makeListing() {
    if (!selected.length) return;
    setBusy(true);
    setError(null);
    try {
      const { listing_id } = await api.showcaseListing(
        name.trim() || "Showcase",
        selected.map((s) => ({ asset_id: s.pick.asset_id, place: s.place })),
      );
      router.push(`/listings/${listing_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the listing");
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

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Showcase</h2>
        <span className="faint mono">the best finished work, one photograph per place</span>
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
          <label className="faint" style={{ fontSize: "0.8rem" }}>
            Alternates each
            <input
              className="input"
              type="number"
              min={1}
              max={8}
              style={{ marginLeft: 6, width: 60 }}
              value={form.alternates}
              onChange={(e) => setForm({ ...form, alternates: Number(e.target.value) || 3 })}
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
          <label className="faint" style={{ display: "flex", gap: 6, fontSize: "0.8rem" }}>
            <input
              type="checkbox"
              checked={form.allow_people}
              onChange={(e) => setForm({ ...form, allow_people: e.target.checked })}
            />
            Allow people in frame
          </label>
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
              {places.length} places · {selected.length} chosen
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
            <div className="toolbar" style={{ marginTop: 0 }}>
              <input
                className="input"
                style={{ flex: 1, minWidth: 220 }}
                value={name}
                onChange={(e) => setName(e.target.value)}
                aria-label="Listing name"
              />
              <button
                className="btn btn-primary"
                disabled={busy || !selected.length}
                onClick={makeListing}
                title="A listing in this order, each file named after its place; export it at full size to send"
              >
                Make a listing of {selected.length}
              </button>
            </div>
            <span className="faint" style={{ fontSize: "0.75rem" }}>
              Then Export on the listing: choose “Full size” and keep the photo index and
              contact sheets — that zip is what gets sent.
            </span>
          </div>
        </>
      )}
    </Shell>
  );
}
