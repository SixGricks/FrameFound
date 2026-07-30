# Roadmap & MVP Backlog

Versioning: SemVer. MVP = end of Milestone 8 ≈ v0.9; v1.0 after beta feedback.
Each milestone maps to a GitHub Milestone; items become issues at milestone start.

## Where things stand — 2026-07-29

Milestones were not completed strictly in order: deployment happened early
(which surfaced eleven real bugs), and backup/restore was pulled forward
because a production install without it is negligent. What follows reflects
reality rather than the original sequence.

### Done and running in production

| Milestone | State | Notes |
|---|---|---|
| **M0** Architecture | ✅ | 10 accepted ADRs, threat model, licence inventory |
| **M1** Foundation | ✅ | auth, migrations, queues, CI/release, health checks |
| **M2** Indexing | ✅ | 15,774 assets across 4 libraries incl. the 18 TB GELCO share |
| **M3** Proxies & previews | ✅ | thumbnails, posters, H.264 proxies, signed URLs |
| **M4** Transcription | ✅ | faster-whisper + VAD, sidecar import; a retry sweep now catches work that fails and is forgotten |
| **M5** Visual search | ✅ | CLIP ViT-B/32 via ONNX, pgvector HNSW, similar-assets |
| **M6** Web UI alpha | ✅ | search, browse, asset detail, places, storage, tags, dashboards |
| **M7** Remote access | ✅ | 2FA, sealed secrets, DDNS, kill switch, sessions, Tailscale enrolment |
| **M8** Hardening | ✅ | backup/restore, image scanning + SBOMs, benchmarks, failure drills (9/9) |
| **M9** Editorial handoff | 🔶 started | ADR-0019 decided; FCP7 XML export shipped |

### Measured state of the production install — 2026-07-29 17:45 UTC

| | |
|---|---|
| Assets indexed | **15,774** across 4 libraries |
| — GELCO (18 TB share) | 6,820, scan complete |
| — Intel 2026 / Promo / Breeze | 8,954 |
| Thumbnails ready | 10,694 and climbing |
| Located — EXIF | 4,510 (up ~600 as GELCO metadata lands) |
| Located — inferred | 264, across 67 places |
| Transcripts | 29, backlog re-queuing after the retry-sweep fix |
| Geocode cache | 0 rows — no Google keys configured, by choice |
| Queues | metadata 5,042 · vision 1,749 · transcribe 18 — all draining |

### In progress

- **GELCO processing** — scan complete at 6,820 assets; metadata, thumbnails
  and embeddings are draining now, with location inference to follow. First
  library at real scale, and the honest benchmark for everything after.
- **Transcript backlog** — the retry sweep is feeding the 40 assets that
  failed on the old permission fault back through, 25 at a time.

### Needs attention

- **21 damaged files, 127 GB** — found by the QA sweep, not a FrameFound bug.
  All fail with "moov atom not found": recordings interrupted before the camera
  finished writing the container. They will not open in Premiere either. One is
  125 GB (`Brian Job and Promo/2023 11 21 Camera 2`), fifteen are DJI drone
  clips, and four are 24–48 byte stubs. Repair tools sometimes recover this;
  FrameFound now says so instead of reporting a generic failure.

- **80 failed derivatives.** Four are the 1–2 GB TIFF panoramas that exceed
  the worker's 1.27 GB memory limit (waiting on the RAM upgrade, and now
  reported honestly as running out of memory). The rest are mostly DJI MP4
  proxy failures and the BRAW proxies deferred until a GPU exists — worth a
  pass to confirm nothing else is hiding in there.
- **SMB reads at 5.2 MB/s** (measured). This is the binding constraint on
  every whole-file operation and shapes timeouts throughout.

### Next up

1. **Tag the things you care about.** The mechanism is proven — one DJI aerial
   produced 52 correct suggestions out of 60. It gets useful once real tags
   exist.
2. **Inference tuning** (#34) against places you can verify.
3. **M9 remaining**: marker export from transcript hits, panel tokens with
   revocation, then a UXP spike against a current Premiere (ADR-0019).
4. **Fixture corpus** (#26) — needs real RAW/HEIC samples.
5. **Next 16 upgrade** as its own piece of work (PRs #8/#11 held together).

### Recently landed

- **QA sweep** — twelve data-integrity checks over the live catalogue; eleven
  clean. The twelfth asked why 72 assets were `ready` with no thumbnail and
  found 21 damaged files (127 GB): recordings interrupted before the camera
  finished writing the container. FrameFound now names that plainly instead of
  reporting a generic failure — 20 of the 21 already updated.
- **Tags are searchable** — the QA sweep found the obvious hole: tagging
  existed but search did not know about it, which made a tag a label rather
  than a search feature. Tag hits now lead the search page (a tag is a human
  judgement; a filename or a CLIP score is a guess), Browse filters by tag from
  the URL, and confirmed tags are kept visually distinct from unreviewed
  suggestions everywhere they appear.
- **Nav split at ten items** — four "find" destinations stay visible; the six
  administrative ones moved behind one Manage menu with click-away, Escape and
  `aria-haspopup`.
- **M9 started** — [ADR-0019](adr/0019-premiere-panel.md): UXP over the
  deprecated CEP, with a browser handoff shipping first because it needs no
  Adobe SDK and serves Resolve and Final Cut too. FCP7 XML export is live.
- **M8 complete** — [benchmarks](benchmarks.md) measured on the real install,
  and `drills.sh` failure drills at 9/9 including a backup verified restorable
  by `pg_restore`.
- **Vector search 27x faster** (75.0 ms → 2.8 ms p50). The benchmark found it
  doing a Sort instead of an HNSW index scan; the cause was stale statistics
  after a bulk embedding run, not a missing index. The scanner now ANALYZEs on
  its maintenance tick.
- **The shell scripts were never executable** from a clone — all four committed
  mode 644, so backup had only ever worked for people invoking `bash manage.sh`.
  Found by drill 4 on its first run.
- **Learning tags** — tag a video "Power Broom" and the system finds the other
  power brooms. CLIP puts words and images in one space, so a new tag works
  zero-shot from its own name, then shifts toward the operator's examples as
  they accumulate. The match bar is derived per tag, not fixed: low enough to
  admit the weakest accepted example, high enough to exclude the closest
  rejected one. Removing a tag is stored as a *rejection*, so a wrong guess is
  never offered twice — every correction tightens the next round.
- **Tailscale enrolment** (#30) — guided setup on the Security page, an
  optional sidecar behind the `tailnet` profile, and a tailnet address that is
  *learned* from a request that actually arrived over it rather than assembled
  from configuration, so it cannot be shown wrong.
- **Storage management from the UI** (ADR-0018) — add and remove media and
  cache drives from a form, via a scoped mount helper that is off unless
  enabled.
- **UI/UX audit fixes** — keyboard focus was invisible app-wide (no
  `:focus-visible` anywhere, and `a { text-decoration: none }`); `--paper-faint`
  measured ~3.3:1 against the background, under WCAG AA for text that carries
  real information; the only layout breakpoint was for asset detail, so the
  8-item nav had no mobile behaviour. All three fixed, plus a skip link, touch
  targets, and `aria-current` on the active nav item.
- **Transcription retry sweep** — 555 jobs had failed on a models-directory
  permission fault, exhausted their Celery retries, and were never looked at
  again; the fault was fixed long before anyone noticed the backlog. The
  scanner now re-queues audio that never got a successful attempt, bounded
  and skipping files that have failed repeatedly.
- **Maps and location documentation** — [maps.md](maps.md) and
  [location.md](location.md), linked from the settings card itself.
- **GELCO library** — the 18 TB share added read-only, with Premiere scratch
  folders (previews, auto-save, captured-and-generated, #recycle) excluded so
  regenerable intermediates never enter the catalogue. Proxies off, matching
  Intel 2026.
- **Google Maps, opt-in** — a real basemap on Places and address lookup for
  clusters the folders cannot name. Two separate keys (browser, referrer-
  restricted; geocoding, server-side and IP-restricted), both sealed at rest
  and configured on the Security page. Off by default: enabling either sends
  data to Google. Geocoding results are cached in the database, and a place
  the folders already name is never looked up.
- **Place detail view** — a place opens as a library-style page with media,
  position-source and sort filters, and paging.
- **Places** (#34): 4,177 located assets clustered into 67 named shoots,
  named from folder structure rather than a gazetteer.
- **`/assets/near` was unreachable** — registered after `/assets/{asset_id}`,
  so FastAPI parsed "near" as a UUID. Never worked until now; no test had
  covered it.
- **Location inference**: 264 positions lent from GPS-bearing cameras to
  cameras that were on the same job. Anchors are EXIF-only, so no inferred
  position ever seeds another.
- **Cross-library move detection** and the watcher departure lane — see
  "Media that moves" below.
- **Duplicate detection** (#24) with on-demand full-BLAKE3 verification.
  Real result: ~3.5 GB reclaimable, 1.2% of the corpus.
- **Trusted-proxy client IP handling** — a client can no longer spoof a LAN
  address past the public-access gate.
- **Supply chain** (#19): both images scanned and SBOM'd on every push;
  releases scan before publishing and pin by digest. Caught a real CVE on
  its first run.
- **Large-image handling**: images over 192 MB scale through FFmpeg rather
  than Pillow, with timeouts derived from file size.

### Deliberately deferred

Face recognition (privacy-gated), OIDC/SSO, multi-tenant permissions, mobile
apps, OpenSearch backend, Kubernetes, DaVinci/Lightroom integrations,
generative summaries, cloud storage drivers.

## Storage management from the UI (shipped)

Drives are added and removed from **Storage** in the UI. A media drive is
mounted read-only and can register a library and start a scan in one step; a
cache drive is writable and holds thumbnails and proxies, keeping generated
files off the system disk.

Mounting needs `CAP_SYS_ADMIN`, so it lives in a `mounter` sidecar that holds
that capability and drops every other one — never in the API, which terminates
untrusted requests. It sits behind a compose profile and is **off by default**:

```bash
docker compose --profile storage up -d
```

An install that never adds a drive from the UI never runs a privileged
container at all. Constraints and the reasoning behind each are in
[ADR-0018](adr/0018-mount-helper.md); the short version is cifs/nfs only,
targets confined under `/mnt/media` or `/mnt/cache`, options constructed
rather than accepted, argv with no shell, credentials via a 0600 file, media
always read-only, and validation repeated inside the helper because that is
the side holding the capability.

Mounts made this way are live immediately but do not survive a host reboot.
The UI returns the exact fstab line and says so, rather than silently writing
to the host's `/etc/fstab` — a larger privilege that was deliberately not
taken.

**Still open:** health-aware storage — disconnected-mount alerts, capacity
warnings, and per-library storage attribution on the System page.

## Maps provider — revisit

Google Maps is wired in as an **opt-in** basemap and address-lookup layer, off
by default. It is the only outbound dependency in normal operation, and worth
revisiting once real usage shows what is actually needed.

The seams are deliberately narrow — `media/geocoding.py` is the only file that
speaks Google's protocol, `media/maps_store.py` holds the keys and toggles,
and `PlaceMap.tsx` already renders two ways. `geocode_cache` is keyed on
coordinates, not on a provider, so cached addresses survive a switch.

Worth weighing when the time comes:

- **Self-hosted tiles** (OpenMapTiles / Protomaps + MapLibre) — nothing leaves
  the network; real setup cost and disk. Most consistent with a self-hosted
  catalogue.
- **Nominatim, self-hosted, regional extract** — offline reverse geocoding,
  no per-lookup cost. Folder-name naming already does most of this work, so
  the marginal value is small.
- **Mapbox / MapTiler / Esri** — commercial like Google; Esri's aerial imagery
  is arguably better for property work.

Decision deferred deliberately: the current setup costs nothing until enabled,
and the folder-name naming means geocoding may barely be used. Full comparison
in [maps.md](maps.md#switching-to-a-different-map-provider).

## Media that moves (mostly shipped)

A file that reappears at a new path with the same size and partial hash
re-binds to the existing asset, keeping its UUID, transcripts, thumbnails, and
embeddings (ADR-0010).

**Working now:**

- **Across libraries** — the lookup is global, not per-library. A clip dragged
  from `Intel 2026` to `Archive 2026` keeps its derived data and simply
  changes `library_id`. Before re-binding, the old path is stat'd: if the file
  is still there, this is a genuine duplicate, not a move.
- **Whole-folder reorganisation** — the watcher walks a moved directory's
  subtree, since watchdog reports the folder and says nothing about its
  contents. Each file then re-binds through the same content lookup.
- **Departures** — deletes and moves out of a watched tree flag their assets
  `missing` after a 60-second grace period and a confirming stat, instead of
  waiting for the next reconciliation scan. Nothing is deleted from the
  catalogue; a NAS that blinks must not take the catalogue with it.
- **Verification pass** — `POST /api/v1/duplicates/verify` runs full BLAKE3
  hashing on demand to confirm that files really are the same bytes.

**Still open:**

- **Across mounts and drives** — same content on a new NAS or a new share,
  including when a library root itself changes.
- **Re-linking after restore** — after `manage.sh restore` onto new hardware,
  reconcile the catalog against storage by content rather than by path.

Design note: all of this hangs off the existing `partial_hash` /
`content_hash` columns — no schema change was needed, and none is expected for
what remains.

## M0 — Product & architecture definition ✅

- [x] Architecture doc + diagrams · data model · ADR seed set (0001-0005)
- [x] Threat-model outline · license inventory · repo scaffolding · compose definition
- [ ] Backlog converted to GitHub issues; labels + milestones created
- **Definition of done**: docs above merged; `docker compose config` valid; API
  and web skeletons boot with health checks green; CI runs lint+tests on PR;
  a new contributor can go from clone → running skeleton via CONTRIBUTING.md
  in under 30 minutes.

## M1 — Repository & infrastructure foundation (v0.1)

- Compose stack boots end-to-end (pg + redis + api + web + caddy + workers)
- Alembic baseline migration; config system; structured logging
- Local auth: argon2id, sessions, roles, rate limiting, setup token, first-run wizard (admin creation)
- Health endpoints (`/healthz`, `/readyz`) for every service; system-health API
- CI: ruff/mypy/pytest, ESLint/tsc/vitest, docker build, Trivy scan, license scan
- GHCR publishing with version tags; Dependabot config

## M2 — Library indexing (v0.2)

- Library CRUD + path validation against allowlist; path-mapping profiles
- Recursive initial scan (bounded memory, progress, pause/resume, restart-safe)
- File-identity strategy (ADR-0010): size+mtime+partial hash; full BLAKE3 on demand; dedupe
- Stability detection for in-flight copies; watcher (watchdog) + periodic reconciliation
- Missing-mount detection → assets flagged `unmounted`, never deleted
- Metadata extraction: ffprobe, ExifTool, EXIF/GPS, camera fields

## M3 — Proxies & previews (v0.3)

- Image thumbnails + previews; video poster frames; waveforms (audio)
- 1080p H.264 proxies (CPU x264; NVENC when available); HLS or MP4 range streaming
- Derivative tracking, retention, regeneration; processing profiles v1
- Signed short-lived media URLs (ADR-0012); range-request streaming through API

## M4 — Transcription (v0.4)

- Audio extraction; faster-whisper provider (`TranscriptionProvider` interface)
- Language detection, timestamped segments, SRT/VTT generation
- Postgres FTS over transcripts; search API returns asset + timestamp
- Transcript panel in UI; click-to-seek

## M5 — Visual search (v0.5)

- `EmbeddingProvider` interface + OpenCLIP implementation
- Image embeddings; scene detection + duration-aware frame sampling; frame embeddings
- Text→image semantic query; related/similar assets; optional OCR stage
- Hybrid ranking: RRF over exact/FTS/vector (ADR-0011); quoted-phrase exact mode

## M6 — Web UI alpha (v0.6)

- Polished search page (unified box, filters, media-type toggles, match reasons)
- Asset detail: player with match/scene markers, transcript sync, metadata, paths + copy buttons
- Browse (folder tree mirroring NAS), processing dashboard, system health page
- Collections + saved searches; mobile-usable search/preview; attribution screen

## M7 — Remote access (v0.7)

- Remote-access wizard (4 modes); Caddyfile templating; public-HTTPS hardening
- Cloudflare DDNS adapter + sidecar (scoped token, IPv4/IPv6, error surfacing)
- Cloudflare Tunnel instructions/profile; Tailscale docs + detection
- TOTP 2FA, audit log, session management UI, "disable public access" kill switch

## M8 — Beta hardening (v0.8-0.9)

- Large-library benchmarks (target: 100k+ assets; publish measured numbers)
- Failure-mode drills: NAS disconnect mid-scan, worker OOM, power loss
- `manage.sh backup/restore` + documented full VM recovery; update workflow with
  preflight, migration backup, health-gated rollback (ADR-0014)
- Security review vs threat model; FFmpeg sandbox decision; pen-test checklist
- Proxmox deployment guide finalized (GPU passthrough, sizing, mounts)

## M9 — Adobe proof of concept (post-1.0 track)

- UXP vs CEP research (ADR-0015) → auth flow → search panel → proxy preview →
  import via path mapping → transcript→marker experiment

## Deferred beyond 1.0

Face recognition (privacy-gated), OIDC/SSO, multi-tenant permissions, mobile
apps, OpenSearch backend, Kubernetes, DaVinci/Lightroom integrations,
generative summaries, cloud storage drivers.

## Hardware path — 2012 Mac Pro (5,1) as the primary host

Decision 2026-07-28: the production host is the owner's 2012 Mac Pro running
Proxmox (2× Westmere Xeon, no AVX; upgradeable RAM and GPU). Strategy: prove
each milestone on this box, upgrading components only when a milestone needs
them. Any GPU purchased carries over to a future host — no stranded spend.

| Phase | Hardware change | Unlocks | Risk |
|---|---|---|---|
| A (now) | none — 4 GB VM | M2 verified at scale; M3 thumbnails + CPU (x264) proxies, slow but correct | low |
| B | +RAM (DDR3 ECC is cheap; target 48–64 GB host → 16–24 GB VM, restore HA to 8 GB) | full-archive scans, real proxy concurrency, M3 done at production scale | low |
| C | +GPU (NVIDIA ≤225 W for the dual 6-pin aux budget; RTX 3060 12 GB is the reference pick) + VT-d passthrough | **NVENC proxy transcoding** (M3 gets fast with zero software fight — NVENC needs no AVX) | low-medium (MP5,1 passthrough is well-trodden but firmware-quirky) |
| D | same GPU, custom AI builds | M4 transcription: faster-whisper/CTranslate2 has runtime CPU dispatch and CUDA execution — expected to work without AVX, must be proven on-box | medium |
| E | same GPU, custom AI builds | M5 visual search: official PyTorch wheels **require AVX and will not load**; path is ONNX-exported CLIP on onnxruntime-gpu or a from-source AVX-free torch build | **high** — timebox it; if it stalls, M5 waits for a CPU-era upgrade and everything else still ships |

Standing caveats: PCIe 2.0 bandwidth (minor for inference), Westmere
single-thread speed (scans/transcodes are parallel, so throughput is fine),
and the AVX ceiling is permanent — a future platform swap is migration-by-
design (`manage.sh backup` → restore; GPU moves over).

## Major technical risks

| Risk | Exposure | Mitigation |
|---|---|---|
| pgvector HNSW performance at millions of frame vectors | search latency promise | benchmark at M5/M8; SearchBackend seam ready for OpenSearch/Qdrant |
| SMB/NFS watcher unreliability | missed/duplicate events | reconciliation scan is the source of truth; watcher is an optimization |
| FFmpeg/codec matrix (HEVC 10-bit, MXF, HEIC, RAW) | proxy failures on pro media | fixture corpus per codec; per-stage degradation, never total failure |
| GPU driver/CUDA/toolkit variance on user hardware | install failures | CPU is the default path; GPU strictly additive via overlay file |
| Whisper accuracy on domain audio (auctioneers!) | search quality | model-size setting per profile; user-corrected transcripts feed back into FTS |
| Adobe UXP API surface for panels still maturing | M9 slip | research-first ADR; core API designed panel-agnostic |
| One-VM resource contention (DB vs FFmpeg vs GPU) | perceived instability | queue segregation, concurrency caps, quiet hours (M2/M8) |
| Redis licensing drift | distribution clarity | pin BSD build or move to Valkey (tracked in licensing.md) |
