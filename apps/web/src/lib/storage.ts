// The words and sums of the Storage page's "where the bulky files live"
// section (components/BulkStorage.tsx), kept apart from the page so they can
// be tested.
//
// Nothing is imported at run time (the one import is types only), so
// `node --test` can load this file as it is (tests/storage.test.mjs).

import type { KindStatus, MoveStatus, StorageKinds } from "./api";

const MB = 1024 ** 2;
const GB = 1024 ** 3;

// What the client puts in place of a sentence when an answer had none in it
// (see `request` in api.ts).
const FILLER = /^Request failed \(\d+\)$/;

/**
 * How long a removal of old copies that was never answered is taken to be
 * still running. The server sees a removal through whatever happens to the
 * connection, and thousands of deletions on a share take minutes; after this
 * long the line offers the button again, and asking again is harmless (it
 * removes what is left and gives a real answer).
 */
export const REMOVAL_WAIT_MS = 3 * 60_000;

/**
 * A size for this page: GB with one decimal, whole MB below 1 GB.
 *
 * Not `bytes()` from format.ts: that one rounds to whole units from 10 up,
 * so a 40 GB copy read "12 GB of 40 GB" for minutes at a time and looked
 * stuck, and it changes unit (KB, TB) from one row to the next, which makes
 * a column of sizes hard to compare.
 */
export function diskSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "—";
  // 1023.6 MB would otherwise read "1,024 MB".
  if (bytes >= GB || Math.round(bytes / MB) >= 1024) {
    return `${(bytes / GB).toLocaleString("en-US", {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    })} GB`;
  }
  if (bytes > 0 && bytes < MB) return "under 1 MB";
  return `${Math.round(bytes / MB)} MB`;
}

/** The server gives a disk's free and total space in GB; `diskSize` takes bytes. */
export function gbToBytes(gb: number | null | undefined): number | null {
  return gb === null || gb === undefined ? null : gb * GB;
}

export function count(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString("en-US")} ${n === 1 ? one : many}`;
}

/** How a kind is spoken of in the middle of a sentence: "video proxies". */
export function things(label: string): string {
  return label.toLowerCase();
}

export const WHERE_WORDS: Record<KindStatus["where"], string> = {
  local: "On this machine",
  bulk: "In the bulk folder",
  custom: "A folder of its own",
  none: "Nowhere yet",
};

/**
 * Whether the server's folders read like a Mac's. Only then is
 * /Volumes/Photos/Framefound a sensible example of a bulk folder: on the
 * containers it is a path that cannot exist.
 */
export function looksLikeMac(paths: Array<string | null | undefined>): boolean {
  return paths.some((path) => !!path && /^\/(Users|Volumes)\//.test(path));
}

/** What the folder field is filled in with when a kind's move form opens. */
export function suggestedTarget(kind: KindStatus): string {
  if (kind.bulk_path) return kind.bulk_path;
  // A store that is away cannot be copied from. Its replacement is usually
  // a new drive under the old name, started empty — the same folder.
  if (!kind.connected && kind.path) return kind.path;
  return "";
}

/** How a kind's files come back after its folder was started empty; the
 *  end of a sentence. */
export function comesBack(kind: string): string {
  switch (kind) {
    case "proxies":
      return "the proxies are made again from the originals (“framefound proxies rebuild” in a terminal)";
    case "renders":
      return "each slideshow’s video comes back when you press Render again";
    case "exports":
      return "each listing’s zip comes back when you export it again";
    case "basemaps":
      return "the maps are downloaded again on the Basemaps page";
    case "models":
      // Downloaded, not made: "the files are made again" promised what
      // cannot happen for a model.
      return "the models are downloaded again on the AI models page";
    case "backups":
      return "backups made from now on are copied there";
    default:
      return "the files are made again";
  }
}

export interface PhaseWords {
  /** A word or two for the status chip. */
  short: string;
  tone: "ok" | "warn" | "bad" | undefined;
  /** What is happening, or how it ended. */
  sentence: string;
}

export function phaseWords(move: MoveStatus): PhaseWords {
  const name = things(move.label);
  switch (move.phase) {
    case "queued":
      return {
        short: "Waiting",
        tone: "warn",
        sentence:
          "Waiting for its turn. If a video is being converted this can take a while. " +
          "Nothing has been changed yet.",
      };
    case "checking":
      return { short: "Checking", tone: "warn", sentence: "Checking the new folder." };
    case "copying":
      return {
        short: "Copying",
        tone: "warn",
        sentence: "Copying. Everything keeps working while this runs.",
      };
    case "finishing":
      return {
        short: "Finishing",
        tone: "warn",
        sentence: `Nearly done: bringing over what changed during the copy. New ${name} wait a moment.`,
      };
    case "switching":
      return {
        short: "Switching over",
        tone: "warn",
        sentence: "Checking that everything arrived, then switching over to the new folder.",
      };
    case "done":
      return {
        short: "Finished",
        tone: "ok",
        sentence: `Finished. ${move.label} now live in the new folder.`,
      };
    case "cancelled":
      return { short: "Cancelled", tone: undefined, sentence: "The move was cancelled." };
    default:
      return { short: "Stopped", tone: "bad", sentence: "The move stopped before it finished." };
  }
}

export interface MoveProgress {
  /** How full the bar is, 0–100; null when there is nothing honest to draw. */
  percent: number | null;
  /** "1,204 of 3,310 files · 12.4 GB of 40.1 GB"; null when nothing is counted yet. */
  counts: string | null;
}

/**
 * The bar and the figures under it.
 *
 * The server's figures are of the pass in hand, and they start again for
 * each short catch-up pass at the end ("finishing"). Drawn as they come, the
 * bar ran to the end and then jumped back to nothing for a pass of three
 * files. So the bar follows the long copy only and stays full after it.
 */
export function moveProgress(move: MoveStatus): MoveProgress {
  const counted = move.files_total > 0 || move.bytes_total > 0;
  const ratio =
    move.bytes_total > 0
      ? move.bytes_done / move.bytes_total
      : move.files_total > 0
        ? move.files_done / move.files_total
        : 0;
  const figures = counted
    ? `${move.files_done.toLocaleString("en-US")} of ${count(move.files_total, "file")} · ` +
      `${diskSize(move.bytes_done)} of ${diskSize(move.bytes_total)}`
    : null;

  switch (move.phase) {
    case "queued":
    case "checking":
      return { percent: null, counts: null };
    case "copying":
      return {
        percent: counted ? Math.max(0, Math.min(100, Math.round(ratio * 100))) : 0,
        counts: figures,
      };
    case "finishing":
      return { percent: 100, counts: figures && `Catching up: ${figures}` };
    case "switching":
      return { percent: 100, counts: null };
    case "done":
      return {
        percent: 100,
        counts: counted
          ? `${count(move.files_total, "file")} · ${diskSize(move.bytes_total)} in the new folder`
          : null,
      };
    default:
      // Failed or cancelled: how far the pass in hand had got, and no bar —
      // a bar stopped part way reads as one still moving.
      return { percent: null, counts: figures && `It had copied ${figures}.` };
  }
}

/**
 * What a finished move had to do: a move asked for a second time, or after
 * an interruption, finds most of the files already there, and "0 files
 * copied" on its own reads as a move that did nothing.
 */
export function copiedWords(move: MoveStatus): string | null {
  if (move.phase !== "done" || move.copied === null) return null;
  if (move.files_total === 0) return "There was nothing to copy yet.";
  if (move.copied === 0) return "Every file was already there, so nothing had to be copied.";
  if (move.copied < move.files_total) {
    return `${count(move.copied, "file")} had to be copied; the rest were already there.`;
  }
  return `${count(move.copied, "file")} ${move.copied === 1 ? "was" : "were"} copied.`;
}

/**
 * The sentence for a request the server refused. Its own message is the
 * sentence to show (given a full stop when it has none: more is often said
 * after it); a 409 from this part of the API always means a move is running,
 * and says which.
 */
export function refusal(status: number | null, message: string, fallback: string): string {
  // "Request failed (500)" is the client's own filler for an answer with no
  // sentence in it: what was being tried says more.
  const filler = FILLER.test(message.trim());
  const text = (!filler && message.trim()) || fallback;
  const said = /[.?!…]$/.test(text) ? text : `${text}.`;
  if (status === 409 && !/^a move is already running/i.test(said)) {
    return `A move is already running. ${said}`;
  }
  return said;
}

/**
 * Whether a request that failed got no answer from FrameFound itself: the
 * connection dropped (`status` null), or something in between gave up
 * waiting and answered for it (a 502 or 504 with no sentence in it).
 *
 * It matters for the one request on this page that the server carries on
 * with by itself, removing old copies: "could not be removed" was shown for
 * a laptop that went to sleep while the files were in fact being deleted.
 */
export function noAnswer(status: number | null, message: string): boolean {
  if (status === null) return true;
  return status >= 500 && FILLER.test(message.trim());
}

/**
 * The word beside the section's heading: the one-glance state of every line
 * under it, so it is worked out from those lines and nothing else.
 *
 * It used to follow whether a bulk folder was named, and read "all on this
 * machine" above proxies that live on a NAS in a folder of their own (the
 * Mac install as it was before there was a bulk folder). The model library
 * counts like any other line: it was once held back as "Reserved", which
 * read above a working Move button and hid that its drive was away.
 */
export function kindsSummary(kinds: KindStatus[]): { text: string; bad: boolean } {
  const away = kinds.filter((kind) => !kind.connected && kind.where !== "none");
  if (away.length) return { text: `${away.length} not connected`, bad: true };
  const allHere = kinds.every((kind) => kind.where === "local" || kind.where === "none");
  return { text: allHere ? "all on this machine" : "all connected", bad: false };
}

/** What Remove old copies answered, and which of its two steps was asked. */
export interface OldCopiesSaid {
  folder: string;
  files: number;
  bytes: number;
  kept: number;
  /** The second step: files with no copy were removed too. */
  everything: boolean;
}

/**
 * Whether a remembered answer from Remove old copies is still about this
 * line. An answer is about one folder; after another move the old copies
 * are in a different one, and an answer kept past that put its second step
 * ("remove those 3 too") in front of a folder it had never looked at.
 */
export function stillAbout(said: OldCopiesSaid | null, oldPath: string | null): boolean {
  if (!said) return false;
  if (said.folder === oldPath) return true;
  // The folder is no longer on record as holding old copies. What was
  // removed from it is still true to say; that files were left in it is not.
  return oldPath === null && said.kept === 0;
}

/**
 * How many files the second, explicit step may be offered for: those the
 * first step left because they have no copy — and only while the folder it
 * left them in is still the one the server has on record, because the
 * request names no folder and the server acts on the one on record.
 */
export function leftBehind(said: OldCopiesSaid | null, oldPath: string | null): number {
  if (!said || said.everything || said.folder !== oldPath) return 0;
  return Math.max(0, said.kept);
}

/**
 * Whether a removal that was started and never answered should still be
 * taken as running: it was for this folder, and not long ago.
 */
export function mayStillBeRemoving(
  started: { folder: string; at: number } | null,
  oldPath: string | null,
  now: number,
): boolean {
  if (!started || started.folder !== oldPath) return false;
  return now >= started.at && now - started.at < REMOVAL_WAIT_MS;
}

/**
 * Whether the list of kinds in an answer is from before the move in the same
 * answer finished. The server reads where everything lives first, measures
 * the folders (up to ten seconds for a slow drive), and reads the move last,
 * so one answer can say "finished" above a line still showing the old folder.
 */
export function layoutBehind(data: StorageKinds): boolean {
  const move = data.move;
  if (!move || move.phase !== "done") return false;
  const line = data.kinds.find((kind) => kind.kind === move.kind);
  return !!line && line.path !== move.target;
}
