"use client";

// One cut: a version of it, shot by shot, with the other versions a button
// away — a local draft, Claude's version of it, a timeline that was imported.
//
// The address is `/cuts/<any version's id>`, and the version on screen is
// `?v=<its number>`, so a reload and a pasted link show the same version. A
// version keeps its number when another is deleted.
//
// The shot list is the page: every row's picture opens the footage at the
// piece's in point (CutShots). Around it are the things that can be done
// with a version — download it for Premiere, ask the local Editor to draft
// it again (the result is the next version here), delete it — and what is
// known about how it was made: which build drafted it, from which shots, and
// what FrameFound changed when it checked the answer.
//
// Two versions can be put side by side (Compare, `?v=2&with=1` in the
// address), and any version can be handed to Claude to review: the button
// opens the chat panel on a new conversation with the request attached, and
// nothing is sent until Send is pressed there. A version Claude builds is
// kept beside this one, and the page says so when it appears.
//
// The address names one version, and that version can be deleted from
// somewhere else while the page is open. The cut is then still there: the
// address moves to the newest version left and the page says which one
// went. "This cut no longer exists." is only said when no version answers.
//
// Nothing about a draft lives in the tab. The page reads the cut again every
// 3 s while a draft of it is queued or being drafted, every 30 s otherwise
// (every 10 s for half an hour after a review was opened from here), never
// while the tab is hidden, and at once after every action. Reading,
// downloading and deleting need no model: on the container server only
// "Draft again" has nothing to offer, and it says so in one sentence.

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";

import CutCompare from "@/components/CutCompare";
import CutShots from "@/components/CutShots";
import DraftCard from "@/components/DraftCard";
import DraftCutForm, { type DraftSeed } from "@/components/DraftCutForm";
import PremiereDownload from "@/components/PremiereDownload";
import Shell from "@/components/Shell";
import {
  ApiError,
  api,
  type ChatStatus,
  type CutDetail,
  type CutDraft,
  type CutMadeFrom,
  type CutVersion,
} from "@/lib/api";
import { askClaude as askClaudeAbout, useChat } from "@/lib/chatStore";
import { gate } from "@/lib/chatWords";
import {
  DEMO_DRAFTS,
  afterDelete,
  arrivedSentence,
  arrivedSinceReview,
  arrivedVersions,
  askClaudeText,
  askedStands,
  compareAfterShow,
  compareDefault,
  compareHref,
  comparedWith,
  deleteQuestion,
  endedDrafts,
  endedSentence,
  formFromDraft,
  formFromVersion,
  madeLine,
  momentHref,
  otherVersions,
  readAgainIn,
  reviewEvery,
  shownVersion,
  spanText,
  versionButton,
  versionHref,
  versionLine,
  type DraftForm,
  type ReviewWatch,
} from "@/lib/cuts";
import { relativeTime } from "@/lib/format";
import { usePageTitle } from "@/lib/hooks";
import { focusIfLost, rereads, type Rereader } from "@/lib/localModels";

/** A sentence above the drafts. `draftId`: it says that draft has been
 *  asked for, which stops being true when the draft ends (`askedStands`). */
type Said = { text: string; tone: "alert" | "status"; draftId?: string };
/** A draft of this cut that ended, or a version that appeared, while the
 *  page was open. `show`: the number of the version there now is, when
 *  there is one; `parent`: the number of the version it revises, when that
 *  is still kept — what Compare is offered against. */
type Ended = { id: string; text: string; show: number | null; parent: number | null };
/** The "Draft again" panel, open. A new `seed.key` is a new form. `revises`
 *  is the version the form was opened for: the result follows that one,
 *  whichever version is looked at while the brief is being written. */
type Again = { seed: DraftSeed; revises: string };

// What deleting a version said, when the address had to move to another
// version to keep showing the cut: the page that opens there says it once.
let carried: { id: string; text: string } | null = null;

// The version an address named turned out to be gone, and the page moved to
// another version of the same cut: the versions still to try should that
// one be gone too, and the sentence to say when one of them answers.
let fallback: { id: string; rest: string[]; text: string } | null = null;

// Reviews opened from here, by the cut's root id: when Ask Claude to review
// was last pressed, and the versions the cut had at the last reading since.
// Out here so it outlives the page: the reader may look at another page
// while Claude works, and the page that opens when they come back says what
// was built meanwhile (it has no earlier reading of its own to tell by).
const reviewsOpened = new Map<string, ReviewWatch>();

const CHAT_MISSING = "The chat panel is not part of this install.";

const UNAVAILABLE =
  "Local AI models run on the Mac install of FrameFound. Everything else on this page works here.";

function CutInner() {
  const params = useParams<{ id: string }>();
  const id = params.id;
  const router = useRouter();
  const search = useSearchParams();
  const v = search.get("v");
  const withV = search.get("with");

  const [detail, setDetail] = useState<CutDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  const goneRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<Said | null>(null);
  const [ended, setEnded] = useState<Ended[]>([]);
  const [again, setAgain] = useState<Again | null>(null);
  const [role, setRole] = useState<string | null>(null);
  // Whether Claude can be asked: what `/chat/status` said for this visit.
  // `missing`: this server has no chat at all.
  const [claude, setClaude] = useState<{ status: ChatStatus | null; missing: boolean }>({
    status: null,
    missing: false,
  });
  const [attaching, setAttaching] = useState(false);
  const previous = useRef<CutDetail | null>(null);
  const reader = useRef<Rereader<CutDetail> | null>(null);
  const head = useRef<HTMLHeadingElement | null>(null);
  const draftsHead = useRef<HTMLHeadingElement | null>(null);
  const againPanel = useRef<HTMLDivElement | null>(null);
  const againToggle = useRef<HTMLButtonElement | null>(null);
  const shotsHead = useRef<HTMLHeadingElement | null>(null);
  // Set by a button that goes away once the address it asked for is on
  // screen ("Show version 2"): the keyboard then carries on from the shots.
  const toShots = useRef(false);
  const seedKey = useRef(1);
  const { status: chatStatus } = useChat();

  // An answer about another cut is not this one's: the address can change
  // under the page (a version deleted, a link followed) before the new cut
  // has been read.
  const cut = detail && detail.versions.some((version) => version.id === id) ? detail : null;
  const pageTitle = cut?.title || "Cut";
  usePageTitle(pageTitle);
  // Choosing a version changes `?v=`, which to Next is a navigation: it puts
  // the site's own title back, and `usePageTitle` has no reason to write the
  // same words again. Without this the tab read "FrameFound" after the first
  // press of a version's button.
  useEffect(() => {
    document.title = `${pageTitle} · FrameFound`;
  }, [pageTitle, v, withV]);
  // Not from the click itself: the address changes a moment later, and the
  // button that was pressed is still there until it has.
  useEffect(() => {
    if (!toShots.current) return;
    toShots.current = false;
    const active = document.activeElement;
    if (!active || active === document.body || !active.isConnected) shotsHead.current?.focus();
  }, [v, withV]);

  // How each draft of this cut that ended between two readings ended. One
  // that became a version has left the drafts, and so has one that was
  // cancelled, so each is read by itself to tell which. And any version
  // that appeared without a draft of this page's behind it: Claude building
  // one after a review, or a draft asked for and finished somewhere else.
  // `arrived` is those versions: since the page's last reading, or, on its
  // first, since a review was opened from an earlier visit.
  const sayEnded = useCallback(async (before: CutDetail | null, next: CutDetail, arrived: CutVersion[]) => {
    const ids = endedDrafts(before, next);
    const numberOf = (versionId: string | null): number | null =>
      next.versions.find((version) => version.id === versionId)?.version ?? null;
    const found = await Promise.all(
      ids.map(async (draftId): Promise<CutDraft | null> => {
        const listed = next.drafts.find((draft) => draft.id === draftId);
        if (listed) return listed;
        try {
          return await api.cutDraft(draftId);
        } catch {
          return null;
        }
      }),
    );
    const lines = found
      .filter((draft): draft is CutDraft => draft !== null)
      .map((draft): Ended => {
        const made =
          draft.state === "done" && draft.cut_id
            ? (next.versions.find((version) => version.id === draft.cut_id) ?? null)
            : null;
        return {
          id: draft.id,
          text: made ? `Version ${made.version} is ready.` : endedSentence(draft),
          show: made ? made.version : null,
          parent: made ? numberOf(made.parent_id) : null,
        };
      });
    for (const version of arrived) {
      if (lines.some((line) => line.show === version.version)) continue;
      lines.push({
        id: version.id,
        text: arrivedSentence(version),
        show: version.version,
        parent: numberOf(version.parent_id),
      });
    }
    if (lines.length) setEnded(lines);
  }, []);

  useEffect(() => {
    goneRef.current = false;
    previous.current = null;
    setGone(false);
    setBusy(false);
    setAgain(null);
    setEnded([]);
    setLoadError(null);
    const own = rereads<CutDetail>({
      read: () => api.cut(id),
      answered: (next) => {
        const watch = reviewsOpened.get(next.root_id);
        const before = previous.current;
        void sayEnded(before, next, before ? arrivedVersions(before, next) : arrivedSinceReview(watch, next));
        // What this reading had is what the next page to open tells by, so a
        // version is said once.
        if (watch) reviewsOpened.set(next.root_id, { at: watch.at, versions: next.versions.map((each) => each.id) });
        previous.current = next;
        setDetail(next);
        // "Asked. It is under Drafts of this cut" goes once the draft is not
        // under way: the line for how it ended takes its place.
        setSaid((before) => (before?.draftId && !askedStands(before.draftId, next.drafts) ? null : before));
        setLoadError(null);
        setGone(false);
        if (fallback?.id === id) fallback = null;
        if (carried) {
          if (carried.id === id) setSaid({ text: carried.text, tone: "status" });
          carried = null;
        }
      },
      failed: (err) => {
        if (err instanceof ApiError && err.status === 404) {
          // The version the address names is gone (deleted here, in another
          // tab, or by someone else). That is not the cut: the address
          // moves to the newest other version the page knew of, and the cut
          // is only said to be gone when none of them answers.
          goneRef.current = true;
          const known = previous.current;
          const waiting = fallback?.id === id ? fallback : null;
          const number = known?.versions.find((version) => version.id === id)?.version;
          const text = known
            ? number === undefined
              ? "The version that was on screen has been deleted."
              : `Version ${number} of this cut has been deleted.`
            : (waiting?.text ?? "");
          const [next, ...rest] = known ? otherVersions(known.versions, id) : (waiting?.rest ?? []);
          if (next) {
            fallback = { id: next, rest, text };
            carried = { id: next, text };
            router.replace(`/cuts/${next}${window.location.search}`, { scroll: false });
            return;
          }
          fallback = null;
          setGone(true);
          return;
        }
        // The last good answer stays on screen beneath the sentence.
        setLoadError(
          err instanceof ApiError
            ? err.message
            : "FrameFound could not be reached to read this cut. It tries again shortly.",
        );
      },
      every: (latest, failed) =>
        goneRef.current
          ? null
          : reviewEvery(
              readAgainIn(latest, failed),
              (latest && reviewsOpened.get(latest.root_id)?.at) ?? null,
              Date.now(),
            ),
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
  }, [id, router, sayEnded]);

  const load = useCallback(async () => {
    await reader.current?.now();
  }, []);

  // Whether this account may change anything. The cut's own answer says so
  // on a Mac (`can_draft`); where models cannot run that is false for
  // everyone, so the account is asked for once.
  useEffect(() => {
    let live = true;
    api
      .me()
      .then((me) => {
        if (live) setRole(me.role);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  // Whether Claude can be asked, for "Ask Claude to review" and for a failed
  // draft's "Ask Claude instead": asked once per visit, and only once there
  // is a button that needs the answer. Whatever the chat panel learns after
  // that (it asks again each time it is opened) replaces it.
  const hasFailed = cut?.drafts.some((draft) => draft.state === "failed") ?? false;
  const mayChange = cut ? cut.can_draft || (role !== null && role !== "readonly") : false;
  const asksClaude = hasFailed || mayChange;
  useEffect(() => {
    if (!asksClaude) return;
    let live = true;
    api
      .chatStatus()
      .then((status) => {
        if (live) setClaude({ status, missing: false });
      })
      .catch((err: unknown) => {
        // No such route: a server without the chat. Anything else is left
        // unknown, and the panel says for itself what is wrong when opened.
        if (live && err instanceof ApiError && err.status === 404) setClaude({ status: null, missing: true });
      });
    return () => {
      live = false;
    };
  }, [asksClaude]);
  useEffect(() => {
    if (chatStatus) setClaude({ status: chatStatus, missing: false });
  }, [chatStatus]);
  const claudeStatus = claude.status;
  const claudeProblem = claudeStatus
    ? (claudeStatus.problem ??
      (claudeStatus.can_chat ? null : "This account can read conversations but not start one."))
    : claude.missing
      ? CHAT_MISSING
      : null;

  /** After the render that follows an action: carry on from a heading when
   *  the button pressed has gone with what it acted on. */
  function refocus(target: () => HTMLElement | null) {
    window.setTimeout(() => focusIfLost(target()), 0);
  }

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
      refocus(() => draftsHead.current ?? head.current);
    }
  }

  const back = (
    <Link href="/cuts" className="compare-back">
      ‹ Cuts
    </Link>
  );

  if (gone) {
    return (
      <Shell>
        <div className="sectionhead" style={{ marginTop: 0 }}>
          {back}
        </div>
        <div className="empty">
          This cut no longer exists. <Link href="/cuts">Back to Cuts</Link>
        </div>
      </Shell>
    );
  }

  const shown = cut ? shownVersion(cut.versions, id, v) : null;

  if (!cut || !shown) {
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

  // Not read-only: said at once by a cut that can be drafted from, and by
  // the account itself where nothing can be drafted at all.
  const canChange = mayChange;
  const readOnly = !cut.can_draft && role === "readonly";
  const local = shown.source === "local";
  const made = madeLine(shown);
  const problems = shown.draft?.problems ?? [];
  // A failed draft stays listed until it is cleared; it is not under way.
  const underWay = cut.drafts.filter((draft) => draft.state !== "failed" && draft.state !== "cancelled").length;
  const title = cut.title || shown.title || "Untitled cut";

  // The version this one is being compared with (`?with=`), when it is.
  const other = comparedWith(cut.versions, shown, withV);
  const compareStart = compareDefault(cut.versions, shown);

  /** The address for a pair, or for one version when `beside` is null. */
  function go(version: { version: number }, beside: { version: number } | null) {
    router.replace(beside ? compareHref(id, version, beside) : versionHref(id, version), { scroll: false });
  }

  // None of the three ways to another version below closes the Draft again
  // form: looking at the other version is what one does while writing
  // "like version 1, but…", and closing it threw the brief away.
  function show(version: CutVersion) {
    if (!other || !shown) {
      go(version, null);
      return;
    }
    // Comparing: the comparison stays, around the version just chosen.
    const next = compareAfterShow(shown, other, version);
    go(next.shown, next.other);
  }

  function toggleCompare() {
    if (!shown) return;
    if (other) go(shown, null);
    else if (compareStart) go(shown, compareStart);
  }

  /** Another version picked for one side of the comparison. */
  function choose(side: "older" | "newer", version: CutVersion) {
    if (!shown || !other) return;
    const shownSide = shown.version < other.version ? "older" : "newer";
    if (side === shownSide) {
      go(version, other);
    } else {
      go(shown, version);
    }
  }

  /** From the line that says a version appeared. The button pressed goes
   *  once what it offered is on screen, so the keyboard carries on from the
   *  shots (see `toShots`). */
  function fromEnded(version: number, beside: number | null) {
    toShots.current = true;
    go({ version }, beside === null ? null : { version: beside });
  }

  async function review(version: CutVersion, pressed: HTMLElement) {
    if (!cut || attaching) return;
    setAttaching(true);
    setSaid(null);
    try {
      const request = await api.cutReviewRequest(version.id);
      // The panel opens on a new conversation with the request above its
      // box. Nothing is sent from here: the reader presses Send there.
      // `pressed` is where the keyboard goes back to when the panel closes
      // (the button is switched off while the request is fetched, so by now
      // it may not be what has focus).
      askClaudeAbout({ attachment: { label: request.label, text: request.text } }, pressed);
      // If Claude builds a version, see it without a reload — and, when the
      // reader has gone to another page meanwhile, on coming back.
      reviewsOpened.set(cut.root_id, { at: Date.now(), versions: cut.versions.map((each) => each.id) });
      void load();
    } catch (err) {
      setSaid({
        text:
          err instanceof ApiError
            ? err.message
            : "FrameFound could not be reached, so nothing was attached. Try again.",
        tone: "alert",
      });
    } finally {
      setAttaching(false);
    }
  }

  function openAgain(form: DraftForm, revises: string) {
    seedKey.current += 1;
    setAgain({ seed: { key: seedKey.current, form, focus: true }, revises });
    // The panel opens below the buttons: bring it into view and put the
    // keyboard in its first field (or on the sentence, where there is no form).
    window.setTimeout(() => {
      const panel = againPanel.current;
      const target = panel?.querySelector<HTMLElement>("textarea") ?? panel;
      target?.scrollIntoView({ block: "center" });
      target?.focus({ preventScroll: true });
    }, 0);
  }

  function toggleAgain(version: CutVersion) {
    if (again) {
      setAgain(null);
      return;
    }
    // The form holds the footage itself: a folder, or the clips or "folders
    // matching" the version was drafted from, each with a way to change it.
    openAgain(formFromVersion(version).form, version.id);
  }

  function tryAgain(draft: CutDraft) {
    if (!shown) return;
    // The version it revised may have been deleted since (now, or by the
    // time Draft is pressed: see `revising` below); the next version of
    // this cut is still what is being asked for.
    openAgain(formFromDraft(draft), draft.revises_cut_id ?? shown.id);
  }

  function askClaude(draft: CutDraft) {
    // A new conversation with the request written in its text box and the
    // keyboard in it; the person reads it and presses Send. Nothing is sent
    // from here.
    askClaudeAbout({ text: askClaudeText(draft) });
  }

  async function drafted(draft: CutDraft) {
    setAgain(null);
    setEnded([]);
    setSaid({
      text: "Asked. It is under “Drafts of this cut”; when it is ready it becomes the next version here. You can leave this page.",
      tone: "status",
      draftId: draft.id,
    });
    await load();
    refocus(() => draftsHead.current ?? againToggle.current ?? head.current);
  }

  // Why Claude cannot be asked from here, when it cannot: the same sentence
  // the chat panel would show in place of its box.
  const claudeGate = gate(claudeStatus, role === "admin");
  const reviewWhy = claude.missing ? CHAT_MISSING : (claudeGate?.sentence ?? null);
  const older = other ? (other.version < shown.version ? other : shown) : null;
  const newer = other ? (other.version < shown.version ? shown : other) : null;
  // The version an open Draft again form follows: the one it was opened
  // for, or the one on screen once that one has been deleted.
  const revising = again ? (cut.versions.find((version) => version.id === again.revises) ?? shown) : null;

  async function remove(version: CutVersion) {
    if (!cut) return;
    if (!window.confirm(deleteQuestion(version.title || title, version.version, cut.versions.length))) return;
    const left = afterDelete(cut.versions, version.id);
    const done = `Version ${version.version} was deleted.`;
    setBusy(true);
    setSaid(null);
    setEnded([]);
    setAgain(null);
    try {
      await api.deleteCut(version.id);
    } catch (err) {
      setSaid({
        text:
          err instanceof ApiError
            ? err.message
            : "FrameFound could not be reached, so nothing was deleted. Try again.",
        tone: "alert",
      });
      setBusy(false);
      await load();
      return;
    }
    if (!left) {
      // It was the only version: the cut has left the list.
      reader.current?.stop();
      router.push("/cuts");
      return;
    }
    if (version.id === id) {
      // The address names the version that is gone. Move to one that is
      // left before anything reads the old address and finds nothing.
      reader.current?.stop();
      carried = { id: left.id, text: done };
      router.replace(`/cuts/${left.id}`);
      return;
    }
    // Shown without it at once; the next read agrees.
    setDetail((before) =>
      before ? { ...before, versions: before.versions.filter((each) => each.id !== version.id) } : before,
    );
    setSaid({ text: done, tone: "status" });
    router.replace(versionHref(id, left), { scroll: false });
    setBusy(false);
    await load();
    // The button pressed now belongs to another version ("Delete this cut",
    // when one is left): the keyboard goes to the title, beside the sentence
    // that says what was deleted, not back onto a Delete.
    window.setTimeout(() => head.current?.focus(), 0);
  }

  return (
    <Shell>
      <div className="sectionhead cut-head" style={{ marginTop: 0 }}>
        {back}
      </div>
      <div className="cut-title">
        <h2 ref={head} tabIndex={-1}>
          {title}
        </h2>
        {cut.client && <span className="faint">{cut.client}</span>}
      </div>
      <p className="cut-line">
        <span className="faint mono">{versionLine(shown, cut.versions)}</span>
        {shown.demo && (
          <span className="pill" data-tone="warn">
            demo
          </span>
        )}
        {shown.sent_at && (
          <span className="pill" data-tone="ok" title={`Downloaded for Premiere ${relativeTime(shown.sent_at)}`}>
            downloaded
          </span>
        )}
        <span className="faint" title={shown.created_at ?? undefined}>
          made {relativeTime(shown.created_at)}
          {shown.created_by ? ` by ${shown.created_by}` : ""}
        </span>
      </p>

      {shown.demo && (
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
      {said && (
        <div
          className="card cut-said"
          role={said.tone}
          style={{ borderColor: said.tone === "alert" ? "var(--ember)" : "var(--sage)" }}
        >
          {said.text}
        </div>
      )}
      {ended.length > 0 && (
        <div className="cuts-ended" role="status">
          {ended.map((line) => (
            <p key={line.id}>
              {line.text}
              <EndedButtons line={line} shown={shown} other={other} versions={cut.versions} onGo={fromEnded} />
            </p>
          ))}
        </div>
      )}

      {cut.drafts.length > 0 && (
        <section className="secsection" aria-labelledby="cut-drafts">
          <div className="sectionhead">
            <h2 id="cut-drafts" ref={draftsHead} tabIndex={-1}>
              Drafts of this cut
            </h2>
            {underWay > 0 && <span className="faint mono">{underWay} under way</span>}
          </div>
          <div className="draftcards">
            {cut.drafts.map((draft) => (
              <DraftCard
                key={draft.id}
                draft={draft}
                busy={busy}
                canTryAgain={cut.can_draft}
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

      {cut.versions.length > 1 && (
        <div className="toolbar cut-versions" role="group" aria-label="Versions">
          <span className="compare-label-strong" aria-hidden="true">
            Versions
          </span>
          {cut.versions.map((version) => (
            <button
              key={version.id}
              type="button"
              className="btn"
              data-made={version.source}
              aria-pressed={version.id === shown.id}
              onClick={() => show(version)}
            >
              {versionButton(version)}
            </button>
          ))}
          <button
            type="button"
            className="btn cut-compare-toggle"
            aria-pressed={other !== null}
            aria-controls="cut-shots-section"
            onClick={toggleCompare}
            title={
              other
                ? "Back to this version's shot list alone"
                : "Put two versions side by side and see which shots were kept, trimmed, dropped or added"
            }
          >
            Compare
          </button>
        </div>
      )}

      {local && made && (
        <div className="card cut-draftnote">
          <p>
            <strong>A draft, not a decision.</strong> {made}{" "}
            {shown.demo && (
              <span className="pill" data-tone="warn">
                demo
              </span>
            )}
          </p>
          {shown.draft?.brief && (
            // The brief is what a person typed; shown as text all the same.
            <p className="cut-brief">“{shown.draft.brief}”</p>
          )}
        </div>
      )}
      {problems.length > 0 && (
        <div className="card cut-checked" role="status">
          <p>
            <strong>Changed when it was checked.</strong> FrameFound checks every in and out point
            the Editor gives against the footage.
          </p>
          <ul>
            {problems.map((problem, index) => (
              <li key={index}>{problem}</li>
            ))}
          </ul>
        </div>
      )}

      <PremiereDownload
        cutId={shown.id}
        what={`version ${shown.version}`}
        firstOfDraft={local && !shown.sent_at}
        canSend={canChange && shown.pieces.length > 0}
        busy={busy}
        onSent={load}
      >
        {canChange && (
          <button
            type="button"
            className="btn"
            disabled={busy || attaching || reviewWhy !== null}
            aria-describedby={reviewWhy ? "cut-review-why" : undefined}
            onClick={(event) => void review(shown, event.currentTarget)}
            title="Opens the chat panel on a new conversation with this version attached. Nothing is sent until you press Send there."
          >
            {attaching ? "Getting it ready…" : "Ask Claude to review"}
          </button>
        )}
        {canChange && (
          <button
            ref={againToggle}
            type="button"
            className="btn"
            aria-expanded={again !== null}
            aria-controls="cut-again"
            disabled={busy}
            onClick={() => toggleAgain(shown)}
            title="Asks the Editor, a model on this Mac, for another draft. It becomes the next version here; this one stays."
          >
            {local ? "Draft again" : "Draft a new version"}
          </button>
        )}
        {shown.can_delete && (
          <button
            type="button"
            className="btn compare-delete"
            disabled={busy}
            onClick={() => void remove(shown)}
          >
            {cut.versions.length > 1 ? "Delete this version" : "Delete this cut"}
          </button>
        )}
      </PremiereDownload>
      {canChange && reviewWhy && (
        // A disabled button cannot be reached to read its tooltip: the
        // reason is said here, with the way to put it right when there is one.
        <p className="faint cut-readonly" id="cut-review-why">
          Claude cannot be asked to review this: {reviewWhy}
          {claudeGate?.fix && (
            <>
              {" "}
              <Link href={claudeGate.fix.href} className="cutshot-link">
                {claudeGate.fix.label}
              </Link>
            </>
          )}
        </p>
      )}
      {readOnly && <p className="faint cut-readonly">This account can look but not change anything.</p>}

      <div id="cut-again" ref={againPanel} tabIndex={-1} className="cut-again" hidden={again === null}>
        {again &&
          revising &&
          (cut.editor.mode === "unavailable" ? (
            <div className="card" role="status">
              <p style={{ margin: 0 }}>{cut.editor.note ?? UNAVAILABLE}</p>
              <p className="faint" style={{ margin: "8px 0 0" }}>
                This cut can still be downloaded for Premiere here.
              </p>
            </div>
          ) : (
            <>
              <p className="faint cut-again-lead">
                The result is kept as the next version of this cut, as a new take on version{" "}
                {revising.version}
                {revising.id === shown.id ? "" : ` (the one on screen now is version ${shown.version})`}.
                Every version here stays as it is.
              </p>
              <DraftCutForm
                key={again.seed.key}
                editor={cut.editor}
                canDraft={cut.can_draft}
                readOnly={false}
                seed={again.seed}
                revises={revising.id}
                onDrafted={drafted}
                onGone={load}
              />
            </>
          ))}
      </div>

      <section className="secsection" id="cut-shots-section" aria-labelledby="cut-shots">
        <div className="sectionhead">
          <h2 id="cut-shots" ref={shotsHead} tabIndex={-1}>
            {older && newer ? `Version ${older.version} beside version ${newer.version}` : "Shots"}
          </h2>
          {!other && (
            <span className="faint mono">
              {shown.pieces.length} in version {shown.version}
            </span>
          )}
        </div>
        {older && newer ? (
          <CutCompare versions={cut.versions} older={older} newer={newer} onChoose={choose} />
        ) : (
          // Keyed by version: the viewer open on one version's shot does not
          // carry over to the shot at the same place in another.
          <CutShots key={shown.id} version={shown} />
        )}
      </section>

      {shown.draft && shown.draft.candidates.length > 0 && <ChosenFrom draft={shown.draft} />}
    </Shell>
  );
}

/**
 * After the sentence that says a version appeared: the way to it, and the
 * way to see it beside the one it revises (or, when that one is gone or was
 * never recorded, beside the one on screen). Neither is offered for what is
 * already on screen; the page never changes version by itself.
 */
function EndedButtons({
  line,
  shown,
  other,
  versions,
  onGo,
}: {
  line: Ended;
  shown: CutVersion;
  other: CutVersion | null;
  versions: CutVersion[];
  onGo: (version: number, beside: number | null) => void;
}) {
  const number = line.show;
  const kept = (candidate: number | null): candidate is number =>
    candidate !== null && versions.some((version) => version.version === candidate);
  if (!kept(number)) return null;
  const beside = [line.parent, shown.version].find(
    (candidate): candidate is number => kept(candidate) && candidate !== number,
  );
  const pair = other ? [shown.version, other.version] : [];
  const onScreen = shown.version === number || other?.version === number;
  const compared = beside !== undefined && pair.includes(number) && pair.includes(beside);
  return (
    <>
      {!onScreen && (
        <>
          {" "}
          <button type="button" className="btn" onClick={() => onGo(number, null)}>
            Show version {number}
          </button>
        </>
      )}
      {beside !== undefined && !compared && (
        <>
          {" "}
          <button type="button" className="btn" onClick={() => onGo(number, beside)}>
            Compare with version {beside}
          </button>
        </>
      )}
    </>
  );
}

/**
 * What the Editor was offered, with a tick on what it took — so what it
 * passed over can be seen without leaving the page. The line is what the
 * Editor was told about the shot; it holds file names and is shown as text.
 */
function ChosenFrom({ draft }: { draft: CutMadeFrom }) {
  const chosen = draft.candidates.filter((candidate) => candidate.chosen).length;
  return (
    <details className="disclosure cut-offered">
      <summary>
        What it chose from ({draft.candidates.length} {draft.candidates.length === 1 ? "shot" : "shots"})
      </summary>
      <p className="faint cut-offered-lead">
        Every shot the Editor was offered, as it was described to it. {chosen} of them{" "}
        {chosen === 1 ? "is" : "are"} in this version.
      </p>
      <div className="tablewrap card cuts-table">
        <table className="table">
          <thead>
            <tr>
              <th>No.</th>
              <th>Clip</th>
              <th>In–out</th>
              <th>What the Editor was told</th>
              <th>Chosen</th>
            </tr>
          </thead>
          <tbody>
            {draft.candidates.map((candidate) => (
              <tr key={candidate.n} data-chosen={candidate.chosen ? "true" : undefined}>
                <td className="mono">{candidate.n}</td>
                <td>
                  <Link
                    href={momentHref(candidate.asset_id, candidate.start_s)}
                    prefetch={false}
                    className="cutshot-link"
                  >
                    {candidate.clip}
                  </Link>
                </td>
                <td className="mono cut-offered-span">{spanText(candidate.start_s, candidate.end_s)}</td>
                <td className="cut-offered-line">{candidate.line}</td>
                <td>
                  {candidate.chosen ? (
                    <>
                      <span aria-hidden="true">✓</span>
                      <span className="compare-sr">chosen</span>
                    </>
                  ) : (
                    <span className="compare-sr">not chosen</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

export default function CutPage() {
  // useSearchParams needs a Suspense boundary round it (as the Cuts list has).
  return (
    <Suspense>
      <CutInner />
    </Suspense>
  );
}
