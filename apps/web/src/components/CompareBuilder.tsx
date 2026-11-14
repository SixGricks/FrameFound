"use client";

// New comparison: what to compare on, how many, which models, and what that
// will give — then Start. Nothing is started until the last button.
//
// The preview only counts (the server writes nothing), so it is asked again
// 400 ms after any change to the sample, and an answer that is not about
// what the form now says is dropped. Start waits for the preview of exactly
// what it would send: nothing is started on a sample the owner has not seen
// counted.
//
// Every count and every refusal is the server's own; the page lays them out.
// No model is ordered, marked or described by a figure here.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import {
  ApiError,
  api,
  type CompareCountedCut,
  type CompareCut,
  type CompareFolder,
  type CompareModelChoice,
  type CompareOverview,
  type CompareWhat,
  type ListingSummary,
  type SamplePreview,
} from "@/lib/api";
import {
  NO_CUTS,
  NO_MODEL,
  ONE_MODEL,
  SIZE_PROBLEM,
  SOURCE_CARDS,
  START_NOTE,
  bodyKey,
  cutLine,
  cutsByClient,
  defaultWhat,
  firstTicks,
  folderCounts,
  folderPlace,
  listingLine,
  liveTicks,
  modelInAddress,
  paceHint,
  previewLines,
  sampleBody,
  sampleMissing,
  sizeOf,
  startBlocker,
  tickBlocker,
  whatChoices,
  type BuilderState,
  type CompareKind,
} from "@/lib/compare";
import { focusIfLost } from "@/lib/localModels";
import { fixLink } from "@/lib/localModelsCompare";

const unreachable = (what: string) =>
  `FrameFound could not be reached to ${what}. Try again.`;

function sentence(err: unknown, what: string): string {
  return err instanceof ApiError ? err.message : unreachable(what);
}

export default function CompareBuilder({ overview }: { overview: CompareOverview }) {
  const router = useRouter();
  const limits = overview.limits;
  const manage = overview.can_manage;

  // 1 · What to compare on
  const [kind, setKind] = useState<CompareKind | null>(null);
  const [cuts, setCuts] = useState<CompareCut[] | null>(null);
  const [cutsProblem, setCutsProblem] = useState<string | null>(null);
  const [cutFilter, setCutFilter] = useState("");
  const [cutIds, setCutIds] = useState<string[]>([]);
  const [neighbours, setNeighbours] = useState(true);
  const [folderText, setFolderText] = useState("");
  const [found, setFound] = useState<CompareFolder[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [folderProblem, setFolderProblem] = useState<string | null>(null);
  const [folder, setFolder] = useState<CompareFolder | null>(null);
  const [what, setWhat] = useState<CompareWhat | null>(null);
  // The cuts that touch the chosen folder, as its first preview counted
  // them, and the ones the owner unticked since.
  const [folderCuts, setFolderCuts] = useState<CompareCountedCut[]>([]);
  const [unticked, setUnticked] = useState<string[]>([]);
  const [listings, setListings] = useState<ListingSummary[] | null>(null);
  const [listingsProblem, setListingsProblem] = useState<string | null>(null);
  const [listingId, setListingId] = useState<string | null>(null);
  // 2 · How many
  const [sizeText, setSizeText] = useState(String(limits.default_size));
  const [stills, setStills] = useState<1 | 3>(3);
  // 3 · Which models — null until the first answer has been read.
  const [ticked, setTicked] = useState<string[] | null>(null);
  // 4 · What you will get
  const [preview, setPreview] = useState<{ key: string; answer: SamplePreview } | null>(null);
  const [previewProblem, setPreviewProblem] = useState<{ key: string; text: string } | null>(null);
  const [counting, setCounting] = useState(false);
  const previewTicket = useRef(0);
  const searchTicket = useRef(0);
  // Start
  const [starting, setStarting] = useState(false);
  const [startProblem, setStartProblem] = useState<string | null>(null);
  // Focus, after the control that was pressed has gone.
  const searchField = useRef<HTMLInputElement>(null);
  const chosenFolder = useRef<HTMLButtonElement>(null);
  const [folderFocus, setFolderFocus] = useState<{ to: "field" | "chosen"; n: number }>({ to: "field", n: 0 });

  useEffect(() => {
    if (!folderFocus.n) return;
    focusIfLost(folderFocus.to === "chosen" ? chosenFolder.current : searchField.current);
  }, [folderFocus]);

  // The ticks start as every model that can be compared, or only the one
  // L6's link names (`?model=`). The address is read here, once, rather
  // than with useSearchParams, which would need a Suspense boundary under
  // Shell.
  useEffect(() => {
    if (ticked !== null) return;
    setTicked(firstTicks(overview.models, limits.max_models, modelInAddress(window.location.search)));
  }, [overview.models, limits.max_models, ticked]);

  // The cut list and the listings are read the first time their card is
  // chosen; each is small.
  useEffect(() => {
    if (kind !== "cuts" || cuts !== null) return;
    let live = true;
    api
      .compareCuts()
      .then((answer) => {
        if (!live) return;
        setCuts(answer);
        setCutsProblem(null);
      })
      .catch((err: unknown) => {
        if (live) setCutsProblem(sentence(err, "read the recorded cuts"));
      });
    return () => {
      live = false;
    };
  }, [kind, cuts]);

  useEffect(() => {
    if (kind !== "listing" || listings !== null) return;
    let live = true;
    api
      .listings()
      .then((answer) => {
        if (!live) return;
        setListings(answer);
        setListingsProblem(null);
      })
      .catch((err: unknown) => {
        if (live) setListingsProblem(sentence(err, "read the listings"));
      });
    return () => {
      live = false;
    };
  }, [kind, listings]);

  // The folder search runs 300 ms after the last key. With nothing typed it
  // shows the folders with the most shots, so there is something to choose
  // from before the owner knows a name.
  useEffect(() => {
    if (kind !== "folder" || folder) return;
    const mine = ++searchTicket.current;
    const timer = window.setTimeout(() => {
      setSearching(true);
      api
        .compareFolders(folderText.trim().slice(0, 200))
        .then((answer) => {
          if (mine !== searchTicket.current) return;
          setFound(answer);
          setFolderProblem(null);
        })
        .catch((err: unknown) => {
          if (mine !== searchTicket.current) return;
          setFolderProblem(sentence(err, "search the folders"));
        })
        .finally(() => {
          if (mine === searchTicket.current) setSearching(false);
        });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [kind, folder, folderText]);

  const state: BuilderState = {
    kind,
    cutIds,
    neighbours,
    folder: folder ? { library_id: folder.library_id, path: folder.path } : null,
    what,
    folderCutIds: folderCuts.map((cut) => cut.id),
    untickedCutIds: unticked,
    listingId,
    sizeText,
    stills,
  };
  const missing = sampleMissing(state, limits);
  const key = bodyKey(sampleBody(state, limits));

  // The preview, 400 ms after the sample last changed. `key` is the whole
  // request, so it is the one thing this waits on.
  useEffect(() => {
    const mine = ++previewTicket.current;
    if (!key) {
      setCounting(false);
      return;
    }
    const timer = window.setTimeout(() => {
      setCounting(true);
      api
        .previewComparison(JSON.parse(key))
        .then((answer) => {
          if (mine !== previewTicket.current) return;
          setPreview({ key, answer });
          setPreviewProblem(null);
        })
        .catch((err: unknown) => {
          if (mine !== previewTicket.current) return;
          setPreviewProblem({ key, text: sentence(err, "count the sample") });
        })
        .finally(() => {
          if (mine === previewTicket.current) setCounting(false);
        });
    }, 400);
    return () => window.clearTimeout(timer);
  }, [key]);

  // A folder's counted cuts are taken from a preview that counted every cut
  // touching it (nothing unticked yet); later previews count fewer.
  useEffect(() => {
    if (!preview || preview.key !== key || kind !== "folder" || unticked.length) return;
    setFolderCuts(preview.answer.truth.used.cuts);
  }, [preview, key, kind, unticked.length]);

  function chooseFolder(chosen: CompareFolder) {
    setFolder(chosen);
    setWhat(defaultWhat(chosen));
    setFolderCuts([]);
    setUnticked([]);
    setFolderFocus((now) => ({ to: "chosen", n: now.n + 1 }));
  }

  function forgetFolder() {
    setFolder(null);
    setWhat(null);
    setFolderCuts([]);
    setUnticked([]);
    setFolderFocus((now) => ({ to: "field", n: now.n + 1 }));
  }

  const models = overview.models;
  const live = liveTicks(models, ticked ?? []);
  const tickedModels = models.filter((model) => live.includes(model.id));
  const current = preview && preview.key === key ? preview.answer : null;
  const failedNow = !!previewProblem && previewProblem.key === key;
  const shown = current ?? (counting ? (preview?.answer ?? null) : null);
  const blocker = startBlocker({
    missing,
    key,
    preview: preview ? { key: preview.key, ok: preview.answer.ok } : null,
    previewFailed: failedNow,
    ticked: live.length,
  });
  const sizeNow = sizeOf(sizeText, limits);
  const shotsInPlay = kind === "cuts" || (kind === "folder" && what !== "photos");

  async function start() {
    const body = sampleBody(state, limits);
    if (!body || blocker || starting) return;
    setStarting(true);
    setStartProblem(null);
    try {
      const made = await api.startComparison({ ...body, name: null, seed: null, model_ids: live });
      // Stays "starting" until the page changes, so it cannot be pressed twice.
      router.push(`/models/compare/${made.id}`);
    } catch (err) {
      setStartProblem(sentence(err, "start the comparison"));
      setStarting(false);
    }
  }

  function toggle(id: string) {
    setTicked((now) => {
      const list = liveTicks(models, now ?? []);
      return list.includes(id) ? list.filter((one) => one !== id) : [...list, id];
    });
  }

  return (
    <div className="card compare-builder">
      <h3 className="compare-step">1 · What to compare on</h3>
      <div className="sourcecards" role="radiogroup" aria-label="What to compare on">
        {SOURCE_CARDS.map((card) => (
          <label key={card.kind} className="sourcecard">
            <input
              type="radio"
              name="compare-source"
              value={card.kind}
              checked={kind === card.kind}
              onChange={() => setKind(card.kind)}
            />
            <span>
              <strong>{card.label}</strong>
              <small className="faint">{card.text}</small>
            </span>
          </label>
        ))}
      </div>

      {kind === "cuts" && (
        <div className="compare-source">
          {cutsProblem && <p className="compare-said" data-tone="bad">{cutsProblem}</p>}
          {cuts === null && !cutsProblem && <p className="faint">Reading the recorded cuts…</p>}
          {cuts !== null && cuts.length === 0 && <p className="faint">{NO_CUTS}</p>}
          {cuts !== null && cuts.length > 0 && (
            <>
              <input
                className="input"
                aria-label="Show only cuts whose title holds"
                placeholder="Filter by title"
                value={cutFilter}
                onChange={(event) => setCutFilter(event.target.value)}
                style={{ width: "100%", maxWidth: 360 }}
              />
              <div className="compare-cuts">
                {cutsByClient(cuts, cutFilter).map((group) => (
                  <fieldset key={group.client}>
                    <legend className="eyebrow">{group.client}</legend>
                    {group.cuts.map((cut) => {
                      const line = cutLine(cut);
                      return (
                        <label key={cut.id} className="compare-tick">
                          <input
                            type="checkbox"
                            checked={cutIds.includes(cut.id)}
                            onChange={() =>
                              setCutIds((now) =>
                                now.includes(cut.id) ? now.filter((id) => id !== cut.id) : [...now, cut.id],
                              )
                            }
                          />
                          <span>
                            {line.title} <span className="faint">· {line.rest}</span>
                          </span>
                        </label>
                      );
                    })}
                  </fieldset>
                ))}
                {cutsByClient(cuts, cutFilter).length === 0 && (
                  <p className="faint" style={{ margin: 0 }}>
                    No recorded cut has that in its title.
                  </p>
                )}
              </div>
              {cutIds.length > 0 && (
                <p className="faint" style={{ margin: "8px 0 0" }}>
                  {cutIds.length === 1 ? "1 cut ticked." : `${cutIds.length} cuts ticked.`}
                </p>
              )}
              <label className="compare-tick" style={{ marginTop: 10 }}>
                <input
                  type="checkbox"
                  checked={neighbours}
                  onChange={(event) => setNeighbours(event.target.checked)}
                />
                <span>
                  Also take shots from the same folders that were not used.{" "}
                  <span className="faint">
                    (Leave this on: the figures need shots that were <em>not</em> chosen to compare
                    against.)
                  </span>
                </span>
              </label>
            </>
          )}
        </div>
      )}

      {kind === "folder" && (
        <div className="compare-source">
          {!folder && (
            <>
              <input
                ref={searchField}
                className="input"
                aria-label="Type part of a folder's name"
                placeholder="Type part of a folder's name"
                value={folderText}
                onChange={(event) => setFolderText(event.target.value)}
                autoComplete="off"
                spellCheck={false}
                style={{ width: "100%", maxWidth: 420 }}
              />
              {folderProblem && <p className="compare-said" data-tone="bad">{folderProblem}</p>}
              {searching && !found && <p className="faint">Looking…</p>}
              {found && found.length === 0 && (
                <p className="faint">
                  {folderText.trim() ? "No folder has that in its name." : "No library has any folders yet."}
                </p>
              )}
              {found && found.length > 0 && (
                <ul className="compare-results" aria-busy={searching}>
                  {found.map((one) => (
                    <li key={`${one.library_id}/${one.path}`}>
                      <button type="button" className="btn" onClick={() => chooseFolder(one)}>
                        <span className="compare-result-place">{folderPlace(one)}</span>
                        <span className="faint"> — {folderCounts(one)}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
          {folder && (
            <>
              <p style={{ margin: 0 }}>
                <span className="pill compare-chosen">
                  {folderPlace(folder)}
                  <button
                    ref={chosenFolder}
                    type="button"
                    className="chipx"
                    aria-label={`Choose another folder than ${folderPlace(folder)}`}
                    title="Choose another folder"
                    onClick={forgetFolder}
                  >
                    ×
                  </button>
                </span>{" "}
                <span className="faint">{folderCounts(folder)}</span>
              </p>
              <div className="toolbar" style={{ margin: "12px 0 0" }} role="group" aria-label="Look at">
                <span className="faint">Look at</span>
                {whatChoices(folder).map((choice) => (
                  <button
                    key={choice.what}
                    type="button"
                    className="btn"
                    aria-pressed={what === choice.what}
                    disabled={!!choice.blocked}
                    title={choice.blocked ?? undefined}
                    onClick={() => setWhat(choice.what)}
                  >
                    {choice.label}
                  </button>
                ))}
              </div>
              {[...new Set(whatChoices(folder).map((choice) => choice.blocked).filter(Boolean))].map((why) => (
                <p key={why} className="faint" style={{ margin: "6px 0 0" }}>
                  {why}
                </p>
              ))}
              {what !== "photos" && folderCuts.length > 0 && (
                <fieldset className="compare-counted">
                  <legend>Counted as used:</legend>
                  {folderCuts.map((cut) => (
                    <label key={cut.id} className="compare-tick">
                      <input
                        type="checkbox"
                        checked={!unticked.includes(cut.id)}
                        onChange={() =>
                          setUnticked((now) =>
                            now.includes(cut.id) ? now.filter((id) => id !== cut.id) : [...now, cut.id],
                          )
                        }
                      />
                      <span>{cut.title}</span>
                    </label>
                  ))}
                </fieldset>
              )}
            </>
          )}
        </div>
      )}

      {kind === "listing" && (
        <div className="compare-source">
          {listingsProblem && <p className="compare-said" data-tone="bad">{listingsProblem}</p>}
          {listings === null && !listingsProblem && <p className="faint">Reading the listings…</p>}
          {listings !== null && listings.length === 0 && (
            <p className="faint">
              No listings yet. <Link href="/listings">Listings</Link> is where one is made.
            </p>
          )}
          {listings !== null && listings.length > 0 && (
            <select
              className="select"
              aria-label="Listing"
              value={listingId ?? ""}
              onChange={(event) => setListingId(event.target.value || null)}
              style={{ maxWidth: "100%" }}
            >
              <option value="">Choose a listing…</option>
              {listings.map((listing) => (
                <option key={listing.id} value={listing.id}>
                  {listingLine(listing)}
                </option>
              ))}
            </select>
          )}
        </div>
      )}

      <h3 className="compare-step">2 · How many</h3>
      <div className="toolbar" style={{ margin: 0 }}>
        <label className="compare-size">
          <span>Items</span>
          <input
            className="input mono"
            type="number"
            inputMode="numeric"
            min={limits.min_size}
            max={limits.max_size}
            step={1}
            value={sizeText}
            onChange={(event) => setSizeText(event.target.value)}
            aria-invalid={sizeNow === null}
            aria-describedby="compare-size-hint"
          />
        </label>
        <span id="compare-size-hint" className="faint" style={{ flex: "1 1 16em" }}>
          {sizeNow === null ? (
            <span style={{ color: "var(--ember)" }}>{SIZE_PROBLEM(limits)}</span>
          ) : (
            paceHint(tickedModels, current?.ok ? current.items : sizeNow)
          )}
        </span>
      </div>
      {shotsInPlay && (
        <details className="disclosure" style={{ marginTop: 10 }}>
          <summary>More</summary>
          <fieldset className="compare-counted">
            <legend>Stills per shot</legend>
            <label className="compare-tick">
              <input type="radio" name="compare-stills" checked={stills === 3} onChange={() => setStills(3)} />
              <span>Three (start, middle, end)</span>
            </label>
            <label className="compare-tick">
              <input type="radio" name="compare-stills" checked={stills === 1} onChange={() => setStills(1)} />
              <span>One (the middle)</span>
            </label>
            <p className="faint" style={{ margin: "6px 0 0" }}>
              Use one if a model cannot read three pictures at once.
            </p>
          </fieldset>
        </details>
      )}

      <h3 className="compare-step">3 · Which models</h3>
      {models.length === 0 || !models.some((model) => model.can_compare) ? (
        <p className="compare-said" data-tone="warn">
          {models.length === 0 ? NO_MODEL : "None of the installed models can be compared yet."}{" "}
          <Link href="/models#add">AI models › Add a model</Link>
        </p>
      ) : null}
      {models.length > 0 && (
        <ul className="compare-models">
          {models.map((model) => (
            <ModelRow
              key={model.id}
              model={model}
              checked={live.includes(model.id)}
              blocker={tickBlocker(model, live, limits.max_models)}
              onToggle={() => toggle(model.id)}
            />
          ))}
        </ul>
      )}
      {live.length === 1 && <p className="faint">{ONE_MODEL}</p>}

      <h3 className="compare-step">4 · What you will get</h3>
      <div className="compare-preview" role="status">
        {missing ? (
          <p className="faint" style={{ margin: 0 }}>
            {missing}
          </p>
        ) : (
          <>
            {counting && (
              <p className="compare-counting">
                <span className="spinner" aria-hidden="true" /> Counting…
              </p>
            )}
            {failedNow && !counting && (
              <p className="compare-said" data-tone="bad" style={{ margin: 0 }}>
                {previewProblem?.text}
              </p>
            )}
            {shown && (
              // An older count stays, faded, while the new one is asked:
              // the card does not flash empty at every key.
              <div style={{ opacity: current ? 1 : 0.5 }}>
                {previewLines(shown).map((line, n) => (
                  <p key={n} className="compare-said" data-tone={line.tone === "warn" ? "warn" : undefined}>
                    {line.lead && <strong>{line.lead}</strong>} {line.text}
                  </p>
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {manage ? (
        <div className="compare-start">
          {startProblem && (
            <div className="card" role="alert" style={{ borderColor: "var(--ember)", margin: "0 0 12px" }}>
              {startProblem}
            </div>
          )}
          <button
            type="button"
            className="btn btn-primary"
            disabled={!!blocker || starting}
            title={blocker ?? undefined}
            aria-describedby={blocker ? "compare-start-why" : undefined}
            onClick={start}
          >
            {starting ? "Starting…" : "Start comparing"}
          </button>
          {blocker && (
            <span id="compare-start-why" className="faint">
              {blocker}
            </span>
          )}
          <p className="faint" style={{ margin: "10px 0 0" }}>
            {START_NOTE}
          </p>
        </div>
      ) : (
        <p className="faint" style={{ margin: "16px 0 0" }}>
          Only an administrator can start a comparison.
        </p>
      )}
    </div>
  );
}

function ModelRow({
  model,
  checked,
  blocker,
  onToggle,
}: {
  model: CompareModelChoice;
  checked: boolean;
  blocker: string | null;
  onToggle: () => void;
}) {
  const fits =
    model.fits === "day"
      ? { tone: "ok", label: "by day", title: "Fits the daytime budget, so it can run at any hour." }
      : model.fits === "night"
        ? {
            tone: "warn",
            label: "overnight only",
            title: "It needs more than the daytime budget, so it runs in the overnight window.",
          }
        : { tone: "bad", label: "over budget", title: "It needs more than the overnight budget." };
  // Its program not installed: the reason says so, and this is where to.
  const fix = fixLink(model);
  return (
    <li className="compare-model" data-blocked={blocker && !checked ? "true" : undefined}>
      <label className="compare-tick">
        <input
          type="checkbox"
          checked={checked}
          disabled={!!blocker && !checked}
          onChange={onToggle}
          title={blocker && !checked ? blocker : undefined}
          aria-describedby={blocker && !checked && !model.can_compare ? `compare-model-${model.id}` : undefined}
        />
        <span className="compare-model-name">
          {model.name} <span className="faint mono">build {model.revision_short}</span>
        </span>
      </label>
      <span className="compare-model-pills">
        <span className="pill" data-tone={fits.tone} title={fits.title}>
          {fits.label}
        </span>
        {model.demo && (
          <span className="pill" data-tone="warn" title="A demonstration model: nothing it says is real.">
            demo
          </span>
        )}
      </span>
      {/* A model that cannot be compared says why; one held back only by the
          limit of six says so in its tick box's title. */}
      {blocker && !checked && !model.can_compare && (
        <span id={`compare-model-${model.id}`} className="compare-model-why">
          {blocker}
          {fix && (
            <>
              {" "}
              <Link href={fix.href}>{fix.label}</Link>
            </>
          )}
        </span>
      )}
      {model.can_compare && model.roles.length === 0 && (
        <span className="compare-model-why faint">
          Not on this Mac: it loads from the model library, which is slower.
        </span>
      )}
    </li>
  );
}
