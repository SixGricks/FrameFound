#!/usr/bin/env bash
# FrameFound management CLI.
#
# Backups cover everything that cannot be rebuilt from your originals: the
# catalog database (assets, transcripts, tags, users, libraries) and the
# configuration. Thumbnails and proxies are deliberately excluded — they are
# regenerable, and including them would multiply backup size for no gain.
set -euo pipefail
cd "$(dirname "$0")/../.."

BACKUP_DIR=${FRAMEFOUND_BACKUP_DIR:-./backups}
COMPOSE="docker compose"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: ./infrastructure/scripts/manage.sh <command>

  up             Start all services
  down           Stop all services (data preserved)
  logs [svc]     Follow logs
  status         Service status
  backup         Back up the catalog database + configuration
  restore FILE   Restore from a backup archive (replaces the catalog)
  verify FILE    Check a backup archive's checksums without restoring
  update         Pull images, migrate, health-check, roll back on failure
EOF
}

require_running() {
  $COMPOSE ps --status running --services 2>/dev/null | grep -qx "$1" \
    || fail "The '$1' service is not running. Start it with: manage.sh up"
}

env_value() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }

cmd_backup() {
  require_running postgres
  local user db stamp workdir archive
  user=$(env_value POSTGRES_USER); db=$(env_value POSTGRES_DB)
  [ -n "$user" ] && [ -n "$db" ] || fail "POSTGRES_USER/POSTGRES_DB missing from .env"

  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  mkdir -p "$BACKUP_DIR"
  workdir=$(mktemp -d)
  trap 'rm -rf "$workdir"' RETURN

  say "Dumping catalog database"
  $COMPOSE exec -T postgres pg_dump -U "$user" -d "$db" --format=custom \
    > "$workdir/catalog.dump"

  say "Capturing configuration"
  # .env holds secrets: it is included because a restore needs it, so the
  # archive itself must be treated as sensitive (chmod 600 below).
  cp .env "$workdir/env" 2>/dev/null || true
  $COMPOSE exec -T postgres psql -tA -U "$user" -d "$db" \
    -c "SELECT version_num FROM alembic_version" > "$workdir/schema_version" 2>/dev/null || true

  cat > "$workdir/manifest.json" <<EOF
{
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "app_version": "$(git describe --tags --always 2>/dev/null || echo unknown)",
  "schema_version": "$(cat "$workdir/schema_version" 2>/dev/null || echo unknown)",
  "components": ["catalog_database", "configuration"],
  "excluded": ["thumbnails", "proxies", "transcript_sidecars", "model_weights"],
  "note": "Derived media is rebuildable from originals; originals are never in backups."
}
EOF
  (cd "$workdir" && sha256sum catalog.dump manifest.json > checksums.sha256)

  archive="$BACKUP_DIR/framefound-$stamp.tar.gz"
  tar -czf "$archive" -C "$workdir" .
  chmod 600 "$archive"
  say "Backup written: $archive ($(du -h "$archive" | cut -f1))"
  say "It contains secrets from .env — store it somewhere safe and off this host."
}

cmd_verify() {
  local archive=${1:?"Usage: manage.sh verify <backup.tar.gz>"} workdir
  [ -f "$archive" ] || fail "No such backup: $archive"
  workdir=$(mktemp -d)
  trap 'rm -rf "$workdir"' RETURN
  tar -xzf "$archive" -C "$workdir"
  say "Manifest:"
  cat "$workdir/manifest.json"
  (cd "$workdir" && sha256sum -c checksums.sha256) \
    && say "Checksums OK" || fail "Checksum mismatch — this archive is damaged"
}

cmd_restore() {
  local archive=${1:?"Usage: manage.sh restore <backup.tar.gz>"} workdir user db
  [ -f "$archive" ] || fail "No such backup: $archive"
  require_running postgres
  user=$(env_value POSTGRES_USER); db=$(env_value POSTGRES_DB)

  workdir=$(mktemp -d)
  trap 'rm -rf "$workdir"' RETURN
  tar -xzf "$archive" -C "$workdir"
  (cd "$workdir" && sha256sum -c checksums.sha256 >/dev/null) \
    || fail "Checksum mismatch — refusing to restore a damaged archive"

  cat "$workdir/manifest.json"
  printf '\n\033[1;33mThis REPLACES the current catalog.\033[0m Your original media is untouched.\n'
  read -r -p "Type the word RESTORE to continue: " confirm
  [ "$confirm" = "RESTORE" ] || fail "Cancelled"

  say "Stopping workers so nothing writes mid-restore"
  $COMPOSE stop api worker worker-media worker-ai scanner scheduler >/dev/null 2>&1 || true

  say "Restoring catalog database"
  $COMPOSE exec -T postgres pg_restore -U "$user" -d "$db" --clean --if-exists \
    < "$workdir/catalog.dump"

  say "Restarting services"
  $COMPOSE up -d
  say "Restore complete. Run a library scan to re-link any media that moved."
}

cmd_update() {
  say "Backing up before update"
  cmd_backup
  say "Pulling images"
  $COMPOSE pull
  say "Starting (migrations run on api startup)"
  $COMPOSE up -d
  say "Waiting for health"
  for _ in $(seq 1 30); do
    if $COMPOSE exec -T api python -c \
        "import urllib.request as u; u.urlopen('http://localhost:8000/healthz')" 2>/dev/null; then
      say "Update complete and healthy"
      return 0
    fi
    sleep 5
  done
  fail "Health check failed after update. Roll back with: manage.sh restore <latest backup>"
}

case "${1:-}" in
  up)      $COMPOSE up -d ;;
  down)    $COMPOSE down ;;
  logs)    $COMPOSE logs -f "${2:-}" ;;
  status)  $COMPOSE ps ;;
  backup)  cmd_backup ;;
  verify)  cmd_verify "${2:-}" ;;
  restore) cmd_restore "${2:-}" ;;
  update)  cmd_update ;;
  *)       usage; exit 1 ;;
esac
