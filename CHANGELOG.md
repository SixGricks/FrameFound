# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning: [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added (2026-11-14) — local AI models, the Claude chat panel, bulk storage on the NAS
Nothing below has run with a real model, on a Mac, or against the real
Claude API; model answers in the tests come from a stand-in
(docs/local-models.md).
- **Bulk storage, a folder per kind** (docs/storage.md): proxies, renders,
  exports, basemaps, backup copies and the model library each live in a
  subfolder of one bulk folder, or anywhere of their own. A **Storage**
  page and `framefound storage` / `framefound storage move <kind> <folder>`
  move one kind at a time (copy, check, switch; never a delete), with
  Remove old copies and **Make the missing proxies again**
  (`POST /storage/proxies/rebuild`). `framefound install --store <folder>`,
  `FRAMEFOUND_BULK_DIR`.
- **Chat panel with Claude** (docs/chat.md): **Ask Claude** on every page;
  the connector's tools run on the server as the signed-in user, with the
  page's context; streaming turns; tag decisions, paid reviews and folder
  reviews ask first (`chat.tool_approved` / `chat.tool_declined` in the
  audit log); **Stop**; the monthly budget checked before every request
  (ledger purpose `chat`); pictures on by default with a per-conversation
  switch; **Chat model** on Security. `/api/v1/chat`, migration 0031.
  Panel-only tools `local_cut_draft` and `start_folder_review`; the
  external connector does not gain them.
- **AI models page** (Manage → AI models, Mac install; docs/mac.md, ADR-0023):
  check a Hugging Face model before downloading (publisher, source,
  licence, files, size, memory, runtime), download, resume, remove; the
  **Looker** and **Editor** roles, copied to this Mac first when the
  library is on a share; **Test**; the software that runs models (MLX-VLM,
  then llama.cpp) installed, updated and rolled back from the page; day and
  overnight memory budgets, the overnight window, Pause, Unload now; makers,
  converters and blocked publisher lists (44 blocked built in); a sealed
  Hugging Face token; recent activity. `/api/v1/local-models`, migration
  0032. New Mac services `model-server` and `worker-local-ai` (queue
  `local_ai`); `FRAMEFOUND_RUNTIMES_DIR`; `framefound doctor` local-AI lines.
- **Compare** (AI models › Compare models; ADR-0024): a frozen sample from
  finished cuts' shots, a folder, a listing or an earlier comparison; one
  `compare_model` job per model; answers side by side with agreement
  against decided tags and used shots given as counts; the owner's
  decision and an optional "Make it the Looker". Nothing reaches the
  catalogue. `/api/v1/local-models/compare`, migration 0033.
- **Looking** (docs/local-models.md): **Review with AI** on a folder or
  library, Start now or Tonight, clips made ready first, Cancel keeps what
  was looked at; a per-library "Look at new footage with AI" switch; tag
  suggestions of origin `local` with the moment in the clip; a caption and
  0–100 score per photograph and shot. Browse gains a **Folders** view and
  a viewer; reviews show on Processing; an item's page shows what the
  model saw and a clip's page lists its **Shots**. Search ranks by the
  score (±10% at most) and matches "AI caption"; listing culling and
  `find_shots` (`min_score`, `order="score"`) use it. `/api/v1/looks`,
  `GET /assets/{id}/shots`, migration 0034.
- **Colour** (docs/video-looks.md): a **Look card** on each clip (the
  model's suggestion, the learned look, no change), Suggest a look for a
  clip or a folder, Adjust with eight sliders, a `.cube` LUT (33 or 65
  points) or a folder's as one zip; on a listing, **Ask the local model**
  for straightening, sky, caption and file name, applied with auto-edit
  mode `local`. `/api/v1/clip-looks`, `/listings/{id}/local-edit`,
  migration 0035.
- **Rough cuts from the local Editor**: a **Cuts** page (`/cuts`, one line
  per family of versions) and a page per cut with its shots, Compare of
  two versions, Download for Premiere for any path profile, Draft again,
  Delete this version and Ask Claude to review. **Draft a cut** from the
  Cuts form, Search's ticked clips, a clip's page or a folder in Browse.
  `build_rough_cut` takes `parent` to keep a revision beside the cut.
  `/api/v1/cuts`, job `draft_cut`, migration 0036 (versions, provenance,
  `sent_at`, piece notes, `cut_drafts`).
- **llama.cpp** as a second runtime for GGUF builds, from GitHub's pinned,
  checksummed release archive, by version or daily build; the Check lists
  a repository's builds and suggests one; a model on llama.cpp not yet
  compared asks "Make it the Looker anyway?" before a role.
- `framefound update` backs up the catalogue first and stops if the backup
  fails; `framefound update --skip-backup`.
- `framefound doctor`: `locale` and `rsync` lines.
- `GET /cuts` pages with `offset` and `more`.
- Demo mode for review on any machine (`FRAMEFOUND_LOCAL_AI_DEMO=1`, made-up
  answers, ignored on a real install) and a dev seed for every new page
  (`seed_dev.py`, `--local-ai-only`).
- Docs: docs/chat.md, docs/local-models.md (now the guide as built),
  docs/video-looks.md, ADR-0023, ADR-0024; docs/mac.md gains Updating,
  Bulk storage and Local AI models.

### Changed (2026-11-14)
- `framefound ops shots backfill` with no `--library` does every library
  in turn.
- The viewer from Tags (← →, Y / N, Esc) is the one viewer on People,
  listings and slideshows too; People opens a clip at the moment the face
  appears.
- Every page has its own browser-tab title instead of plain "FrameFound".
- The shot search, cut history and rough-cut writer moved out of the
  connector into `framefound/nle/`, so `ops.shots import-cut` and the
  Cuts API work without the `mcp` extra; connector answers are unchanged.
- An update git refuses ends with a plain message naming the files edited
  on this Mac, instead of `git failed (exit 1)`; an edit is never discarded.
- Answering a tag suggestion and Re-learn no longer wait on the queue.
- Storage lists the model library like any other kind (no longer
  "Reserved"); Basemaps says where its maps are kept.
- The Manage menu works from the keyboard.

### Fixed (2026-11-14)
- **Mac install:** Homebrew's rsync is installed beside macOS's older one;
  the `framefound` command is in place before the database and services
  start; every service starts with `LC_ALL`/`LANG` `en_US.UTF-8`, so
  Postgres starts as a background service.
- A nightly backup whose copy to the NAS fails (full or dropped share) is
  kept on the Mac and says so, instead of failing.
- A folder review could ask about the same clip again and again and never
  finish ("20 of 8") when a clip had shots on record but no ready
  shot-analysis file. Such a clip is made ready like any other, and if it
  never becomes ready it is looked at shot by shot from the shots on
  record. A review never asks the same model about an item twice and its
  count never passes its total; an answer that did not count shows as
  "could not be looked at" with a plain sentence, and the answer and its
  tag suggestions are kept.
- Applying a listing's local edits leaves out a photograph whose
  suggestion was made again after you looked, and says how many.
- The Draft a cut folder picker counts clips in sub-folders; a draft from
  footage holding more than 20,000 shots is refused in plain words, from
  the page and the chat panel alike.
- Sampled search frames are written whole or not at all.
- Send to Premiere from a search result found by its AI caption starts at
  the matching shot, instead of at 0 s.
- `ops.shots backfill --inline` drops a shot's old AI look when the shot's
  boundaries change, and no longer stops after one clip fails.
- The model server refuses a model whose llama.cpp program was deleted with
  a sentence, instead of an internal error; Compare holds back a model
  whose software is not installed.
- A model download that drops reports the amount actually kept.
- Slideshows: Render while the queue is down no longer leaves a slideshow
  stuck at "queued", and a finished video stays playable.
- A clip's page says why a clip will not play instead of showing a dead
  player; Develop on a phone puts the photograph above the sliders.
- Listings: Add photos › Search finds photos by tags and captions, not
  only file names; a quick local Apply says when it has finished.
- Tailscale on a Mac: the Security card reads the Tailscale app's real
  state and shows the Mac app's steps.
- Review with AI says what was already looked at by kind ("6 photographs
  and 2 clips (6 shots)") instead of a count like "12 items" for 8 files,
  and its progress names its unit ("4 of 4 photographs and shots").
- Counts read as English ("1 person", "1 tag").
- A folder review whose model could never run as things stood (a
  llama.cpp model before llama.cpp is installed) crashed while ending
  itself and stayed "active" for ever; it now ends "failed" with the
  reason at once. Review with AI and the chat panel now refuse such a
  model up front (409), as Compare does, and the dialog shows it as not
  available with a link to Software that runs models.
- Review with AI: after a review ends, "See what is left here" no longer
  keeps the old "Also look again at …" sentence; a count left by an older
  build is never shown as "N of M" with N past M.
- A clip that cannot play here: its shot buttons no longer say "Play",
  and the Look card no longer mentions a player that is not there.

### Added (2026-11-11) — FrameFound as a program on a Mac
- **Native macOS install** (docs/mac.md, ADR-0022):
  `infrastructure/mac/install.sh` and the `framefound` command
  (`framefound/native`). Every service runs as a launchd agent in the login
  session — no Docker — from one folder, `~/FrameFound`.
  Commands: `install`, `status`, `start`, `stop`, `restart`, `open`,
  `doctor`, `logs`, `update`, `backup`, `restore`, `proxies`, `migrate`,
  `ops`, `uninstall`.
- **A separate, movable proxy store**: `FRAMEFOUND_PROXY_DIR`,
  `framefound proxies move <folder>`, `framefound proxies rebuild`
  (`python -m framefound.ops.proxy_store`). A store that is away pauses
  proxy generation instead of failing it, and the media endpoint answers
  503 "not connected" rather than 404. The System page lists the store.
- **`framefound migrate --from user@host`**: moves a container install's
  catalogue, settings and derived files to a native install and rewrites
  library roots.
- `REDIS_PASSWORD` (optional; set by the native install).
- `jobs.worker` (migration 0030) and a **Machines** section on the
  processing page; recent failures name their machine.
- Workers on another machine, as an unpackaged option
  (docs/remote-workers.md): `FRAMEFOUND_ROOT_MAP`, hand-back of jobs a
  remote worker cannot do, `python -m framefound.ops.worker_check`,
  `docker-compose.remote-workers.yml`.
- VideoToolbox detection for proxies (used once its quality setting is
  chosen), `FRAMEFOUND_ONNX_PROVIDER=coreml`, `FRAMEFOUND_MODELS_DIR`.

### Changed (2026-11-11)
- Listing exports, slideshow renders, object removal, AI edits, basemaps and
  training pairs moved from the `media` queue to a new `exports` queue,
  consumed by `worker-media` as before; `media` is now proxies alone.
- The scanner skips any folder that holds a FrameFound store marker, so a
  proxy or data store placed on a library's share is never catalogued.

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

### Added (2026-11-06)
- Send selects to Premiere: one FCP7 XML with a bin of the source clips (a
  marker at each moment, its comment the sentence said, the tag or
  `visual: <query>`) and a `<title> selects` sequence of the moments with
  1 s handles. Soundbites widen to whole sentences (≤ 20 s), pictures to
  their detected shot (≤ 8 s, ±3 s without scene data), photos hold 4 s;
  overlapping moments merge; search or shooting order. MCP tool
  `build_selects`, *Send to Premiere* on the search page,
  `POST /panel/export/selects` (`export` scope). ADR-0021.
- The web FCP7 bin export (`POST /tags/export/fcp7`) takes `profile` and
  writes workstation paths, like the panel's.
- `fixtures/premiere/subclip-probe.xml`: import it to learn whether Premiere
  takes FCP7 in/out clips as subclips.

### Changed (2026-11-06)
- Premiere proxies (`Proxies/<stem>_Proxy.<ext>`, a per-library
  `proxy_pattern`) are paired with their original instead of catalogued:
  out of search, Browse, tagging, the review queues and the processing
  sweeps, and decoded in the original's place for frame sampling and scene
  detection. A proxy with no original stays an asset. Migration 0029;
  existing rows: `python -m framefound.ops.pair_proxies` (report), then
  `--apply`. Tag decisions on a proxy move to its original.

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
