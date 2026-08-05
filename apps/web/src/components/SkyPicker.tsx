"use client";

// Choosing a sky is a visual decision; a dropdown of filenames made the
// operator do the imagining. This shows the skies themselves — click the
// one the photograph deserves.

import { api, type SkyAsset } from "@/lib/api";

export const skyImageUrl = (name: string) =>
  `/api/v1/develop/skies/${encodeURIComponent(name)}/image`;

export function prettySkyName(name: string): string {
  return name
    .replace(/-\d+\.(jpg|jpeg|png|webp)$/i, "")
    .replace(/\.(jpg|jpeg|png|webp)$/i, "")
    .replace(/-/g, " ");
}

export default function SkyPicker({
  skies,
  value,
  disabled,
  onChange,
  allowNone = true,
}: {
  skies: SkyAsset[];
  value: string;
  disabled?: boolean;
  onChange: (name: string) => void;
  allowNone?: boolean;
}) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "repeat(auto-fill, minmax(70px, 1fr))",
        gap: 6,
      }}
      role="radiogroup"
      aria-label="Replacement sky"
    >
      {allowNone && (
        <button
          type="button"
          role="radio"
          aria-checked={value === ""}
          className="btn"
          disabled={disabled}
          style={{
            aspectRatio: "3/2",
            fontSize: "0.66rem",
            outline: value === "" ? "2px solid var(--amber)" : undefined,
          }}
          onClick={() => onChange("")}
          title="Leave every sky as it was shot"
        >
          As shot
        </button>
      )}
      {skies.map((sky) => (
        <button
          type="button"
          role="radio"
          aria-checked={value === sky.name}
          key={sky.name}
          disabled={disabled}
          style={{
            padding: 0,
            border: 0,
            borderRadius: 4,
            overflow: "hidden",
            cursor: disabled ? "default" : "pointer",
            aspectRatio: "3/2",
            outline: value === sky.name ? "2px solid var(--amber)" : "1px solid var(--line)",
            background: "var(--ink-deep)",
          }}
          onClick={() => onChange(sky.name)}
          title={prettySkyName(sky.name)}
        >
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={skyImageUrl(sky.name)}
            alt={prettySkyName(sky.name)}
            loading="lazy"
            style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }}
          />
        </button>
      ))}
    </div>
  );
}
