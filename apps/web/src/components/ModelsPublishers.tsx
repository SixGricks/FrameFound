"use client";

// Approved publishers: who may make the models this Mac runs. Three lists —
// makers (who trained a model), converters (who packaged one for the Mac)
// and blocked — and, folded away, the Hugging Face token a gated model needs.
//
// The lists decide approval everywhere at once: taking google off the makers
// stops every model from google, or built from theirs, from running. So a
// removal that leaves installed models without approval asks first, naming
// how many and which role is among them, and the answer afterwards names the
// models the server found it left unapproved. Nothing is deleted by a list
// change; putting the name back makes them run again.
//
// The built-in blocked names carry no remove button: blocked always wins,
// and those are not the owner's to unblock from here. There are forty-odd of
// them, so they sit folded under the owner's own additions.
//
// The token is never shown, not even masked: the server never sends it back.
// The field is for pasting a new one; the line beside it says only whether
// one is stored (or that the stored one can no longer be read).

import { useEffect, useRef, useState } from "react";
import Link from "next/link";

import { ApiError, api, type LocalModelsOverview, type LocalModelsPublishersSaved } from "@/lib/api";
import {
  TOKEN_NONE,
  TOKEN_REMOVE_QUESTION,
  TOKEN_STORED,
  addProblem,
  blockQuestion,
  listAfter,
  listPatch,
  publisherName,
  publishersSavedSentence,
  removeListQuestion,
  tokenSavedSentence,
  tokenState,
  type ModelsAct,
  type PublisherList,
} from "@/lib/localModels";

type Note = {
  tone: "alert" | "status";
  text: string;
  /** Shown only while the token's state still reads this: a note about a
   *  token that was since replaced or removed elsewhere would mislead. */
  while?: string;
};

const LISTS: ReadonlyArray<{ list: PublisherList; title: string; about: string; one: string }> = [
  { list: "makers", title: "Makers", about: "Who trained the model.", one: "maker" },
  {
    list: "converters",
    title: "Converters",
    about:
      "Who packaged it for the Mac. A conversion is accepted only if it says which model it was made from, and that model is a maker's.",
    one: "converter",
  },
  {
    list: "blocked",
    title: "Blocked",
    about:
      "Never, whatever the other two lists say. A model from one of these, or built from one of theirs, is refused.",
    one: "blocked publisher",
  },
];

export default function ModelsPublishers({
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
      <div className="publists">
        {LISTS.map((entry) => (
          <ListCard key={entry.list} {...entry} overview={overview} act={act} busy={busy} />
        ))}
      </div>
      <TokenCard overview={overview} act={act} busy={busy} />
      <p className="faint" style={{ margin: "12px 0 0", fontSize: "0.86rem" }}>
        These lists decide which models may be downloaded and run. Library and model locations are
        on the <Link href="/storage">Storage</Link> page.
      </p>
    </>
  );
}

function ListCard({
  list,
  title,
  about,
  one,
  overview,
  act,
  busy,
}: {
  list: PublisherList;
  title: string;
  about: string;
  one: string;
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
}) {
  const [text, setText] = useState("");
  const [note, setNote] = useState<Note | null>(null);
  const field = useRef<HTMLInputElement>(null);
  const manage = overview.can_manage;
  const { publishers, models, roles } = overview;
  const names =
    list === "blocked"
      ? publishers.blocked
      : publishers[list].map((name) => ({ name, built_in: false }));
  const own = names.filter((entry) => !entry.built_in);
  const builtIn = names.filter((entry) => entry.built_in);

  // The server's refusal goes beside this list's field, where the owner is
  // looking; only a failure to reach the server at all is left to the line
  // at the top of the section. Resolves true only when the server took the
  // change: a refused name stays in the field so it can be corrected.
  async function save(change: "added" | "removed", typed: string, name: string): Promise<boolean> {
    setNote(null);
    let accepted = false;
    await act(
      "publishers",
      async (): Promise<LocalModelsPublishersSaved | null> => {
        try {
          // The list is sent whole, so it is built from the lists as they
          // are now: another window may have changed them since this one
          // last read, and a stale list would undo that (a block, even).
          const fresh = (await api.localModels()).publishers;
          return await api.setModelPublishers(listPatch(list, listAfter(fresh, list, change, typed)));
        } catch (err) {
          if (!(err instanceof ApiError)) throw err;
          setNote({ tone: "alert", text: err.message });
          return null;
        }
      },
      (saved) => {
        if (saved) {
          accepted = true;
          setNote({ tone: "status", text: publishersSavedSentence(change, list, name, saved) });
        }
        return null;
      },
    );
    return accepted;
  }

  function add(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    const problem = addProblem(publishers, list, text);
    if (problem) {
      setNote({ tone: "alert", text: problem });
      return;
    }
    const name = publisherName(text);
    if (list === "blocked") {
      const question = blockQuestion(name, models, roles);
      if (question && !window.confirm(question)) return;
    }
    save("added", text, name).then((accepted) => {
      if (accepted) setText("");
    });
  }

  function remove(name: string) {
    if (busy) return;
    if (list !== "blocked") {
      const question = removeListQuestion(name, models, roles);
      if (question && !window.confirm(question)) return;
    }
    save("removed", name, name).then(() => {
      // The button that was pressed is gone; keep the keyboard in this list.
      field.current?.focus();
    });
  }

  return (
    <div className="card publist">
      <h3 style={{ margin: 0 }}>{title}</h3>
      <p className="faint" style={{ margin: "4px 0 10px", fontSize: "0.86rem" }}>
        {about}
      </p>
      {own.length ? (
        <ul className="publist-names" aria-label={list === "blocked" ? "Blocked by you" : title}>
          {own.map((entry) => (
            <li key={entry.name}>
              <span className="pill publist-name">
                {entry.name}
                {manage && (
                  <button
                    type="button"
                    className="chipx"
                    aria-label={`Remove ${entry.name}`}
                    title={`Remove ${entry.name}`}
                    disabled={busy}
                    onClick={() => remove(entry.name)}
                  >
                    ×
                  </button>
                )}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="faint" style={{ margin: 0 }}>
          {list === "blocked" ? "None added by you." : "None."}
        </p>
      )}
      {manage && (
        <form className="publist-add" onSubmit={add}>
          <input
            ref={field}
            className="input mono"
            aria-label={`Add a ${one}`}
            placeholder={list === "converters" ? "mlx-community" : list === "makers" ? "google" : "publisher"}
            autoComplete="off"
            spellCheck={false}
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              setNote(null);
            }}
          />
          <button type="submit" className="btn" disabled={busy || !text.trim()}>
            Add
          </button>
        </form>
      )}
      {note && (
        <p
          role={note.tone}
          className="models-save-note publist-note"
          data-tone={note.tone === "alert" ? "bad" : "ok"}
        >
          {note.text}
        </p>
      )}
      {/* Forty-odd built-in names would bury the owner's own few and push the
          field down, so they come last, folded — one click from view, and
          never removable. */}
      {builtIn.length > 0 && (
        <details className="disclosure" style={{ marginTop: 10, fontSize: "0.86rem" }}>
          <summary>
            Built in: {builtIn.length} publishers that are never approved
          </summary>
          <ul className="publist-names" aria-label="Blocked, built in">
            {builtIn.map((entry) => (
              <li key={entry.name}>
                <span className="pill publist-name" title="Built in to FrameFound; it cannot be removed">
                  {entry.name}
                  <small className="faint">built in</small>
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function TokenCard({
  overview,
  act,
  busy,
}: {
  overview: LocalModelsOverview;
  act: ModelsAct;
  busy: boolean;
}) {
  const [token, setToken] = useState("");
  const [note, setNote] = useState<Note | null>(null);
  const { settings } = overview;
  const state = tokenState(settings);
  const manage = overview.can_manage;
  const stored = settings.hf_token_configured || !!settings.hf_token_problem;
  const disclosure = useRef<HTMLDetailsElement>(null);
  const unreadable = !!settings.hf_token_problem;

  // Opened by itself when the stored token can no longer be read, because
  // the way out (paste it again, or remove it) is inside. Only opened, never
  // closed from here: closing it under the owner after a save would hide
  // the answer they are reading.
  useEffect(() => {
    if (unreadable && disclosure.current) disclosure.current.open = true;
  }, [unreadable]);

  function send(value: string) {
    setNote(null);
    return act(
      "publishers",
      async (): Promise<boolean> => {
        try {
          await api.setModelToken(value);
          return true;
        } catch (err) {
          if (!(err instanceof ApiError)) throw err;
          setNote({ tone: "alert", text: err.message, while: state.text });
          return false;
        }
      },
      (saved) => {
        if (saved) {
          // Nothing of the token stays in the page once it is stored.
          setToken("");
          setNote({
            tone: "status",
            text: tokenSavedSentence(value),
            while: value ? TOKEN_STORED : TOKEN_NONE,
          });
        }
        return null;
      },
    );
  }

  return (
    <div className="card" style={{ marginTop: 14 }}>
      <details className="disclosure" ref={disclosure}>
        <summary>
          Hugging Face account — only for gated models{" "}
          <span className="faint" style={{ fontWeight: 400, fontSize: "0.86rem" }}>
            · {state.tone === "bad" ? "the stored token cannot be read" : state.text}
          </span>
        </summary>
        <p style={{ margin: "8px 0 0", fontSize: "0.92rem" }}>
          A few models ask you to accept their terms on Hugging Face before they can be downloaded.
          Nothing FrameFound recommends needs this. If you do need one: accept the terms on the
          model&apos;s page, make a <em>read</em> token in your Hugging Face settings, and paste it
          here. It is stored sealed and is only ever sent to Hugging Face.
        </p>
        <p
          className={state.tone === null ? "faint" : undefined}
          style={{
            margin: "8px 0 0",
            fontSize: "0.9rem",
            color: state.tone === "bad" ? "var(--ember)" : state.tone === "ok" ? "var(--sage)" : undefined,
          }}
        >
          {state.text}
        </p>
        {manage && (
          <form
            className="publist-add"
            onSubmit={(event) => {
              event.preventDefault();
              if (!busy && token.trim()) send(token);
            }}
          >
            <input
              className="input mono"
              type="password"
              aria-label="Hugging Face read token"
              placeholder={stored ? "Replace the stored token…" : "Paste a read token…"}
              // Not a sign-in field: the browser must not fill in FrameFound's
              // saved password here, nor offer to save the token as that
              // password when the field is emptied after Save (which looks
              // like a sign-in to it).
              // Browsers ignore "off" on a password field; "new-password" is
              // the value they honour. The last two attributes ask password
              // manager extensions to leave it alone too.
              name="hf-token"
              autoComplete="new-password"
              data-1p-ignore=""
              data-lpignore="true"
              spellCheck={false}
              value={token}
              onChange={(event) => {
                setToken(event.target.value);
                setNote(null);
              }}
            />
            <button type="submit" className="btn btn-primary" disabled={busy || !token.trim()}>
              Save
            </button>
            {stored && (
              <button
                type="button"
                className="btn"
                disabled={busy}
                onClick={() => {
                  if (window.confirm(TOKEN_REMOVE_QUESTION)) send("");
                }}
              >
                Remove
              </button>
            )}
          </form>
        )}
        {note && (!note.while || note.while === state.text) && (
          <p
            role={note.tone}
            className="models-save-note publist-note"
            data-tone={note.tone === "alert" ? "bad" : "ok"}
          >
            {note.text}
          </p>
        )}
      </details>
    </div>
  );
}
