#!/usr/bin/env bash
#
# Create one complete, versioned Menhir production backup "generation".
#
# A generation is a self-contained, verifiable snapshot of the COMPLETE
# enumerated durable authority:
#   neo4j/           Neo4j offline dumps: neo4j + system databases
#   state/oauth/     OAuth AS SQLite stores (WAL-safe + integrity proof)
#   state/telemetry/ MCP telemetry database (WAL-safe + integrity proof)
#   secrets/         operator secret files: neo4j/neo4j-auth, menhir/neo4j-password,
#                    oauth/oauth_signing_key.json + oauth/retry-response-keyring.json
#                    (signing key + retry keyring), consent secret if configured,
#                    LLM keys, operator/static keys (copied + hashed, never printed)
#   policy/          immutable client policy + its SHA-256 digest
#   config/          deployment + release manifests (compose, Dockerfile,
#                    image digests, git commit)
#   SHA256SUMS       per-file hashes
#   MANIFEST.json    generation/build/config/image/database/secret identity + restore order
#   COMPLETE         completion marker (hash of MANIFEST.json), written last
#
# Design contract:
#   * strict: set -euo pipefail + umask 077; any failure aborts
#   * fixed-root: validated absolute, symlink-free roots
#   * one host-wide maintenance flock shared by release/backup/restore/rollback
#   * unique no-collision generation id (mktemp -d)
#   * quiesced: the menhir-prod stack is stopped while the snapshot is taken;
#     on failure the stack is LEFT STOPPED; it is restarted only after a fully
#     successful, wrapper-uploaded generation, and a restart failure is FATAL
#   * never logs secrets: contents are copied + hashed, never echoed
#   * no fail-open: every required evidence item aborts on missing/failure
#   * neo4j-admin runs as UID/GID 7474 against a writable, 7474-owned target
#   * encrypted off-host/WORM upload is a REQUIRED root-owned wrapper; the
#     script fails closed if absent and only reports success after the wrapper
#     encrypts client-side, uploads off-host, verifies identity/immutability/
#     WORM retention, and removes the plaintext staging.
#
# Run as root. Same environment as `docker compose up` is required.
#
# Usage:
#   deploy/backup-generation.sh
#
# Environment (fixed defaults shown):
#   MENHIR_PROD_ROOT=/srv/menhir/production
#   MENHIR_PROD_STATE_DIR=${MENHIR_PROD_ROOT}/state
#   MENHIR_PROD_SECRETS_DIR=${MENHIR_PROD_ROOT}/secrets
#   MENHIR_PROD_POLICY_DIR=${MENHIR_PROD_ROOT}/policy
#   MENHIR_BACKUP_ROOT=/srv/menhir/backups
#   MENHIR_MAINTENANCE_LOCK=/run/lock/menhir-production.lock
#   MENHIR_BACKUP_WRAPPER=/usr/local/sbin/menhir-backup-upload
#   MENHIR_IMAGE, NEO4J_IMAGE (required, digest-pinned)
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER_DIR="${SCRIPT_DIR}/lib"
[ -d "$HELPER_DIR" ] || HELPER_DIR="$SCRIPT_DIR"
SCHEMA="${HELPER_DIR}/menhir_schema.py"
# shellcheck source=release-lib.sh
. "${SCRIPT_DIR}/release-lib.sh"
# shellcheck source=secrets-map.sh
. "${SCRIPT_DIR}/secrets-map.sh"
DEPLOY_DIR="${MENHIR_DEPLOY_DIR:-/srv/menhir/production/deploy}"
COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.production.yml"
DOCKERFILE="${DEPLOY_DIR}/Dockerfile"
INVENTORY="${MENHIR_DURABLE_INVENTORY:-${DEPLOY_DIR}/durable-state-inventory.json}"

MENHIR_PROD_ROOT="${MENHIR_PROD_ROOT:-/srv/menhir/production}"
STATE_DIR="${MENHIR_PROD_STATE_DIR:-${MENHIR_PROD_ROOT}/state}"
SECRETS_DIR="${MENHIR_PROD_SECRETS_DIR:-${MENHIR_PROD_ROOT}/secrets}"
POLICY_DIR="${MENHIR_PROD_POLICY_DIR:-${MENHIR_PROD_ROOT}/policy}"
BACKUP_ROOT="${MENHIR_BACKUP_ROOT:-/srv/menhir/backups}"
LOCK="${MENHIR_MAINTENANCE_LOCK:-/run/lock/menhir-production.lock}"
WRAPPER="${MENHIR_BACKUP_WRAPPER:-/usr/local/sbin/menhir-backup-upload}"
PRODUCTION_ENV="/srv/menhir/production/release/production.env"

load_production_env

MENHIR_IMAGE="${MENHIR_IMAGE:?MENHIR_IMAGE (digest-pinned menhir image) is required}"
NEO4J_IMAGE="${NEO4J_IMAGE:?NEO4J_IMAGE (digest-pinned neo4j image) is required}"
MENHIR_RELEASE_COMMIT="${MENHIR_RELEASE_COMMIT:?MENHIR_RELEASE_COMMIT is required}"

# Pinned-image consistency: both refs must carry a digest.
case "$MENHIR_IMAGE" in *@sha256:*) ;; *) echo "MENHIR_IMAGE must be digest-pinned (@sha256:...)" >&2; exit 1 ;; esac
case "$NEO4J_IMAGE" in *@sha256:*) ;; *) echo "NEO4J_IMAGE must be digest-pinned (@sha256:...)" >&2; exit 1 ;; esac
[[ "$MENHIR_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] \
    || { echo "MENHIR_IMAGE digest must be exactly 64 lowercase hex characters" >&2; exit 1; }
[[ "$NEO4J_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]] \
    || { echo "NEO4J_IMAGE digest must be exactly 64 lowercase hex characters" >&2; exit 1; }

for cmd in docker flock date find sort xargs sha256sum tr sqlite3 mktemp basename dirname python3; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "required tool not found: $cmd" >&2; exit 1; }
done

# Reject relative paths, '..' traversal, and any symlinked component.
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
for root in "$MENHIR_PROD_ROOT" "$STATE_DIR" "$SECRETS_DIR" "$POLICY_DIR" "$BACKUP_ROOT"; do
    reject_unsafe_root "$root" "root"
done

# Immutable release authority is validated before any snapshot (blocker 7).
RELEASE_JSON="${MENHIR_RELEASE_JSON:-${MENHIR_PROD_ROOT}/release/release.json}"
[ -f "$RELEASE_JSON" ] && [ ! -L "$RELEASE_JSON" ] \
    || { echo "release.json missing or symlink: $RELEASE_JSON" >&2; exit 1; }
[ "$(stat -c '%u' "$RELEASE_JSON")" = 0 ] || { echo "release.json must be root-owned" >&2; exit 1; }
python3 "$SCHEMA" validate-release "$RELEASE_JSON" \
    || { echo "release.json validation failed" >&2; exit 1; }
python3 "${HELPER_DIR}/validate_durable_inventory.py" "$INVENTORY" "$COMPOSE_FILE" \
    --live "$PRODUCTION_ENV" "/var/lib/menhir-production/durable-live-census.json" \
    || { echo "durable-state inventory validation failed" >&2; exit 1; }
release_id="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["release_id"])' "$RELEASE_JSON")"
release_manifest_sha256="$(sha256sum "$RELEASE_JSON" | cut -d' ' -f1)"

# Enforce + verify the secret owner/mode map (blocker 1).
secrets_enforce "$SECRETS_DIR"
secrets_verify "$SECRETS_DIR"

# --- Required secrets + policy (fail closed before quiescing) ---
for s in \
    neo4j/neo4j-auth \
    menhir/neo4j-password \
    menhir/operator-key \
    menhir/source-fence-token \
    oauth/oauth_signing_key.json \
    oauth/retry-response-keyring.json \
    oauth/oauth-consent-secret; do
    [ -f "${SECRETS_DIR}/${s}" ] || { echo "missing required secret: ${SECRETS_DIR}/${s}" >&2; exit 1; }
done
providers="${LLM_CHAT_PROVIDER:-local},${GRAPHITI_LLM_PROVIDER:-local},${GRAPHITI_EMBED_PROVIDER:-}"
case "$providers" in
    *openai*) [ -f "${SECRETS_DIR}/menhir/openai-api-key" ] \
        || { echo "missing required OpenAI provider secret" >&2; exit 1; } ;;
esac
case "$providers" in
    *gemini*) [ -f "${SECRETS_DIR}/menhir/gemini-api-key" ] \
        || { echo "missing required Gemini provider secret" >&2; exit 1; } ;;
esac
[ -f "${POLICY_DIR}/client-policy.json" ] || { echo "missing client policy: ${POLICY_DIR}/client-policy.json" >&2; exit 1; }

GENERATIONS_ROOT="${BACKUP_ROOT}/generations"
mkdir -p "${BACKUP_ROOT}" "${GENERATIONS_ROOT}"

# One fixed host-wide maintenance lock shared by release/backup/restore/rollback.
mkdir -p "$(dirname "$LOCK")"
exec 9>"${LOCK}"
flock -n 9 || { echo "maintenance lock is held: ${LOCK}" >&2; exit 1; }

# Unique, no-collision generation id (mktemp -d cannot collide or traverse).
target="$(mktemp -d "${GENERATIONS_ROOT}/generation.XXXXXXXXXX")"
generation="$(basename "$target")"

was_running="stopped"
if docker compose -f "${COMPOSE_FILE}" ps --status running --quiet 2>/dev/null | grep -q .; then
    was_running="running"
fi

echo "Backup generation: ${generation} (stack was ${was_running})"
echo "Quiescing stack menhir-prod ..."
docker compose -f "${COMPOSE_FILE}" stop
# NOTE: no EXIT trap. If anything below fails, the stack stays stopped.

mkdir -p "${target}/neo4j" "${target}/state/oauth" "${target}/state/telemetry" \
    "${target}/secrets" "${target}/policy" "${target}/config"

# neo4j-admin runs as UID/GID 7474 with a minimal conf injected so the data
# directory is always /data (independent of the running server's home conf).
run_neo4j_admin() { # data_dir backup_dir [args...]
    local data_dir="$1" backup_dir="$2"; shift 2
    local conf_dir
    conf_dir="$(mktemp -d "${BACKUP_ROOT}/.neo4j-conf.XXXXXXXX")"
    printf 'server.directories.data=/data\nserver.directories.logs=/logs\n' > "${conf_dir}/neo4j.conf"
    chmod -R a+rX "${conf_dir}"
    docker run --rm --user "7474:7474" \
        --entrypoint /var/lib/neo4j/bin/neo4j-admin \
        --mount "type=bind,src=${conf_dir},dst=/var/lib/neo4j/conf,readonly" \
        --mount "type=bind,src=${data_dir},dst=/data" \
        --mount "type=bind,src=${backup_dir},dst=/backup" \
        "${NEO4J_IMAGE}" \
        "$@"
    local rc=$?
    rm -rf "${conf_dir}"
    return $rc
}

# --- Neo4j offline dumps (neo4j AND system), as 7474 ---
chown 7474:7474 "${target}/neo4j"   # dump target writable by the neo4j user
echo "Dumping Neo4j database 'neo4j' ..."
run_neo4j_admin "${STATE_DIR}/neo4j/data" "${target}/neo4j" database dump neo4j --to-path=/backup
echo "Dumping Neo4j database 'system' ..."
run_neo4j_admin "${STATE_DIR}/neo4j/data" "${target}/neo4j" database dump system --to-path=/backup
chown -R root:7474 "${target}/neo4j" && chmod -R u=rwX,g=rX,o= "${target}/neo4j"

# A dump is not accepted merely because neo4j-admin created it. Load both
# databases into a clean temporary store with the exact pinned image, then run
# offline consistency checks against that loaded store. The verification store
# and reports are outside the generation and are always removed.
VERIFY_ROOT="$(mktemp -d "${BACKUP_ROOT}/.neo4j-verify.${generation}.XXXXXXXX")"
trap 'rm -rf "$VERIFY_ROOT"' EXIT
mkdir -p "${VERIFY_ROOT}/data" "${VERIFY_ROOT}/reports"
chown -R 7474:7474 "$VERIFY_ROOT"
echo "Loading both Neo4j dumps into a clean verification store ..."
run_neo4j_admin "${VERIFY_ROOT}/data" "${target}/neo4j" \
    database load neo4j --from-path=/backup --overwrite-destination=true
run_neo4j_admin "${VERIFY_ROOT}/data" "${target}/neo4j" \
    database load system --from-path=/backup --overwrite-destination=true
echo "Checking both loaded Neo4j databases ..."
run_neo4j_admin "${VERIFY_ROOT}/data" "${VERIFY_ROOT}/reports" \
    database check neo4j --report-path=/backup
run_neo4j_admin "${VERIFY_ROOT}/data" "${VERIFY_ROOT}/reports" \
    database check system --report-path=/backup
rm -rf "$VERIFY_ROOT"
trap - EXIT

# --- SQLite snapshot helper (WAL-safe checkpoint + integrity proof) ---
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

echo "Snapshotting OAuth authority (WAL-safe) ..."
snapshot_sqlite_dir "${STATE_DIR}/oauth" "${target}/state/oauth"
echo "Snapshotting telemetry authority (WAL-safe) ..."
snapshot_sqlite_dir "${STATE_DIR}/telemetry" "${target}/state/telemetry"
echo "Snapshotting secret files (hashed only, never printed) ..."
cp -a "${SECRETS_DIR}/." "${target}/secrets/"
chown -R 0:0 "${target}/secrets" && chmod -R u=rwX,go= "${target}/secrets"
echo "Snapshotting policy ..."
cp -a "${POLICY_DIR}/." "${target}/policy/"

# --- Config / release manifests (no fail-open on required evidence) ---
cp -a "${COMPOSE_FILE}" "${target}/config/docker-compose.production.yml"
cp -a "${DOCKERFILE}" "${target}/config/Dockerfile"
cp -a "${PRODUCTION_ENV}" "${target}/config/production.env"
cp -a "${RELEASE_JSON}" "${target}/config/release.json"
cp -a "${INVENTORY}" "${target}/config/durable-state-inventory.json"
docker image inspect "${MENHIR_IMAGE}" >/dev/null \
    || { echo "pinned Menhir image is not present locally" >&2; exit 1; }
docker image inspect "${NEO4J_IMAGE}" >/dev/null \
    || { echo "pinned Neo4j image is not present locally" >&2; exit 1; }
menhir_digest="${MENHIR_IMAGE##*@}"
neo4j_digest="${NEO4J_IMAGE##*@}"
printf '%s\n' "${MENHIR_RELEASE_COMMIT}" > "${target}/config/commit.txt"

# --- Required evidence present (fail closed if any item is missing) ---
for evidence in \
    neo4j/neo4j.dump \
    neo4j/system.dump \
    state/oauth/menhir_oauth_as.db \
    state/telemetry/mcp_telemetry.db \
    secrets/neo4j/neo4j-auth \
    secrets/menhir/neo4j-password \
    secrets/menhir/operator-key \
    secrets/menhir/source-fence-token \
    secrets/oauth/oauth_signing_key.json \
    secrets/oauth/retry-response-keyring.json \
    secrets/oauth/oauth-consent-secret \
    policy/client-policy.json \
    config/docker-compose.production.yml \
    config/Dockerfile \
    config/production.env \
    config/release.json \
    config/durable-state-inventory.json \
    config/commit.txt; do
    [ -f "${target}/${evidence}" ] || { echo "missing required evidence: ${evidence}" >&2; exit 1; }
done

# --- Hash every file (except the markers written after this) ---
( cd "${target}" && find . -type f -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
sha256sums_sha256="$( (cd "${target}" && sha256sum SHA256SUMS | cut -d' ' -f1) )"

# --- Manifest (strict duplicate-key-rejecting schema; exact set equality +
# per-file classification) via deploy/lib/make_manifest.py ---
python3 "${HELPER_DIR}/make_manifest.py" "${target}" "${generation}" \
    "${MENHIR_IMAGE}" "${menhir_digest}" "${NEO4J_IMAGE}" "${neo4j_digest}" \
    "$(cat "${target}/config/commit.txt")" "${sha256sums_sha256}" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${release_id}" "${release_manifest_sha256}"

# Self-validate the manifest against the strict schema (fail closed).
python3 "$SCHEMA" validate-manifest "${target}/MANIFEST.json" "${target}" \
    || { echo "generated manifest failed validation" >&2; exit 1; }

manifest_sha256="$( (cd "${target}" && sha256sum MANIFEST.json | cut -d' ' -f1) )"
printf '%s\n' "${manifest_sha256}" > "${target}/COMPLETE"

echo "Local generation complete and verified: ${target}"
echo "Invoking encrypted off-host/WORM upload wrapper: ${WRAPPER}"

if [ ! -x "${WRAPPER}" ] || [ -L "${WRAPPER}" ] \
    || [ "$(stat -c '%u' "${WRAPPER}" 2>/dev/null || echo -1)" != 0 ]; then
    echo "FATAL: upload wrapper must be a root-owned regular executable (not a" >&2
    echo "symlink) at ${WRAPPER}. Stack is left stopped." >&2
    exit 1
fi
mode="$(stat -c '%a' "${WRAPPER}")"
# shellcheck disable=SC2016
(( ((8#${mode}) & 8#022) == 0 )) \
    || { echo "FATAL: upload wrapper must not be group/other writable" >&2; exit 1; }

# Fail closed: only the wrapper's success counts. It removes the plaintext
# staging directory on success; any non-zero exit is a failed backup.
"${WRAPPER}" "${target}" || { echo "FATAL: off-host/WORM upload failed; stack is left stopped" >&2; exit 1; }

# The wrapper must have written an atomic, structured receipt proving the
# off-host object identity, WORM retention, plaintext removal, and /readyz
# recovery. Promotion re-parses this exact file and never reads mtime.
BACKUP_RECEIPT="${MENHIR_BACKUP_RECEIPT:-${STATUS_DIR:-/var/lib/menhir-production}/backup-upload-receipt.json}"
[ -f "$BACKUP_RECEIPT" ] && [ ! -L "$BACKUP_RECEIPT" ] \
    || { echo "FATAL: upload wrapper did not produce a receipt at ${BACKUP_RECEIPT}" >&2; exit 1; }
[ "$(stat -c '%u' "$BACKUP_RECEIPT")" = 0 ] \
    || { echo "FATAL: backup upload receipt must be root-owned" >&2; exit 1; }
receipt_mode="$(stat -c '%a' "$BACKUP_RECEIPT")"
(( ((8#${receipt_mode}) & 8#022) == 0 )) \
    || { echo "FATAL: backup upload receipt must not be group/other writable" >&2; exit 1; }
python3 "$SCHEMA" validate-receipt-binding "$BACKUP_RECEIPT" backup-upload \
    "$RELEASE_JSON" "$generation" "$manifest_sha256" "$menhir_digest" "$neo4j_digest" \
    || { echo "FATAL: backup upload receipt failed validation or release binding" >&2; exit 1; }

echo "Backup ${generation} uploaded and verified; plaintext staging removed."

# Restart only after a fully successful, uploaded generation; restart failure is fatal.
if [ "${was_running}" = "running" ]; then
    echo "Restoring stack menhir-prod to its prior running state ..."
    docker compose -f "${COMPOSE_FILE}" up -d \
        || { echo "FATAL: failed to restart stack after successful backup; restart it manually" >&2; exit 1; }
fi
printf 'generation=%s\n' "${generation}"
