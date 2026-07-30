#!/usr/bin/env bash
# FrameFound guided installer (Milestone 0 skeleton — expanded in M1).
# Verifies prerequisites, generates secrets, and starts the stack.
set -euo pipefail

cd "$(dirname "$0")/../.."

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

say "Checking prerequisites"
command -v docker >/dev/null 2>&1 || fail "Docker is not installed. See https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required (docker compose)."
command -v openssl >/dev/null 2>&1 || fail "openssl is required to generate secrets."

ARCH=$(uname -m)
say "CPU architecture: $ARCH"
if command -v nvidia-smi >/dev/null 2>&1; then
  say "NVIDIA GPU detected: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
  docker info 2>/dev/null | grep -qi nvidia \
    && say "NVIDIA Container Toolkit: OK" \
    || say "NOTE: NVIDIA Container Toolkit not detected; GPU acceleration unavailable until installed."
else
  say "No NVIDIA GPU detected; CPU processing will be used."
fi

if [ ! -f .env ]; then
  say "Creating .env from .env.example with generated secrets"
  cp .env.example .env
  # Portable in-place replacement (GNU/BSD sed differences avoided via tmp file).
  gen() { openssl rand -hex 32; }
  SETUP_TOKEN=$(openssl rand -hex 16)
  awk -v sk="$(gen)" -v pg="$(gen)" -v st="$SETUP_TOKEN" '
    sub(/^FRAMEFOUND_SECRET_KEY=.*/,   "FRAMEFOUND_SECRET_KEY=" sk)   {}
    sub(/^POSTGRES_PASSWORD=.*/,     "POSTGRES_PASSWORD=" pg)     {}
    sub(/^FRAMEFOUND_SETUP_TOKEN=.*/,  "FRAMEFOUND_SETUP_TOKEN=" st)  {}
    { print }' .env > .env.tmp && mv .env.tmp .env
  chmod 600 .env
else
  say ".env already exists — keeping it"
  SETUP_TOKEN=$(grep '^FRAMEFOUND_SETUP_TOKEN=' .env | cut -d= -f2-)
fi

# TODO(m1): prompt for FRAMEFOUND_DATA_DIR and first media mount; run migrations;
# TODO(m1): wait for health checks and roll back on failure.

say "Starting FrameFound"
docker compose up -d

PORT=$(grep '^FRAMEFOUND_HTTP_PORT=' .env | cut -d= -f2- || true)
say "FrameFound is starting at http://localhost:${PORT:-8080}"
say "First-run setup token: ${SETUP_TOKEN}  (expires after first use)"
say "Remote access options: docs/remote-access.md (Tailscale recommended)"
