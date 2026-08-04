"use client";

// One listing: the shoot in gallery order, each photo labelled with its room.
//
// The order IS the deliverable — MLS galleries display in upload order, so
// the numbered filenames in the exported zip are this page's arrangement,
// verbatim. Labels arrive as suggestions (dashed chip); picking one from the
// dropdown is what confirms it. Drag a photo to move it; "Arrange" resets to
// the canonical walk-through when a drag session has made a mess.

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import {
  api,
  listingExportUrl,
  type ListingDetail,
  type RoomOption,
  type SearchResponse,
} from "@/lib/api";

const POLL_MS = 3000;

export default function ListingPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const listingId = params.id;

  const [listing, setListing] = useState<ListingDetail | null>(null);
  const [rooms, setRooms] = useState<RoomOption[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const dragFrom = useRef<number | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      setListing(await api.listing(listingId));
    } catch {
      setError("Could not load this listing.");
    }
  }, [listingId]);

  useEffect(() => {
    load();
    api.rooms().then(setRooms).catch(() => setRooms([]));
  }, [load]);

  // Poll only while an export is in flight.
  const exporting =
    listing?.export_status === "queued" || listing?.export_status === "exporting";
  useEffect(() => {
    if (!exporting) return;
    timer.current = setTimeout(load, POLL_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [exporting, listing, load]);

  async function act(what: () => Promise<ListingDetail | void>) {
    setBusy(true);
    setError(null);
    try {
      const updated = await what();
      if (updated) setListing(updated);
      else await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save that");
    } finally {
      setBusy(false);
    }
  }

  async function search() {
    if (!query.trim()) return;
    setResults(await api.search(query.trim()));
  }

  function togglePick(assetId: string) {
    setPicked((current) => {
      const next = new Set(current);
      if (next.has(assetId)) next.delete(assetId);
      else next.add(assetId);
      return next;
    });
  }

  async function addPicked() {
    if (!picked.size) return;
    await act(() => api.addListingItems(listingId, [...picked]));
    setPicked(new Set());
    setResults(null);
    setQuery("");
    setAdding(false);
  }

  function drop(target: number) {
    const from = dragFrom.current;
    dragFrom.current = null;
    if (from === null || from === target || !listing) return;
    const order = [...listing.items].sort((a, b) => a.position - b.position);
    const [moved] = order.splice(from, 1);
    if (!moved) return;
    order.splice(target, 0, moved);
    act(() => api.reorderListing(listingId, order.map((i) => i.asset_id)));
  }

  async function deleteListing() {
    if (!confirm("Delete this listing? The photographs stay in the catalogue.")) return;
    setBusy(true);
    await api.deleteListing(listingId).catch(() => undefined);
    router.push("/listings");
  }

  if (error && !listing) {
    return (
      <Shell>
        <div className="empty">{error}</div>
        <Link className="btn" href="/listings">
          Back to listings
        </Link>
      </Shell>
    );
  }

  const items = [...(listing?.items ?? [])].sort((a, b) => a.position - b.position);
  // The number each image will carry in the zip: images only, in order.
  const exportNumbers = new Map<string, number>();
  items
    .filter((i) => i.media_type === "image")
    .forEach((i, index) => exportNumbers.set(i.asset_id, index + 1));

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>{listing?.name ?? "Loading…"}</h2>
        {listing && (
          <span className="faint mono">
            {items.length} photos · drag to reorder · the zip follows this order
          </span>
        )}
      </div>

      {error && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
          {error}
        </div>
      )}
      {listing?.export_status === "failed" && listing.export_error && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
          Export failed: {listing.export_error}
        </div>
      )}
      {listing?.export_status === "ready" && listing.export_error && (
        <div className="card" role="status">
          {listing.export_error}
        </div>
      )}
      {listing && !listing.classified && (
        <div className="card" role="status">
          Room suggestions are unavailable on this server — label photos with the dropdowns.
        </div>
      )}

      <div className="toolbar">
        <Link className="btn" href="/listings" style={{ padding: "6px 12px" }}>
          ← All listings
        </Link>
        <button className="btn" disabled={busy} onClick={() => setAdding((v) => !v)}>
          {adding ? "Close" : "Add photos"}
        </button>
        <button
          className="btn"
          disabled={busy || !items.length}
          onClick={() => act(() => api.arrangeListing(listingId))}
          title="Reset to the canonical walk-through: exterior, living spaces, bedrooms, outside, plans"
        >
          Arrange by room
        </button>
        <button
          className="btn btn-primary"
          disabled={busy || exporting || !items.some((i) => i.media_type === "image")}
          onClick={() => act(() => api.exportListing(listingId))}
        >
          {exporting ? "Exporting…" : "Export zip"}
        </button>
        {listing?.export_status === "ready" && (
          <a className="btn" href={listingExportUrl(listingId)}>
            Download
          </a>
        )}
        <button
          className="btn"
          style={{ marginLeft: "auto", borderColor: "var(--ember)", color: "var(--ember)" }}
          disabled={busy}
          onClick={deleteListing}
        >
          Delete listing
        </button>
      </div>

      {adding && (
        <div className="card">
          <div className="toolbar" style={{ marginTop: 0 }}>
            <input
              className="input"
              style={{ flex: 1, minWidth: 220 }}
              placeholder="Search the catalogue — filename, folder, or what's in the photo"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") search();
              }}
              aria-label="Search for photos to add"
            />
            <button className="btn" onClick={search}>
              Search
            </button>
            {picked.size > 0 && (
              <button className="btn btn-primary" disabled={busy} onClick={addPicked}>
                Add {picked.size} photo{picked.size === 1 ? "" : "s"}
              </button>
            )}
          </div>
          {results && (
            <div className="grid">
              {[
                ...results.visual_hits.map((h) => ({
                  asset_id: h.asset_id,
                  filename: h.filename,
                })),
                ...results.filename_hits.map((h) => ({
                  asset_id: h.asset_id,
                  filename: h.filename,
                })),
              ]
                .filter(
                  (hit, index, all) =>
                    all.findIndex((other) => other.asset_id === hit.asset_id) === index &&
                    !items.some((i) => i.asset_id === hit.asset_id),
                )
                .map((hit) => (
                  <button
                    type="button"
                    key={hit.asset_id}
                    className="tile"
                    data-selected={picked.has(hit.asset_id)}
                    style={
                      picked.has(hit.asset_id)
                        ? { outline: "2px solid var(--amber)" }
                        : undefined
                    }
                    onClick={() => togglePick(hit.asset_id)}
                    title={hit.filename}
                  >
                    <div className="tile-frame">
                      <Thumb assetId={hit.asset_id} mediaType="image" status="ready" />
                    </div>
                    <span className="faint mono" style={{ fontSize: "0.7rem" }}>
                      {hit.filename}
                    </span>
                  </button>
                ))}
            </div>
          )}
        </div>
      )}

      <div className="grid">
        {items.map((item, index) => (
          <div
            key={item.asset_id}
            className="tile"
            draggable
            onDragStart={() => {
              dragFrom.current = index;
            }}
            onDragOver={(e) => e.preventDefault()}
            onDrop={() => drop(index)}
            title={`${item.filename} — drag to reorder`}
          >
            <div className="tile-frame" style={{ position: "relative" }}>
              <Thumb assetId={item.asset_id} mediaType={item.media_type} status="ready" />
              <span
                className="pill mono"
                style={{ position: "absolute", top: 6, left: 6 }}
                title={
                  item.media_type === "image"
                    ? "Position in the exported zip"
                    : "Videos stay out of the photo zip"
                }
              >
                {item.media_type === "image"
                  ? String(exportNumbers.get(item.asset_id)).padStart(2, "0")
                  : "video"}
              </span>
            </div>
            <select
              className="select"
              aria-label={`Room for ${item.filename}`}
              value={item.room}
              disabled={busy}
              // A suggested label reads tentative; picking is what confirms.
              style={
                item.room && item.room_source === "suggested"
                  ? { borderStyle: "dashed" }
                  : undefined
              }
              onChange={(e) =>
                act(() => api.setListingRoom(listingId, item.asset_id, e.target.value))
              }
            >
              <option value="">
                {item.room ? "— clear label —" : "unlabelled"}
              </option>
              {rooms.map((room) => (
                <option key={room.key} value={room.key}>
                  {room.label}
                  {item.room === room.key && item.room_source === "suggested"
                    ? " (suggested)"
                    : ""}
                </option>
              ))}
            </select>
            <div style={{ display: "flex", gap: 4 }}>
              {item.media_type === "image" && (
                <Link
                  className="btn"
                  style={{ fontSize: "0.7rem", padding: "2px 8px" }}
                  href={`/edit/${item.asset_id}?listing=${listingId}`}
                  title={item.edited ? "Edited — click to adjust" : "Colour-correct this photo"}
                >
                  {item.edited ? "Edited ✦" : "Edit"}
                </Link>
              )}
              <button
                className="btn"
                style={{ fontSize: "0.7rem", padding: "2px 8px" }}
                disabled={busy}
                onClick={() => act(() => api.removeListingItem(listingId, item.asset_id))}
              >
                Remove
              </button>
            </div>
          </div>
        ))}
      </div>

      {listing && !items.length && (
        <div className="empty">
          Nothing here yet — “Add photos” searches the catalogue.
        </div>
      )}
    </Shell>
  );
}
