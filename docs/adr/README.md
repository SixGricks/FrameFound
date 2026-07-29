# Architecture Decision Records

Format: [template.md](template.md). One decision per file, numbered, never
deleted — superseded ADRs are marked as such.

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-monorepo-single-server-image.md) | Monorepo with a single Python server image | Accepted |
| [0002](0002-fastapi-backend.md) | FastAPI + SQLAlchemy backend | Accepted |
| [0003](0003-postgres-pgvector.md) | PostgreSQL + pgvector as the only datastore for MVP | Accepted |
| [0004](0004-celery-redis-queue.md) | Celery + Redis for background jobs | Accepted |
| [0005](0005-caddy-reverse-proxy.md) | Caddy as default reverse proxy | Accepted |
| 0006 | Next.js App Router frontend | TODO(m6) |
| 0007 | FFmpeg/ExifTool media toolchain and subprocess sandboxing | TODO(m2) |
| 0008 | AI provider interfaces (ASR, embeddings, captions, OCR) | TODO(m4) |
| [0009](0009-local-auth-sessions.md) | Local auth with server-side sessions; OIDC deferred | Accepted |
| [0010](0010-file-identity.md) | File identity strategy (size + mtime + partial/full hash) | Accepted |
| 0011 | Hybrid search ranking via Reciprocal Rank Fusion | Implemented (search/ranking.py) |
| [0012](0012-signed-media-urls.md) | Signed short-lived media URLs | Accepted |
| 0013 | Remote access modes (Tailscale / DDNS+Caddy / Cloudflare Tunnel) | TODO(m7) |
| 0014 | Update & rollback strategy, release manifest | TODO(m8) |
| 0015 | Adobe panel technology (UXP vs CEP) — research first | TODO(m9) |
| [0016](0016-valkey-queue-store.md) | Valkey as the queue store image | Accepted |
| [0017](0017-onnx-embeddings.md) | ONNX Runtime for visual embeddings (no AVX required) | Accepted |
| 0018 | Privileged mount helper for UI-managed storage | TODO |
