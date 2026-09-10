#!/usr/bin/env bash
# Menhir scheduled backup wrapper.
#
# WHY THIS EXISTS
# ---------------
# bin/backup-generation.sh stops the whole menhir-prod stack, takes an offline
# neo4j-admin dump, verifies it by loading both dumps back, encrypts, and only
# then restarts. Neo4j Community has no online backup, so the outage is
# unavoidable.
#
# Critically, that script has NO EXIT TRAP: every failure path exits with the
# stack left stopped, on purpose, so an operator inspects before it serves again.
# That is right for an attended run and wrong for an unattended one -- a 04:00
# failure would leave memory.ctharvey.me down until somebody noticed.
#
# This wrapper makes unattended runs safe without weakening the attended path:
#
#   1. it never runs concurrently with another SCHEDULED run (its own lock);
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
#
# LOCKING
# -------
# This wrapper does NOT take /run/lock/menhir-production.lock. backup-generation.sh
# takes that lock itself, non-blocking, and exits 1 if another mutation holds it.
# An earlier version of this file took it first, which meant the child could
# never acquire it and every scheduled run would have failed with "maintenance
# lock is held". The wrapper's own lock exists only to stop two SCHEDULED runs
# overlapping; host-wide serialization stays where it already was.
set -uo pipefail

MENHIR_PROD_ROOT="${MENHIR_PROD_ROOT:-/srv/menhir/production}"
BIN_DIR="${MENHIR_BIN_DIR:-${MENHIR_PROD_ROOT}/bin}"
# The managed, release-verified implementation. NOT ${MENHIR_PROD_ROOT}/deploy/,
# which is an unmanaged shadow copy outside verify-artifacts coverage.
BACKUP_SCRIPT="${MENHIR_BACKUP_SCRIPT:-${BIN_DIR}/backup-generation.sh}"
RELEASE_LIB="${MENHIR_RELEASE_LIB:-${BIN_DIR}/release-lib.sh}"
COMPOSE_PROJECT="${MENHIR_COMPOSE_PROJECT:-menhir-prod}"
STATUS_DIR="${MENHIR_STATUS_DIR:-/var/lib/menhir-production}"
LOCK="${MENHIR_SCHEDULED_BACKUP_LOCK:-/run/lock/menhir-scheduled-backup.lock}"
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

# Ask the daemon which containers carry the compose project label, rather than
# `docker compose -f <file> ps`. The compose file is variable-interpolated and
# unparseable without --env-file (production.env is root-only, mode 0400), so a
# bare `-f` invocation fails outright -- with stderr discarded that read as
# "stopped", which silently disabled the restart guarantee below.
#
# Exit: 0 running, 1 not running, 2 could not determine.
stack_running() {
    local out
    out="$(docker ps --quiet --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}" 2>&1)" \
        || { log "WARN cannot query docker: ${out%%$'\n'*}"; return 2; }
    [ -n "$out" ]
}

describe_stack_state() {
    stack_running
    case $? in
        0) printf 'running' ;;
        1) printf 'stopped' ;;
        *) printf 'unknown' ;;
    esac
}

# Restart via the same code path production uses, not a hand-rolled
# `docker compose up -d`: production_up supplies the eight MENHIR_* runtime
# variables plus --env-file and --project-name, then waits for both containers
# to report healthy. Sourced in a subshell so its `set -euo pipefail` cannot
# leak into our own error handling.
restart_stack() {
    ( . "$RELEASE_LIB" && production_up ) >/dev/null 2>&1
}

[ -x "$BACKUP_SCRIPT" ] || { log "FATAL backup script not executable: $BACKUP_SCRIPT"; exit 1; }
[ -r "$RELEASE_LIB" ]   || { log "FATAL release library unreadable: $RELEASE_LIB"; exit 1; }
[ -d "$STATUS_DIR" ]    || { log "FATAL status dir absent: $STATUS_DIR"; exit 1; }

# Non-blocking: if a previous scheduled run is somehow still going, this one is
# skipped rather than queued. A skipped nightly backup is a non-event.
exec 9>"$LOCK" || { log "FATAL cannot open lock $LOCK"; exit 1; }
if ! flock -n 9; then
    log "SKIP another scheduled backup holds $LOCK"
    exit 0
fi

was_running="$(describe_stack_state)"
if [ "$was_running" = "unknown" ]; then
    log "FATAL cannot determine stack state before starting; refusing to run"
    exit 1
fi

started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
start_epoch="$(date +%s)"
log "starting (stack was ${was_running})"

# backup-generation.sh hands the finished generation to
# /usr/local/sbin/menhir-backup-local, which refuses to encrypt without a job id
# matching ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$. Normally submit_op mints that id
# and the worker exports it (lib.sh worker line 79). Bypassing submit_op means
# nothing supplies it, and the run dies AFTER the stack has been stopped, dumped
# and verified -- the most expensive possible place to fail. Mint one here in
# the same shape submit_op uses.
job_id="${MENHIR_OPERATION_JOB_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$((RANDOM % 10000))}"
[[ "$job_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] \
    || { log "FATAL generated job id is invalid: ${job_id}"; exit 1; }
log "operation job id ${job_id}"

rc=0
MENHIR_OPERATION_JOB_ID="$job_id" "$BACKUP_SCRIPT" || rc=$?

elapsed=$(( $(date +%s) - start_epoch ))
finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# The guarantee: restore the pre-run service state no matter how we got here.
# If the post-run state cannot be determined, attempt the restart anyway --
# production_up is idempotent, so a needless call is harmless where a skipped
# one leaves the endpoint down.
restart_note="not-needed"
if [ "$was_running" = "running" ]; then
    post_state="$(describe_stack_state)"
    if [ "$post_state" != "running" ]; then
        log "stack is ${post_state} after backup (rc=${rc}); restarting"
        if restart_stack; then
            restart_note="restarted"
            log "stack restarted"
        else
            restart_note="restart-failed"
            log "FATAL could not restart the stack; manual recovery required"
        fi
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
