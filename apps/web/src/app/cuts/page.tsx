"use client";

// Cuts: rough cuts for Premiere — drafted here by the local Editor, built by
// Claude, or imported from a finished timeline. Cut history has existed since
// the connector first recorded a cut; this is the first page that shows it.
//
// One request draws the page (`api.cuts()`), read again every 3 s while a
// draft is queued or being drafted, every 30 s while one only waits for the
// overnight window or nothing is under way, never while the tab is hidden,
// and at once after every action (`rereads`, the AI models page's reader).
// Nothing about a draft lives in the tab: a draft asked for, left and come
// back to — or asked for from the chat panel — is shown from the server.
//
// Where models cannot run (the container server) the form's place holds one
// sentence and the rest of the page works: the list, and every cut in it.
//
// The list starts with the newest thirty cuts. When it is full it says "the
// newest 30" and offers the older ones (the server lists up to 200 at once):
// this page is the only way in the menu to a cut, so one that cannot be
// listed cannot be opened, downloaded again, compared or deleted. Past 200,
// while the server says more come after, "Show more" brings the next 200.
//
// Three other pages lead here with the footage already chosen. A clip's page
// and a folder in Browse name a folder (`?library=&folder=`), which the form
// takes once and may then change. Search hands over the clips ticked there
// (`?clips=`): those the form cannot hold as a folder, so the address holds
// them. While it names them the form's footage is those clips — a reload
// shows the same form — and when the form lets go of them (a draft asked
// for, "Choose a folder instead", Try again on another draft) they are taken
// out of the address, so a later reload does not bring them back. Back after
// "Choose a folder instead" brings them back; after a draft it does not.

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";

import DraftCard from "@/components/DraftCard";
import DraftCutForm, { type DraftSeed } from "@/components/DraftCutForm";
import Shell from "@/components/Shell";
import { ApiError, api, type CutDraft, type CutsOverview } from "@/lib/api";
import { askClaude as askClaudeAbout, chat, useChat } from "@/lib/chatStore";
import {
  CUTS_FIRST,
  CUTS_MOST,
  DEMO_DRAFTS,
  askClaudeText,
  askedStands,
  blankForm,
  cutsShown,
  endedDrafts,
  endedSentence,
  formFromDraft,
  handedOver,
  joinPages,
  laterPages,
  letGoBy,
  type LetGo,
  moreCuts,
  offerFurther,
  readAgainIn,
  secondsText,
} from "@/lib/cuts";
import { relativeTime } from "@/lib/format";
import { usePageTitle } from "@/lib/hooks";
import { focusIfLost, rereads, type Rereader } from "@/lib/localModels";

type Ended = { id: string; text: string; cutId: string | null };
type Said = { text: string; tone: "alert" | "status" };
/** One reading of the list, with how many cuts it asked for: whether the
 *  list is full is judged against the limit of the answer on screen, not
 *  against one that has been asked for and not answered yet. */
type Reading = { overview: CutsOverview; limit: number };

// How many cuts the list asks for. Out here so it outlives the page: someone
// who asked for the older cuts, opened one and came back finds them still
// listed.
let listLimit = CUTS_FIRST;
// How many cuts past the first page "Show more" has brought (0 until the list
// asks for the most one answer holds). Kept here for the same reason.
let listBeyond = 0;

function CutsInner() {
  usePageTitle("Cuts");
  const router = useRouter();
  const params = useSearchParams();
  const [overview, setOverview] = useState<CutsOverview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<Said | null>(null);
  const [ended, setEnded] = useState<Ended[]>([]);
  const [seed, setSeed] = useState<DraftSeed | null>(null);
  // The draft the form last asked for, while the list has it under way: the
  // form's "Asked. … is under In progress below" is said for no longer.
  const [askedFor, setAskedFor] = useState<string | null>(null);
  // How many cuts the answer on screen asked for.
  const [limit, setLimit] = useState(listLimit);
  const lastFailed = useRef(false);
  const [older, setOlder] = useState<{ busy: boolean; said: string }>({ busy: false, said: "" });
  const previous = useRef<CutsOverview | null>(null);
  const reader = useRef<Rereader<Reading> | null>(null);
  const progressHead = useRef<HTMLHeadingElement | null>(null);
  const cutsHead = useRef<HTMLHeadingElement | null>(null);
  const cutsTable = useRef<HTMLDivElement | null>(null);
  const draftSection = useRef<HTMLElement | null>(null);
  const { status: chatStatus } = useChat();

  // Clips ticked in Search, for as long as the address names them.
  const handed = useMemo(() => handedOver(params), [params]);

  // A link from a clip's page or Browse names a folder: the form opens with
  // it chosen. Read once; the address is not kept in step with the form.
  // Clips from Search bring the search with them, as the start of a brief.
  const linkLibrary = params.get("library");
  const linkFolder = params.get("folder");
  useEffect(() => {
    if (handed) {
      setSeed({ key: 1, form: { ...blankForm(), brief: handed.brief }, focus: false });
      return;
    }
    if (!linkLibrary || !linkFolder) return;
    setSeed({
      key: 1,
      form: { ...blankForm(), folder: { library_id: linkLibrary, path: linkFolder } },
      focus: false,
    });
    // Once, as the page opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // "Draft a cut…" in Search, or "Draft a cut from this folder" on a clip's
  // page or in Browse, was pressed to write a brief for footage already
  // chosen: once the form is there, the keyboard is in the brief (after the
  // search it starts with, from Search). Once per arrival; a plain visit to
  // Cuts leaves the keyboard where the browser puts it.
  const arrived = useRef(handed !== null || Boolean(linkLibrary && linkFolder));
  const formShown = overview !== null;
  useEffect(() => {
    if (!formShown || !arrived.current) return;
    arrived.current = false;
    const brief = draftSection.current?.querySelector<HTMLTextAreaElement>("textarea");
    if (!brief) return;
    brief.focus();
    brief.setSelectionRange(brief.value.length, brief.value.length);
  }, [formShown]);

  // The form has let go of the clips Search handed over: out of the address
  // they go, so that what a reload shows is what the form holds now. After
  // the × that is a step Back undoes (the clips come back with the address);
  // after a draft is asked for, or Try again, it is not (`letGoBy`). To Next
  // that is a navigation and it puts the site's own title back (a cut's page
  // has the same trouble with `?v=`), so the title is written again.
  const letGo = useCallback(
    (why: LetGo) => {
      if (!handedOver(new URLSearchParams(window.location.search))) return;
      if (letGoBy(why) === "push") router.push("/cuts", { scroll: false });
      else router.replace("/cuts", { scroll: false });
    },
    [router],
  );
  useEffect(() => {
    document.title = "Cuts · FrameFound";
  }, [params]);

  // How each draft that ended between two readings ended. One that became a
  // cut has left the list, so it is read by itself; one cleared meanwhile in
  // another tab is simply not mentioned.
  const sayEnded = useCallback(async (before: CutsOverview | null, next: CutsOverview) => {
    const ids = endedDrafts(before, next);
    if (!ids.length) return;
    const found = await Promise.all(
      ids.map(async (id): Promise<CutDraft | null> => {
        const listed = next.drafts.find((draft) => draft.id === id);
        if (listed) return listed;
        try {
          return await api.cutDraft(id);
        } catch {
          return null;
        }
      }),
    );
    const lines = found
      .filter((draft): draft is CutDraft => draft !== null)
      .map((draft) => ({ id: draft.id, text: endedSentence(draft), cutId: draft.state === "done" ? draft.cut_id : null }));
    if (lines.length) setEnded(lines);
  }, []);

  useEffect(() => {
    const own = rereads<Reading>({
      // Read off the module each time: every later reading asks for as many
      // as "Show older cuts" and "Show more" last asked for.
      read: async () => {
        const asked = listLimit;
        const beyond = asked >= CUTS_MOST ? listBeyond : 0;
        const first = await api.cuts(asked);
        if (!beyond || !first.more) return { overview: first, limit: asked };
        const later = await Promise.all(laterPages(beyond).map((page) => api.cuts(page.limit, page.offset)));
        return { overview: { ...first, ...joinPages(first, later) }, limit: asked };
      },
      answered: ({ overview: next, limit: asked }) => {
        void sayEnded(previous.current, next);
        previous.current = next;
        lastFailed.current = false;
        setOverview(next);
        setAskedFor((before) => (before && !askedStands(before, next.drafts) ? null : before));
        setLimit(asked);
        setLoadError(null);
      },
      failed: (err) => {
        lastFailed.current = true;
        // The last good answer stays on screen beneath the sentence.
        setLoadError(
          err instanceof ApiError
            ? err.message
            : "FrameFound could not be reached to read the cuts. It tries again shortly.",
        );
      },
      every: (latest, failed) => readAgainIn(latest?.overview ?? null, failed),
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
  }, [sayEnded]);

  const load = useCallback(async () => {
    await reader.current?.now();
  }, []);

  // The chat panel's own reason it would not answer, for "Ask Claude
  // instead"; asked for once a failed draft is on the page.
  const hasFailed = overview?.drafts.some((draft) => draft.state === "failed") ?? false;
  useEffect(() => {
    if (hasFailed && chatStatus === null) void chat.refreshStatus();
  }, [hasFailed, chatStatus]);
  const claudeProblem = chatStatus
    ? (chatStatus.problem ??
      (chatStatus.can_chat ? null : "This account can read conversations but not start one."))
    : null;

  async function act(work: () => Promise<unknown>, done: string | null) {
    setBusy(true);
    setSaid(null);
    // What ended earlier has been seen; the action's own sentence replaces it.
    setEnded([]);
    try {
      await work();
      if (done) setSaid({ text: done, tone: "status" });
    } catch (err) {
      setSaid({
        text: err instanceof Error && err.message ? err.message : "That did not work. Try again.",
        tone: "alert",
      });
    } finally {
      setBusy(false);
      await load();
      // The button pressed may have gone with its card: carry on from the
      // section's heading rather than from the top of the page. After the
      // render that removes it — until then the button still holds focus.
      window.setTimeout(() => focusIfLost(progressHead.current ?? cutsHead.current), 0);
    }
  }

  function tryAgain(draft: CutDraft) {
    // Everything the draft was asked for goes into the form, and the form
    // shows it: clips named one by one, the chat's "folders matching", and
    // the cut it was to be the next version of (which the form looks up,
    // and drops when that cut has been deleted since).
    setSeed((before) => ({ key: (before?.key ?? 1) + 1, form: formFromDraft(draft), focus: true }));
    // The form now holds that draft's footage, not the clips from Search.
    letGo("retried");
  }

  /** "Show older cuts" up to the most one answer holds, then "Show more":
   *  the next page after the cuts listed. */
  async function showOlder() {
    if (older.busy || !overview) return;
    const had = overview.cuts.length;
    const before = { limit: listLimit, beyond: listBeyond };
    if (limit < CUTS_MOST) listLimit = moreCuts(limit);
    else listBeyond = listBeyond + CUTS_MOST;
    setOlder({ busy: true, said: "" });
    await load();
    if (lastFailed.current) {
      // Not read: the sentence above the list says so, the button stays, and
      // the page goes on asking for what it was showing.
      listLimit = before.limit;
      listBeyond = before.beyond;
      setOlder({ busy: false, said: "" });
      return;
    }
    const now = previous.current?.cuts.length ?? had;
    const more = now - had;
    setOlder({
      busy: false,
      said: more > 0 ? `${more} older ${more === 1 ? "cut is" : "cuts are"} now listed.` : "There are no older cuts.",
    });
    // The button may have gone (everything is listed now): carry on from the
    // first of the cuts it brought, or from the heading when it brought none.
    window.setTimeout(() => {
      const links = cutsTable.current?.querySelectorAll<HTMLAnchorElement>("tbody a");
      const first = now > had ? links?.[had] : null;
      if (first) first.focus();
      else focusIfLost(cutsHead.current);
    }, 0);
  }

  function askClaude(draft: CutDraft) {
    // A new conversation with the request written in its text box and the
    // keyboard in it; the person reads it and presses Send. Nothing is sent
    // from here.
    askClaudeAbout({ text: askClaudeText(draft) });
  }

  const heading = (
    <>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Cuts</h2>
      </div>
      <p className="faint cuts-lead">
        Rough cuts for Premiere — drafted here, built by Claude, or imported from a finished
        timeline.
      </p>
    </>
  );

  if (!overview) {
    return (
      <Shell>
        {heading}
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

  const { editor } = overview;
  const listed = cutsShown(overview.cuts.length, limit, overview.more);
  const further = offerFurther(limit, overview.more);
  const readOnly = !overview.can_draft && editor.why_not === null && editor.mode !== "unavailable";

  return (
    <Shell>
      {heading}

      {editor.mode === "demo" && (
        <div className="demo-banner" role="status">
          <strong>Demonstration only.</strong> {DEMO_DRAFTS} It is here so the page can be looked at
          on a development machine.
        </div>
      )}
      {loadError && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)", marginTop: 14 }}>
          {loadError}
        </div>
      )}
      {ended.length > 0 && (
        <div className="cuts-ended" role="status">
          {ended.map((line) => (
            <p key={line.id}>
              {line.text}
              {line.cutId && (
                <>
                  {" "}
                  <Link href={`/cuts/${line.cutId}`}>Open the draft</Link>
                </>
              )}
            </p>
          ))}
        </div>
      )}

      <section className="secsection" aria-labelledby="cuts-draft" ref={draftSection}>
        <div className="sectionhead">
          <h2 id="cuts-draft">Draft a cut</h2>
        </div>
        {editor.mode === "unavailable" ? (
          <div className="card" role="status">
            <p style={{ margin: 0 }}>
              {editor.note ??
                "Local AI models run on the Mac install of FrameFound. Everything else on this page works here."}
            </p>
            <p className="faint" style={{ margin: "8px 0 0" }}>
              The cuts below can still be opened, downloaded for Premiere and reviewed by Claude.
            </p>
          </div>
        ) : (
          <DraftCutForm
            editor={editor}
            canDraft={overview.can_draft}
            readOnly={readOnly}
            seed={seed}
            selection={handed}
            onUnselect={() => letGo("unselect")}
            askedFor={askedFor}
            onDrafted={async (draft) => {
              // Before anything else, so the form's "Asked." and this are drawn together.
              setAskedFor(draft.id);
              // Asked for: the clips from Search are the draft's now, not the form's.
              letGo("drafted");
              await load();
            }}
          />
        )}
      </section>

      {overview.drafts.length > 0 && (
        <section className="secsection" aria-labelledby="cuts-progress">
          <div className="sectionhead">
            <h2 id="cuts-progress" ref={progressHead} tabIndex={-1}>
              In progress
            </h2>
            <span className="faint mono">
              {overview.drafts.filter((draft) => draft.state !== "failed" && draft.state !== "cancelled").length}{" "}
              under way
            </span>
          </div>
          {said && (
            <div
              className="card"
              role={said.tone}
              style={{ borderColor: said.tone === "alert" ? "var(--ember)" : "var(--sage)", margin: "0 0 12px" }}
            >
              {said.text}
            </div>
          )}
          <div className="draftcards">
            {overview.drafts.map((draft) => (
              <DraftCard
                key={draft.id}
                draft={draft}
                busy={busy}
                canTryAgain={overview.can_draft}
                claudeProblem={claudeProblem}
                onCancel={(one) => void act(() => api.cancelCutDraft(one.id), null)}
                onClear={(one) => void act(() => api.clearCutDraft(one.id), `“${one.title}” was cleared.`)}
                onTryAgain={tryAgain}
                onAskClaude={askClaude}
              />
            ))}
          </div>
        </section>
      )}
      {said && overview.drafts.length === 0 && (
        <div className="card" role={said.tone} style={{ marginTop: 14 }}>
          {said.text}
        </div>
      )}

      <section className="secsection" aria-labelledby="cuts-list">
        <div className="sectionhead">
          <h2 id="cuts-list" ref={cutsHead} tabIndex={-1}>
            Cuts
          </h2>
          <span className="faint mono">{listed.text}</span>
        </div>
        {overview.cuts.length === 0 ? (
          <div className="empty">No cuts yet. Draft one above, or ask Claude in the chat panel.</div>
        ) : (
          <div className="tablewrap card cuts-table" ref={cutsTable}>
            <table className="table">
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Made by</th>
                  <th>Versions</th>
                  <th>Shots</th>
                  <th>Length</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {overview.cuts.map((family) => (
                  <tr key={family.root_id}>
                    <td>
                      <Link href={`/cuts/${family.id}`}>{family.title || "Untitled cut"}</Link>
                      {family.client && <div className="faint">{family.client}</div>}
                    </td>
                    <td>
                      <span className="cuts-made">
                        <span className="pill" data-made={family.newest.source}>
                          {family.newest.label}
                        </span>
                        {family.newest.demo && (
                          <span className="pill" data-tone="warn">
                            demo
                          </span>
                        )}
                        {family.sent && (
                          <span className="pill" data-tone="ok" title="Downloaded for Premiere">
                            downloaded
                          </span>
                        )}
                      </span>
                    </td>
                    <td className="mono">{family.versions}</td>
                    <td className="mono">{family.newest.piece_count}</td>
                    <td className="mono">{secondsText(family.newest.duration_s)}</td>
                    <td className="faint" title={family.newest.created_at ?? undefined}>
                      {relativeTime(family.newest.created_at)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {(listed.more || further || older.busy) && (
          <div className="cuts-older">
            <button type="button" className="btn" disabled={older.busy} onClick={() => void showOlder()}>
              {older.busy ? "Reading…" : further ? "Show more" : "Show older cuts"}
            </button>
            <span className="faint">
              {further
                ? `These are the newest ${overview.cuts.length}; there are older ones.`
                : `These are the newest ${overview.cuts.length}; there may be older ones.`}
            </span>
          </div>
        )}
        {listed.note && <p className="faint cuts-older">{listed.note}</p>}
        {/* Always in the page, so what the button brought is read out. */}
        <p className="compare-sr" role="status">
          {older.said}
        </p>
      </section>
    </Shell>
  );
}

export default function CutsPage() {
  // useSearchParams needs a Suspense boundary round it on a page that is
  // otherwise rendered ahead of time (as Browse and Listings have).
  return (
    <Suspense>
      <CutsInner />
    </Suspense>
  );
}
