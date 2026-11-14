// Why a clip on its own page will not play, in the words the in-page viewer
// (components/MediaPreview.tsx) already uses for the same clip.
//
// The final QA found the two disagreeing: Browse's viewer said "No playable
// copy of this clip has been made yet.", and the clip's own page showed the
// poster and live-looking controls with no word at all, so clicking a shot
// moved the playhead and nothing happened. A test holds these sentences to
// the viewer's, so the two pages keep saying the same thing.

/** Said the moment the player gives up, before the server has said why, and
 *  kept when the server cannot be asked (signed out, offline). */
export const NOT_PLAYABLE = "This clip cannot be played here right now.";

/**
 * What to say about a clip whose playable copy would not load, given the
 * HTTP status the server gives for that copy (`mediaStatus(id, "proxy")`;
 * 0 when it could not be reached). 404: no copy was ever made (a library
 * with proxies off, or a clip still in the queue). 503: the copies exist on
 * a drive that is away, which "it can be made again" would misdescribe.
 */
export function cannotPlayText(status: number): string {
  if (status === 503) {
    return "The drive that holds the playable copies is not connected, so this clip cannot be played right now.";
  }
  if (status === 404) return "No playable copy of this clip has been made yet.";
  // The file is there and was served; the player still refused it.
  if (status >= 200 && status < 300) return "This browser could not play the clip.";
  return NOT_PLAYABLE;
}

/** The sentence for the clip on screen, or null when it plays (or has not
 *  failed). Kept with the clip it is about: this page is reused for the
 *  next clip, and one clip's apology must never sit under another. */
export function missingFor(
  missing: { of: string; text: string } | null,
  assetId: string,
): string | null {
  return missing && missing.of === assetId ? missing.text : null;
}
