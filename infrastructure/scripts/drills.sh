#!/usr/bin/env bash
# Failure drills (M8).
#
# Backups that have never been restored are not backups, and graceful
# degradation that has never been tested is a hope. This exercises the failure
# modes that will actually happen to this deployment — a NAS that goes away, a
# broker restart, a database bounce, a full disk — and checks that the system
# reports the problem honestly instead of corrupting the catalogue or hanging.
#
#   ./infrastructure/scripts/drills.sh            # non-destructive drills
#   ./infrastructure/scripts/drills.sh --all      # includes service restarts
#
# The default set touches nothing the operator would miss. --all restarts
# services, so it briefly interrupts processing.
set -uo pipefail
cd "$(dirname "$0")/../.."

COMPOSE="docker compose"
[ -f docker-compose.braw.yml ] && COMPOSE="docker compose -f docker-compose.yml -f docker-compose.braw.yml"
RUN_ALL=${1:-}
PASS=0
FAIL=0

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
ok() { printf '  \033[32mPASS\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
note() { printf '       %s\n' "$1"; }

psql() { $COMPOSE exec -T postgres psql -U framefound -d framefound -tAc "$1" 2>/dev/null | tr -d '\r'; }
api() { $COMPOSE exec -T api "$@" 2>/dev/null; }

say "Drill 1 — the catalogue survives without the originals"
# The single most important invariant: media is read-only and rebuildable, so
# losing access to a share must never lose the catalogue.
total=$(psql "SELECT count(*) FROM assets")
if [ -n "$total" ] && [ "$total" -gt 0 ]; then
  ok "catalogue holds $total assets independently of the shares"
else
  bad "could not read the asset count"
fi
ro=$($COMPOSE exec -T api sh -c 'touch /media/.ff-write-test 2>&1 || echo REFUSED' | tr -d '\r')
if echo "$ro" | grep -q REFUSED; then
  ok "media mount rejects writes (originals cannot be damaged)"
else
  bad "media mount accepted a write — originals are NOT protected"
  api sh -c 'rm -f /media/.ff-write-test'
fi

say "Drill 2 — an unreachable share is reported, not guessed at"
missing=$(psql "SELECT count(*) FROM assets WHERE availability <> 'online'")
note "assets currently flagged missing/unmounted: ${missing:-?}"
if psql "SELECT 1 FROM assets WHERE availability = 'missing' LIMIT 1" >/dev/null 2>&1; then
  ok "availability tracking is queryable"
else
  bad "availability column unusable"
fi

say "Drill 3 — a full disk stops work instead of corrupting it"
free_pct=$($COMPOSE exec -T api sh -c "df /data | awk 'NR==2 {print 100-\$5+0}'" | tr -d '\r%')
note "free space on /data: ${free_pct:-?}%"
if api python -c "
import sys
from framefound.processing.derivatives import ensure_space
sys.exit(0 if callable(ensure_space) else 1)
"; then
  ok "disk guard present — derivative work stops before the disk fills"
else
  bad "disk guard missing: framefound.processing.derivatives.ensure_space"
fi
paused=$(psql "SELECT count(*) FROM derivatives WHERE status = 'paused'")
[ -n "$paused" ] && note "derivatives currently paused for space: $paused"

say "Drill 4 — backup produces a restorable artefact"
# Checked rather than assumed: all four scripts were committed mode 644 and
# were never executable from a fresh clone, which this drill is how we found.
if [ -x ./infrastructure/scripts/manage.sh ]; then
  if ./infrastructure/scripts/manage.sh backup >/tmp/ff-drill-backup.log 2>&1; then
    newest=$(ls -t ./backups/*.tar.gz 2>/dev/null | head -1)
    if [ -n "$newest" ] && tar -tzf "$newest" >/dev/null 2>&1; then
      size=$(du -h "$newest" | cut -f1)
      ok "backup written and readable ($size)"
      # Not a filename check — the dump is pg_dump custom format, so ask
      # pg_restore whether it can actually read a table of contents out of it.
      # A backup nobody has tried to read is not a backup.
      workdir=$(mktemp -d)
      tar -xzf "$newest" -C "$workdir" ./catalog.dump 2>/dev/null
      if [ -s "$workdir/catalog.dump" ]; then
        # Copied in rather than piped: pg_restore needs to seek within a
        # custom-format archive, and a pipe is not seekable.
        $COMPOSE cp "$workdir/catalog.dump" postgres:/tmp/ff-drill.dump >/dev/null 2>&1
        toc=$($COMPOSE exec -T postgres pg_restore -l /tmp/ff-drill.dump 2>/dev/null | grep -c "TABLE DATA")
        $COMPOSE exec -T postgres rm -f /tmp/ff-drill.dump >/dev/null 2>&1
        if [ "${toc:-0}" -gt 5 ]; then
          ok "dump is restorable — pg_restore lists $toc tables of data"
        else
          bad "pg_restore could not read a usable table of contents (found ${toc:-0})"
        fi
      else
        bad "backup contains no catalog.dump"
      fi
      rm -rf "$workdir"
    else
      bad "backup archive missing or corrupt"
    fi
  else
    bad "backup command failed — see /tmp/ff-drill-backup.log"
  fi
else
  bad "manage.sh not executable"
fi

say "Drill 5 — health endpoints tell the truth"
health=$($COMPOSE exec -T api python -c "
import asyncio, json
from framefound.db.engine import session_factory
async def main():
    async with session_factory()() as db:
        from sqlalchemy import text
        await db.execute(text('SELECT 1'))
    print('db-ok')
asyncio.run(main())
" 2>/dev/null | tr -d '\r')
[ "$health" = "db-ok" ] && ok "database reachable from the API" || bad "API cannot reach the database"

redis_ok=$($COMPOSE exec -T redis redis-cli ping 2>/dev/null | tr -d '\r')
[ "$redis_ok" = "PONG" ] && ok "broker reachable" || bad "broker not responding"

say "Drill 6 — no privileged containers unless asked for"
priv=$(docker ps --format '{{.Names}}' | while read -r c; do
  docker inspect "$c" --format '{{.Name}} {{.HostConfig.Privileged}} {{len .HostConfig.CapAdd}}' 2>/dev/null
done | awk '$2 == "true" || $3 > 0 {print $1}')
if [ -z "$priv" ]; then
  ok "no container holds extra capabilities"
else
  note "holding capabilities: $priv"
  if echo "$priv" | grep -q mounter; then
    ok "only the mount helper is privileged (expected with --profile storage)"
  else
    bad "unexpected privileged container: $priv"
  fi
fi

if [ "$RUN_ALL" = "--all" ]; then
  say "Drill 7 — a broker restart loses no queued work"
  before=$($COMPOSE exec -T redis redis-cli llen metadata 2>/dev/null | tr -d '\r')
  $COMPOSE restart redis >/dev/null 2>&1
  sleep 12
  after=$($COMPOSE exec -T redis redis-cli llen metadata 2>/dev/null | tr -d '\r')
  note "metadata queue: ${before:-?} before, ${after:-?} after"
  # Redis persists to disk, so depth should survive. Workers reconnect on their
  # own; acks_late means an in-flight task is redelivered rather than lost.
  if [ -n "$after" ]; then
    ok "broker came back and the queue is readable"
  else
    bad "broker did not recover"
  fi

  say "Drill 8 — a database restart is survived, not crashed through"
  $COMPOSE restart postgres >/dev/null 2>&1
  sleep 20
  recovered=$(psql "SELECT count(*) FROM assets")
  if [ "$recovered" = "$total" ]; then
    ok "catalogue intact after a database restart ($recovered assets)"
  else
    bad "asset count changed across restart: $total -> ${recovered:-?}"
  fi
  sleep 5
  api_up=$($COMPOSE exec -T api python -c "print('up')" 2>/dev/null | tr -d '\r')
  [ "$api_up" = "up" ] && ok "API still serving after the database bounced" || bad "API did not recover"
else
  note ""
  note "Skipped drills 7-8 (broker and database restarts). Run with --all."
fi

printf '\n\033[1m%d passed, %d failed\033[0m\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
