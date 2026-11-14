// Words for the Slideshows page that tests can read
// (tests/listings-slideshows.test.mjs).

/**
 * What the page says beside Render when the render was not taken: the
 * server's reason, and that nothing was made. It used to say only "The
 * render could not be queued." at the top of the page, far above the button,
 * while a slideshow that would never render was left in the list each time.
 * The server now makes nothing when it refuses, and the selection stays on
 * the page to try again.
 */
export function renderRefused(why: string | null): string {
  let reason = (why ?? "").trim().replace(/\.$/, "") || "the server could not be reached";
  // The server's reason is a sentence of its own ("The processing queue is
  // unavailable"); after the colon it reads on in lower case. An acronym
  // ("API key …", two capitals) keeps its case.
  if (/^[A-Z][a-z]/.test(reason)) {
    reason = reason.charAt(0).toLowerCase() + reason.slice(1);
  }
  return `Not rendered: ${reason}. Nothing was made, and your selection is still here to render again.`;
}
