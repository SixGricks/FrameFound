"use client";

// Add a model: a name, Check, and — only if the check allows it — Download.
//
// Check only asks Hugging Face; nothing is downloaded until Download is
// pressed, and Download sends back the exact build the check showed, so what
// is downloaded is what was read. Every verdict, row, problem, warning and
// note on the card is the server's own sentence, shown as it comes: the page
// never decides whether a model is acceptable, it only lays out what the
// server found.
//
// The warnings are asked once, by the server (a 428 that `act` turns into a
// confirm()), rather than with a checkbox here, so a warning the page did not
// know about is still asked.

import { useEffect, useId, useRef, useState, type RefObject } from "react";

import {
  ApiError,
  api,
  type LocalModelPreflight,
  type LocalModelsOverview,
  type LocalModelsSettings,
} from "@/lib/api";
import {
  BUILDS_INTRO,
  BUILDS_SHOWN,
  BUILD_RECHECK_MS,
  SUGGESTED_MODEL,
  buildLine,
  buildText,
  buildsInOrder,
  checkName,
  checkToneWord,
  downloadBlocker,
  downloadLabel,
  focusIfLost,
  hfLink,
  otherProblems,
  settler,
  startedSentence,
  type ModelsAct,
} from "@/lib/localModels";

export default function ModelsAdd({
  overview,
  act,
  busy,
  suggest,
}: {
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
  /** Goes up by one each time Getting started's "Choose one" is pressed:
   *  the field is given the suggested model (when it is empty). */
  suggest: number;
}) {
  const [text, setText] = useState("");
  const [check, setCheck] = useState<LocalModelPreflight | null>(null);
  const [checking, setChecking] = useState(false);
  // The build chosen in "Which build" while its check is still out, so the
  // select shows the choice instead of springing back to the old card's.
  const [pendingVariant, setPendingVariant] = useState<string | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  // A failed re-check of a build: said beside the list, the card kept.
  const [buildProblem, setBuildProblem] = useState<string | null>(null);
  // Bumped by every ask and by every edit of the field: an answer whose
  // number is no longer the newest is about a name the field no longer
  // shows (or a build since changed again), and is dropped.
  const asked = useRef(0);
  // The wait before a build chosen in the list is checked, and the check out.
  const settle = useRef(settler(BUILD_RECHECK_MS)).current;
  const inFlight = useRef<AbortController | null>(null);
  // Set when a re-check began with the keyboard inside the card; the card
  // may come back laid out differently (Check again becomes Download).
  const [refocus, setRefocus] = useState(0);
  const field = useRef<HTMLInputElement>(null);
  const verdict = useRef<HTMLParagraphElement>(null);
  const manage = overview.can_manage;
  const away = downloadBlocker(overview.library);

  useEffect(() => {
    if (!suggest) return;
    setText((current) => (current.trim() ? current : SUGGESTED_MODEL));
    // The jump has already put the section in view; focusing must not move it.
    field.current?.focus({ preventScroll: true });
  }, [suggest]);

  useEffect(() => {
    if (refocus) focusIfLost(verdict.current);
  }, [refocus]);

  // Check only reads, so it is sent here rather than through the page's
  // `act`: that would make the whole page busy for as long as Hugging Face
  // takes — a minute on a poor connection — and leave the Cancel of a
  // download filling the disk unpressable meanwhile.
  //
  // `fromList`: a re-check of one build chosen in "Which build". If it fails
  // (Hugging Face busy, a dropped connection) the card stays as it was, with
  // the failure beside the list: the card is still true of the build it
  // shows, and taking it away would take the radio the keyboard is on with it.
  async function ask(name: string, variant: string | null = null, fromList = false) {
    stopAsking();
    const mine = ++asked.current;
    const controller = new AbortController();
    inFlight.current = controller;
    const keyboardInCard = !!check && !!document.activeElement?.closest(".preflight");
    setChecking(true);
    setRefused(null);
    setBuildProblem(null);
    // A re-check of the same model keeps its card on screen (with Download
    // held) so the select or button that was used is not taken away from
    // under the keyboard; a check of a new name clears the old card.
    if (!check || check.repo_id !== name) setCheck(null);
    setPendingVariant(variant);
    try {
      const found = await api.preflightModel(name, variant, controller.signal);
      if (mine !== asked.current) return;
      setCheck(found);
      if (keyboardInCard) setRefocus((count) => count + 1);
    } catch (err) {
      if (mine !== asked.current) return;
      const sentence =
        err instanceof ApiError
          ? err.message
          : "FrameFound could not be reached to ask Hugging Face. Try again.";
      if (fromList && check?.repo_id === name) {
        setBuildProblem(sentence);
      } else {
        setCheck(null);
        setRefused(sentence);
      }
    } finally {
      if (mine === asked.current) {
        inFlight.current = null;
        setChecking(false);
        setPendingVariant(null);
      }
    }
  }

  // A build chosen in the list shows as chosen at once (and Download waits),
  // but is checked only once the choice has settled: see BUILD_RECHECK_MS.
  function chooseBuild(name: string, variant: string) {
    stopAsking();
    asked.current += 1;
    setBuildProblem(null);
    setChecking(true);
    setPendingVariant(variant);
    settle.choose(() => ask(name, variant, true));
  }

  // A check the page no longer wants, waiting or out: the wait is dropped and
  // the request cancelled, so a superseded build costs Hugging Face nothing.
  function stopAsking() {
    settle.stop();
    inFlight.current?.abort();
    inFlight.current = null;
  }

  useEffect(() => {
    const running = inFlight;
    return () => {
      settle.stop();
      running.current?.abort();
    };
  }, [settle]);

  // An edit of the field forgets any check, finished or still out: a card
  // about another name must not sit under this one with a Download button.
  function forget() {
    stopAsking();
    asked.current += 1;
    setChecking(false);
    setPendingVariant(null);
    setCheck(null);
    setRefused(null);
    setBuildProblem(null);
  }

  function download(found: LocalModelPreflight) {
    act(
      "add",
      (acknowledged) =>
        api.addModel(
          { name: found.repo_id, revision: found.revision, variant: found.variant },
          acknowledged,
        ),
      (model) => {
        // The model is in the table now, with its bar; the card has done its job.
        setCheck(null);
        setText("");
        return startedSentence(model);
      },
    ).then((worked) => {
      // The card and its Download button are gone; the keyboard goes back
      // to the field, ready for another name.
      if (worked) focusIfLost(field.current);
    });
  }

  const name = checkName(text);

  return (
    <div className="card">
      {/* A reader has no field to paste into, so the sentence does not tell
          them to paste. */}
      <p style={{ margin: 0 }}>
        {manage
          ? "Paste the name of a model from Hugging Face. FrameFound asks Hugging Face about that name and, if you go ahead, downloads its files."
          : "An administrator adds a model here by its name on Hugging Face; FrameFound asks Hugging Face about it before downloading anything."}{" "}
        Nothing about your footage is sent anywhere: models run on this Mac.
      </p>

      {manage && (
        <form
          className="toolbar"
          style={{ margin: "14px 0 0" }}
          onSubmit={(event) => {
            event.preventDefault();
            if (name && !busy) ask(name);
          }}
        >
          <input
            id="models-add-name"
            aria-label="Model name on Hugging Face"
            ref={field}
            className="input mono"
            style={{ flex: "1 1 260px", minWidth: 0 }}
            placeholder={SUGGESTED_MODEL}
            autoComplete="off"
            spellCheck={false}
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              forget();
            }}
          />
          <button type="submit" className="btn" disabled={busy || !name}>
            Check
          </button>
        </form>
      )}

      {manage && !checking && !check && !text && !overview.models.length && (
        <p className="faint models-suggest">
          <span>
            A good first model: Google&rsquo;s Gemma 4 E4B, packaged for the Mac by mlx-community.
          </span>
          <button
            type="button"
            className="btn"
            style={{ padding: "3px 10px" }}
            disabled={busy}
            onClick={() => {
              setText(SUGGESTED_MODEL);
              ask(SUGGESTED_MODEL);
              // This button goes as the check starts; the keyboard waits in
              // the field, which now holds the name being checked.
              field.current?.focus({ preventScroll: true });
            }}
          >
            Check this one
          </button>
        </p>
      )}

      {/* A build being checked says so beside its list instead, where the
          eye and the keyboard are. */}
      {checking && !(check && pendingVariant && check.variants.length > 1) && (
        <p className="models-progress-line" role="status" style={{ margin: "12px 0 0" }}>
          <span className="spinner" aria-hidden="true" />
          <span className="faint">Asking Hugging Face…</span>
        </p>
      )}

      {refused && (
        <p role="alert" className="models-save-note" data-tone="bad" style={{ margin: "12px 0 0" }}>
          {refused}
        </p>
      )}

      {check && (
        <CheckCard
          check={check}
          settings={overview.settings}
          busy={busy}
          checking={checking}
          pendingVariant={pendingVariant}
          buildProblem={buildProblem}
          verdictRef={verdict}
          away={away}
          onDownload={() => download(check)}
          onVariant={(variant) => chooseBuild(check.repo_id, variant)}
          onAgain={() => ask(check.repo_id, check.variant)}
        />
      )}
    </div>
  );
}

/**
 * "Which build": one radio per build of a repository that holds several,
 * smallest first. Choosing one asks the server to check that build, so the
 * size, memory and files on the card above are always the server's for the
 * build the radio shows. Native radios: Tab reaches the chosen one, the
 * arrows move between them. A repository of one build draws nothing.
 */
function WhichBuild({
  check,
  settings,
  busy,
  pendingVariant,
  problem,
  onVariant,
}: {
  check: LocalModelPreflight;
  settings: LocalModelsSettings;
  busy: boolean;
  /** The build whose check is still out, while it is. */
  pendingVariant: string | null;
  /** Why the last build chosen could not be checked; the list shows the
   *  build the card is about again. */
  problem: string | null;
  onVariant: (variant: string) => void;
}) {
  const group = useId();
  const builds = buildsInOrder(check);
  if (!builds.length) return null;
  const chosen = pendingVariant ?? check.variant;
  const chosenBuild = builds.find((variant) => variant.id === chosen) ?? null;
  return (
    <fieldset className="models-builds" aria-describedby={`${group}-intro`}>
      <legend>
        Which build
        {pendingVariant && (
          <span className="models-builds-asking" role="status">
            <span className="spinner" aria-hidden="true" />
            <span className="faint">Checking {pendingVariant}…</span>
          </span>
        )}
      </legend>
      <p id={`${group}-intro`} className="faint models-builds-intro">
        {BUILDS_INTRO}
      </p>
      <div
        className="tablewrap models-builds-list"
        // Past BUILDS_SHOWN rows the box scrolls by itself; the keyboard needs
        // nothing more, since a radio the arrows reach is scrolled into view.
        data-scrolls={builds.length > BUILDS_SHOWN ? "yes" : undefined}
      >
        <table className="table">
          <thead>
            <tr>
              <th scope="col">Build</th>
              <th scope="col">Size</th>
              <th scope="col">Memory</th>
              <th scope="col">Runs</th>
            </tr>
          </thead>
          <tbody>
            {builds.map((variant) => {
              const words = buildText(variant, settings);
              const id = `${group}-${variant.id}`;
              const picked = variant.id === chosen;
              // Every cell is a label of the row's radio: a click anywhere on
              // the row chooses it, and the radio's name is the whole row.
              return (
                <tr key={variant.id} data-chosen={picked ? "yes" : undefined}>
                  <td>
                    <label htmlFor={id} className="models-build-id">
                      <input
                        type="radio"
                        id={id}
                        name={group}
                        value={variant.id}
                        checked={picked}
                        disabled={busy}
                        onChange={() => onVariant(variant.id)}
                      />
                      <span className="mono">{variant.id}</span>
                      {words.suggested && <span className="pill models-suggested">suggested</span>}
                    </label>
                    {words.note && (
                      <label htmlFor={id} className="faint models-build-note">
                        {words.note}
                      </label>
                    )}
                  </td>
                  <td>
                    <label htmlFor={id}>{words.size}</label>
                  </td>
                  <td>
                    <label htmlFor={id}>{words.memory}</label>
                  </td>
                  <td>
                    <label htmlFor={id}>
                      <span className="pill" data-tone={words.fits.tone} title={words.fits.sentence}>
                        {words.fits.label}
                      </span>
                    </label>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {problem && (
        <p role="alert" className="models-row-sentence" data-tone="bad">
          {problem}
        </p>
      )}
      {chosenBuild && (
        <p className="faint models-builds-reads">{buildText(chosenBuild, settings).reads}</p>
      )}
    </fieldset>
  );
}

function CheckCard({
  check,
  settings,
  busy,
  checking,
  pendingVariant,
  buildProblem,
  verdictRef,
  away,
  onDownload,
  onVariant,
  onAgain,
}: {
  check: LocalModelPreflight;
  settings: LocalModelsSettings;
  busy: boolean;
  /** A re-check is out: the card stays, but Download waits for the answer —
   *  it would fetch the build this card shows, not the one just chosen. */
  checking: boolean;
  pendingVariant: string | null;
  buildProblem: string | null;
  verdictRef: RefObject<HTMLParagraphElement | null>;
  away: string | null;
  onDownload: () => void;
  onVariant: (variant: string) => void;
  onAgain: () => void;
}) {
  const more = otherProblems(check);
  return (
    <section
      className="preflight"
      aria-label={`What Check found about ${check.repo_id}`}
      aria-busy={checking || undefined}
      style={checking ? { opacity: 0.6 } : undefined}
    >
      <p ref={verdictRef} className="preflight-verdict" data-ok={check.ok ? "yes" : "no"}>
        {check.verdict}
      </p>
      <p className="faint mono" style={{ margin: "2px 0 0", fontSize: "0.8rem", overflowWrap: "anywhere" }}>
        <a href={hfLink(check.repo_id)} target="_blank" rel="noreferrer">
          {check.repo_id}
        </a>{" "}
        · build {check.revision_short}
      </p>

      <dl className="preflight-checks">
        {check.checks.map((row) => (
          <div key={row.key} className="preflight-row">
            <dt>{row.label}</dt>
            <dd>
              <span className="preflight-value">
                <span>{row.value}</span>
                <span className="pill" data-tone={row.tone}>
                  {checkToneWord(row.tone)}
                </span>
              </span>
              {row.detail && <span className="faint preflight-detail">{row.detail}</span>}
            </dd>
          </div>
        ))}
      </dl>

      {more.length > 0 && (
        <ul className="preflight-list" data-tone="bad">
          {more.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}

      {/* Shown even on a refused card: a smaller build may be the way past
          a refusal about room on the disk. */}
      <WhichBuild
        check={check}
        settings={settings}
        busy={busy}
        pendingVariant={checking ? pendingVariant : null}
        problem={buildProblem}
        onVariant={onVariant}
      />

      {check.ok && check.warnings.length > 0 && (
        <>
          <p className="eyebrow" style={{ margin: "14px 0 0" }}>
            Before you download
          </p>
          <ul className="preflight-list" data-tone="warn">
            {check.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </>
      )}
      {check.notes.map((note) => (
        <p key={note} className="faint" style={{ margin: "10px 0 0", fontSize: "0.86rem" }}>
          {note}
        </p>
      ))}

      {check.ok ? (
        <div style={{ marginTop: 16 }}>
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || checking || !!away}
            title={away ?? undefined}
            onClick={onDownload}
          >
            {downloadLabel(check)}
          </button>
          {away && (
            <p style={{ margin: "8px 0 0", color: "var(--ember)", fontSize: "0.86rem" }}>{away}</p>
          )}
          <p className="faint" style={{ margin: "8px 0 0", fontSize: "0.84rem" }}>
            {buildLine(check)}
          </p>
        </div>
      ) : (
        <div style={{ marginTop: 14 }}>
          <button type="button" className="btn" disabled={busy} onClick={onAgain}>
            Check again
          </button>
        </div>
      )}
    </section>
  );
}
