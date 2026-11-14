# FrameFound

**Self-hosted, AI-powered media catalog and search for people with real archives.**

FrameFound watches your existing NAS or local folders, indexes photos, video, and audio
*in place*, and makes everything findable from a browser:

- 🔎 **Natural-language search** — "red barn at sunset", "drone footage of a white farmhouse"
- 🗣️ **Transcript search** — every video where someone says *"starting bid"*, with timestamped seek
- 🖼️ **Visual similarity** — CLIP-style embeddings for photos and sampled video frames
- 🎞️ **Browser proxies** — lightweight previews generated automatically; originals never touched
- 📁 **Originals stay put** — read-only mounts, no import, no proprietary library
- 🔐 **Local-first** — all AI runs on your hardware by default; nothing leaves your network
- 🌐 **Secure remote access** — Tailscale, your own domain with automatic HTTPS, or Cloudflare Tunnel
- 🎬 **Adobe Premiere Pro and Lightroom Classic panels** — search, preview, and import from inside the editor
- 🤖 **Claude as a client** — an MCP connector, and a chat panel in the web app: ask for media by tag, have Claude look at pictures and propose tags, under a monthly budget you set

Built for photographers, videographers, churches, auction houses, real-estate media teams,
drone operators, and small production companies.

> **Status: pre-alpha.** There is no release yet. It runs one archive day to day, on a
> Mac mini since 2026-11-13, but nothing here is packaged or supported for anyone
> else's production use yet. See [docs/roadmap.md](docs/roadmap.md).

## How it works

1. Deploy with Docker Compose on any Linux server (Proxmox VM, bare metal, Unraid, TrueNAS SCALE).
2. Mount your NAS shares (SMB/NFS) into the host — **read-only recommended**.
3. Add those folders as watched libraries in the web UI.
4. FrameFound scans, extracts metadata, generates thumbnails and proxies, transcribes speech,
   and creates visual embeddings — all as *derived* files in its own data directory.
5. Search from any browser. Click a transcript match, the video seeks to that moment.

Original files are never renamed, moved, modified, or deleted. The entire catalog is
rebuildable from your originals.

## Quick start (once released)

```bash
git clone https://github.com/SixGricks/FrameFound.git
cd FrameFound
cp .env.example .env    # edit: set data path and secrets
docker compose up -d
```

Then open `http://<server>:8080` and follow the first-run wizard.

GPU acceleration (NVIDIA): `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d`

On a Mac, FrameFound installs as a program instead — no Docker, so it can use
Apple silicon's GPU and hardware encoder: [docs/mac.md](docs/mac.md).

## Documentation

| Document | Purpose |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System architecture and diagrams |
| [docs/data-model.md](docs/data-model.md) | Database entity model |
| [docs/adr/](docs/adr/) | Architecture decision records |
| [docs/threat-model.md](docs/threat-model.md) | Security threat model |
| [docs/remote-access.md](docs/remote-access.md) | Tailscale / DDNS / Cloudflare Tunnel design |
| [docs/storage.md](docs/storage.md) | Where everything lives: originals, the catalogue, derived files, and bulk storage with a folder per kind |
| [docs/mac.md](docs/mac.md) | Installing and running natively on a Mac; moving proxies; migrating from the containers |
| [docs/remote-workers.md](docs/remote-workers.md) | Workers on a second machine (optional, not packaged) |
| [docs/local-models.md](docs/local-models.md) | Local open-weight models on the Mac mini for tagging, scoring, colour and rough cuts, with Compare to choose between them; the plan, and what is built |
| [docs/video-looks.md](docs/video-looks.md) | Looks for video clips: three versions side by side, chosen by eye, exported to Premiere as a LUT |
| [docs/chat.md](docs/chat.md) | The chat panel: Claude inside FrameFound, what it can do, what leaves the building, what it costs |
| [docs/location.md](docs/location.md) | GPS, location inference, and places |
| [docs/maps.md](docs/maps.md) | Optional Google Maps basemap and address lookup |
| [docs/tagging.md](docs/tagging.md) | Tags that learn from your corrections, and Claude's second opinion |
| [docs/mcp.md](docs/mcp.md) | Connecting Claude Code / Claude Desktop (MCP) |
| [docs/panels.md](docs/panels.md) | The Premiere Pro and Lightroom Classic panels |
| [docs/testing/2026-07-31-panel-first-run.md](docs/testing/2026-07-31-panel-first-run.md) | Test report: the panels' first run |
| [docs/real-estate-editing.md](docs/real-estate-editing.md) | Real-estate editing and listing export: feasibility and plan |
| [docs/ai-editing-models.md](docs/ai-editing-models.md) | AI editing: auto-edit's modes, the models compared, measured quality and cost |
| [docs/video-generation.md](docs/video-generation.md) | Automated video and slideshow generation: options |
| [docs/future-features.md](docs/future-features.md) | Proposed features: where FrameFound can replace paid services |
| [docs/benchmarks.md](docs/benchmarks.md) | Measured performance and scaling limits |
| [docs/roadmap.md](docs/roadmap.md) | Milestones and MVP backlog |
| [docs/licensing.md](docs/licensing.md) | Dependency and model license inventory |
| [docs/deployment/proxmox.md](docs/deployment/proxmox.md) | Proxmox VM deployment guide |

## Repository layout

```
apps/
  server/        # Python: FastAPI API + Celery workers + scanner (one package, many entrypoints)
  web/           # Next.js + TypeScript frontend
  adobe-panel/   # Future Premiere Pro panel (research phase)
infrastructure/
  caddy/         # Reverse proxy configuration
  scripts/       # install.sh, manage.sh helpers
docs/            # Architecture, ADRs, threat model, deployment guides
fixtures/        # Small redistributable test media (planned)
.github/         # CI workflows, issue/PR templates
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Good first issues are labeled
[`good first issue`](../../labels/good%20first%20issue).

## License

[Apache License 2.0](LICENSE). AI model weights are downloaded at runtime and carry
their own licenses — see [docs/licensing.md](docs/licensing.md).
