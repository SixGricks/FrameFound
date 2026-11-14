// Which kind of machine the Tailscale card is talking about, so it shows
// that machine's way to install Tailscale (components/TailnetCard.tsx).
//
// The Security page's answer says `platform` ("macos" / "linux"). A server
// older than the Mac install leaves it out; such a server still names the
// download page for its own kind of machine, the Mac page on a Mac, so that
// is the fallback.
//
// Only a type is imported (erased when run), so `node --test` loads this
// file as it is (tests/systemShell.test.mjs).

import type { TailscaleState } from "../../lib/api";

/** The Mac download page the server names when it runs on a Mac
 *  (MAC_DOWNLOAD in apps/server/framefound/ddns/tailscale_local.py). */
const MAC_DOWNLOAD = /^https:\/\/tailscale\.com\/download\/mac\/?$/;

/** Tailscale for any phone or laptop: the page that offers every kind of
 *  device (LINUX_DOWNLOAD in tailscale_local.py). Not `download`, which on a
 *  Mac server is the macOS page and would send a phone to the Mac app. */
export const ANY_DEVICE_DOWNLOAD = "https://tailscale.com/download";

/** Whether the server is a Mac: `platform` when the answer says it, else the
 *  download page it names. A server too old to say either is taken as
 *  Linux, which every install was before the Mac. */
export function onMac(ts: Partial<Pick<TailscaleState, "platform" | "download">>): boolean {
  if (ts.platform) return ts.platform === "macos";
  return MAC_DOWNLOAD.test(ts.download ?? "");
}
