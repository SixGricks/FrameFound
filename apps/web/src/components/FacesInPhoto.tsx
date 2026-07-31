"use client";

// Naming the people in a photograph, on the photograph.
//
// The People page is a review queue — one person's faces, in bulk. That is the
// right shape for working through a backlog and the wrong shape for the moment
// you are *looking at a picture* and can see who is in it. Asking someone to
// memorise a face, navigate away, find the right cluster and confirm is making
// them carry context the image was already showing them.
//
// So the boxes are drawn over the picture and clicking one asks who it is.
//
// The default assumption is that the person is **already known**: the top
// guess is offered as a single button, alternatives sit under it, and naming
// somebody new is the last option rather than the first. That is the inverse
// of the review queue's ordering, and deliberately so — after a week of use,
// a face belonging to nobody is the exception.

import { useCallback, useEffect, useState } from "react";

import Autocomplete from "@/components/Autocomplete";
import { api, mediaUrl, type FaceInPhoto, type FacesInPhoto as Payload } from "@/lib/api";

export default function FacesInPhoto({ assetId }: { assetId: string }) {
  const [data, setData] = useState<Payload | null>(null);
  const [open, setOpen] = useState<FaceInPhoto | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  const load = useCallback(async () => {
    try {
      setData(await api.facesInAsset(assetId));
    } catch {
      setData(null);
    }
  }, [assetId]);

  useEffect(() => {
    load();
  }, [load]);

  async function assign(face: FaceInPhoto, body: { person_id?: string; name?: string }) {
    setBusy(true);
    try {
      const result = await api.assignFace(face.face_id, body);
      setNote(
        result.created
          ? `Added ${result.name}, and this photo is their first.`
          : `Saved as ${result.name} — ${result.confirmed_count} confirmed.`,
      );
      setOpen(null);
      setDraft("");
      await load();
    } catch {
      setNote("That could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  if (!data || !data.faces.length) return null;

  return (
    <div className="card">
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h3>People in this photo</h3>
        <span className="faint mono">{data.note}</span>
      </div>

      <div className="facephoto">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={mediaUrl(assetId, "preview")} alt="" />
        {data.faces.map((face) => (
          <button
            key={face.face_id}
            type="button"
            className="facebox"
            // Green means "we know who this is", which is a claim only a *name*
            // supports. A face grouped into an unnamed cluster still needs the
            // one thing the operator can supply.
            data-known={Boolean(face.person_name)}
            data-open={open?.face_id === face.face_id}
            title={face.person_name || "Click to say who this is"}
            style={{
              left: `${face.box_x * 100}%`,
              top: `${face.box_y * 100}%`,
              width: `${face.box_w * 100}%`,
              height: `${face.box_h * 100}%`,
            }}
            onClick={() => {
              setOpen(open?.face_id === face.face_id ? null : face);
              setDraft("");
              setNote("");
            }}
          >
            {face.person_name && <span className="facebox-name">{face.person_name}</span>}
          </button>
        ))}
      </div>

      {note && <p className="faint">{note}</p>}

      {open && (
        <div className="card" style={{ marginTop: 10 }}>
          <strong>Who is this?</strong>

          {/* The confident guess gets its own button. One click is the whole
              interaction in the common case, which is what makes tagging a
              group of twenty people bearable. */}
          {open.guesses.filter((g) => g.confident).slice(0, 1).map((guess) => (
            <div className="toolbar" key={guess.person_id}>
              <button
                className="btn btn-primary"
                disabled={busy}
                onClick={() => assign(open, { person_id: guess.person_id })}
              >
                Yes — {guess.name}
              </button>
              <span className="faint mono">{(guess.similarity * 100).toFixed(0)}% match</span>
            </div>
          ))}

          {/* Everything else it considered, including matches below the
              threshold. A near-miss is often right and is exactly what a
              person with only three confirmed faces looks like. */}
          {open.guesses.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 7, margin: "8px 0" }}>
              {open.guesses
                .filter((g) => !g.confident || open.guesses.filter((x) => x.confident).length > 1)
                .map((guess) => (
                  <button
                    key={guess.person_id}
                    className="btn"
                    disabled={busy}
                    onClick={() => assign(open, { person_id: guess.person_id })}
                  >
                    {guess.name}{" "}
                    <span className="faint mono">{(guess.similarity * 100).toFixed(0)}%</span>
                  </button>
                ))}
            </div>
          )}

          <div className="field">
            <label htmlFor="face-name">
              {open.guesses.length ? "Someone else" : "Their name"}
            </label>
            <Autocomplete
              value={draft}
              onChange={setDraft}
              ariaLabel="Who is this"
              placeholder="Type a name"
              disabled={busy}
              fetcher={async (q) => {
                const found = await api.suggestNames(q);
                return found.map((p) => ({
                  id: p.id,
                  label: p.name,
                  hint: p.confirmed_count ? `${p.confirmed_count} photos` : "new",
                  exact: p.exact,
                }));
              }}
              // Picking an existing person assigns immediately — there is no
              // second decision to make once a name has been chosen.
              onPick={(picked) => assign(open, { person_id: picked.id })}
            />
          </div>

          <div className="toolbar">
            <button
              className="btn"
              disabled={busy || !draft.trim()}
              onClick={() => assign(open, { name: draft.trim() })}
            >
              Add {draft.trim() || "someone new"}
            </button>
            <button className="btn" onClick={() => setOpen(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
