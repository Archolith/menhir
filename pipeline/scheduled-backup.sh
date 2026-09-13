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
# failure would leave memory.ctharvey.me down until somebody noticed. Observed
# for real on 2026-09-10: the encryption step refused a missing job id and the
# stack stayed stopped until this wrapper restarted it.
#
# This wrapper makes unattended runs safe without weakening the attended path:
#
#   1. it never runs concurrently with another SCHEDULED run (its own lock);
#   2. if the stack was running when we started, it is running when we finish,
#      on EVERY exit path including SIGTERM (see the trap below);
#   3. every outcome, including an early abort, leaves a durable record;
#   4. once production is back, the new generation is staged and REHEARSED
#      (restore-generation.sh into scratch), so every nightly backup is proven
#      restorable. stage-generation.sh only accepts a backup receipt younger
#      than one hour, so nobody can do this later: either the wrapper does it
#      inside the window or the generation is never rehearsed, and the scaffold
#      refuses its next install at seed-drill. The rehearsal's plaintext copies
#      and its restore-selection marker are removed on success -- they are this
#      wrapper's scratch, not an operator's intent. The rehearsal is skipped,
#      and reported as such, while a maintenance holds the admission lock.
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
# An earlier version took it first, which meant the child could never acquire it
# and every scheduled run would have failed with "maintenance lock is held". The
# wrapper's own lock exists only to stop two SCHEDULED runs overlapping.
#
# INSTALL
# -------
#   install -o root -g root -m 0755 scheduled-backup.sh \
#       /usr/local/sbin/menhir-scheduled-backup
# menhir-backup.service ExecStart must match that path. Do not point it at a
# directory that no release creates.
set -uo pipefail

MENHIR_PROD_ROOT="${MENHIR_PROD_ROOT:-/srv/menhir/production}"
BIN_DIR="${MENHIR_BIN_DIR:-${MENHIR_PROD_ROOT}/bin}"
# The managed, release-verified implementation. NOT ${MENHIR_PROD_ROOT}/deploy/,
# which is an unmanaged shadow copy outside verify-artifacts coverage.
BACKUP_SCRIPT="${MENHIR_BACKUP_SCRIPT:-${BIN_DIR}/backup-generation.sh}"
RELEASE_LIB="${MENHIR_RELEASE_LIB:-${BIN_DIR}/release-lib.sh}"
STAGE_SCRIPT="${MENHIR_STAGE_SCRIPT:-${BIN_DIR}/stage-generation.sh}"
RESTORE_SCRIPT="${MENHIR_RESTORE_SCRIPT:-${BIN_DIR}/restore-generation.sh}"
BACKUP_ROOT="${MENHIR_BACKUP_ROOT:-/srv/menhir/backups}"
ADMISSION_LOCK="${MENHIR_ADMISSION_LOCK:-/run/lock/menhir-production-admission.lock}"
COMPOSE_PROJECT="${MENHIR_COMPOSE_PROJECT:-menhir-prod}"
APP_CONTAINER="${MENHIR_APP_CONTAINER:-menhir-prod-app}"
NEO4J_CONTAINER="${MENHIR_NEO4J_CONTAINER:-menhir-prod-neo4j}"
STATUS_DIR="${MENHIR_STATUS_DIR:-/var/lib/menhir-production}"
LOCK="${MENHIR_SCHEDULED_BACKUP_LOCK:-/run/lock/menhir-scheduled-backup.lock}"
FAILURE_MARKER="${STATUS_DIR}/scheduled-backup-failure.json"
LAST_RUN="${STATUS_DIR}/scheduled-backup-last-run.json"
REHEARSAL_RECEIPT="${STATUS_DIR}/rehearsal-receipt.json"
SELECTION="${STATUS_DIR}/restore-selection"

log() { printf '%s scheduled-backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

write_json() { # path json
    local tmp
    tmp="$(mktemp "${1}.tmp.XXXXXX")" || return 1
    printf '%s\n' "$2" >"$tmp"
    chmod 0400 "$tmp"
    mv -f "$tmp" "$1"
    sync -f "$(dirname "$1")" 2>/dev/null || true
}

# Run state, maintained so the EXIT trap can record and repair from anywhere.
was_running="not-started"
started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
start_epoch="$(date +%s)"
rc=0
restart_note="not-needed"
rehearsal_note="not-run"
generation="-"
downtime_seconds=0
job_id="-"
finalized=0

# Ask the daemon which containers carry the compose project label, rather than
# `docker compose -f <file> ps`. The compose file is variable-interpolated and
# unparseable without --env-file (production.env is root-only, mode 0400), so a
# bare `-f` invocation fails outright -- with stderr discarded that read as
# "stopped", which silently disabled the restart guarantee.
#
# Both containers must be present. An any-of test reports "running" when only
# neo4j came back and the app is down, which is a half-outage that every signal
# would then call healthy.
#
# Exit: 0 both up, 1 not both up, 2 could not determine.
stack_running() {
    local out
    out="$(docker ps --format '{{.Names}}' \
             --filter "label=com.docker.compose.project=${COMPOSE_PROJECT}" 2>&1)" \
        || { log "WARN cannot query docker: ${out%%$'\n'*}"; return 2; }
    grep -qx -- "$APP_CONTAINER" <<<"$out" || return 1
    grep -qx -- "$NEO4J_CONTAINER" <<<"$out" || return 1
    return 0
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
# to report healthy. Sourced in a SUBSHELL -- release-lib.sh assigns LOCK and
# STATUS_DIR at top level, and sourcing it directly would silently rebind this
# wrapper's own LOCK. Its `set -euo pipefail` is likewise contained.
# stderr is deliberately NOT discarded: this is the one path that ends in
# "manual recovery required", and wait_healthy's timeout message is the only
# diagnostic an operator gets at 04:00.
restart_stack() {
    ( . "$RELEASE_LIB" && production_up ) >/dev/null
}

# If the stack was running when we started and is not running now, bring it
# back. Called from the main path as soon as the backup returns (so the outage
# ends before the rehearsal starts) and again from finalize, where it is a
# no-op unless something went wrong in between.
ensure_stack_up() {
    [ "$was_running" = "running" ] || return 0
    local post_state
    post_state="$(describe_stack_state)"
    [ "$post_state" != "running" ] || return 0
    log "stack is ${post_state} after backup (rc=${rc}); restarting"
    if restart_stack; then
        restart_note="restarted"
        log "stack restarted"
    else
        restart_note="restart-failed"
        log "FATAL could not restart the stack; manual recovery required"
    fi
}

# Stage and rehearse the generation this run just produced. Production is
# already back up; the rehearsal restores into a scratch root and never
# touches it. Sets rehearsal_note and, on any failure, rc=1: a backup that
# cannot be shown restorable is a failed nightly, even though the encrypted
# archive stays where it is for a human to look at.
rehearse() {
    if ! ( exec 8>"$ADMISSION_LOCK" && flock -n 8 ); then
        rehearsal_note="skipped-maintenance"
        log "rehearsal skipped: a maintenance holds ${ADMISSION_LOCK}"
        return 0
    fi
    log "staging the new generation for rehearsal"
    if ! "$STAGE_SCRIPT"; then
        rehearsal_note="stage-failed"; rc=1
        log "FAILED staging the generation for rehearsal"
        return 0
    fi
    generation="$(cat "$SELECTION" 2>/dev/null || true)"
    if [[ ! "$generation" =~ ^generation\.[A-Za-z0-9]{1,64}$ ]]; then
        rehearsal_note="selection-invalid"; rc=1
        log "FAILED restore-selection is not a generation id: ${generation}"
        generation="-"
        return 0
    fi
    log "rehearsing ${generation}"
    if ! "$RESTORE_SCRIPT" "$generation"; then
        rehearsal_note="rehearsal-failed"; rc=1
        log "FAILED rehearsal of ${generation}; plaintext kept under ${BACKUP_ROOT} for inspection"
        return 0
    fi
    if ! python3 - "$REHEARSAL_RECEIPT" "$generation" <<'PY'
import json, sys
receipt = json.load(open(sys.argv[1], encoding="utf-8"))
ok = receipt.get("generation") == sys.argv[2] and receipt.get("neo4j_check") == "ok"
raise SystemExit(0 if ok else 1)
PY
    then
        rehearsal_note="receipt-mismatch"; rc=1
        log "FAILED rehearsal receipt does not bind ${generation} with neo4j_check=ok"
        return 0
    fi
    rehearsal_note="ok"
    log "rehearsal ok: ${generation}"
    # Scratch cleanup: the plaintext copies (the encrypted archive is the
    # authority) and the selection marker, which only ever named our own
    # generation here. Anything an operator staged would have been refused
    # above because a maintenance holds admission.
    rm -rf -- "${BACKUP_ROOT}/decrypted/${generation}" "${BACKUP_ROOT}/candidate/${generation}"
    [ "$(cat "$SELECTION" 2>/dev/null || true)" != "$generation" ] || rm -f -- "$SELECTION"
}

# Restore pre-run service state and record the outcome. Runs on EVERY exit path
# -- normal return, `exit 1` from a preflight check, SIGTERM from
# `systemctl stop`, shutdown during the ~2 minute window when the stack is down.
#
# Without this, a TERM mid-backup kills both this wrapper and its child and the
# straight-line restart never executes. The containers do not self-heal:
# backup-generation.sh uses `docker compose stop`, an explicit stop, which the
# `unless-stopped` restart policy deliberately does not undo on daemon start.
# Production would stay down across a reboot.
finalize() {
    local signal="${1:-}"
    [ "$finalized" = 0 ] || return 0
    finalized=1
    [ -z "$signal" ] || { log "received ${signal}; restoring state before exit"; rc=1; }

    ensure_stack_up
    [ "$downtime_seconds" -gt 0 ] || downtime_seconds=$(( $(date +%s) - start_epoch ))

    # A backup that succeeded but left production down is a FAILED run. Without
    # this the process would exit 0, systemd would record success, and the only
    # signal that memory.ctharvey.me is down would be a marker file nothing reads.
    [ "$restart_note" != "restart-failed" ] || rc=1

    local finished elapsed
    finished="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # Measured at finalize, so the restart is inside the number. Computing it
    # before the restart understated a real incident by 40%.
    elapsed=$(( $(date +%s) - start_epoch ))

    if [ "$rc" -eq 0 ]; then
        rm -f "$FAILURE_MARKER"
        log "success in ${elapsed}s (production was down for about ${downtime_seconds}s; rehearsal ${rehearsal_note})"
    else
        write_json "$FAILURE_MARKER" "$(printf '{"schema":1,"kind":"scheduled-backup-failure","exit_code":%d,"restart":"%s","rehearsal":"%s","generation":"%s","job_id":"%s","started_utc":"%s","finished_utc":"%s","elapsed_seconds":%d,"downtime_seconds":%d}' \
            "$rc" "$restart_note" "$rehearsal_note" "$generation" "$job_id" "$started" "$finished" "$elapsed" "$downtime_seconds")"
        log "FAILED rc=${rc} restart=${restart_note} rehearsal=${rehearsal_note} after ${elapsed}s"
    fi

    # Written on every path, including preflight aborts. A wrapper that fails
    # nightly before doing anything must not be indistinguishable from one that
    # was never scheduled.
    write_json "$LAST_RUN" "$(printf '{"schema":1,"kind":"scheduled-backup-last-run","exit_code":%d,"restart":"%s","rehearsal":"%s","generation":"%s","stack_was":"%s","job_id":"%s","started_utc":"%s","finished_utc":"%s","elapsed_seconds":%d,"downtime_seconds":%d}' \
        "$rc" "$restart_note" "$rehearsal_note" "$generation" "$was_running" "$job_id" "$started" "$finished" "$elapsed" "$downtime_seconds")"

    exit "$rc"
}

trap 'finalize' EXIT
trap 'finalize SIGTERM' TERM
trap 'finalize SIGINT' INT

fatal() { log "FATAL $*"; rc=1; exit 1; }

[ -d "$STATUS_DIR" ]    || fatal "status dir absent: $STATUS_DIR"
[ -x "$BACKUP_SCRIPT" ] || fatal "backup script not executable: $BACKUP_SCRIPT"
[ -r "$RELEASE_LIB" ]   || fatal "release library unreadable: $RELEASE_LIB"
[ -x "$STAGE_SCRIPT" ]  || fatal "stage script not executable: $STAGE_SCRIPT"
[ -x "$RESTORE_SCRIPT" ] || fatal "restore script not executable: $RESTORE_SCRIPT"

# Non-blocking: if a previous scheduled run is somehow still going, this one is
# skipped rather than queued. A skipped nightly backup is a non-event.
exec 9>"$LOCK" || fatal "cannot open lock $LOCK"
if ! flock -n 9; then
    log "SKIP another scheduled backup holds $LOCK"
    finalized=1   # nothing was touched; do not write markers or restart
    exit 0
fi

was_running="$(describe_stack_state)"
[ "$was_running" != "unknown" ] \
    || fatal "cannot determine stack state before starting; refusing to run"

# backup-generation.sh hands the finished generation to
# /usr/local/sbin/menhir-backup-local, which refuses to encrypt without a job id
# matching ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$. Normally submit_op mints that id
# and the worker exports it (lib.sh worker line 79). Bypassing submit_op means
# nothing supplies it, and the run dies AFTER the stack has been stopped, dumped
# and verified -- the most expensive possible place to fail. Observed 2026-09-10.
job_id="${MENHIR_OPERATION_JOB_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$((RANDOM % 10000))}"
[[ "$job_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] \
    || fatal "generated job id is invalid: ${job_id}"

log "starting (stack was ${was_running}, job ${job_id})"
MENHIR_OPERATION_JOB_ID="$job_id" "$BACKUP_SCRIPT" || rc=$?

# End the outage here, before the rehearsal, whatever the backup's result.
ensure_stack_up
downtime_seconds=$(( $(date +%s) - start_epoch ))

if [ "$rc" -eq 0 ] && [ "$restart_note" != "restart-failed" ]; then
    rehearse
else
    rehearsal_note="skipped-backup-failed"
fi

# finalize runs from the EXIT trap.
