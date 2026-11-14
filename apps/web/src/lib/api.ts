// Typed client for the FrameFound API. Same-origin through Caddy, so the
// session cookie rides along automatically.

import { StreamRefused, chatEvent, readEventStream } from "./chatStream";
import { urlStatus } from "./probe";

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

export interface TailscaleState {
  state:
    | "not_installed"
    | "needs_login"
    | "needs_approval"
    | "stopped"
    | "starting"
    | "connected"
    | "unavailable";
  login_url: string;
  dns_name: string;
  host_name: string;
  tailnet: string;
  ips: string[];
  url: string;
  short_url: string;
  mcp_url: string;
  admin_console: string;
  download: string;
  message: string;
  /** The kind of machine the server is: the card shows its way to install.
   *  Absent from a server older than the Mac install (onMac then falls back
   *  to `download`). */
  platform?: "macos" | "linux";
  /** Read through the Mac app, which cannot hand out a sign-in link:
   *  signing in or turning it on is then done in the app. Absent (false)
   *  from an older server. */
  via_app?: boolean;
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
  /** null = rescanned only when someone presses Scan now. */
  scan_interval_minutes: number | null;
  last_scan_at: string | null;
  asset_count: number;
  /** New photographs and clips are looked at by the local Looker as they
   *  finish processing; what was already there is not. */
  look_at_ingest: boolean;
  /** When that was switched on (UTC); null while it is off. */
  look_since: string | null;
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
  /** A local model's 0-100 opinion of how usable this is: a photograph's
   *  own, a clip's best shot's. Null when nothing has looked at it. */
  look_score: number | null;
  /** What that model said it shows. A model's text: shown as text only. */
  look_caption: string | null;
  /** Set once nothing in it still needs looking at (every shot of a
   *  clip), so a clip can have a score while this is still null. */
  looked_at: string | null;
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
  /** Why there are (or are not) words beside this clip. */
  transcript_state: "none" | "speech" | "no_speech" | "queued" | "failed" | "off";
  /** What a local model made of it: a photograph's look, or a clip's best
   *  shot. Null when nothing has looked at it. */
  look: AssetLook | null;
}

/** The transcript as a caption file (the session cookie authorises it). */
export const subtitlesUrl = (assetId: string, format: "srt" | "vtt") =>
  `/api/v1/assets/${assetId}/subtitles?format=${format}`;

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
  /** Who suggested it: the CLIP learner, the Claude reviewer, Claude via
   *  MCP, or a model on this Mac. */
  origin: "clip" | "claude" | "mcp" | "local";
  /** The moment of a clip the suggestion is about (the middle of the shot
   *  a local model was shown); null for a photograph or a whole clip. */
  at_ms: number | null;
  /** The local model's name, for origin "local" only. */
  model: string | null;
  /** Made up by the demonstration; the page says so. */
  demo: boolean;
}

export interface ClaudeReviewState {
  state: "idle" | "queued" | "running" | "done" | "budget" | "failed" | string;
  started_at: string;
  finished_at: string;
  requested: number;
  reviewed: number;
  suggested: number;
  new_tags: number;
  skipped: number;
  failed: number;
  spent_usd: number;
  message: string;
}

export interface ClaudeReviewStatus {
  configured: boolean;
  model: string;
  budget_usd: number;
  spent_this_month_usd: number;
  spent_on_review_usd: number;
  remaining_usd: number;
  auto: boolean;
  batch: number;
  /** Pictures nobody has judged yet: no tag or answer, no Claude suggestion, not yet reviewed by the API (the learner's guesses do not count). */
  untagged: number;
  pending_from_claude: number;
  last: ClaudeReviewState;
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
  /** False where the server cannot see or add drives (the Mac install:
   *  drives are connected in Finder there). The page then shows `hint` in
   *  place of the drives table and the add-a-drive form. */
  drive_mounting: boolean;
}

export interface DriveResult {
  ok: boolean;
  target: string;
  detail: string;
  library_id: string | null;
  fstab_line: string;
  persist_hint: string;
}

/** One bulky kind of file FrameFound makes, and where it lives. */
export interface KindStatus {
  kind: "proxies" | "renders" | "exports" | "basemaps" | "models" | "backups" | "grabs";
  label: string;
  /** One plain sentence, ready to show. */
  description: string;
  /** The folder in use. Null only for backup copies when none is kept. */
  path: string | null;
  default_path: string | null;
  /** Its folder inside the bulk folder, when there is one and the kind is
   *  not already in it: what a move's folder is filled in with. */
  bulk_path: string | null;
  /** local = its usual place on the server's own disk; none = backup copies
   *  with no folder. */
  where: "local" | "bulk" | "custom" | "none";
  connected: boolean;
  /** Why its folder cannot be used right now. Has server paths. */
  problem: string | null;
  /** Null when counting would need a drive that is away. */
  files: number | null;
  size_bytes: number | null;
  /** Of the disk the folder is on; null when it is not connected. */
  free_gb: number | null;
  total_gb: number | null;
  /** False for frame grabs (always on this machine). */
  movable: boolean;
  /** Where the last move left this kind's old copies, until they are removed. */
  old_path: string | null;
}

/** The one move the Storage page can have running, or the last one and how
 *  it ended. It lives on the server, so the page can be closed meanwhile. */
export interface MoveStatus {
  id: string;
  kind: string;
  label: string;
  source: string | null;
  target: string;
  mode: "copy" | "empty";
  phase:
    | "queued"
    | "checking"
    | "copying"
    | "finishing"
    | "switching"
    | "done"
    | "failed"
    | "cancelled";
  /** Waiting or running: keep asking, and nothing else can be moved. */
  active: boolean;
  /** Cancel was asked for and the move has not stopped yet. */
  cancelling: boolean;
  /** Of the pass in hand: they start again for each short catch-up pass
   *  while "finishing". At "done" they are what the new folder now holds. */
  files_total: number;
  files_done: number;
  bytes_total: number;
  bytes_done: number;
  /** Set at "done": files that had to be copied (the rest were there). */
  copied: number | null;
  /** The sentence to show for a move that failed or was cancelled. */
  error: string | null;
  requested_by: string;
  requested_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface RebuildProxiesResult {
  /** Proxies the catalogue lists as made, and of those how many have no file. */
  on_record: number;
  missing: number;
  /** Videos that should have a proxy and never got one. */
  never_made: number;
  /** How many were queued to be made. */
  queued: number;
}

export interface StorageKinds {
  /** The one folder for everything bulky, if one has been named. */
  bulk_root: string | null;
  /** Set only when FrameFound's own record of where things live cannot be read. */
  problem: string | null;
  kinds: KindStatus[];
  move: MoveStatus | null;
  /** A move this page is not following is running (one started from a
   *  terminal): the sentence to show. Nothing can be moved until it ends. */
  busy: string | null;
  /** This account may start a move or name the bulk folder right now. */
  can_move: boolean;
}

export interface BulkFolderResult {
  bulk_root: string;
  /** Kinds that already hold files and were left where they are. */
  staying: string[];
}

export interface OldCopiesResult {
  kind: string;
  folder: string;
  files: number;
  bytes: number;
  /** Old files left alone because the folder in use has no copy of them. */
  kept: number;
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

export interface PathMappingInput {
  profile_name: string;
  platform: "windows" | "macos" | "linux";
  mapped_prefix: string;
}

export interface PathMapping extends PathMappingInput {
  id: string;
  /** The library root as that machine would see it — a worked example. */
  example: string;
}

export interface PanelToken {
  id: string;
  name: string;
  host: "premiere" | "lightroom" | "mcp" | "other";
  scopes: string[];
  prefix: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  last_used_ip: string | null;
  revoked: boolean;
}

export interface PanelTokenCreated extends PanelToken {
  /** Returned exactly once. There is no endpoint that can show it again. */
  token: string;
}

export interface FaceGuess {
  person_id: string;
  name: string;
  similarity: number;
  confident: boolean;
}

export interface FaceInPhoto {
  face_id: string;
  box_x: number;
  box_y: number;
  box_w: number;
  box_h: number;
  detection_score: number;
  source: string;
  person_id: string | null;
  person_name: string;
  guesses: FaceGuess[];
}

export interface FacesInPhoto {
  asset_id: string;
  faces: FaceInPhoto[];
  note: string;
}

export interface TagSuggestion {
  id: string;
  name: string;
  slug: string;
  asset_count: number;
  exact: boolean;
}

export interface NameSuggestion {
  id: string;
  name: string;
  confirmed_count: number;
  exact: boolean;
  cover: FaceRef | null;
}

export interface Basemap {
  name: string;
  label: string;
  installed: boolean;
  size_gb: number | null;
  approx_gb: number | null;
  tiles_url: string;
}

export interface BasemapList {
  basemaps: Basemap[];
  installed_count: number;
  note: string;
}

export interface SlideshowTheme {
  slug: string;
  label: string;
  hold_seconds: number;
  transition_seconds: number;
  accent: string;
}

export interface ProposedSlide {
  asset_id: string;
  filename: string;
  media_type: MediaType;
  captured_at: string | null;
  theme_score: number;
  people: string[];
  has_preview: boolean;
}

export interface SlideshowProposal {
  theme: string;
  slides: ProposedSlide[];
  considered: number;
  dropped_duplicates: number;
  people_covered: string[];
  people_missing: string[];
  note: string;
}

export interface Slideshow {
  id: string;
  title: string;
  theme: string;
  status: "pending" | "rendering" | "ready" | "failed";
  slide_count: number;
  segments_done: number;
  duration_seconds: number | null;
  size_mb: number | null;
  error: string | null;
  created_at: string;
  video_url: string | null;
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
  /** What the face is in: a photograph or a clip. */
  media_type: MediaType;
  /** When in the clip the face is seen, in milliseconds; null for a photograph. */
  ts_ms: number | null;
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
  suggestion_count: number;
}

export interface DiscoverResult {
  found: number;
  searched: number;
  threshold: number;
}

/** A bulk judgement: these exact faces, or every face this good and better. */
export type BulkPick = { faceIds: string[] } | { minSimilarity: number };

function bulkBody(pick: BulkPick) {
  return "faceIds" in pick
    ? { face_ids: pick.faceIds }
    : { min_similarity: pick.minSimilarity };
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

/** An asset matched by a local model's caption. `ts_ms` is the middle of
 * the shot the caption is about; null for a photograph. */
export interface CaptionHit {
  asset_id: string;
  filename: string;
  library_id: string;
  media_type: MediaType;
  caption: string;
  ts_ms: number | null;
}

/** One asset in the fused ranking, with why it matched. `ts_ms` is the
 * moment to open it at (speech if words matched, else the best frame);
 * `frame_ms` is that best frame alone — a transcript time is not a
 * sampled frame, so thumbnails use `frame_ms`. */
export interface FusedResult {
  asset_id: string;
  filename: string;
  library_id: string;
  media_type: MediaType;
  score: number;
  reasons: string[];
  ts_ms: number | null;
  frame_ms: number | null;
  snippet: string | null;
  tag: string | null;
  /** A local model's 0-100 score, where one has looked at it. */
  look_score: number | null;
  /** The model's caption that matched, when `reasons` holds "caption". */
  caption: string | null;
}

/** A workstation's view of one library (Libraries -> path profiles). */
export interface PathProfile {
  profile_name: string;
  platform: "windows" | "macos" | "linux";
  library_id: string;
  library_name: string;
  server_prefix: string;
  mapped_prefix: string;
}

/** One moment to put in a selects reel: a hit (`at_s`) FrameFound widens to
 * the sentence or the shot around it. */
export interface SelectMoment {
  id: string;
  at_s?: number | null;
  kind?: "speech" | "visual" | "tag" | "name" | null;
  reason?: string;
}

export interface SelectsResult {
  title: string;
  filename: string;
  profile: string | null;
  xml: string;
  selects: Array<{ name: string; filename: string; in_s: number; out_s: number; reason: string }>;
  clips: number;
  duration_s: number;
  unmapped: string[];
  offline: string[];
  note: string;
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
  /** "indexing" clears itself as frames are embedded; "unavailable" (no
   * model, or the encoder crashed) will not, so the two are told apart.
   * Absent on a server older than the field. */
  visual_status?: VisualStatus;
  /** visual_status === "ready"; kept for older clients. */
  visual_available: boolean;
  tag_hits: TagHit[];
  /** Assets found by what a local model said they show. Absent on a
   *  server older than the field. */
  caption_hits?: CaptionHit[];
  results: FusedResult[];
}

export type VisualStatus = "ready" | "indexing" | "unavailable";

export interface RoomOption {
  key: string;
  label: string;
}

export interface ListingItem {
  asset_id: string;
  filename: string;
  media_type: MediaType;
  position: number;
  room: string;
  room_label: string;
  room_source: "suggested" | "confirmed";
  room_score: number | null;
  /** A develop recipe exists; the export will apply it. */
  edited: boolean;
  /** When the newest recipe was saved — how a run's progress is told apart
   *  from edits that were already there. */
  edited_at: string | null;
  /** What it shows — the photo index line. */
  caption: string;
  /** The words in its exported file name. */
  slug: string;
  /** "" unnamed, "suggested" by the AI, "confirmed" by you. */
  naming_source: "" | "suggested" | "confirmed";
  /** When the AI last named it — naming-run progress. */
  named_at: string | null;
  /** Its name in an SEO-named export; null for videos. */
  export_name: string | null;
}

export interface ListingSummary {
  id: string;
  name: string;
  item_count: number;
  export_status: "none" | "queued" | "exporting" | "ready" | "failed";
  export_error: string | null;
  cover_asset_id: string | null;
}

export interface ListingDetail extends ListingSummary {
  items: ListingItem[];
  classified: boolean;
  /** A zip exists but the listing changed since; the download refuses it. */
  export_stale: boolean;
  /** Ends every exported file name ("" = use suggested_suffix). */
  file_suffix: string;
  suggested_suffix: string;
  /** Heads the photo index: auction date, terms. */
  notes: string;
  /** The auto-edit run, held on the server: one at a time per listing. */
  ai_edit_state: "idle" | "queued" | "running" | "done" | "failed";
  /** "local": applying the local model's suggestions to ticked photographs. */
  ai_edit_mode: "" | "ai" | "preset" | "describe" | "local";
  ai_edit_started_at: string | null;
  /** What the last run did, e.g. "62 edited, 1 skipped — first: …". */
  ai_edit_message: string;
}

export type ListingNaming = "seo" | "simple";

/** What a showcase looks for: the finished work, a crew in a scenic
 *  setting, dramatic construction, or a company group photo. */
export type ShowcaseKind = "finished" | "crew" | "construction" | "group";

/** Showcase: the best photographs, one place at a time. */
export interface ShowcaseRequest {
  library_ids: string[];
  subject: string;
  avoid: string;
  count: number;
  alternates: number;
  orientation: "landscape" | "portrait" | "any";
  min_megapixels: number;
  allow_people: boolean;
  kind: ShowcaseKind;
  /** Only these places (labels from showcasePlaces); empty for all. */
  places: string[];
  /** Leave out what these listings hold, and near-duplicates of it. */
  exclude_listing_ids: string[];
  /** Rank video frames with the stills (full-size grabs on import). */
  include_video: boolean;
  /** Only what was taken in this window, whole days inclusive (YYYY-MM-DD). */
  captured_from?: string | null;
  captured_to?: string | null;
}

export interface ShowcasePick {
  asset_id: string;
  filename: string;
  relative_path: string;
  width: number;
  height: number;
  megapixels: number;
  captured_at: string | null;
  score: number;
  parts: Record<string, number>;
  /** "spring" | "summer" | "fall" | "winter", or "" undated. */
  season: string;
  /** "drone" | "ground". */
  source: string;
  /** A frame of a video (asset_id is the video): its time in ms. */
  frame_ms: number | null;
}

export interface ShowcasePlaceCount {
  key: string;
  label: string;
  photos: number;
}

export interface ShowcaseListingPick {
  asset_id: string;
  place: string;
  kind: ShowcaseKind;
  season: string;
  source: string;
  frame_ms: number | null;
}

export interface ShowcasePlace {
  key: string;
  label: string;
  picks: ShowcasePick[];
}

export interface ShowcaseResponse {
  places: ShowcasePlace[];
  considered: number;
}

export interface SkyChoice {
  name: string;
  feather: number;
  shift: number;
  relight: number;
}

/** Slider values for the develop engine. 0 everywhere = the original. */
export interface DevelopRecipe {
  exposure: number;
  contrast: number;
  temperature: number;
  tint: number;
  shadows: number;
  highlights: number;
  vibrance: number;
  saturation: number;
  rotate: number;
  keystone: number;
  window_pull: number;
  auto_wb: number;
  local_contrast: number;
  auto: boolean;
  sky: SkyChoice | null;
}

export const EMPTY_RECIPE: DevelopRecipe = {
  exposure: 0,
  contrast: 0,
  temperature: 0,
  tint: 0,
  shadows: 0,
  highlights: 0,
  vibrance: 0,
  saturation: 0,
  rotate: 0,
  keystone: 0,
  window_pull: 0,
  auto_wb: 0,
  local_contrast: 0,
  auto: false,
  sky: null,
};

export interface SkyAsset {
  name: string;
  size_bytes: number;
}

export interface SkyInfo {
  fraction: number;
  usable: boolean;
  available: boolean;
}

export interface ListingFolder {
  library_id: string;
  library_name: string;
  path: string;
  image_count: number;
}

export interface FolderAsset {
  asset_id: string;
  filename: string;
  media_type: MediaType;
}

export interface RemovalSuggestion {
  asset_id: string;
  filename: string;
  media_type: MediaType;
  reason: string;
  keep_instead: string | null;
  /** The local model's 0-100 score of this photograph, when it has one. */
  score: number | null;
}

export interface AiEditSettings {
  configured: boolean;
  enabled: boolean;
  model: string;
  /** Shipped photos the installed learned look was made from; 0 = none. */
  look_examples: number;
  /** The monthly ceiling on every Claude API call, and what the ledger says is spent. */
  monthly_budget_usd: number;
  spent_this_month_usd: number;
  review_model: string;
  review_auto: boolean;
  review_batch: number;
  /** The model a new chat-panel conversation uses, and the ones it may be
   *  set to (only models FrameFound has a price for). */
  chat_model: string;
  chat_models: string[];
}

export interface GdriveSettings {
  configured: boolean;
  enabled: boolean;
  client_email: string;
  /** Drive folders collected nightly as training pairs. */
  training_folder_ids: string[];
}

export interface GdriveRename {
  file_id: string;
  old_name: string;
  new_name: string;
  room: string;
  room_label: string;
  score: number;
  source: "catalogue" | "thumbnail";
}

export interface GdriveSkipped {
  file_id: string;
  name: string;
  reason: string;
}

export interface GdrivePreview {
  folder_id: string;
  folder_name: string;
  total: number;
  renames: GdriveRename[];
  skipped: GdriveSkipped[];
  from_catalogue: number;
  from_thumbnail: number;
}

export interface GdriveApplyResult {
  renamed: number;
  failed: GdriveSkipped[];
  manifest_file_id: string | null;
}

export interface GdriveUndoResult {
  restored: number;
  failed: GdriveSkipped[];
}

export interface InpaintVersion {
  version: number;
  status: "queued" | "running" | "ready" | "failed";
  error: string | null;
  created_at: string;
}

export interface InpaintState {
  asset_id: string;
  versions: InpaintVersion[];
  busy: boolean;
}

export interface EditState {
  asset_id: string;
  recipe: Partial<DevelopRecipe>;
  version: number;
  edited: boolean;
}

export interface ProcessingReport {
  queue_depths: Record<string, number>;
  assets_by_status: Record<string, number>;
  derivatives: Record<string, Record<string, number>>;
  jobs_last_hour: Record<string, number>;
  /** worker -> status -> count; "server" is the server's own containers. */
  workers_last_hour?: Record<string, Record<string, number>>;
  recent_failures: Array<{
    id: string;
    task_name: string;
    asset_id: string | null;
    error: string | null;
    started_at: string;
    worker?: string | null;
  }>;
}

export interface VolumeStatus {
  label: string;
  path: string;
  total_gb: number;
  free_gb: number;
  used_percent: number;
  status: "ok" | "low" | "full" | "unreachable";
  detail: string;
}

export interface BackupStatus {
  status: "ok" | "stale" | "missing";
  last_backup_at: string | null;
  detail: string;
}

export interface HealthReport {
  version: string;
  database: { status: string; detail: string | null };
  queue: { status: string; detail: string | null };
  data_dir_free_gb: number | null;
  volumes: VolumeStatus[];
  backup: BackupStatus;
}

export interface SystemAlert {
  level: "error" | "warning";
  title: string;
  detail: string;
  href: string;
}

// --- The chat panel (`/chat`). docs/chat.md says what the server does. ---

/** Whether the panel can be used at all, and by this account. */
export interface ChatStatus {
  /** The server has the packages the panel needs. */
  available: boolean;
  /** An Anthropic key is stored / Claude has not been switched off. */
  configured: boolean;
  enabled: boolean;
  /** Why a message would not be answered, as a sentence to show in place of
   *  the text box; null when nothing is in the way. */
  problem: string | null;
  /** False for a read-only account: it may read its conversations only. */
  can_chat: boolean;
  /** The model a new conversation starts on, and the ones on offer. */
  model: string;
  models: string[];
  /** The monthly ceiling shared with every other Claude feature. */
  budget_usd: number;
  spent_this_month_usd: number;
}

/** `running` with no `run` in the conversation = a turn the server lost. */
export type ChatState = "idle" | "running" | "awaiting_confirmation";

export interface ChatSummary {
  id: string;
  /** The first line typed; "" until there is a message. */
  title: string;
  /** A conversation keeps the model it was created with. */
  model: string;
  /** Whether pictures Claude asks to see are sent to it. */
  pictures: boolean;
  state: ChatState;
  spent_usd: number;
  created_at: string;
  updated_at: string;
}

/**
 * Where the reader was when they wrote a message. The server keeps these
 * keys and drops any other; strings are cut to 300 characters and
 * `selection` to its first 50 ids (`selection_count` is the server's count
 * of what it was sent, and is only ever read back).
 */
export interface ChatContext {
  path?: string;
  title?: string;
  library_id?: string;
  library?: string;
  folder?: string;
  asset_id?: string;
  filename?: string;
  /** Seconds into a clip. */
  at_s?: number;
  listing_id?: string;
  person_id?: string;
  tag?: string;
  query?: string;
  selection?: string[];
  selection_count?: number;
}

/** A picture a tool returned: `chatBlobUrl(conversation, blob)`. */
export interface ChatPicture {
  blob: string;
  media_type: string;
}

/** A file a tool made (a rough cut): `chatArtifactUrl(conversation, chip id)`. */
export interface ChatArtifact {
  name: string;
}

/** A tool Claude asked for, and what became of it. */
export interface ChatChip {
  id: string;
  name: string;
  input: Record<string, unknown>;
  /** `pending` is waiting for a yes or no. `running` in a conversation
   *  with no turn running was interrupted and will not finish. */
  state: "running" | "ran" | "failed" | "pending" | "approved" | "declined";
  needs_confirm: boolean;
  /** True only for `ran` and `approved`. */
  ok: boolean;
  /** One line of the tool's answer; "" until it has one. */
  summary: string;
  /** What saying yes will do; non-null exactly when `needs_confirm`. Reads
   *  "Run <tool name>" when Claude wrote arguments of the wrong kind. */
  ask: string | null;
  images: ChatPicture[];
  artifact: ChatArtifact | null;
}

export interface ChatMessage {
  id: string;
  /** Has gaps: the turns that carry tool answers back to Claude are stored
   *  and not shown. */
  seq: number;
  role: "user" | "assistant";
  /** What was typed, or Claude's text (which may be ""). */
  text: string;
  /** The page context sent with a typed message; null for Claude's. */
  context: ChatContext | null;
  tools: ChatChip[];
  stop_reason: string | null;
  cost_usd: number | null;
  created_at: string;
}

/** A call waiting for a yes or no: the same four fields as the `confirm` event. */
export interface ChatPending {
  id: string;
  name: string;
  input: Record<string, unknown>;
  summary: string;
}

/** The turn the server holds in memory for a conversation. */
export interface ChatRun {
  /** Still going. False = finished, kept ten minutes for replay. */
  active: boolean;
  /** How many events it has produced so far. */
  events: number;
  /** The `seq` of the stored message it began from; null until it has said `start`. */
  seq: number | null;
}

export interface ChatConversation extends ChatSummary {
  input_tokens: number;
  output_tokens: number;
  /** How the last turn ended. While `state` is `running` this is the turn
   *  before: ignore it. `message` is null for `end` and `awaiting_confirmation`. */
  last_turn: { reason: string; message: string | null } | null;
  run: ChatRun | null;
  messages: ChatMessage[];
  pending: ChatPending[];
}

export interface ChatDecision {
  tool_use_id: string;
  approve: boolean;
}

/** What each event of a turn carries, by the event's name. */
export interface ChatEventData {
  /** `message_id` is null when the turn carries on from a yes or no. `seq`
   *  is the stored message the turn began from. */
  start: {
    conversation_id: string;
    model: string;
    pictures: boolean;
    message_id: string | null;
    seq: number | null;
  };
  thinking: { on: boolean };
  text: { delta: string };
  /** `preparing` (Claude is still writing the call; `input` is null), then `running`. */
  tool: {
    id: string;
    name: string;
    input: Record<string, unknown> | null;
    state: "preparing" | "running";
  };
  /** After every request to Claude. It closes the answer being written —
   *  which is not to say an answer was stored: a request that failed
   *  part-way is billed too, and its `usage` comes just before `error`. */
  usage: {
    cost_usd: number;
    conversation_usd: number;
    month_usd: number;
    budget_usd: number;
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    cache_write_tokens: number;
  };
  tool_result: {
    id: string;
    name: string;
    state: "ran" | "failed" | "approved" | "declined";
    ok: boolean;
    summary: string;
    images: ChatPicture[];
    artifact: ChatArtifact | null;
  };
  confirm: ChatPending;
  /** Always followed by `done` with the same sentence. */
  error: { kind: "config" | "budget" | "api" | "internal"; message: string };
  /** Always last. `reason` is one of end, awaiting_confirmation, stopped,
   *  budget, steps, refusal, max_tokens, error. */
  done: { reason: string; message: string | null };
}

/** One event of a turn. `index` counts from 0 within the turn (null if the
 *  event carried no id). */
export type ChatEvent = {
  [Name in keyof ChatEventData]: {
    event: Name;
    index: number | null;
    data: ChatEventData[Name];
  };
}[keyof ChatEventData];

// ---- AI models: models that run on the Mac itself (/local-models) ----------
//
// The server decides everything the page shows, including whether models can
// run here at all (`mode`): the page never guesses from the browser. Times
// are UTC ("…Z") except `LocalModelsNow`, which is the Mac's own wall clock.

/** `unavailable`: the container server, where no model can run (every write
 *  answers 409). `demo`: a development machine pretending (nothing is real). */
export type LocalModelsMode = "ready" | "demo" | "unavailable";
/** Which memory budget a model's size fits: the daytime one, only the
 *  overnight one, or neither. */
export type LocalModelFits = "day" | "night" | "never";
export type LocalModelServerState = "down" | "idle" | "loading" | "ready" | "busy";
/** As the server reads a job: a running job whose worker stopped beating
 *  already reads `failed`. queued, waiting and running are the active ones. */
export type LocalModelJobState = "queued" | "waiting" | "running" | "done" | "failed" | "cancelled";
export type LocalModelPublisherIs = "maker" | "converter" | "blocked" | "unknown";
export type LocalAiRoleName = "looker" | "editor";

/** One piece of work on the models' own lane (a download, a copy to this Mac,
 *  an install, a test). One runs at a time. */
export interface LocalModelJob {
  id: string;
  /** download | place | runtime_install | test (later updates add more). */
  kind: string;
  title: string;
  /** `publisher/name@<8 of the build>`, or "<program> <version>" for an install. */
  subject: string;
  model_id: string | null;
  state: LocalModelJobState;
  /** The sentence to show: why it waits and until when, how it ended. */
  message: string;
  /** Technical detail (paths, the last lines a program printed). Admins only. */
  detail: string | null;
  not_before: string | null;
  /** "downloading" / "copying" while bytes move; then done and total are bytes. */
  phase: string;
  done: number;
  total: number;
  cancelling: boolean;
  can_cancel: boolean;
  requested_by: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

/** The newest Test of a model. The fields after `finished_at` stay null (or
 *  false) until it has finished. */
export interface LocalModelTest {
  job_id: string;
  state: LocalModelJobState;
  message: string;
  detail: string | null;
  not_before: string | null;
  asked_at: string | null;
  finished_at: string | null;
  kind: "picture" | "text" | null;
  /** The picture it was shown: from the catalogue (`asset_id`) or drawn. */
  sample: {
    asset_id: string | null;
    media_type: string | null;
    filename: string | null;
    label: string;
  } | null;
  answer: Record<string, unknown> | null;
  /** What the model said, as text. Show it as text, never as HTML. */
  raw: string | null;
  structured_ok: boolean;
  seconds: number | null;
  load_seconds: number | null;
  memory_gb: number | null;
  memory_source: string | null;
  ran_from: string | null;
  attempts: number | null;
  finish_reason: string | null;
  model: { repo_id: string; revision_short: string } | null;
  runtime: string | null;
  runtime_version: string | null;
  prompt_version: string | null;
  demo: boolean;
}

export interface LocalModelBuiltFrom {
  repo_id: string;
  publisher: string | null;
  publisher_is: LocalModelPublisherIs;
  /** False: Hugging Face has no such model now. */
  found: boolean;
}

/** One installed (or downloading) build of a model. */
export interface LocalModel {
  id: string;
  repo_id: string;
  name: string;
  revision: string;
  revision_short: string;
  /** The build of a repository that holds several (llama.cpp's files);
   *  "" for MLX. */
  variant: string;
  /** What the page shows for the build: "" or its id ("Q4_K_M"). */
  variant_label: string;
  build_date: string | null;
  /** That exact build's page on Hugging Face. */
  url: string;
  publisher: string;
  publisher_is: LocalModelPublisherIs;
  maker: string | null;
  built_from: LocalModelBuiltFrom[];
  approved: boolean;
  /** Why it is no longer approved. */
  approval_problem: string | null;
  licence: string;
  licence_name: string;
  licence_url: string | null;
  gated: boolean;
  size_bytes: number;
  /** The figure that decides what fits (measured once it has run). */
  memory_gb: number;
  memory_measured: boolean;
  fits: LocalModelFits;
  /** What its page says it reads: "image", "text", "audio", "video". */
  takes: string[];
  runtime: string;
  runtime_label: string;
  runtime_known: boolean;
  /** Its software asks once before the model is given a role it has not
   *  been compared for (llama.cpp); false when the software is not here. */
  needs_compare: boolean;
  /** Seen answering on your footage: a finished Compare column (or, for a
   *  model that reads no pictures, a text Test answered in the form asked).
   *  Judged for the Looker when it reads pictures, else for the Editor;
   *  always true for software that does not ask (MLX-VLM). */
  compared: boolean;
  compared_at: string | null;
  compared_by: "compare" | "test" | null;
  runtime_installed: boolean;
  runtime_problem: string | null;
  state: "downloading" | "installed" | "incomplete";
  error: string | null;
  /** null: nobody could look (the library did not answer). */
  in_library: boolean | null;
  on_this_machine: boolean | null;
  roles: string[];
  pending_roles: string[];
  role_problems: Record<string, string | null>;
  last_loaded_with: string | null;
  /** The active download, else the active copy to this Mac. */
  job: LocalModelJob | null;
  test: LocalModelTest | null;
  demo: boolean;
  added_by: string | null;
  added_at: string | null;
}

/** The Looker or the Editor, and which model has it. */
export interface LocalModelRole {
  role: string;
  title: string;
  description: string;
  model_id: string | null;
  model_name: string | null;
  /** True also with a `problem` that is only information ("Only runs
   *  overnight (21:30–05:30)."). */
  ready: boolean;
  problem: string | null;
  /** Set while a copy to this Mac is under way; the old model keeps the role
   *  until it is whole. */
  pending_model_id: string | null;
  pending_model_name: string | null;
  /** The active copy to this Mac. */
  job: LocalModelJob | null;
  assigned_at: string | null;
  /** false: its model holds the role without having been compared for it
   *  (given it anyway). null: no model, or software that does not ask. */
  compared: boolean | null;
}

/** A kind of software that runs models (MLX-VLM, llama.cpp). */
export interface LocalModelRuntime {
  name: string;
  label: string;
  summary: string;
  demo: boolean;
  supported: boolean;
  unsupported_reason: string | null;
  installed: string | null;
  previous: string | null;
  installed_at: string | null;
  versions: string[];
  about: string;
  /** More about the installed version ("build b12110"); null when there is
   *  nothing more to say or nothing is installed. */
  installed_detail: string | null;
  update_note: string;
  /** What "Install a particular version" takes: "0.7.12", "0.9.0 or a daily
   *  build such as b12318". */
  version_hint: string;
  /** Models added that run on it, whatever their state. */
  models: number;
  latest: string | null;
  latest_checked_at: string | null;
  latest_problem: string | null;
  update_available: boolean;
  problem: string | null;
  /** The active install. */
  job: LocalModelJob | null;
}

export interface LocalModelsSettings {
  day_budget_gb: number;
  night_budget_gb: number;
  /** "HH:MM" on the Mac's clock. */
  night_start: string;
  night_end: string;
  idle_unload_minutes: number;
  paused: boolean;
  paused_at: string | null;
  /** The email of whoever paused it. */
  paused_by: string | null;
  hf_token_configured: boolean;
  hf_token_problem: string | null;
}

export interface LocalModelsPublishers {
  makers: string[];
  converters: string[];
  /** Built-in names first; those cannot be removed. */
  blocked: { name: string; built_in: boolean }[];
}

/** The Mac's own clock, as plain local strings with no zone. */
export interface LocalModelsNow {
  /** "2026-11-12T14:05:00" */
  local_time: string;
  overnight: boolean;
  /** The budget in force now. */
  budget_gb: number;
  /** When the budget next changes; null when there is no overnight window. */
  changes_at: string | null;
}

export interface LocalModelServer {
  state: LocalModelServerState;
  model_id?: string | null;
  model_name?: string | null;
  runtime?: string | null;
  runtime_version?: string | null;
  memory_gb?: number | null;
  loaded_for_s?: number | null;
  unloads_in_s?: number | null;
  problem?: string | null;
}

export interface LocalModelLibrary {
  /** An administrator's only (null otherwise). */
  path: string | null;
  where: "local" | "bulk" | "custom";
  connected: boolean;
  /** For an administrator it names paths; others get a sentence without. */
  problem: string | null;
  free_gb: number | null;
  /** True when a model given a role is copied here from elsewhere. */
  copies_to_this_machine: boolean;
}

/** Everything the AI models page draws, in one answer. In `unavailable` mode
 *  roles, models, runtimes and jobs are empty, the server is `down` and the
 *  library is null. */
export interface LocalModelsOverview {
  mode: LocalModelsMode;
  /** The unavailable or demonstration sentence; null when ready. */
  note: string | null;
  /** An administrator, and models can run here. */
  can_manage: boolean;
  machine: { system: string; machine: string; memory_gb: number | null };
  now: LocalModelsNow;
  settings: LocalModelsSettings;
  publishers: LocalModelsPublishers;
  server: LocalModelServer;
  library: LocalModelLibrary | null;
  roles: LocalModelRole[];
  /** Newest first. */
  models: LocalModel[];
  runtimes: LocalModelRuntime[];
  /** Every active job, then the last 15 finished, newest first. */
  jobs: LocalModelJob[];
}

/** One line of the Check card. */
export interface LocalModelCheck {
  /** publisher, built_from, licence, access, build (only with several
   *  builds), files, size, memory, runtime */
  key: string;
  label: string;
  tone: "ok" | "warn" | "bad";
  value: string;
  detail: string | null;
}

/** One file a build downloads: the model, the part that reads pictures, or
 *  a text such as its README. */
export interface LocalModelVariantFile {
  path: string;
  size: number;
  part: "model" | "pictures" | "text";
}

/** One build of a repository that holds several (llama.cpp's files). */
export interface LocalModelVariant {
  id: string;
  /** "Q4_K_M — 24.3 GB" */
  label: string;
  quant: string;
  /** Bits per weight its name says; null when it says none. */
  bits: number | null;
  size_bytes: number;
  memory_gb: number;
  fits: LocalModelFits;
  /** Exactly one build of the list is the suggested one. */
  recommended: boolean;
  takes: string[];
  /** "" or the server's sentence about a very small build. */
  note: string;
  files: LocalModelVariantFile[];
}

/** What Check found. Changes nothing; Download sends `revision` back. */
export interface LocalModelPreflight {
  repo_id: string;
  /** The 40-character build to download, exactly. */
  revision: string;
  revision_short: string;
  build_date: string | null;
  ok: boolean;
  verdict: string;
  checks: LocalModelCheck[];
  problems: string[];
  warnings: string[];
  notes: string[];
  access: "open" | "ok" | "needs_token" | "no_access" | "untested";
  size_bytes: number;
  memory_gb: number;
  /** null: no build could be sized. */
  fits: LocalModelFits | null;
  takes: string[];
  runtime: { name: string; label: string; installed: boolean } | null;
  /** Empty unless the repository holds more than one build. */
  variants: LocalModelVariant[];
  /** The build this check is about (the suggested one unless another was
   *  asked for); null for a repository of one build. */
  variant: string | null;
  /** A build of this model already installed; `same_build` only for the
   *  same revision and the same build. */
  existing: {
    model_id: string;
    revision_short: string;
    variant: string;
    same_build: boolean;
  } | null;
}

/** Every field optional; one left out (or null) is left alone. */
export interface LocalModelsSettingsPatch {
  day_budget_gb?: number | null;
  night_budget_gb?: number | null;
  night_start?: string | null;
  night_end?: string | null;
  idle_unload_minutes?: number | null;
  paused?: boolean | null;
}

export interface LocalModelsSettingsSaved {
  settings: LocalModelsSettings;
  now: LocalModelsNow;
}

/** Each list is sent whole; one left out is left alone. */
export interface LocalModelsPublishersPatch {
  makers?: string[];
  converters?: string[];
  blocked_added?: string[];
}

export interface LocalModelsPublishersSaved {
  publishers: LocalModelsPublishers;
  /** Installed models a removal left without approval. */
  no_longer_approved: { id: string; repo_id: string; reason: string; roles: string[] }[];
}

// ---- Compare: local models side by side on your footage (/local-models/compare) ----
//
// A comparison asks every chosen model about the same fixed sample and lays
// the answers beside what a person already decided. Its figures are counts
// of agreement, never a grade: no field ranks a model. Reads are anyone's;
// every write is an administrator's and answers 409 with L1's sentence where
// models cannot run. Times are UTC ("…Z").

/** A comparison as a whole: `running` while any model is queued or running,
 *  `waiting` while none runs and some wait, `stopped` when one stopped or
 *  failed and none is active, `empty` with no models. */
export type CompareState = "running" | "waiting" | "finished" | "stopped" | "empty";
/** One model's column, as `comparisons.shown` reads it. */
export type CompareEntryState = "queued" | "running" | "waiting" | "done" | "stopped" | "failed";
export type CompareWhat = "shots" | "photos" | "both";
export type CompareSourceKind = "folder" | "listing" | "cuts" | "comparison";
/** "" no decision yet, "chosen" a model looks, "none" none is good enough yet. */
export type CompareVerdictValue = "" | "chosen" | "none";

export interface CompareLimits {
  min_size: number;
  max_size: number;
  default_size: number;
  max_models: number;
}

/** An installed model, as the builder offers it. */
export interface CompareModelChoice {
  id: string;
  /** The repository's name, with " <variant>" for a build of several. */
  name: string;
  repo_id: string;
  revision_short: string;
  variant: string;
  can_compare: boolean;
  /** Why it cannot be: "It does not read pictures." Null when it can. */
  reason: string | null;
  /** True when `reason` is that the program it runs on is not installed:
   *  the builder links to Software that runs models. Absent (false) from a
   *  server older than that check. */
  program_missing?: boolean;
  fits: LocalModelFits;
  memory_gb: number;
  /** Roles it holds now; non-empty means it is on this Mac, empty that it
   *  loads from the model library. */
  roles: string[];
  /** About how long one item took it before, in seconds; null if never. */
  seconds_hint: number | null;
  demo: boolean;
}

export interface ComparisonSummary {
  id: string;
  name: string;
  source_kind: CompareSourceKind;
  source_label: string;
  size: number;
  photos: number;
  shots: number;
  /** False when it was made with an earlier version of the question. */
  current: boolean;
  /** In the order added; never ordered by a figure. */
  models: { entry_id: string; name: string; state: CompareEntryState; done: number; failed: number }[];
  state: CompareState;
  verdict: CompareVerdictValue;
  verdict_model: string | null;
  created_at: string | null;
  created_by: string | null;
}

/** The list page and the builder's models. In `unavailable` mode `models`
 *  is empty and the comparisons still read. */
export interface CompareOverview {
  mode: LocalModelsMode;
  note: string | null;
  can_manage: boolean;
  prompt_version: string;
  limits: CompareLimits;
  /** Every installed model, newest first. */
  models: CompareModelChoice[];
  /** Newest first, at most 100. */
  comparisons: ComparisonSummary[];
}

/** A folder found by its name, counted with everything under it. A library's
 *  root is `path: ""`. */
export interface CompareFolder {
  library_id: string;
  library_name: string;
  path: string;
  photos: number;
  clips: number;
  clips_with_shots: number;
  shots: number;
}

/** A recorded, finished cut (drafts are never offered). */
export interface CompareCut {
  id: string;
  title: string;
  client: string;
  /** "mcp" built with Claude; "import" an imported timeline. */
  source: string;
  created_at: string | null;
  pieces: number;
  clips: number;
  clips_with_shots: number;
  duration_s: number;
}

export type CompareSource =
  | { kind: "folder"; library_id: string; folder: string }
  | { kind: "listing"; listing_id: string }
  | { kind: "cuts"; cut_ids: string[]; neighbours: boolean }
  | { kind: "comparison"; comparison_id: string };

/** What to draw. A value left null is the server's to fill in (cuts always
 *  look at shots, a listing at photographs). `truth_cut_ids` is a folder's
 *  only: null counts every finished cut that touches it. */
export interface SampleRequest {
  source: CompareSource;
  what?: CompareWhat | null;
  size?: number | null;
  stills?: 1 | 3 | null;
  truth_cut_ids?: string[] | null;
}

/** Start: the sample, the models (one to six) and an optional name (blank
 *  is "<label>, <day>"). A null seed draws a fresh sample. */
export interface StartComparisonRequest extends SampleRequest {
  name?: string | null;
  model_ids: string[];
  seed?: number | null;
}

export interface CompareLeftOut {
  why: string;
  count: number;
  sentence: string;
}

export interface CompareCountedCut {
  id: string;
  title: string;
  source: string;
  created_at: string | null;
}

/** What a sample would hold; drawn with seed 0, so Start's may differ in its
 *  items but not in the pool's counts. `ok: false` (a 200) with the reason
 *  in `notes[0]` when there would be nothing to compare on. */
export interface SamplePreview {
  ok: boolean;
  label: string;
  what: CompareWhat | null;
  size: number | null;
  stills: number | null;
  items: number;
  photos: number;
  shots: number;
  clips: number;
  pool: { photos: number; shots: number; clips: number };
  truth: {
    tags: { footage: number; yes: number; no: number };
    used: { used: number; unused: number; cuts: CompareCountedCut[] };
  };
  left_out: CompareLeftOut[];
  /** The server's sentences: a truth that is missing, "only a few were used",
   *  and how tags are checked. */
  notes: string[];
}

/** How a model answered: asked, answered in the form asked, second tries,
 *  and failures by reason. */
export interface CompareForm {
  asked: number;
  answered: number;
  second_tries: number;
  failed: Record<string, number>;
}

export interface ComparePace {
  answered: number;
  median_s: number | null;
  slowest_s: number | null;
  per_hour: number | null;
}

/** Agreement with decided tags at one confidence floor (0, 0.6, 0.85). */
export interface TagAgreement {
  floor: number;
  footage: number;
  yes_total: number;
  yes_found: number;
  no_total: number;
  no_repeated: number;
  proposed: number;
  unjudged: number;
  unjudged_new: number;
}

/** Agreement of the scores with the shots finished cuts used. */
export interface ScoreAgreement {
  used: number;
  unused: number;
  pairs: number;
  higher: number;
  lower: number;
  same: number;
  scored: number;
  distinct: number;
  few: boolean;
}

/** One column's figures. `tags` and `scores` are null until it is done, or
 *  when there is no such truth to check against. */
export interface CompareMeasures {
  form: CompareForm;
  shots: ComparePace;
  photos: ComparePace;
  tags: TagAgreement[] | null;
  scores: ScoreAgreement | null;
}

/** One model's column of a comparison. */
export interface ComparisonEntry {
  id: string;
  model_id: string | null;
  /** False once the model was removed; its answers are kept. */
  installed: boolean;
  name: string;
  repo_id: string;
  revision_short: string;
  variant: string;
  runtime: string | null;
  runtime_label: string | null;
  runtime_version: string | null;
  demo: boolean;
  state: CompareEntryState;
  /** The sentence to show: why it waits and until when, how it ended. */
  message: string;
  /** Technical detail; administrators only. */
  detail: string | null;
  not_before: string | null;
  cancelling: boolean;
  done: number;
  failed: number;
  total: number;
  load_seconds: number | null;
  ran_from: "this_machine" | "library" | null;
  memory_gb: number | null;
  memory_source: "runtime" | "process" | null;
  /** Both null once the model is no longer installed. */
  fits: LocalModelFits | null;
  roles: string[] | null;
  measures: CompareMeasures;
  can_cancel: boolean;
  can_carry_on: boolean;
  can_retry_failed: boolean;
  can_remove: boolean;
  started_at: string | null;
  finished_at: string | null;
  added_by: string | null;
}

export interface CompareCounts {
  photos: number;
  shots: number;
  clips: number;
  used: number;
  unused: number;
  footage_with_tags: number;
  yes: number;
  no: number;
}

export interface CompareVerdict {
  verdict: CompareVerdictValue;
  entry_id: string | null;
  note: string;
  at: string | null;
  by: string | null;
}

/** One comparison, without its items (poll this; re-read the items when
 *  `answers_stored` moves). */
export interface Comparison {
  id: string;
  name: string;
  source_kind: CompareSourceKind;
  source_label: string;
  what: CompareWhat;
  size: number;
  stills: number;
  seed: number;
  prompt_version: string;
  current: boolean;
  truth_at: string | null;
  created_at: string | null;
  created_by: string | null;
  counts: CompareCounts;
  counted_cuts: { id: string; title: string; source: string }[];
  left_out: CompareLeftOut[];
  vocabulary_size: number;
  /** Items every finished column answered: what the figures are counted over. */
  over: number;
  answers_stored: number;
  /** In the order added; never ordered by a figure. */
  entries: ComparisonEntry[];
  verdict: CompareVerdict;
  can_add_models: boolean;
  mode: LocalModelsMode;
  note: string | null;
  can_manage: boolean;
}

export interface CompareAnswerTag {
  name: string;
  confidence: number | null;
  in_vocabulary: boolean;
  /** What a person decided about this tag on this footage, if anything. */
  ruling: "yes" | "no" | null;
}

/** One model on one item. `raw` only when it failed or took a second try. */
export interface ComparisonAnswer {
  ok: boolean;
  problem: "bad_answer" | "died" | "timeout" | "runtime_error" | "no_picture" | null;
  message: string;
  /** Strongest first. */
  tags: CompareAnswerTag[];
  caption: string;
  score: number | null;
  reason: string;
  seconds: number | null;
  attempts: number;
  raw: string | null;
}

export interface ComparePicture {
  n: number;
  at_s: number;
  /** Ready to use as an <img> src. */
  url: string;
}

export interface ComparisonItem {
  id: string;
  position: number;
  kind: "shot" | "photo";
  asset_id: string | null;
  in_catalogue: boolean;
  media_type: string;
  filename: string;
  library: string;
  folder: string;
  start_s: number | null;
  end_s: number | null;
  facts_line: string;
  pictures: ComparePicture[];
  truth: { yes: string[]; no: string[] };
  used: boolean | null;
  used_in: string[];
  counted: boolean;
  /** By entry id; a column with no answer for the item has no key. */
  answers: Record<string, ComparisonAnswer>;
}

export interface ComparisonItems {
  total: number;
  items: ComparisonItem[];
}

/** Rename and decide. A field left out is left as it is. */
export interface ComparisonChange {
  name?: string;
  verdict?: CompareVerdictValue;
  entry_id?: string | null;
  note?: string;
}

// ---- Colour looks for clips (/clip-looks) and the local model on a listing --------
//
// A clip has three looks on the same still — the local model's suggestion,
// the learned look, no change — each shown through the LUT a download would
// hold. Colour is judged by eye: no field holds a colour difference, a score
// or a rank, and nothing is ever chosen for anyone. Choosing and downloading
// need no model and work on the container server; asking the model answers
// 409 with the sentence to show where models cannot run. Every route is any
// signed-in user's. Times are UTC ("…Z").

export type ClipLookSource = "model" | "learned" | "none";
export type ClipLookChosenSource = ClipLookSource | "custom";
/** The two LUT sizes: points a side. 33 is the standard. */
export type ClipLookSize = 33 | 65;

/** The eight sliders a LUT carries, as the editor names them: exposure in
 *  stops (−2..2), shadows only darken (−1..0), auto_wb 0..1, the rest −1..1.
 *  A look's recipe holds only those it moves. */
export interface LookSliders {
  exposure?: number;
  contrast?: number;
  temperature?: number;
  tint?: number;
  saturation?: number;
  vibrance?: number;
  shadows?: number;
  auto_wb?: number;
}

/** What made a model's suggestion. `variant` is "" when the model has none. */
export interface ClipLookProvenance {
  model: string | null;
  model_revision: string | null;
  variant: string;
  runtime: string | null;
  runtime_version: string | null;
  prompt_version: string | null;
}

/** A request to the local model (a clip's suggestion, or a listing's
 *  photographs), as the page shows it. A running request whose worker
 *  stopped already reads `failed`. */
export interface ClipLookJob {
  id: string;
  state: LocalModelJobState;
  /** The sentence to show: why it waits and until when, why it failed; a
   *  listing's request names the photograph it is on while it runs. */
  message: string;
  /** What the model wrote, a path on the server. Admins only, else null. */
  detail: string | null;
  /** Only while waiting. */
  not_before: string | null;
  done: number;
  total: number;
  cancelling: boolean;
  can_cancel: boolean;
}

export interface ClipLookStill {
  ts_ms: number;
  /** A shot's middle still, or a sampled frame standing in. */
  from: "shot" | "frame";
}

/** One of the three tiles. Unavailable: `note` says why and `recipe` is {}. */
export interface ClipLookCandidate {
  source: ClipLookSource;
  available: boolean;
  /** Why it is not available; for the learned look, where it comes from. */
  note: string | null;
  recipe: LookSliders;
  /** The look in words, with no numbers. */
  in_words: string;
  /** The model's one line; "" for the others. */
  reason: string;
  /** What the look wanted and a LUT cannot carry, in words ready to print. */
  left_out: string[];
  /** The still its white balance was measured on. */
  measured_at_ms: number | null;
  basis: "clips" | "photos" | null;
  /** How many chosen clips or shipped photographs it was learned from. */
  examples: number;
  provenance: ClipLookProvenance | null;
  made_at: string | null;
  demo: boolean;
  /** Changes whenever the look does: put it in the preview's address. */
  version: string;
}

/** The look a person chose, copied as it was offered then. */
export interface ClipLookChoice {
  source: ClipLookChosenSource;
  recipe: LookSliders;
  in_words: string;
  left_out: string[];
  measured_at_ms: number | null;
  chosen_at: string | null;
  /** An e-mail address. */
  chosen_by: string | null;
  provenance: ClipLookProvenance | null;
  demo: boolean;
  version: string;
}

/** Whether the local model can be asked here, and if not, why. */
export interface LocalModelsAsk {
  mode: LocalModelsMode;
  /** False when the model cannot be asked here; `note` says why. */
  ok: boolean;
  note: string | null;
}

export interface ClipLookFolderClip {
  asset_id: string;
  filename: string;
  state: "chosen" | "suggested" | "none";
}

/** The clips directly in this clip's folder (video, catalogued, online). The
 *  counts include this clip; `others` leaves it out (at most 200). */
export interface ClipLookFolder {
  /** Inside the library; "" at its top. */
  path: string;
  clips: number;
  chosen: number;
  suggested: number;
  others: ClipLookFolderClip[];
}

export interface ClipLooks {
  asset_id: string;
  filename: string;
  /** Set when the clip has no still yet: then `stills` is empty and every
   *  candidate is unavailable with this as its note. */
  problem: string | null;
  /** Each shot's middle still, or sampled frames; up to 12, in time order.
   *  A preview may only be asked for at one of these or the reference. */
  stills: ClipLookStill[];
  /** The still every look is measured on, and previews' default moment. */
  reference_ts_ms: number | null;
  /** Always three, in the order model, learned, none. */
  candidates: ClipLookCandidate[];
  chosen: ClipLookChoice | null;
  /** `job`: the newest request while it is on its way, or one that failed
   *  after the stored suggestion; else null. */
  suggest: LocalModelsAsk & { job: ClipLookJob | null };
  folder: ClipLookFolder;
  lut: { sizes: number[]; default: number; assumes: string };
}

/** What asking for a whole folder did. `already`: a suggestion, a choice or a
 *  request on its way. `no_still`: nothing to show a model yet. */
export interface FolderLooksQueued {
  queued: number;
  already: number;
  no_still: number;
}

export type LocalEditState = "proposed" | "applied" | "dismissed";

/** What the local model proposes for one photograph of a listing. Nothing
 *  about the photograph changes until it is applied. */
export interface LocalEditSuggestion {
  item_id: string;
  asset_id: string;
  filename: string;
  /** Gallery order. */
  position: number;
  /** Degrees; positive turns the picture counterclockwise. */
  rotate: number;
  /** −1..1; positive pulls in verticals that lean together toward the top. */
  keystone: number;
  /** A correction big enough to look at first. */
  large: boolean;
  sky_worth_replacing: boolean;
  caption: string;
  slug: string;
  note: string;
  state: LocalEditState;
  /** The photograph's name was confirmed by a person: Apply keeps it. */
  name_is_yours: boolean;
  provenance: ClipLookProvenance | null;
  demo: boolean;
  made_at: string | null;
}

/** How the newest finished request went (for one failed or cancelled part
 *  way, how far it got). `first_problem`: "IMG_0412.jpg: <sentence>". */
export interface LocalEditLastRun {
  looked: number;
  failed: number;
  unreadable: number;
  first_problem: string | null;
}

export interface LocalEdit extends LocalModelsAsk {
  /** The newest request while on its way, or failed; else null. */
  job: ClipLookJob | null;
  /** Photographs of the listing. */
  photographs: number;
  proposed: number;
  applied: number;
  dismissed: number;
  /** Where Apply's tone comes from: the learned look, or the listing preset. */
  tone: { from: "look" | "preset"; examples: number };
  last_run: LocalEditLastRun | null;
  /** Every row, whatever its state, in gallery order. */
  suggestions: LocalEditSuggestion[];
}

/** What Apply uses of the local model's suggestions (auto-edit mode "local"). */
export interface LocalApplyOptions {
  /** The ticked photographs (`LocalEditSuggestion.item_id`); null = every
   *  suggestion still waiting. */
  item_ids: string[] | null;
  /** Keep the model's straightening; off = tone (and names) only. */
  straighten: boolean;
  /** Use the model's caption and file name (a name a person confirmed is
   *  kept either way). */
  names: boolean;
  /** With a sky picked: only where the model would replace it, or wherever
   *  there is sky. */
  sky: "suggested" | "all";
  /** Each ticked row's `made_at` as the list showed it (`madeAtShown`): a
   *  photograph whose suggestion was made again since is left waiting, not
   *  given the new one nobody looked at. Rows without an entry are applied
   *  unchecked; leaving it out entirely is an older client's request. */
  made_at?: Record<string, string>;
  /** Told the server's answer before the page goes on with the run, so the
   *  list can say what was left (`note`). Never sent. */
  heard?: (answer: AiEditStarted) => void;
}

/** What `POST /listings/{id}/ai-edit` answers. `left` and `note` come only
 *  when a local Apply sent `made_at`: how many ticked photographs were left
 *  because their suggestion was made again after the list was read, and the
 *  server's sentence saying so ("" when none was). */
export interface AiEditStarted {
  queued: number;
  mode: "ai" | "preset" | "describe" | "local";
  look: number;
  left?: number;
  note?: string;
}

// ---- Rough cuts (/cuts): cut history, and drafts by the local Editor ---------------
//
// A cut is one version in a family (`root_id`): a local draft, Claude's
// version of it, an imported timeline. A draft is a request to the Editor (a
// model on the Mac) that becomes a cut when it succeeds, and leaves the
// drafts list then. Everything a draft says is read off the server; reading,
// downloading and the review request need no model and work on the
// container server, where drafting answers 409 with the sentence to show.
// Times are UTC ("…Z") except `CutEditorStatus.starts_at`.

export type CutSource = "local" | "mcp" | "import";
export type CutDraftState = "queued" | "waiting" | "running" | "done" | "failed" | "cancelled";

/** Whether the Editor can draft here, and when it would run. */
export interface CutEditorStatus {
  mode: "ready" | "demo" | "unavailable";
  /** The demonstration's or the container server's sentence. */
  note: string | null;
  model_id: string | null;
  model_name: string | null;
  runs: "now" | "overnight" | "never" | null;
  /** The Mac's own wall clock, no zone ("2026-11-14T21:30:00"): when an
   *  overnight-only Editor starts. Only while it is day. */
  starts_at: string | null;
  paused: boolean;
  /** Why a draft would be refused now, as the sentence to show. */
  why_not: string | null;
}

export interface CutScope {
  kind: "folder" | "selection";
  library_id: string | null;
  /** This folder and everything beneath it, inside the library. */
  folder: string;
  /** The chat's "folders matching": these letters anywhere in the path. */
  contains: string;
  asset_ids: string[];
  /** "GELCO › 2026/Pete Dye", "6 clips ticked in Search". */
  label: string;
}

export interface CutDraft {
  id: string;
  state: CutDraftState;
  /** Cancel was pressed while the Editor was answering. */
  stopping: boolean;
  /** The server's sentence for where it stands or why it ended. */
  message: string;
  title: string;
  brief: string;
  scope: CutScope;
  target_s: number;
  min_shot_s: number;
  max_shot_s: number;
  exclude_used: boolean;
  shots_offered: number;
  shots_in_scope: number;
  clips: number;
  cut_id: string | null;
  revises_cut_id: string | null;
  origin: "web" | "chat";
  /** What the check changed or dropped, one sentence each. */
  problems: string[];
  /** What the Editor wrote, only on a failed draft. A model's words: text only. */
  answer_text: string | null;
  model_name: string | null;
  demo: boolean;
  seconds: number | null;
  /** Email of whoever asked, or null. */
  requested_by: string | null;
  can_cancel: boolean;
  can_clear: boolean;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface CutDraftRequest {
  brief: string;
  title?: string;
  scope: {
    kind: "folder" | "selection";
    library_id?: string;
    folder?: string;
    contains?: string;
    asset_ids?: string[];
  };
  target_s?: number | null;
  min_shot_s?: number;
  max_shot_s?: number;
  exclude_used?: boolean;
  /** "Draft again": the cut this is the next version of. */
  revises?: string;
}

/** A family's newest version, as the list shows it. */
export interface CutFamilyVersion {
  id: string;
  version: number;
  source: CutSource;
  /** "Local draft", "Claude", "Imported timeline". */
  label: string;
  duration_s: number;
  piece_count: number;
  created_at: string | null;
  demo: boolean;
}

/** One line of the list: a cut and its versions. */
export interface CutFamily {
  /** The newest version's id. */
  id: string;
  root_id: string;
  title: string;
  client: string;
  versions: number;
  newest: CutFamilyVersion;
  has_local_draft: boolean;
  /** Any version has been downloaded for Premiere. */
  sent: boolean;
}

export interface CutsOverview {
  editor: CutEditorStatus;
  /** Not read-only, and the Editor could take a draft now (or tonight). */
  can_draft: boolean;
  /** Drafts that are not a cut: under way first, then the newest ended. */
  drafts: CutDraft[];
  cuts: CutFamily[];
  /** Families come after this page (`GET /cuts?offset=`). Absent from a
   *  server from before paging, which listed at most 200 and said nothing. */
  more?: boolean;
}

export interface CutFolder {
  library_id: string;
  library_name: string;
  /** Inside the library, as Browse shows it. */
  path: string;
  clips: number;
  /** Clips that have been analysed into shots. */
  analysed: number;
  newest: string | null;
}

export interface CutShot {
  move: string | null;
  direction: string | null;
  light: string | null;
  sky: string | null;
  subjects: string[];
  smooth: number | null;
  energy: string | null;
}

export interface CutPiece {
  position: number;
  /** Null once the clip has been deleted from the catalogue. */
  asset_id: string | null;
  filename: string;
  clip: string;
  media_type: MediaType;
  /** False when the clip is deleted or not on the share now. */
  available: boolean;
  in_s: number;
  out_s: number;
  length_s: number;
  /** Why it is there. A model's words: shown only as text. */
  note: string;
  shot: CutShot | null;
  still_url: string | null;
}

/** A shot the Editor was offered. */
export interface CutCandidate {
  n: number;
  asset_id: string;
  clip: string;
  start_s: number;
  end_s: number;
  line: string;
  chosen: boolean;
}

/** The draft a version was made from. */
export interface CutMadeFrom {
  id: string;
  brief: string;
  scope: CutScope;
  target_s: number;
  min_shot_s: number;
  max_shot_s: number;
  shots_offered: number;
  shots_in_scope: number;
  clips: number;
  seconds: number | null;
  problems: string[];
  candidates: CutCandidate[];
}

/** The local build that drafted a version. */
export interface CutProvenance {
  model: string;
  model_name: string;
  model_revision: string | null;
  revision_short: string | null;
  variant: string;
  runtime: string | null;
  runtime_version: string | null;
  prompt_version: string | null;
}

export interface CutVersion {
  id: string;
  version: number;
  /** The version it revises; it may have been deleted since. */
  parent_id: string | null;
  source: CutSource;
  label: string;
  demo: boolean;
  title: string;
  duration_s: number;
  piece_count: number;
  created_at: string | null;
  created_by: string | null;
  sent_at: string | null;
  can_delete: boolean;
  provenance: CutProvenance | null;
  draft: CutMadeFrom | null;
  pieces: CutPiece[];
}

export interface CutDetail {
  /** The version asked for. */
  id: string;
  root_id: string;
  /** The newest version's. */
  title: string;
  client: string;
  editor: CutEditorStatus;
  can_draft: boolean;
  /** Drafts revising a version of this cut that are not a version yet. */
  drafts: CutDraft[];
  /** Oldest first. */
  versions: CutVersion[];
}

export interface CutSendResult {
  filename: string;
  xml: string;
  profile: string | null;
  duration_s: number;
  cuts: number;
  /** No path for this computer: they import offline until relinked. */
  unmapped: string[];
  /** Deleted from the catalogue: left out. */
  missing: string[];
  /** Not on the share now: written; relink once it is back. */
  offline: string[];
  note: string;
  sent_at: string | null;
}

export interface CutReviewRequest {
  cut_id: string;
  label: string;
  shots: number;
  candidates: number;
  chars: number;
  text: string;
}

// ---- Folders, and looking (/assets/folders, /assets/{id}/shots, /looks) --
//
// A folder is a library and a path inside it, exactly as the catalogue wrote
// it ("" is the library itself). Browsing folders and reading a clip's shots
// need no model and work on every install.
//
// A look is what a model on the Mac said of a photograph or of one shot: a
// caption, a 0-100 score and a reason. A review is a person asking for a
// whole folder to be looked at. Reads are for anyone signed in; starting,
// cancelling and the settings are an administrator's, and where models cannot
// run every write answers 409 with the sentence to show. Times end in "Z"
// (UTC) except `LookPreview.tonight.starts_at`, and the times on a folder,
// which are the catalogue's own.

export interface FolderCrumb {
  name: string;
  path: string;
}

/** `items` is every catalogued file (audio too); `photos` and `clips` are
 *  the images and the videos among them. */
export interface FolderCounts {
  items: number;
  photos: number;
  clips: number;
}

export interface FolderInside extends FolderCounts {
  /** Photographs and clips with nothing left for a local model to look at. */
  looked: number;
}

/** A folder directly inside the one shown; its counts are for everything
 *  beneath it. */
export interface FolderRow extends FolderInside {
  name: string;
  path: string;
  /** The latest capture time beneath it (else the file's own time). */
  newest: string | null;
}

/** One level of a library (`GET /assets/folders`). A path nothing is
 *  catalogued under is answered too, with empty lists and zeros. */
export interface FolderLevel {
  library_id: string;
  library_name: string;
  /** As the server reads it: slashes at either end dropped. */
  path: string;
  crumbs: FolderCrumb[];
  /** By name; at most 500, and `truncated` says there were more. */
  folders: FolderRow[];
  /** The files directly in this folder. */
  here: FolderCounts;
  /** Everything beneath it, its own files included. */
  inside: FolderInside;
  truncated: boolean;
}

/** What a local model said of a photograph or of one shot, and who said it.
 *  `caption` and `reason` are a model's text: shown as text, nothing more. */
export interface AssetLook {
  score: number | null;
  caption: string;
  reason: string;
  /** True for a photograph, or a clip judged from one picture; false for a
   *  shot, which has its times. */
  whole: boolean;
  start_ms: number | null;
  end_ms: number | null;
  /** On an asset: nothing in it still needs looking at. Always true on a shot. */
  complete: boolean;
  looked_at: string;
  /** The model's repository name, and the build that answered. */
  model: string;
  model_revision: string;
  revision_short: string;
  variant: string;
  runtime: string;
  runtime_version: string;
  prompt_version: string;
  /** Made up by the demonstration; the page says so. */
  demo: boolean;
}

/** One shot of a clip (`GET /assets/{id}/shots`): what was measured, and the
 *  local model's look at it where there is one. */
export interface ShotRow {
  idx: number;
  start_ms: number;
  end_ms: number;
  /** The shot's middle still: `/media/{id}/frame?ts=<still_ms>`. */
  still_ms: number;
  move: string;
  direction: string | null;
  /** 0-1, steadier towards 1. */
  smooth: number;
  energy: string;
  light: string | null;
  sky: string | null;
  /** At most five, the most certain first. */
  subjects: string[];
  look: AssetLook | null;
}

export type LookMode = "ready" | "demo" | "unavailable";

/** A model that could look, for the review dialog's choice. */
export interface LookCandidate {
  id: string;
  name: string;
  repo_id: string;
  memory_gb: number;
  /** "day": any time; "night": only in the overnight window. */
  fits: "day" | "night" | string;
  is_looker: boolean;
  /** Why it cannot start at this hour; null when it can. */
  why_not_now: string | null;
  /** Why it cannot be used at all as things stand (its program is not
   *  installed here); null or absent when it can. Older servers omit both. */
  problem?: string | null;
  /** `problem` is its program: Software that runs models puts it right. */
  program_missing?: boolean;
}

export interface LookSettings {
  /** The model may propose tags that are not in use yet. */
  new_tags: boolean;
  /** Pictures shown for each shot: 3 (start, middle, end) or 1 (the middle). */
  stills_per_shot: number;
}

/** What a page needs to know before offering anything (`GET /looks/status`);
 *  never an error. */
export interface LookStatus {
  available: boolean;
  mode: LookMode;
  /** Why models cannot run here, or the demonstration's note; null when ready. */
  reason: string | null;
  /** An administrator, where models can run. */
  can_manage: boolean;
  /** Null where models cannot run. With no Looker, every field but `problem` is null. */
  looker: { model_id: string | null; name: string | null; problem: string | null } | null;
  /** The Looker first; empty where models cannot run. */
  models: LookCandidate[];
  now: { overnight: boolean; night_start: string; night_end: string; paused: boolean };
  settings: LookSettings;
  /** Tag suggestions from a local model that nobody has answered yet. */
  pending_suggestions: number;
}

export type LookReviewState =
  | "tonight"
  | "waiting"
  | "paused"
  | "looking"
  | "done"
  | "failed"
  | "cancelled";

/** A folder review somebody asked for. Its state and its sentence are read
 *  off the server every time; nothing about a run is kept in the tab. */
export interface LookReview {
  id: string;
  library_id: string;
  library_name: string;
  /** "" is the whole library. */
  folder: string;
  when: "now" | "tonight" | string;
  redo: boolean;
  prepare: boolean;
  model_name: string;
  state: LookReviewState;
  /** The sentence to show; while it is looking, the file it is on. */
  message: string;
  not_before: string | null;
  done: number;
  total: number;
  /** Items that could not be looked at. */
  failed: number;
  tags_suggested: number;
  new_tags: number;
  /** Suggestions held back because a tag already had enough waiting. */
  held: number;
  /** Clips judged from one still. */
  stills_only: number;
  /** Clips being made ready: "" when there is nothing to make ready. */
  preparing: { state: "" | "running" | "done" | string; total: number; ready: number; gave_up: number };
  /** At the measured speed; null without one. */
  seconds_left: number | null;
  requested_by: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** Still active, and the reader is an administrator. */
  can_cancel: boolean;
  demo: boolean;
}

/** What a review of a folder would take (`GET /looks/preview`). Where models
 *  cannot run: `available` false, the reason, every count 0. */
export interface LookPreview {
  available: boolean;
  reason: string | null;
  library_id: string;
  library_name: string;
  folder: string;
  folders_inside: number;
  /** Not looked at yet and ready to be: `clips` have `shots`; `stills_only`
   *  are clips judged from one still. */
  todo: { photos: number; clips: number; shots: number; stills_only: number };
  /** `by_another_model` is what ticking "redo" adds. */
  already: { items: number; by_another_model: number };
  /** `already.items` and `already.by_another_model` by kind: photographs,
   *  clips (each once) and the shots in those clips. */
  already_kinds?: { photos: number; clips: number; shots: number } | null;
  by_another_kinds?: { photos: number; clips: number; shots: number } | null;
  /** Clips that are not ready, and why. `problem` is the drive's sentence
   *  when the drive for playable copies is away. */
  first: {
    need_copy: number;
    need_copy_footage_s: number;
    copy_away: number;
    need_shots: number;
    still_processing: number;
    problem: string | null;
  };
  skipped: { missing: number; audio: number };
  failed_before: number;
  /** The sentence when no model has the Looker role. */
  no_looker: string | null;
  /** The model named, else the Looker; null with no Looker. */
  model: {
    id: string;
    name: string;
    fits: "day" | "night" | string;
    is_looker: boolean;
    problem: string | null;
    /** `problem` is that its program is not installed here (absent from
     *  older servers). */
    program_missing?: boolean;
    why_not_now: string | null;
    tonight_problem: string | null;
  } | null;
  /** `words` is the sentence to show; `seconds` is null when nothing was measured. */
  estimate: {
    seconds: number | null;
    basis: "measured" | "test" | "none" | string;
    per_photo_s: number | null;
    per_shot_s: number | null;
    sample: number;
    words: string;
  };
  /** `starts_at` is the Mac's own wall clock, no zone. */
  tonight: { starts_at: string | null; open_now: boolean; problem: string | null };
  paused: boolean;
  /** The review already running for exactly this library and folder. */
  active: LookReview | null;
}

/** A library that has its new footage looked at as it arrives. */
export interface LookIngestLine {
  library_id: string;
  library_name: string;
  /** Files that arrived since the switch went on and still wait. */
  waiting: number;
  state: "idle" | "waiting" | "paused" | "looking" | "failed" | string;
  message: string;
  done: number;
  total: number;
  /** While the library is being left alone after a failure: when it is
   *  tried again (the message says the same in words). Absent on a server
   *  older than the field. */
  retry_at?: string | null;
}

/** Reviews and new-footage looking, for Processing and the dialog
 *  (`GET /looks/reviews`): the active reviews first, then the newest 20 that
 *  ended. Where models cannot run: `available` false and both lists empty. */
export interface LookActivity {
  available: boolean;
  reviews: LookReview[];
  ingest: LookIngestLine[];
}

export interface StartReviewRequest {
  library_id: string;
  /** "" for the whole library. */
  folder?: string;
  when?: "now" | "tonight";
  /** A model other than the Looker; null uses the Looker. */
  model_id?: string | null;
  /** Also look again at what another model, or an older question, looked at. */
  redo?: boolean;
  /** Make clips ready (a playable copy, their shots) when they are not. */
  prepare?: boolean;
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
  if (!resp.ok) throw await refusal(resp, path);
  return resp.status === 204 ? (undefined as T) : ((await resp.json()) as T);
}

/** A request answered with a picture or a file rather than JSON. A refusal
 *  is read exactly as `request` reads it. */
async function requestBlob(path: string, init?: RequestInit): Promise<Blob> {
  const resp = await fetch(`/api/v1${path}`, { ...init, credentials: "same-origin" });
  if (!resp.ok) throw await refusal(resp, path);
  return resp.blob();
}

/** A file to save, with the answer's Content-Disposition for its name. Used
 *  where the server may refuse a download a plain link would save the
 *  refusal as; a refusal is read exactly as `request` reads it. */
async function requestFile(path: string): Promise<{ blob: Blob; disposition: string | null }> {
  const resp = await fetch(`/api/v1${path}`, { credentials: "same-origin" });
  if (!resp.ok) throw await refusal(resp, path);
  return { blob: await resp.blob(), disposition: resp.headers.get("Content-Disposition") };
}

/** The `ApiError` for an answer that was not ok, carrying the server's
 *  sentence; a 401 outside /auth/ goes to the sign-in page as well. */
async function refusal(resp: Response, path: string): Promise<ApiError> {
  const body = await resp.json().catch(() => null);
  // A session that ends while a page is open — expiry, or "sign out
  // everywhere" from another device — used to surface as every button
  // failing with a generic error. Outside /auth/ a 401 only ever means
  // "no valid session"; inside it, a wrong password is not an expiry.
  if (resp.status === 401 && !path.startsWith("/auth/") && typeof window !== "undefined") {
    // Back to the same page after signing in: a session that expired on a
    // listing used to land on Search.
    const here = window.location.pathname + window.location.search;
    const next = here.startsWith("/login") ? "" : `?next=${encodeURIComponent(here)}`;
    window.location.assign(`/login${next}`);
  }
  return new ApiError(resp.status, body?.error?.message ?? `Request failed (${resp.status})`);
}

/**
 * A chat request that is answered with a stream of events (a message, a yes
 * or no, or picking up a turn already running). Each event is handed to
 * `onEvent` as it arrives.
 *
 * Resolves with 200 when the stream is over — whether it ended or the
 * connection dropped; the caller knows a turn finished by having seen
 * `done` — and with 204 when the server had no turn to follow. Rejects with
 * an `ApiError` carrying the server's sentence when it said no before
 * streaming, exactly as `request` does (a 401 goes to the sign-in page the
 * same way), and with what `fetch` threw when the server was not reached or
 * `init.signal` was aborted.
 *
 * `request` cannot do this: it waits for the whole body.
 */
async function eventStream(
  path: string,
  init: RequestInit,
  onEvent: (event: ChatEvent) => void,
): Promise<number> {
  try {
    return await readEventStream(
      `/api/v1${path}`,
      { ...init, credentials: "same-origin" },
      (raw) => {
        const event = chatEvent(raw);
        if (event) onEvent(event);
      },
    );
  } catch (err) {
    if (!(err instanceof StreamRefused)) throw err;
    if (err.status === 401 && typeof window !== "undefined") {
      const here = window.location.pathname + window.location.search;
      const next = here.startsWith("/login") ? "" : `?next=${encodeURIComponent(here)}`;
      window.location.assign(`/login${next}`);
    }
    throw new ApiError(err.status, err.message);
  }
}

function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") usp.set(key, String(value));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

/**
 * `search` with a folder on the end. `query` leaves out an empty value, and
 * for a folder empty is a value: "" is the top of the library, where no
 * folder at all means the whole library. Left out, the top of a library
 * listed every file beneath it.
 */
function withFolder(search: string, name: string, folder: string | undefined): string {
  if (folder === undefined) return search;
  return `${search}${search ? "&" : "?"}${name}=${encodeURIComponent(folder)}`;
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

  panelTokens: () => request<PanelToken[]>("/auth/panel-tokens"),
  createPanelToken: (body: {
    name: string;
    host: string;
    scopes: string[];
    expires_in_days?: number | null;
  }) =>
    request<PanelTokenCreated>("/auth/panel-tokens", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  revokePanelToken: (id: string) =>
    request<void>(`/auth/panel-tokens/${id}`, { method: "DELETE" }),

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
  tailscale: () => request<TailscaleState>("/remote-access/tailscale"),
  tailscaleLogin: () =>
    request<TailscaleState>("/remote-access/tailscale/login", { method: "POST" }),
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
  pathMappings: (libraryId: string) =>
    request<PathMapping[]>(`/libraries/${libraryId}/path-mappings`),
  replacePathMappings: (libraryId: string, mappings: PathMappingInput[]) =>
    request<PathMapping[]>(`/libraries/${libraryId}/path-mappings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(mappings),
    }),
  scanLibrary: (id: string) => request<unknown>(`/libraries/${id}/scan`, { method: "POST" }),
  /** Switching `look_at_ingest` on where models cannot run is refused (409)
   *  with the sentence to show, and nothing in the patch is applied. */
  updateLibrary: (
    id: string,
    patch: { scan_interval_minutes?: number | null; look_at_ingest?: boolean },
  ) =>
    request<Library>(`/libraries/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),

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
    /** A folder of `library_id`; "" is the library's own top. Left out,
     *  the list is not about a folder at all. */
    folder?: string;
    /** With `folder`: also everything in the folders inside it. */
    deep?: boolean;
  }) => {
    const { folder, ...rest } = params;
    return request<Page<AssetSummary>>(`/assets${withFolder(query(rest), "folder", folder)}`);
  },
  asset: (id: string) => request<AssetDetail>(`/assets/${id}`),
  transcript: (id: string) => request<Transcript>(`/assets/${id}/transcript`),
  reprocess: (id: string) => request<unknown>(`/assets/${id}/reprocess`, { method: "POST" }),

  search: (q: string, libraryId?: string, mediaType?: MediaType) =>
    request<SearchResponse>(
      `/search${query({ q, library_id: libraryId, media_type: mediaType, limit: 40 })}`,
    ),
  similar: (assetId: string) => request<VisualHit[]>(`/search/similar/${assetId}`),
  pathProfiles: () => request<PathProfile[]>("/panel/profiles"),
  selects: (body: {
    title: string;
    profile: string;
    query: string;
    moments: SelectMoment[];
    order?: "rank" | "captured";
  }) =>
    request<SelectsResult>("/selects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  scenes: (assetId: string) => request<SceneFrame[]>(`/assets/${assetId}/scenes`),

  places: (radiusKm: number, includeInferred: boolean) =>
    request<Place[]>(
      `/places${query({ radius_km: radiusKm, include_inferred: includeInferred })}`,
    ),
  assetsNear: (lat: number, lon: number, radiusKm: number) =>
    request<NearbyAsset[]>(
      // The server's maximum. A place view is silently capped at this many;
      // the card's count says how many there really are.
      `/assets/near${query({ lat, lon, radius_km: radiusKm, limit: 200 })}`,
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
  /** The whole queue: a tag holds at most a couple of hundred waiting. */
  pendingForTag: (tagId: string, limit = 200) =>
    request<PendingSuggestion[]>(`/tags/${tagId}/pending?limit=${limit}`),
  /** Many answers about one tag in one request; the tag re-learns once. */
  decideTags: (tagId: string, body: { accept: string[]; reject: string[] }) =>
    request<{ accepted: number; rejected: number; skipped: number }>(`/tags/${tagId}/decide`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  relearnTag: (tagId: string) =>
    request<{ status: string }>(`/tags/${tagId}/relearn`, { method: "POST" }),
  claudeReviewStatus: () => request<ClaudeReviewStatus>("/tags/review/claude"),
  startClaudeReview: (body: { asset_ids?: string[]; limit?: number; library_id?: string }) =>
    request<{ queued: number; message: string }>("/tags/review/claude", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  people: () => request<PersonSummary[]>("/people"),
  person: (id: string, limit = 600) =>
    request<PersonDetail>(`/people/${id}?limit=${limit}`),
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
  // Either an explicit list, or everything at or above a similarity. The
  // second form is what lets one gesture settle more faces than the page has
  // actually loaded.
  confirmBulk: (id: string, pick: BulkPick) =>
    request<{ confirmed: number }>(`/people/${id}/confirm-above`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(bulkBody(pick)),
    }),
  discoverMore: (id: string) =>
    request<DiscoverResult>(`/people/${id}/discover`, { method: "POST" }),
  suggestions: (id: string) => request<FaceRef[]>(`/people/${id}/suggestions`),
  acceptSuggestions: (id: string, pick: BulkPick) =>
    request<{ accepted: number }>(`/people/${id}/suggestions/accept`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(bulkBody(pick)),
    }),
  rejectSuggestions: (id: string, faceIds: string[]) =>
    request<{ rejected: number }>(`/people/${id}/suggestions/reject`, {
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

  // Where the bulky files live. A 428 from the two that name a folder is a
  // question (its message), answered by sending the same request again with
  // `confirm`: nothing is made in the folder until then.
  storageKinds: () => request<StorageKinds>("/storage/kinds"),
  setBulkFolder: (path: string, confirm = false) =>
    request<BulkFolderResult>("/storage/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, confirm_same_disk: confirm }),
    }),
  moveKind: (kind: string, target: string, mode: "copy" | "empty", confirm = false) =>
    request<MoveStatus>(`/storage/kinds/${encodeURIComponent(kind)}/move`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, mode, confirm_same_disk: confirm }),
    }),
  storageMove: () => request<MoveStatus | null>("/storage/move"),
  cancelStorageMove: () => request<MoveStatus | null>("/storage/move/cancel", { method: "POST" }),
  // `expectedFolder` is the old folder the page showed when the yes was given.
  // If the server's record names another by then, nothing is removed (409).
  removeOldCopies: (kind: string, everything = false, expectedFolder?: string) =>
    request<OldCopiesResult>(`/storage/kinds/${encodeURIComponent(kind)}/cleanup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ everything, expected_folder: expectedFolder ?? null }),
    }),
  // Queue every proxy that should exist and does not. Asking twice is safe:
  // a proxy that exists is never made again.
  rebuildProxies: () => request<RebuildProxiesResult>("/storage/proxies/rebuild", { method: "POST" }),

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

  facesInAsset: (assetId: string) => request<FacesInPhoto>(`/people/faces/in-asset/${assetId}`),
  assignFace: (faceId: string, body: { person_id?: string; name?: string }) =>
    request<{ face_id: string; person_id: string; name: string; created: boolean; confirmed_count: number }>(
      `/people/faces/${faceId}/assign`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
    ),

  suggestTags: (q: string) => request<TagSuggestion[]>(`/tags/suggest${query({ q })}`),
  suggestNames: (q: string, exclude?: string) =>
    request<NameSuggestion[]>(`/people/suggest/names${query({ q, exclude })}`),
  mergePeople: (into: string, from: string) =>
    request<{ merged: number }>(`/people/${into}/merge/${from}`, { method: "POST" }),

  basemaps: () => request<BasemapList>("/basemaps"),
  downloadBasemap: (name: string, bbox = "") =>
    request<{ status: string; name: string }>("/basemaps/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, bbox }),
    }),
  deleteBasemap: (name: string) => request<void>(`/basemaps/${name}`, { method: "DELETE" }),
  useBasemap: (name: string) =>
    request<{ provider: string; style_url: string; note: string }>(
      `/basemaps/${name}/use`,
      { method: "POST" },
    ),

  slideshowThemes: () => request<SlideshowTheme[]>("/slideshows/themes"),
  proposeSlideshow: (body: Record<string, unknown>) =>
    request<SlideshowProposal>("/slideshows/propose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  createSlideshow: (body: Record<string, unknown>) =>
    request<Slideshow>("/slideshows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  slideshows: () => request<Slideshow[]>("/slideshows"),
  slideshow: (id: string) => request<Slideshow>(`/slideshows/${id}`),
  rerenderSlideshow: (id: string) =>
    request<Slideshow>(`/slideshows/${id}/render`, { method: "POST" }),
  deleteSlideshow: (id: string) => request<void>(`/slideshows/${id}`, { method: "DELETE" }),

  rooms: () => request<RoomOption[]>("/listings/rooms"),
  listings: () => request<ListingSummary[]>("/listings"),
  listing: (id: string) => request<ListingDetail>(`/listings/${id}`),
  showcase: (body: ShowcaseRequest) =>
    request<ShowcaseResponse>("/showcase", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  showcasePlaces: (libraryIds: string[], from?: string | null, to?: string | null) =>
    request<ShowcasePlaceCount[]>(
      `/showcase/places?${[
        ...libraryIds.map((id) => `library_ids=${encodeURIComponent(id)}`),
        ...(from ? [`captured_from=${from}`] : []),
        ...(to ? [`captured_to=${to}`] : []),
      ].join("&")}`,
    ),
  /** A new listing by `target.name`, or add to `target.listing_id`; file
   *  numbers carry on from `continueFrom` (earlier batches). */
  showcaseListing: (
    target: { name: string } | { listing_id: string },
    picks: ShowcaseListingPick[],
    continueFrom: string[] = [],
  ) =>
    request<{ listing_id: string; added: number }>("/showcase/listing", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...target, picks, continue_numbering_from: continueFrom }),
    }),
  createListing: (name: string, assetIds: string[]) =>
    request<ListingDetail>("/listings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, asset_ids: assetIds }),
    }),
  addListingItems: (id: string, assetIds: string[]) =>
    request<ListingDetail>(`/listings/${id}/items`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset_ids: assetIds }),
    }),
  removeListingItem: (id: string, assetId: string) =>
    request<ListingDetail>(`/listings/${id}/items/${assetId}`, { method: "DELETE" }),
  setListingRoom: (id: string, assetId: string, room: string) =>
    request<ListingDetail>(`/listings/${id}/items/${assetId}/room`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ room }),
    }),
  arrangeListing: (id: string) =>
    request<ListingDetail>(`/listings/${id}/arrange`, { method: "POST" }),
  reorderListing: (id: string, assetIds: string[]) =>
    request<ListingDetail>(`/listings/${id}/reorder`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset_ids: assetIds }),
    }),
  exportListing: (
    id: string,
    maxEdge = 3840,
    quality = 85,
    naming: ListingNaming = "seo",
    includeIndex = true,
  ) =>
    request<ListingDetail>(`/listings/${id}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        max_edge: maxEdge,
        quality,
        naming,
        include_index: includeIndex,
      }),
    }),
  updateListing: (id: string, patch: { name?: string; file_suffix?: string; notes?: string }) =>
    request<ListingDetail>(`/listings/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  setListingNaming: (id: string, assetId: string, caption: string, slug: string) =>
    request<ListingDetail>(`/listings/${id}/items/${assetId}/naming`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ caption, slug }),
    }),
  deleteListing: (id: string) => request<void>(`/listings/${id}`, { method: "DELETE" }),
  searchFolders: (q: string) =>
    request<ListingFolder[]>(`/listings/folders?q=${encodeURIComponent(q)}`),
  folderAssets: (libraryId: string, path: string) =>
    request<FolderAsset[]>(
      `/listings/folders/assets?library_id=${libraryId}&path=${encodeURIComponent(path)}`,
    ),
  reclassifyListing: (id: string) =>
    request<ListingDetail>(`/listings/${id}/classify`, { method: "POST" }),
  curateListing: (id: string) =>
    request<RemovalSuggestion[]>(`/listings/${id}/curate`, { method: "POST" }),
  /** Mode "local" applies what the local model suggested, to the ticked
   *  photographs only (`local`); the others ignore it. `queued` is how many
   *  will be edited; 400 when none of those ticked has a suggestion waiting,
   *  409 (with the sentence) when every one ticked was suggested again after
   *  the list was read (`made_at`). */
  aiEditListing: async (
    id: string,
    skyName: string | null,
    mode: "edit" | "describe" | "local" = "edit",
    local?: LocalApplyOptions,
  ): Promise<AiEditStarted> => {
    const { heard, ...sent } = local ?? { heard: undefined };
    const answer = await request<AiEditStarted>(`/listings/${id}/ai-edit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        mode === "local" && local ? { sky_name: skyName, mode, ...sent } : { sky_name: skyName, mode },
      ),
    });
    if (mode === "local") heard?.(answer);
    return answer;
  },
  aiEditSettings: () => request<AiEditSettings>("/develop/settings/ai"),
  updateAiEditSettings: (patch: {
    api_key?: string;
    enabled?: boolean;
    model?: string;
    monthly_budget_usd?: number;
    review_model?: string;
    review_auto?: boolean;
    review_batch?: number;
    chat_model?: string;
  }) =>
    request<AiEditSettings>("/develop/settings/ai", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),

  gdriveSettings: () => request<GdriveSettings>("/gdrive/settings"),
  updateGdriveSettings: (patch: {
    service_account_json?: string;
    enabled?: boolean;
    training_folders?: string[];
  }) =>
    request<GdriveSettings>("/gdrive/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  gdrivePreview: (folder: string) =>
    request<GdrivePreview>("/gdrive/organize/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder }),
    }),
  gdriveApply: (folder: string, renames: GdriveRename[]) =>
    request<GdriveApplyResult>("/gdrive/organize/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder, renames }),
    }),
  gdriveUndo: (folder: string) =>
    request<GdriveUndoResult>("/gdrive/organize/undo", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder }),
    }),

  skies: () => request<SkyAsset[]>("/develop/skies"),
  uploadSky: (name: string, file: File) =>
    fetch(`/api/v1/develop/skies/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: file,
    }).then((resp) => {
      if (!resp.ok) throw new Error("Could not add that sky");
      return resp.json() as Promise<SkyAsset>;
    }),
  deleteSky: (name: string) =>
    request<void>(`/develop/skies/${encodeURIComponent(name)}`, { method: "DELETE" }),
  skyInfo: (assetId: string) => request<SkyInfo>(`/develop/${assetId}/sky-info`),
  inpaintState: (assetId: string) => request<InpaintState>(`/develop/${assetId}/inpaint`),
  requestInpaint: (assetId: string, maskPngBase64: string) =>
    request<InpaintState>(`/develop/${assetId}/inpaint`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mask_png: maskPngBase64 }),
    }),
  undoInpaint: (assetId: string, version: number) =>
    request<InpaintState>(`/develop/${assetId}/inpaint/${version}`, { method: "DELETE" }),
  developState: (assetId: string) => request<EditState>(`/develop/${assetId}`),
  saveDevelop: (assetId: string, recipe: DevelopRecipe) =>
    request<EditState>(`/develop/${assetId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(recipe),
    }),
  clearDevelop: (assetId: string) =>
    request<EditState>(`/develop/${assetId}`, { method: "DELETE" }),
  applyDevelopToListing: (listingId: string, recipe: DevelopRecipe) =>
    request<{ applied: number }>(`/develop/listing/${listingId}/apply`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(recipe),
    }),

  processing: () => request<ProcessingReport>("/system/processing"),
  health: () => request<HealthReport>("/system/health"),
  alerts: () => request<SystemAlert[]>("/system/alerts"),

  // The chat panel. Conversations are private to their owner: somebody
  // else's answers 404 "No such conversation" on every route, admins too.
  chatStatus: () => request<ChatStatus>("/chat/status"),
  /** Newest first, at most 50. */
  chatConversations: () => request<ChatSummary[]>("/chat/conversations"),
  createChatConversation: (pictures: boolean) =>
    request<ChatConversation>("/chat/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pictures }),
    }),
  chatConversation: (id: string) => request<ChatConversation>(`/chat/conversations/${id}`),
  /** The pictures switch. It decides what tool answers carry from now on
   *  (a turn that is running included), never what was already sent. */
  setChatPictures: (id: string, pictures: boolean) =>
    request<ChatSummary>(`/chat/conversations/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pictures }),
    }),
  /** 409 with a sentence while Claude is still working in it. */
  deleteChatConversation: (id: string) =>
    request<void>(`/chat/conversations/${id}`, { method: "DELETE" }),
  /**
   * Stop the turn that is running. The request to Claude is abandoned at
   * once; a tool already running finishes. The stream being read then ends
   * with `done` (reason `stopped`), so keep reading it: closing a stream
   * does not stop a turn or its cost. With nothing running this changes
   * nothing. A server from before the route existed answers 404 or 405.
   */
  stopChat: (id: string) =>
    request<ChatSummary>(`/chat/conversations/${id}/stop`, { method: "POST" }),
  /** Send a message and read the turn it starts. Calls still waiting for a
   *  yes are answered "no" by this. See `eventStream` for how it ends. */
  sendChatMessage: (
    id: string,
    body: { text: string; context?: ChatContext | null; pictures?: boolean },
    onEvent: (event: ChatEvent) => void,
    signal?: AbortSignal,
  ) =>
    eventStream(
      `/chat/conversations/${id}/messages`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      },
      onEvent,
    ),
  /** Answer calls that are waiting for a yes or no, and read the same turn
   *  carrying on. Answering only some leaves the rest waiting. */
  answerChat: (
    id: string,
    decisions: ChatDecision[],
    onEvent: (event: ChatEvent) => void,
    signal?: AbortSignal,
  ) =>
    eventStream(
      `/chat/conversations/${id}/confirm`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decisions }),
        signal,
      },
      onEvent,
    ),
  /** The events of the turn the server has in memory, from number `after`
   *  (how many the caller already has), then live. 204 when it has none. */
  followChat: (
    id: string,
    after: number,
    onEvent: (event: ChatEvent) => void,
    signal?: AbortSignal,
  ) =>
    eventStream(
      `/chat/conversations/${id}/events${query({ after })}`,
      { cache: "no-store", signal },
      onEvent,
    ),

  // AI models. Reads are anyone's; every write is an administrator's and
  // answers 409 with a sentence where models cannot run. A 428 is a question
  // (its message), answered by sending the same request again with the yes.
  localModels: () =>
    request<LocalModelsOverview>("/local-models", { cache: "no-store" }),
  /** 400 with a sentence for a value it will not take. */
  updateLocalModelSettings: (patch: LocalModelsSettingsPatch) =>
    request<LocalModelsSettingsSaved>("/local-models/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  setModelPublishers: (patch: LocalModelsPublishersPatch) =>
    request<LocalModelsPublishersSaved>("/local-models/publishers", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  /** "" removes the stored token. The token itself is never sent back. */
  setModelToken: (token: string) =>
    request<{ hf_token_configured: boolean }>("/local-models/token", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    }),
  /** Check: asks Hugging Face, changes nothing. `signal` drops a check the
   *  page no longer wants (another build chosen since). */
  preflightModel: (name: string, variant?: string | null, signal?: AbortSignal) =>
    request<LocalModelPreflight>("/local-models/preflight", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, variant: variant ?? null }),
      signal,
    }),
  /** Download the exact build Check showed. 428 lists the warnings as a
   *  question; `acceptWarnings` is the yes. Answers 202 with the model. */
  addModel: (
    body: { name: string; revision: string; variant?: string | null },
    acceptWarnings = false,
  ) =>
    request<LocalModel>("/local-models/models", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: body.name,
        revision: body.revision,
        variant: body.variant ?? null,
        accept_warnings: acceptWarnings,
      }),
    }),
  /** Resume a download that did not finish, or download it again. */
  resumeModel: (id: string) =>
    request<LocalModel>(`/local-models/models/${id}/download`, { method: "POST" }),
  removeModel: (id: string) =>
    request<{ freed_bytes: number }>(`/local-models/models/${id}`, { method: "DELETE" }),
  /** null takes the role away. 202 (rather than 200) when a copy to this Mac
   *  was queued first: the old model keeps the role until it is whole. 428
   *  asks about a llama.cpp model not yet compared for the role (its message
   *  is the question); `skipCompare` is the yes. */
  setModelRole: (role: LocalAiRoleName, modelId: string | null, skipCompare = false) =>
    request<LocalModelRole>(`/local-models/roles/${role}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_id: modelId, skip_compare: skipCompare }),
    }),
  /** One question about a picture: the one given, else the newest photograph,
   *  else one FrameFound draws. Nothing it answers is saved. */
  testModel: (id: string, assetId?: string | null) =>
    request<LocalModelTest>(`/local-models/models/${id}/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset_id: assetId ?? null }),
    }),
  cancelModelJob: (id: string) =>
    request<LocalModelJob>(`/local-models/jobs/${id}/cancel`, { method: "POST" }),
  /** Ask the package index for the newest version now. */
  checkRuntime: (name: string) =>
    request<LocalModelRuntime>(`/local-models/runtimes/${encodeURIComponent(name)}/check`, {
      method: "POST",
    }),
  /** null (or blank) installs the newest. */
  installRuntime: (name: string, version?: string | null) =>
    request<LocalModelRuntime>(`/local-models/runtimes/${encodeURIComponent(name)}/install`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version: version ?? null }),
    }),
  rollbackRuntime: (name: string) =>
    request<LocalModelRuntime>(`/local-models/runtimes/${encodeURIComponent(name)}/rollback`, {
      method: "POST",
    }),
  /** Free the memory now. `unloaded` is false when nothing was loaded. */
  unloadModel: () =>
    request<{ unloaded: boolean }>("/local-models/server/unload", { method: "POST" }),

  // Compare. Reads (the preview and the source searches included) are
  // anyone's; writes are an administrator's. A refusal's message is the
  // sentence to show, and a 503 means nothing was started.
  compareOverview: () =>
    request<CompareOverview>("/local-models/compare", { cache: "no-store" }),
  /** Folders whose path holds `q` (any case), most shots first, at most 30. */
  compareFolders: (q: string, libraryId?: string | null) =>
    request<CompareFolder[]>(
      `/local-models/compare/sources/folders${query({ q, library_id: libraryId })}`,
    ),
  /** Finished cuts, newest first. */
  compareCuts: () => request<CompareCut[]>("/local-models/compare/sources/cuts"),
  /** Counts what a sample would hold. Writes nothing. */
  previewComparison: (body: SampleRequest) =>
    request<SamplePreview>("/local-models/compare/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  /** Fixes the sample and queues one turn per model (201). */
  startComparison: (body: StartComparisonRequest) =>
    request<Comparison>("/local-models/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  comparison: (id: string) =>
    request<Comparison>(`/local-models/compare/${encodeURIComponent(id)}`, { cache: "no-store" }),
  comparisonItems: (id: string) =>
    request<ComparisonItems>(`/local-models/compare/${encodeURIComponent(id)}/items`, {
      cache: "no-store",
    }),
  /** More models on the same sample (202). */
  addComparisonModels: (id: string, modelIds: string[]) =>
    request<Comparison>(`/local-models/compare/${encodeURIComponent(id)}/models`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_ids: modelIds }),
    }),
  /** Carry on, or with `retryFailed` try the failed items again (202). */
  runComparisonModel: (id: string, entryId: string, retryFailed = false) =>
    request<Comparison>(
      `/local-models/compare/${encodeURIComponent(id)}/models/${encodeURIComponent(entryId)}/run`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ retry_failed: retryFailed }),
      },
    ),
  cancelComparisonModel: (id: string, entryId: string) =>
    request<Comparison>(
      `/local-models/compare/${encodeURIComponent(id)}/models/${encodeURIComponent(entryId)}/cancel`,
      { method: "POST" },
    ),
  /** Takes a model out, with its answers. */
  removeComparisonModel: (id: string, entryId: string) =>
    request<Comparison>(
      `/local-models/compare/${encodeURIComponent(id)}/models/${encodeURIComponent(entryId)}`,
      { method: "DELETE" },
    ),
  updateComparison: (id: string, change: ComparisonChange) =>
    request<Comparison>(`/local-models/compare/${encodeURIComponent(id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(change),
    }),
  /** Removes it and its copies of the pictures; the catalogue is untouched. */
  deleteComparison: (id: string) =>
    request<void>(`/local-models/compare/${encodeURIComponent(id)}`, { method: "DELETE" }),

  // Colour looks for a clip. A refusal's message is the sentence to show; a
  // 503 means nothing was started.
  clipLooks: (assetId: string) =>
    request<ClipLooks>(`/clip-looks/${encodeURIComponent(assetId)}`, { cache: "no-store" }),
  /** The still at `ts` (one of the clip's listed moments; the reference when
   *  left out) through sliders set by hand, as a JPEG. White balance is
   *  measured on the reference still. */
  clipLookPreview: (assetId: string, sliders: LookSliders, ts?: number | null, size?: ClipLookSize) =>
    requestBlob(`/clip-looks/${encodeURIComponent(assetId)}/preview${query({ ts, size })}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sliders),
    }),
  /** Ask the local model for a look (202). It may wait for its window. */
  suggestClipLook: (assetId: string) =>
    request<{ job: ClipLookJob }>(`/clip-looks/${encodeURIComponent(assetId)}/suggest`, {
      method: "POST",
    }),
  cancelClipLook: (assetId: string) =>
    request<{ job: ClipLookJob }>(`/clip-looks/${encodeURIComponent(assetId)}/suggest/cancel`, {
      method: "POST",
    }),
  /** Ask for a look for every clip of this clip's folder that has none (202). */
  suggestFolderLooks: (assetId: string) =>
    request<FolderLooksQueued>("/clip-looks/folder/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset_id: assetId }),
    }),
  /** Use a look as offered now; `custom` sends the sliders of one's own. */
  chooseClipLook: (assetId: string, source: ClipLookChosenSource, recipe?: LookSliders | null) =>
    request<ClipLooks>(`/clip-looks/${encodeURIComponent(assetId)}/choice`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source, recipe: source === "custom" ? (recipe ?? {}) : null }),
    }),
  clearClipLook: (assetId: string) =>
    request<ClipLooks>(`/clip-looks/${encodeURIComponent(assetId)}/choice`, { method: "DELETE" }),
  /** The folder's chosen looks as one zip, fetched rather than linked: the
   *  page cannot tell from its counts whether the server will make it (a
   *  folder of "No change" choices has nothing to put in it), and a refusal
   *  must be said, not saved as the zip. */
  folderLooksFile: (assetId: string, size: ClipLookSize) =>
    requestFile(`/clip-looks/folder.zip${query({ asset_id: assetId, size })}`),

  // The local model on a listing: asking, cancelling, dismissing. Nothing
  // about a photograph changes until its suggestion is applied.
  localEdit: (listingId: string) =>
    request<LocalEdit>(`/listings/${encodeURIComponent(listingId)}/local-edit`, { cache: "no-store" }),
  /** Ask the local model about the listing's photographs (202). `redo` asks
   *  again about those that already have a suggestion. */
  askLocalEdit: (listingId: string, redo = false) =>
    request<{ job: ClipLookJob; to_look_at: number }>(
      `/listings/${encodeURIComponent(listingId)}/local-edit`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ redo }),
      },
    ),
  cancelLocalEdit: (listingId: string) =>
    request<{ job: ClipLookJob }>(`/listings/${encodeURIComponent(listingId)}/local-edit/cancel`, {
      method: "POST",
    }),
  /** null dismisses every waiting suggestion. */
  dismissLocalEdit: (listingId: string, itemIds: string[] | null) =>
    request<LocalEdit>(`/listings/${encodeURIComponent(listingId)}/local-edit/dismiss`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_ids: itemIds }),
    }),

  // Rough cuts (/cuts). A refusal's message is the sentence to show; a 503
  // from drafting means nothing was started.
  /** A page of the cut list: `offset` skips that many families (newest
   *  first); `more` says whether any come after. */
  cuts: (limit = 30, offset = 0) =>
    request<CutsOverview>(`/cuts${query({ limit, offset: offset || undefined })}`, { cache: "no-store" }),
  /** Folders of video for the draft form; empty `q` lists the newest. */
  cutFolders: (q = "", limit = 20) => request<CutFolder[]>(`/cuts/folders${query({ q, limit })}`),
  /** Ask the Editor for a draft (202). It may wait for its window. */
  draftCut: (body: CutDraftRequest) =>
    request<CutDraft>("/cuts/drafts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  cutDraft: (id: string) =>
    request<CutDraft>(`/cuts/drafts/${encodeURIComponent(id)}`, { cache: "no-store" }),
  cancelCutDraft: (id: string) =>
    request<CutDraft>(`/cuts/drafts/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  /** Removes a failed or cancelled draft from the list. */
  clearCutDraft: (id: string) =>
    request<void>(`/cuts/drafts/${encodeURIComponent(id)}`, { method: "DELETE" }),
  /** A cut and every version of its family (any version's id). */
  cut: (id: string) => request<CutDetail>(`/cuts/${encodeURIComponent(id)}`, { cache: "no-store" }),
  /** The Premiere file for a path profile ("" for none). The first one marks
   *  the cut sent, after which a local draft's shots count as used. */
  sendCut: (id: string, profile = "") =>
    request<CutSendResult>(`/cuts/${encodeURIComponent(id)}/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile }),
    }),
  /** The message that asks Claude to review this version. Sends nothing. */
  cutReviewRequest: (id: string) =>
    request<CutReviewRequest>(`/cuts/${encodeURIComponent(id)}/review-request`),
  /** Deletes one version; the others keep their numbers. */
  deleteCut: (id: string) => request<void>(`/cuts/${encodeURIComponent(id)}`, { method: "DELETE" }),

  // Folders and looking. A refusal's message is the sentence to show.
  /** One level of a library: the folders directly inside `path` ("" is the
   *  library itself), and what is in it. */
  folderLevel: (libraryId: string, path = "") =>
    request<FolderLevel>(
      `/assets/folders${withFolder(query({ library_id: libraryId }), "path", path)}`,
      { cache: "no-store" },
    ),
  /** A clip's shots, in clip order; empty for a photograph or a clip whose
   *  shots have not been found. */
  assetShots: (assetId: string) =>
    request<ShotRow[]>(`/assets/${encodeURIComponent(assetId)}/shots`, { cache: "no-store" }),
  lookStatus: () => request<LookStatus>("/looks/status", { cache: "no-store" }),
  /** What a review of this folder would take, for the model named (else the
   *  Looker). Starts nothing. */
  lookPreview: (libraryId: string, folder: string, modelId?: string | null, redo = false) =>
    request<LookPreview>(
      `/looks/preview${withFolder(
        query({ library_id: libraryId, model_id: modelId, redo: redo || undefined }),
        "folder",
        folder,
      )}`,
      { cache: "no-store" },
    ),
  /** Start a review now or tonight (202). A 503 means nothing was started. */
  startReview: (body: StartReviewRequest) =>
    request<LookReview>("/looks/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  lookReviews: () => request<LookActivity>("/looks/reviews", { cache: "no-store" }),
  /** Stops a review; what it has looked at is kept. */
  cancelReview: (id: string) =>
    request<LookReview>(`/looks/reviews/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  /** A field left out stays as it is. */
  updateLookSettings: (patch: Partial<LookSettings>) =>
    request<LookSettings>("/looks/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
};

export const listingExportUrl = (id: string) => `/api/v1/listings/${id}/export/download`;

export const mediaUrl = (assetId: string, kind: string) => `/api/v1/media/${assetId}/${kind}`;

/**
 * Why a derivative would not load, as the HTTP status the server gives for it
 * (0 when the server could not be reached): 404 for a file that was never
 * made, 503 for one whose drive is away. Always asked of the server, never
 * of the browser's cache — see `urlStatus`.
 */
export const mediaStatus = (assetId: string, kind: string): Promise<number> =>
  urlStatus(mediaUrl(assetId, kind));

/** A square crop of one face, cut from the frame it was detected in. */
export const faceCropUrl = (faceId: string, size = 192) =>
  `/api/v1/people/faces/${faceId}/crop?size=${size}`;

/** A picture a chat tool returned (`ChatPicture.blob`), for an <img>. */
export const chatBlobUrl = (conversationId: string, blob: string) =>
  `/api/v1/chat/conversations/${conversationId}/blobs/${blob}`;

/** A file a chat tool made (a rough cut, selects); the link downloads it
 *  under the name in `ChatChip.artifact`. `chipId` is the chip's `id`. */
export const chatArtifactUrl = (conversationId: string, chipId: string) =>
  `/api/v1/chat/conversations/${conversationId}/artifacts/${encodeURIComponent(chipId)}`;

/** The picture FrameFound draws for a model's Test when it has no photograph
 *  to use, so what the model was shown can always be looked at. */
export const modelSampleUrl = () => "/api/v1/local-models/sample.jpg";

/** A clip's still at `ts` through one look, as an <img> address. `ts` must be
 *  one of the clip's listed moments (null: the reference); `version` is the
 *  look's, so a changed look is a new address. "none" is the still itself. */
export const clipLookPreviewUrl = (
  assetId: string,
  source: ClipLookSource | "chosen",
  ts: number | null,
  size: ClipLookSize,
  version: string,
) => `/api/v1/clip-looks/${encodeURIComponent(assetId)}/preview.jpg${query({ source, ts, size, v: version })}`;

/** A look's `.cube` file, for an <a download>. */
export const clipLutUrl = (assetId: string, source: "chosen" | "model" | "learned", size: ClipLookSize) =>
  `/api/v1/clip-looks/${encodeURIComponent(assetId)}/look.cube${query({ source, size })}`;

/** One zip of the chosen looks of every clip in this clip's folder. */
export const folderLooksUrl = (assetId: string, size: ClipLookSize) =>
  `/api/v1/clip-looks/folder.zip${query({ asset_id: assetId, size })}`;

/** A listing photograph as the local model's suggestion would leave it
 *  (less the sky), beside the photograph as shot. */
export const localEditPreviewUrl = (listingId: string, itemId: string, straighten = true) =>
  `/api/v1/listings/${encodeURIComponent(listingId)}/local-edit/${encodeURIComponent(itemId)}/preview.jpg${query({ straighten })}`;
