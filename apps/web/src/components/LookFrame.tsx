"use client";

// A tile's picture, as the thing that opens the viewer (MediaPreview).
//
// Several grids showed a picture that could only be judged at 212 px: a
// listing's own gallery (where a video could not be opened or played at
// all), its suggested removals, the photos offered to add to it, and a
// slideshow's proposal. Each now opens the viewer over the page, so looking
// costs nothing the page was holding — an open panel, a selection, a search.
//
// It is a link to the asset's own page rather than a button, for two
// reasons. A modified or middle click still does what it does on any link
// (the full page in a new tab), so the direct route is never taken away.
// And a listing's tiles are dragged by their pictures to reorder them:
// Firefox does not start a drag from a <button>, while a link drags like
// the image inside it.
//
// A page can draw its own picture in place of the asset's thumbnail
// (`picture`): Compare shows the stills a model was shown, frozen when the
// comparison was made, rather than whatever the catalogue holds now. It can
// also say where the link goes (`href`): a shot's tile opens the asset page
// at the shot, not at the clip's start. Left out, the tile is exactly as it
// was.

import type { MouseEvent, ReactNode } from "react";
import Link from "next/link";

import Thumb from "@/components/Thumb";
import type { MediaType } from "@/lib/api";

export default function LookFrame({
  assetId,
  mediaType,
  filename,
  onLook,
  focusKey,
  hint = "click to look",
  picture,
  href,
  children,
}: {
  assetId: string;
  mediaType: MediaType;
  filename: string;
  /** Open the viewer on this item. */
  onLook: () => void;
  /** What the pointer can do here, after the file name in the tooltip. */
  hint?: string;
  /** Written to `data-look`, for a page to find this picture again and
   *  return focus to it when the viewer closes. A page that shows one asset
   *  in two grids gives each its own; by default, the asset id. */
  focusKey?: string;
  /** Drawn instead of the asset's thumbnail. */
  picture?: ReactNode;
  /** Where a modified or middle click goes; by default the asset's page. */
  href?: string;
  /** Badges drawn over the picture. */
  children?: ReactNode;
}) {
  function onClick(event: MouseEvent<HTMLAnchorElement>) {
    // With a modifier held the click is the browser's: a new tab or window.
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    onLook();
  }

  return (
    <Link
      className="tile-frame tile-look"
      href={href ?? `/assets/${assetId}`}
      // Not prefetched: a grid of two hundred would fetch two hundred pages
      // nobody asked for.
      prefetch={false}
      data-look={focusKey ?? assetId}
      aria-haspopup="dialog"
      aria-label={`Look at ${filename}`}
      title={`${filename} — ${hint}`}
      onClick={onClick}
    >
      {picture ?? <Thumb assetId={assetId} mediaType={mediaType} status="ready" />}
      {mediaType !== "image" && <span className="tagpick-play" aria-hidden="true" />}
      {children}
    </Link>
  );
}
