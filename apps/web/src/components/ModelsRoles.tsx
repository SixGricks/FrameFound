"use client";

// Who does what: the Looker and the Editor, and which model has each.
//
// Giving a role usually copies the model to this Mac first, so it loads
// quickly from local disk instead of the NAS. Until that copy is whole the
// old model keeps the role — the card says so under the bar — because a
// role that went empty for the length of a 5 GB copy would stop work that
// was already relying on it.
//
// The list of models to give the role to holds every model: one that cannot
// take this role stays in the list, disabled, with the server's reason in
// its text, so a model never seems to have vanished from the choice.

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { api, type LocalModel, type LocalModelRole, type LocalModelsOverview } from "@/lib/api";
import {
  NOTHING_CHANGED,
  NOT_COMPARED,
  aheadOf,
  assignBlocker,
  firstUsable,
  fitsText,
  focusIfLost,
  givenUncompared,
  isRoleName,
  notComparedTitle,
  roleGivenSentence,
  roleOptions,
  roleQuestion,
  roleTakeQuestion,
  rolesNote,
  rowName,
  wayOut,
  wayOutForViewer,
  type ModelsAct,
} from "@/lib/localModels";
import ModelsProgress from "@/components/ModelsProgress";

const NOT_HERE = "Not on this Mac.";

/** The two presses that give a role: Assign, and the holder's Copy again. */
type Pressed = "assign" | "copy";

export default function ModelsRoles({
  overview,
  act,
  busy,
}: {
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
}) {
  return (
    <>
      <div className="rolecards">
        {overview.roles.map((role) => (
          <RoleCard key={role.role} role={role} overview={overview} act={act} busy={busy} />
        ))}
      </div>
      <p className="faint" style={{ margin: "12px 0 0", fontSize: "0.86rem" }}>
        {rolesNote(overview)}
      </p>
    </>
  );
}

function RoleCard({
  role,
  overview,
  act,
  busy,
}: {
  role: LocalModelRole;
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
}) {
  const [chosenId, setChosenId] = useState("");
  // The model the owner just declined to give the role to without Compare:
  // the card offers Compare instead, until another model is chosen.
  const [declinedId, setDeclinedId] = useState<string | null>(null);
  // Where the keyboard goes once a press here settles. Every control is
  // disabled while a request is out, and Assign stays disabled after a role
  // was given (nothing is chosen any more), so focus would otherwise fall
  // back to the top of a long page.
  const [settled, setSettled] = useState<{ to: Pressed | "select"; n: number }>({ to: "select", n: 0 });
  const cardRef = useRef<HTMLDivElement>(null);
  const selectRef = useRef<HTMLSelectElement>(null);
  const assignRef = useRef<HTMLButtonElement>(null);
  const copyRef = useRef<HTMLButtonElement>(null);
  const manage = overview.can_manage && isRoleName(role.role);
  const holder = overview.models.find((model) => model.id === role.model_id) ?? null;
  const options = roleOptions(overview.models, role);
  const chosen = overview.models.find((model) => model.id === chosenId) ?? null;
  // A choice that can no longer hold the role (it was removed, or the server
  // now refuses it) is not sent: the list shows why.
  const choice = options.find((option) => option.id === chosenId && !option.disabled) ? chosen : null;
  const blocked = assignBlocker(role, choice, overview.library);
  const copying = role.job && role.pending_model_id;
  const declined = declinedId === chosenId ? chosen : null;
  const pending = overview.models.find((model) => model.id === role.pending_model_id) ?? null;
  const pendingName = pending ? rowName(pending) : role.pending_model_name;
  const holderName = holder ? rowName(holder) : role.model_name;

  // Once per press, after the page is no longer busy: `busy` also changes for
  // the other card's presses, and those are not this card's to answer.
  const placed = useRef(0);
  useEffect(() => {
    if (settled.n === placed.current || busy) return;
    placed.current = settled.n;
    // Back to the button pressed when nothing changed — unless it cannot be
    // pressed now: a refused choice is disabled once the page reads the
    // server again, and Copy again leaves nothing chosen, so Assign is
    // disabled. Focus sent to a disabled button goes nowhere; the list
    // (or, failing that, the card) takes it instead.
    const pressed = settled.to === "assign" ? assignRef.current : settled.to === "copy" ? copyRef.current : null;
    const target = firstUsable<HTMLElement>(pressed, selectRef.current) ?? cardRef.current;
    // A control of this card disabled under the keyboard still reads as
    // focused for a moment before the browser drops focus to the page; that
    // is lost focus too.
    const active = document.activeElement;
    if (active instanceof HTMLElement && cardRef.current?.contains(active) && !firstUsable(active)) active.blur();
    focusIfLost(target);
  }, [settled, busy]);

  function settle(to: Pressed | "select") {
    setSettled((now) => ({ to, n: now.n + 1 }));
  }

  // A llama.cpp model not yet compared for this role makes the server ask
  // first (428): the page's act() puts its question, and a yes is the same
  // request with `skip_compare`. A no changes nothing, and the card offers
  // Compare instead.
  function give(model: LocalModel, from: Pressed) {
    if (!isRoleName(role.role)) return;
    const name = role.role;
    setDeclinedId(null);
    act(
      "roles",
      (yes) => api.setModelRole(name, model.id, yes),
      (answer) => roleGivenSentence(answer, rowName(model), overview.models),
      () => setDeclinedId(model.id),
    ).then((worked) => {
      if (worked) setChosenId("");
      settle(worked ? "select" : from);
    });
  }

  function assign() {
    if (!choice) return;
    const question = roleQuestion(role, choice, overview.models);
    if (question && !window.confirm(question)) return;
    give(choice, "assign");
  }

  function takeAway() {
    if (!isRoleName(role.role)) return;
    if (!window.confirm(roleTakeQuestion(role, overview.library, overview.models))) return;
    const name = role.role;
    setDeclinedId(null);
    act("roles", () => api.setModelRole(name, null), `No model is the ${role.title} now.`).then(() =>
      settle("select"),
    );
  }

  return (
    <div className="card" ref={cardRef}>
      <h3 style={{ margin: 0 }}>{role.title}</h3>
      <p className="faint" style={{ margin: "4px 0 10px", fontSize: "0.88rem" }}>
        {role.description}
        {/* Where the Editor's work is asked for, so this card is not a dead
            end: the Cuts page's form. Shown whoever holds the role, or none —
            the form says what is missing. */}
        {role.role === "editor" && (
          <>
            {" "}
            <Link href="/cuts">Draft a cut →</Link>
          </>
        )}
      </p>

      {holder ? (
        <Holder model={holder} role={role} overview={overview} manage={manage} />
      ) : (
        <p style={{ margin: 0 }}>{role.model_name ?? "No model has this role yet."}</p>
      )}

      {role.problem && (
        <p
          style={{
            margin: "6px 0 0",
            fontSize: "0.86rem",
            color: role.ready ? "var(--amber)" : "var(--ember)",
          }}
        >
          {role.problem}
          {holder && !holder.approved && (
            <>
              {" "}
              <a href="#publishers">Approved publishers</a>
            </>
          )}
        </p>
      )}
      {manage && holder && role.problem === NOT_HERE && !copying && (
        <div className="models-actions">
          <button
            ref={copyRef}
            type="button"
            className="btn"
            style={{ padding: "3px 10px" }}
            disabled={busy || !!assignBlocker(role, holder, overview.library)}
            title={
              assignBlocker(role, holder, overview.library) ??
              `Copy ${rowName(holder)} to this Mac again so it can do the ${role.title}'s work`
            }
            onClick={() => give(holder, "copy")}
          >
            Copy again
          </button>
        </div>
      )}

      {role.job && (
        <ModelsProgress
          job={role.job}
          ahead={aheadOf(role.job, overview.jobs)}
          where="roles"
          act={act}
          canManage={overview.can_manage}
          busy={busy}
        />
      )}
      {copying && pendingName && role.pending_model_id !== role.model_id && (
        <p className="faint" style={{ margin: "6px 0 0", fontSize: "0.84rem" }}>
          {holderName
            ? `${holderName} is still the ${role.title} until this finishes; then ${pendingName} takes over.`
            : `${pendingName} becomes the ${role.title} when this finishes.`}
        </p>
      )}

      {manage && (
        <div className="models-role-give">
          <label className="field" style={{ flex: "1 1 220px", minWidth: 0 }}>
            Give this role to…
            <select
              ref={selectRef}
              className="select"
              value={choice ? chosenId : ""}
              disabled={busy || !options.length}
              onChange={(event) => {
                setChosenId(event.target.value);
                setDeclinedId(null);
              }}
            >
              <option value="">
                {options.length ? "Choose a model" : "No models yet — add one below"}
              </option>
              {options.map((option) => (
                <option key={option.id} value={option.id} disabled={option.disabled}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <button
            ref={assignRef}
            type="button"
            className="btn btn-primary"
            disabled={busy || !choice || !!blocked}
            title={blocked ?? (choice ? `Make ${rowName(choice)} the ${role.title}` : "Choose a model first")}
            onClick={assign}
          >
            Assign
          </button>
          {(role.model_id || role.pending_model_id) && (
            <button
              type="button"
              className="btn"
              disabled={busy}
              title={`Leave the ${role.title} role empty`}
              onClick={takeAway}
            >
              Take the role away
            </button>
          )}
        </div>
      )}
      {manage && blocked && choice && (
        <p className="faint" style={{ margin: "6px 0 0", fontSize: "0.84rem" }}>
          {blocked}
        </p>
      )}
      {/* Present before it has anything to say, so the line is announced. */}
      {manage && (
        <div role="status">
          {declined && (
            <p style={{ margin: "6px 0 0", fontSize: "0.86rem" }}>
              {NOTHING_CHANGED} <WayOut model={declined} first />
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function Holder({
  model,
  role,
  overview,
  manage,
}: {
  model: LocalModel;
  role: LocalModelRole;
  overview: LocalModelsOverview;
  manage: boolean;
}) {
  const fits = fitsText(model.fits, overview.settings);
  // Given the role after the Compare question was answered yes; both go by
  // themselves once a Compare run of it finishes (the server judges it).
  const uncompared = role.compared === false;
  return (
    <div>
      <a href={model.url} target="_blank" rel="noreferrer" title="This build on Hugging Face">
        {rowName(model)}
      </a>
      <div className="models-tile-pills">
        {model.demo && <span className="pill">demo</span>}
        {uncompared && (
          <span className="pill" data-tone="warn" title={notComparedTitle(model)}>
            {NOT_COMPARED}
          </span>
        )}
        {model.fits !== "day" && (
          <span className="pill" data-tone={fits.tone} title={fits.sentence}>
            {fits.label}
          </span>
        )}
        {!model.approved && (
          <span className="pill" data-tone="bad">
            no longer approved
          </span>
        )}
      </div>
      {uncompared && (
        <p className="faint" style={{ margin: "6px 0 0", fontSize: "0.84rem" }}>
          {givenUncompared(model)} {manage ? <WayOut model={model} /> : wayOutForViewer(model)}
        </p>
      )}
    </div>
  );
}

/** Compare (its own page, the model ticked), or Test in the model's row for
 *  one that reads no pictures. */
function WayOut({ model, first = false }: { model: LocalModel; first?: boolean }) {
  const way = wayOut(model, first);
  return way.href.startsWith("#") ? <a href={way.href}>{way.label}</a> : <Link href={way.href}>{way.label}</Link>;
}
