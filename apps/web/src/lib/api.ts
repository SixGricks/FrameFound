// Typed client for the FrameFound API. Same-origin through Caddy, so the
// session cookie rides along automatically.

export type MediaType = "image" | "video" | "audio";

export interface User {
  id: string;
  email: string;
  role: string;
  totp_enabled: boolean;
}

export interface AuthSession {
  id: string;
  created_at: string;
  expires_at: string;
  ip: string | null;
  user_agent: string | null;
  current: boolean;
}

export interface RemoteAccess {
  mode: "local" | "tailscale" | "domain" | "tunnel";
  public_access_enabled: boolean;
  domain: string;
  ddns_provider: string;
  ddns_zone: string;
  ddns_record: string;
  ddns_configured: boolean;
  ddns_ipv4: boolean;
  ddns_ipv6: boolean;
  ddns_proxied: boolean;
  ddns_interval_minutes: number;
  your_connection: "local" | "lan" | "tailnet" | "internet" | "unknown";
  last_ipv4: string;
  last_checked_at: string;
  last_updated_at: string;
  last_error: string;
  tailnet_host: string;
  tailnet_url: string;
  tailnet_seen_at: string;
  on_tailnet_now: boolean;
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

export interface DuplicateMember {
  asset_id: string;
  library_id: string;
  relative_path: string;
  filename: string;
  size_bytes: number;
  mtime: string;
  content_hash_verified: boolean;
}

export interface DuplicateGroup {
  key: string;
  kind: "identical" | "similar";
  count: number;
  size_bytes: number;
  reclaimable_bytes: number;
  members: DuplicateMember[];
}

export interface DuplicateReport {
  groups: DuplicateGroup[];
  total_groups: number;
  total_reclaimable_bytes: number;
  note: string;
}

export interface AssetTag {
  tag_id: string;
  name: string;
  slug: string;
  source: "manual" | "confirmed" | "suggested" | "rejected";
  confidence: number | null;
}

export interface TagSummary {
  id: string;
  name: string;
  slug: string;
  example_count: number;
  asset_count: number;
  pending_count: number;
  threshold: number | null;
  threshold_reason: string;
  learned_at: string | null;
  suggest_enabled: boolean;
}

export interface PendingSuggestion {
  asset_id: string;
  filename: string;
  media_type: MediaType;
  confidence: number | null;
}

export interface Mount {
  path: string;
  fstype: string;
  is_network: boolean;
  total_gb: number | null;
  free_gb: number | null;
  writable: boolean;
  role: string;
  library_name: string | null;
  asset_count: number | null;
}

export interface StorageReport {
  media_root: string;
  data_store: string;
  mounts: Mount[];
  hint: string;
}

export interface DriveResult {
  ok: boolean;
  target: string;
  detail: string;
  library_id: string | null;
  fstab_line: string;
  persist_hint: string;
}

export interface Place {
  name: string;
  named_from: "folder" | "geocode" | "unknown";
  lat: number;
  lon: number;
  radius_km: number;
  asset_count: number;
  inferred_count: number;
  first_captured_at: string | null;
  last_captured_at: string | null;
  cover_asset_id: string | null;
}

export interface MapConfig {
  basemap_enabled: boolean;
  browser_key: string;
  geocoding_ready: boolean;
  provider: "none" | "maplibre" | "google";
  style_url: string;
  library_url: string;
  stylesheet_url: string;
}

export interface MapsSettings {
  basemap_enabled: boolean;
  browser_key_configured: boolean;
  geocoding_key_configured: boolean;
  geocode_unnamed_places: boolean;
  provider: "none" | "maplibre" | "google";
  style_url: string;
  library_url: string;
  stylesheet_url: string;
}

export interface NearbyAsset {
  asset_id: string;
  filename: string;
  media_type: MediaType;
  distance_km: number;
  gps_lat: number;
  gps_lon: number;
  gps_source: string | null;
  gps_confidence: number | null;
  captured_at: string | null;
}

export interface SceneFrame {
  ts_ms: number;
  is_scene_change: boolean;
  scene_number: number | null;
  url: string;
}

export interface FaceRef {
  face_id: string;
  asset_id: string;
  frame_id: string;
  filename: string;
  box_x: number;
  box_y: number;
  box_w: number;
  box_h: number;
  detection_score: number;
  similarity: number | null;
  source: "detected" | "confirmed" | "rejected";
}

export interface PersonSummary {
  id: string;
  name: string;
  slug: string;
  named: boolean;
  confirmed_count: number;
  pending_count: number;
  cover: FaceRef | null;
}

export interface PersonDetail extends PersonSummary {
  faces: FaceRef[];
}

export interface FaceSettings {
  enabled: boolean;
  suggest_across_libraries: boolean;
  people_count: number;
  faces_count: number;
  unnamed_clusters: number;
}

export interface TagHit {
  asset_id: string;
  filename: string;
  library_id: string;
  media_type: MediaType;
  tag_name: string;
  tag_slug: string;
  confirmed: boolean;
}

export interface VisualHit {
  asset_id: string;
  filename: string;
  library_id: string;
  media_type: MediaType;
  ts_ms: number;
  similarity: number;
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
  visual_hits: VisualHit[];
  visual_available: boolean;
  tag_hits: TagHit[];
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
  login: (email: string, password: string, totpCode?: string) =>
    request<User>("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, totp_code: totpCode ?? null }),
    }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),

  sessions: () => request<AuthSession[]>("/auth/sessions"),
  revokeSession: (id: string) =>
    request<void>(`/auth/sessions/${id}`, { method: "DELETE" }),
  revokeOtherSessions: () =>
    request<{ revoked: number }>("/auth/sessions/revoke-others", { method: "POST" }),

  totpStart: (password: string) =>
    request<{ provisioning_uri: string; secret: string }>("/auth/totp/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    }),
  totpConfirm: (code: string) =>
    request<{ recovery_codes: string[] }>("/auth/totp/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    }),
  totpDisable: (password: string, code: string) =>
    request<void>("/auth/totp/disable", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password, code }),
    }),

  remoteAccess: () => request<RemoteAccess>("/remote-access"),
  updateRemoteAccess: (patch: Record<string, unknown>) =>
    request<RemoteAccess>("/remote-access", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  disablePublicAccess: () =>
    request<RemoteAccess>("/remote-access/disable-public", { method: "POST" }),
  testDns: () =>
    request<{ ok: boolean; message: string; detected_ipv4: string | null }>(
      "/remote-access/test-dns",
      { method: "POST" },
    ),

  libraries: () => request<Library[]>("/libraries"),
  scanLibrary: (id: string) => request<unknown>(`/libraries/${id}/scan`, { method: "POST" }),

  assets: (params: {
    library_id?: string;
    media_type?: string;
    previewable?: boolean;
    tag?: string;
    include_suggested_tags?: boolean;
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
  similar: (assetId: string) => request<VisualHit[]>(`/search/similar/${assetId}`),
  scenes: (assetId: string) => request<SceneFrame[]>(`/assets/${assetId}/scenes`),

  places: (radiusKm: number, includeInferred: boolean) =>
    request<Place[]>(
      `/places${query({ radius_km: radiusKm, include_inferred: includeInferred })}`,
    ),
  assetsNear: (lat: number, lon: number, radiusKm: number) =>
    request<NearbyAsset[]>(
      `/assets/near${query({ lat, lon, radius_km: radiusKm, limit: 120 })}`,
    ),

  tags: () => request<TagSummary[]>("/tags"),
  assetTags: (assetId: string) => request<AssetTag[]>(`/tags/assets/${assetId}`),
  addAssetTag: (assetId: string, name: string) =>
    request<AssetTag[]>(`/tags/assets/${assetId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  removeAssetTag: (assetId: string, tagId: string) =>
    request<void>(`/tags/assets/${assetId}/${tagId}`, { method: "DELETE" }),
  decideAssetTag: (assetId: string, tagId: string, accept: boolean) =>
    request<AssetTag[]>(`/tags/assets/${assetId}/${tagId}/decide`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accept }),
    }),
  pendingForTag: (tagId: string) =>
    request<PendingSuggestion[]>(`/tags/${tagId}/pending`),
  relearnTag: (tagId: string) =>
    request<{ status: string }>(`/tags/${tagId}/relearn`, { method: "POST" }),

  people: () => request<PersonSummary[]>("/people"),
  person: (id: string) => request<PersonDetail>(`/people/${id}`),
  namePerson: (id: string, name: string) =>
    request<PersonSummary>(`/people/${id}/name`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  confirmFaces: (id: string, faceIds: string[]) =>
    request<{ confirmed: number }>(`/people/${id}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ face_ids: faceIds }),
    }),
  rejectFaces: (id: string, faceIds: string[]) =>
    request<{ rejected: number }>(`/people/${id}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ face_ids: faceIds }),
    }),
  forgetPerson: (id: string) => request<void>(`/people/${id}`, { method: "DELETE" }),
  faceSettings: () => request<FaceSettings>("/people/settings/current"),
  updateFaceSettings: (patch: Partial<FaceSettings>) =>
    request<FaceSettings>("/people/settings/current", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),

  storage: () => request<StorageReport>("/storage"),
  addDrive: (body: Record<string, unknown>) =>
    request<DriveResult>("/storage/drives", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  unmountDrive: (target: string) =>
    request<DriveResult>("/storage/drives/unmount", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target }),
    }),

  mapConfig: () => request<MapConfig>("/places/map-config"),
  mapsSettings: () => request<MapsSettings>("/places/maps-settings"),
  updateMapsSettings: (patch: Partial<MapsSettings & { browser_key: string; geocoding_key: string }>) =>
    request<MapsSettings>("/places/maps-settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),

  duplicates: (kind: string, minSizeMb: number) =>
    request<DuplicateReport>(`/duplicates${query({ kind, min_size_mb: minSizeMb })}`),
  verifyDuplicates: (assetIds: string[]) =>
    request<{ status: string; queued: number }>("/duplicates/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset_ids: assetIds }),
    }),

  processing: () => request<ProcessingReport>("/system/processing"),
  health: () => request<HealthReport>("/system/health"),
};

export const mediaUrl = (assetId: string, kind: string) => `/api/v1/media/${assetId}/${kind}`;
