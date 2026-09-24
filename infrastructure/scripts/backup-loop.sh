#!/usr/bin/env bash
# Nightly catalogue backup, run by the `backup` compose service.
#
# The install went eight weeks without one: `manage.sh backup` existed and
# worked, and nothing ever ran it. This takes a backup whenever the newest
# archive is more than a day old — so the first lands the moment the service
# starts — keeps the newest FRAMEFOUND_BACKUP_KEEP, and writes the same
# archive `manage.sh backup` does, so `manage.sh verify` and `restore` read it.
#
# Archives go to the data disk, not the disk the database lives on, so one
# failed drive cannot take both. They are still on the same machine: copying
# them somewhere else is the operator's job (docs/deployment).
set -uo pipefail
umask 077 # archives carry .env, and with it every sealed-secret key

DEST=/data/backups
KEEP=${FRAMEFOUND_BACKUP_KEEP:-14}
MAX_AGE_S=$((24 * 3600))
export PGHOST=${PGHOST:-postgres}
export PGUSER=${POSTGRES_USER:?POSTGRES_USER missing from .env}
export PGPASSWORD=${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing from .env}
export PGDATABASE=${POSTGRES_DB:?POSTGRES_DB missing from .env}

log() { printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

newest_age() {
  local newest
  newest=$(ls -1t "$DEST"/framefound-*.tar.gz 2>/dev/null | head -1)
  if [ -z "$newest" ]; then
    echo 999999999
  else
    echo $(($(date +%s) - $(stat -c %Y "$newest")))
  fi
}

backup_once() {
  local stamp work archive schema
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  work=$(mktemp -d)
  if ! pg_dump --format=custom --file="$work/catalog.dump"; then
    log "pg_dump failed; trying again in an hour"
    rm -rf "$work"
    return 1
  fi
  cp /config/env "$work/env" 2>/dev/null || log "no .env mounted; archive holds the database only"
  schema=$(psql -tA -c "SELECT version_num FROM alembic_version" 2>/dev/null || echo unknown)
  printf '%s\n' "$schema" >"$work/schema_version"
  cat >"$work/manifest.json" <<EOF
{
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "app_version": "backup-service",
  "schema_version": "$schema",
  "components": ["catalog_database", "configuration"],
  "excluded": ["thumbnails", "proxies", "transcript_sidecars", "model_weights"],
  "note": "Automatic nightly backup. Derived media is rebuildable from originals; originals are never in backups."
}
EOF
  (cd "$work" && sha256sum catalog.dump manifest.json >checksums.sha256)
  archive="$DEST/framefound-$stamp.tar.gz"
  # Written under another name and renamed, so nothing — the health check
  # least of all — ever mistakes a half-written archive for a backup.
  if tar -czf "$archive.partial" -C "$work" . && mv "$archive.partial" "$archive"; then
    log "wrote $archive ($(du -h "$archive" | cut -f1))"
  else
    rm -f "$archive.partial"
    log "could not write the archive"
  fi
  rm -rf "$work"
}

prune() {
  ls -1t "$DEST"/framefound-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f --
}

mkdir -p "$DEST"
log "started; keeping the newest $KEEP archives in $DEST"
while true; do
  if [ "$(newest_age)" -ge "$MAX_AGE_S" ]; then
    backup_once && prune
  fi
  sleep 3600
done
