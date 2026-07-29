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
| **M2** Indexing | ✅ | 8,954 assets indexed from an 18 TB SMB archive |
| **M3** Proxies & previews | ✅ | thumbnails, posters, H.264 proxies, signed URLs |
| **M4** Transcription | ✅ | faster-whisper + VAD, SRT sidecar import, timestamped search |
| **M5** Visual search | ✅ | CLIP ViT-B/32 via ONNX, pgvector HNSW, similar-assets |
| **M6** Web UI alpha | ✅ | search, browse, asset detail, dashboards, security page |
| **M7** Remote access | ✅ core | 2FA, sealed secrets, DDNS, kill switch, sessions |
| **M8** Hardening | 🔶 partial | backup/restore/verify/update done; benchmarks pending |

### In progress

- **Library-wide visual indexing** — embedding ~9k assets (~70 min of
  background work at 288 ms/image on current hardware).
- **Transcription backfill** across the promo/auction footage.
- **Trusted-proxy client IP handling** (issue #31) — **must land before
  public exposure**, or a client could spoof a LAN address past the gate.

### Next up

1. **Storage management from the UI** (see "Storage management" below).
2. **Cross-library move detection** (see "Media that moves" below).
3. M8 proper: large-library benchmarks, failure drills, security review.
4. M9: Adobe Premiere panel research (UXP vs CEP).

### Deliberately deferred

Face recognition (privacy-gated), OIDC/SSO, multi-tenant permissions, mobile
apps, OpenSearch backend, Kubernetes, DaVinci/Lightroom integrations,
generative summaries, cloud storage drivers.

## Storage management from the UI (planned)

Today, adding storage means editing `/etc/fstab` on the host and setting
`FRAMEFOUND_DATA_STORE`. The goal is to do it from **Settings → Storage**:
discover a NAS share, mount it, and choose its role — **media library**
(read-only, gets scanned) or **cache storage** (read-write, holds thumbnails
and proxies).

**Why this is not built yet:** mounting a filesystem needs `CAP_SYS_ADMIN`.
Granting that to the web application would mean a container that can mount
arbitrary network paths as root — squarely against the threat model's "no
privileged containers unless unavoidable", and a serious escalation surface
for an app that is designed to be internet-facing. That decision deserves an
ADR, not a quick patch.

Planned in three stages, each independently useful:

1. **Read-only storage view** (safe, next) — list mounted filesystems visible
   to the containers with free space and role, let an admin assign an
   already-mounted path as a library or as the derivative store, and for
   anything not yet mounted, *generate the exact fstab line to paste*. No new
   privileges at all.
2. **Guided mount via a scoped helper** (needs ADR-0018) — a tiny sidecar
   holding only `CAP_SYS_ADMIN`, accepting mount requests over a private
   socket, constrained to an allowlist of mount types and target directories,
   with every mount audited. The web app itself stays unprivileged.
3. **Health-aware storage** — surface disconnected mounts, capacity warnings,
   and per-library storage attribution on the System page.

## Media that moves (planned)

Move detection works **within** a library today: a file that reappears at a
new path with the same size and partial hash re-binds to the existing asset,
keeping its UUID, transcripts, thumbnails, and embeddings (ADR-0010). What is
missing is the rest of a real storage ecosystem:

- **Across libraries** — a clip moved from `Intel 2026` to `Archive 2026` is
  currently a delete plus a re-add, which discards its derived data. Needs a
  global content-hash index consulted before any asset is created.
- **Across mounts and drives** — same content on a new NAS or a new share
  should be recognised, including when a library root itself changes.
- **Whole-folder reorganisation** — detect that a directory moved as a unit
  and re-bind its assets in one operation rather than thousands of individual
  matches.
- **Re-linking after restore** — after `manage.sh restore` onto new hardware,
  reconcile the catalog against storage by content rather than by path.
- **Verification pass** — full BLAKE3 hashing on demand to confirm that
  re-bound assets really are the same bytes, and to catch silent corruption.

Design note: all of this hangs off the existing `partial_hash` / `content_hash`
columns, so no schema change is expected — the work is a global lookup path
plus a smarter reconciliation step, not new data.

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
