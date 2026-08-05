"use client";

// The develop editor: sliders on the left, the photograph as it will export
// on the right.
//
// The preview is rendered SERVER-side by the same engine the export runs —
// there is deliberately no client-side approximation of the maths, because
// two implementations of "what does +0.4 contrast look like" will disagree,
// and the operator would be correcting toward a preview the zip then
// contradicts. The cost is a round-trip per adjustment, softened by
// debouncing; on the LAN this reads as a beat, not a wait.
//
// Nothing here touches the original file. A recipe is saved as a new
// version; Reset deletes recipes. Both are metadata operations.

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";

import Shell from "@/components/Shell";
import {
  api,
  EMPTY_RECIPE,
  type DevelopRecipe,
  type ListingDetail,
  type InpaintState,
  type SkyAsset,
  type SkyInfo,
} from "@/lib/api";

const DEBOUNCE_MS = 250;

const SLIDERS: Array<{
  key: keyof Omit<DevelopRecipe, "auto" | "sky">;
  label: string;
  min: number;
  max: number;
  step: number;
}> = [
  { key: "exposure", label: "Exposure", min: -2, max: 2, step: 0.05 },
  { key: "contrast", label: "Contrast", min: -1, max: 1, step: 0.02 },
  { key: "temperature", label: "Temperature", min: -1, max: 1, step: 0.02 },
  { key: "tint", label: "Tint", min: -1, max: 1, step: 0.02 },
  { key: "shadows", label: "Shadows", min: -1, max: 1, step: 0.02 },
  { key: "highlights", label: "Highlights", min: -1, max: 1, step: 0.02 },
  { key: "vibrance", label: "Vibrance", min: -1, max: 1, step: 0.02 },
  { key: "saturation", label: "Saturation", min: -1, max: 1, step: 0.02 },
  { key: "auto_wb", label: "Auto WB", min: 0, max: 1, step: 0.02 },
  { key: "local_contrast", label: "Local contrast", min: 0, max: 1, step: 0.02 },
  { key: "window_pull", label: "Window pull", min: 0, max: 1, step: 0.02 },
  { key: "rotate", label: "Straighten", min: -5, max: 5, step: 0.1 },
  { key: "keystone", label: "Verticals", min: -1, max: 1, step: 0.02 },
];

export default function EditPage() {
  const params = useParams<{ assetId: string }>();
  const searchParams = useSearchParams();
  const router = useRouter();
  const assetId = params.assetId;
  const listingId = searchParams.get("listing");

  const [recipe, setRecipe] = useState<DevelopRecipe>(EMPTY_RECIPE);
  const [savedVersion, setSavedVersion] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [rendering, setRendering] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [listing, setListing] = useState<ListingDetail | null>(null);
  const [skies, setSkies] = useState<SkyAsset[]>([]);
  const [skyInfo, setSkyInfo] = useState<SkyInfo | null>(null);
  const [marking, setMarking] = useState(false);
  const [brush, setBrush] = useState(24);
  const [marked, setMarked] = useState(false);
  const [inpaint, setInpaint] = useState<InpaintState | null>(null);
  const paintRef = useRef<HTMLCanvasElement | null>(null);
  const maskRef = useRef<HTMLCanvasElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const painting = useRef(false);
  const debounce = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Renders can land out of order; the ticket makes the latest one win.
  const ticket = useRef(0);

  useEffect(() => {
    api
      .developState(assetId)
      .then((state) => {
        setRecipe({ ...EMPTY_RECIPE, ...state.recipe });
        setSavedVersion(state.version);
      })
      .catch(() => setError("Could not load this photograph's edits."));
    if (listingId) {
      api.listing(listingId).then(setListing).catch(() => setListing(null));
    }
    api.skies().then(setSkies).catch(() => setSkies([]));
    api.skyInfo(assetId).then(setSkyInfo).catch(() => setSkyInfo(null));
    api.inpaintState(assetId).then(setInpaint).catch(() => setInpaint(null));
  }, [assetId, listingId]);

  const render = useCallback(
    async (current: DevelopRecipe) => {
      const mine = ++ticket.current;
      setRendering(true);
      try {
        const resp = await fetch(`/api/v1/develop/${assetId}/preview`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(current),
        });
        if (!resp.ok) throw new Error("preview failed");
        const blob = await resp.blob();
        if (mine !== ticket.current) return;
        setPreviewUrl((old) => {
          if (old) URL.revokeObjectURL(old);
          return URL.createObjectURL(blob);
        });
        setError(null);
      } catch {
        if (mine === ticket.current) setError("Could not render the preview.");
      } finally {
        if (mine === ticket.current) setRendering(false);
      }
    },
    [assetId],
  );

  // While a removal runs, poll; when it lands, the base image changed, so
  // re-render the preview and drop the marks.
  useEffect(() => {
    if (!inpaint?.busy) return;
    const timer = setTimeout(async () => {
      try {
        const state = await api.inpaintState(assetId);
        setInpaint(state);
        if (!state.busy) {
          clearMarks();
          render(recipe);
          const failed = state.versions.at(-1);
          setNotice(
            failed?.status === "failed"
              ? `Removal failed: ${failed.error ?? "unknown error"}`
              : "Removal finished.",
          );
        }
      } catch {
        /* poll again next tick */
      }
    }, 3000);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inpaint, assetId, recipe]);

  // First render + re-render on every recipe change, debounced.
  useEffect(() => {
    if (debounce.current) clearTimeout(debounce.current);
    debounce.current = setTimeout(() => render(recipe), DEBOUNCE_MS);
    return () => {
      if (debounce.current) clearTimeout(debounce.current);
    };
  }, [recipe, render]);

  function set<K extends keyof DevelopRecipe>(key: K, value: DevelopRecipe[K]) {
    setRecipe((current) => ({ ...current, [key]: value }));
    setDirty(true);
    setNotice(null);
  }

  async function save() {
    setBusy(true);
    try {
      const state = await api.saveDevelop(assetId, recipe);
      setSavedVersion(state.version);
      setDirty(false);
      setNotice("Saved. The export will use this look.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save");
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    try {
      await api.clearDevelop(assetId);
      setRecipe(EMPTY_RECIPE);
      setSavedVersion(0);
      setDirty(false);
      setNotice("Back to the original.");
    } catch {
      setError("Could not reset");
    } finally {
      setBusy(false);
    }
  }

  async function applyToListing() {
    if (!listingId) return;
    setBusy(true);
    try {
      await api.saveDevelop(assetId, recipe);
      setDirty(false);
      const { applied } = await api.applyDevelopToListing(listingId, recipe);
      setNotice(`Applied to ${applied} photos in this listing.`);
    } catch {
      setError("Could not apply to the listing");
    } finally {
      setBusy(false);
    }
  }

  function canvasPoint(e: React.PointerEvent<HTMLCanvasElement>) {
    const canvas = paintRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    return {
      x: ((e.clientX - rect.left) / rect.width) * canvas.width,
      y: ((e.clientY - rect.top) / rect.height) * canvas.height,
    };
  }

  function dab(x: number, y: number) {
    const canvas = paintRef.current;
    const mask = maskRef.current;
    if (!canvas || !mask) return;
    const scale = canvas.width / (canvas.getBoundingClientRect().width || canvas.width);
    const radius = (brush / 2) * scale;
    for (const [ctx, style] of [
      [canvas.getContext("2d"), "rgba(255,60,60,0.55)"],
      [mask.getContext("2d"), "#ffffff"],
    ] as const) {
      if (!ctx) continue;
      ctx.fillStyle = style;
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, Math.PI * 2);
      ctx.fill();
    }
    setMarked(true);
  }

  function syncCanvases() {
    const img = imgRef.current;
    if (!img || !img.naturalWidth) return;
    for (const ref of [paintRef, maskRef]) {
      const canvas = ref.current;
      if (!canvas) continue;
      if (canvas.width !== img.naturalWidth || canvas.height !== img.naturalHeight) {
        canvas.width = img.naturalWidth;
        canvas.height = img.naturalHeight;
        if (ref === maskRef) {
          const ctx = canvas.getContext("2d");
          if (ctx) {
            ctx.fillStyle = "#000000";
            ctx.fillRect(0, 0, canvas.width, canvas.height);
          }
        }
      }
    }
  }

  function clearMarks() {
    for (const ref of [paintRef, maskRef]) {
      const canvas = ref.current;
      const ctx = canvas?.getContext("2d");
      if (!canvas || !ctx) continue;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (ref === maskRef) {
        ctx.fillStyle = "#000000";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
      }
    }
    setMarked(false);
  }

  async function submitRemoval() {
    const mask = maskRef.current;
    if (!mask || !marked) return;
    setBusy(true);
    try {
      const b64 = mask.toDataURL("image/png").split(",")[1] ?? "";
      setInpaint(await api.requestInpaint(assetId, b64));
      setNotice("Removing — this takes about a minute on this hardware.");
      setMarking(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not queue the removal");
    } finally {
      setBusy(false);
    }
  }

  async function undoRemoval() {
    const latest = inpaint?.versions.filter((v) => v.status === "ready").at(-1);
    if (!latest) return;
    setBusy(true);
    try {
      setInpaint(await api.undoInpaint(assetId, latest.version));
      render(recipe);
      setNotice("Removal undone.");
    } catch {
      setError("Could not undo");
    } finally {
      setBusy(false);
    }
  }

  const readyRemovals = inpaint?.versions.filter((v) => v.status === "ready").length ?? 0;

  // Prev/next within the listing's images, in gallery order.
  const siblings = (listing?.items ?? [])
    .filter((i) => i.media_type === "image")
    .sort((a, b) => a.position - b.position)
    .map((i) => i.asset_id);
  const at = siblings.indexOf(assetId);
  const go = (target: string) =>
    router.push(`/edit/${target}?listing=${listingId}`);

  return (
    <Shell>
      <div className="sectionhead" style={{ marginTop: 0 }}>
        <h2>Develop</h2>
        <span className="faint mono">
          {savedVersion > 0 ? `version ${savedVersion} saved` : "original"}
          {dirty && " · unsaved changes"}
          {rendering && " · rendering…"}
        </span>
      </div>

      {error && (
        <div className="card" role="alert" style={{ borderColor: "var(--ember)" }}>
          {error}
        </div>
      )}
      {notice && !error && (
        <div className="card" role="status">
          {notice}
        </div>
      )}

      <div className="toolbar">
        {listingId ? (
          <Link className="btn" href={`/listings/${listingId}`} style={{ padding: "6px 12px" }}>
            ← Listing
          </Link>
        ) : (
          <Link className="btn" href={`/assets/${assetId}`} style={{ padding: "6px 12px" }}>
            ← Asset
          </Link>
        )}
        {at > 0 && (
          <button className="btn" onClick={() => go(siblings[at - 1]!)}>
            ‹ Previous
          </button>
        )}
        {at >= 0 && at < siblings.length - 1 && (
          <button className="btn" onClick={() => go(siblings[at + 1]!)}>
            Next ›
          </button>
        )}
        <button className="btn btn-primary" disabled={busy || !dirty} onClick={save}>
          Save
        </button>
        {listingId && (
          <button
            className="btn"
            disabled={busy}
            onClick={applyToListing}
            title="Save this look onto every photo in the listing — one light, one correction"
          >
            Apply to whole listing
          </button>
        )}
        <button
          className="btn"
          disabled={busy || inpaint?.busy}
          onClick={() => {
            setMarking((v) => !v);
            if (!marking) setNotice("Paint over what should disappear, then press Remove.");
          }}
        >
          {marking ? "Cancel marking" : "Remove objects"}
        </button>
        {readyRemovals > 0 && !inpaint?.busy && (
          <button className="btn" disabled={busy} onClick={undoRemoval}>
            Undo removal
          </button>
        )}
        <button
          className="btn"
          style={{ marginLeft: "auto" }}
          disabled={busy || (savedVersion === 0 && !dirty)}
          onClick={reset}
        >
          Reset to original
        </button>
      </div>

      {marking && (
        <div className="toolbar">
          <span className="mono faint">Brush</span>
          <input
            type="range"
            min={8}
            max={80}
            step={2}
            value={brush}
            onChange={(e) => setBrush(Number(e.target.value))}
            aria-label="Brush size"
          />
          <button className="btn btn-primary" disabled={busy || !marked} onClick={submitRemoval}>
            Remove marked
          </button>
          <button className="btn" disabled={!marked} onClick={clearMarks}>
            Clear marks
          </button>
        </div>
      )}
      {inpaint?.busy && (
        <div className="card" role="status">
          Removing the marked object — LaMa runs about a minute per region on
          these CPUs. The page will update when it lands.
        </div>
      )}

      <div style={{ display: "flex", gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>
        <div className="card" style={{ width: 280, flexShrink: 0 }}>
          <label
            style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 10 }}
          >
            <input
              type="checkbox"
              checked={recipe.auto}
              onChange={(e) => set("auto", e.target.checked)}
            />
            Auto levels
            <span className="faint" style={{ fontSize: "0.75rem" }}>
              a sane base; sliders shape it
            </span>
          </label>
          {SLIDERS.map((slider) => (
            <div key={slider.key} style={{ marginBottom: 8 }}>
              <div
                style={{ display: "flex", justifyContent: "space-between" }}
                className="mono"
              >
                <label htmlFor={`s-${slider.key}`}>{slider.label}</label>
                <span className="faint">
                  {slider.key === "exposure"
                    ? recipe.exposure.toFixed(2)
                    : slider.key === "rotate"
                      ? `${recipe.rotate.toFixed(1)}°`
                      : Math.round(recipe[slider.key] * 100)}
                </span>
              </div>
              <input
                id={`s-${slider.key}`}
                type="range"
                style={{ width: "100%" }}
                min={slider.min}
                max={slider.max}
                step={slider.step}
                value={recipe[slider.key]}
                onChange={(e) => set(slider.key, Number(e.target.value))}
                onDoubleClick={() => set(slider.key, 0)}
                title="Double-click to zero"
              />
            </div>
          ))}
          <div style={{ borderTop: "1px solid var(--line)", marginTop: 12, paddingTop: 10 }}>
            <div className="mono" style={{ marginBottom: 6 }}>
              Sky
              {skyInfo && !skyInfo.available && (
                <span className="faint"> · unavailable on this server</span>
              )}
              {skyInfo && skyInfo.available && !skyInfo.usable && (
                <span className="faint"> · no sky found — looks like an interior</span>
              )}
            </div>
            {skyInfo?.usable && (
              <>
                <select
                  className="select"
                  style={{ width: "100%" }}
                  aria-label="Replacement sky"
                  value={recipe.sky?.name ?? ""}
                  onChange={(e) =>
                    set(
                      "sky",
                      e.target.value
                        ? { name: e.target.value, feather: 0.02, shift: 0, relight: 0.4 }
                        : null,
                    )
                  }
                >
                  <option value="">Original sky</option>
                  {skies.map((sky) => (
                    <option key={sky.name} value={sky.name}>
                      {sky.name}
                    </option>
                  ))}
                </select>
                {recipe.sky && (
                  <>
                    {(
                      [
                        ["feather", "Blend", 0, 0.2, 0.005],
                        ["shift", "Height", -0.5, 0.5, 0.01],
                        ["relight", "Match light", 0, 1, 0.02],
                      ] as const
                    ).map(([key, label, min, max, step]) => (
                      <div key={key} style={{ marginTop: 6 }}>
                        <div
                          className="mono"
                          style={{ display: "flex", justifyContent: "space-between" }}
                        >
                          <label htmlFor={`sky-${key}`}>{label}</label>
                          <span className="faint">{(recipe.sky?.[key] ?? 0).toFixed(2)}</span>
                        </div>
                        <input
                          id={`sky-${key}`}
                          type="range"
                          style={{ width: "100%" }}
                          min={min}
                          max={max}
                          step={step}
                          value={recipe.sky?.[key] ?? 0}
                          onChange={(e) =>
                            set("sky", { ...recipe.sky!, [key]: Number(e.target.value) })
                          }
                        />
                      </div>
                    ))}
                  </>
                )}
                {!skies.length && (
                  <p className="faint" style={{ fontSize: "0.75rem", marginTop: 6 }}>
                    No skies yet — add photographs of skies you own below.
                    FrameFound ships none and fetches none.
                  </p>
                )}
                <label className="btn" style={{ marginTop: 8, display: "inline-block" }}>
                  Add a sky…
                  <input
                    type="file"
                    accept="image/*"
                    style={{ display: "none" }}
                    onChange={async (e) => {
                      const file = e.target.files?.[0];
                      if (!file) return;
                      try {
                        await api.uploadSky(file.name, file);
                        setSkies(await api.skies());
                        setNotice(`Added ${file.name} to the sky library.`);
                      } catch {
                        setError("Could not add that sky.");
                      }
                    }}
                  />
                </label>
              </>
            )}
          </div>
          <p className="faint" style={{ fontSize: "0.75rem", marginTop: 10 }}>
            The preview and the export run the same maths — what you see here
            is what the zip contains. The original file is never modified.
          </p>
        </div>

        <div style={{ flex: 1, minWidth: 320 }}>
          {previewUrl ? (
            <div style={{ position: "relative", display: "inline-block" }}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                ref={imgRef}
                src={previewUrl}
                alt="Develop preview"
                onLoad={syncCanvases}
                style={{
                  maxWidth: "100%",
                  borderRadius: 6,
                  opacity: rendering ? 0.7 : 1,
                  transition: "opacity 0.15s",
                  display: "block",
                }}
              />
              <canvas
                ref={paintRef}
                style={{
                  position: "absolute",
                  inset: 0,
                  width: "100%",
                  height: "100%",
                  cursor: marking ? "crosshair" : "default",
                  pointerEvents: marking ? "auto" : "none",
                  touchAction: "none",
                }}
                onPointerDown={(e) => {
                  painting.current = true;
                  e.currentTarget.setPointerCapture(e.pointerId);
                  const p = canvasPoint(e);
                  if (p) dab(p.x, p.y);
                }}
                onPointerMove={(e) => {
                  if (!painting.current) return;
                  const p = canvasPoint(e);
                  if (p) dab(p.x, p.y);
                }}
                onPointerUp={() => {
                  painting.current = false;
                }}
              />
              <canvas ref={maskRef} style={{ display: "none" }} />
            </div>
          ) : (
            <div className="empty">Rendering the first preview…</div>
          )}
        </div>
      </div>
    </Shell>
  );
}
