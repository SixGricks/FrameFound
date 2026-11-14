"use client";

// One comparison: its name and sample, one column of figures per model with
// its progress and buttons, and what the owner decided.
//
// Nothing about a run lives in the tab. One request (`api.comparison(id)`)
// draws the page, read again every 2.5 s while a column is queued or
// running, every 30 s while columns only wait, never while the tab is
// hidden, and at once after every action — so a bar survives a reload, and
// a comparison left and come back to is where the server says it is.
//
// Every change goes through one `act()`: it shows the server's own sentence
// when it says no, at the top of the part of the page the change belongs
// to, and reads the comparison again either way. A reader who is not an
// administrator sees everything and no buttons.
//
// The answers, item by item, go between the figures and the decision. They
// are read on their own (`api.comparisonItems(id)`), on load and again when
// something about the answers moved, at most every five seconds: the polled
// comparison stays small, and two hundred items are not read every 2.5 s.

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

import CompareAnswers, { ANSWERS_HEAD } from "@/components/CompareAnswers";
import CompareMeasures, { type CompareAct } from "@/components/CompareMeasures";
import CompareVerdict from "@/components/CompareVerdict";
import Shell from "@/components/Shell";
import { ApiError, api, type CompareModelChoice, type Comparison, type ComparisonItems } from "@/lib/api";
import {
  DELETE_QUESTION,
  TOO_MANY,
  addChoices,
  againTicks,
  comparisonEvery,
  comparisonLine,
  earlierQuestion,
  endedColumns,
  itemsKey,
  itemsReader,
  tickBlocker,
  type ItemsReader,
} from "@/lib/compare";
import { usePageTitle } from "@/lib/hooks";
import { focusIfLost, leadSentence, rereads, type Rereader } from "@/lib/localModels";

type Where = "head" | "figures" | "decision";
type Said = { where: Where; text: string; tone: "alert" | "status" };

const NAME_CHARS = 200;

function sentenceOf(err: unknown, what: string): string {
  return err instanceof ApiError ? err.message : `FrameFound could not be reached to ${what}. Try again.`;
}

export default function ComparisonPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const router = useRouter();

  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  const goneRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<Said | null>(null);
  const [ended, setEnded] = useState<string | null>(null);
  const previous = useRef<Comparison | null>(null);
  const reader = useRef<Rereader<Comparison> | null>(null);
  const [items, setItems] = useState<ComparisonItems | null>(null);
  const [itemsProblem, setItemsProblem] = useState<string | null>(null);

  usePageTitle(comparison?.name ?? "Comparison");

  useEffect(() => {
    goneRef.current = false;
    previous.current = null;
    const own = rereads<Comparison>({
      read: () => api.comparison(id),
      answered: (next) => {
        const before = previous.current;
        if (before) {
          const finished = endedColumns(before.entries, next.entries);
          if (finished) setEnded(finished);
        }
        previous.current = next;
        setComparison(next);
        setLoadError(null);
        setGone(false);
      },
      failed: (err) => {
        if (err instanceof ApiError && err.status === 404) {
          // Deleted (here or in another tab): say so, and stop reading.
          goneRef.current = true;
          setGone(true);
          return;
        }
        // The last good comparison stays on screen beneath the sentence.
        setLoadError(
          err instanceof ApiError
            ? err.message
            : "FrameFound could not be reached to read this comparison. It tries again shortly.",
        );
      },
      every: (latest, failed) => (goneRef.current ? null : comparisonEvery(latest, failed)),
      hidden: () => document.hidden,
      later: (run, ms) => window.setTimeout(run, ms),
      cancel: (handle) => window.clearTimeout(handle as number),
    });
    reader.current = own;
    void own.now();
    const onShow = () => {
      if (!document.hidden) void own.now();
    };
    document.addEventListener("visibilitychange", onShow);
    return () => {
      document.removeEventListener("visibilitychange", onShow);
      own.stop();
      if (reader.current === own) reader.current = null;
    };
  }, [id]);

  const load = useCallback(async () => {
    await reader.current?.now();
  }, []);

  // The items: read when the comparison first arrives and whenever its
  // answers move, never sooner than five seconds after the last read. A read
  // that has started is let finish (see `itemsReader`); only another
  // comparison (Compare again lands on the same page component) or leaving
  // the page drops it, so nothing of the last one's items shows under this
  // one's name.
  const itemsReaderRef = useRef<ItemsReader | null>(null);
  useEffect(() => {
    setItems(null);
    setItemsProblem(null);
    const own = itemsReader<ComparisonItems>({
      read: () => api.comparisonItems(id),
      answered: (read) => {
        setItems(read);
        setItemsProblem(null);
      },
      failed: (err) => {
        // A 404 is the comparison going; its own read says so.
        if (err instanceof ApiError && err.status === 404) return false;
        setItemsProblem(
          err instanceof ApiError
            ? err.message
            : "FrameFound could not be reached to read the answers. It tries again shortly.",
        );
        return true;
      },
      later: (run, ms) => window.setTimeout(run, ms),
      cancel: (handle) => window.clearTimeout(handle as number),
    });
    itemsReaderRef.current = own;
    return () => {
      own.stop();
      if (itemsReaderRef.current === own) itemsReaderRef.current = null;
    };
  }, [id]);

  const answersKey = comparison && !gone ? itemsKey(comparison) : null;
  useEffect(() => {
    if (answersKey !== null) itemsReaderRef.current?.moved(answersKey);
  }, [id, answersKey]);

  const act = useCallback(
    async (where: Where, work: () => Promise<unknown>, done?: string | null) => {
      setBusy(true);
      setSaid(null);
      let worked = false;
      try {
        await work();
        worked = true;
        if (done) setSaid({ where, text: done, tone: "status" });
      } catch (err) {
        setSaid({ where, text: sentenceOf(err, "make that change"), tone: "alert" });
      }
      // The buttons stay off until the comparison is read again: drawn from
      // the old one, a second press would repeat a change that worked (a model
      // added twice, a column carried on twice) and the server's refusal would
      // read as a failure.
      try {
        await load();
      } finally {
        setBusy(false);
      }
      return worked;
    },
    [load],
  );

  const actIn = useCallback(
    (where: Where): CompareAct =>
      (work, done) =>
        act(where, work, done),
    [act],
  );

  function saidIn(where: Where) {
    if (!said || said.where !== where) return null;
    return (
      <div
        className="card"
        role={said.tone}
        style={{ borderColor: said.tone === "alert" ? "var(--ember)" : "var(--sage)", margin: "0 0 12px" }}
      >
        {said.text}
      </div>
    );
  }

  const back = (
    <Link href="/models/compare" className="compare-back">
      ‹ Compare models
    </Link>
  );

  if (gone) {
    return (
      <Shell>
        <div className="sectionhead" style={{ marginTop: 0 }}>
          {back}
        </div>
        <div className="empty">
          This comparison is not here any more. <Link href="/models/compare">Compare models</Link>
        </div>
      </Shell>
    );
  }

  if (!comparison) {
    return (
      <Shell>
        <div className="sectionhead" style={{ marginTop: 0 }}>
          {back}
        </div>
        {loadError ? (
          <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
            {loadError}
          </div>
        ) : (
          <div className="empty">Loading…</div>
        )}
      </Shell>
    );
  }

  const [demoLead, demoRest] = leadSentence(comparison.note ?? "Demonstration only.");
  // The mode is said right under the name, above everything else on the page.
  const notice = (
    <>
      {comparison.mode === "unavailable" && (
        <div className="card" role="status" style={{ marginTop: 14 }}>
          <p style={{ margin: 0 }}>
            <strong>Models run only on a Mac.</strong>{" "}
            {comparison.note ??
              "This FrameFound runs in containers on a Linux server, where the software that runs these models does not exist. Everything else works as before."}
          </p>
          <p className="muted" style={{ margin: "8px 0 0" }}>
            This comparison was made on a Mac. It can be read here, not changed.
          </p>
        </div>
      )}
      {comparison.mode === "demo" && (
        <div className="demo-banner" role="status">
          <strong>{demoLead}</strong> {demoRest} It is here so Compare can be looked at on a
          development machine.
        </div>
      )}
    </>
  );

  return (
    <Shell>
      <Header
        comparison={comparison}
        back={back}
        busy={busy}
        act={(work, done) => act("head", work, done)}
        onDelete={async () => {
          if (!window.confirm(DELETE_QUESTION)) return;
          setBusy(true);
          setSaid(null);
          try {
            await api.deleteComparison(comparison.id);
            router.push("/models/compare");
          } catch (err) {
            setSaid({ where: "head", text: sentenceOf(err, "delete the comparison"), tone: "alert" });
            try {
              await load();
            } finally {
              setBusy(false);
            }
          }
        }}
        saidHere={saidIn("head")}
        notice={notice}
      />

      {loadError && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)", marginTop: 14 }}>
          {loadError}
        </div>
      )}
      {!comparison.can_manage && comparison.mode !== "unavailable" && (
        <p className="faint" style={{ margin: "12px 0 0" }}>
          Only an administrator can change anything here.
        </p>
      )}
      {ended && (
        <p role="status" style={{ margin: "12px 0 0", color: "var(--sage)" }}>
          {ended}
        </p>
      )}

      <nav className="jumpbar" aria-label="Parts of this comparison">
        <a href="#figures">Figures</a>
        <a href="#answers">Answers</a>
        <a href="#decision">Decision</a>
      </nav>

      <section id="figures" className="secsection">
        <div className="sectionhead">
          <h2 id="compare-figures-head" tabIndex={-1}>
            Figures
          </h2>
          <span className="faint mono">
            {comparison.entries.length === 1 ? "1 model" : `${comparison.entries.length} models`} · in the order
            added
          </span>
        </div>
        {saidIn("figures")}
        <CompareMeasures
          comparison={comparison}
          busy={busy}
          act={async (work, done) => {
            const worked = await actIn("figures")(work, done);
            // A column taken out takes its button with it.
            focusIfLost(document.getElementById("compare-figures-head"));
            return worked;
          }}
        />
      </section>

      <section id="answers" className="secsection">
        <div className="sectionhead">
          <h2 id={ANSWERS_HEAD} tabIndex={-1}>
            Answers
          </h2>
          <span className="faint mono">
            {items ? (items.total === 1 ? "1 item" : `${items.total} items`) : ""}
            {items && comparison.entries.length ? " · each model's answer beside what you had decided" : ""}
          </span>
        </div>
        <CompareAnswers comparison={comparison} items={items} problem={itemsProblem} />
      </section>

      <section id="decision" className="secsection">
        <div className="sectionhead">
          <h2>What did you decide?</h2>
        </div>
        {saidIn("decision")}
        <CompareVerdict comparison={comparison} busy={busy} act={actIn("decision")} />
      </section>
    </Shell>
  );
}

// ---- the header: name, Rename, the sample, Add a model, Compare again, Delete ----------

function Header({
  comparison,
  back,
  busy,
  act,
  onDelete,
  saidHere,
  notice,
}: {
  comparison: Comparison;
  back: ReactNode;
  busy: boolean;
  act: CompareAct;
  onDelete: () => Promise<void>;
  saidHere: ReactNode;
  notice: ReactNode;
}) {
  const router = useRouter();
  const manage = comparison.can_manage;
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState("");
  const renameButton = useRef<HTMLButtonElement | null>(null);
  const [panel, setPanel] = useState<"add" | "again" | null>(null);
  const [models, setModels] = useState<CompareModelChoice[] | null>(null);
  const [maxModels, setMaxModels] = useState(6);
  const [modelsProblem, setModelsProblem] = useState<string | null>(null);
  const [addId, setAddId] = useState("");
  const [againIds, setAgainIds] = useState<string[]>([]);
  const [againProblem, setAgainProblem] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  // Closing the field (saved or not) puts focus back on Rename, which is
  // drawn again only after the field goes.
  const refocus = useRef(false);
  useEffect(() => {
    if (renaming || !refocus.current) return;
    refocus.current = false;
    renameButton.current?.focus();
  }, [renaming]);

  function closeRename() {
    refocus.current = true;
    setRenaming(false);
  }

  async function saveName() {
    if (draft === comparison.name) {
      closeRename();
      return;
    }
    if (await act(() => api.updateComparison(comparison.id, { name: draft }))) closeRename();
  }

  // The models offered by Add and Compare again come from the Compare
  // overview, read when a panel opens, so they are as the server has them now.
  async function open(which: "add" | "again") {
    if (panel === which) {
      setPanel(null);
      return;
    }
    setPanel(which);
    setModelsProblem(null);
    setAgainProblem(null);
    try {
      const overview = await api.compareOverview();
      setModels(overview.models);
      setMaxModels(overview.limits.max_models);
      if (which === "again") setAgainIds(againTicks(overview.models, comparison.entries, overview.limits.max_models));
      if (which === "add") {
        const first = addChoices(overview.models, comparison.entries, overview.limits.max_models).find(
          (one) => !one.blocked,
        );
        setAddId(first?.model.id ?? "");
      }
    } catch (err) {
      setModelsProblem(sentenceOf(err, "read the installed models"));
    }
  }

  const offered = models ? addChoices(models, comparison.entries, maxModels) : [];
  const adding = offered.find((one) => one.model.id === addId && !one.blocked) ?? null;

  // A model added closes the panel and its Add button with it: focus goes
  // back to "Add a model", or — when that one went because the comparison is
  // now full — to "Compare again", so the next Tab carries on from here and
  // not from the top of the page. Done in an effect: the buttons are enabled
  // again only once the comparison has been read again.
  const addToggle = useRef<HTMLButtonElement | null>(null);
  const againToggle = useRef<HTMLButtonElement | null>(null);
  const refocusAdd = useRef(false);
  useEffect(() => {
    if (panel !== null || busy || !refocusAdd.current) return;
    refocusAdd.current = false;
    focusIfLost(addToggle.current ?? againToggle.current);
  }, [panel, busy]);

  async function add() {
    if (!adding) return;
    const name = adding.model.name;
    if (await act(() => api.addComparisonModels(comparison.id, [adding.model.id]), `${name} is added. It runs on the same sample.`)) {
      refocusAdd.current = true;
      setPanel(null);
    }
  }

  async function compareAgain() {
    if (!againIds.length || starting) return;
    setStarting(true);
    setAgainProblem(null);
    try {
      const made = await api.startComparison({
        source: { kind: "comparison", comparison_id: comparison.id },
        name: null,
        seed: null,
        model_ids: againIds,
      });
      // Stays "starting" until the page changes, so it cannot be pressed twice.
      router.push(`/models/compare/${made.id}`);
    } catch (err) {
      setAgainProblem(sentenceOf(err, "start the comparison"));
      setStarting(false);
    }
  }

  const againOffered = (models ?? []).filter((model) => model.can_compare);

  return (
    <div className="compare-head">
      <div className="sectionhead" style={{ marginTop: 0, flexDirection: "column", alignItems: "flex-start", gap: 4 }}>
        {back}
        {renaming && manage ? (
          <form
            className="compare-rename"
            onSubmit={(event) => {
              event.preventDefault();
              void saveName();
            }}
          >
            <input
              className="input"
              value={draft}
              aria-label="The comparison's name"
              maxLength={NAME_CHARS}
              disabled={busy}
              autoFocus
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") closeRename();
              }}
            />
            <button type="submit" className="btn btn-primary" disabled={busy}>
              Save
            </button>
            <button type="button" className="btn" onClick={closeRename}>
              Cancel
            </button>
          </form>
        ) : (
          <div className="compare-title">
            <h2>{comparison.name}</h2>
            {manage && (
              <button
                ref={renameButton}
                type="button"
                className="btn"
                disabled={busy}
                onClick={() => {
                  setDraft(comparison.name);
                  setRenaming(true);
                }}
              >
                Rename
              </button>
            )}
          </div>
        )}
      </div>
      <p className="faint mono compare-line">{comparisonLine(comparison)}</p>
      {notice}
      {!comparison.current && <p className="compare-said" data-tone="warn">{earlierQuestion(comparison.prompt_version)}</p>}
      {comparison.left_out.map((left) => (
        <p key={left.why} className="faint compare-said">
          Left out: {left.sentence}
        </p>
      ))}

      {saidHere}

      {manage && (
        <div className="compare-buttons compare-head-buttons">
          {comparison.can_add_models ? (
            <button
              ref={addToggle}
              type="button"
              className="btn"
              aria-expanded={panel === "add"}
              aria-controls="compare-add"
              disabled={busy}
              onClick={() => void open("add")}
            >
              Add a model
            </button>
          ) : (
            comparison.current &&
            comparison.entries.length >= maxModels && <span className="faint">{TOO_MANY(maxModels)}</span>
          )}
          <button
            ref={againToggle}
            type="button"
            className="btn"
            aria-expanded={panel === "again"}
            aria-controls="compare-again"
            disabled={busy}
            onClick={() => void open("again")}
          >
            Compare again on the same sample
          </button>
          <button type="button" className="btn compare-delete" disabled={busy} onClick={() => void onDelete()}>
            Delete
          </button>
        </div>
      )}

      {manage && panel === "add" && (
        <div id="compare-add" className="card compare-panel">
          {modelsProblem ? (
            <p className="compare-said" data-tone="bad" role="alert">
              {modelsProblem}
            </p>
          ) : !models ? (
            <p className="compare-counting">
              <span className="spinner" aria-hidden="true" /> Reading the installed models…
            </p>
          ) : offered.length === 0 ? (
            <p className="compare-said">
              Every installed model is in this comparison already.{" "}
              <Link href="/models#add" className="compare-link">
                Add a model to FrameFound
              </Link>
            </p>
          ) : (
            <>
              <label className="compare-label-strong" htmlFor="compare-add-model">
                The model to add
              </label>
              <div className="compare-start" style={{ marginTop: 6 }}>
                <select
                  id="compare-add-model"
                  className="select"
                  value={addId}
                  disabled={busy}
                  onChange={(event) => setAddId(event.target.value)}
                >
                  {!adding && <option value="">Choose a model</option>}
                  {offered.map(({ model, blocked }) => (
                    <option key={model.id} value={model.id} disabled={!!blocked}>
                      {model.name} (build {model.revision_short}){blocked ? ` — ${blocked}` : ""}
                    </option>
                  ))}
                </select>
                <button type="button" className="btn btn-primary" disabled={busy || !adding} onClick={() => void add()}>
                  Add
                </button>
              </div>
              <p className="faint" style={{ margin: "8px 0 0" }}>
                It is shown exactly the pictures and words the others were, and runs after any
                model still working here.
              </p>
            </>
          )}
        </div>
      )}

      {manage && panel === "again" && (
        <div id="compare-again" className="card compare-panel">
          {modelsProblem ? (
            <p className="compare-said" data-tone="bad" role="alert">
              {modelsProblem}
            </p>
          ) : !models ? (
            <p className="compare-counting">
              <span className="spinner" aria-hidden="true" /> Reading the installed models…
            </p>
          ) : (
            <>
              <p style={{ marginTop: 0 }}>
                A new comparison on exactly these items, with the question as it is now. This one
                is kept as it is.
              </p>
              {againOffered.length === 0 ? (
                <p className="compare-said" data-tone="warn">
                  None of the installed models can be compared.
                </p>
              ) : (
                <fieldset className="compare-counted">
                  <legend className="compare-label-strong">Which models</legend>
                  {againOffered.map((model) => {
                    const checked = againIds.includes(model.id);
                    const blocker = tickBlocker(model, againIds, maxModels);
                    return (
                      <label key={model.id} className="compare-tick">
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={busy || starting || (!!blocker && !checked)}
                          title={blocker && !checked ? blocker : undefined}
                          onChange={() =>
                            setAgainIds((now) =>
                              now.includes(model.id) ? now.filter((one) => one !== model.id) : [...now, model.id],
                            )
                          }
                        />
                        <span>
                          {model.name} <span className="faint mono">build {model.revision_short}</span>
                        </span>
                      </label>
                    );
                  })}
                </fieldset>
              )}
              {againProblem && (
                <div className="card" role="alert" style={{ borderColor: "var(--ember)", margin: "10px 0 0" }}>
                  {againProblem}
                </div>
              )}
              <div className="compare-start">
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={busy || starting || !againIds.length}
                  title={!againIds.length ? "Tick at least one model." : undefined}
                  onClick={() => void compareAgain()}
                >
                  {starting ? "Starting…" : "Start"}
                </button>
                {!againIds.length && <span className="faint">Tick at least one model.</span>}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
