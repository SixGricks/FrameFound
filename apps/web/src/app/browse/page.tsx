"use client";

// Contact-sheet browse. Defaults to "ready to view" because the first real
// session surfaced thousands of still-processing assets as blank tiles —
// the state is now filterable and always labelled rather than hidden.
//
// Every filter, the sort and the page live in the URL. Pressing Back from an
// asset used to land on page 1 of all libraries, recently added, whatever had
// been chosen; and the Libraries page's "Browse" link (`?library=…`) was
// ignored. The URL is the state, so a view is linkable, survives a reload and
// comes back as it was.
//
// Two views share that URL. "Everything" is the flat sheet Browse has always
// been, and stays what a plain /browse opens. "Folders" (`view=folders`)
// walks a library the way it is laid out on disk: the libraries, then the
// folders inside a folder with what each holds, then the files there. A
// filter replaces the address, as it always has; opening a folder or a
// library is a link, so Back goes up one folder.
//
// A picture opens the viewer over the page and the name under it opens the
// asset's own page (as on Search, Tags and Listings). The whole tile used to
// be one link to the page, so looking at twenty photographs was twenty trips
// there and back. Nothing was taken away to get that: the name, a modified
// or middle click on the picture, and the viewer's "Open full page" all
// still go to the page.
//
// In the Folders view a folder (or a whole library) can be handed to a local
// model to look through: "Review with AI", beside the breadcrumb, opens the
// dialog that says what that would take. Pressing the button starts nothing.
// Where models can run, a folder with clips beneath it can also be handed to
// the local Editor: "Draft a cut from this folder" opens the Cuts page's form
// with the folder chosen, and nothing is drafted until Draft is pressed there.

import { Suspense, useCallback, useEffect, useRef, useState, type MouseEvent } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import LookFrame from "@/components/LookFrame";
import MediaPreview from "@/components/MediaPreview";
import ReviewWithAi, { type ReviewTarget } from "@/components/ReviewWithAi";
import Shell from "@/components/Shell";
import Thumb from "@/components/Thumb";
import { api, type AssetSummary, type FolderLevel, type Library } from "@/lib/api";
import { setChatContext } from "@/lib/chatContext";
import { browseDraftHref, draftAsksPicker, pickerOffers } from "@/lib/cuts";
import { duration, resolution, shortDate } from "@/lib/format";
import { usePageTitle } from "@/lib/hooks";
import {
  browseHref,
  contentsText,
  emptyReason,
  filesHeading,
  folderOf,
  folderParts,
  folderQuery,
  librariesQuery,
  lookedText,
  parentOf,
  placeOf,
  scoreLabel,
  scoreTone,
  shownNote,
  viewQuery,
  withChanges,
} from "@/lib/looks";

import { filtersOf, turnTo } from "./paging";

const PAGE_SIZE = 60;

const MEDIA_WORDS: Record<string, string> = {
  "": "all media",
  video: "video only",
  image: "images only",
  audio: "audio only",
};

// The pages and the folder levels last shown, so Back renders one at once
// (and the browser has a height to restore the scroll position to) while it
// is re-fetched. A handful, for going back up through a few folders; not a
// history.
const KEPT = 12;
const shownPages = new Map<string, { items: AssetSummary[]; total: number }>();
const shownLevels = new Map<string, FolderLevel>();

function keep<T>(kept: Map<string, T>, key: string, value: T) {
  kept.delete(key);
  kept.set(key, value);
  for (const oldest of kept.keys()) {
    if (kept.size <= KEPT) break;
    kept.delete(oldest);
  }
}

/** A local model's 0-100 score of an asset, or null when nothing has looked
 *  at it (and on a server from before there were scores). */
function scoreOf(asset: AssetSummary): number | null {
  return typeof asset.look_score === "number" ? asset.look_score : null;
}

function BrowsePage() {
  const params = useSearchParams();
  const router = useRouter();
  usePageTitle("Browse");

  const tag = params.get("tag") ?? "";
  const includeSuggested = params.get("suggested") === "1";
  const libraryId = params.get("library") ?? "";
  const mediaType = params.get("media") ?? "";
  const previewable = params.get("all") !== "1";
  const sort = params.get("sort") ?? "recent";
  const page = Math.max(1, Number(params.get("page") ?? "1") || 1);
  // Only the Folders view is about a folder. The flat view, which a plain
  // /browse still opens, reads neither `folder` nor `deep`.
  const inFolders = params.get("view") === "folders";
  const folder = inFolders ? (params.get("folder") ?? "") : "";
  const deep = inFolders && params.get("deep") === "1";
  const key = params.toString();
  // The Folders view with no library chosen lists the libraries: no files.
  const choosing = inFolders && !libraryId;
  const levelKey = inFolders && libraryId ? `${libraryId}\n${folder}` : "";

  const [libraries, setLibraries] = useState<Library[]>([]);
  // Whether the libraries have been answered for, and why they could not be
  // listed. Only the Folders view says so: there the list is the page.
  const [librariesRead, setLibrariesRead] = useState(false);
  const [librariesProblem, setLibrariesProblem] = useState<string | null>(null);
  const [items, setItems] = useState<AssetSummary[]>(() => shownPages.get(key)?.items ?? []);
  const [total, setTotal] = useState(() => shownPages.get(key)?.total ?? 0);
  // The filters `total` was counted for, or null before any answer. Until
  // the count is these filters' own, the page arrows go by the URL alone.
  const [countedFor, setCountedFor] = useState<string | null>(() =>
    shownPages.has(key) ? filtersOf(key) : null,
  );
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // The folder level on screen and the folder it is of; likewise why one
  // could not be read. Each is only used for the folder the URL names.
  const [level, setLevel] = useState<{ key: string; found: FolderLevel } | null>(() => {
    const kept = shownLevels.get(levelKey);
    return kept ? { key: levelKey, found: kept } : null;
  });
  const [levelProblem, setLevelProblem] = useState<{ key: string; message: string } | null>(null);
  // Whether models can run on this install: "looked at" and "Best first" are
  // only offered where anything could have been looked at.
  const [aiHere, setAiHere] = useState(false);
  // Whether the server answered about looking at all. "Review with AI" is
  // offered wherever it did — where models cannot run, the dialog says why —
  // and not on a server too old to have been asked.
  const [canAskAi, setCanAskAi] = useState(false);
  // The asset open in the viewer.
  const [viewing, setViewing] = useState<string | null>(null);
  // The folder the "Review with AI" dialog is open on, and the button that
  // opened it (focus goes back there).
  const [reviewing, setReviewing] = useState<ReviewTarget | null>(null);
  const reviewOpener = useRef<HTMLElement | null>(null);
  // Bumped when that dialog closes after a review ran: the files and the
  // folder's "looked at" counts are read again.
  const [lookedRound, setLookedRound] = useState(0);

  useEffect(() => {
    api
      .libraries()
      .then(setLibraries)
      .catch((err) =>
        setLibrariesProblem(err instanceof Error ? err.message : "The server did not answer."),
      )
      .finally(() => setLibrariesRead(true));
    // An older server has no such route; then nothing about looking is shown.
    api
      .lookStatus()
      .then((status) => {
        setAiHere(status.available);
        setCanAskAi(true);
      })
      .catch(() => undefined);
  }, []);

  // The query the URL is about to have. `router.replace` lands a moment
  // later, and a second change made before it did was built on the old
  // query and undid the first (a library chosen, then the media type at
  // once: the library was lost). Back and links still set it, through
  // `params`. Every control on this page changes the address through
  // `update`, `replaceWith` or `linkTo`, all three built on this.
  const query = useRef(key);
  useEffect(() => {
    query.current = key;
  }, [key]);

  /** Show another Browse query in place: a filter, the sort, the page, the view. */
  const replaceWith = useCallback(
    (next: string) => {
      query.current = next;
      router.replace(browseHref(next), { scroll: false });
    },
    [router],
  );

  /** Change filters in the URL. A new filter starts at page 1. */
  const update = useCallback(
    (changes: Record<string, string | null>, keepPage = false) =>
      replaceWith(withChanges(query.current, changes, keepPage)),
    [replaceWith],
  );

  /**
   * A link to another place in Browse: a folder, a library, the list of
   * libraries. `make` builds that place's query from a query. The link's own
   * address is built from the URL as it stands, which is what a modified or
   * middle click opens in a new tab. A plain click is built on the query the
   * URL is about to have, like every other control here — so "Include
   * sub-folders" ticked a moment before a folder is opened is not lost — and
   * is pushed, so Back comes up one folder.
   */
  const linkTo = (make: (from: string) => string) => ({
    href: browseHref(make(key)),
    // Not prefetched: a library can list five hundred folders.
    prefetch: false,
    onClick: (event: MouseEvent<HTMLAnchorElement>) => {
      if (event.button !== 0) return;
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      const next = make(query.current);
      query.current = next;
      router.push(browseHref(next));
    },
  });

  // Whose files are on screen (the folder; nothing in the flat view).
  const placeShown = useRef(placeOf(key));
  useEffect(() => {
    const place = placeOf(key);
    const moved = placeShown.current !== place;
    placeShown.current = place;
    if (choosing) {
      setItems([]);
      setTotal(0);
      setCountedFor(null);
      setError(null);
      setBusy(false);
      return;
    }
    let live = true;
    setBusy(true);
    const kept = shownPages.get(key);
    if (kept) {
      setItems(kept.items);
      setTotal(kept.total);
      setCountedFor(filtersOf(key));
    } else if (moved) {
      // While a filter changes, the files on screen stay until the new ones
      // arrive. Another folder's files must not: they would sit under this
      // folder's name. The same for what went wrong reading the other one.
      setItems([]);
      setError(null);
    }
    api
      .assets({
        library_id: libraryId || undefined,
        media_type: mediaType || undefined,
        previewable: previewable || undefined,
        tag: tag || undefined,
        include_suggested_tags: tag && includeSuggested ? true : undefined,
        sort,
        page,
        page_size: PAGE_SIZE,
        // "" is the library's own top, so the folder is sent whenever this
        // is the Folders view and left out whenever it is not.
        folder: inFolders ? folder : undefined,
        deep: deep || undefined,
      })
      .then((data) => {
        if (!live) return;
        keep(shownPages, key, { items: data.items, total: data.total });
        setItems(data.items);
        setTotal(data.total);
        setCountedFor(filtersOf(key));
        setError(null);
      })
      .catch((err) => {
        // A failed request is not "no assets match"; it is its own message.
        if (live) setError(err instanceof Error ? err.message : "Could not load assets");
      })
      .finally(() => {
        if (live) setBusy(false);
      });
    // An answer to an older query never lands: two changes in quick
    // succession each start a request, and the first may come back last.
    return () => {
      live = false;
    };
  }, [
    key,
    choosing,
    libraryId,
    mediaType,
    previewable,
    tag,
    includeSuggested,
    sort,
    page,
    inFolders,
    folder,
    deep,
    lookedRound,
  ]);

  // The folders inside this folder, and what it holds.
  useEffect(() => {
    if (!levelKey) return;
    let live = true;
    const kept = shownLevels.get(levelKey);
    if (kept) setLevel({ key: levelKey, found: kept });
    api
      .folderLevel(libraryId, folder)
      .then((found) => {
        if (!live) return;
        keep(shownLevels, levelKey, found);
        setLevel({ key: levelKey, found });
        setLevelProblem(null);
      })
      .catch((err) => {
        if (!live) return;
        setLevelProblem({
          key: levelKey,
          message: err instanceof Error ? err.message : "The server did not answer.",
        });
      });
    return () => {
      live = false;
    };
  }, [levelKey, libraryId, folder, lookedRound]);

  // Another address is another list: the viewer does not stay open over it
  // (its "Open its folder" link leads away from the page it was opened on),
  // and nor does a dialog about the folder that was on screen.
  useEffect(() => {
    setViewing(null);
    setReviewing(null);
  }, [key]);

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const counted = countedFor === filtersOf(key);
  const here = level?.key === levelKey ? level.found : null;
  const hereProblem = levelProblem?.key === levelKey ? levelProblem.message : null;
  const crumbs = folderParts(folder);
  // The folder as the catalogue writes it (no slash at either end).
  const folderPath = crumbs[crumbs.length - 1]?.path ?? "";
  const nameOfLibrary = (id: string) => libraries.find((library) => library.id === id)?.name ?? "";

  // Whether the draft form's picker lists this folder, asked only of a folder
  // with clips in folders inside it (`browseDraftHref` says why), and kept
  // with the folder it was asked of.
  const [pickerHas, setPickerHas] = useState<{ key: string; offered: boolean } | null>(null);
  const asksPicker = aiHere && here !== null && folderPath !== "" && draftAsksPicker(here);
  useEffect(() => {
    if (!asksPicker) return;
    let live = true;
    const asked = levelKey;
    api
      .cutFolders(folderPath, 50)
      .then((rows) => {
        if (live) setPickerHas({ key: asked, offered: pickerOffers(rows, libraryId, folderPath) });
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [asksPicker, levelKey, libraryId, folderPath]);
  const pickerOffered = pickerHas?.key === levelKey ? pickerHas.offered : null;

  // For the chat panel: the library by name and the filters in words, so
  // "what is on this page?" can be answered. The tag is in the address; the
  // folder is published, so "review this folder" means this one.
  const libraryName = here?.library_name ?? nameOfLibrary(libraryId);
  const shown = [
    MEDIA_WORDS[mediaType] ?? "all media",
    previewable ? "ready to view only" : "",
    deep ? "with the folders inside it" : "",
    tag && includeSuggested ? "including unreviewed suggestions" : "",
    // Counts only once they are this view's, not the last one's.
    busy ? `page ${page}` : `page ${page} of ${pages}, ${total.toLocaleString("en-US")} in all`,
  ]
    .filter(Boolean)
    .join(", ");
  const where = !inFolders
    ? ""
    : choosing
      ? "the list of libraries"
      : folderPath
        ? `the folder ${folderPath}`
        : "the top of the library";
  useEffect(
    () =>
      setChatContext(
        choosing
          ? { title: `Browse — ${where}` }
          : inFolders
            ? {
                title: `Browse — ${where}, ${shown}`,
                library: libraryName,
                library_id: libraryId,
                folder: folderPath,
              }
            : { title: `Browse — ${shown}`, library: libraryName },
      ),
    [shown, libraryName, where, choosing, inFolders, libraryId, folderPath],
  );
  /** One page forward or back — from the page the URL is about to show, so
   *  two quick presses of → move two pages. */
  const turn = (by: number) => {
    const n = turnTo(query.current, by, countedFor === filtersOf(query.current) ? pages : null);
    update({ page: n <= 1 ? null : String(n) }, true);
  };

  // The item in the viewer, looked up in the page as it is now.
  const viewAt = viewing ? items.findIndex((asset) => asset.id === viewing) : -1;
  const viewed = viewAt >= 0 ? items[viewAt] : undefined;
  /** Where focus goes when the viewer closes: the picture last looked at. */
  const focusAfterViewing = (): HTMLElement | null =>
    viewed ? document.querySelector<HTMLElement>(`[data-look="${viewed.id}"]`) : null;

  /** Whether a file is somewhere other than the folder on screen: always in
   *  the flat view, and in the Folders view when it is in a folder inside. */
  const elsewhere = (asset: AssetSummary) =>
    !inFolders || folderOf(asset.relative_path) !== folderPath;
  /** The link to a file's own folder, in the Folders view. */
  const toFolderOf = (asset: AssetSummary) =>
    linkTo((from) => folderQuery(from, asset.library_id, folderOf(asset.relative_path)));
  /** That folder in words: its path, or its library for a file at the top. */
  const folderWords = (asset: AssetSummary) =>
    folderOf(asset.relative_path) || nameOfLibrary(asset.library_id) || "the top of its library";

  // What the Folders view shows under the breadcrumb, before any files.
  const empty = here && here.inside.items <= 0 ? emptyReason(here, deep) : null;
  // The files: always in the flat view; in the Folders view once the folder
  // is known to hold something.
  const showFiles = !inFolders || (!choosing && !hereProblem && here !== null && empty === null);
  // "Include sub-folders" where there are any — and wherever it is ticked,
  // so it can always be unticked.
  const offerDeep = inFolders && showFiles && (deep || (here?.folders.length ?? 0) > 0);
  // How many files this folder holds, before any filter: its own, or with
  // "Include sub-folders" everything beneath it.
  const filesHere = here ? (deep ? here.inside.items : here.here.items) : 0;
  // "Review with AI": for a folder (or library) that was found and holds
  // something. A path nothing is catalogued under has nothing to review.
  const offerReview = canAskAi && here !== null && here.inside.items > 0;
  const offerListing = here !== null && folderPath !== "" && here.here.photos > 0;
  // "Draft a cut from this folder": where models can run, for a folder (not
  // a whole library: a draft is always of a folder) with clips beneath it,
  // which is what a folder draft takes; one with clips in folders inside it
  // once the form's picker offers it (`browseDraftHref`).
  const draftHref =
    aiHere && here !== null ? browseDraftHref(libraryId, folderPath, here, pickerOffered) : null;

  return (
    <>
      <div className="sectionhead">
        <h2>Browse</h2>
        {!inFolders && <span className="faint mono">{total.toLocaleString()} assets</span>}
      </div>

      <div className="toolbar browse-views" role="group" aria-label="How to browse">
        <button
          type="button"
          className="btn"
          aria-pressed={!inFolders}
          onClick={() => replaceWith(viewQuery(query.current, "flat"))}
        >
          Everything
        </button>
        <button
          type="button"
          className="btn"
          aria-pressed={inFolders}
          onClick={() => replaceWith(viewQuery(query.current, "folders"))}
        >
          Folders
        </button>
      </div>

      {tag && (
        <div className="toolbar">
          <span className="pill" data-tone="ok">
            tag: {tag}
            <button
              className="chipx"
              aria-label="Clear the tag filter"
              onClick={() => update({ tag: null, suggested: null })}
            >
              ×
            </button>
          </span>
          <label className="faint" style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input
              type="checkbox"
              checked={includeSuggested}
              onChange={(e) => update({ suggested: e.target.checked ? "1" : null })}
            />
            Include suggestions I haven&rsquo;t reviewed
          </label>
        </div>
      )}

      {inFolders && (
        <div className="crumbs-row">
          <nav className="crumbs" aria-label="Folder">
            <ol>
              <li>
                {choosing ? (
                  <span aria-current="page">Libraries</span>
                ) : (
                  <Link {...linkTo(librariesQuery)}>Libraries</Link>
                )}
              </li>
              {!choosing && (
                <li>
                  {crumbs.length === 0 ? (
                    <span aria-current="page">{libraryName || "This library"}</span>
                  ) : (
                    <Link {...linkTo((from) => folderQuery(from, libraryId, ""))}>
                      {libraryName || "This library"}
                    </Link>
                  )}
                </li>
              )}
              {crumbs.map((crumb, index) => (
                <li key={crumb.path}>
                  {index === crumbs.length - 1 ? (
                    <span aria-current="page">{crumb.name}</span>
                  ) : (
                    <Link {...linkTo((from) => folderQuery(from, libraryId, crumb.path))}>
                      {crumb.name}
                    </Link>
                  )}
                </li>
              ))}
            </ol>
          </nav>
          {/* What can be done with this folder as a whole: have a local
              model look through it (the whole library, at its top), start
              a listing — the same one the "＋ listing" badge on a
              photograph starts — and have the local Editor draft a cut. */}
          {!choosing && (offerReview || offerListing || draftHref) && (
            <div className="crumbs-actions">
              {offerReview && (
                <button
                  type="button"
                  className="btn"
                  title={
                    folderPath
                      ? `See what it would take for a local model to look through ${folderPath}`
                      : "See what it would take for a local model to look through this library"
                  }
                  onClick={(event) => {
                    reviewOpener.current = event.currentTarget;
                    setReviewing({ libraryId, libraryName, folder: folderPath });
                  }}
                >
                  Review with AI
                </button>
              )}
              {offerListing && (
                <button
                  type="button"
                  className="btn"
                  title={`Start a listing from the photographs in ${folderPath}`}
                  onClick={() =>
                    router.push(
                      `/listings?folder=${encodeURIComponent(folderPath)}&library=${libraryId}`,
                    )
                  }
                >
                  Start a listing from this folder
                </button>
              )}
              {draftHref && (
                <Link
                  className="btn"
                  href={draftHref}
                  title={`Have the local Editor draft a rough cut from the clips in ${folderPath}. Opens the form on the Cuts page; nothing is drafted until you press Draft there`}
                >
                  Draft a cut from this folder
                </Link>
              )}
            </div>
          )}
        </div>
      )}

      {choosing &&
        (!librariesRead ? (
          <div className="empty">Loading…</div>
        ) : librariesProblem ? (
          <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
            Could not load the libraries: {librariesProblem}
          </div>
        ) : libraries.length === 0 ? (
          <div className="empty">
            There are no libraries yet. Add one from <Link href="/libraries">Libraries</Link>.
          </div>
        ) : (
          <ul className="folderlist" aria-label="Libraries">
            {libraries.map((library) => (
              <li key={library.id}>
                <Link
                  className="folderrow"
                  {...linkTo((from) => folderQuery(from, library.id, ""))}
                >
                  <span className="folderrow-name">{library.name}</span>
                  <span className="folderrow-what">
                    {library.asset_count.toLocaleString("en-US")}{" "}
                    {library.asset_count === 1 ? "item" : "items"}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        ))}

      {inFolders &&
        !choosing &&
        (hereProblem ? (
          // A failed request is not an empty folder; it is its own message.
          <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
            Could not load this folder: {hereProblem}
          </div>
        ) : !here ? (
          <div className="empty">Loading…</div>
        ) : empty === "library" ? (
          <div className="empty">
            Nothing has been catalogued in this library yet. Scan it from{" "}
            <Link href="/libraries">Libraries</Link>.
          </div>
        ) : empty !== null ? (
          <div className="empty">
            Nothing is catalogued under this folder. It may have been renamed or moved.{" "}
            <Link {...linkTo((from) => folderQuery(from, libraryId, parentOf(folderPath)))}>
              Go up to {parentOf(folderPath) || libraryName || "the library"}
            </Link>
            .
          </div>
        ) : (
          here.folders.length > 0 && (
            <>
              <ul className="folderlist" aria-label="Folders inside">
                {here.folders.map((row) => (
                  <li key={row.path}>
                    <Link
                      className="folderrow"
                      {...linkTo((from) => folderQuery(from, libraryId, row.path))}
                    >
                      <span className="folderrow-name">{row.name}</span>
                      <span className="folderrow-what">{contentsText(row)}</span>
                      {aiHere && lookedText(row) && (
                        <span className="folderrow-looked">{lookedText(row)}</span>
                      )}
                      {row.newest && (
                        <span className="folderrow-what" title="The newest file inside">
                          {shortDate(row.newest)}
                        </span>
                      )}
                    </Link>
                  </li>
                ))}
              </ul>
              {here.truncated && (
                <p className="faint">
                  Showing the first {here.folders.length.toLocaleString("en-US")} folders.
                </p>
              )}
            </>
          )
        ))}

      {inFolders && showFiles && here && (
        <div className="browse-fileshead">
          <h3>{filesHeading(filesHere, deep)}</h3>
          {counted && !busy && shownNote(total, filesHere) && (
            <span className="faint mono">{shownNote(total, filesHere)}</span>
          )}
        </div>
      )}

      {showFiles && (
        <div className="toolbar">
          {!inFolders && (
            <select
              className="select"
              value={libraryId}
              onChange={(e) => update({ library: e.target.value })}
              aria-label="Library"
            >
              <option value="">All libraries</option>
              {libraries.map((lib) => (
                <option key={lib.id} value={lib.id}>
                  {lib.name} ({lib.asset_count.toLocaleString()})
                </option>
              ))}
            </select>
          )}

          <select
            className="select"
            value={mediaType}
            onChange={(e) => update({ media: e.target.value })}
            aria-label="Media type"
          >
            <option value="">All media</option>
            <option value="video">Video</option>
            <option value="image">Images</option>
            <option value="audio">Audio</option>
          </select>

          <select
            className="select"
            value={sort}
            onChange={(e) => update({ sort: e.target.value === "recent" ? null : e.target.value })}
            aria-label="Sort"
          >
            <option value="recent">Recently added</option>
            <option value="captured">Capture date (newest)</option>
            <option value="captured_asc">Capture date (oldest)</option>
            <option value="name">Filename</option>
            <option value="size">Largest first</option>
            {/* A local model's score: offered where models can run, and shown
                wherever a link already chose it. */}
            {(aiHere || sort === "score") && <option value="score">Best first (AI score)</option>}
          </select>

          <label
            className="pill"
            style={{ cursor: "pointer", gap: 7 }}
            data-tone={previewable ? "warn" : undefined}
          >
            <input
              type="checkbox"
              checked={previewable}
              onChange={(e) => update({ all: e.target.checked ? null : "1" })}
              style={{ accentColor: "var(--amber)" }}
            />
            Ready to view
          </label>

          {offerDeep && (
            <label className="pill" style={{ cursor: "pointer", gap: 7 }}>
              <input
                type="checkbox"
                checked={deep}
                onChange={(e) => update({ deep: e.target.checked ? "1" : null })}
              />
              Include sub-folders
            </label>
          )}

          <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
            <span className="faint mono" style={{ fontSize: "0.78rem" }}>
              {page} / {counted ? pages : "…"}
            </span>
            <button
              className="btn"
              disabled={page <= 1}
              onClick={() => turn(-1)}
              aria-label="Previous page"
            >
              ←
            </button>
            <button
              className="btn"
              disabled={page >= pages}
              onClick={() => turn(1)}
              aria-label="Next page"
            >
              →
            </button>
          </div>
        </div>
      )}

      {!showFiles ? null : error ? (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
          Could not load {inFolders ? "the files in this folder" : "this page"}: {error}
        </div>
      ) : busy && items.length === 0 ? (
        <div className="empty">Loading…</div>
      ) : items.length === 0 ? (
        here && emptyReason(here, deep) === "folders" ? (
          <div className="empty">
            No files directly in this folder. Open a folder above, or tick Include sub-folders.
          </div>
        ) : (
          <div className="empty">
            No assets match these filters.
            {previewable && " Try turning off “Ready to view” to include items still processing."}
          </div>
        )
      ) : (
        <div className="grid">
          {items.map((asset, index) => (
            <div
              key={asset.id}
              className="tile browse-tile"
              style={{ animationDelay: `${Math.min(index, 20) * 18}ms` }}
              title={asset.relative_path}
            >
              {/* The stage holds the picture and what sits over it: a button
                  cannot live inside the picture's link. */}
              <div className="tile-stage">
                <LookFrame
                  assetId={asset.id}
                  mediaType={asset.media_type}
                  filename={asset.filename}
                  onLook={() => setViewing(asset.id)}
                  // Drawn here so a file still being processed says so, as
                  // it always has on this page, instead of "No preview".
                  picture={
                    <Thumb
                      assetId={asset.id}
                      mediaType={asset.media_type}
                      status={asset.processing_status}
                    />
                  }
                >
                  {asset.media_type === "video" && asset.duration_s !== null && (
                    <span className="tile-badge">{duration(asset.duration_s)}</span>
                  )}
                  {asset.media_type === "audio" && <span className="tile-badge">audio</span>}
                  {scoreOf(asset) !== null && (
                    <span
                      className="scorepill"
                      data-tone={scoreTone(scoreOf(asset) ?? 0)}
                      role="img"
                      aria-label={scoreLabel(scoreOf(asset) ?? 0)}
                      title={`${scoreLabel(scoreOf(asset) ?? 0)} — a local model's opinion of how usable this is`}
                    >
                      AI {scoreOf(asset)}
                    </span>
                  )}
                </LookFrame>
                {asset.media_type === "image" && asset.relative_path.includes("/") && (
                  <button
                    type="button"
                    className="tile-badge"
                    style={{ right: "auto", left: 6, cursor: "pointer", border: 0 }}
                    title={`Start a listing from this shoot folder: ${asset.relative_path.slice(0, asset.relative_path.lastIndexOf("/"))}`}
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      const folder = asset.relative_path.slice(
                        0,
                        asset.relative_path.lastIndexOf("/"),
                      );
                      router.push(
                        `/listings?folder=${encodeURIComponent(folder)}&library=${asset.library_id}`,
                      );
                    }}
                  >
                    ＋ listing
                  </button>
                )}
              </div>
              <div className="tile-meta">
                <div className="tile-name">
                  <Link href={`/assets/${asset.id}`} title={`Open ${asset.filename}'s own page`}>
                    {asset.filename}
                  </Link>
                </div>
                <div className="tile-sub">
                  <span>{resolution(asset.width, asset.height)}</span>
                  <span>{shortDate(asset.captured_at ?? asset.mtime)}</span>
                </div>
                {elsewhere(asset) && (
                  <div className="tile-where">
                    <Link {...toFolderOf(asset)} title={`Open this folder: ${folderWords(asset)}`}>
                      in {folderWords(asset)}
                    </Link>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {viewed && (
        // Mounted once, beside the grid — never inside a tile, whose
        // `overflow: hidden` and hover transform would clip it.
        <MediaPreview
          assetId={viewed.id}
          mediaType={viewed.media_type}
          filename={viewed.filename}
          // What a local model said it shows, when one has; else where it is.
          detail={viewed.look_caption || viewed.relative_path}
          position={`${viewAt + 1} of ${items.length} on this page`}
          onClose={() => setViewing(null)}
          onPrev={viewAt > 0 ? () => setViewing(items[viewAt - 1]?.id ?? null) : undefined}
          onNext={
            viewAt < items.length - 1 ? () => setViewing(items[viewAt + 1]?.id ?? null) : undefined
          }
          returnFocus={focusAfterViewing}
        >
          {scoreOf(viewed) !== null && (
            <span className="scorepill" data-tone={scoreTone(scoreOf(viewed) ?? 0)}>
              {scoreLabel(scoreOf(viewed) ?? 0)}
            </span>
          )}
          {elsewhere(viewed) && (
            <Link className="btn" {...toFolderOf(viewed)}>
              Open its folder
            </Link>
          )}
        </MediaPreview>
      )}

      {/* Mounted once, as the viewer is. Opening it only reads; a review
          starts when Start is pressed inside it. */}
      <ReviewWithAi
        target={reviewing}
        returnFocus={() => reviewOpener.current}
        onClose={(touched) => {
          setReviewing(null);
          if (touched) setLookedRound((n) => n + 1);
        }}
      />
    </>
  );
}

export default function Page() {
  return (
    <Shell>
      <Suspense fallback={<div className="empty">Loading…</div>}>
        <BrowsePage />
      </Suspense>
    </Shell>
  );
}
