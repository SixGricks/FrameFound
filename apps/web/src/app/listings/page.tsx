"use client";

// Listings: property shoots on their way to becoming upload-ready galleries.
//
// The fast path is the address box: shoots live in folders named after the
// property, so typing "5096" finds "00-00 5096 Old Philadelphia Pike
// Kinzers" as you type, and picking it creates the listing — named from the
// address, photos imported, rooms suggested — in one gesture. Browse's
// "＋ listing" badge deep-links into the same flow with ?folder=&library=.

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import { api, type ListingFolder, type ListingSummary } from "@/lib/api";

const STATUS_LABEL: Record<string, string> = {
  none: "",
  queued: "export queued",
  exporting: "exporting…",
  ready: "zip ready",
  failed: "export failed",
};

/** "00-00 5096 Old Philadelphia Pike Kinzers" -> "5096 Old Philadelphia Pike Kinzers".
 * The date prefix is filing convention, not part of the address. */
function addressFromFolder(path: string): string {
  const leaf = path.split("/").pop() ?? path;
  return leaf.replace(/^\s*\d{2}\s*-\s*\d{2}\s*-?\s*/, "").trim() || leaf;
}

function ListingsInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [listings, setListings] = useState<ListingSummary[] | null>(null);
  const [name, setName] = useState("");
  const [folders, setFolders] = useState<ListingFolder[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);
  const ticket = useRef(0);

  const load = useCallback(async () => {
    try {
      setListings(await api.listings());
    } catch {
      setError("Could not load listings.");
    }
  }, []);

  const createFromFolder = useCallback(
    async (libraryId: string, path: string) => {
      setBusy(true);
      setError(null);
      try {
        const assets = await api.folderAssets(libraryId, path);
        if (!assets.length) throw new Error("That folder has no photographs");
        const created = await api.createListing(
          addressFromFolder(path),
          assets.map((a) => a.asset_id),
        );
        router.push(`/listings/${created.id}`);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not create the listing");
        setBusy(false);
      }
    },
    [router],
  );

  useEffect(() => {
    load();
    // Deep link from Browse: ?folder=...&library=... creates immediately —
    // the operator already said which shoot by clicking it.
    const folder = params.get("folder");
    const library = params.get("library");
    if (folder && library) {
      router.replace("/listings");
      setNotice(`Creating a listing from ${addressFromFolder(folder)}…`);
      createFromFolder(library, folder);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Address lookup, as you type. Folder names are the addresses.
  useEffect(() => {
    if (debounce.current) clearTimeout(debounce.current);
    const query = name.trim();
    if (query.length < 2) {
      setFolders([]);
      return;
    }
    debounce.current = setTimeout(async () => {
      const mine = ++ticket.current;
      try {
        const found = await api.searchFolders(query);
        if (mine === ticket.current) setFolders(found);
      } catch {
        if (mine === ticket.current) setFolders([]);
      }
    }, 250);
    return () => {
      if (debounce.current) clearTimeout(debounce.current);
    };
  }, [name]);

  async function createEmpty() {
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const created = await api.createListing(name.trim(), []);
      router.push(`/listings/${created.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create the listing");
      setBusy(false);
    }
  }

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Listings</h2>
        <span className="faint mono">
          type the address — shoots are found by their folder
        </span>
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

      <div className="toolbar" style={{ position: "relative" }}>
        <div style={{ flex: 1, minWidth: 260, position: "relative" }}>
          <input
            className="input"
            style={{ width: "100%" }}
            placeholder="Property address — matches shoot folders as you type"
            value={name}
            disabled={busy}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && folders.length === 0) createEmpty();
            }}
            aria-label="New listing address"
          />
          {folders.length > 0 && (
            <div
              className="card"
              style={{
                position: "absolute",
                top: "100%",
                left: 0,
                right: 0,
                zIndex: 30,
                marginTop: 4,
                display: "grid",
                gap: 4,
              }}
            >
              {folders.map((folder) => (
                <button
                  type="button"
                  key={`${folder.library_id}:${folder.path}`}
                  className="btn"
                  disabled={busy}
                  style={{ display: "flex", justifyContent: "space-between", gap: 8 }}
                  onClick={() => createFromFolder(folder.library_id, folder.path)}
                >
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                    {addressFromFolder(folder.path)}
                  </span>
                  <span className="faint mono">
                    {folder.image_count} photos · {folder.library_name}
                  </span>
                </button>
              ))}
              <span className="faint" style={{ fontSize: "0.72rem", padding: "0 4px" }}>
                Pick a shoot to create the listing with its photos — or press
                Enter for an empty listing named “{name.trim()}”.
              </span>
            </div>
          )}
        </div>
        <button
          className="btn btn-primary"
          disabled={busy || !name.trim()}
          onClick={createEmpty}
        >
          New empty listing
        </button>
      </div>

      {listings && listings.length === 0 && (
        <div className="empty">
          No listings yet. Type a property address above — the shoot folder and
          its photos come with it.
        </div>
      )}

      <div className="grid">
        {(listings ?? []).map((listing) => (
          <Link key={listing.id} href={`/listings/${listing.id}`} className="tile">
            <div className="tile-frame">
              {listing.cover_asset_id ? (
                <Thumb assetId={listing.cover_asset_id} mediaType="image" status="ready" />
              ) : (
                <div className="placeholder">
                  <span>empty</span>
                </div>
              )}
            </div>
            <strong>{listing.name}</strong>
            <span className="faint mono">
              {listing.item_count} photos
              {STATUS_LABEL[listing.export_status] &&
                ` · ${STATUS_LABEL[listing.export_status]}`}
            </span>
          </Link>
        ))}
      </div>
    </Shell>
  );
}

export default function ListingsPage() {
  return (
    <Suspense>
      <ListingsInner />
    </Suspense>
  );
}
