// When the viewer should move a clip that is already playing. Free of
// imports so `npm test` can load it.

/** The moment a page asks the viewer to show: seconds into the clip, and
 *  what the moment belongs to (a face, a shot), when the page knows. */
export interface Moment {
  at?: number;
  key?: string;
}

/**
 * Where to move a player that already holds this item, or null to leave it.
 *
 * A clip starts at its moment when it loads, and the viewer keeps the same
 * player for as long as the item is the same. A page that steps between two
 * moments of one clip (two faces of one person, two shots) changed its
 * caption while the clip played on from the first moment. Only that case
 * needs a seek: another item gets a new player, which starts where asked.
 *
 * A new `key` at the same second still moves the clip back: two faces found
 * in one frame, after the reader has scrubbed away. The same moment drawn
 * again (a redraw after an answer was saved) leaves the clip where it is.
 */
export function seekOnChange(before: Moment, now: Moment): number | null {
  if (now.at == null || !Number.isFinite(now.at) || now.at < 0) return null;
  if (before.at === now.at && before.key === now.key) return null;
  return now.at;
}
