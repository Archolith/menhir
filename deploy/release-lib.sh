#!/usr/bin/env bash
set -euo pipefail
umask 077

MENHIR_ROOT="/srv/menhir/production"
MENHIR_PROD_ROOT="${MENHIR_PROD_ROOT:-${MENHIR_ROOT}}"
COMPOSE_FILE="${MENHIR_ROOT}/deploy/docker-compose.production.yml"
PRODUCTION_ENV="${MENHIR_ROOT}/release/production.env"
STATUS_DIR="/var/lib/menhir-production"
BACKUP_ROOT="/srv/menhir/backups"
LOCK="/run/lock/menhir-production.lock"

require_root_file() {
    local path="$1" label="$2" owner mode
    [ -f "$path" ] && [ ! -L "$path" ] \
        || { echo "$label must be a regular non-symlink file: $path" >&2; exit 1; }
    owner="$(stat -c '%u' "$path")"; mode="$(stat -c '%a' "$path")"
    [ "$owner" = 0 ] || { echo "$label must be root-owned" >&2; exit 1; }
    (( ((8#${mode}) & 8#022) == 0 )) \
        || { echo "$label must not be group/other writable" >&2; exit 1; }
}

load_production_env() {
    require_root_file "$PRODUCTION_ENV" "production release environment"
    validate_release_authority
    local expected_env_sha actual_env_sha
    expected_env_sha="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["rendered"]["production_env_sha256"])' "$RELEASE_JSON")"
    actual_env_sha="$(sha256sum "$PRODUCTION_ENV" | cut -d' ' -f1)"
    [ "$actual_env_sha" = "$expected_env_sha" ] \
        || { echo "production.env digest differs from immutable release authority" >&2; exit 1; }
    set -a
    # Root-owned, non-writable-by-group/other operator configuration.
    # shellcheck source=/dev/null
    . "$PRODUCTION_ENV"
    set +a
    [[ "${MENHIR_IMAGE:-}" =~ @sha256:[0-9a-f]{64}$ ]] \
        || { echo "MENHIR_IMAGE must be digest-pinned" >&2; exit 1; }
    [[ "${NEO4J_IMAGE:-}" =~ @sha256:[0-9a-f]{64}$ ]] \
        || { echo "NEO4J_IMAGE must be digest-pinned" >&2; exit 1; }
    validate_runtime_release_binding
}

read_generation() {
    local path="$1" label="$2" value
    require_root_file "$path" "$label"
    [ "$(wc -l < "$path")" -eq 1 ] \
        || { echo "$label must contain exactly one line" >&2; exit 1; }
    IFS= read -r value < "$path"
    [[ "$value" =~ ^generation\.[A-Za-z0-9]+$ ]] \
        || { echo "$label contains an invalid generation id" >&2; exit 1; }
    printf '%s\n' "$value"
}

acquire_release_lock() {
    install -d -o root -g root -m 0755 "$(dirname "$LOCK")"
    exec 9>"$LOCK"
    flock -n 9 || { echo "maintenance lock is held: $LOCK" >&2; exit 75; }
}

compose_env() {
    local project="$1"; shift
    docker compose --project-name "$project" --env-file "$PRODUCTION_ENV" \
        --file "$COMPOSE_FILE" "$@"
}

wait_healthy() {
    local app_container="$1" neo4j_container="$2" deadline=$((SECONDS + 240))
    while (( SECONDS < deadline )); do
        if [ "$(docker inspect -f '{{.State.Health.Status}}' "$app_container" 2>/dev/null || true)" = healthy ] \
           && [ "$(docker inspect -f '{{.State.Health.Status}}' "$neo4j_container" 2>/dev/null || true)" = healthy ]; then
            return 0
        fi
        sleep 3
    done
    echo "containers did not become healthy within 240 seconds" >&2
    return 1
}

wait_container_healthy() {
    local container="$1" deadline=$((SECONDS + 240))
    while (( SECONDS < deadline )); do
        if [ "$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || true)" = healthy ]; then
            return 0
        fi
        sleep 3
    done
    echo "container did not become healthy within 240 seconds: $container" >&2
    return 1
}

candidate_compose() {
    local generation="$1"
    local candidate_root="${BACKUP_ROOT}/candidate/${generation}"
    shift
    MENHIR_COMPOSE_PROJECT=menhir-candidate \
    MENHIR_RUNTIME_MODE=candidate-readonly \
    MENHIR_STATE_ROOT="${MENHIR_ROOT}/state" \
    MENHIR_TELEMETRY_ROOT="${candidate_root}/probe-output/telemetry" \
    MENHIR_PROD_SECRETS_DIR="${MENHIR_ROOT}/secrets" \
    MENHIR_PROD_POLICY_DIR="${MENHIR_ROOT}/policy" \
    MENHIR_AUTHORITIES_READ_ONLY=true \
    MENHIR_APP_CONTAINER=menhir-candidate-app \
    MENHIR_NEO4J_CONTAINER=menhir-candidate-neo4j \
    MENHIR_PROXY_IPV4=172.30.0.3 \
    MENHIR_PROXY_ALIAS=menhir-prod-app \
        compose_env menhir-candidate "$@"
}

candidate_neo4j_up() {
    candidate_compose "$1" up -d neo4j
    wait_container_healthy menhir-candidate-neo4j
}

candidate_app_up() {
    candidate_compose "$1" up -d menhir
    wait_healthy menhir-candidate-app menhir-candidate-neo4j
}

authority_digest_tool() {
    if [ -f "${SCRIPT_DIR}/lib/authority_digest.py" ]; then
        printf '%s\n' "${SCRIPT_DIR}/lib/authority_digest.py"
    else
        printf '%s\n' "${SCRIPT_DIR}/authority_digest.py"
    fi
}

candidate_local_authority_digest() {
    local generation="$1"
    : "$generation"
    python3 "$(authority_digest_tool)" local-set \
        "oauth=${MENHIR_ROOT}/state/oauth" \
        "telemetry=${MENHIR_ROOT}/state/telemetry" \
        "secrets=${MENHIR_ROOT}/secrets" \
        "policy=${MENHIR_ROOT}/policy"
}

candidate_neo4j_authority_digest() {
    local generation="$1"
    # Execute the reviewed standard-library encoder in a one-shot container
    # from the candidate's pinned Menhir image before the long-running app is
    # started. Once the reviewed candidate is running, execute the same encoder
    # there so a Compose one-off container cannot contend for its fixed proxy
    # address. The script is streamed over stdin and emits only the final digest.
    MENHIR_APP_MEMORY_LIMIT=4g candidate_compose "$generation" config --quiet
    if [ "$(docker inspect -f '{{.State.Running}}' menhir-candidate-app 2>/dev/null || true)" = true ]; then
        MENHIR_APP_MEMORY_LIMIT=4g candidate_compose "$generation" exec -T menhir python3 - neo4j \
            < "$(authority_digest_tool)"
    else
        MENHIR_APP_MEMORY_LIMIT=4g candidate_compose "$generation" run --rm --no-deps -T menhir python3 - neo4j \
            < "$(authority_digest_tool)"
    fi
}

candidate_authority_digest() {
    local generation="$1" local_hex neo4j_hex
    local_hex="$(candidate_local_authority_digest "$generation")"
    neo4j_hex="$(candidate_neo4j_authority_digest "$generation")"
    python3 "$(authority_digest_tool)" combine "$local_hex" "$neo4j_hex"
}

candidate_up() {
    local generation="$1"
    local candidate_root="${BACKUP_ROOT}/candidate/${generation}"
    local state_root="${candidate_root}/state"
    [ -f "${BACKUP_ROOT}/candidate/${generation}/REHEARSAL-PASSED" ] \
        || { echo "candidate rehearsal marker missing" >&2; return 1; }
    # The production image runs as the fixed unprivileged UID/GID 10001.  The
    # candidate telemetry sink must therefore be writable by that identity while
    # remaining private from every other host user.
    install -d -o 10001 -g 10001 -m 0700 "${candidate_root}/probe-output/telemetry"
    candidate_compose "$generation" up -d --remove-orphans
    wait_healthy menhir-candidate-app menhir-candidate-neo4j
}

candidate_down() {
    local candidate_root="${BACKUP_ROOT}/candidate/$1"
    candidate_compose "$1" down
}

production_up() {
    MENHIR_COMPOSE_PROJECT=menhir-prod \
    MENHIR_RUNTIME_MODE=production \
    MENHIR_STATE_ROOT=/srv/menhir/production/state \
    MENHIR_AUTHORITIES_READ_ONLY=false \
    MENHIR_APP_CONTAINER=menhir-prod-app \
    MENHIR_NEO4J_CONTAINER=menhir-prod-neo4j \
    MENHIR_PROXY_IPV4=172.30.0.3 \
    MENHIR_PROXY_ALIAS=menhir-prod-app \
        compose_env menhir-prod up -d --remove-orphans
    wait_healthy menhir-prod-app menhir-prod-neo4j
}

production_down() {
    MENHIR_COMPOSE_PROJECT=menhir-prod \
    MENHIR_RUNTIME_MODE=production \
    MENHIR_STATE_ROOT=/srv/menhir/production/state \
    MENHIR_AUTHORITIES_READ_ONLY=false \
    MENHIR_APP_CONTAINER=menhir-prod-app \
    MENHIR_NEO4J_CONTAINER=menhir-prod-neo4j \
    MENHIR_PROXY_IPV4=172.30.0.3 \
    MENHIR_PROXY_ALIAS=menhir-prod-app \
        compose_env menhir-prod down
}

# ---------------------------------------------------------------------------
# Immutable release authority + receipts + mutation marker (blockers 3-7)
# ---------------------------------------------------------------------------

RELEASE_JSON="${MENHIR_RELEASE_JSON:-/srv/menhir/production/release/release.json}"

schema_py() {
    local helper_dir schema
    helper_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    schema="${helper_dir}/lib/menhir_schema.py"
    [ -f "$schema" ] || schema="${helper_dir}/menhir_schema.py"
    python3 "$schema" "$@"
}

validate_release_authority() {
    require_root_file "$RELEASE_JSON" "release.json"
    schema_py validate-release "$RELEASE_JSON"
}

validate_runtime_release_binding() {
    validate_release_authority
    [ -f "$COMPOSE_FILE" ] && [ ! -L "$COMPOSE_FILE" ] \
        || { echo "production compose artifact missing or symlinked" >&2; exit 1; }
    local policy="${MENHIR_PROD_POLICY_DIR:-${MENHIR_ROOT}/policy}/client-policy.json"
    [ -f "$policy" ] && [ ! -L "$policy" ] \
        || { echo "production client policy missing or symlinked" >&2; exit 1; }
    python3 - "$RELEASE_JSON" "$COMPOSE_FILE" "$policy" \
        "$PRODUCTION_ENV" \
        "${MENHIR_RELEASE_ID:?MENHIR_RELEASE_ID is required}" \
        "${MENHIR_RELEASE_COMMIT:?MENHIR_RELEASE_COMMIT is required}" \
        "$MENHIR_IMAGE" "$NEO4J_IMAGE" "${MENHIR_CLIENT_POLICY_DIGEST:?}" <<'PYEOF'
import hashlib, json, sys
release_path, compose_path, policy_path, production_env_path, release_id, commit, menhir, neo4j, policy_digest = sys.argv[1:10]
release=json.load(open(release_path, encoding="utf-8"))
policy=json.load(open(policy_path, encoding="utf-8"))
declared_policy_digest=policy.pop("canonical_digest", "")
actual_policy_digest=hashlib.sha256(json.dumps(
    policy, sort_keys=True, separators=(",", ":"), ensure_ascii=True
).encode("ascii")).hexdigest()
sha=lambda p: hashlib.sha256(open(p,"rb").read()).hexdigest()
checks=[
    (release["release_id"], release_id, "release id"),
    (release["repos"]["menhir"], commit, "Menhir commit"),
    (release["images"]["menhir"], menhir.rsplit("@",1)[-1], "Menhir image"),
    (release["images"]["neo4j"], neo4j.rsplit("@",1)[-1], "Neo4j image"),
    (release["rendered"]["menhir_compose_sha256"], sha(compose_path), "compose"),
    (release["rendered"]["policy_sha256"], sha(policy_path), "policy"),
    (release["rendered"]["production_env_sha256"], sha(production_env_path), "production env"),
    (policy_digest, declared_policy_digest, "configured policy"),
    (declared_policy_digest, actual_policy_digest, "canonical policy"),
]
for expected, actual, label in checks:
    if expected != actual:
        raise SystemExit("%s binding mismatch" % label)
PYEOF
}

validate_receipt_file() { # path kind
    require_root_file "$1" "$2 receipt"
    schema_py validate-receipt "$1" "$2"
}

validate_receipt_binding() { # path kind generation manifest_sha menhir_digest neo4j_digest
    local path="$1" kind="$2" generation="$3" manifest_sha="$4"
    local menhir_digest="$5" neo4j_digest="$6"
    validate_release_authority
    require_root_file "$path" "$kind receipt"
    schema_py validate-receipt-binding "$path" "$kind" "$RELEASE_JSON" \
        "$generation" "$manifest_sha" "$menhir_digest" "$neo4j_digest"
}

validate_external_prerequisite_binding() { # path
    local path="$1"
    require_root_file "$path" "external prerequisite receipt"
    validate_release_authority
    schema_py validate-prerequisite-binding "$path" "$RELEASE_JSON"
}

# The local backup wrapper must be a root-owned, regular, non-symlink, non-
# group/other-writable executable.
require_local_backup_wrapper() { # path
    local path="$1"
    [ -f "$path" ] && [ ! -L "$path" ] && [ -x "$path" ] \
        || { echo "local backup wrapper must be a regular executable file: $path" >&2; exit 1; }
    [ "$(stat -c '%u' "$path")" = 0 ] || { echo "local backup wrapper must be root-owned" >&2; exit 1; }
    local mode; mode="$(stat -c '%a' "$path")"
    # shellcheck disable=SC2016
    (( ((8#${mode}) & 8#022) == 0 )) \
        || { echo "local backup wrapper must not be group/other writable" >&2; exit 1; }
}

# Durable first-target-mutation marker (the point of no return).
mutation_marker="${STATUS_DIR}/first-mutation"

record_first_mutation() { # generation
    local generation="$1"
    install -d -o root -g root -m 0755 "$STATUS_DIR"
    python3 - "$mutation_marker" "$generation" <<'PYEOF'
import datetime, os, sys, tempfile
path, generation = sys.argv[1:3]
directory = os.path.dirname(path)
fd, tmp = tempfile.mkstemp(prefix=".first-mutation-", dir=directory, text=True)
try:
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(generation + "\n")
        handle.write("mutated_utc=" + datetime.datetime.now(datetime.timezone.utc).isoformat() + "\n")
        handle.flush(); os.fsync(handle.fileno())
    os.chmod(tmp, 0o400)
    os.replace(tmp, path)
    parent = os.open(directory, os.O_RDONLY)
    try: os.fsync(parent)
    finally: os.close(parent)
finally:
    if os.path.exists(tmp): os.unlink(tmp)
PYEOF
}

write_generation_record() { # path generation
    local path="$1" generation="$2"
    python3 - "$path" "$generation" <<'PYEOF'
import os, sys, tempfile
path, generation = sys.argv[1:3]
directory = os.path.dirname(path)
fd, tmp = tempfile.mkstemp(prefix=".generation-", dir=directory, text=True)
try:
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(generation + "\n"); handle.flush(); os.fsync(handle.fileno())
    os.chmod(tmp, 0o400); os.replace(tmp, path)
    parent=os.open(directory, os.O_RDONLY)
    try: os.fsync(parent)
    finally: os.close(parent)
finally:
    if os.path.exists(tmp): os.unlink(tmp)
PYEOF
}

first_mutation_occurred() {
    [ -f "$mutation_marker" ]
}

# Read the immutable current-generation record (single line, root-owned).
current_generation() {
    read_generation "${STATUS_DIR}/current-generation" "current generation"
}

# Receipts bound to the accepted generation; promotion parses these exact files
# and never consults mtime.
backup_receipt_path() { printf '%s\n' "${STATUS_DIR}/backup-local-receipt.json"; }
accept_receipt_path() { printf '%s\n' "${STATUS_DIR}/candidate-accept-receipt.json"; }
