"use client";

// The answers of one comparison, item by item: a row per item and a cell per
// model, beside what the catalogue already knew about the item — the tags a
// person said yes or no to, and whether the shot ended up in a finished cut.
// This is where the figures are checked by eye.
//
// The way to the footage is the item's stills. A plain click opens the viewer
// over the page at the shot's start, with every model's answer beside it, and
// ← → step through the rows as they are shown; a modified or middle click
// opens the asset's own page in a new tab, as everywhere else. Nothing else on
// a row competes with that click: there is no selection here.
//
// Every word a model wrote is shown as text, never as HTML — tags, captions,
// reasons, and what it wrote when it did not answer in the form asked for.
// Items may be ordered by one model's score; the models themselves never are.

import Link from "next/link";
import { useState } from "react";

import { DemoPill } from "@/components/CompareMeasures";
import LookFrame from "@/components/LookFrame";
import MediaPreview from "@/components/MediaPreview";
import type {
  ComparePicture,
  Comparison,
  ComparisonAnswer,
  ComparisonEntry,
  ComparisonItem,
  ComparisonItems,
  MediaType,
} from "@/lib/api";
import {
  AS_SHOT,
  NO_TRUTH_HERE,
  NOT_ASKED,
  SHOW_CHOICES,
  answerFoot,
  assetHref,
  canOpen,
  failureSentence,
  goneLine,
  keep,
  lookingAt,
  notCountedLine,
  nothingShown,
  orderChoices,
  orderOf,
  rulingMark,
  scoreLine,
  showCounts,
  sorted,
  spanWords,
  sureTitle,
  viewerSteps,
  type Order,
  type Show,
} from "@/lib/compare";

/** The id of the section's heading, where focus goes when nothing better is left. */
export const ANSWERS_HEAD = "compare-answers-head";

function mediaTypeOf(item: ComparisonItem): MediaType {
  return item.media_type === "image" ? "image" : item.media_type === "audio" ? "audio" : "video";
}

export default function CompareAnswers({
  comparison,
  items,
  problem,
}: {
  comparison: Comparison;
  /** Null until the first read answers. */
  items: ComparisonItems | null;
  /** Why the last read failed; the last good items stay beneath it. */
  problem: string | null;
}) {
  const [show, setShow] = useState<Show>("all");
  const [chosenOrder, setOrder] = useState<Order>(AS_SHOT);
  const [viewing, setViewing] = useState<string | null>(null);

  const entries = comparison.entries;
  const entryIds = entries.map((entry) => entry.id);
  const order = orderOf(chosenOrder, entries);

  const alert = problem && (
    <div className="card" role="alert" style={{ borderColor: "var(--ember)", margin: "0 0 12px" }}>
      {problem}
    </div>
  );

  if (!items) {
    return alert || <div className="empty">Loading…</div>;
  }

  const all = items.items;
  const counts = showCounts(all, entryIds);
  const rows = sorted(
    all.filter((item) => keep(item, show, entryIds)),
    order,
  );
  const looking = new Map(entries.map((entry) => [entry.id, lookingAt(all, entry)]));

  const viewed = viewing ? (all.find((item) => item.id === viewing && canOpen(item)) ?? null) : null;
  const steps = viewed ? viewerSteps(rows, viewed.id) : null;

  function focusAfterViewing(): HTMLElement | null {
    const tile = viewed ? document.querySelector<HTMLElement>(`[data-look="item:${viewed.id}"]`) : null;
    return tile ?? document.getElementById(ANSWERS_HEAD);
  }

  return (
    <>
      {alert}
      <div className="toolbar compare-answers-tools">
        <div className="compare-show" role="group" aria-label="Show">
          <span className="compare-label-strong" aria-hidden="true">
            Show
          </span>
          {SHOW_CHOICES.map((choice) => (
            <button
              key={choice.value}
              type="button"
              className="btn"
              aria-pressed={show === choice.value}
              onClick={() => setShow(choice.value)}
            >
              {choice.label} <span className="mono compare-count">{counts[choice.value]}</span>
            </button>
          ))}
        </div>
        <label className="compare-order">
          <span className="compare-label-strong">Order</span>
          <select className="select" value={order} onChange={(event) => setOrder(event.target.value)}>
            {orderChoices(entries).map((choice) => (
              <option key={choice.value} value={choice.value}>
                {choice.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {rows.length === 0 ? (
        <div className="empty">
          {all.length === 0 ? "This comparison has no items." : nothingShown(show)}{" "}
          {show !== "all" && (
            <button type="button" className="btn" onClick={() => setShow("all")}>
              Show all
            </button>
          )}
        </div>
      ) : (
        <div className="tablewrap compare-answers-wrap">
          <div
            className="compare-answers"
            data-models={entries.length}
            style={{ ["--compare-models" as string]: Math.max(1, entries.length) }}
          >
            <div className="comparerow comparerow-head" aria-hidden="true">
              <div className="comparecell">Item</div>
              {entries.map((entry) => (
                <div key={entry.id} className="comparecell">
                  {entry.name} {entry.demo && <DemoPill />}
                </div>
              ))}
            </div>
            {rows.map((item) => (
              <Row
                key={item.id}
                item={item}
                entries={entries}
                looking={looking}
                onLook={() => setViewing(item.id)}
              />
            ))}
          </div>
        </div>
      )}

      {viewed && viewed.asset_id && (
        // Mounted once, after the grid — never inside a row.
        <MediaPreview
          assetId={viewed.asset_id}
          mediaType={mediaTypeOf(viewed)}
          filename={viewed.filename}
          startAt={viewed.start_s ?? undefined}
          // Two shots of one clip side by side: stepping between them moves
          // the clip to the next shot's start, even at the same second.
          momentKey={viewed.id}
          detail={[spanWords(viewed.start_s, viewed.end_s), viewed.folder].filter(Boolean).join(" · ") || undefined}
          position={steps?.position ?? undefined}
          onClose={() => setViewing(null)}
          onPrev={steps?.prev ? () => setViewing(steps.prev?.id ?? null) : undefined}
          onNext={steps?.next ? () => setViewing(steps.next?.id ?? null) : undefined}
          returnFocus={focusAfterViewing}
        >
          <ViewerAnswers item={viewed} entries={entries} />
        </MediaPreview>
      )}
    </>
  );
}

// ---- one row -------------------------------------------------------------------------

function Row({
  item,
  entries,
  looking,
  onLook,
}: {
  item: ComparisonItem;
  entries: ComparisonEntry[];
  looking: Map<string, string | null>;
  onLook: () => void;
}) {
  const span = spanWords(item.start_s, item.end_s);
  const href = assetHref(item);
  const open = canOpen(item);
  const counted = notCountedLine(item, entries);
  const nameId = `compare-item-${item.id}`;
  const stills = <Stills pictures={item.pictures} />;

  return (
    <article className="comparerow" aria-labelledby={nameId}>
      <div className="comparecell compare-item">
        {open && item.asset_id ? (
          <LookFrame
            assetId={item.asset_id}
            mediaType={mediaTypeOf(item)}
            // The label a screen reader hears: two shots of one clip are two
            // different things to look at.
            filename={span ? `${item.filename}, ${span}` : item.filename}
            focusKey={`item:${item.id}`}
            hint={item.kind === "shot" ? "click to watch from the shot's start" : "click to look"}
            onLook={onLook}
            picture={stills}
            // A modified or middle click opens the page at the shot, as the
            // filename link below does — not at the clip's 0:00.
            href={href ?? undefined}
          />
        ) : (
          <div className="tile-frame compare-gone-frame">{stills}</div>
        )}
        <p className="compare-item-name">
          {open && href ? (
            <Link id={nameId} href={href} target="_blank" rel="noreferrer" prefetch={false} className="compare-link">
              {item.filename}
            </Link>
          ) : (
            <span id={nameId}>{item.filename}</span>
          )}
          {span && <span className="faint mono"> {span}</span>}
        </p>
        {!open && <p className="faint compare-item-line">{goneLine(item.kind)}</p>}
        {item.folder && <p className="faint mono compare-item-line">{item.folder}</p>}
        {item.kind === "shot" && item.facts_line && (
          <p className="faint compare-item-line">
            What every model was told: <span className="compare-facts">{item.facts_line}</span>
          </p>
        )}
        <Truth item={item} />
        {item.used !== null && (
          <p className="compare-item-line">
            {item.used ? (
              <>
                <span className="pill" data-tone="ok" title={item.used_in.join(" · ") || undefined}>
                  used
                </span>
                {item.used_in.length > 0 && <span className="faint compare-used-in"> in {item.used_in.join(" · ")}</span>}
              </>
            ) : (
              <span className="pill">not used</span>
            )}
          </p>
        )}
        {counted && <p className="faint compare-item-line">{counted}</p>}
      </div>
      {entries.map((entry) => (
        <div key={entry.id} className="comparecell compare-answer">
          <span className="comparecell-name">{entry.name}</span>
          {/* Every answer of a demonstration model is marked as made up. */}
          {entry.demo && <DemoPill />}
          <AnswerCell answer={item.answers[entry.id]} lookingNow={looking.get(entry.id) === item.id} />
        </div>
      ))}
    </article>
  );
}

/** The stills a model was shown, frozen when the comparison was made. */
function Stills({ pictures }: { pictures: ComparePicture[] }) {
  return (
    <span className="compare-stills" style={{ ["--compare-stills" as string]: Math.max(1, pictures.length) }}>
      {pictures.length === 0 ? (
        <span className="placeholder">picture missing</span>
      ) : (
        pictures.map((picture) => <Still key={picture.n} url={picture.url} />)
      )}
    </span>
  );
}

function Still({ url }: { url: string }) {
  const [missing, setMissing] = useState(false);
  if (missing) return <span className="placeholder compare-still-missing">picture missing</span>;
  return (
    // Served by our own session-guarded route, as every picture in the app.
    // eslint-disable-next-line @next/next/no-img-element
    <img src={url} alt="" loading="lazy" onError={() => setMissing(true)} />
  );
}

/** What a person had decided about this footage's tags. */
function Truth({ item }: { item: ComparisonItem }) {
  const { yes, no } = item.truth;
  if (!yes.length && !no.length) return <p className="faint compare-item-line">{NO_TRUTH_HERE}</p>;
  return (
    <>
      {yes.length > 0 && (
        <p className="compare-item-line compare-truth">
          <span className="faint">You said yes:</span>{" "}
          {yes.map((name) => (
            <span key={name} className="pill compare-truth-tag">
              {name}
            </span>
          ))}
        </p>
      )}
      {no.length > 0 && (
        <p className="compare-item-line compare-truth">
          <span className="faint">You said no:</span>{" "}
          {no.map((name) => (
            <s key={name} className="pill compare-truth-tag">
              {name}
            </s>
          ))}
        </p>
      )}
    </>
  );
}

// ---- one model's answer --------------------------------------------------------------

function AnswerCell({ answer, lookingNow }: { answer: ComparisonAnswer | undefined; lookingNow: boolean }) {
  if (!answer) {
    return lookingNow ? (
      <p className="compare-counting compare-looking">
        <span className="spinner" aria-hidden="true" /> Looking at it now…
      </p>
    ) : (
      <p className="faint" title="Not answered yet">
        {NOT_ASKED}
      </p>
    );
  }
  const foot = answerFoot(answer);
  if (!answer.ok) {
    return (
      <>
        <p className="compare-said" data-tone="bad">
          {failureSentence(answer)}
        </p>
        {answer.raw && (
          <details className="disclosure compare-detail">
            <summary>What it wrote</summary>
            {/* Its own words, as text: never read as HTML or used for anything. */}
            <pre className="answerbox">{answer.raw}</pre>
          </details>
        )}
        {foot && <p className="faint mono compare-answer-foot">{foot}</p>}
      </>
    );
  }
  const line = scoreLine(answer);
  return (
    <>
      {answer.tags.length > 0 && (
        <ul className="compare-tags" aria-label="Tags it proposed">
          {answer.tags.map((tag, at) => {
            const mark = rulingMark(tag.ruling);
            return (
              <li key={`${tag.name}-${at}`}>
                <span className="pill compare-tag" data-ruling={tag.ruling ?? undefined} title={sureTitle(tag.confidence) ?? undefined}>
                  {mark && <span aria-hidden="true">{mark.mark}</span>}
                  {tag.name}
                  {mark && <span className="compare-sr"> ({mark.words})</span>}
                  {!tag.in_vocabulary && (
                    <small className="compare-new" title="A word the catalogue does not have">
                      new
                    </small>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {answer.caption && <p className="compare-caption">{answer.caption}</p>}
      {line && <p className="compare-score">{line}</p>}
      {foot && <p className="faint mono compare-answer-foot">{foot}</p>}
    </>
  );
}

/** Beside the footage in the viewer: each model's score and caption for it. */
function ViewerAnswers({ item, entries }: { item: ComparisonItem; entries: ComparisonEntry[] }) {
  if (!entries.length) return null;
  return (
    <div className="compare-viewer-answers">
      {entries.map((entry) => {
        const answer = item.answers[entry.id];
        return (
          <div key={entry.id} className="compare-viewer-answer">
            <strong>{entry.name}</strong> {entry.demo && <DemoPill />}{" "}
            {!answer ? (
              <span className="faint">not answered yet</span>
            ) : !answer.ok ? (
              <span className="compare-said" data-tone="bad">
                {failureSentence(answer)}
              </span>
            ) : (
              <>
                {scoreLine(answer) && <span>{scoreLine(answer)}</span>}
                {answer.caption && <span className="muted compare-viewer-caption">{answer.caption}</span>}
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
