"use client";

// AI models: the models that run on the Mac itself — adding them, giving
// them a role, testing them, and sharing the Mac's memory with Premiere.
//
// The server decides what this page is (`mode`), never the browser: on the
// container server models cannot run and the page says so in one card; on a
// development machine in demonstration mode a banner says nothing is real.
//
// One request draws everything (`api.localModels()`), read again every
// 2.5 s while something moves and every 15 s otherwise, never while the tab
// is hidden, and at once after every action. Nothing about a run is kept in
// the tab: a download started here, left, and come back to still shows its
// bar, because the bar is the server's (the Basemaps and Slideshows pages
// learned that a run kept in the tab is lost on reload).
//
// Every change goes through one `act()`, which shows the server's own
// sentence when it says no — at the top of the section the change belongs
// to, not at the top of a long page — and asks a 428's question with
// `confirm()` before sending the same request again with the yes.

import { useCallback, useEffect, useRef, useState } from "react";

import Shell from "@/components/Shell";
import ModelsActivity from "@/components/ModelsActivity";
import ModelsAdd from "@/components/ModelsAdd";
import ModelsCompare, { ModelsCompareSaved } from "@/components/ModelsCompare";
import ModelsInstalled from "@/components/ModelsInstalled";
import ModelsLooking from "@/components/ModelsLooking";
import ModelsPublishers from "@/components/ModelsPublishers";
import ModelsRoles from "@/components/ModelsRoles";
import ModelsRuntimes from "@/components/ModelsRuntimes";
import ModelsSharing from "@/components/ModelsSharing";
import ModelsStart from "@/components/ModelsStart";
import { ApiError, api, type LocalModelsOverview } from "@/lib/api";
import { usePageTitle } from "@/lib/hooks";
import {
  DECLINED,
  JUMPS,
  clockLine,
  endedJobs,
  endedSentence,
  leadSentence,
  readAgainIn,
  rereads,
  sendAsking,
  type ModelsAct,
  type ModelsSectionId,
  type Rereader,
} from "@/lib/localModels";

type Said = { where: ModelsSectionId; text: string; tone: "alert" | "status" };

// The jump bar, with Compare after Models, where its card sits. Compare has
// no actions on this page (its card only leads to /models/compare), so it is
// a section to jump to but not one `act()` reports into.
const JUMPS_HERE: ReadonlyArray<{ id: string; label: string }> = JUMPS.flatMap((jump) =>
  jump.id === "models" ? [jump, { id: "compare", label: "Compare" }] : [jump],
);

export default function ModelsPage() {
  usePageTitle("AI models");
  const [overview, setOverview] = useState<LocalModelsOverview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<Said | null>(null);
  const [ended, setEnded] = useState<string | null>(null);
  // Getting started's "Choose one" asks Add to fill in the suggested model.
  const [suggest, setSuggest] = useState(0);
  const previous = useRef<LocalModelsOverview | null>(null);
  const reader = useRef<Rereader<LocalModelsOverview> | null>(null);

  // Read now and on a timer armed again by the end of every read, worked or
  // failed (`rereads`): quickly while something moves, slowly otherwise,
  // never where models cannot run. Only the newest read draws. A hidden tab
  // lets its turn pass; showing the tab again reads at once.
  useEffect(() => {
    const own = rereads<LocalModelsOverview>({
      read: () => api.localModels(),
      answered: (next) => {
        const before = previous.current;
        if (before) {
          const finished = endedJobs(before.jobs, next.jobs);
          if (finished.length) setEnded(finished.map(endedSentence).join(" "));
        }
        previous.current = next;
        setOverview(next);
        setLoadError(null);
      },
      failed: (err) => {
        // The last good overview stays on screen beneath the sentence.
        setLoadError(
          err instanceof ApiError
            ? err.message
            : "FrameFound could not be reached to read the AI models. It tries again shortly.",
        );
      },
      every: readAgainIn,
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
  }, []);

  const load = useCallback(async () => {
    await reader.current?.now();
  }, []);

  const act: ModelsAct = useCallback(
    async (where, work, done, declined) => {
      setBusy(true);
      setSaid(null);
      let worked = false;
      try {
        // A 428 is a question, not a refusal: nothing was done yet.
        const result = await sendAsking(work, (question) => window.confirm(question));
        if (result === DECLINED) {
          declined?.();
        } else {
          worked = true;
          const sentence = typeof done === "function" ? done(result) : done;
          if (sentence) setSaid({ where, text: sentence, tone: "status" });
        }
      } catch (err) {
        setSaid({
          where,
          text: err instanceof Error && err.message ? err.message : "That did not work. Try again.",
          tone: "alert",
        });
      } finally {
        setBusy(false);
        await load();
      }
      return worked;
    },
    [load],
  );

  // What the last action said, at the top of the section it belongs to.
  function saidIn(where: ModelsSectionId) {
    if (!said || said.where !== where) return null;
    return said.tone === "alert" ? (
      <div className="card" role="alert" style={{ borderColor: "var(--ember)", margin: "0 0 12px" }}>
        {said.text}
      </div>
    ) : (
      <div className="card" role="status" style={{ borderColor: "var(--sage)", margin: "0 0 12px" }}>
        {said.text}
      </div>
    );
  }

  const heading = (
    <div className="sectionhead" style={{ marginTop: 0 }}>
      <h2>AI models</h2>
    </div>
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

  if (overview.mode === "unavailable") {
    return (
      <Shell>
        {heading}
        <div className="card" role="status">
          <p style={{ margin: 0 }}>
            <strong>Models run only on a Mac.</strong>{" "}
            {overview.note ??
              "This FrameFound runs in containers on a Linux server, where the software that runs these models does not exist. Everything else works as before."}
          </p>
          <p className="muted" style={{ margin: "8px 0 0" }}>
            When FrameFound is installed on the Mac mini, this is the page where models are added.
          </p>
        </div>
        <ModelsCompareSaved />
      </Shell>
    );
  }

  const [demoLead, demoRest] = leadSentence(overview.note ?? "Demonstration only.");

  return (
    <Shell>
      {heading}
      <p className="faint mono" style={{ margin: "-6px 0 0", fontSize: "0.8rem" }}>
        {clockLine(overview.now, overview.settings)}
      </p>

      {overview.mode === "demo" && (
        <div className="demo-banner" role="status">
          <strong>{demoLead}</strong> {demoRest} It is here so the page can be looked at on a
          development machine.
        </div>
      )}
      {loadError && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)", marginTop: 14 }}>
          {loadError}
        </div>
      )}
      {!overview.can_manage && (
        <p className="faint" style={{ margin: "12px 0 0" }}>
          Only an administrator can change anything here.
        </p>
      )}
      {ended && (
        <p role="status" style={{ margin: "12px 0 0", color: "var(--sage)" }}>
          {ended}
        </p>
      )}

      <nav className="jumpbar" aria-label="Sections of this page">
        {JUMPS_HERE.map((jump) => (
          <a key={jump.id} href={`#${jump.id}`}>
            {jump.label}
          </a>
        ))}
      </nav>

      <section id="start" className="secsection">
        {saidIn("start")}
        <ModelsStart
          overview={overview}
          act={act}
          busy={busy}
          onChooseModel={() => setSuggest((count) => count + 1)}
        />
      </section>

      <section id="roles" className="secsection">
        <div className="sectionhead">
          <h2>Who does what</h2>
        </div>
        {saidIn("roles")}
        <ModelsRoles overview={overview} act={act} busy={busy} />
        <ModelsLooking />
      </section>

      <section id="models" className="secsection">
        <div className="sectionhead">
          <h2>Models</h2>
          <span className="faint mono">{overview.models.length} installed or on the way</span>
        </div>
        {saidIn("models")}
        <ModelsInstalled overview={overview} act={act} busy={busy} />
      </section>

      <section id="compare" className="secsection">
        <div className="sectionhead">
          <h2>Compare on your footage</h2>
        </div>
        <ModelsCompare />
      </section>

      <section id="add" className="secsection">
        <div className="sectionhead">
          <h2>Add a model</h2>
        </div>
        {saidIn("add")}
        <ModelsAdd overview={overview} act={act} busy={busy} suggest={suggest} />
      </section>

      <section id="software" className="secsection">
        <div className="sectionhead">
          <h2>Software that runs models</h2>
        </div>
        {saidIn("software")}
        <ModelsRuntimes overview={overview} act={act} busy={busy} />
      </section>

      <section id="sharing" className="secsection">
        <div className="sectionhead">
          <h2>Sharing this Mac</h2>
        </div>
        {saidIn("sharing")}
        <ModelsSharing overview={overview} act={act} busy={busy} />
      </section>

      <section id="publishers" className="secsection">
        <div className="sectionhead">
          <h2>Approved publishers</h2>
        </div>
        {saidIn("publishers")}
        <ModelsPublishers overview={overview} act={act} busy={busy} />
      </section>

      <section id="activity" className="secsection">
        <div className="sectionhead">
          <h2>Recent activity</h2>
        </div>
        {saidIn("activity")}
        <ModelsActivity overview={overview} act={act} busy={busy} />
      </section>
    </Shell>
  );
}
