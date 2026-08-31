#!/usr/bin/env bash
#
# Restore a complete Menhir production backup generation.
#
# Strict, non-destructive, and rehearsal-first by design:
#   * accepts ONLY a strictly validated generation id under the fixed decrypted
#     staging root (${MENHIR_BACKUP_ROOT}/decrypted/<id>), never a path; rejects
#     symlinks and special files in the restore inputs
#   * verifies the full integrity chain (COMPLETE -> MANIFEST.json ->
#     SHA256SUMS -> files) with the manifest parsed as strict JSON (python3),
#     plus image identity, schema identity, and secret/policy enumeration
#   * refuses mixed-generation state (any hash/digest/schema mismatch aborts)
#   * pinned-image offline consistency: both image refs must be digest-pinned
#     and present locally, and their digests must match the manifest
#   * DEFAULT mode is a rehearsal into a clean scratch root that never touches
#     production; a distinct, guarded --production mode restores production
#   * never merge-copies into existing directories: current authority is moved
#     aside as a COMPLETE, hashed, manifest-bound pre-restore generation
#     suitable for rollback before anything is overwritten
#   * preserves whether the stack was running; a failed restore leaves the stack
#     STOPPED; restart happens only after full success and is fatal on failure
#   * ownership/permissions failures abort (no best-effort chown)
#   * neo4j-admin database load runs as UID/GID 7474
#   * never logs secrets
#
# Run as root. Same environment as `docker compose up` is required.
#
# Usage:
#   # Rehearsal (default; safe, does not touch production):
#   deploy/restore-generation.sh <generation-id>
#   # Production restore (guarded):
#   MENHIR_RESTORE_CONFIRM=<generation-id> \
#       deploy/restore-generation.sh <generation-id> --yes --production
#
# Environment (same fixed defaults as backup-generation.sh):
#   MENHIR_PROD_ROOT=/srv/menhir/production
#   MENHIR_PROD_STATE_DIR=${MENHIR_PROD_ROOT}/state
#   MENHIR_PROD_SECRETS_DIR=${MENHIR_PROD_ROOT}/secrets
#   MENHIR_PROD_POLICY_DIR=${MENHIR_PROD_ROOT}/policy
#   MENHIR_BACKUP_ROOT=/srv/menhir/backups
#   MENHIR_MAINTENANCE_LOCK=/run/lock/menhir-production.lock
#   MENHIR_IMAGE, NEO4J_IMAGE (required, digest-pinned)
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER_DIR="${SCRIPT_DIR}/lib"
[ -d "$HELPER_DIR" ] || HELPER_DIR="$SCRIPT_DIR"
SCHEMA="${HELPER_DIR}/menhir_schema.py"
INVENTORY_VALIDATOR="${HELPER_DIR}/validate_durable_inventory.py"
RESTORE_TXN="${HELPER_DIR}/restore_authority_txn.py"
# shellcheck source=release-lib.sh
. "${SCRIPT_DIR}/release-lib.sh"
# shellcheck source=secrets-map.sh
. "${SCRIPT_DIR}/secrets-map.sh"
DEPLOY_DIR="${MENHIR_DEPLOY_DIR:-/srv/menhir/production/deploy}"
COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.production.yml"

MENHIR_PROD_ROOT="${MENHIR_PROD_ROOT:-/srv/menhir/production}"
STATE_DIR="${MENHIR_PROD_STATE_DIR:-${MENHIR_PROD_ROOT}/state}"
SECRETS_DIR="${MENHIR_PROD_SECRETS_DIR:-${MENHIR_PROD_ROOT}/secrets}"
POLICY_DIR="${MENHIR_PROD_POLICY_DIR:-${MENHIR_PROD_ROOT}/policy}"
BACKUP_ROOT="${MENHIR_BACKUP_ROOT:-/srv/menhir/backups}"
LOCK="${MENHIR_MAINTENANCE_LOCK:-/run/lock/menhir-production.lock}"
STATUS_DIR="${MENHIR_STATUS_DIR:-/var/lib/menhir-production}"

load_production_env

usage() { echo "usage: $0 <generation-id> [--yes] [--production]" >&2; exit 2; }

[ $# -ge 1 ] || usage
gen_id="$1"; shift
confirm=0 production=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y) confirm=1 ;;
        --production) production=1 ;;
        *) usage ;;
    esac
done

# Strict generation id: exactly the shape the backup emits (generation.<alnum>).
if ! [[ "$gen_id" =~ ^generation\.[A-Za-z0-9]+$ ]]; then
    echo "invalid generation id (expected generation.<alnum>): ${gen_id}" >&2; exit 1
fi

DECRYPTED="${BACKUP_ROOT}/decrypted"
gen="${DECRYPTED}/${gen_id}"

MENHIR_IMAGE="${MENHIR_IMAGE:?MENHIR_IMAGE (digest-pinned menhir image) is required}"
NEO4J_IMAGE="${NEO4J_IMAGE:?NEO4J_IMAGE (digest-pinned neo4j image) is required}"

# Pinned-image offline consistency: refs must carry a digest and be present locally.
case "$MENHIR_IMAGE" in *@sha256:*) ;; *) echo "MENHIR_IMAGE must be digest-pinned (@sha256:...)" >&2; exit 1 ;; esac
case "$NEO4J_IMAGE" in *@sha256:*) ;; *) echo "NEO4J_IMAGE must be digest-pinned (@sha256:...)" >&2; exit 1 ;; esac
[[ "$MENHIR_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] \
    || { echo "MENHIR_IMAGE digest must be exactly 64 lowercase hex characters" >&2; exit 1; }
[[ "$NEO4J_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] \
    || { echo "NEO4J_IMAGE digest must be exactly 64 lowercase hex characters" >&2; exit 1; }
docker image inspect "${MENHIR_IMAGE}" >/dev/null 2>&1 || { echo "menhir image not present locally: ${MENHIR_IMAGE}" >&2; exit 1; }
docker image inspect "${NEO4J_IMAGE}" >/dev/null 2>&1 || { echo "neo4j image not present locally: ${NEO4J_IMAGE}" >&2; exit 1; }

for cmd in docker flock date cp sha256sum sqlite3 find sort xargs mktemp python3 sed; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "required tool not found: $cmd" >&2; exit 1; }
done

reject_unsafe_root() {
    local p="$1" label="$2"
    case "$p" in
        /*) ;;
        *) echo "$label must be an absolute path: $p" >&2; exit 1 ;;
    esac
    case "$p" in
        *..*) echo "$label must not contain '..': $p" >&2; exit 1 ;;
    esac
    local comp="$p"
    while [ "$comp" != "/" ]; do
        if [ -L "$comp" ]; then echo "$label must not traverse a symlink: $comp" >&2; exit 1; fi
        comp="$(dirname "$comp")"
    done
}
for root in "$STATE_DIR" "$SECRETS_DIR" "$POLICY_DIR" "$BACKUP_ROOT"; do
    reject_unsafe_root "$root" "root"
done

# Serialize against release/backup/restore/rollback with one fixed flock.
mkdir -p "$(dirname "$LOCK")"
exec 9>"${LOCK}"
flock -n 9 || { echo "maintenance lock is held: ${LOCK}" >&2; exit 1; }

# Reject symlinks and special files in the restore inputs.
[ -d "$gen" ] || { echo "generation not found under ${DECRYPTED}: ${gen_id}" >&2; exit 1; }
[ -L "$gen" ] && { echo "generation path is a symlink; refusing" >&2; exit 1; }
if [ -n "$(find "$gen" -type l -print -quit)" ]; then echo "generation contains symlinks; refusing" >&2; exit 1; fi
if [ -n "$(find "$gen" ! -type f ! -type d -print -quit)" ]; then echo "generation contains special files; refusing" >&2; exit 1; fi

# --- 1. Verify the integrity chain (COMPLETE -> MANIFEST -> SHA256SUMS -> files) ---
[ -f "${gen}/COMPLETE" ] || { echo "COMPLETE marker missing; incomplete generation" >&2; exit 1; }
[ -f "${gen}/MANIFEST.json" ] || { echo "MANIFEST.json missing" >&2; exit 1; }
[ -f "${gen}/SHA256SUMS" ] || { echo "SHA256SUMS missing" >&2; exit 1; }

manifest_sha256_actual="$( (cd "${gen}" && sha256sum MANIFEST.json | cut -d' ' -f1) )"
manifest_sha256_recorded="$(cat "${gen}/COMPLETE")"
[ "$manifest_sha256_actual" = "$manifest_sha256_recorded" ] \
    || { echo "COMPLETE does not bind MANIFEST.json; refusing" >&2; exit 1; }

# Strict manifest validation (duplicate-key rejection, exact set equality,
# per-file classification + hash, required authority, SHA256SUMS binding).
python3 "$SCHEMA" validate-manifest "${gen}/MANIFEST.json" "${gen}" \
    || { echo "manifest validation failed" >&2; exit 1; }
python3 "$INVENTORY_VALIDATOR" "${gen}/config/durable-state-inventory.json" \
    "${gen}/config/docker-compose.production.yml" \
    || { echo "generation durable-state inventory validation failed" >&2; exit 1; }
( cd "${gen}" && sha256sum -c SHA256SUMS --strict ) || { echo "hash verification failed" >&2; exit 1; }
echo "Integrity chain verified: ${gen}"

# --- 2. Image identity (refuse mixed-generation / wrong-schema image) ---
manifest_menhir_digest="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"]["menhir_image_digest"])' "${gen}/MANIFEST.json")"
manifest_neo4j_digest="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"]["neo4j_image_digest"])' "${gen}/MANIFEST.json")"
current_menhir_digest="${MENHIR_IMAGE##*@}"
current_neo4j_digest="${NEO4J_IMAGE##*@}"
[ "$manifest_menhir_digest" = "$current_menhir_digest" ] \
    || { echo "menhir image digest mismatch (mixed-generation refused)" >&2; exit 1; }
[ "$manifest_neo4j_digest" = "$current_neo4j_digest" ] \
    || { echo "neo4j image digest mismatch (mixed-generation refused)" >&2; exit 1; }
echo "Image + schema identity verified."

# --- Release authority is validated on every lifecycle operation (blocker 7) ---
RELEASE_JSON="${MENHIR_RELEASE_JSON:-${MENHIR_PROD_ROOT}/release/release.json}"
[ -f "$RELEASE_JSON" ] && [ ! -L "$RELEASE_JSON" ] \
    || { echo "release.json missing or symlink: $RELEASE_JSON" >&2; exit 1; }
python3 "$SCHEMA" validate-release "$RELEASE_JSON" \
    || { echo "release.json validation failed" >&2; exit 1; }
release_id="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["release_id"])' "$RELEASE_JSON")"
release_manifest_sha256="$(sha256sum "$RELEASE_JSON" | cut -d' ' -f1)"

backup_receipt="${STATUS_DIR}/backup-receipts/${gen_id}.json"
[ -f "$backup_receipt" ] && [ ! -L "$backup_receipt" ] \
    || { echo "release-bound local backup receipt is required" >&2; exit 1; }
[ "$(stat -c '%u' "$backup_receipt")" = 0 ] \
    || { echo "local backup receipt must be root-owned" >&2; exit 1; }
backup_receipt_mode="$(stat -c '%a' "$backup_receipt")"
(( ((8#${backup_receipt_mode}) & 8#022) == 0 )) \
    || { echo "local backup receipt must not be group/other writable" >&2; exit 1; }
python3 "$SCHEMA" validate-receipt-binding "$backup_receipt" backup-local \
    "$RELEASE_JSON" "$gen_id" "$manifest_sha256_actual" \
    "$manifest_menhir_digest" "$manifest_neo4j_digest" \
    || { echo "backup receipt does not bind this restored generation" >&2; exit 1; }

# neo4j-admin runs as UID/GID 7474 with a minimal injected conf.
run_neo4j_admin() { # data_dir exchange_dir [args...]
    local data_dir="$1" exchange_dir="$2"; shift 2
    local conf_dir
    conf_dir="$(mktemp -d)"
    printf 'server.directories.data=/data\nserver.directories.logs=/logs\n' > "${conf_dir}/neo4j.conf"
    chmod -R a+rX "${conf_dir}"
    docker run --rm --user "7474:7474" \
        --entrypoint /var/lib/neo4j/bin/neo4j-admin \
        --mount "type=bind,src=${conf_dir},dst=/var/lib/neo4j/conf,readonly" \
        --mount "type=bind,src=${data_dir},dst=/data" \
        --mount "type=bind,src=${exchange_dir},dst=/backup" \
        "${NEO4J_IMAGE}" \
        "$@"
    local rc=$?
    rm -rf "${conf_dir}"
    return $rc
}

load_neo4j_into() { # data_dir
    local data_dir="$1"
    mkdir -p "$data_dir"
    chown -R 7474:7474 "$data_dir"
    chmod -R u+rX,g+rX,o+rX "${gen}/neo4j"   # metadata-only; content hash unchanged
    run_neo4j_admin "$data_dir" "${gen}/neo4j" database load neo4j --from-path=/backup --overwrite-destination=true
    run_neo4j_admin "$data_dir" "${gen}/neo4j" database load system --from-path=/backup --overwrite-destination=true
}

check_neo4j_in() { # data_dir
    local data_dir="$1" report_dir
    report_dir="$(mktemp -d)"
    chown 7474:7474 "$report_dir"
    run_neo4j_admin "$data_dir" "$report_dir" database check neo4j --report-path=/backup
    run_neo4j_admin "$data_dir" "$report_dir" database check system --report-path=/backup
    rm -rf "$report_dir"
}

# SQLite WAL-safe checkpoint + integrity proof (same as backup generation).
snapshot_sqlite_dir() { # src_dir dst_dir
    local src_dir="$1" dst_dir="$2"
    [ -d "$src_dir" ] || { echo "authority directory missing: $src_dir" >&2; return 1; }
    mkdir -p "$dst_dir"
    cp -a "${src_dir}/." "${dst_dir}/"
    local db
    for db in "${dst_dir}"/*.db; do
        [ -e "$db" ] || continue
        local base tmp
        base="$(basename "$db" .db)"
        tmp="${dst_dir}/.${base}.snapshot.db"
        sqlite3 "$db" ".backup '$tmp'"
        local proof
        proof="$(sqlite3 "$tmp" 'PRAGMA integrity_check;')"
        [ "$proof" = "ok" ] || { echo "sqlite integrity_check failed for ${base}: ${proof}" >&2; return 1; }
        printf 'ok\n' > "${dst_dir}/${base}.integrity.txt"
        mv -f "$tmp" "$db"
        rm -f "${dst_dir}/${base}.db-wal" "${dst_dir}/${base}.db-shm"
    done
}

# --- 4. Rehearsal (default): clean scratch, never touches production ---
if [ "$production" = 0 ]; then
    mkdir -p "${BACKUP_ROOT}/candidate"
    rehearsal="${BACKUP_ROOT}/candidate/${gen_id}"
    [ ! -e "$rehearsal" ] \
        || { echo "candidate rehearsal already exists: ${rehearsal}" >&2; exit 1; }
    mkdir -m 0700 "$rehearsal"
    echo "Rehearsal restore into clean scratch: ${rehearsal}"
    mkdir -p "${rehearsal}/state/oauth" "${rehearsal}/state/telemetry" \
        "${rehearsal}/state/neo4j/data" "${rehearsal}/state/neo4j/logs" \
        "${rehearsal}/secrets" "${rehearsal}/policy"
    cp -a "${gen}/state/oauth/." "${rehearsal}/state/oauth/"
    cp -a "${gen}/state/telemetry/." "${rehearsal}/state/telemetry/"
    cp -a "${gen}/secrets/." "${rehearsal}/secrets/"
    cp -a "${gen}/policy/." "${rehearsal}/policy/"
    # Normalize + verify secret permissions identical to production (blocker 1).
    secrets_enforce "${rehearsal}/secrets"
    secrets_verify "${rehearsal}/secrets"
    load_neo4j_into "${rehearsal}/state/neo4j/data"
    check_neo4j_in "${rehearsal}/state/neo4j/data"
    chown -R 10001:10001 "${rehearsal}/state/oauth" "${rehearsal}/state/telemetry"
    chown -R 7474:7474 "${rehearsal}/state/neo4j"
    chown -R 0:10001 "${rehearsal}/policy"
    chmod -R u=rwX,g=rX,o= "${rehearsal}/policy"
    for db in "${rehearsal}"/state/*/*.db; do
        [ -e "$db" ] || continue
        proof="$(sqlite3 "$db" 'PRAGMA integrity_check;')"
        [ "$proof" = "ok" ] || { echo "rehearsal sqlite integrity_check failed: $db -> $proof" >&2; exit 1; }
    done
    printf '%s\n' "$gen_id" > "${rehearsal}/REHEARSAL-PASSED"
    chmod 0400 "${rehearsal}/REHEARSAL-PASSED"

    # Root-owned rehearsal receipt bound to generation + manifest + release/config/image.
    manifest_sha256="$( (cd "${gen}" && sha256sum MANIFEST.json | cut -d' ' -f1) )"
    install -d -o root -g root -m 0755 "$STATUS_DIR"
    receipt="${STATUS_DIR}/rehearsal-receipt.json"
    receipt_tmp="$(mktemp "${STATUS_DIR}/.rehearsal.XXXXXXXX")"
    python3 - "$receipt_tmp" "$gen_id" "$manifest_sha256" "$release_id" \
        "$release_manifest_sha256" "$manifest_menhir_digest" "$manifest_neo4j_digest" <<'PYEOF'
import json, os, sys
(path, generation, manifest_sha256, release_id, release_manifest_sha256,
 menhir_digest, neo4j_digest) = sys.argv[1:8]
receipt = {
    "schema": 1,
    "kind": "rehearsal",
    "generation": generation,
    "manifest_sha256": manifest_sha256,
    "release": {
        "release_id": release_id,
        "release_manifest_sha256": release_manifest_sha256,
        "menhir_image_digest": menhir_digest,
        "neo4j_image_digest": neo4j_digest,
    },
    "neo4j_check": "ok",
    "sqlite_integrity": "ok",
    "checked_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
}
with open(path, "w", encoding="ascii") as f:
    json.dump(receipt, f, indent=2, sort_keys=True)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())
PYEOF
    chown 0:0 "$receipt_tmp" && chmod 0400 "$receipt_tmp"
    mv -f "$receipt_tmp" "$receipt"
    python3 "$SCHEMA" validate-receipt "$receipt" rehearsal \
        || { echo "rehearsal receipt failed validation" >&2; exit 1; }
    echo "Rehearsal passed (graph consistency + SQLite integrity + secret map)."
    echo "Receipt: ${receipt}. Nothing in production was touched."
    exit 0
fi

# --- 5. Production restore (guarded) ---
[ "$confirm" = 1 ] || { echo "refusing production restore without --yes" >&2; exit 1; }
[ "${MENHIR_RESTORE_CONFIRM:-}" = "$gen_id" ] \
    || { echo "set MENHIR_RESTORE_CONFIRM=${gen_id} to confirm production restore" >&2; exit 1; }

# Production restore requires a root-owned rehearsal receipt bound to this
# generation (blocker 3).
rehearsal_receipt="${STATUS_DIR}/rehearsal-receipt.json"
[ -f "$rehearsal_receipt" ] && [ ! -L "$rehearsal_receipt" ] \
    || { echo "rehearsal receipt missing; run a rehearsal first" >&2; exit 1; }
[ "$(stat -c '%u' "$rehearsal_receipt")" = 0 ] || { echo "rehearsal receipt must be root-owned" >&2; exit 1; }
receipt_mode="$(stat -c '%a' "$rehearsal_receipt")"
(( ((8#${receipt_mode}) & 8#022) == 0 )) \
    || { echo "rehearsal receipt must not be group/other writable" >&2; exit 1; }
python3 "$SCHEMA" validate-receipt-binding "$rehearsal_receipt" rehearsal \
    "$RELEASE_JSON" "$gen_id" "$manifest_sha256_actual" \
    "$manifest_menhir_digest" "$manifest_neo4j_digest" \
    || { echo "rehearsal receipt failed validation or release binding" >&2; exit 1; }
rec_generation="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["generation"])' "$rehearsal_receipt")"
[ "$rec_generation" = "$gen_id" ] \
    || { echo "rehearsal receipt is for a different generation" >&2; exit 1; }

# An empty first install requires explicit release-bound approval. An existing
# production install must have a valid current-generation marker; its exact
# authority directories are preserved by the swap journal below.
approval="${STATUS_DIR}/initial-restore-approved"
install -d -o root -g root -m 0755 "$STATUS_DIR"
prior_generation=""
if [ -e "${STATUS_DIR}/current-generation" ]; then
    prior_generation="$(read_generation "${STATUS_DIR}/current-generation" "current generation")"
else
    [ -f "$approval" ] && [ ! -L "$approval" ] && [ "$(stat -c '%u' "$approval")" = 0 ] \
        || { echo "root-owned initial restore approval is required: $approval" >&2; exit 1; }
    [ "$(cat "$approval")" = "$release_id" ] \
        || { echo "initial restore approval is not bound to release $release_id" >&2; exit 1; }
    for target_dir in "${STATE_DIR}/oauth" "${STATE_DIR}/telemetry" "${STATE_DIR}/neo4j/data" \
                      "${SECRETS_DIR}" "${POLICY_DIR}"; do
        if [ -d "$target_dir" ] && find "$target_dir" -mindepth 1 -print -quit | grep -q .; then
            echo "untracked production authority exists without current-generation: $target_dir" >&2
            exit 1
        fi
    done
fi

# Prepare and fully validate incoming authority in same-filesystem sibling
# directories. No production path is touched during this phase.
transaction_id="restore-${gen_id}-$(date -u +%Y%m%dT%H%M%SZ)"
restore_journal="${STATUS_DIR}/restore-journal.json"
restore_anchor_root="${STATUS_DIR}/pre-restore-anchors"
[ ! -e "$restore_journal" ] && [ ! -L "$restore_journal" ] \
    || { echo "unfinished restore journal exists: $restore_journal" >&2; exit 1; }

oauth_stage="${STATE_DIR}/.menhir-restore-stage-${transaction_id}-oauth"
telemetry_stage="${STATE_DIR}/.menhir-restore-stage-${transaction_id}-telemetry"
neo4j_stage="${STATE_DIR}/neo4j/.menhir-restore-stage-${transaction_id}-neo4j-data"
secrets_stage="$(dirname "$SECRETS_DIR")/.menhir-restore-stage-${transaction_id}-secrets"
policy_stage="$(dirname "$POLICY_DIR")/.menhir-restore-stage-${transaction_id}-policy"
restore_stages=("$oauth_stage" "$telemetry_stage" "$neo4j_stage" "$secrets_stage" "$policy_stage")
for stage in "${restore_stages[@]}"; do
    [ ! -e "$stage" ] && [ ! -L "$stage" ] \
        || { echo "restore staging path is occupied: $stage" >&2; exit 1; }
    mkdir -p "$(dirname "$stage")"
    mkdir -m 0700 "$stage"
done

stages_owned=1
cleanup_unapplied_stages() {
    if [ "$stages_owned" = 1 ]; then
        for stage in "${restore_stages[@]}"; do
            case "$(basename "$stage")" in
                .menhir-restore-stage-${transaction_id}-*) [ ! -e "$stage" ] || rm -rf -- "$stage" ;;
                *) echo "unsafe restore staging cleanup path refused: $stage" >&2 ;;
            esac
        done
    fi
}
trap cleanup_unapplied_stages EXIT

cp -a "${gen}/state/oauth/." "$oauth_stage/"
cp -a "${gen}/state/telemetry/." "$telemetry_stage/"
cp -a "${gen}/secrets/." "$secrets_stage/"
cp -a "${gen}/policy/." "$policy_stage/"
load_neo4j_into "$neo4j_stage"
check_neo4j_in "$neo4j_stage"

chown -R 10001:10001 "$oauth_stage" "$telemetry_stage"
chown -R 7474:7474 "$neo4j_stage"
secrets_enforce "$secrets_stage" && secrets_verify "$secrets_stage"
chown -R 0:0 "$policy_stage"
chmod -R u=rwX,g=rX,o=rX "$policy_stage"
for db in "$oauth_stage"/*.db "$telemetry_stage"/*.db; do
    [ -e "$db" ] || continue
    proof="$(sqlite3 "$db" 'PRAGMA integrity_check;')"
    [ "$proof" = "ok" ] || { echo "staged sqlite integrity_check failed: $db -> $proof" >&2; exit 1; }
done

was_running="stopped"
if docker compose -f "${COMPOSE_FILE}" ps --status running --quiet 2>/dev/null | grep -q .; then
    was_running="running"
fi
echo "Quiescing stack menhir-prod (was ${was_running}) ..."
docker compose -f "${COMPOSE_FILE}" stop
# From here onward, any unsuccessful restore keeps or recovers prior authority.

python3 "$RESTORE_TXN" begin "$restore_journal" "$transaction_id" \
    "$prior_generation" "$gen_id" \
    "oauth=${STATE_DIR}/oauth=${oauth_stage}" \
    "telemetry=${STATE_DIR}/telemetry=${telemetry_stage}" \
    "neo4j-data=${STATE_DIR}/neo4j/data=${neo4j_stage}" \
    "secrets=${SECRETS_DIR}=${secrets_stage}" \
    "policy=${POLICY_DIR}=${policy_stage}"

if ! python3 "$RESTORE_TXN" apply "$restore_journal"; then
    echo "restore swap failed; recovering prior authority" >&2
    python3 "$RESTORE_TXN" rollback "$restore_journal" \
        || { echo "FATAL: automatic restore rollback failed; keep stack stopped" >&2; exit 1; }
    exit 1
fi
stages_owned=0

restore_anchor="$(python3 "$RESTORE_TXN" commit "$restore_journal" "$restore_anchor_root")" \
    || { echo "FATAL: restored authority applied but rollback anchor commit failed; keep stack stopped" >&2; exit 1; }

recover_prior_authority() {
    docker compose -f "${COMPOSE_FILE}" stop >/dev/null 2>&1 || true
    restore_rollback_receipt="${restore_anchor%.json}.rollback.json"
    python3 "$RESTORE_TXN" rollback "$restore_anchor" "$restore_rollback_receipt" \
        || { echo "FATAL: automatic rollback failed; keep stack stopped and inspect $restore_anchor" >&2; return 1; }
    if [ "$was_running" = "running" ]; then
        docker compose -f "${COMPOSE_FILE}" up -d \
            || { echo "FATAL: prior authority restored but prior stack restart failed" >&2; return 1; }
    fi
}

if ! check_neo4j_in "${STATE_DIR}/neo4j/data" \
        || ! secrets_verify "$SECRETS_DIR"; then
    echo "post-swap authority validation failed; rolling back" >&2
    recover_prior_authority || true
    exit 1
fi

if [ "$was_running" = "running" ]; then
    echo "Starting restored stack menhir-prod ..."
    if ! docker compose -f "${COMPOSE_FILE}" up -d; then
        echo "restored stack failed to start; rolling back to prior authority" >&2
        recover_prior_authority || true
        exit 1
    fi
fi

if ! write_generation_record "${STATUS_DIR}/current-generation" "$gen_id" \
        || ! write_generation_record "${STATUS_DIR}/restored-generation" "$gen_id"; then
    echo "generation marker commit failed; rolling back to prior authority" >&2
    recover_prior_authority || true
    if [ -n "$prior_generation" ]; then
        write_generation_record "${STATUS_DIR}/current-generation" "$prior_generation" || true
        write_generation_record "${STATUS_DIR}/restored-generation" "$prior_generation" || true
    else
        rm -f "${STATUS_DIR}/current-generation" "${STATUS_DIR}/restored-generation"
    fi
    exit 1
fi

rm -f "$approval"
trap - EXIT
echo "Production restore complete from ${gen}."
echo "Immutable rollback anchor: ${restore_anchor}"
