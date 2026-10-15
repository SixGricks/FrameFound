# Contributing to FrameFound

Thanks for your interest! This project is in early development (Milestone 0-1);
expect churn. Please open an issue before large PRs.

## Development setup

Prerequisites: Docker + Docker Compose, Python 3.12+, Node 20+.

```bash
git clone https://github.com/SixGricks/FrameFound.git
cd FrameFound
cp .env.example .env

# Backend
cd apps/server
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest

# Frontend
cd ../web
npm install
npm run dev
```

Full stack: `docker compose up -d --build`.

### Looking at the UI with a seeded catalogue

No Postgres, Redis or NAS needed: the API runs on SQLite and the Next dev
server forwards `/api` to it.

```bash
# API on SQLite, in apps/server (media and data dirs can be anywhere)
export FRAMEFOUND_DATABASE_URL=sqlite+aiosqlite:////tmp/ff-ui/ui.db
export FRAMEFOUND_DATA_DIR=/tmp/ff-ui/data FRAMEFOUND_MEDIA_ROOT=/tmp/ff-ui/media
export FRAMEFOUND_SECRET_KEY=$(openssl rand -hex 32) FRAMEFOUND_SETUP_TOKEN=dev-setup
pip install -e ".[dev,db,media]"
alembic upgrade head
python ../../infrastructure/scripts/seed_dev.py /tmp/ff-ui   # 66 generated assets, faces, tags, 2 listings
uvicorn framefound.main:app --port 8000

# Web, in apps/web — .env.local is git-ignored
echo FRAMEFOUND_API_PROXY=http://localhost:8000 > .env.local
npm run dev
```

Create the first admin once with `POST /api/v1/auth/setup`
(`{"setup_token": "dev-setup", "email": ..., "password": ...}`), then sign in
at http://localhost:3000. Visual search, auto-edit and Drive need the real
models and keys and stay off; everything else works on the seeded data.

## Standards

- **Python**: type hints everywhere, `ruff` for lint+format, `mypy` clean, pytest for tests.
- **TypeScript**: strict mode, ESLint + Prettier.
- **Commits**: [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`...).
- **Branches**: `main` is always releasable; work on feature branches; PRs required.
- **Migrations**: every schema change ships as an Alembic migration, committed with the code.
- **Security**: never commit secrets, real paths, domains, or tokens. `.env` is gitignored.
- **Architecture changes**: significant decisions need an ADR in `docs/adr/` (see template).

## Non-negotiables (see docs/architecture.md)

1. Original media is never modified — read-only mounts, derived files only.
2. Local-first — no required cloud services.
3. All paths validated against approved library roots; never trust filenames or metadata.
4. Authorization enforced before serving any proxy, thumbnail, or transcript.

## Pull requests

1. Fork/branch, make the change, add tests.
2. `ruff check && mypy && pytest` (backend) or `npm run lint && npm test` (frontend) pass locally.
3. Fill in the PR template; link the issue.
4. CI must be green; one maintainer review required.
