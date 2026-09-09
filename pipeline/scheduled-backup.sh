#!/usr/bin/env bash
# Menhir scheduled backup wrapper.
#
# WHY THIS EXISTS
# ---------------
# deploy/backup-generation.sh stops the whole menhir-prod stack, takes an
# offline neo4j-admin dump, verifies it by loading both dumps back, encrypts,
# and only then restarts. Neo4j Community has no online backup, so the outage is
# unavoidable.
#
# Critically, that script has NO EXIT TRAP: every failure path exits with the
# stack left stopped, on purpose, so an operator inspects before it serves again.
# That is right for an attended run and wrong for an unattended one -- a 04:00
# failure would leave memory.ctharvey.me down until somebody noticed.
#
# This wrapper makes unattended runs safe without weakening the attended path:
#
#   1. it never runs concurrently with another mutation (same host lock);
#   2. if the stack was running when we started, it is running when we finish,
#      whatever happened in between;
#   3. a failure is recorded durably where backup-status will surface it, rather
#      than only in a log nobody reads.
#
# Auto-restart is safe HERE specifically because a backup does not modify live
# data. It reads the data directory and writes elsewhere. A failed backup means
# "no new backup", never "the database is now suspect", so bringing the stack
# back does not risk serving corrupt state. Do not copy this wrapper for restore
# or deploy operations, where fail-stopped is the correct behaviour.
set -uo pipefail

MENHIR_PROD_ROOT="${MENHIR_PROD_ROOT:-/srv/menhir/production}"
COMPOSE_FILE="${MENHIR_COMPOSE_FILE:-${MENHIR_PROD_ROOT}/deploy/docker-compose.production.yml}"
BACKUP_SCRIPT="${MENHIR_BACKUP_SCRIPT:-${MENHIR_PROD_ROOT}/deploy/backup-generation.sh}"
STATUS_DIR="${MENHIR_STATUS_DIR:-/var/lib/menhir-production}"
LOCK="${MENHIR_MAINTENANCE_LOCK:-/run/lock/menhir-production.lock}"
FAILURE_MARKER="${STATUS_DIR}/scheduled-backup-failure.json"
LAST_RUN="${STATUS_DIR}/scheduled-backup-last-run.json"

log() { printf '%s scheduled-backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

write_json() { # path json
    local tmp
    tmp="$(mktemp "${1}.tmp.XXXXXX")" || return 1
    printf '%s\n' "$2" >"$tmp"
    chmod 0400 "$tmp"
    mv -f "$tmp" "$1"
    sync -f "$(dirname "$1")" 2>/dev/null || true
}

stack_running() {
    docker compose -f "$COMPOSE_FILE" ps --status running --quiet 2>/dev/null | grep -q .
}

[ -r "$COMPOSE_FILE" ]   || { log "FATAL compose file unreadable: $COMPOSE_FILE"; exit 1; }
[ -x "$BACKUP_SCRIPT" ]  || { log "FATAL backup script not executable: $BACKUP_SCRIPT"; exit 1; }
[ -d "$STATUS_DIR" ]     || { log "FATAL status dir absent: $STATUS_DIR"; exit 1; }

# One mutation at a time. Non-blocking: if a deploy or another backup holds the
# lock, this run is skipped rather than queued. A skipped nightly backup is a
# non-event; a queued one that fires mid-deploy is not.
exec 9>"$LOCK" || { log "FATAL cannot open lock $LOCK"; exit 1; }
if ! flock -n 9; then
    log "SKIP another operation holds $LOCK"
    exit 0
fi

was_running="stopped"
stack_running && was_running="running"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
start_epoch="$(date +%s)"
log "starting (stack was ${was_running})"

rc=0
"$BACKUP_SCRIPT" || rc=$?

elapsed=$(( $(date +%s) - start_epoch ))
finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# The guarantee: restore the pre-run service state no matter how we got here.
restart_note="not-needed"
if [ "$was_running" = "running" ] && ! stack_running; then
    log "stack is down after backup (rc=${rc}); restarting"
    if docker compose -f "$COMPOSE_FILE" up -d >/dev/null 2>&1; then
        restart_note="restarted"
        log "stack restarted"
    else
        restart_note="restart-failed"
        log "FATAL could not restart the stack; manual recovery required"
    fi
fi

if [ "$rc" -eq 0 ] && [ "$restart_note" != "restart-failed" ]; then
    rm -f "$FAILURE_MARKER"
    log "success in ${elapsed}s (downtime is approximately this long)"
else
    write_json "$FAILURE_MARKER" "$(printf '{"schema":1,"kind":"scheduled-backup-failure","exit_code":%d,"restart":"%s","started_utc":"%s","finished_utc":"%s","elapsed_seconds":%d}' \
        "$rc" "$restart_note" "$started" "$finished" "$elapsed")"
    log "FAILED rc=${rc} restart=${restart_note} after ${elapsed}s"
fi

write_json "$LAST_RUN" "$(printf '{"schema":1,"kind":"scheduled-backup-last-run","exit_code":%d,"restart":"%s","stack_was":"%s","started_utc":"%s","finished_utc":"%s","elapsed_seconds":%d}' \
    "$rc" "$restart_note" "$was_running" "$started" "$finished" "$elapsed")"

exit "$rc"
