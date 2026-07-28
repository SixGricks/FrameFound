// Typed client for the FrameFound API. Same-origin through Caddy, so the
// session cookie rides along automatically.

export type MediaType = "image" | "video" | "audio";

export interface User {
  id: string;
  email: string;
  role: string;
}

export interface Library {
  id: string;
  name: string;
  root_path: string;
  read_only: boolean;
  watcher_enabled: boolean;
  enabled: boolean;
  generate_proxies: boolean;
  proxy_resolution: number;
  transcribe_enabled: boolean;
  last_scan_at: string | null;
  asset_count: number;
}

export interface AssetSummary {
  id: string;
  library_id: string;
  relative_path: string;
  filename: string;
  media_type: MediaType;
  size_bytes: number;
  mtime: string;
  availability: string;
  processing_status: string;
  duration_s: number | null;
  width: number | null;
  height: number | null;
  captured_at: string | null;
}

export interface AssetDetail extends AssetSummary {
  extension: string;
  mime_type: string | null;
  content_hash: string | null;
  fps: number | null;
  video_codec: string | null;
  audio_codec: string | null;
  sample_rate: number | null;
  channels: number | null;
  bitrate: number | null;
  camera_make: string | null;
  camera_model: string | null;
  lens: string | null;
  focal_length_mm: number | null;
  aperture_f: number | null;
  shutter_speed: string | null;
  iso: number | null;
  gps_lat: number | null;
  gps_lon: number | null;
  favorite: boolean;
  title: string | null;
  description: string | null;
  first_indexed_at: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

export interface TranscriptSegment {
  start_ms: number;
  end_ms: number;
  text: string;
  speaker: string | null;
  confidence: number | null;
}

export interface Transcript {
  language: string;
  language_confidence: number | null;
  model_name: string;
  processed_at: string;
  segment_count: number;
  segments: TranscriptSegment[];
}

export interface SearchResponse {
  query: string;
  transcript_hits: Array<{
    asset_id: string;
    filename: string;
    library_id: string;
    media_type: MediaType;
    start_ms: number;
    end_ms: number;
    text: string;
  }>;
  filename_hits: Array<{
    asset_id: string;
    filename: string;
    library_id: string;
    media_type: MediaType;
    captured_at: string | null;
  }>;
}

export interface ProcessingReport {
  queue_depths: Record<string, number>;
  assets_by_status: Record<string, number>;
  derivatives: Record<string, Record<string, number>>;
  jobs_last_hour: Record<string, number>;
  recent_failures: Array<{
    id: string;
    task_name: string;
    asset_id: string | null;
    error: string | null;
    started_at: string;
  }>;
}

export interface HealthReport {
  version: string;
  database: { status: string; detail: string | null };
  queue: { status: string; detail: string | null };
  data_dir_free_gb: number | null;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`/api/v1${path}`, { ...init, credentials: "same-origin" });
  if (!resp.ok) {
    const body = await resp.json().catch(() => null);
    throw new ApiError(resp.status, body?.error?.message ?? `Request failed (${resp.status})`);
  }
  return resp.status === 204 ? (undefined as T) : ((await resp.json()) as T);
}

function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") usp.set(key, String(value));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

export const api = {
  me: () => request<User>("/auth/me"),
  login: (email: string, password: string) =>
    request<User>("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),

  libraries: () => request<Library[]>("/libraries"),
  scanLibrary: (id: string) => request<unknown>(`/libraries/${id}/scan`, { method: "POST" }),

  assets: (params: {
    library_id?: string;
    media_type?: string;
    previewable?: boolean;
    status?: string;
    sort?: string;
    page?: number;
    page_size?: number;
  }) => request<Page<AssetSummary>>(`/assets${query(params)}`),
  asset: (id: string) => request<AssetDetail>(`/assets/${id}`),
  transcript: (id: string) => request<Transcript>(`/assets/${id}/transcript`),
  reprocess: (id: string) => request<unknown>(`/assets/${id}/reprocess`, { method: "POST" }),

  search: (q: string, libraryId?: string) =>
    request<SearchResponse>(`/search${query({ q, library_id: libraryId, limit: 40 })}`),

  processing: () => request<ProcessingReport>("/system/processing"),
  health: () => request<HealthReport>("/system/health"),
};

export const mediaUrl = (assetId: string, kind: string) => `/api/v1/media/${assetId}/${kind}`;
