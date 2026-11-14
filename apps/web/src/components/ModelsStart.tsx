"use client";

// The top of the AI models page: Getting started, then Right now.
//
// Getting started is four lines read off the server's answer — never off
// anything the tab remembers — so a download started yesterday, or from
// another browser, ticks its line all the same. Once all four are done the
// list folds into one line: an owner who has set it up does not need the
// checklist every visit, and the tiles below say what matters day to day.
//
// Right now is four tiles: who has each role, what the model server is
// doing, and how much memory a model may take now and later. Pause is in
// its heading because it is the one control that affects everything else on
// the page.

import { api, type LocalModelRole, type LocalModelsOverview } from "@/lib/api";
import {
  aheadOf,
  memoryLine,
  rowName,
  serverLine,
  startSteps,
  testTitle,
  type ModelsAct,
  type StartStep,
} from "@/lib/localModels";
import ModelsProgress from "@/components/ModelsProgress";

const PAUSE_QUESTION =
  "Pause local AI? The model in memory is unloaded and no model runs until you resume. " +
  "Downloads carry on.";

export default function ModelsStart({
  overview,
  act,
  busy,
  onChooseModel,
}: {
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
  /** "Choose one" jumps to Add a model; this has the suggestion filled in there. */
  onChooseModel?: () => void;
}) {
  const plan = startSteps(overview);
  const manage = overview.can_manage;
  const { settings } = overview;
  const server = serverLine(overview.server, settings);

  function action(step: StartStep) {
    if (!manage || step.done || step.job) return null;
    switch (step.key) {
      case "software":
        if (plan.install) {
          const runtime = plan.install;
          return (
            <span className="models-step-action">
              <span className="faint">needs the internet</span>
              <button
                type="button"
                className="btn"
                style={{ padding: "3px 10px" }}
                disabled={busy}
                onClick={() =>
                  act(
                    "start",
                    () => api.installRuntime(runtime.name),
                    `Installing ${runtime.label}. It takes a few minutes; you can leave this page.`,
                  )
                }
              >
                Install {runtime.label}
              </button>
            </span>
          );
        }
        return plan.cannotInstall ? <span className="faint">{plan.cannotInstall}</span> : null;
      case "model":
        return (
          <a className="btn" style={{ padding: "3px 10px" }} href="#add" onClick={onChooseModel}>
            Choose one
          </a>
        );
      case "role":
        return (
          <a className="btn" style={{ padding: "3px 10px" }} href="#roles">
            Choose
          </a>
        );
      case "test": {
        const model = plan.testModel;
        if (!model) return <span className="faint">Add a model first.</span>;
        return (
          <button
            type="button"
            className="btn"
            style={{ padding: "3px 10px" }}
            title={testTitle(model)}
            disabled={busy}
            onClick={() =>
              act(
                "start",
                () => api.testModel(model.id),
                `Testing ${rowName(model)}. Its answer shows under Models when it is ready.`,
              )
            }
          >
            Test
          </button>
        );
      }
    }
  }

  return (
    <>
      {plan.done ? (
        <p className="faint" style={{ margin: "14px 0 0" }}>
          {plan.summary}
        </p>
      ) : (
        <div className="card checkup" style={{ marginTop: 14 }}>
          <p className="eyebrow" style={{ marginTop: 0 }}>
            Getting started
          </p>
          <ul>
            {plan.steps.map((step) => (
              <li key={step.key} className="models-step">
                <span className="checkup-dot" data-tone={step.done ? undefined : "warn"} aria-hidden />
                <span style={{ flex: 1, minWidth: 0 }}>{step.text}</span>
                {step.done ? (
                  <span className="pill" data-tone="ok">
                    done
                  </span>
                ) : (
                  action(step)
                )}
                {/* A line already done is not reopened by later work (a second
                    download); that work shows in its own section. */}
                {step.job && !step.done && (
                  <div className="models-step-job">
                    <ModelsProgress
                      job={step.job}
                      ahead={aheadOf(step.job, overview.jobs)}
                      where="start"
                      act={act}
                      canManage={manage}
                      busy={busy}
                    />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="sectionhead">
        <h2>Right now</h2>
        {manage && (
          <button
            type="button"
            className="btn"
            style={{ marginLeft: "auto", padding: "4px 12px" }}
            disabled={busy}
            onClick={() => {
              if (settings.paused) {
                act(
                  "start",
                  () => api.updateLocalModelSettings({ paused: false }),
                  "Local AI is running again.",
                );
              } else if (window.confirm(PAUSE_QUESTION)) {
                act(
                  "start",
                  () => api.updateLocalModelSettings({ paused: true }),
                  "Local AI is paused.",
                );
              }
            }}
          >
            {settings.paused ? "Resume" : "Pause"}
          </button>
        )}
      </div>
      {settings.paused && (
        <p className="faint" style={{ margin: "0 0 12px" }}>
          Models are unloaded and nothing new starts until you resume. Downloads carry on.
        </p>
      )}

      <div className="statgrid models-tiles">
        {overview.roles.map((role) => (
          <RoleTile key={role.role} role={role} overview={overview} />
        ))}

        <div className="stat">
          <p className="eyebrow">
            Right now
            {overview.mode === "demo" && (
              <span className="pill" style={{ marginLeft: 8 }}>
                demo
              </span>
            )}
          </p>
          <p className="models-tile-text">{server.text}</p>
          {server.detail && (
            <p className="faint" style={{ fontSize: "0.8rem", margin: "4px 0 0" }}>
              {server.detail}
            </p>
          )}
          {manage && server.unload && (
            <div style={{ marginTop: 10 }}>
              <button
                type="button"
                className="btn"
                style={{ padding: "3px 10px" }}
                disabled={busy}
                onClick={() =>
                  act(
                    "start",
                    () => api.unloadModel(),
                    (result) =>
                      result.unloaded ? "Unloaded. Its memory is free." : "Nothing was loaded.",
                  )
                }
              >
                Unload now
              </button>
              <p className="faint" style={{ fontSize: "0.78rem", margin: "4px 0 0" }}>
                Frees the memory now. It loads again when next needed.
              </p>
            </div>
          )}
        </div>

        <div className="stat">
          <p className="eyebrow">Memory</p>
          <p className="models-tile-text">{memoryLine(overview.now, settings)}</p>
          {overview.machine.memory_gb != null && (
            <p className="faint" style={{ fontSize: "0.8rem", margin: "4px 0 0" }}>
              This Mac has {Math.round(overview.machine.memory_gb)} GB.
            </p>
          )}
        </div>
      </div>
    </>
  );
}

function RoleTile({ role, overview }: { role: LocalModelRole; overview: LocalModelsOverview }) {
  const model = overview.models.find((m) => m.id === role.model_id) ?? null;
  // The server marks "only runs overnight" as a problem on a role that is
  // ready: information, not a fault.
  const note = role.ready ? role.problem : null;
  const fault = role.ready ? null : role.problem;
  return (
    <div className="stat">
      <p className="eyebrow">{role.title}</p>
      <p className="models-tile-name">{role.model_name ?? "No model yet"}</p>
      <div className="models-tile-pills">
        {model?.demo && <span className="pill">demo</span>}
        {note && (
          <span className="pill" data-tone="warn" title={note}>
            {note.startsWith("Only runs overnight") ? "overnight only" : note}
          </span>
        )}
        {fault && (
          <span className="pill" data-tone="bad">
            not ready
          </span>
        )}
      </div>
      {fault && (
        <p style={{ color: "var(--ember)", fontSize: "0.82rem", margin: "6px 0 0" }}>{fault}</p>
      )}
      {role.model_id && !role.ready && !role.problem && (
        <p className="faint" style={{ fontSize: "0.8rem", margin: "6px 0 0" }}>
          Where its copy is could not be checked just now.
        </p>
      )}
      {role.pending_model_name && (
        <p className="faint" style={{ fontSize: "0.8rem", margin: "6px 0 0" }}>
          {role.pending_model_name} takes over once its copy to this Mac is whole.
        </p>
      )}
    </div>
  );
}
