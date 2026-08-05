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
  type AiEditSettings,
  type FolderAsset,
  type SkyAsset,
  type ListingDetail,
  type ListingFolder,
  type RemovalSuggestion,
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
  const [notice, setNotice] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [addMode, setAddMode] = useState<"folders" | "search">("folders");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [folders, setFolders] = useState<ListingFolder[] | null>(null);
  const [folderAssets, setFolderAssets] = useState<FolderAsset[] | null>(null);
  const [openFolder, setOpenFolder] = useState<ListingFolder | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [aiSettings, setAiSettings] = useState<AiEditSettings | null>(null);
  const [aiRunning, setAiRunning] = useState(false);
  const [skies, setSkies] = useState<SkyAsset[]>([]);
  const [skyChoice, setSkyChoice] = useState<string>("");
  const [maxEdge, setMaxEdge] = useState(3840);
  const [quality, setQuality] = useState(85);
  const [suggestions, setSuggestions] = useState<RemovalSuggestion[] | null>(null);
  const [curating, setCurating] = useState(false);
  const editedBaseline = useRef(0);
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
    api.aiEditSettings().then(setAiSettings).catch(() => setAiSettings(null));
    api.skies().then(setSkies).catch(() => setSkies([]));
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

  const searchTicket = useRef(0);

  async function search() {
    const q = query.trim();
    if (!q) return;
    const mine = ++searchTicket.current;
    if (addMode === "folders") {
      setOpenFolder(null);
      setFolderAssets(null);
      const found = await api.searchFolders(q);
      if (mine === searchTicket.current) setFolders(found);
    } else {
      const found = await api.search(q);
      if (mine === searchTicket.current) setResults(found);
    }
  }

  // Search as you type: results arrive without pressing anything, and the
  // ticket makes the latest keystroke win over slower earlier requests.
  useEffect(() => {
    if (!adding) return;
    const q = query.trim();
    if (q.length < 2) return;
    const timer = setTimeout(() => search(), 300);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, addMode, adding]);

  async function browseFolder(folder: ListingFolder) {
    setOpenFolder(folder);
    setFolderAssets(await api.folderAssets(folder.library_id, folder.path));
  }

  async function aiEditAll() {
    setBusy(true);
    setError(null);
    try {
      const { queued, mode } = await api.aiEditListing(listingId, skyChoice || null);
      editedBaseline.current = (listing?.items ?? []).filter(
        (i) => i.media_type === "image" && i.edited,
      ).length;
      setAiRunning(true);
      setNotice(
        mode === "ai"
          ? `AI editing ${queued} photos — a preview of each goes to the Claude API, ` +
              `slider values come back, and the full-resolution render happens here.` +
              (skyChoice ? ` Skies swap in wherever the photo has sky.` : "")
          : `Auto-editing ${queued} photos with the listing preset, entirely on this ` +
              `machine.` +
              (skyChoice ? ` Skies swap in wherever the photo has sky.` : "") +
              ` Add an Anthropic key on Security for per-photo AI judgment.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start auto-editing");
    } finally {
      setBusy(false);
    }
  }

  // While an AI run is in flight, refresh so edited badges appear; stop when
  // every image is edited or after 15 minutes, whichever comes first.
  useEffect(() => {
    if (!aiRunning) return;
    const started = Date.now();
    const timer = setInterval(async () => {
      await load();
      const all = (listing?.items ?? []).filter((i) => i.media_type === "image");
      if (
        (all.length > 0 && all.every((i) => i.edited)) ||
        Date.now() - started > 15 * 60 * 1000
      ) {
        setAiRunning(false);
      }
    }, 5000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aiRunning]);

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

  async function runCuration() {
    setCurating(true);
    setError(null);
    try {
      setSuggestions(await api.curateListing(listingId));
    } catch {
      setError("Could not analyse the listing");
    } finally {
      setCurating(false);
    }
  }

  async function removeSuggested(assetIds: string[]) {
    setBusy(true);
    try {
      for (const id of assetIds) {
        await api.removeListingItem(listingId, id);
      }
      setSuggestions((current) =>
        current ? current.filter((s) => !assetIds.includes(s.asset_id)) : current,
      );
      await load();
    } finally {
      setBusy(false);
    }
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
  const imageCount = items.filter((i) => i.media_type === "image").length;
  const editedImages = items.filter((i) => i.media_type === "image" && i.edited).length;
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
      {notice && !error && (
        <div className="card" role="status">
          {notice}
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
      </div>

      <div style={{ display: "flex", gap: 16, alignItems: "flex-start" }}>
        <div style={{ flex: 1, minWidth: 0 }}>

      {adding && (
        <div className="card">
          <div className="toolbar" style={{ marginTop: 0 }}>
            <button
              className="btn"
              data-active={addMode === "folders"}
              style={addMode === "folders" ? { borderColor: "var(--amber)" } : undefined}
              onClick={() => setAddMode("folders")}
            >
              Folders
            </button>
            <button
              className="btn"
              data-active={addMode === "search"}
              style={addMode === "search" ? { borderColor: "var(--amber)" } : undefined}
              onClick={() => setAddMode("search")}
            >
              Search
            </button>
            <input
              className="input"
              style={{ flex: 1, minWidth: 220 }}
              placeholder={
                addMode === "folders"
                  ? "Folder name — e.g. the property address"
                  : "Search the catalogue — filename, folder, or what's in the photo"
              }
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
          {addMode === "folders" && !openFolder && folders && (
            <div style={{ display: "grid", gap: 6 }}>
              {folders.length === 0 && (
                <div className="empty">No folders match that name.</div>
              )}
              {folders.map((folder) => (
                <button
                  type="button"
                  key={`${folder.library_id}:${folder.path}`}
                  className="btn"
                  style={{ justifyContent: "space-between", display: "flex", gap: 8 }}
                  onClick={() => browseFolder(folder)}
                >
                  <span className="mono" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                    {folder.path || "(library root)"}
                  </span>
                  <span className="faint mono">
                    {folder.library_name} · {folder.image_count} photos
                  </span>
                </button>
              ))}
            </div>
          )}
          {addMode === "folders" && openFolder && folderAssets && (
            <>
              <div className="toolbar" style={{ marginTop: 0 }}>
                <button className="btn" onClick={() => setOpenFolder(null)}>
                  ← Folders
                </button>
                <span className="faint mono" style={{ flex: 1 }}>
                  {openFolder.path || "(library root)"} · {folderAssets.length} photos
                </span>
                <button
                  className="btn"
                  onClick={() => {
                    const inListing = new Set(items.map((i) => i.asset_id));
                    setPicked(
                      new Set(
                        folderAssets
                          .filter((a) => !inListing.has(a.asset_id))
                          .map((a) => a.asset_id),
                      ),
                    );
                  }}
                >
                  Select all new
                </button>
                <button className="btn" onClick={() => setPicked(new Set())}>
                  Clear
                </button>
              </div>
              <div className="grid">
                {folderAssets.map((a) => {
                  const already = items.some((i) => i.asset_id === a.asset_id);
                  return (
                    <button
                      type="button"
                      key={a.asset_id}
                      className="tile"
                      disabled={already}
                      style={
                        picked.has(a.asset_id)
                          ? { outline: "2px solid var(--amber)" }
                          : already
                            ? { opacity: 0.4 }
                            : undefined
                      }
                      onClick={() => togglePick(a.asset_id)}
                      title={already ? `${a.filename} — already in this listing` : a.filename}
                    >
                      <div className="tile-frame">
                        <Thumb assetId={a.asset_id} mediaType="image" status="ready" />
                      </div>
                      <span className="faint mono" style={{ fontSize: "0.7rem" }}>
                        {a.filename}
                      </span>
                    </button>
                  );
                })}
              </div>
            </>
          )}
          {addMode === "search" && results && (
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

      {suggestions && (
        <div className="card">
          <div className="sectionhead" style={{ marginTop: 0 }}>
            <h2>Suggested removals</h2>
            <span className="faint mono">
              {suggestions.length
                ? "every room keeps at least one photo"
                : "nothing worth removing — the shoot is tight"}
            </span>
          </div>
          {suggestions.length > 0 && (
            <div className="toolbar" style={{ marginTop: 0 }}>
              <button
                className="btn btn-primary"
                disabled={busy}
                onClick={() => removeSuggested(suggestions.map((s) => s.asset_id))}
              >
                Remove all {suggestions.length}
              </button>
              <button className="btn" onClick={() => setSuggestions(null)}>
                Dismiss
              </button>
            </div>
          )}
          <div className="grid">
            {suggestions.map((s) => (
              <div key={s.asset_id} className="tile">
                <div className="tile-frame">
                  <Thumb assetId={s.asset_id} mediaType="image" status="ready" />
                </div>
                <span className="faint" style={{ fontSize: "0.7rem" }}>{s.reason}</span>
                <div style={{ display: "flex", gap: 4 }}>
                  <button
                    className="btn"
                    style={{ fontSize: "0.7rem", padding: "2px 8px" }}
                    disabled={busy}
                    onClick={() => removeSuggested([s.asset_id])}
                  >
                    Remove
                  </button>
                  <button
                    className="btn"
                    style={{ fontSize: "0.7rem", padding: "2px 8px" }}
                    onClick={() =>
                      setSuggestions((cur) =>
                        cur ? cur.filter((x) => x.asset_id !== s.asset_id) : cur,
                      )
                    }
                  >
                    Keep
                  </button>
                </div>
              </div>
            ))}
          </div>
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
              {aiRunning && item.media_type === "image" && !item.edited && (
                <span
                  className="pill mono"
                  style={{ position: "absolute", bottom: 6, left: 6 }}
                >
                  <span className="spinner" /> editing…
                </span>
              )}
              {aiRunning && item.media_type === "image" && item.edited && (
                <span
                  className="pill mono"
                  style={{ position: "absolute", bottom: 6, left: 6 }}
                >
                  ✓ edited
                </span>
              )}
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

        </div>

        {/* Listing-wide controls live here, out of the photo flow: the grid
            is the work surface, the rail is what happens to the whole shoot. */}
        <aside
          style={{
            width: 250,
            flexShrink: 0,
            position: "sticky",
            top: 12,
            display: "grid",
            gap: 10,
          }}
        >
          <div className="card">
            <div className="mono" style={{ marginBottom: 6 }}>Auto-edit</div>
            <select
              className="select"
              style={{ width: "100%" }}
              aria-label="Sky replacement for auto-edit"
              value={skyChoice}
              disabled={busy || aiRunning}
              onChange={(e) => setSkyChoice(e.target.value)}
              title="Composited wherever a photo has sky; interiors pass through untouched"
            >
              <option value="">Keep skies as shot</option>
              {skies.map((sky) => (
                <option key={sky.name} value={sky.name}>
                  Sky: {sky.name.replace(/-\d+\.(jpg|jpeg|png|webp)$/i, "")}
                </option>
              ))}
            </select>
            <button
              className="btn btn-primary"
              style={{ width: "100%", marginTop: 6 }}
              disabled={busy || aiRunning || !items.some((i) => i.media_type === "image")}
              onClick={aiEditAll}
              title={
                aiSettings?.configured && aiSettings.enabled
                  ? "Per-photo AI judgment via the Claude API; renders happen here at full resolution"
                  : "Tuned listing preset, entirely local. Add an Anthropic key on Security for per-photo AI judgment."
              }
            >
              {aiRunning
                ? `Editing ${editedImages}/${imageCount}…`
                : "Auto-edit photos"}
            </button>
            <span className="faint" style={{ fontSize: "0.72rem" }}>
              {aiSettings?.configured && aiSettings.enabled
                ? "Claude picks per-photo settings; renders stay local."
                : "Local preset. Add a key on Security for per-photo AI."}
            </span>
          </div>

          <div className="card">
            <div className="mono" style={{ marginBottom: 6 }}>Rooms & order</div>
            <button
              className="btn"
              style={{ width: "100%" }}
              disabled={busy || !items.length}
              onClick={() => act(() => api.arrangeListing(listingId))}
              title="Reset to the canonical walk-through: exterior, living spaces, bedrooms, outside, plans"
            >
              Arrange by room
            </button>
            <button
              className="btn"
              style={{ width: "100%", marginTop: 6 }}
              disabled={busy || !items.length}
              onClick={() => act(() => api.reclassifyListing(listingId))}
              title="Re-suggest room labels for everything you have not confirmed"
            >
              Re-suggest rooms
            </button>
          </div>

          <div className="card">
            <div className="mono" style={{ marginBottom: 6 }}>Curate</div>
            <button
              className="btn"
              style={{ width: "100%" }}
              disabled={busy || curating || items.length < 3}
              onClick={runCuration}
              title="Find near-duplicates and soft frames the listing can afford to lose — every room keeps at least one photo"
            >
              {curating ? "Analysing…" : "Suggest removals"}
            </button>
          </div>

          <div className="card">
            <div className="mono" style={{ marginBottom: 6 }}>Export</div>
            <select
              className="select"
              style={{ width: "100%" }}
              aria-label="Export size"
              value={maxEdge}
              onChange={(e) => setMaxEdge(Number(e.target.value))}
            >
              <option value={2048}>2048 px longest edge</option>
              <option value={3840}>3840 px longest edge</option>
              <option value={4096}>4096 px longest edge</option>
            </select>
            <select
              className="select"
              style={{ width: "100%", marginTop: 6 }}
              aria-label="Export quality"
              value={quality}
              onChange={(e) => setQuality(Number(e.target.value))}
            >
              <option value={80}>Quality 80</option>
              <option value={85}>Quality 85</option>
              <option value={90}>Quality 90</option>
            </select>
            <button
              className="btn btn-primary"
              style={{ width: "100%", marginTop: 6 }}
              disabled={busy || exporting || !items.some((i) => i.media_type === "image")}
              onClick={() => act(() => api.exportListing(listingId, maxEdge, quality))}
            >
              {exporting ? "Exporting…" : "Export zip"}
            </button>
            {listing?.export_status === "ready" && (
              <a
                className="btn"
                style={{ width: "100%", marginTop: 6, display: "block", textAlign: "center" }}
                href={listingExportUrl(listingId)}
              >
                Download
              </a>
            )}
          </div>

          <button
            className="btn"
            style={{ borderColor: "var(--ember)", color: "var(--ember)" }}
            disabled={busy}
            onClick={deleteListing}
          >
            Delete listing
          </button>
        </aside>
      </div>
    </Shell>
  );
}
