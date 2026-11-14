// Asking the server why a file would not load.
//
// No imports, so `node --test` can load this file as it is
// (tests/probe.test.mjs).

/**
 * The HTTP status the server gives for a file a player or a picture could
 * not load (0 when the server could not be reached). A <video> or <img> only
 * reports "error"; the server tells a file that was never made (404) from
 * one whose drive is away (503), and the page says which.
 *
 * One byte is asked for, so a file that is in fact there is not downloaded
 * to find that out. And the question is never answered from the browser's
 * cache: derivatives are served cacheable for a day, so a clip played this
 * morning answered "here it is" for its first byte after its drive had gone
 * — and the viewer blamed the browser for a disconnected drive.
 */
export async function urlStatus(url: string): Promise<number> {
  try {
    const resp = await fetch(url, {
      headers: { Range: "bytes=0-0" },
      credentials: "same-origin",
      cache: "no-store",
    });
    resp.body?.cancel().catch(() => undefined);
    return resp.status;
  } catch {
    return 0;
  }
}
