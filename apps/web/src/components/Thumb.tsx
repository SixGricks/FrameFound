"use client";

// Thumbnail with an honest empty state. The first browse session made the
// lesson clear: a missing thumbnail must say WHY (still processing, no
// preview available) instead of rendering a broken image.

import { useState } from "react";

import { mediaUrl, type MediaType } from "@/lib/api";

const LABEL: Record<string, string> = {
  pending: "Queued",
  processing: "Working",
  ready: "No preview",
  metadata_failed: "Failed",
  path_rejected: "Unavailable",
};

export default function Thumb({
  assetId,
  mediaType,
  status,
  kind = "thumbnail",
}: {
  assetId: string;
  mediaType: MediaType;
  status: string;
  kind?: string;
}) {
  const [failed, setFailed] = useState(false);
  const working = status === "pending" || status === "processing";

  if (failed || working) {
    return (
      <div className="placeholder">
        {working ? <span className="spinner" /> : <span>{mediaType}</span>}
        <span>{LABEL[status] ?? "No preview"}</span>
      </div>
    );
  }
  return (
    // eslint-disable-next-line @next/next/no-img-element -- media is served
    // by our own signed/session-guarded endpoint, not the Next image loader.
    <img
      src={mediaUrl(assetId, kind)}
      alt=""
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
    />
  );
}
