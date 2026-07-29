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
- 🎬 **Adobe Premiere Pro integration** (planned) — search, preview, and import from inside Premiere

Built for photographers, videographers, churches, auction houses, real-estate media teams,
drone operators, and small production companies.

> **Status: pre-alpha.** Milestone 0 (architecture and foundation) is in progress.
> Nothing here is ready for production use yet. See [docs/roadmap.md](docs/roadmap.md).

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

## Documentation

| Document | Purpose |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System architecture and diagrams |
| [docs/data-model.md](docs/data-model.md) | Database entity model |
| [docs/adr/](docs/adr/) | Architecture decision records |
| [docs/threat-model.md](docs/threat-model.md) | Security threat model |
| [docs/remote-access.md](docs/remote-access.md) | Tailscale / DDNS / Cloudflare Tunnel design |
| [docs/location.md](docs/location.md) | GPS, location inference, and places |
| [docs/maps.md](docs/maps.md) | Optional Google Maps basemap and address lookup |
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
