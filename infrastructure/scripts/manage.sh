#!/usr/bin/env bash
# MediaHub management CLI (skeleton — backup/restore/update implemented in M8).
set -euo pipefail
cd "$(dirname "$0")/../.."

usage() {
  cat <<'EOF'
Usage: ./infrastructure/scripts/manage.sh <command>

  up            Start all services
  down          Stop all services (data preserved)
  logs [svc]    Follow logs
  status        Service status
  backup        Back up database + configuration       (TODO m8)
  restore FILE  Restore from a backup file             (TODO m8)
  update        Safe update: backup, pull, migrate,
                health-check, roll back on failure     (TODO m8)
EOF
}

case "${1:-}" in
  up)     docker compose up -d ;;
  down)   docker compose down ;;
  logs)   docker compose logs -f "${2:-}" ;;
  status) docker compose ps ;;
  backup|restore|update)
    echo "'$1' is not implemented yet (Milestone 8). Tracked in docs/roadmap.md." >&2
    exit 1 ;;
  *) usage; exit 1 ;;
esac
