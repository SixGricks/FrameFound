# Roadmap & MVP Backlog

Versioning: SemVer. MVP = end of Milestone 8 ≈ v0.9; v1.0 after beta feedback.
Each milestone maps to a GitHub Milestone; items become issues at milestone start.

## M0 — Product & architecture definition ← current

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
