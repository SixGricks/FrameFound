// The listing page's decisions that do not need React: what to say when an
// auto-edit run has ended, and what "Add photos > Search" offers. Kept apart
// from page.tsx so tests can run them (tests/listings-slideshows.test.mjs).

import type { FolderAsset, ListingDetail, SearchResponse } from "@/lib/api";

/** What the page says about a run that has ended, or null while it has not. */
export type RunEnd = { kind: "notice" | "error"; text: string };

/**
 * The sentence for a run that is over: "done" or "failed" on the listing.
 *
 * Asked both when the page sees a run end and straight after starting one.
 * A one-photograph local Apply can finish before the first reload after
 * pressing Apply, and the page then went on saying "Applying the local
 * model's suggestions…" for good: it only said "finished" when it had seen
 * the run queued or running first.
 */
export function runEnded(
  listing: Pick<ListingDetail, "ai_edit_state" | "ai_edit_message"> | null,
): RunEnd | null {
  if (listing?.ai_edit_state === "done") {
    return { kind: "notice", text: `Auto-edit finished: ${listing.ai_edit_message}` };
  }
  if (listing?.ai_edit_state === "failed") {
    return { kind: "error", text: `Auto-edit ${listing.ai_edit_message || "failed"}` };
  }
  return null;
}

/**
 * The photographs a search offers to add: everything it found, best first,
 * once each, leaving out what the listing already has.
 *
 * By what is in the photograph as well as by name. Only the picture and
 * file-name matches were read, so with picture search unavailable "kitchen"
 * offered nothing although thirty photographs carried the tag.
 */
export function searchOffers(
  results: SearchResponse | null,
  inListing: ReadonlySet<string>,
): { offers: FolderAsset[]; found: number } {
  if (!results) return { offers: [], found: 0 };
  const all = [
    ...results.results,
    ...results.visual_hits,
    ...results.filename_hits,
    ...results.tag_hits,
    ...(results.caption_hits ?? []),
  ];
  const seen = new Set<string>();
  const offers: FolderAsset[] = [];
  for (const hit of all) {
    if (seen.has(hit.asset_id)) continue;
    seen.add(hit.asset_id);
    if (!inListing.has(hit.asset_id)) {
      offers.push({ asset_id: hit.asset_id, filename: hit.filename, media_type: hit.media_type });
    }
  }
  return { offers, found: seen.size };
}

/** The line under a search that offers nothing, so an empty panel says why. */
export function noOffersLine(query: string, found: number): string {
  return found > 0
    ? `Everything that matched “${query}” is already in this listing.`
    : `Nothing matched “${query}”. Search looks at file names, folders, tags and what is in the photo.`;
}
