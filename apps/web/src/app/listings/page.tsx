"use client";

// Listings: property shoots on their way to becoming upload-ready galleries.

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import { api, type ListingSummary } from "@/lib/api";

const STATUS_LABEL: Record<string, string> = {
  none: "",
  queued: "export queued",
  exporting: "exporting…",
  ready: "zip ready",
  failed: "export failed",
};

export default function ListingsPage() {
  const router = useRouter();
  const [listings, setListings] = useState<ListingSummary[] | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setListings(await api.listings());
    } catch {
      setError("Could not load listings.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function create() {
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
          ordered and named for upload — 01_front_exterior.jpg leads
        </span>
      </div>

      {error && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
          {error}
        </div>
      )}

      <div className="toolbar">
        <input
          className="input"
          style={{ flex: 1, minWidth: 220 }}
          placeholder="Property address or shoot name"
          value={name}
          disabled={busy}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") create();
          }}
          aria-label="New listing name"
        />
        <button className="btn btn-primary" disabled={busy || !name.trim()} onClick={create}>
          New listing
        </button>
      </div>

      {listings && listings.length === 0 && (
        <div className="empty">
          No listings yet. Create one, then add the shoot&apos;s photos from its page.
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
