"use client";

// Installed models: one row per build, with what it is, what it costs in
// memory, what runs it and which role it has — and what can be done with it.
//
// Every action is a visible button (nothing hides behind a hover), and a
// button that cannot be pressed now stays on screen, disabled, with the
// reason as its title: a Remove that vanished while a model has a role would
// leave the owner wondering where it went. A row with work under way shows
// that work in place of its buttons, the same bar as everywhere else, read
// from the server, so leaving the page and coming back finds it still there.
//
// The model's name always opens its build on Hugging Face, and Details
// (where its files are, what it was built from) is there in every state.
//
// Test opens its panel under the row (ModelsTest). The panel opens by itself
// while a test waits or runs and for a while after it ends — a test started
// from Getting started is found here too — and the owner's own Show/Hide
// wins over that from then on.

import { Fragment, useEffect, useState, type ReactNode } from "react";
import Link from "next/link";

import { api, type LocalModel, type LocalModelsOverview } from "@/lib/api";
import {
  NOT_COMPARED,
  addedText,
  aheadOf,
  buildPartsText,
  builtFromText,
  byLine,
  compareHref,
  dayText,
  downloadBlocker,
  fitsText,
  focusIfLost,
  isActiveTest,
  gbText,
  hfLink,
  licenceText,
  memoryText,
  notCompared,
  notComparedTitle,
  removeBlocker,
  removeQuestion,
  removedSentence,
  roleLabels,
  rowAction,
  rowName,
  runsOnText,
  takesText,
  testBlocker,
  testOpenByDefault,
  testTitle,
  whereText,
  type ModelsAct,
} from "@/lib/localModels";
import ModelsProgress from "@/components/ModelsProgress";
import ModelsTest from "@/components/ModelsTest";

const COLUMNS = 8;

export default function ModelsInstalled({
  overview,
  act,
  busy,
}: {
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
}) {
  const [open, setOpen] = useState<string[]>([]);
  // The owner's own Show/Hide of a test, by the test's job: a new test of the
  // same model starts from the default again.
  const [testShown, setTestShown] = useState<Record<string, boolean>>({});
  // A removed model's row goes, and its Remove button with it: the keyboard
  // goes to the section's heading, not back to the top of the page.
  const [removed, setRemoved] = useState(0);
  const { library, models } = overview;

  useEffect(() => {
    if (removed) focusIfLost(document.querySelector<HTMLElement>("#models h2"));
  }, [removed]);

  function toggle(id: string) {
    setOpen((now) => (now.includes(id) ? now.filter((other) => other !== id) : [...now, id]));
  }

  function testOpen(model: LocalModel): boolean {
    const test = model.test;
    if (!test) return false;
    return testShown[test.job_id] ?? testOpenByDefault(test);
  }

  function showTest(jobId: string, shown: boolean) {
    setTestShown((now) => ({ ...now, [jobId]: shown }));
  }

  return (
    <>
      {library && !library.connected && (
        <div className="card" role="status" style={{ borderColor: "var(--amber)", marginBottom: 12 }}>
          <p style={{ margin: 0 }}>
            The model library is not connected. Models that have a role are on this Mac and keep
            working; adding, removing and giving a role to another model wait until it is back.
          </p>
          {library.problem && (
            <p className="faint" style={{ margin: "6px 0 0", fontSize: "0.86rem" }}>
              {library.problem}
            </p>
          )}
          <p style={{ margin: "6px 0 0", fontSize: "0.86rem" }}>
            <Link href="/storage">Storage</Link> shows where the library is and every other store.
          </p>
        </div>
      )}

      {!models.length ? (
        <div className="empty">No models yet. Add one below.</div>
      ) : (
        <div className="tablewrap card" style={{ padding: 0 }}>
          <table className="table models-table">
            <thead>
              <tr>
                <th>Model</th>
                <th>Licence</th>
                <th>Size</th>
                <th>Memory</th>
                <th>Takes</th>
                <th>Runs on</th>
                <th>Role</th>
                <th>
                  <span className="faint">What you can do</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {models.map((model) => (
                <Fragment key={model.id}>
                  <Row
                    model={model}
                    overview={overview}
                    act={act}
                    busy={busy}
                    detailsOpen={open.includes(model.id)}
                    onDetails={() => toggle(model.id)}
                    testOpen={testOpen(model)}
                    onTestShown={showTest}
                    onRemoved={() => setRemoved((count) => count + 1)}
                  />
                  {testOpen(model) && (
                    <tr className="models-detailrow">
                      <td colSpan={COLUMNS} id={`model-test-${model.id}`}>
                        <ModelsTest model={model} overview={overview} act={act} busy={busy} />
                      </td>
                    </tr>
                  )}
                  {open.includes(model.id) && (
                    <tr className="models-detailrow">
                      <td colSpan={COLUMNS} id={`model-details-${model.id}`}>
                        <Details model={model} overview={overview} />
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function Row({
  model,
  overview,
  act,
  busy,
  detailsOpen,
  onDetails,
  testOpen,
  onTestShown,
  onRemoved,
}: {
  model: LocalModel;
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
  detailsOpen: boolean;
  onDetails: () => void;
  testOpen: boolean;
  onTestShown: (jobId: string, shown: boolean) => void;
  onRemoved: () => void;
}) {
  const manage = overview.can_manage;
  const fits = fitsText(model.fits, overview.settings);
  const licence = licenceText(model);
  const runsOn = runsOnText(model);
  const roles = roleLabels(model, overview.roles);
  const state = rowAction(model);
  const away = downloadBlocker(overview.library);
  const holdRemove = removeBlocker(model, overview.roles, overview.library);
  const testing = isActiveTest(model.test);
  const holdTest = testBlocker(model, overview.settings);
  // A llama.cpp build nobody has seen answer on this footage: giving it a
  // role asks first, and Compare is the way to stop asking.
  const uncompared = notCompared(model);

  function resume(again: boolean) {
    act("models", () => api.resumeModel(model.id), (resumed) =>
      again
        ? `Downloading ${rowName(resumed)} again. You can leave this page; the download carries on.`
        : `Resuming ${rowName(resumed)}. You can leave this page; the download carries on.`,
    );
  }

  function remove() {
    if (!window.confirm(removeQuestion(model))) return;
    act("models", () => api.removeModel(model.id), (removed) =>
      removedSentence(rowName(model), removed.freed_bytes),
    ).then((worked) => {
      if (worked) onRemoved();
    });
  }

  const resumeButton = (again: boolean) =>
    manage && (
      <button
        type="button"
        className="btn"
        style={{ padding: "3px 10px" }}
        disabled={busy || !!away}
        title={away ?? undefined}
        onClick={() => resume(again)}
      >
        {again ? "Download again" : "Resume"}
      </button>
    );

  function test() {
    act("models", () => api.testModel(model.id), (started) => {
      onTestShown(started.job_id, true);
      return null;
    });
  }

  // Shown while a test is under way too, as "Testing…", so the button does
  // not jump away from under the pointer the moment it is pressed.
  const testButton = manage && (
    <button
      type="button"
      className="btn"
      style={{ padding: "3px 10px" }}
      disabled={busy || !!holdTest}
      title={holdTest ?? testTitle(model)}
      onClick={test}
    >
      {testing ? "Testing…" : "Test"}
    </button>
  );

  // A link, not a button: Compare is its own page, and anyone may look there.
  // Compare looks at pictures; a model that reads none is cleared by its Test.
  const compareLink = uncompared && model.takes.includes("image") && (
    <Link
      className="btn"
      style={{ padding: "3px 10px" }}
      href={compareHref(model.id)}
      title={`Run ${rowName(model)} on a sample of your footage beside other models`}
    >
      Compare
    </Link>
  );

  const removeButton = manage && (
    <button
      type="button"
      className="btn"
      style={{ padding: "3px 10px" }}
      disabled={busy || !!holdRemove}
      title={holdRemove ?? `Remove ${rowName(model)} and its files`}
      onClick={remove}
    >
      Remove
    </button>
  );

  const detailsButton = (
    <button
      type="button"
      className="linkish"
      aria-expanded={detailsOpen}
      aria-controls={`model-details-${model.id}`}
      onClick={onDetails}
    >
      {detailsOpen ? "Hide details" : "Details"}
    </button>
  );

  const shownTest = model.test;
  const testToggle = shownTest && (
    <button
      type="button"
      className="linkish"
      aria-expanded={testOpen}
      aria-controls={`model-test-${model.id}`}
      onClick={() => onTestShown(shownTest.job_id, !testOpen)}
    >
      {testOpen ? "Hide test" : testing ? "Show test" : "Last test"}
    </button>
  );

  let actions: ReactNode;
  switch (state) {
    case "job":
      actions = model.job && (
        <ModelsProgress
          job={model.job}
          ahead={aheadOf(model.job, overview.jobs)}
          where="models"
          act={act}
          canManage={manage}
          busy={busy}
        />
      );
      break;
    case "incomplete":
    case "stalled":
      actions = (
        <>
          <p className="models-row-sentence" data-tone="bad">
            {model.error ?? (state === "stalled" ? "Its download is not running." : "Its download did not finish.")}
          </p>
          <span className="models-actions">
            {resumeButton(false)}
            {removeButton}
          </span>
        </>
      );
      break;
    case "missing":
      actions = (
        <>
          <p className="models-row-sentence" data-tone="bad">
            Its files are not in the library.
          </p>
          <span className="models-actions">
            {resumeButton(true)}
            {removeButton}
          </span>
        </>
      );
      break;
    case "installed":
      actions = (
        <span className="models-actions">
          {testButton}
          {compareLink}
          {removeButton}
        </span>
      );
      break;
  }

  return (
    <tr>
      <td style={{ minWidth: 220 }}>
        <a href={model.url} target="_blank" rel="noreferrer" title="This build on Hugging Face">
          {model.name}
        </a>
        <div className="faint" style={{ fontSize: "0.8rem" }}>
          {byLine(model)}
        </div>
        <div className="models-tile-pills">
          {model.demo && <span className="pill">demo</span>}
          {model.gated && (
            <span className="pill" title="Its maker asks you to accept their terms first">
              gated
            </span>
          )}
          {uncompared && (
            <span className="pill" data-tone="warn" title={notComparedTitle(model)}>
              {NOT_COMPARED}
            </span>
          )}
          {!model.approved && (
            <span className="pill" data-tone="bad">
              no longer approved
            </span>
          )}
        </div>
        {!model.approved && (
          <p className="models-row-sentence" data-tone="bad">
            {model.approval_problem}{" "}
            {/* The way out — put the entry back — is in another section. */}
            <a href="#publishers">Approved publishers</a>
          </p>
        )}
      </td>
      <td>
        {licence.missing ? (
          <span className="pill" data-tone="warn" title="Read the model's page before using it for client work">
            none stated
          </span>
        ) : licence.url ? (
          <a href={licence.url} target="_blank" rel="noreferrer">
            {licence.text}
          </a>
        ) : (
          licence.text
        )}
      </td>
      <td className="mono" style={{ whiteSpace: "nowrap" }}>
        {gbText(model.size_bytes)}
      </td>
      <td style={{ minWidth: 120 }}>
        <span className="mono" style={{ whiteSpace: "nowrap" }}>
          {memoryText(model)}
        </span>
        <div className="models-tile-pills">
          <span className="pill" data-tone={fits.tone} title={fits.sentence}>
            {fits.label}
          </span>
        </div>
      </td>
      <td title="As its page says">{takesText(model.takes)}</td>
      <td style={{ minWidth: 120 }}>
        {runsOn.label}
        {runsOn.pill && (
          <div className="models-tile-pills">
            <span className="pill" data-tone={runsOn.pill.tone}>
              {runsOn.pill.text}
            </span>
          </div>
        )}
        {runsOn.sentence && (
          <p className="models-row-sentence" data-tone={runsOn.pill?.tone === "bad" ? "bad" : undefined}>
            {runsOn.sentence}
          </p>
        )}
      </td>
      <td>
        {roles.length ? (
          <div className="models-tile-pills" style={{ marginTop: 0 }}>
            {roles.map((label) => (
              <span className="pill" key={label} data-tone={label.startsWith("→") ? undefined : "ok"}>
                {label}
              </span>
            ))}
          </div>
        ) : (
          <span className="faint">—</span>
        )}
      </td>
      <td style={{ minWidth: 230 }}>
        {actions}
        <div className="models-actions" style={{ gap: "4px 12px" }}>
          {detailsButton}
          {testToggle}
        </div>
      </td>
    </tr>
  );
}

function Details({ model, overview }: { model: LocalModel; overview: LocalModelsOverview }) {
  const licence = licenceText(model);
  const day = dayText(model.build_date);
  // One build of a repository that holds several: which, and what came with it.
  const parts = buildPartsText(model);
  return (
    <dl className="kv models-details">
      <dt>Where</dt>
      <dd>{whereText(model, overview.library)}</dd>

      <dt>Build</dt>
      <dd>
        <a href={model.url} target="_blank" rel="noreferrer" className="mono" style={{ overflowWrap: "anywhere" }}>
          {model.revision}
        </a>
        {day && ` · made ${day}`}
      </dd>

      {parts && (
        <>
          <dt>Which build</dt>
          <dd>{parts}</dd>
        </>
      )}

      <dt>Built from</dt>
      <dd>
        {model.built_from.length ? (
          <ul className="models-chain">
            {model.built_from.map((entry) => (
              <li key={entry.repo_id}>
                {entry.found ? (
                  <a href={hfLink(entry.repo_id)} target="_blank" rel="noreferrer">
                    {builtFromText(entry)}
                  </a>
                ) : (
                  builtFromText(entry)
                )}
              </li>
            ))}
          </ul>
        ) : (
          "Nothing — its page names no other model."
        )}
      </dd>

      <dt>Memory</dt>
      <dd>
        {memoryText(model)}. {fitsText(model.fits, overview.settings).sentence}
      </dd>

      <dt>Licence</dt>
      <dd>
        {licence.missing
          ? "Its page states no licence. Read the model's page before using it for client work."
          : licence.text}
      </dd>

      {!model.approved && (
        <>
          <dt>Approval</dt>
          <dd>{model.approval_problem ?? "No longer approved."}</dd>
        </>
      )}

      <dt>Added</dt>
      <dd>
        {addedText(model)}
      </dd>
    </dl>
  );
}
