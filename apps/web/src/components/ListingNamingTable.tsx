"use client";

// The listing as its delivery will read: each photo's number, the file name
// it will ship under, and the line the photo index says about it.
//
// Edits save when a field loses focus, and typing is what confirms a name —
// a later AI run leaves confirmed names alone (dashed = AI suggestion, the
// same visual contract as the room labels).

import { useEffect, useState } from "react";

import Thumb from "@/components/Thumb";
import type { ListingItem } from "@/lib/api";

type Props = {
  items: ListingItem[];
  busy: boolean;
  onSave: (assetId: string, caption: string, slug: string) => Promise<void>;
};

export default function ListingNamingTable({ items, busy, onSave }: Props) {
  const images = items.filter((i) => i.media_type === "image");
  if (!images.length) return <div className="empty">No photos to name yet.</div>;
  return (
    <div className="tablewrap card" style={{ padding: 0 }}>
      <table className="table">
        <thead>
          <tr>
            <th scope="col" style={{ width: 96 }}>
              Photo
            </th>
            <th scope="col">File name</th>
            <th scope="col">What it shows</th>
            <th scope="col">Original</th>
          </tr>
        </thead>
        <tbody>
          {images.map((item) => (
            <NamingRow key={item.asset_id} item={item} busy={busy} onSave={onSave} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function NamingRow({
  item,
  busy,
  onSave,
}: {
  item: ListingItem;
  busy: boolean;
  onSave: Props["onSave"];
}) {
  const [caption, setCaption] = useState(item.caption);
  const [slug, setSlug] = useState(item.slug);
  // Follow the server when it changes underneath (an AI run landing), but
  // never while the operator has unsaved typing in the field.
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (dirty) return;
    setCaption(item.caption);
    setSlug(item.slug);
  }, [item.caption, item.slug, dirty]);

  async function save() {
    if (!dirty) return;
    if (caption.trim() !== item.caption || slug.trim() !== item.slug) {
      await onSave(item.asset_id, caption, slug);
    }
    // Only now: clearing it first would let the effect above snap the field
    // back to the old server value while the save is still in flight.
    setDirty(false);
  }

  const suggested = item.naming_source === "suggested";
  const fieldStyle = {
    width: "100%",
    padding: "6px 9px",
    fontSize: "0.82rem",
    ...(suggested ? { borderStyle: "dashed" as const } : {}),
  };

  return (
    <tr>
      <td>
        <div style={{ width: 88, position: "relative" }}>
          <Thumb assetId={item.asset_id} mediaType="image" status="ready" />
        </div>
      </td>
      <td style={{ minWidth: 220 }}>
        <input
          className="input mono"
          style={fieldStyle}
          aria-label={`File-name words for ${item.filename}`}
          placeholder={item.room ? item.room.replace(/_/g, "-") : "photo"}
          value={slug}
          disabled={busy}
          onChange={(e) => {
            setSlug(e.target.value);
            setDirty(true);
          }}
          onBlur={save}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
        />
        <div
          className="faint mono"
          style={{ fontSize: "0.68rem", marginTop: 4, wordBreak: "break-all" }}
          title="The name this photo ships under"
        >
          {item.export_name}
        </div>
      </td>
      <td style={{ minWidth: 260 }}>
        <input
          className="input"
          style={fieldStyle}
          aria-label={`What ${item.filename} shows`}
          placeholder={item.room_label || "What this photo shows"}
          value={caption}
          disabled={busy}
          maxLength={300}
          onChange={(e) => {
            setCaption(e.target.value);
            setDirty(true);
          }}
          onBlur={save}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
        />
        {suggested && (
          <div className="faint" style={{ fontSize: "0.68rem", marginTop: 4 }}>
            AI suggestion — edit to confirm
          </div>
        )}
      </td>
      <td className="faint mono" style={{ fontSize: "0.75rem" }}>
        {item.filename}
      </td>
    </tr>
  );
}
