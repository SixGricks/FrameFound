"use client";

// Software that runs models: one row per kind of program FrameFound can run
// models with (MLX-VLM, and llama.cpp for GGUF builds), what is installed,
// whether a newer one is out, and Install / Update / Roll back. Every row is
// the same component with the same buttons; what differs is said in the
// server's words (its version hint, its update note) or by a pure helper.
//
// An install takes minutes and runs on the Mac's own work list, so the row
// shows the server's job — leave the page and come back, and it is still
// there. It moves no bytes FrameFound can count, so it gets a spinner and the
// job's own sentence rather than an invented bar. A failed install leaves the
// version in use alone; the row says so and shows the last lines the
// installer printed, because "it failed" alone gives the owner nothing to
// act on.

import { useId, useRef, useState } from "react";

import { api, ApiError, type LocalModelRuntime, type LocalModelsOverview } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import {
  aheadOf,
  checkTitle,
  checkedSentence,
  dailyBuildSentence,
  failedInstallSentence,
  focusIfLost,
  installStartedSentence,
  installedText,
  isVersionRefusal,
  returnQuestion,
  rollbackQuestion,
  runtimeNeeded,
  runtimePlan,
  softwareNotes,
  updateQuestion,
  versionPlaceholder,
  versionQuestion,
  whenText,
  type ModelsAct,
} from "@/lib/localModels";
import ModelsProgress from "@/components/ModelsProgress";

export default function ModelsRuntimes({
  overview,
  act,
  busy,
}: {
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
}) {
  return (
    <div className="card" style={{ display: "grid", gap: 18 }}>
      {overview.runtimes.length ? (
        overview.runtimes.map((runtime, index) => (
          <RuntimeRow
            key={runtime.name}
            runtime={runtime}
            first={index === 0}
            overview={overview}
            act={act}
            busy={busy}
          />
        ))
      ) : (
        <p style={{ margin: 0 }}>FrameFound has no software that runs models on this machine.</p>
      )}
      {softwareNotes(overview.runtimes).map((line) => (
        <p key={line} className="faint" style={{ margin: 0, fontSize: "0.86rem" }}>
          {line}
        </p>
      ))}
    </div>
  );
}

function RuntimeRow({
  runtime,
  first,
  overview,
  act,
  busy,
}: {
  runtime: LocalModelRuntime;
  /** The program Getting started installs: never "nothing needs it". */
  first: boolean;
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
}) {
  const [version, setVersion] = useState("");
  // The server's refusal of the version typed ("That is not a llama.cpp
  // version…", "0.9.0 is already the version in use."), said beside the field
  // it is about rather than at the top of the section, away from it.
  const [versionProblem, setVersionProblem] = useState<string | null>(null);
  const problemId = useId();
  const versionField = useRef<HTMLInputElement>(null);
  const plan = runtimePlan(runtime, overview.jobs);
  const manage = overview.can_manage && runtime.supported;
  const working = !!plan.working;
  const name = runtime.name;
  const label = runtime.label;

  function install(chosen: string | null, question: string | null) {
    if (question && !window.confirm(question)) return Promise.resolve(false);
    return act("software", () => api.installRuntime(name, chosen), installStartedSentence);
  }

  function rollBack(to: string) {
    const question = plan.rollbackIsNewer ? returnQuestion(to) : rollbackQuestion(to);
    if (!window.confirm(question)) return;
    act("software", () => api.rollbackRuntime(name), `${label} is back to ${to}.`);
  }

  function installParticular(event: React.FormEvent) {
    event.preventDefault();
    const chosen = version.trim();
    if (!chosen) return;
    const question = versionQuestion(runtime, chosen);
    if (question && !window.confirm(question)) return;
    setVersionProblem(null);
    let refused: string | null = null;
    act(
      "software",
      () =>
        api.installRuntime(name, chosen).catch((err: unknown) => {
          // A refusal of what was typed is the field's, not the section's:
          // answered here, so act() has no alert of its own to show. Every
          // other refusal (being installed already, the installer missing)
          // is not about the version and goes to the section as before.
          if (!(err instanceof ApiError && isVersionRefusal(err.status, err.message))) throw err;
          refused = err.message || "That version cannot be installed.";
          return null;
        }),
      (answer) => (answer ? installStartedSentence(answer) : null),
    ).then((worked) => {
      if (refused) {
        setVersionProblem(refused);
        // Install was disabled while the request was out, which drops the
        // keyboard to the page (or leaves it on Install); it goes back to the
        // field to be corrected — unless the owner has moved elsewhere.
        const input = versionField.current;
        if (input?.form && input.form.contains(document.activeElement)) input.focus();
        else focusIfLost(input);
      } else if (worked) setVersion("");
    });
  }

  const waitTitle = working ? `${label} is being installed; this waits until it has finished.` : undefined;
  const needed = runtimeNeeded(runtime, first);
  const daily = dailyBuildSentence(runtime);

  return (
    <div className="models-runtime">
      <p style={{ margin: 0 }}>
        <strong>{label}</strong> — {runtime.summary}{" "}
        {runtime.demo && <span className="pill">demo</span>}
      </p>

      {!runtime.supported ? (
        <p style={{ margin: "6px 0 0" }}>{runtime.unsupported_reason}</p>
      ) : (
        <dl className="kv models-runtime-facts">
          <dt>Installed</dt>
          <dd>
            {installedText(runtime)}
            {/* Said in brackets above when it is about the version in use. */}
            {runtime.about && runtime.about !== runtime.installed_detail && (
              <span className="faint" style={{ display: "block", fontSize: "0.84rem" }}>
                {runtime.about}
              </span>
            )}
            {needed && (
              <span className="faint" style={{ display: "block", fontSize: "0.84rem" }}>
                {needed}
              </span>
            )}
          </dd>
          <dt>Newest</dt>
          <dd>
            <span className="models-runtime-newest">
              <span>
                {runtime.latest_problem ? (
                  <>
                    could not be checked{" "}
                    <span className="faint" style={{ display: "block", fontSize: "0.84rem" }}>
                      {runtime.latest_problem}
                    </span>
                  </>
                ) : runtime.latest ? (
                  `${runtime.latest}${runtime.latest_checked_at ? ` · checked ${relativeTime(runtime.latest_checked_at)}` : ""}`
                ) : (
                  "not checked yet"
                )}
              </span>
              {overview.can_manage && (
                <button
                  type="button"
                  className="btn"
                  style={{ padding: "3px 10px" }}
                  disabled={busy}
                  title={checkTitle(runtime)}
                  onClick={() => act("software", () => api.checkRuntime(name), checkedSentence)}
                >
                  Check now
                </button>
              )}
            </span>
          </dd>
        </dl>
      )}

      {runtime.problem && (
        <p style={{ margin: "8px 0 0", color: "var(--ember)" }}>{runtime.problem}</p>
      )}

      {plan.working && (
        <ModelsProgress
          job={plan.working}
          ahead={aheadOf(plan.working, overview.jobs)}
          where="software"
          act={act}
          canManage={overview.can_manage}
          busy={busy}
        />
      )}

      {plan.failed && (
        <div style={{ marginTop: 8 }}>
          <p style={{ margin: 0, color: "var(--ember)" }}>
            {failedInstallSentence(plan.failed, runtime)}{" "}
            {plan.failed.finished_at && (
              <span className="faint">({whenText(plan.failed.finished_at)})</span>
            )}
          </p>
          {plan.failed.detail && (
            <pre className="pathbox models-lastlines" aria-label="The last lines the installer printed">
              {plan.failed.detail}
            </pre>
          )}
        </div>
      )}

      {manage && (
        <div className="models-actions">
          {plan.retry && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || working}
              title={waitTitle ?? `Install ${label} ${plan.retry} again`}
              onClick={() => install(plan.retry, plan.retry ? versionQuestion(runtime, plan.retry) : null)}
            >
              Try again
            </button>
          )}
          {plan.install && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || working}
              title={waitTitle ?? `Install the newest ${label}; it needs the internet`}
              onClick={() => install(null, null)}
            >
              Install
            </button>
          )}
          {plan.update && (
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy || working}
              title={waitTitle ?? `Replace ${runtime.installed} with ${plan.update}`}
              onClick={() => {
                if (plan.update) install(plan.update, updateQuestion(runtime, plan.update));
              }}
            >
              Update to {plan.update}
            </button>
          )}
          {plan.rollback && (
            <button
              type="button"
              className="btn"
              disabled={busy || working}
              title={
                waitTitle ??
                (plan.rollbackIsNewer
                  ? `Go back to ${plan.rollback}, still on this Mac from before the roll back`
                  : `Go back to ${plan.rollback}, the version kept from before`)
              }
              onClick={() => {
                if (plan.rollback) rollBack(plan.rollback);
              }}
            >
              {plan.rollbackIsNewer ? "Go back to" : "Roll back to"} {plan.rollback}
            </button>
          )}
        </div>
      )}

      {manage && daily && (
        <p className="faint" style={{ margin: "8px 0 0", fontSize: "0.86rem" }}>
          {daily}
        </p>
      )}

      {manage && (
        <details className="disclosure" style={{ marginTop: 10 }}>
          <summary>Install a particular version</summary>
          <form className="models-version" onSubmit={installParticular}>
            <label className="field" style={{ margin: 0, flex: "1 1 16em" }}>
              Version
              <input
                ref={versionField}
                className="input"
                value={version}
                placeholder={versionPlaceholder(runtime)}
                autoComplete="off"
                spellCheck={false}
                aria-invalid={versionProblem ? true : undefined}
                aria-describedby={versionProblem ? problemId : undefined}
                onChange={(event) => {
                  setVersion(event.target.value);
                  setVersionProblem(null);
                }}
              />
            </label>
            <button
              type="submit"
              className="btn"
              disabled={busy || working || !version.trim()}
              title={waitTitle ?? `Install exactly this version of ${label}`}
            >
              Install
            </button>
          </form>
          {versionProblem && (
            <p id={problemId} role="alert" className="models-row-sentence" data-tone="bad">
              {versionProblem}
            </p>
          )}
          <p className="faint" style={{ margin: "6px 0 0", fontSize: "0.84rem" }}>
            {runtime.update_note ||
              "The way back when the newest version does not work and no older one is kept."}
          </p>
        </details>
      )}
    </div>
  );
}
