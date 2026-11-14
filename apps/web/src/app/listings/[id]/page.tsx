"use client";

// One listing: the shoot in gallery order, each photo labelled with its room.
//
// The order IS the deliverable — MLS galleries display in upload order, so
// the numbered filenames in the exported zip are this page's arrangement,
// verbatim. Labels arrive as suggestions (dashed chip); picking one from the
// dropdown is what confirms it. Drag a photo to move it; "Arrange" resets to
// the canonical walk-through when a drag session has made a mess.
//
// Every picture on this page opens in the viewer (MediaPreview). None did:
// the gallery's tiles opened nothing — a photograph could be reached through
// "Edit", a video in a listing could not be opened or played at all — and
// both "Suggested removals" (which of two near-duplicates goes?) and "Add
// photos" asked for a decision on a 212 px thumbnail. The viewer opens over
// the page, so the add panel, its ticks and the suggestions stay as they are.
// In "Add photos" the picture looks and the tick in its corner chooses, the
// rule Search and Tags follow.
//
// The local model (LocalEditPanel): asked from the Auto-edit card, its
// suggestions wait in a list above the gallery until the ticked ones are
// applied — an auto-edit run of mode "local" that this page shows as it
// shows any run. Each suggestion's photograph opens in the same viewer.

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import ListingNamingTable from "@/components/ListingNamingTable";
import LocalEditPanel, { LocalEditAsk, useLocalEdit } from "@/components/LocalEditPanel";
import LookFrame from "@/components/LookFrame";
import { ScorePill } from "@/components/LookNote";
import MediaPreview from "@/components/MediaPreview";
import Shell from "@/components/Shell";
import { setChatContext } from "@/lib/chatContext";
import {
  appliedTargets,
  applyNotice,
  applyingLine,
  editingLabel,
  runTargets,
  skyText,
  straightenText,
  waitingSuggestions,
} from "@/lib/clipLooks";
import { usePageTitle } from "@/lib/hooks";
import { noOffersLine, runEnded, searchOffers, type RunEnd } from "./listingWords";
import SkyPicker, { prettySkyName } from "@/components/SkyPicker";
import {
  api,
  listingExportUrl,
  type AiEditSettings,
  type LocalApplyOptions,
  type FolderAsset,
  type MediaType,
  type SkyAsset,
  type ListingDetail,
  type ListingFolder,
  type ListingItem,
  type ListingNaming,
  type RemovalSuggestion,
  type RoomOption,
  type SearchResponse,
} from "@/lib/api";

const POLL_MS = 3000;

// Which grid the viewer was opened from: the listing's own gallery, the
// suggested removals, the photos offered in "Add photos", or the local
// model's suggestions. One asset can be in two of them at once, so the grid
// is part of what is open.
type ViewSource = "listing" | "removals" | "offered" | "proposals";

// What this tab asked a local Apply to edit, kept for the tab's life so a
// reload still marks the right tiles: {startedAt, assetIds} per listing.
const APPLYING_KEY = "framefound.listing.localApply.";

function rememberedApply(listingId: string): { startedAt: string; assetIds: string[] } | null {
  try {
    const raw = window.sessionStorage.getItem(APPLYING_KEY + listingId);
    const value: unknown = raw ? JSON.parse(raw) : null;
    if (
      value &&
      typeof value === "object" &&
      typeof (value as { startedAt?: unknown }).startedAt === "string" &&
      Array.isArray((value as { assetIds?: unknown }).assetIds)
    ) {
      return value as { startedAt: string; assetIds: string[] };
    }
  } catch {
    // Storage refused (a private window): the tiles fall back to marking
    // only what is done.
  }
  return null;
}

export default function ListingPage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const listingId = params.id;

  const [listing, setListing] = useState<ListingDetail | null>(null);
  const [rooms, setRooms] = useState<RoomOption[]>([]);
  usePageTitle(listing?.name ?? "Listing");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [addMode, setAddMode] = useState<"folders" | "search">("folders");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [folders, setFolders] = useState<ListingFolder[] | null>(null);
  const [folderAssets, setFolderAssets] = useState<FolderAsset[] | null>(null);
  const [openFolder, setOpenFolder] = useState<ListingFolder | null>(null);
  // What is ticked to add, and what kind of thing each is. The ticks are
  // kept when the folder or the search changes — a shoot is often two
  // folders, the camera's and the drone's — so the count is not only what
  // is on screen, and the panel says so (see `pickedElsewhere`).
  const [picked, setPicked] = useState<Map<string, MediaType>>(new Map());
  // The last tick, which a shift-click extends from.
  const [pickAnchor, setPickAnchor] = useState<string | null>(null);
  const [viewing, setViewing] = useState<{ from: ViewSource; id: string } | null>(null);
  // In "Suggested removals": the viewer is showing the photograph that would
  // be kept, not the one suggested for removal.
  const [comparing, setComparing] = useState(false);
  // What the last answer to a suggestion did, said in the viewer: the page
  // behind it cannot be read while it is open.
  const [removalNote, setRemovalNote] = useState<string | null>(null);
  const removalsHead = useRef<HTMLHeadingElement>(null);
  const addSearch = useRef<HTMLInputElement>(null);
  const [aiSettings, setAiSettings] = useState<AiEditSettings | null>(null);
  const [skies, setSkies] = useState<SkyAsset[]>([]);
  const [skyChoice, setSkyChoice] = useState<string>("");
  const [maxEdge, setMaxEdge] = useState(3840);
  const [quality, setQuality] = useState(85);
  const [suggestions, setSuggestions] = useState<RemovalSuggestion[] | null>(null);
  const [curating, setCurating] = useState(false);
  const [view, setView] = useState<"grid" | "names">("grid");
  const [naming, setNaming] = useState<ListingNaming>("seo");
  const [includeIndex, setIncludeIndex] = useState(true);
  const [suffixDraft, setSuffixDraft] = useState<string | null>(null);
  const [notesDraft, setNotesDraft] = useState<string | null>(null);
  const dragFrom = useRef<number | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The local model's suggestions: one reader for the rail's Ask, the list
  // above the gallery and the viewer.
  const localEdit = useLocalEdit(listingId);
  const [applying, setApplying] = useState<{ startedAt: string; assetIds: string[] } | null>(null);
  useEffect(() => setApplying(rememberedApply(listingId)), [listingId]);

  const load = useCallback(async (): Promise<ListingDetail | null> => {
    try {
      const fresh = await api.listing(listingId);
      setListing(fresh);
      return fresh;
    } catch {
      setError("Could not load this listing.");
      return null;
    }
  }, [listingId]);

  useEffect(() => {
    load();
    api.rooms().then(setRooms).catch(() => setRooms([]));
    api.aiEditSettings().then(setAiSettings).catch(() => setAiSettings(null));
    api.skies().then(setSkies).catch(() => setSkies([]));
  }, [load]);

  // Poll only while an export is in flight.
  const exporting =
    listing?.export_status === "queued" || listing?.export_status === "exporting";
  useEffect(() => {
    if (!exporting) return;
    timer.current = setTimeout(load, POLL_MS);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [exporting, listing, load]);

  // For the chat panel: how many photographs the listing holds and, while
  // photos are being added, the folder open and what is ticked to add. The
  // listing itself is in the address. Joined into one string so the effect
  // runs when the ticks change, not on every render.
  const listingName = listing?.name ?? "";
  const itemCount = listing?.items.length ?? null;
  const addingFrom = adding ? openFolder : null;
  const ticked = adding ? Array.from(picked.keys()).join(" ") : "";
  useEffect(
    () =>
      setChatContext({
        ...(listingName
          ? {
              title:
                itemCount === null
                  ? `Listing “${listingName}”`
                  : `Listing “${listingName}” — ${itemCount} item${itemCount === 1 ? "" : "s"}${
                      adding ? ", adding photos" : ""
                    }`,
            }
          : {}),
        folder: addingFrom?.path,
        library: addingFrom?.library_name,
        library_id: addingFrom?.library_id,
        selection: ticked ? ticked.split(" ") : [],
      }),
    [listingName, itemCount, adding, addingFrom, ticked],
  );

  /** Run a change and show the listing as it then is. False if it failed. */
  async function act(what: () => Promise<ListingDetail | void>): Promise<boolean> {
    setBusy(true);
    setError(null);
    try {
      const updated = await what();
      if (updated) setListing(updated);
      else await load();
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save that");
      return false;
    } finally {
      setBusy(false);
    }
  }

  const searchTicket = useRef(0);
  // Which opening of a folder an answer belongs to. Bumped on every way out
  // of a folder as well as every way in, so an answer that arrives late — a
  // big folder on a slow share — finds that it is no longer wanted.
  const folderTicket = useRef(0);

  /** Back to the list of folders. */
  function closeFolder() {
    folderTicket.current += 1;
    setOpenFolder(null);
    setFolderAssets(null);
  }

  async function search() {
    const q = query.trim();
    if (!q) return;
    const mine = ++searchTicket.current;
    if (addMode === "folders") {
      closeFolder();
      const found = await api.searchFolders(q);
      if (mine === searchTicket.current) setFolders(found);
    } else {
      const found = await api.search(q);
      if (mine === searchTicket.current) setResults(found);
    }
  }

  // Search as you type: results arrive without pressing anything, and the
  // ticket makes the latest keystroke win over slower earlier requests.
  useEffect(() => {
    if (!adding) return;
    const q = query.trim();
    if (q.length < 2) return;
    const timer = setTimeout(() => search(), 300);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, addMode, adding]);

  async function browseFolder(folder: ListingFolder) {
    const mine = ++folderTicket.current;
    setOpenFolder(folder);
    // Emptied first: the last folder's photographs used to stay on screen
    // under this folder's name until its own arrived.
    setFolderAssets(null);
    try {
      const found = await api.folderAssets(folder.library_id, folder.path);
      // Only the folder that is still open is filled. The answer for one
      // left earlier (open A, search again, open B, and then A's answer
      // lands) used to replace B's photographs under B's name — and "Select
      // all new" then ticked A's.
      if (mine === folderTicket.current) setFolderAssets(found);
    } catch (err) {
      // A folder the reader has already left failing is not news, and must
      // not shut the one they are in.
      if (mine !== folderTicket.current) return;
      // Back to the list of folders, with the reason, rather than an open
      // folder that never fills.
      closeFolder();
      setError(err instanceof Error ? err.message : "Could not open that folder");
    }
  }

  /** Say how a run ended: a notice when it is done, the error when it failed. */
  function sayEnded(ended: RunEnd) {
    if (ended.kind === "notice") setNotice(ended.text);
    else setError(ended.text);
  }

  async function aiEditAll(kind: "edit" | "describe" = "edit") {
    setBusy(true);
    setError(null);
    try {
      const { queued, mode, look } = await api.aiEditListing(
        listingId,
        kind === "edit" ? skyChoice || null : null,
        kind,
      );
      if (mode === "describe" && queued === 0) {
        setNotice("Every photo already has a name you confirmed — nothing to send.");
        return;
      }
      // The run's state now lives on the listing; loading it starts the
      // progress polling below. A run already over by then is said as over.
      const ended = runEnded(await load());
      if (ended) {
        sayEnded(ended);
        return;
      }
      const lookNote =
        look > 0
          ? ` Tone comes from your learned look (${look} shipped photos, on this machine)` +
            (mode === "ai" ? "; Claude straightens and names each photo." : ".")
          : "";
      setNotice(
        mode === "describe"
          ? `Naming ${queued} photos — a small preview of each goes to the Claude API and ` +
              `a caption and file name come back. Nothing is edited; names you typed are kept.`
          : look > 0
            ? `Auto-editing ${queued} photos.${lookNote}` +
              (skyChoice ? ` Skies swap in wherever the photo has sky.` : "")
          : mode === "ai"
            ? `AI editing ${queued} photos — a preview of each goes to the Claude API, ` +
              `slider values and a name come back, and the full-resolution render happens here.` +
              (skyChoice ? ` Skies swap in wherever the photo has sky.` : "")
            : `Auto-editing ${queued} photos with the listing preset, entirely on this ` +
              `machine.` +
              (skyChoice ? ` Skies swap in wherever the photo has sky.` : "") +
              ` Add an Anthropic key on Security for per-photo AI judgment and naming.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start auto-editing");
    } finally {
      setBusy(false);
    }
  }

  /**
   * Apply the local model's suggestions to the ticked photographs: an
   * auto-edit run of mode "local", shown from here on as any run is. Throws
   * the server's sentence when it is refused, for the list to show beside
   * its button.
   */
  async function applyLocal(
    itemIds: string[],
    assetIds: string[],
    options: Omit<LocalApplyOptions, "item_ids">,
  ) {
    setBusy(true);
    setError(null);
    try {
      const { queued, look } = await api.aiEditListing(listingId, skyChoice || null, "local", {
        item_ids: itemIds,
        ...options,
      });
      const fresh = await load();
      // Which tiles this run edits, for their pills — only the tab knows,
      // and only when the server queued every tick sent: a list read before
      // another tab applied or dismissed some would otherwise leave
      // "editing…" on tiles the run never touches, and "Editing 1/3…" under
      // a notice that says 1.
      const started = fresh?.ai_edit_started_at;
      const covers = appliedTargets(assetIds, queued);
      if (started && covers) {
        const mine = { startedAt: started, assetIds: covers };
        setApplying(mine);
        try {
          window.sessionStorage.setItem(APPLYING_KEY + listingId, JSON.stringify(mine));
        } catch {
          // Kept for this page only; a reload marks what is done.
        }
      } else {
        setApplying(null);
        try {
          window.sessionStorage.removeItem(APPLYING_KEY + listingId);
        } catch {
          // Nothing was kept to remove.
        }
      }
      // Finished before the page first looked: said as finished, and the
      // applied suggestions leave the list now, as when the page sees it end.
      const ended = runEnded(fresh);
      if (ended) {
        sayEnded(ended);
        localEdit.readNow();
        return;
      }
      setNotice(
        applyNotice(queued, look > 0 ? { from: "look", examples: look } : { from: "preset", examples: 0 }) +
          (skyChoice
            ? options.sky === "all"
              ? " Skies swap in wherever the photo has sky."
              : " Skies swap in where the model said the sky is worth replacing."
            : ""),
      );
    } finally {
      setBusy(false);
    }
  }

  async function saveListingField(patch: { file_suffix?: string; notes?: string }) {
    await act(() => api.updateListing(listingId, patch));
    setSuffixDraft(null);
    setNotesDraft(null);
  }

  // The run is the server's: queued/running on the listing means a run is
  // live — in this tab, another tab, or before a reload. Poll while it is,
  // and say what it did when it ends. (It used to live in this tab only: a
  // reload lost it, and a second press started a second run beside the
  // first — Smyrna Rd, Sep 2026.)
  const aiRunning =
    listing?.ai_edit_state === "queued" || listing?.ai_edit_state === "running";
  const runIsNaming = listing?.ai_edit_mode === "describe";
  const lastState = useRef<string | undefined>(undefined);
  const readLocalEdit = localEdit.readNow;
  useEffect(() => {
    if (!aiRunning) return;
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, [aiRunning, load]);
  useEffect(() => {
    const state = listing?.ai_edit_state;
    const was = lastState.current;
    lastState.current = state;
    if (!listing || !(was === "queued" || was === "running")) return;
    const ended = runEnded(listing);
    if (ended?.kind === "notice") setNotice(ended.text);
    if (ended?.kind === "error") setError(ended.text);
    // The applied suggestions leave the list once the run has written them.
    if (state !== was && listing.ai_edit_mode === "local") readLocalEdit();
  }, [listing, readLocalEdit]);

  function doneThisRun(item: ListingItem): boolean {
    // Landed since the run started — edited, or named for a naming run.
    // To the whole second: an edit's stamp can be the database's own, which
    // keeps no fraction of a second, while the start keeps microseconds — a
    // photograph edited in the run's first second read as not done.
    const at = listing?.ai_edit_started_at ? Date.parse(listing.ai_edit_started_at) : NaN;
    const started = Math.floor(at / 1000) * 1000;
    if (runIsNaming && item.naming_source === "confirmed") return true;
    const stamp = runIsNaming ? item.named_at : item.edited_at;
    return stamp !== null && !Number.isNaN(started) && Date.parse(stamp) >= started;
  }

  /**
   * Tick or untick one offered photograph — or, with shift held, every one
   * from the last tick to this one, as the Tags and People grids do.
   * Photographs already in the listing are never ticked.
   */
  function pick(list: FolderAsset[], index: number, shift: boolean) {
    const target = list[index];
    if (!target) return;
    const have = new Set(listing?.items.map((i) => i.asset_id));
    const anchored = pickAnchor ? list.findIndex((a) => a.asset_id === pickAnchor) : -1;
    const from = shift && anchored >= 0 ? anchored : index;
    setPicked((current) => {
      const next = new Map(current);
      const [lo, hi] = from <= index ? [from, index] : [index, from];
      const adding = !next.has(target.asset_id);
      for (let i = lo; i <= hi; i += 1) {
        const row = list[i];
        if (!row || have.has(row.asset_id)) continue;
        if (adding) next.set(row.asset_id, row.media_type);
        else next.delete(row.asset_id);
      }
      return next;
    });
    setPickAnchor(target.asset_id);
  }

  function clearPicked() {
    setPicked(new Map());
    setPickAnchor(null);
  }

  async function addPicked() {
    if (!picked.size) return;
    // A failed add keeps the ticks and the panel: it used to clear both and
    // close, as if the photographs had gone in.
    if (!(await act(() => api.addListingItems(listingId, [...picked.keys()])))) return;
    clearPicked();
    setResults(null);
    setQuery("");
    setAdding(false);
  }

  function reorder(from: number, target: number) {
    if (from === target || !listing) return;
    const order = [...listing.items].sort((a, b) => a.position - b.position);
    const [moved] = order.splice(from, 1);
    if (!moved) return;
    order.splice(target, 0, moved);
    act(() => api.reorderListing(listingId, order.map((i) => i.asset_id)));
  }

  function drop(target: number) {
    const from = dragFrom.current;
    dragFrom.current = null;
    if (from !== null) reorder(from, target);
  }

  // One step either way. Dragging is the fast way with a mouse; on a phone
  // there is no drag, and the gallery order is the deliverable.
  function nudge(index: number, delta: number) {
    const target = index + delta;
    if (target < 0 || target >= items.length) return;
    reorder(index, target);
  }

  async function runCuration() {
    setCurating(true);
    setError(null);
    try {
      setSuggestions(await api.curateListing(listingId));
    } catch {
      setError("Could not analyse the listing");
    } finally {
      setCurating(false);
    }
  }

  /**
   * Take a suggestion out of the list without acting on it ("Keep"), or
   * after it has been acted on. `thenView` is the suggestion the viewer
   * moves to if it was showing one of these — set together with the list,
   * so no render has the viewer pointing at a suggestion that has gone.
   */
  function dropSuggestions(assetIds: string[], thenView: string | null = null) {
    setSuggestions((current) =>
      current ? current.filter((s) => !assetIds.includes(s.asset_id)) : current,
    );
    setComparing(false);
    setViewing((current) =>
      current?.from === "removals" && assetIds.includes(current.id)
        ? thenView
          ? { from: "removals", id: thenView }
          : null
        : current,
    );
  }

  async function removeSuggested(assetIds: string[], thenView: string | null = null) {
    setBusy(true);
    setError(null);
    setRemovalNote(null);
    try {
      for (const id of assetIds) {
        await api.removeListingItem(listingId, id);
      }
      dropSuggestions(assetIds, thenView);
      setRemovalNote(
        assetIds.length === 1 ? "Removed from the listing." : `${assetIds.length} removed.`,
      );
      await load();
    } catch (err) {
      // Was unhandled: a removal the server refused looked like one that
      // was still on its way. Of several, some may have gone before the
      // one that failed, so the gallery is read again.
      setError(err instanceof Error ? err.message : "Could not remove that photograph");
      await load();
    } finally {
      setBusy(false);
    }
  }

  async function deleteListing() {
    if (!confirm("Delete this listing? The photographs stay in the catalogue.")) return;
    setBusy(true);
    await api.deleteListing(listingId).catch(() => undefined);
    router.push("/listings");
  }

  const items =[...(listing?.items ?? [])].sort((a, b) => a.position - b.position);
  // What the live run works on: every photograph, but for a local Apply only
  // the ones ticked — and when this tab did not press it, not known (null):
  // then only the photographs already done are marked.
  const targets = runTargets(
    listing?.ai_edit_mode ?? "",
    items.filter((i) => i.media_type === "image").map((i) => i.asset_id),
    listing?.ai_edit_started_at ?? null,
    applying,
  );
  const inRun = targets ? new Set(targets) : null;
  const runTotal = targets ? targets.length : null;
  const doneImages = items.filter(
    (i) => i.media_type === "image" && (!inRun || inRun.has(i.asset_id)) && doneThisRun(i),
  ).length;
  // The number each image will carry in the zip: images only, in order.
  const exportNumbers = new Map<string, number>();
  items
    .filter((i) => i.media_type === "image")
    .forEach((i, index) => exportNumbers.set(i.asset_id, index + 1));
  const inListing = new Map(items.map((i) => [i.asset_id, i]));

  // "Add photos": what a search found that is not in the listing yet, by
  // name or by what is in the photograph.
  const { offers: searchHits, found: searchFound } = searchOffers(
    results,
    new Set(inListing.keys()),
  );
  // The photographs the panel is offering right now: an open folder's, or
  // the search's. Empty on the list of folders.
  const offered: FolderAsset[] = !adding
    ? []
    : addMode === "search"
      ? searchHits
      : openFolder && folderAssets
        ? folderAssets
        : [];
  // The honest count. "Add 12 photos" used to be all the button said, with
  // seven of the twelve ticked in a folder no longer on screen.
  const pickedHere = offered.filter((a) => picked.has(a.asset_id)).length;
  const pickedElsewhere = picked.size - pickedHere;
  const allPhotos = [...picked.values()].every((kind) => kind === "image");
  const pickedNoun = `${allPhotos ? "photo" : "item"}${picked.size === 1 ? "" : "s"}`;
  const elsewhereNote =
    pickedElsewhere === 0
      ? null
      : pickedHere > 0
        ? `${picked.size} chosen: ${pickedHere} shown here, ${pickedElsewhere} in other folders ` +
          `or searches. Add puts all ${picked.size} into the listing.`
        : picked.size === 1
          ? "The one chosen is in a folder or search that is not on screen. Add still puts it " +
            "into the listing."
          : `The ${picked.size} chosen are in folders or searches that are not on screen. Add ` +
            "still puts them into the listing.";

  // The local model's suggestions still waiting, in the list's order.
  const proposals = localEdit.edit ? waitingSuggestions(localEdit.edit.suggestions) : [];

  // What the viewer is showing, looked up in its grid as that grid is now:
  // an item that has since left it is not found, and the viewer is not drawn.
  const viewList: FolderAsset[] =
    viewing?.from === "listing"
      ? items
      : viewing?.from === "proposals"
        ? proposals.map((p) => ({ asset_id: p.asset_id, filename: p.filename, media_type: "image" }))
      : viewing?.from === "removals"
        ? (suggestions ?? []).map((s) => ({
            asset_id: s.asset_id,
            filename: s.filename,
            media_type: s.media_type,
          }))
        : viewing?.from === "offered"
          ? offered
          : [];
  const viewAt = viewing ? viewList.findIndex((row) => row.asset_id === viewing.id) : -1;
  const viewed = viewAt >= 0 ? viewList[viewAt] : undefined;
  const viewedItem =
    viewed && viewing?.from === "listing" ? inListing.get(viewed.asset_id) : undefined;
  const viewedProposal =
    viewed && viewing?.from === "proposals"
      ? proposals.find((p) => p.asset_id === viewed.asset_id)
      : undefined;
  const viewedRemoval =
    viewed && viewing?.from === "removals"
      ? suggestions?.find((s) => s.asset_id === viewed.asset_id)
      : undefined;
  // The photograph a removal would leave in its place, when the viewer has
  // been flipped to it.
  const kept =
    comparing && viewedRemoval?.keep_instead
      ? inListing.get(viewedRemoval.keep_instead)
      : undefined;

  // An item that leaves its grid by some way other than the viewer's own
  // buttons takes the viewer with it — and must take the record of it too.
  // "Add photos" searches as you type: click a picture from the results for
  // "kit" while those for "kitchen" are on their way, and when they land
  // without it the viewer goes. Left pointing at that picture, it opened
  // again by itself, over the panel, the next time a search brought the
  // picture back.
  const viewerLost = viewing !== null && viewed === undefined;
  useEffect(() => {
    if (!viewerLost) return;
    setViewing(null);
    setComparing(false);
  }, [viewerLost]);

  function look(from: ViewSource, id: string) {
    // Opening afresh (not stepping): the last answer is old news.
    if (!viewing) setRemovalNote(null);
    setComparing(false);
    setViewing({ from, id });
  }

  /** Move the viewer along the grid it was opened from. */
  function step(by: number) {
    const target = viewList[viewAt + by];
    if (viewing && target) look(viewing.from, target.asset_id);
  }

  /** Answer the suggestion in the viewer and show the next one. */
  function answerRemoval(remove: boolean) {
    if (!viewedRemoval || busy) return;
    // Picked before anything changes: once this one leaves the list every
    // index after it shifts. The next one down; at the end, the one above.
    const next = (viewList[viewAt + 1] ?? viewList[viewAt - 1])?.asset_id ?? null;
    if (remove) {
      removeSuggested([viewedRemoval.asset_id], next);
    } else {
      dropSuggestions([viewedRemoval.asset_id], next);
      setRemovalNote("Kept.");
    }
  }

  /** Where focus goes when the viewer closes: the picture last on screen —
   *  or, when that has gone, the head of the suggestions (it was answered),
   *  the head of the local model's list (an Apply run, or Dismiss in another
   *  tab, took its row; the rail's Ask when the whole list went) or the
   *  search field of "Add photos" (a newer search no longer has it). */
  function focusAfterViewing(): HTMLElement | null {
    const tile =
      viewing && viewed
        ? document.querySelector<HTMLElement>(`[data-look="${viewing.from}:${viewed.asset_id}"]`)
        : null;
    if (tile) return tile;
    if (viewing?.from === "removals") return removalsHead.current;
    if (viewing?.from === "proposals") {
      return (
        document.querySelector<HTMLElement>(".localedit h2") ??
        document.querySelector<HTMLElement>(".localedit-ask .btn:not(:disabled)")
      );
    }
    return viewing?.from === "offered" ? addSearch.current : null;
  }

  /**
   * One photograph offered in "Add photos". The picture opens the viewer and
   * the tick in its corner chooses it. The whole tile used to be the toggle,
   * so there was no looking at a photograph without ticking or unticking it.
   */
  function offerTile(asset: FolderAsset, index: number, list: FolderAsset[]) {
    const already = inListing.has(asset.asset_id);
    const ticked = picked.has(asset.asset_id);
    return (
      <div
        key={asset.asset_id}
        className="tile tagpick"
        data-selected={ticked}
        style={already ? { opacity: 0.4 } : undefined}
      >
        {/* Picture and tick are siblings: a tick inside the picture's link
            would open the viewer as well. */}
        <div className="tagpick-stage">
          <LookFrame
            assetId={asset.asset_id}
            mediaType={asset.media_type}
            filename={asset.filename}
            focusKey={`offered:${asset.asset_id}`}
            onLook={() => look("offered", asset.asset_id)}
          />
          <label className="tagpick-check" title={already ? "Already in this listing" : "Choose"}>
            <input
              type="checkbox"
              checked={ticked}
              disabled={already}
              aria-label={`Choose ${asset.filename}`}
              // A checkbox's change rides on its click, so the shift key is
              // there to be read.
              onChange={(event) =>
                pick(list, index, (event.nativeEvent as MouseEvent).shiftKey === true)
              }
            />
          </label>
        </div>
        <div className="tile-meta">
          <div className="tile-name faint mono" style={{ fontSize: "0.7rem" }}>
            {asset.filename}
          </div>
          {already && (
            <div className="faint" style={{ fontSize: "0.7rem" }}>
              already in this listing
            </div>
          )}
        </div>
      </div>
    );
  }

  // After every hook above: a hook below an early return is skipped on the
  // renders that return early.
  if (error && !listing) {
    return (
      <Shell>
        <div className="empty">{error}</div>
        <Link className="btn" href="/listings">
          Back to listings
        </Link>
      </Shell>
    );
  }

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>{listing?.name ?? "Loading…"}</h2>
        {listing && (
          <span className="faint mono">
            {items.length} photos · click to look, drag to reorder · the zip follows this order
          </span>
        )}
      </div>

      {error && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
          {error}
        </div>
      )}
      {notice && !error && (
        <div className="card" role="status">
          {notice}
        </div>
      )}
      {listing?.export_status === "failed" && listing.export_error && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
          Export failed: {listing.export_error}
        </div>
      )}
      {listing?.export_status === "ready" && listing.export_error && (
        <div className="card" role="status">
          {listing.export_error}
        </div>
      )}
      {listing && !listing.classified && (
        <div className="card" role="status">
          Room suggestions are unavailable on this server — label photos with the dropdowns.
        </div>
      )}

      <div className="toolbar">
        <Link className="btn" href="/listings" style={{ padding: "6px 12px" }}>
          ← All listings
        </Link>
        <button className="btn" disabled={busy} onClick={() => setAdding((v) => !v)}>
          {adding ? "Close" : "Add photos"}
        </button>
        <span style={{ flex: 1 }} />
        <button
          className="btn"
          style={view === "grid" ? { borderColor: "var(--amber)" } : undefined}
          onClick={() => setView("grid")}
          title="Arrange: drag to reorder, label rooms"
        >
          Photos
        </button>
        <button
          className="btn"
          style={view === "names" ? { borderColor: "var(--amber)" } : undefined}
          onClick={() => setView("names")}
          title="File names and the photo index, as the delivery will read"
        >
          Names &amp; index
        </button>
      </div>

      <div className="listing-layout">
        <div className="listing-main">

      {adding && (
        <div className="card">
          <div className="toolbar" style={{ marginTop: 0 }}>
            <button
              className="btn"
              data-active={addMode === "folders"}
              style={addMode === "folders" ? { borderColor: "var(--amber)" } : undefined}
              onClick={() => setAddMode("folders")}
            >
              Folders
            </button>
            <button
              className="btn"
              data-active={addMode === "search"}
              style={addMode === "search" ? { borderColor: "var(--amber)" } : undefined}
              onClick={() => setAddMode("search")}
            >
              Search
            </button>
            <input
              ref={addSearch}
              className="input"
              style={{ flex: 1, minWidth: 220 }}
              placeholder={
                addMode === "folders"
                  ? "Folder name — e.g. the property address"
                  : "Search the catalogue — filename, folder, or what's in the photo"
              }
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") search();
              }}
              aria-label="Search for photos to add"
            />
            <button className="btn" onClick={search}>
              Search
            </button>
            {picked.size > 0 && (
              <>
                <button className="btn btn-primary" disabled={busy} onClick={addPicked}>
                  Add {picked.size} {pickedNoun}
                </button>
                <button
                  className="btn"
                  onClick={clearPicked}
                  title="Untick everything chosen, on screen or not"
                >
                  Clear
                </button>
              </>
            )}
          </div>
          {/* Ticks outlive the folder or search they were made in, so the
              button's number can be more than is on screen. Say so: what Add
              will do must be readable before it is pressed. */}
          {elsewhereNote && (
            <p className="faint" role="status" style={{ margin: "0 0 12px", fontSize: "0.8rem" }}>
              {elsewhereNote}
            </p>
          )}
          {offered.length > 0 && (
            <p className="faint" style={{ margin: "0 0 12px", fontSize: "0.8rem" }}>
              Click a picture to look at it · tick to choose
              <span className="pointer-hint">, shift-click a tick for a range</span>
            </p>
          )}
          {addMode === "folders" && !openFolder && folders && (
            <div style={{ display: "grid", gap: 6 }}>
              {folders.length === 0 && (
                <div className="empty">No folders match that name.</div>
              )}
              {folders.map((folder) => (
                <button
                  type="button"
                  key={`${folder.library_id}:${folder.path}`}
                  className="btn"
                  style={{ justifyContent: "space-between", display: "flex", gap: 8 }}
                  onClick={() => browseFolder(folder)}
                >
                  <span className="mono" style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                    {folder.path || "(library root)"}
                  </span>
                  <span className="faint mono">
                    {folder.library_name} · {folder.image_count} photos
                  </span>
                </button>
              ))}
            </div>
          )}
          {addMode === "folders" && openFolder && !folderAssets && (
            <div className="empty">Loading…</div>
          )}
          {addMode === "folders" && openFolder && folderAssets && (
            <>
              <div className="toolbar" style={{ marginTop: 0 }}>
                <button className="btn" onClick={closeFolder}>
                  ← Folders
                </button>
                <span className="faint mono" style={{ flex: 1 }}>
                  {openFolder.path || "(library root)"} · {folderAssets.length} photos
                </span>
                {/* Both act on this folder only. "Select all new" used to
                    replace the whole selection, and "Clear" to empty it —
                    each silently dropping ticks made in another folder. */}
                <button
                  className="btn"
                  onClick={() =>
                    setPicked((current) => {
                      const next = new Map(current);
                      for (const a of folderAssets) {
                        if (!inListing.has(a.asset_id)) next.set(a.asset_id, a.media_type);
                      }
                      return next;
                    })
                  }
                >
                  Select all new
                </button>
                <button
                  className="btn"
                  disabled={pickedHere === 0}
                  onClick={() =>
                    setPicked((current) => {
                      const next = new Map(current);
                      for (const a of folderAssets) next.delete(a.asset_id);
                      return next;
                    })
                  }
                >
                  Untick this folder
                </button>
              </div>
              <div className="grid">
                {folderAssets.map((a, index) => offerTile(a, index, folderAssets))}
              </div>
            </>
          )}
          {addMode === "search" && results &&
            (searchHits.length === 0 ? (
              <div className="empty">{noOffersLine(results.query, searchFound)}</div>
            ) : (
              <div className="grid">
                {searchHits.map((hit, index) => offerTile(hit, index, searchHits))}
              </div>
            ))}
        </div>
      )}

      {suggestions && (
        <div className="card">
          <div className="sectionhead" style={{ marginTop: 0 }}>
            <h2 ref={removalsHead} tabIndex={-1}>
              Suggested removals
            </h2>
            <span className="faint mono">
              {suggestions.length
                ? "every room keeps at least one photo"
                : "nothing worth removing — the shoot is tight"}
            </span>
          </div>
          {suggestions.length > 0 && (
            <div className="toolbar" style={{ marginTop: 0 }}>
              <button
                className="btn btn-primary"
                disabled={busy}
                onClick={() => removeSuggested(suggestions.map((s) => s.asset_id))}
              >
                Remove all {suggestions.length}
              </button>
              <button className="btn" onClick={() => setSuggestions(null)}>
                Dismiss
              </button>
              <span className="faint" style={{ fontSize: "0.8rem" }}>
                Click a picture to see it large and compare it with the one that would be kept
              </span>
            </div>
          )}
          <div className="grid">
            {suggestions.map((s) => (
              <div key={s.asset_id} className="tile">
                <LookFrame
                  assetId={s.asset_id}
                  mediaType={s.media_type}
                  filename={s.filename}
                  focusKey={`removals:${s.asset_id}`}
                  onLook={() => look("removals", s.asset_id)}
                >
                  {/* Where a local model has scored the photograph, the
                      score its reason below speaks of. Nothing without one. */}
                  <ScorePill score={s.score} />
                </LookFrame>
                <span className="faint" style={{ fontSize: "0.7rem" }}>{s.reason}</span>
                <div style={{ display: "flex", gap: 4 }}>
                  <button
                    className="btn"
                    style={{ fontSize: "0.7rem", padding: "2px 8px" }}
                    disabled={busy}
                    onClick={() => removeSuggested([s.asset_id])}
                  >
                    Remove
                  </button>
                  <button
                    className="btn"
                    style={{ fontSize: "0.7rem", padding: "2px 8px" }}
                    onClick={() => dropSuggestions([s.asset_id])}
                  >
                    Keep
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      <LocalEditPanel
        listingId={listingId}
        reader={localEdit}
        skyName={skyChoice ? prettySkyName(skyChoice) : ""}
        running={aiRunning}
        applying={
          aiRunning && listing?.ai_edit_mode === "local" ? applyingLine(doneImages, runTotal) : null
        }
        onApply={applyLocal}
        onLook={(assetId) => look("proposals", assetId)}
      />

      {view === "names" && (
        <ListingNamingTable
          items={items}
          busy={busy}
          onSave={async (assetId, caption, slug) => {
            await act(() => api.setListingNaming(listingId, assetId, caption, slug));
          }}
        />
      )}

      {/* Hidden rather than unmounted so a drag in progress is never torn
          down by a view switch; style, not the hidden attribute, because
          .grid sets display and would override it. */}
      <div className="grid" style={view !== "grid" ? { display: "none" } : undefined}>
        {items.map((item, index) => (
          <div
            key={item.asset_id}
            className="tile"
            draggable
            onDragStart={() => {
              dragFrom.current = index;
            }}
            onDragOver={(e) => e.preventDefault()}
            onDrop={() => drop(index)}
            title={`${item.filename} — drag to reorder`}
          >
            <LookFrame
              assetId={item.asset_id}
              mediaType={item.media_type}
              filename={item.filename}
              focusKey={`listing:${item.asset_id}`}
              hint="click to look, drag to reorder"
              onLook={() => look("listing", item.asset_id)}
            >
              <span
                className="pill mono"
                style={{ position: "absolute", top: 6, left: 6 }}
                title={
                  item.media_type === "image"
                    ? "Position in the exported zip"
                    : "Videos stay out of the photo zip"
                }
              >
                {item.media_type === "image"
                  ? String(exportNumbers.get(item.asset_id)).padStart(2, "0")
                  : "video"}
              </span>
              {aiRunning &&
                item.media_type === "image" &&
                !!inRun?.has(item.asset_id) &&
                !doneThisRun(item) && (
                <span
                  className="pill mono"
                  style={{ position: "absolute", bottom: 6, left: 6 }}
                >
                  <span className="spinner" />{" "}
                  {runIsNaming ? "naming…" : "editing…"}
                </span>
              )}
              {aiRunning &&
                item.media_type === "image" &&
                (!inRun || inRun.has(item.asset_id)) &&
                doneThisRun(item) && (
                <span
                  className="pill mono"
                  style={{ position: "absolute", bottom: 6, left: 6 }}
                >
                  {runIsNaming ? "✓ named" : "✓ edited"}
                </span>
              )}
            </LookFrame>
            {item.export_name && (
              // What it will ship as — the caption, when there is one, on hover.
              <span
                className="faint mono"
                // A block, or the ellipsis never applies and a long name runs
                // past a phone's narrow tile, cut off mid-word.
                style={{
                  display: "block",
                  maxWidth: "100%",
                  fontSize: "0.66rem",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
                title={item.caption ? `${item.export_name}\n${item.caption}` : item.export_name}
              >
                {item.export_name}
              </span>
            )}
            <select
              className="select"
              aria-label={`Room for ${item.filename}`}
              value={item.room}
              disabled={busy}
              // A suggested label reads tentative; picking is what confirms.
              // No wider than the tile: a long room name sized it past a
              // phone's tile, clipping its edge and its arrow.
              style={{
                maxWidth: "100%",
                ...(item.room && item.room_source === "suggested"
                  ? { borderStyle: "dashed" }
                  : {}),
              }}
              onChange={(e) =>
                act(() => api.setListingRoom(listingId, item.asset_id, e.target.value))
              }
            >
              <option value="">
                {item.room ? "— clear label —" : "unlabelled"}
              </option>
              {/* A stored room the list does not have still shows as itself,
                  not as "— clear label —" over a label that is there. */}
              {item.room && !rooms.some((room) => room.key === item.room) && (
                <option value={item.room}>{item.room_label || item.room}</option>
              )}
              {rooms.map((room) => (
                <option key={room.key} value={room.key}>
                  {room.label}
                  {item.room === room.key && item.room_source === "suggested"
                    ? " (suggested)"
                    : ""}
                </option>
              ))}
            </select>
            {/* Wraps: on a 167 px phone tile "Edited ✦ · Remove · ← →" is
                wider than the tile, and the → button ran past its edge into
                the tile's overflow:hidden, half cut off. The arrows (pushed
                right by margin-left:auto) drop to their own line instead. */}
            <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
              {item.media_type === "image" && (
                <Link
                  className="btn"
                  style={{ fontSize: "0.7rem", padding: "2px 8px" }}
                  href={`/edit/${item.asset_id}?listing=${listingId}`}
                  title={item.edited ? "Edited — click to adjust" : "Colour-correct this photo"}
                >
                  {item.edited ? "Edited ✦" : "Edit"}
                </Link>
              )}
              <button
                className="btn"
                style={{ fontSize: "0.7rem", padding: "2px 8px" }}
                disabled={busy}
                onClick={() => act(() => api.removeListingItem(listingId, item.asset_id))}
              >
                Remove
              </button>
              <span className="tile-move">
                <button
                  type="button"
                  className="btn"
                  style={{ fontSize: "0.7rem", padding: "2px 8px" }}
                  disabled={busy || index === 0}
                  aria-label="Move earlier"
                  title="Move earlier in the gallery"
                  onClick={() => nudge(index, -1)}
                >
                  ←
                </button>
                <button
                  type="button"
                  className="btn"
                  style={{ fontSize: "0.7rem", padding: "2px 8px" }}
                  disabled={busy || index === items.length - 1}
                  aria-label="Move later"
                  title="Move later in the gallery"
                  onClick={() => nudge(index, 1)}
                >
                  →
                </button>
              </span>
            </div>
          </div>
        ))}
      </div>

      {listing && !items.length && (
        <div className="empty">
          Nothing here yet — “Add photos” searches the catalogue.
        </div>
      )}

      {viewing && viewed && (
        // One viewer for the page's three grids, mounted beside them — never
        // inside a tile, whose `overflow: hidden` and hover transform would
        // clip it.
        <MediaPreview
          assetId={kept ? kept.asset_id : viewed.asset_id}
          mediaType={kept ? kept.media_type : viewed.media_type}
          filename={kept ? kept.filename : viewed.filename}
          heading={
            viewedRemoval ? (kept ? "The one that would be kept" : "Remove this one?") : undefined
          }
          detail={
            viewedProposal
              ? `As shot. Suggested: ${straightenText(viewedProposal.rotate, viewedProposal.keystone)} · ${skyText(viewedProposal.sky_worth_replacing)}`
              : viewedRemoval
              ? kept
                ? `instead of ${viewed.filename}`
                : viewedRemoval.reason
              : viewedItem
                ? [
                    viewedItem.room_label || "no room label yet",
                    viewedItem.media_type === "image"
                      ? `photo ${String(exportNumbers.get(viewedItem.asset_id)).padStart(2, "0")} in the zip`
                      : "stays out of the photo zip",
                  ].join(" · ")
                : inListing.has(viewed.asset_id)
                  ? "already in this listing"
                  : undefined
          }
          position={`${viewAt + 1} of ${viewList.length}`}
          status={
            viewing.from === "removals"
              ? removalNote
              : viewing.from === "offered" && picked.size > 0
                ? `${picked.size} chosen to add`
                : viewing.from === "proposals"
                  ? `${localEdit.ticks.ticked.length} ticked to apply`
                  : null
          }
          problem={viewing.from === "removals" ? error : null}
          busy={viewing.from === "removals" && busy}
          onClose={() => {
            setViewing(null);
            setComparing(false);
          }}
          onPrev={viewAt > 0 ? () => step(-1) : undefined}
          onNext={viewAt < viewList.length - 1 ? () => step(1) : undefined}
          // Not while the kept photograph is on screen: "Remove" under a
          // picture must never mean a different picture.
          onYes={viewedRemoval && !kept ? () => answerRemoval(true) : undefined}
          onNo={viewedRemoval && !kept ? () => answerRemoval(false) : undefined}
          yesLabel="Remove from the listing"
          noLabel="Keep it"
          onSelect={
            viewing.from === "offered" && !inListing.has(viewed.asset_id)
              ? () => pick(offered, viewAt, false)
              : viewedProposal
                ? () => {
                    // The tick, from the viewer: Space or its button.
                    const id = viewedProposal.item_id;
                    const ticked = localEdit.ticks.ticked;
                    localEdit.setTicked(
                      ticked.includes(id) ? ticked.filter((t) => t !== id) : [...ticked, id],
                    );
                  }
                : undefined
          }
          selected={
            viewedProposal
              ? localEdit.ticks.ticked.includes(viewedProposal.item_id)
              : picked.has(viewed.asset_id)
          }
          returnFocus={focusAfterViewing}
        >
          {/* The score belongs to the photograph suggested for removal, so
              it is not shown while the one that would be kept is on screen. */}
          {viewedRemoval && !kept && <ScorePill score={viewedRemoval.score} />}
          {viewedRemoval?.keep_instead && inListing.has(viewedRemoval.keep_instead) && (
            // A near-duplicate can only be judged against its twin. One
            // press shows the photograph that stays, the next comes back.
            <button
              type="button"
              className="btn"
              aria-pressed={comparing}
              onClick={() => setComparing((showing) => !showing)}
            >
              {comparing ? "Back to the one to remove" : "Show the one it would keep"}
            </button>
          )}
        </MediaPreview>
      )}

        </div>

        {/* Listing-wide controls live here, out of the photo flow: the grid
            is the work surface, the rail is what happens to the whole shoot. */}
        <aside className="listing-rail">
          <div className="card">
            <div className="mono" style={{ marginBottom: 6 }}>Auto-edit</div>
            <SkyPicker
              skies={skies}
              value={skyChoice}
              disabled={busy || aiRunning}
              onChange={setSkyChoice}
            />
            <button
              className="btn btn-primary"
              style={{ width: "100%", marginTop: 6 }}
              disabled={busy || aiRunning || !items.some((i) => i.media_type === "image")}
              onClick={() => aiEditAll("edit")}
              title={
                aiSettings?.configured && aiSettings.enabled
                  ? "Per-photo AI judgment via the Claude API; renders happen here at full resolution"
                  : "Tuned listing preset, entirely local. Add an Anthropic key on Security for per-photo AI judgment."
              }
            >
              {aiRunning && !runIsNaming
                ? editingLabel(doneImages, runTotal)
                : "Auto-edit photos"}
            </button>
            <span className="faint" style={{ fontSize: "0.72rem" }}>
              {aiSettings?.configured && aiSettings.enabled
                ? "Claude picks per-photo settings and names each photo; renders stay local."
                : "Local preset. Add a key on Security for per-photo AI."}
            </span>
            <LocalEditAsk listingId={listingId} reader={localEdit} />
          </div>

          <div className="card">
            <div className="mono" style={{ marginBottom: 6 }}>Names &amp; index</div>
            <label className="faint" style={{ fontSize: "0.72rem" }} htmlFor="file-suffix">
              Every file name ends with
            </label>
            <input
              id="file-suffix"
              className="input mono"
              style={{ width: "100%", padding: "6px 9px", fontSize: "0.8rem" }}
              placeholder={listing?.suggested_suffix || "address-auction"}
              value={suffixDraft ?? listing?.file_suffix ?? ""}
              disabled={busy}
              onChange={(e) => setSuffixDraft(e.target.value)}
              onBlur={() => {
                if (suffixDraft !== null && suffixDraft !== listing?.file_suffix) {
                  saveListingField({ file_suffix: suffixDraft });
                } else setSuffixDraft(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") (e.target as HTMLInputElement).blur();
              }}
            />
            <label
              className="faint"
              style={{ fontSize: "0.72rem", display: "block", marginTop: 6 }}
              htmlFor="index-notes"
            >
              Notes that head the photo index
            </label>
            <textarea
              id="index-notes"
              className="input"
              rows={3}
              style={{ width: "100%", fontSize: "0.8rem", resize: "vertical" }}
              placeholder="Auction date, terms, open house…"
              value={notesDraft ?? listing?.notes ?? ""}
              disabled={busy}
              onChange={(e) => setNotesDraft(e.target.value)}
              onBlur={() => {
                if (notesDraft !== null && notesDraft !== listing?.notes) {
                  saveListingField({ notes: notesDraft });
                } else setNotesDraft(null);
              }}
            />
            <button
              className="btn"
              style={{ width: "100%", marginTop: 6 }}
              disabled={
                busy ||
                aiRunning ||
                !(aiSettings?.configured && aiSettings.enabled) ||
                !items.some((i) => i.media_type === "image")
              }
              onClick={() => aiEditAll("describe")}
              title={
                aiSettings?.configured && aiSettings.enabled
                  ? "Caption and file name for each photo, without editing it — names you typed are kept"
                  : "Needs an Anthropic key on Security"
              }
            >
              {aiRunning && runIsNaming
                ? `Naming ${doneImages}/${runTotal ?? 0}…`
                : "Name photos with AI"}
            </button>
            <span className="faint" style={{ fontSize: "0.72rem" }}>
              For shoots still edited elsewhere. Auto-edit names photos as it goes.
            </span>
          </div>

          <div className="card">
            <div className="mono" style={{ marginBottom: 6 }}>Rooms & order</div>
            <button
              className="btn"
              style={{ width: "100%" }}
              disabled={busy || !items.length}
              onClick={() => act(() => api.arrangeListing(listingId))}
              title="Reset to the canonical walk-through: exterior, living spaces, bedrooms, outside, plans"
            >
              Arrange by room
            </button>
            <button
              className="btn"
              style={{ width: "100%", marginTop: 6 }}
              disabled={busy || !items.length}
              onClick={() => act(() => api.reclassifyListing(listingId))}
              title="Re-suggest room labels for everything you have not confirmed"
            >
              Re-suggest rooms
            </button>
          </div>

          <div className="card">
            <div className="mono" style={{ marginBottom: 6 }}>Curate</div>
            <button
              className="btn"
              style={{ width: "100%" }}
              disabled={busy || curating || items.length < 3}
              onClick={runCuration}
              title="Find near-duplicates and soft frames the listing can afford to lose — every room keeps at least one photo"
            >
              {curating ? "Analysing…" : "Suggest removals"}
            </button>
          </div>

          <div className="card">
            <div className="mono" style={{ marginBottom: 6 }}>Export</div>
            <select
              className="select"
              style={{ width: "100%" }}
              aria-label="Export size"
              value={maxEdge}
              onChange={(e) => setMaxEdge(Number(e.target.value))}
            >
              <option value={2048}>2048 px longest edge</option>
              <option value={3840}>3840 px longest edge</option>
              <option value={4096}>4096 px longest edge</option>
              <option value={8192}>Full size (print, up to 8192 px)</option>
            </select>
            <select
              className="select"
              style={{ width: "100%", marginTop: 6 }}
              aria-label="Export quality"
              value={quality}
              onChange={(e) => setQuality(Number(e.target.value))}
            >
              <option value={80}>Quality 80</option>
              <option value={85}>Quality 85</option>
              <option value={90}>Quality 90</option>
            </select>
            <select
              className="select"
              style={{ width: "100%", marginTop: 6 }}
              aria-label="File names"
              value={naming}
              onChange={(e) => setNaming(e.target.value as ListingNaming)}
            >
              <option value="seo">SEO names (01-kitchen-…-address)</option>
              <option value="simple">Simple names (01_kitchen)</option>
            </select>
            <label
              className="faint"
              style={{ fontSize: "0.75rem", display: "flex", gap: 6, marginTop: 6 }}
            >
              <input
                type="checkbox"
                checked={includeIndex}
                onChange={(e) => setIncludeIndex(e.target.checked)}
              />
              Photo index + contact sheets
            </label>
            <button
              className="btn btn-primary"
              style={{ width: "100%", marginTop: 6 }}
              disabled={busy || exporting || !items.some((i) => i.media_type === "image")}
              onClick={() =>
                act(() => api.exportListing(listingId, maxEdge, quality, naming, includeIndex))
              }
            >
              {exporting ? "Exporting…" : "Export zip"}
            </button>
            {listing?.export_status === "ready" && !listing.export_stale && (
              <a
                className="btn"
                style={{ width: "100%", marginTop: 6, display: "block", textAlign: "center" }}
                href={listingExportUrl(listingId)}
              >
                Download
              </a>
            )}
            {listing?.export_status === "ready" && listing.export_stale && (
              // The listing changed after the zip was made; the server will
              // not serve it, so say why rather than offer a dead link.
              <p className="faint" style={{ fontSize: "0.78rem", margin: "6px 0 0" }}>
                <span className="pill" data-tone="warn">
                  out of date
                </span>{" "}
                The listing changed since the last export — export again to download.
              </p>
            )}
          </div>

          <div className="card">
            <div className="mono" style={{ marginBottom: 6 }}>Edit in Lightroom</div>
            {/* The plugin is the reliable route: it adds the originals where
                they are (the RAW when the camera wrote one), so nothing is
                re-encoded and nothing is copied off the NAS. */}
            <p className="faint" style={{ fontSize: "0.78rem", margin: 0 }}>
              In Lightroom Classic: <strong>File → Plug-in Extras → Import FrameFound
              listings…</strong> and tick “{listing?.name ?? "this listing"}” — or as many
              listings as you like, to edit them all in one grid. With “Copy them into a folder”
              ticked, each photograph — the RAW when the camera wrote one — is copied under the
              name this page shows and added from there; unticked, the originals are added where
              they are. The NAS is never written to.
            </p>
          </div>

          <button
            className="btn"
            style={{ borderColor: "var(--ember)", color: "var(--ember)" }}
            disabled={busy}
            onClick={deleteListing}
          >
            Delete listing
          </button>
        </aside>
      </div>
    </Shell>
  );
}
