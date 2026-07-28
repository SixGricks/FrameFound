// Presentation helpers. Timecode is the professional signifier in this
// domain, so it gets first-class formatting everywhere it appears.

export function timecode(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

export function duration(seconds: number | null): string {
  if (seconds === null || Number.isNaN(seconds)) return "—";
  return timecode(seconds * 1000);
}

export function bytes(n: number | null): string {
  if (n === null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

export function shortDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diff = Date.now() - then;
  const mins = Math.round(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function resolution(width: number | null, height: number | null): string {
  return width && height ? `${width}×${height}` : "—";
}

/** Split text around a query for <mark> highlighting, case-insensitively. */
export function highlight(text: string, query: string): Array<[string, boolean]> {
  const terms = query
    .replace(/["']/g, " ")
    .split(/\s+/)
    .filter((t) => t.length > 1)
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (terms.length === 0) return [[text, false]];
  const parts: Array<[string, boolean]> = [];
  const re = new RegExp(`(${terms.join("|")})`, "ig");
  let last = 0;
  for (const match of text.matchAll(re)) {
    const index = match.index ?? 0;
    if (index > last) parts.push([text.slice(last, index), false]);
    parts.push([match[0], true]);
    last = index + match[0].length;
  }
  if (last < text.length) parts.push([text.slice(last), false]);
  return parts;
}
