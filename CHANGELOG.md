# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Security (2026-11-10)
- Next.js 15.5.22 → 15.5.29: fixes three critical remote-code-execution
  advisories (CVE-2026-75604, GHSA-2464-fhr4-w9xw, GHSA-2xp9-vwfh-vxw4)
  that failed the web image scan.

### Fixed (2026-11-10)
- Backend type check passes under SQLAlchemy 2.0 and 2.1 alike (CI resolves
  2.1; the VM image is pinned to 2.0).
- The Google Drive key box's placeholder no longer looks like a service
  account key to the image's secret scan.
- MCP `decide_tags` and `start_api_review` take the short ids that
  `find_media` and `search_media` print, like every other tool.
- `contact_sheet` reads a `find_shots` line's time range from its own field,
  so a clip named `2026-11-03_course` no longer seeks to 2026 s.

### Added (2026-11-09) — shot intelligence for editing
- Motion analysis per video, from the proxy (never the original), on the
  `frames` queue after the proxy: block phase correlation and an affine fit
  in numpy (no new dependency), camera and subject motion separately, a
  10 Hz motion track (derivative kind `motion`), and **shot windows** —
  move (static, pan, tilt, push-in, pull-out, orbit, rise, descend, lateral
  glide, top-down drift), direction, speed, subject energy, and smoothness
  per playback speed (25–800%). Start/middle/end stills per window.
- Light per window: sun elevation from GPS (or the folder's photographs')
  and capture time with the NOAA formula, as night / blue hour / golden
  hour / low, mid, high sun; and the measured look (overcast or direct,
  warmth). Framing and subjects from CLIP against a vocabulary kept as data
  (golf-course construction built in; JSON files in `<data_dir>/vocabularies`).
  Descriptors, not tags: nothing enters the review queue.
- MCP: `find_shots` (filter windows across a library by folder, dates, move,
  smoothness at a speed, timeline length at that speed, energy, light,
  subjects, already used; flags slow motion that would duplicate frames),
  `contact_sheet` (one JPEG of up to 24 moments, or start/middle/end strips),
  `recent_cuts` (shot count, average length, speed, move, light and subject
  mix of the last cuts). `get_scenes` lists each scene's windows.
- Cut history: `build_rough_cut` records every cut it builds (new `client`
  and `record` parameters); `python -m framefound.ops.shots import-cut`
  reads an FCP7 XML timeline into history.
- `python -m framefound.ops.shots backfill` (with `--make-proxies` for a
  library whose proxies are off, and `--inline` to time it), `describe`,
  `inspect`. The scanner's visual sweep re-queues videos with a proxy and no
  motion track. Migration 0028 (`shot_windows`, `cuts`, `cut_pieces`).

### Changed (2026-11-09)
- MCP list answers are compact by default: `find_media`, `search_media` and
  `find_shots` return one line per item with an 8-character id and only the
  fields that hold something — no signed URLs, server paths or suggested
  tags unless `verbose` or `fields` asks. Every tool accepts the short id.
  The REST API, web UI and panels are unchanged.

### Added (2026-11-05, night)
- Bulk tag review on the Tags page: down-to-here, shift-click ranges,
  select all, a pinned Yes / No / "yes to these, no to the rest" bar and
  Y / N / Esc keys. `POST /tags/{id}/decide` answers many suggestions in one
  request and re-learns the tag once; `decide_tags` on the MCP connector.

### Changed (2026-11-05 QA, security and UI review)
- Search: one fused, ranked *Best matches* list (tag, picture, speech,
  name) with reasons, the moment to open and the matched video frame;
  natural sentences match transcripts (any-word top-up) and tags inside the
  query; transcript hits ranked by relevance, not time; one row per asset in
  visual search; a Photos/Video/Audio filter; offline files excluded;
  pgvector iterative scan for filtered searches; trigram indexes on file
  names (migration 0026); cached query embeddings.
- Tagging: new media is scored against learned tags on arrival, and every
  tag is re-learned nightly; a relaxed bar for well-taught tags; rejections
  veto look-alikes instead of locking a tag; videos judged on their best
  three frames; domain prompt templates; per-tag caps on unanswered
  suggestions.
- Transcription: retries counted per queueing (one bad night no longer
  gave a file up for good); every audio stream mixed before Whisper; speech
  kept unless both doubtful and silent, known invented phrases dropped;
  "no speech" recorded and shown, legacy no-speech clips re-heard (migration
  0027); `transcript_state` on the asset; `GET /assets/{id}/subtitles`
  (SRT/VTT); Reprocess re-transcribes; Redis visibility timeout 12 h; a file
  that keeps killing the worker is given up after three deaths.
- Clients: Lightroom and Premiere choose this computer's path profile on
  first run; profiles must fit their platform; Mac `install-dev.sh`.

### Added (2026-11-05)
- MCP video tools for Claude Cowork: `path_profiles`, `get_media_files`,
  `get_transcript`, `get_scenes`, `build_rough_cut` (FCP7 XML sequences for
  Premiere and Resolve), `view_media(ts_ms=)`, and `profile`/`media_type`
  on the search tools. Working Claude Desktop config for Windows and macOS.

### Security (2026-11-05)
- Cloudflare Tunnel visitors were classified as LAN, bypassing the
  public-access switch: tunnel traffic now enters on Caddy `:8081`, marked.
  **Point the tunnel at `http://caddy:8081`.**
- argon2 off the event loop; per-address sign-in limit (not for shared
  addresses); sky uploads are images only and served inert, CSP on `/api/*`;
  inpaint mask size cap; frame-grab caps; disabled users' tokens refused;
  read-only tokens cannot write; admin-only paid review over MCP; the
  geocoding key kept out of logs.

### Changed (2026-10-14 review)
- Server performance: the alerts banner probes shares off the event loop,
  in parallel, with a timeout and a 30 s cache (one hung CIFS mount froze
  the whole API for 59 s); the photo editor decodes and colour-manages a
  photograph once per editing session instead of on every slider notch;
  Showcase no longer sends 21,000-parameter `IN` lists or copies every
  embedding into Python objects, and encodes its prompts once; `GET /people`
  fetches covers in one query and honours the chosen cover; the session row
  is written about once a minute rather than on every request; derivatives
  carry `ETag`/`Last-Modified` and answer 304; nearest-asset, similar-media
  and folder queries order, de-duplicate and filter in SQL before the limit;
  pgvector searches set `hnsw.ef_search` so a library filter cannot starve
  the result; indexes for Browse's default order and the jobs table
  (migration 0024); the scanner's visual repair sweep runs every 30 minutes
  instead of 5 and face clustering is queued only when faces changed.
- Security: `/api/docs` is not served to the internet; the second-factor
  endpoints share the login rate limiter; the `readonly` role is enforced.
- Panels: see [docs/panels.md](docs/panels.md) ("2026-10-14 — review") —
  Premiere panel 0.4.0, Lightroom plugin 0.7.0, `GET /panel/me`, name
  matches before visual hits.
- Web, 2026-10-15 sweep by eye: listing detail fits a phone (rail below the
  grid) and photos reorder with ← → buttons as well as drag; the top bar
  drops the address before the nav overflows; the alert banner is a title
  on phones; face review on touch; plurals; clearable Showcase fields.
  `FRAMEFOUND_API_PROXY` lets the Next dev server forward `/api` to a local
  API, and `infrastructure/scripts/seed_dev.py` seeds a catalogue to look at.

### Added
- **Claude as a client (2026-11-05).** An MCP connector at `/api/mcp`
  (`framefound/mcp`, ADR-0020): Claude Code or Claude Desktop connects with
  a panel token and can find media by tag, search, look at thumbnails,
  propose tags and record the operator's decisions. Panel tokens gain the
  `mcp` host label and a `tag` scope; Claude's tags are suggestions, never
  facts. See [docs/mcp.md](docs/mcp.md).
- **A second opinion under a budget.** "Review with Claude" on the Tags page
  (and a nightly sweep, off by default) sends thumbnails of untagged media to
  the Claude API and queues what an admission policy admits. One ledger
  (`ai_usage`, migration 0025) records every call FrameFound makes to the API
  — reviewer, auto-edit, naming — and a **monthly budget** on the Security
  page is enforced from it before each call. `asset_tags.origin` says who
  suggested a tag (clip / claude / mcp) and the review queue shows it.
- **Tailscale in one button.** The api container reads the host's Tailscale
  socket (`/var/run/tailscale`, read-only) and the Security page shows the
  real state with a single *Sign in to Tailscale* button that opens the
  sign-in page, then the addresses to bookmark once the server joins.
- **Security page rework**: a plain-language checkup with a fix button per
  line, a jump bar to four sections, a QR code for two-factor setup, readable
  device names, and the own-domain/Cloudflare modes under *Advanced*.
- **Showcase by capture date**: a *Taken between* window (with quick picks)
  limits the ranking and the place list to what was shot in it.
- Tailscale: the Security page's card shows the connector's tailnet address,
  and [docs/remote-access.md](docs/remote-access.md) now has the full
  out-of-office recipe (VM install, `tailscale serve` HTTPS, Proxmox subnet
  router).
- Slideshow rendering: propose a themed selection, review it, render an MP4.
  Ken Burns per still, theme colour grade, crossfades, optional operator-supplied
  music. `slideshows` table (migration 0013), `/api/v1/slideshows`, and a
  Slideshows page. Assembled from per-slide pieces and stitched with the concat
  demuxer, so peak memory does not grow with the length of the slideshow —
  see [docs/video-generation.md](docs/video-generation.md) for the measurements
  that ruled out the single-filter-graph design.
- Manage → Basemaps page: download, size and delete for offline map regions.
  The API already existed; only the UI was missing.
- Milestone 0: repository scaffolding, architecture docs, ADRs, threat model,
  Docker Compose stack definition, backend and frontend skeletons, CI pipeline.

- Autocomplete on tag entry and person naming. Typing surfaces what already
  exists, so a second "power broom" tag or a fourth person called "Dad" can be
  merged at the moment of creation rather than discovered later. Naming a
  person offers a **Merge into** action per match.

- Editing panels for Adobe Premiere Pro (UXP) and Lightroom Classic (Lua SDK),
  plus the `/api/v1/panel` surface they share and scoped, revocable **panel
  tokens** managed under Security. Results carry a path already translated for
  the workstation through the `path_mappings` profiles, which had existed since
  Milestone 2 with nothing consuming them. See [docs/panels.md](docs/panels.md).

### Fixed
- Face thumbnails showed unrelated content. Faces are detected in *sampled
  frames*, but the grid cropped from the asset's thumbnail — a different
  picture — so any face found partway through a video showed whatever was at
  that spot in an unrelated frame (161 of 307 faces on the reference install).
  Compounding it, `object-fit: cover` cropped the tile before the box maths
  applied. Crops are now cut server-side from the correct frame; nothing is
  stored, they are computed per request.
- Faces below 40px are no longer embedded. ArcFace upscales to 112×112, so a
  seven-pixel face produced an embedding that was almost entirely
  interpolation and quietly polluted every cluster it joined.
- FFmpeg's thread cap reached only the decoder. `-threads` is a per-file
  option, so inserting it before `-i` left x264 running one thread per core on
  every proxy transcode while the setting appeared to be in force — measured at
  810 MB against 470 MB once actually applied.
