"use client";

// A model's Test, under its row in Installed models: one question about a
// picture (or, for a model that reads no pictures, one in words), and what
// it answered — nothing it answers is saved anywhere.
//
// What the model was shown can always be looked at: a photograph from the
// catalogue opens its own page, and the picture FrameFound draws when there
// is none opens as it is. The model's words are shown as text, never as
// HTML: a model can write anything, markup included.
//
// The test is the server's (the model's newest test job), so a test started
// from Getting started, or before a reload, is found here all the same.

import { useEffect, useRef, useState } from "react";
import Link from "next/link";

import Thumb from "@/components/Thumb";
import ModelsProgress from "@/components/ModelsProgress";
import {
  api,
  modelSampleUrl,
  type LocalModel,
  type LocalModelTest,
  type LocalModelsOverview,
  type MediaType,
} from "@/lib/api";
import {
  aheadOf,
  focusIfLost,
  isActiveTest,
  notTestedSentence,
  orderText,
  randomPage,
  rollbackOffer,
  rollbackQuestion,
  rowName,
  testAnswer,
  testBlocker,
  testFacts,
  testPictureCaption,
  testSentence,
  type ModelsAct,
} from "@/lib/localModels";

const MEDIA: ReadonlySet<string> = new Set(["image", "video", "audio"]);

export default function ModelsTest({
  model,
  overview,
  act,
  busy,
}: {
  model: LocalModel;
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
}) {
  const test = model.test;
  const manage = overview.can_manage;
  const readsPictures = model.takes.includes("image");
  const blocked = testBlocker(model, overview.settings);
  // "Try another picture" goes once the new test is queued; the keyboard
  // goes to the panel, where the new test's progress now shows.
  const [queued, setQueued] = useState(0);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (queued) focusIfLost(panel.current);
  }, [queued]);

  function another() {
    act(
      "models",
      async () => {
        // Any ready photograph, so the next test is not the newest one again.
        const wanted = { media_type: "image", status: "ready", previewable: true, page_size: 1 };
        const first = await api.assets(wanted);
        const page = randomPage(first.total);
        const picked = page === null || page === 1 ? first : await api.assets({ ...wanted, page });
        return api.testModel(model.id, picked.items[0]?.id ?? null);
      },
      `Testing ${rowName(model)} on another picture.`,
    ).then((worked) => {
      if (worked) setQueued((count) => count + 1);
    });
  }

  return (
    <div className="models-test" ref={panel}>
      <p className="eyebrow" style={{ margin: 0 }}>
        Test {test?.demo && <span className="pill">demo</span>}
      </p>

      {!test ? (
        <p className="faint" style={{ margin: "6px 0 0" }}>
          {notTestedSentence(model)}
        </p>
      ) : isActiveTest(test) ? (
        <Running test={test} model={model} overview={overview} act={act} busy={busy} />
      ) : test.state === "done" ? (
        <Answered test={test} overview={overview} />
      ) : (
        <Ended test={test} model={model} overview={overview} act={act} busy={busy} />
      )}

      {manage && test && !isActiveTest(test) && readsPictures && (
        <div className="models-actions">
          <button
            type="button"
            className="btn"
            style={{ padding: "3px 10px" }}
            disabled={busy || !!blocked}
            title={blocked ?? "Ask it the same question about a photograph chosen at random"}
            onClick={another}
          >
            Try another picture
          </button>
        </div>
      )}
    </div>
  );
}

function Running({
  test,
  model,
  overview,
  act,
  busy,
}: {
  test: LocalModelTest;
  model: LocalModel;
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
}) {
  const job = overview.jobs.find((entry) => entry.id === test.job_id);
  if (!job) {
    return (
      <p className="models-progress-line">
        <span className="spinner" aria-hidden="true" />
        <span className="faint">{test.message || "Waiting its turn."}</span>
      </p>
    );
  }
  const ahead = aheadOf(job, overview.jobs);
  return (
    <ModelsProgress
      job={job}
      ahead={ahead}
      sentence={testSentence(job, model, overview.server, ahead)}
      where="models"
      act={act}
      canManage={overview.can_manage}
      busy={busy}
    />
  );
}

function Answered({ test, overview }: { test: LocalModelTest; overview: LocalModelsOverview }) {
  const answer = testAnswer(test);
  const facts = testFacts(test, overview.runtimes);
  return (
    <div className="models-test-result">
      <Picture test={test} />
      <div style={{ minWidth: 0 }}>
        <p className="faint" style={{ margin: 0, fontSize: "0.82rem" }}>
          The model&apos;s answer — nothing was saved
        </p>
        {answer?.form === "look" && (
          <>
            <p style={{ margin: "4px 0 0" }}>{answer.caption || "It wrote no caption."}</p>
            <div className="models-tile-pills" aria-label="Tags it suggested">
              {answer.tags.map((tag) => (
                <span className="pill models-suggested" key={tag}>
                  {tag}
                </span>
              ))}
            </div>
          </>
        )}
        {answer?.form === "order" && (
          <>
            <p style={{ margin: "4px 0 0" }}>
              It put the three shots in this order: {orderText(answer.order)}.
            </p>
            {answer.reason && <p style={{ margin: "4px 0 0" }}>{answer.reason}</p>}
          </>
        )}
        {answer?.form === "prose" && (
          <>
            <p style={{ margin: "4px 0 0", color: "var(--amber)" }}>
              The model answered, but not in the form asked for.
            </p>
            {/* Text, never HTML: React escapes it, and it scrolls in its box. */}
            <pre className="answerbox">{answer.raw || "(it wrote nothing)"}</pre>
          </>
        )}
        {facts.length > 0 && (
          <dl className="kv" style={{ marginTop: 12 }}>
            {facts.map(([label, value]) => (
              <Fact key={label} label={label} value={value} />
            ))}
          </dl>
        )}
      </div>
    </div>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </>
  );
}

function Picture({ test }: { test: LocalModelTest }) {
  const sample = test.sample;
  const caption = testPictureCaption(test);
  if (test.kind === "text" || !sample) {
    return (
      <p className="faint models-test-words" style={{ margin: 0 }}>
        {sample?.label ?? "A question in words"}: put three described shots in order for the
        opening of a property video.
      </p>
    );
  }
  if (sample.asset_id) {
    const media: MediaType = MEDIA.has(sample.media_type ?? "")
      ? (sample.media_type as MediaType)
      : "image";
    return (
      <figure className="models-test-figure">
        <Link
          href={`/assets/${sample.asset_id}`}
          target="_blank"
          className="models-test-picture"
          title="Open this picture's page"
        >
          <Thumb assetId={sample.asset_id} mediaType={media} status="ready" />
        </Link>
        <figcaption className="faint">{caption}</figcaption>
      </figure>
    );
  }
  return (
    <figure className="models-test-figure">
      <a
        href={modelSampleUrl()}
        target="_blank"
        rel="noreferrer"
        className="models-test-picture"
        title="Open the picture it was shown"
      >
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={modelSampleUrl()} alt="A picture FrameFound drew: a red barn on grass under a blue sky" />
      </a>
      <figcaption className="faint">{caption}</figcaption>
    </figure>
  );
}

function Ended({
  test,
  model,
  overview,
  act,
  busy,
}: {
  test: LocalModelTest;
  model: LocalModel;
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
}) {
  if (test.state === "cancelled") {
    return (
      <p className="faint" style={{ margin: "6px 0 0" }}>
        {test.message || "Cancelled."}
      </p>
    );
  }
  const offer = rollbackOffer(model, overview.runtimes);
  function rollBack(name: string, label: string, version: string) {
    if (!window.confirm(rollbackQuestion(version))) return;
    act("models", () => api.rollbackRuntime(name), `${label} is back to ${version}.`);
  }
  return (
    <div>
      <p style={{ margin: "6px 0 0", color: "var(--ember)" }}>{test.message || "It failed."}</p>
      {test.detail && (
        <details className="disclosure" style={{ marginTop: 8 }}>
          <summary>Details</summary>
          <pre className="answerbox">{test.detail}</pre>
        </details>
      )}
      {offer && (
        <div className="models-actions" style={{ alignItems: "center" }}>
          <span>{offer.sentence}</span>
          {offer.version && overview.can_manage && (
            <button
              type="button"
              className="btn"
              style={{ padding: "3px 10px" }}
              disabled={busy || isActiveTest(model.test)}
              onClick={() => {
                if (offer.version) rollBack(offer.runtime.name, offer.runtime.label, offer.version);
              }}
            >
              Roll back to {offer.version}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
