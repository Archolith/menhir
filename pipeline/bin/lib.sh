#!/usr/bin/env bash
#
# Menhir production — shared constants and helpers.
#
# Root-owned. Sourced (not executed) by the fixed wrappers and the worker.
# Every path, compose project, service name, network, and timeout below is a
# fixed compile-time constant; these scripts never accept caller-supplied
# paths, projects, service names, shell fragments, URLs, or filters.
#
# Install to /srv/menhir/production/bin/lib.sh (mode 0644, root:root).

set -u

# Fixed production locations. Environment overrides exist solely for offline
# interface tests; on the host these always resolve to the fixed paths below.
MENHIR_ROOT="${MENHIR_ROOT:-/srv/menhir/production}"
MENHIR_COMPOSE_FILE="${MENHIR_ROOT}/deploy/docker-compose.production.yml"
MENHIR_COMPOSE_PROJECT="menhir-prod"
MENHIR_EXTERNAL_NETWORK="menhir-proxy"
MENHIR_SERVICE="menhir"
MENHIR_NEO4J_SERVICE="neo4j"

MENHIR_LOCK="${MENHIR_LOCK:-/run/lock/menhir-production.lock}"
MENHIR_ADMISSION_LOCK="${MENHIR_ADMISSION_LOCK:-/run/lock/menhir-production-admission.lock}"
MENHIR_STATUS_DIR="${MENHIR_STATUS_DIR:-/var/lib/menhir-production}"
MENHIR_JOB_DIR="${MENHIR_STATUS_DIR}/jobs"
MENHIR_MUTATION_DIR="${MENHIR_STATUS_DIR}/mutations"
MENHIR_BACKUP_DIR="${MENHIR_STATUS_DIR}/backups"
MENHIR_RECEIPT_DIR="${MENHIR_STATUS_DIR}/receipts"
MENHIR_LOG_DIR="/var/log/menhir-production"
MENHIR_UNIT_PREFIX="menhir-op"
MENHIR_ARM_FILE="${MENHIR_STATUS_DIR}/restore-armed"
MENHIR_RESTORE_SELECTION="${MENHIR_STATUS_DIR}/restore-selection"
MENHIR_FENCE_FILE="${MENHIR_STATUS_DIR}/fence"
MENHIR_RELEASE_JSON="${MENHIR_ROOT}/release/release.json"

# Fixed absolute operation scripts the worker dispatches to. These are the
# authoritative implementations and are owned by the host; the worker fails
# closed (require_script) when any is missing or not executable.
MENHIR_BACKUP_SCRIPT="${MENHIR_ROOT}/bin/backup-generation.sh"
MENHIR_RESTORE_SCRIPT="${MENHIR_ROOT}/bin/restore-generation.sh"
MENHIR_CANDIDATE_SCRIPT="${MENHIR_ROOT}/bin/candidate-deploy.sh"
MENHIR_CANDIDATE_ACCEPT_SCRIPT="${MENHIR_ROOT}/bin/candidate-accept.sh"
MENHIR_PROMOTE_SCRIPT="${MENHIR_ROOT}/bin/promote.sh"
MENHIR_ROLLBACK_SCRIPT="${MENHIR_ROOT}/bin/rollback.sh"

# Caddy route authority (shared yawn proxy), with root-managed immutable
# candidate/previous release directories.
MENHIR_CADDY_RELEASE_SCRIPT="${MENHIR_ROOT}/bin/caddy-release.sh"
MENHIR_ROUTE_CANDIDATE_DIR="/srv/yawn/releases/menhir-route-candidate"
MENHIR_ROUTE_PREVIOUS_DIR="/srv/yawn/releases/menhir-route-previous"
MENHIR_PREVIOUS_RELEASE_JSON="${MENHIR_ROOT}/release/previous-release.json"
MENHIR_PREVIOUS_PREREQ_RECEIPT="${MENHIR_STATUS_DIR}/previous-external-prerequisite.json"

# Root-managed, immutable caddy release reconcile/status evidence. It must be
# present, root-owned, non-symlink, not group/other writable, non-empty, and
# fresh before any caddy-route recovery transition reopens the fence.
MENHIR_CADDY_RECONCILE_EVIDENCE="${MENHIR_STATUS_DIR}/caddy-reconcile.json"
MENHIR_CADDY_CURRENT_LINK="${MENHIR_ROOT}/caddy/current"

# Immutable strict-schema validation tool. Receipts must pass validate-receipt
# and validate-receipt-binding here before any dependent mutation is dispatched.
MENHIR_SCHEMA_TOOL="${MENHIR_SCHEMA_TOOL:-${MENHIR_ROOT}/bin/menhir_schema.py}"

# Artifact verifier used by recovery to establish cryptographic/artifact
# evidence. Resolves to the fixed installed verifier on the host; the
# environment override exists solely for offline interface tests.
VERIFY_ARTIFACTS_BIN="${VERIFY_ARTIFACTS_BIN:-${MENHIR_ROOT}/bin/verify-artifacts}"

log() {
    printf '[menhir %s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

now_iso() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

# Deadline ISO timestamp, now + $1 seconds. GNU date assumed (Linux VPS).
deadline_iso() {
    date -u -d "+${1} seconds" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo ""
}

# Fixed compose invocation (argument-vector style; only fixed subcommands).
compose() {
    docker compose --project-name "${MENHIR_COMPOSE_PROJECT}" \
        --file "${MENHIR_COMPOSE_FILE}" "$@"
}

# Phase-specific runtime cap (RuntimeMaxSec) per operation, in seconds.
op_timeout() {
    case "$1" in
        backup|restore-rehearsal|restore-production) echo 21600 ;;
        candidate-deploy|candidate-accept) echo 1800 ;;
        promote|rollback) echo 600 ;;
        caddy-route-apply|caddy-route-rollback) echo 300 ;;
        *) echo 600 ;;
    esac
}

ensure_status_dirs() {
    install -d -o root -g root -m 0755 "${MENHIR_STATUS_DIR}"
    install -d -o root -g root -m 0755 "${MENHIR_JOB_DIR}"
    install -d -o root -g root -m 0755 "${MENHIR_MUTATION_DIR}"
    install -d -o root -g root -m 0755 "${MENHIR_BACKUP_DIR}"
    install -d -o root -g root -m 0755 "${MENHIR_RECEIPT_DIR}"
    install -d -o root -g root -m 0755 "${MENHIR_LOG_DIR}"
    # Fail closed: a missing fence is recovery-required, never "open". Root
    # must run `recover` once to admit the first operation after installation.
    [ -f "${MENHIR_FENCE_FILE}" ] \
        || printf 'closed missing-at-init (recovery required)\n' \
        | persist_atomic "${MENHIR_FENCE_FILE}"
}

# Atomically persist state: write complete content to a temp file in the same
# directory, fsync it, rename it into place, and fsync the file again. A
# concurrent reader or a crash mid-write observes either the previous complete
# content or the new complete content — never a torn fence/status record.
persist_atomic() {
    local target="$1" tmp
    tmp="${target}.tmp.$$"
    cat > "${tmp}" || { rm -f "${tmp}"; return 1; }
    sync "${tmp}" 2>/dev/null || true
    if ! mv -f "${tmp}" "${target}"; then
        rm -f "${tmp}"
        return 1
    fi
    sync "${target}" 2>/dev/null || true
}

# Fail closed if a required fixed script is missing or not executable.
require_script() {
    local path="$1"
    if [ ! -x "${path}" ]; then
        echo "error: required fixed script missing or not executable: ${path}" >&2
        return 1
    fi
}

# Fail closed if the operation lock is currently held by another process.
lock_busy() {
    exec 9>"${MENHIR_LOCK}"
    if ! flock -n 9; then
        echo "LOCK BUSY: ${MENHIR_LOCK} is held by another Menhir operation"
        exit 75
    fi
    flock -u 9
    exec 9>&-
}

# Read a root-owned, single-line generation id from a fixed file (selection,
# arming). Validates owner 0, non-symlink, non-writable, and a strict
# [A-Za-z0-9._-]+ id. Never a caller path.
read_root_generation_file() {
    local path="$1" label="$2"
    if [ ! -f "${path}" ]; then
        echo "error: missing ${label} file ${path}" >&2
        return 1
    fi
    if [ -L "${path}" ]; then
        echo "error: ${label} file ${path} must not be a symlink" >&2
        return 1
    fi
    if [ "$(stat -c '%u' "${path}" 2>/dev/null || echo -1)" != "0" ]; then
        echo "error: ${label} file ${path} must be root-owned" >&2
        return 1
    fi
    [ "$(wc -l < "${path}")" -eq 1 ] \
        || { echo "error: ${label} file must contain exactly one line" >&2; return 1; }
    local value
    value="$(sed -n '1p' "${path}" | tr -d '\r\n')"
    if [ -z "${value}" ] || ! printf '%s' "${value}" | grep -Eq '^[A-Za-z0-9._-]+$'; then
        echo "error: invalid generation id in ${label} file ${path}" >&2
        return 1
    fi
    printf '%s' "${value}"
}

# ---------------------------------------------------------------------------
# Maintenance fence (fail-closed admission across races/timeout/signal/crash)
# ---------------------------------------------------------------------------

# A missing, unreadable, or corrupt fence file is recovery-required (closed),
# never "open". Only the exact literal state "open" admits mutations.
fence_state() {
    local state
    state="$(cat "${MENHIR_FENCE_FILE}" 2>/dev/null | tr -d '\r\n')" || true
    case "${state}" in
        open) printf 'open\n' ;;
        "")
            printf 'closed missing-or-corrupt-fence (recovery required)\n'
            ;;
        *) printf '%s\n' "${state}" ;;
    esac
}
fence_open()  { printf 'open\n' | persist_atomic "${MENHIR_FENCE_FILE}"; }
fence_close() { printf 'closed %s\n' "$*" | persist_atomic "${MENHIR_FENCE_FILE}"; }

# Refuse any mutation while the fence is closed. A closed fence persists across
# worker crash/kill/timeout and blocks until root runs `recover` (explicit safe
# recovery evidence: lock free, no active units, artifacts verified).
fence_check() {
    local state
    state="$(fence_state)"
    case "${state}" in
        open) return 0 ;;
        *)
            echo "REFUSED: maintenance fence closed: ${state}. Recovery required (root: run recover)." >&2
            exit 69
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Persisted job identity and phase
# ---------------------------------------------------------------------------

# Persist a per-operation record to the host status authority. principal/pid/
# pgid/started/deadline are read from the worker's exported environment;
# heartbeat and updated are refreshed on every call.
mark_phase() {
    local op="$1" job_id="$2" phase="$3" detail="${4:-}" recovery_required="${5:-0}"
    local now
    now="$(now_iso)"
    printf 'operation=%s\njob_id=%s\nprincipal=%s\npid=%s\npgid=%s\nstarted=%s\nheartbeat=%s\ndeadline=%s\nphase=%s\nrecovery_required=%s\ndetail=%s\nupdated=%s\n' \
        "${op}" "${job_id}" "${MENHIR_PRINCIPAL:-root}" "${MENHIR_PID:-0}" "${MENHIR_PGID:-0}" \
        "${MENHIR_STARTED:-}" "${now}" "${MENHIR_DEADLINE:-}" \
        "${phase}" "${recovery_required}" "${detail}" "${now}" \
        | persist_atomic "${MENHIR_JOB_DIR}/${op}.status"
}

# Reconcile the previous operation before admitting a new mutation. If the
# previous operation did not reach a terminal (done/failed) state — e.g. the
# worker was killed and left a stale "running" record — refuse and set the fence.
reconcile_previous() {
    local op="$1"
    local phase
    phase="$(sed -n 's/^phase=//p' "${MENHIR_JOB_DIR}/${op}.status" 2>/dev/null | tail -n1)"
    case "${phase}" in
        done|failed) return 0 ;;
        running|enqueued)
            echo "REFUSED: previous ${op} operation is not reconciled (phase=${phase}); recovery required." >&2
            fence_close "${op} unreconciled (phase=${phase})"
            exit 69
            ;;
        *) return 0 ;;
    esac
}

gen_current()   { cat "${MENHIR_STATUS_DIR}/current-generation"   2>/dev/null || true; }
gen_previous()  { cat "${MENHIR_STATUS_DIR}/previous-generation"  2>/dev/null || true; }
gen_candidate() { cat "${MENHIR_STATUS_DIR}/candidate-generation" 2>/dev/null || true; }

# ---------------------------------------------------------------------------
# Immutable release record and operation receipts (cross-repo gates)
# ---------------------------------------------------------------------------

# Require the root-owned, immutable release.json (mode <= 0444, regular file,
# not a symlink). Restore/route/promote/rollback refuse to run without it.
require_release_record() {
    local f="${MENHIR_RELEASE_JSON}"
    if [ ! -f "${f}" ]; then
        echo "error: missing immutable release record ${f}" >&2
        return 1
    fi
    if [ -L "${f}" ]; then
        echo "error: ${f} must not be a symlink" >&2
        return 1
    fi
    if [ "$(stat -c '%u' "${f}" 2>/dev/null || echo -1)" != "0" ]; then
        echo "error: ${f} must be root-owned" >&2
        return 1
    fi
    local mode
    mode="$(stat -c '%a' "${f}" 2>/dev/null || echo 777)"
    if (( (8#${mode}) & 8#022 )); then
        echo "error: ${f} must not be group/other writable" >&2
        return 1
    fi
    return 0
}

# Require a receipt produced by a fixed operation script. Receipts are
# root-owned files under MENHIR_RECEIPT_DIR (never a caller path). Beyond the
# file checks below, the receipt must pass strict schema validation
# (validate-receipt) and binding validation (validate-receipt-binding: release,
# generation, images, artifacts, freshness) via the immutable Menhir schema
# tool, executed while holding the host-wide operation lock so validation and
# the immediately-following mutation are serialized against every other writer.
require_receipt() {
    local name="$1"
    local f
    case "${name}" in
        backup) f="${MENHIR_STATUS_DIR}/backup-local-receipt.json" ;;
        rehearsal) f="${MENHIR_STATUS_DIR}/rehearsal-receipt.json" ;;
        candidate) f="${MENHIR_STATUS_DIR}/candidate-accept-receipt.json" ;;
        source-fence) f="${MENHIR_STATUS_DIR}/source-writer-fence.json" ;;
        external-prerequisite) f="${MENHIR_STATUS_DIR}/external-prerequisite.json" ;;
        *) echo "error: unknown fixed receipt name: ${name}" >&2; return 1 ;;
    esac
    if [ ! -f "${f}" ]; then
        echo "error: required receipt missing: ${f}" >&2
        return 1
    fi
    if [ -L "${f}" ]; then
        echo "error: ${f} must not be a symlink" >&2
        return 1
    fi
    if [ "$(stat -c '%u' "${f}" 2>/dev/null || echo -1)" != "0" ]; then
        echo "error: ${f} must be root-owned" >&2
        return 1
    fi
    local mode
    mode="$(stat -c '%a' "${f}" 2>/dev/null || echo 777)"
    if (( (8#${mode}) & 8#022 )); then
        echo "error: ${f} must not be group/other writable" >&2
        return 1
    fi
    # Strict schema/binding gate. The schema tool is itself part of the
    # verified artifact set (verify-artifacts), so it cannot be substituted.
    if [ ! -f "${MENHIR_SCHEMA_TOOL}" ] || [ -L "${MENHIR_SCHEMA_TOOL}" ]; then
        echo "error: strict receipt schema tool missing or not a regular file: ${MENHIR_SCHEMA_TOOL}" >&2
        return 1
    fi
    exec 7>"${MENHIR_LOCK}"
    if ! flock -n 7; then
        echo "REFUSED: receipt validation for ${name}: operation lock busy" >&2
        exit 75
    fi
    local ok=1
    case "${name}" in
        source-fence)
            python3 "${MENHIR_SCHEMA_TOOL}" verify-source-fence "${f}" \
                "${MENHIR_RELEASE_JSON}" \
                || { echo "error: source fence failed strict validation: ${f}" >&2; ok=0; }
            ;;
        external-prerequisite)
            python3 "${MENHIR_SCHEMA_TOOL}" validate-prerequisite-binding \
                "${f}" "${MENHIR_RELEASE_JSON}" \
                || { echo "error: external prerequisite failed binding validation: ${f}" >&2; ok=0; }
            ;;
        backup|rehearsal|candidate)
            local kind
            case "${name}" in
                backup) kind="backup-local" ;;
                rehearsal) kind="rehearsal" ;;
                candidate) kind="candidate-accept" ;;
            esac
            python3 "${MENHIR_SCHEMA_TOOL}" validate-receipt "${f}" "${kind}" \
                || { echo "error: receipt failed strict schema/freshness validation: ${f}" >&2; ok=0; }
            if [ "${ok}" -eq 1 ]; then
                # Extract binding fields from the already-schema-validated
                # receipt and the immutable release authority via JSON parsing
                # (never eval; no caller-controlled path).
                local extracted
                extracted="$(python3 - "${f}" "${MENHIR_RELEASE_JSON}" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        receipt = json.load(fh)
    with open(sys.argv[2], encoding="utf-8") as fh:
        release = json.load(fh)
    fields = [
        receipt["generation"],
        receipt["manifest_sha256"],
        release["images"]["menhir"],
        release["images"]["neo4j"],
    ]
except Exception:
    sys.exit(1)
if any(not isinstance(v, str) or not v or "\n" in v for v in fields):
    sys.exit(1)
sys.stdout.write("\n".join(fields) + "\n")
PYEOF
                )" || {
                    echo "error: malformed receipt/release record; cannot extract binding fields: ${f}" >&2
                    ok=0
                }
                if [ "${ok}" -eq 1 ]; then
                    local generation manifest_sha256 menhir_digest neo4j_digest
                    { read -r generation; read -r manifest_sha256; read -r menhir_digest; read -r neo4j_digest; } <<< "${extracted}"
                    python3 "${MENHIR_SCHEMA_TOOL}" validate-receipt-binding \
                        "${f}" "${kind}" "${MENHIR_RELEASE_JSON}" \
                        "${generation}" "${manifest_sha256}" \
                        "${menhir_digest}" "${neo4j_digest}" \
                        || { echo "error: receipt failed binding (release/generation/images/artifacts) validation: ${f}" >&2; ok=0; }
                fi
            fi
            ;;
    esac
    flock -u 7
    exec 7>&-
    [ "${ok}" -eq 1 ] || return 1
    return 0
}

# Validate the immutable receipt for one already-validated generation selected
# from the fixed root-owned restore-selection file. This is intentionally not
# the singleton latest-backup receipt: retained older generations must remain
# independently restorable.
require_generation_backup_receipt() { # generation.<alnum>
    local generation="$1" f mode extracted manifest_sha menhir_digest neo4j_digest
    [[ "${generation}" =~ ^generation\.[A-Za-z0-9]+$ ]] \
        || { echo "error: invalid generation for backup receipt" >&2; return 1; }
    f="${MENHIR_STATUS_DIR}/backup-receipts/${generation}.json"
    if [ -L "${f}" ] || [ ! -f "${f}" ] \
            || [ "$(stat -c '%u' "${f}" 2>/dev/null || echo -1)" != "0" ]; then
        echo "error: immutable generation backup receipt is missing or unsafe: ${f}" >&2
        return 1
    fi
    mode="$(stat -c '%a' "${f}" 2>/dev/null || echo 777)"
    (( ((8#${mode}) & 8#022) == 0 )) \
        || { echo "error: generation backup receipt is group/other writable" >&2; return 1; }
    require_release_record || return 1
    exec 7>"${MENHIR_LOCK}"
    if ! flock -n 7; then
        echo "REFUSED: generation backup receipt validation: operation lock busy" >&2
        exit 75
    fi
    local ok=1
    python3 "${MENHIR_SCHEMA_TOOL}" validate-receipt "${f}" backup-local \
        || { echo "error: generation backup receipt schema failed: ${f}" >&2; ok=0; }
    if [ "${ok}" -eq 1 ]; then
        extracted="$(python3 - "${f}" "${MENHIR_RELEASE_JSON}" "${generation}" <<'PYEOF'
import json,sys
with open(sys.argv[1],encoding="utf-8") as handle: receipt=json.load(handle)
with open(sys.argv[2],encoding="utf-8") as handle: release=json.load(handle)
if receipt.get("generation")!=sys.argv[3]: raise SystemExit(1)
fields=(receipt.get("manifest_sha256"),release["images"]["menhir"],release["images"]["neo4j"])
if any(not isinstance(v,str) or not v or "\n" in v for v in fields): raise SystemExit(1)
print("\n".join(fields))
PYEOF
        )" || ok=0
    fi
    if [ "${ok}" -eq 1 ]; then
        { read -r manifest_sha; read -r menhir_digest; read -r neo4j_digest; } <<<"${extracted}"
        python3 "${MENHIR_SCHEMA_TOOL}" validate-receipt-binding \
            "${f}" backup-local "${MENHIR_RELEASE_JSON}" "${generation}" \
            "${manifest_sha}" "${menhir_digest}" "${neo4j_digest}" || ok=0
    fi
    flock -u 7
    exec 7>&-
    [ "${ok}" -eq 1 ]
}

# ---------------------------------------------------------------------------
# Artifact integrity
# ---------------------------------------------------------------------------

# Verify a single root-executed artifact: owner 0, regular non-symlink, not
# group/other writable, and digest matches. GNU coreutils assumed (Linux VPS).
verify_artifact() {
    local path="$1" expected="$2"
    local owner perms actual
    if [ -L "${path}" ]; then
        echo "error: ${path} must not be a symlink" >&2; return 1
    fi
    if [ ! -f "${path}" ]; then
        echo "error: ${path} missing" >&2; return 1
    fi
    owner="$(stat -c '%u' "${path}" 2>/dev/null || echo -1)"
    perms="$(stat -c '%a' "${path}" 2>/dev/null || echo 777)"
    if [ "${owner}" != "0" ]; then
        echo "error: ${path} must be root-owned" >&2; return 1
    fi
    if [ $(( 8#${perms} & 8#022 )) -ne 0 ]; then
        echo "error: ${path} must not be group/other writable" >&2; return 1
    fi
    actual="$(sha256sum "${path}" 2>/dev/null | awk '{print $1}')"
    if [ "${actual}" != "${expected}" ]; then
        echo "error: ${path} digest mismatch" >&2; return 1
    fi
    return 0
}

# Submit a mutating operation: verify the fence (fail closed), reconcile the
# previous operation, probe the lock (fail closed when busy), persist an
# "enqueued" record, and enqueue a named systemd transient unit that runs the
# fixed worker. Returns the host job/generation ID on stdout.
submit_op() {
    local op="$1"
    ensure_status_dirs
    exec 8>"${MENHIR_ADMISSION_LOCK}"
    flock -n 8 || { echo "ADMISSION BUSY: another Menhir submission is in progress" >&2; exit 75; }
    fence_check
    reconcile_previous "${op}"
    # The operation lock is owned later by the fixed implementation script.
    # The distinct admission lock remains held through fence closure and unit
    # creation, making concurrent submit admission atomic.
    local job_id unit timeout
    job_id="$(date -u +%Y%m%dT%H%M%SZ)-$((RANDOM % 10000))"
    unit="${MENHIR_UNIT_PREFIX}-${op}-${job_id}"
    timeout="$(op_timeout "${op}")"
    mark_phase "${op}" "${job_id}" enqueued
    fence_close "${op} enqueued (${job_id})"
    # Admission is only possible while the fence is open, which means every
    # prior operation for this op reached a reconciled terminal state; drop any
    # stale mutation-started evidence from a previous run so a fresh job starts
    # clean. During its own run the worker re-creates this marker (with this
    # job_id) immediately before launching the fixed mutation script.
    rm -f "${MENHIR_MUTATION_DIR}/${op}.mutated"
    systemd-run --unit="${unit}" --collect \
        --property=Type=oneshot \
        --property=RuntimeMaxSec="${timeout}" \
        --property=StandardOutput=journal \
        --property=StandardError=journal \
        "${MENHIR_ROOT}/bin/worker" "${op}" "${job_id}"
    flock -u 8
    exec 8>&-
    printf 'JOB_ID=%s\nUNIT=%s\n' "${job_id}" "${unit}"
}

# ---------------------------------------------------------------------------
# Durable pre-mutation vs mutation-started evidence
# ---------------------------------------------------------------------------

# Durably record that a fixed mutation script is about to be launched. The
# marker is persisted atomically (persist_atomic, then fsynced) BEFORE the
# mutation script is dispatched, so a SIGKILL/timeout/systemd kill cannot erase
# it. Recovery uses its presence (with a matching job_id) to prove a mutation
# was started and therefore keeps the fence closed; its absence -- together with
# a terminal worker-written "pre-mutation failure" record -- is exactly what
# proves a failure happened before any mutation.
mark_mutation_started() {
    local op="$1" job_id="$2"
    install -d -o root -g root -m 0755 "${MENHIR_MUTATION_DIR}"
    printf 'operation=%s\njob_id=%s\nphase=mutation-started\npid=%s\nstarted=%s\n' \
        "${op}" "${job_id}" "${MENHIR_PID:-0}" "$(now_iso)" \
        | persist_atomic "${MENHIR_MUTATION_DIR}/${op}.mutated"
}

# Worker helper: transition to the mutation-started phase. Sets the in-memory
# MUTATION_BEGUN guard FIRST, then durably persists the mutation-started
# evidence. A failure to persist the evidence still counts as mutation-started
# (conservative: the outcome is treated as post-mutation/ambiguous).
mutation_begun() {
    MUTATION_BEGUN=1
    mark_mutation_started "$1" "$2"
}

# ---------------------------------------------------------------------------
# Fail-closed, operation-specific recovery
# ---------------------------------------------------------------------------

# True for the caddy route operations, which share the yawn proxy release
# authority and therefore require caddy release reconcile/status evidence
# before any recovery transition.
is_caddy_op() {
    case "$1" in
        caddy-route-apply|caddy-route-rollback) return 0 ;;
        *) return 1 ;;
    esac
}

# Name of the operation-specific reconciler required to unblock recovery. Caddy
# route work must first reconcile the caddy release and supply fresh evidence.
op_reconciler_name() {
    case "$1" in
        caddy-route-apply|caddy-route-rollback)
            printf 'reconcile-caddy-route: run caddy-release reconcile/status and provide root-owned fresh reconcile/status evidence (%s)' "${MENHIR_CADDY_RECONCILE_EVIDENCE}"
            ;;
        restore-production|restore-rehearsal)
            printf 'reconcile-restore: repair the %s state and write a reconciled (done) record, then rerun recover' "$1"
            ;;
        *)
            printf 'reconcile-%s: repair the %s state and write a reconciled (done) record, then rerun recover' "$1" "$1"
            ;;
    esac
}

# Validate the externally observable postcondition for a completed operation.
# A worker-written `phase=done` is never sufficient by itself: recovery and the
# worker's own success path both call this function before opening the fence.
# Every check uses fixed root-owned paths, strict receipt binding, and fixed
# container identities; no operator-provided path or command is accepted.
secure_generation_value() { # fixed_path label
    read_root_generation_file "$1" "$2"
}

receipt_generation() { # fixed_receipt_path
    python3 - "$1" <<'PYEOF'
import json,re,sys
with open(sys.argv[1], encoding="utf-8") as handle:
    value=json.load(handle)
generation=value.get("generation", "")
if not isinstance(generation,str) or not re.fullmatch(r"generation\.[A-Za-z0-9]+",generation):
    raise SystemExit(1)
print(generation)
PYEOF
}

receipt_operation_job_id() { # fixed_receipt_path
    python3 - "$1" <<'PYEOF'
import json,re,sys
with open(sys.argv[1], encoding="utf-8") as handle:
    value=json.load(handle)
job_id=value.get("operation_job_id", "")
if not isinstance(job_id,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",job_id):
    raise SystemExit(1)
print(job_id)
PYEOF
}

fixed_container_healthy() {
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$1" 2>/dev/null || true)" = "healthy" ]
}

restore_anchor_evidence() { # generation
    local generation="$1" root="${MENHIR_STATUS_DIR}/pre-restore-anchors" path mode found=0
    [ ! -e "${MENHIR_STATUS_DIR}/restore-journal.json" ] \
        || { echo "error: unfinished restore transaction journal exists" >&2; return 1; }
    [ -d "$root" ] && [ ! -L "$root" ] || return 1
    for path in "$root"/restore-*.json; do
        [ -e "$path" ] || continue
        [ -f "$path" ] && [ ! -L "$path" ] \
            && [ "$(stat -c '%u' "$path" 2>/dev/null || echo -1)" = "0" ] || continue
        mode="$(stat -c '%a' "$path" 2>/dev/null || echo 777)"
        (( ((8#$mode) & 8#022) == 0 )) || continue
        if python3 "${MENHIR_ROOT}/bin/restore_authority_txn.py" validate-anchor \
                "$path" "$generation"; then
            found=$((found + 1))
        fi
    done
    [ "$found" -ge 1 ]
}

operation_completion_evidence() { # operation job_id
    local op="$1" job_id="$2" generation receipt_gen receipt_job selected marker
    require_release_record || return 1
    case "${op}" in
        backup)
            require_receipt backup || return 1
            receipt_gen="$(receipt_generation "${MENHIR_STATUS_DIR}/backup-local-receipt.json")" || return 1
            receipt_job="$(receipt_operation_job_id "${MENHIR_STATUS_DIR}/backup-local-receipt.json")" || return 1
            [ "${receipt_job}" = "${job_id}" ] || return 1
            [ -n "${receipt_gen}" ] || return 1
            ;;
        restore-rehearsal)
            selected="$(secure_generation_value "${MENHIR_RESTORE_SELECTION}" "restore selection")" || return 1
            require_generation_backup_receipt "${selected}" && require_receipt rehearsal || return 1
            receipt_gen="$(receipt_generation "${MENHIR_STATUS_DIR}/rehearsal-receipt.json")" || return 1
            [ "${selected}" = "${receipt_gen}" ] || return 1
            ;;
        restore-production)
            selected="$(secure_generation_value "${MENHIR_RESTORE_SELECTION}" "restore selection")" || return 1
            require_generation_backup_receipt "${selected}" && require_receipt rehearsal || return 1
            generation="$(secure_generation_value "${MENHIR_STATUS_DIR}/restored-generation" "restored generation")" || return 1
            marker="$(secure_generation_value "${MENHIR_STATUS_DIR}/current-generation" "current generation")" || return 1
            [ "${selected}" = "${generation}" ] && [ "${generation}" = "${marker}" ] || return 1
            restore_anchor_evidence "${generation}" || return 1
            ;;
        candidate-deploy)
            generation="$(secure_generation_value "${MENHIR_STATUS_DIR}/candidate-generation" "candidate generation")" || return 1
            selected="$(secure_generation_value "${MENHIR_RESTORE_SELECTION}" "restore selection")" || return 1
            [ "${generation}" = "${selected}" ] || return 1
            fixed_container_healthy menhir-candidate-app \
                && fixed_container_healthy menhir-candidate-neo4j || return 1
            ;;
        candidate-accept)
            require_receipt candidate || return 1
            generation="$(secure_generation_value "${MENHIR_STATUS_DIR}/candidate-generation" "candidate generation")" || return 1
            marker="$(secure_generation_value "${MENHIR_STATUS_DIR}/candidate-accepted" "candidate accepted")" || return 1
            receipt_gen="$(receipt_generation "${MENHIR_STATUS_DIR}/candidate-accept-receipt.json")" || return 1
            [ "${generation}" = "${marker}" ] && [ "${marker}" = "${receipt_gen}" ] || return 1
            fixed_container_healthy menhir-candidate-app \
                && fixed_container_healthy menhir-candidate-neo4j || return 1
            ;;
        promote)
            require_receipt candidate || return 1
            generation="$(secure_generation_value "${MENHIR_STATUS_DIR}/candidate-generation" "candidate generation")" || return 1
            marker="$(secure_generation_value "${MENHIR_STATUS_DIR}/current-generation" "current generation")" || return 1
            [ "${generation}" = "${marker}" ] || return 1
            fixed_container_healthy menhir-prod-app \
                && fixed_container_healthy menhir-prod-neo4j || return 1
            ! docker inspect menhir-candidate-app >/dev/null 2>&1 || return 1
            ;;
        rollback)
            generation="$(secure_generation_value "${MENHIR_STATUS_DIR}/candidate-generation" "candidate generation")" || return 1
            marker="$(secure_generation_value "${MENHIR_STATUS_DIR}/rolled-back-generation" "rolled-back generation")" || return 1
            [ "${generation}" = "${marker}" ] || return 1
            fixed_container_healthy menhir-candidate-app \
                && fixed_container_healthy menhir-candidate-neo4j || return 1
            ! docker inspect menhir-prod-app >/dev/null 2>&1 || return 1
            ;;
        caddy-route-apply)
            caddy_reconcile_evidence || return 1
            ;;
        caddy-route-rollback)
            caddy_reconcile_evidence "${MENHIR_PREVIOUS_RELEASE_JSON}" || return 1
            ;;
        *) echo "error: no completion reconciler for operation ${op}" >&2; return 1 ;;
    esac
    return 0
}

# True when any menhir-op transient unit is active. Only active units are
# listed: transient units are created with --collect, so lingering inactive
# units are garbage. An active unit means a worker (or its systemd scope) is
# still live and recovery must not proceed. Overridable in offline tests.
menhir_units_active() {
    systemctl list-units --no-pager --no-legend "${MENHIR_UNIT_PREFIX}-*" 2>/dev/null | grep -q .
}

# True when any descendant of $1 is a live process. Best-effort recursion; the
# recorded process-group check in recorded_worker_alive covers orphaned
# mutation scripts reparented away from a killed worker.
process_tree_alive() {
    local pid="$1" child
    [ -n "${pid}" ] && [ "${pid}" != "0" ] || return 1
    if kill -0 "${pid}" 2>/dev/null; then
        return 0
    fi
    while read -r child; do
        [ -n "${child}" ] || continue
        if process_tree_alive "${child}"; then
            return 0
        fi
    done < <(pgrep -P "${pid}" 2>/dev/null || true)
    return 1
}

# True while any recorded worker pid or process group from a non-terminal job
# is still alive. SIGKILL of a worker releases the flock (kernel-held) and may
# leave the transient unit gone, but an orphaned mutation script would still be
# mutating: the lock gate plus this descendant/process-group gate block any
# recovery until the stray process is resolved.
recorded_worker_alive() {
    local op phase pid pgid
    for st in "${MENHIR_JOB_DIR}"/*.status; do
        [ -e "${st}" ] || continue
        op="$(basename "${st}" .status)"
        phase="$(sed -n 's/^phase=//p' "${st}" 2>/dev/null | tail -n1)"
        case "${phase}" in
            running|enqueued) : ;;
            *) continue ;;
        esac
        pid="$(sed -n 's/^pid=//p' "${st}" 2>/dev/null | tail -n1)"
        pgid="$(sed -n 's/^pgid=//p' "${st}" 2>/dev/null | tail -n1)"
        if [ -n "${pid}" ] && [ "${pid}" != "0" ] && kill -0 "${pid}" 2>/dev/null; then
            return 0
        fi
        if [ -n "${pgid}" ] && [ "${pgid}" != "0" ] && kill -0 "-${pgid}" 2>/dev/null; then
            return 0
        fi
        if process_tree_alive "${pid}"; then
            return 0
        fi
    done
    return 1
}

# Classify one persisted job for generic recovery. A job may be reopened by
# generic recover ONLY when it is an explicitly proven pre-mutation failure:
# the worker (itself part of the artifact-verified set) reached terminal
# "failed", declared recovery_required=0 with detail "pre-mutation failure",
# and no durable mutation-started evidence exists for the current job_id.
# "done" jobs are reconciled. Every other state -- running/enqueued
# (SIGKILL/timeout stale), missing/corrupt phase, or any job with current
# mutation-started evidence -- is ambiguous: the fence stays closed and the
# operation-specific reconciler is named. Results in RECOVER_STATE
# ("reconciled" | "safe" | "refused") and RECOVER_REASON.
mark_reconciled_phase() { # status_path operation job_id
    local status="$1" op="$2" job_id="$3" now
    now="$(now_iso)"
    python3 - "$status" "$op" "$job_id" "$now" <<'PYEOF' \
        | persist_atomic "$status"
import re,sys
path,expected_op,expected_job,now=sys.argv[1:5]
keys=("operation","job_id","principal","pid","pgid","started","heartbeat",
      "deadline","phase","recovery_required","detail","updated")
values={}
with open(path,encoding="utf-8") as handle:
    for raw in handle:
        line=raw.rstrip("\n")
        if "=" not in line: raise SystemExit("invalid status line")
        key,value=line.split("=",1)
        if key in values: raise SystemExit("duplicate status key")
        values[key]=value
if set(values)!=set(keys): raise SystemExit("status keys mismatch")
if values["operation"]!=expected_op or values["job_id"]!=expected_job:
    raise SystemExit("status identity mismatch")
if not re.fullmatch(r"[a-z][a-z0-9-]*",expected_op) or not re.fullmatch(r"[A-Za-z0-9._-]+",expected_job):
    raise SystemExit("status identity invalid")
values.update(phase="done",recovery_required="0",
              detail="completion evidence reconciled",heartbeat=now,updated=now)
for key in keys: print(f"{key}={values[key]}")
PYEOF
}

recover_classify_op() {
    local op="$1"
    RECOVER_STATE="refused"
    RECOVER_REASON=""
    local st="${MENHIR_JOB_DIR}/${op}.status"
    if [ -L "${st}" ] || [ ! -f "${st}" ]; then
        RECOVER_REASON="status record missing or not a regular file: ${st}"
        return 1
    fi
    if [ "$(stat -c '%u' "${st}" 2>/dev/null || echo -1)" != "0" ]; then
        RECOVER_REASON="status record must be root-owned: ${st}"
        return 1
    fi
    local mode
    mode="$(stat -c '%a' "${st}" 2>/dev/null || echo 777)"
    if (( (8#${mode}) & 8#022 )); then
        RECOVER_REASON="status record must not be group/other writable: ${st}"
        return 1
    fi
    local phase job_id
    phase="$(sed -n 's/^phase=//p' "${st}" 2>/dev/null | tail -n1)"
    job_id="$(sed -n 's/^job_id=//p' "${st}" 2>/dev/null | tail -n1)"
    case "${phase}" in
        "") RECOVER_REASON="phase missing/corrupt in ${st}"; return 1 ;;
        done)
            if operation_completion_evidence "${op}" "${job_id}"; then
                RECOVER_STATE="reconciled"
                return 0
            fi
            RECOVER_REASON="phase=done lacks valid operation-specific completion evidence"
            return 1
            ;;
    esac
    # A worker may be killed after the fixed operation reached its durable
    # postcondition but before it wrote phase=done. Re-run the operation-specific
    # evidence reconciler first. Only exact independently verified completion
    # may convert a stale/post-mutation record into done; otherwise classification
    # continues fail-closed below.
    if operation_completion_evidence "${op}" "${job_id}"; then
        if ! mark_reconciled_phase "${st}" "${op}" "${job_id}"; then
            RECOVER_REASON="completion evidence passed but the status record could not be reconciled"
            return 1
        fi
        rm -f "${MENHIR_MUTATION_DIR}/${op}.mutated"
        RECOVER_STATE="reconciled"
        return 0
    fi
    # Durable mutation-started evidence for the CURRENT job id => the mutation
    # script was launched, so the outcome is post-mutation and unknown. A marker
    # whose job_id does not match the status record is stale evidence from a
    # prior run and is ignored.
    local marker_job
    if [ -f "${MENHIR_MUTATION_DIR}/${op}.mutated" ]; then
        marker_job="$(sed -n 's/^job_id=//p' "${MENHIR_MUTATION_DIR}/${op}.mutated" 2>/dev/null | tail -n1)"
        if [ -n "${marker_job}" ] && [ "${marker_job}" = "${job_id}" ]; then
            RECOVER_REASON="post-mutation state: durable mutation-started evidence exists for job ${job_id} (${MENHIR_MUTATION_DIR}/${op}.mutated)"
            return 1
        fi
    fi
    case "${phase}" in
        running|enqueued)
            RECOVER_REASON="stale non-terminal phase ${phase}: worker interrupted without a terminal record (SIGKILL/timeout) -- outcome unknown"
            return 1
            ;;
        failed)
            local rec detail
            rec="$(sed -n 's/^recovery_required=//p' "${st}" 2>/dev/null | tail -n1)"
            detail="$(sed -n 's/^detail=//p' "${st}" 2>/dev/null | tail -n1)"
            if [ "${rec}" = "0" ] && [ "${detail}" = "pre-mutation failure" ]; then
                RECOVER_STATE="safe"
                return 0
            fi
            RECOVER_REASON="failed record is not an explicitly proven pre-mutation failure (recovery_required=${rec:-?}, detail='${detail:-?}')"
            return 1
            ;;
        *)
            RECOVER_REASON="unrecognized phase '${phase}'"
            return 1
            ;;
    esac
}

# Require a strict, release-bound caddy-release reconciliation receipt before
# any caddy route recovery transition. File metadata alone is not evidence.
caddy_reconcile_evidence() {
    local f="${MENHIR_CADDY_RECONCILE_EVIDENCE}"
    local authority="${1:-${MENHIR_RELEASE_JSON}}"
    if [ -L "${authority}" ] || [ ! -f "${authority}" ] \
            || [ "$(stat -c '%u' "${authority}" 2>/dev/null || echo -1)" != "0" ]; then
        echo "error: Caddy reconcile release authority is missing or unsafe: ${authority}" >&2
        return 1
    fi
    local authority_mode
    authority_mode="$(stat -c '%a' "${authority}" 2>/dev/null || echo 777)"
    if (( (8#${authority_mode}) & 8#022 )); then
        echo "error: Caddy reconcile release authority is writable by group/other: ${authority}" >&2
        return 1
    fi
    if [ -L "${f}" ] || [ ! -f "${f}" ]; then
        echo "error: caddy release reconcile/status evidence missing: ${f}" >&2
        return 1
    fi
    if [ "$(stat -c '%u' "${f}" 2>/dev/null || echo -1)" != "0" ]; then
        echo "error: caddy release reconcile/status evidence must be root-owned: ${f}" >&2
        return 1
    fi
    local mode
    mode="$(stat -c '%a' "${f}" 2>/dev/null || echo 777)"
    if (( (8#${mode}) & 8#022 )); then
        echo "error: caddy release reconcile/status evidence must not be group/other writable: ${f}" >&2
        return 1
    fi
    if [ ! -s "${f}" ]; then
        echo "error: caddy release reconcile/status evidence is empty: ${f}" >&2
        return 1
    fi
    if [ ! -L "${MENHIR_CADDY_CURRENT_LINK}" ]; then
        echo "error: immutable Caddy activation link is missing: ${MENHIR_CADDY_CURRENT_LINK}" >&2
        return 1
    fi
    local active_bundle
    active_bundle="$(readlink -f "${MENHIR_CADDY_CURRENT_LINK}" 2>/dev/null || true)"
    if [ -z "${active_bundle}" ] || [ ! -f "${active_bundle}/MANIFEST.json" ]; then
        echo "error: active immutable Caddy bundle is missing" >&2
        return 1
    fi
    if ! python3 - "${f}" "${authority}" "${active_bundle}" <<'PY'
import datetime,hashlib,json,os,re,sys
receipt_path,release_path,active_bundle=sys.argv[1:4]
def load(path):
    seen=set()
    def hook(pairs):
        out={}
        for k,v in pairs:
            if k in out: raise ValueError("duplicate JSON key: "+k)
            out[k]=v
        return out
    with open(path,encoding="utf-8") as handle: return json.load(handle,object_pairs_hook=hook)
receipt=load(receipt_path); release=load(release_path)
keys={"schema","kind","outcome","journal_phase","active_bundle","bundle_manifest_sha256","release_id","release_manifest_sha256","active_caddyfile_sha256","active_compose_sha256","active_registry_sha256","active_env_sha256","running_image_ref","running_image_id","expected_config_sha256","loaded_config_sha256","networks_sha256","probes_sha256","checked_utc"}
if set(receipt)!=keys or receipt.get("schema")!=1 or receipt.get("kind")!="caddy-reconcile": raise ValueError("schema")
if receipt.get("outcome") not in {"clean","rolled-back"}: raise ValueError("outcome")
if receipt.get("active_bundle")!=active_bundle: raise ValueError("active bundle")
sha=lambda p: hashlib.sha256(open(p,"rb").read()).hexdigest()
if receipt.get("bundle_manifest_sha256")!=sha(os.path.join(active_bundle,"MANIFEST.json")): raise ValueError("bundle digest")
if receipt.get("release_id")!=release.get("release_id") or receipt.get("release_manifest_sha256")!=sha(release_path): raise ValueError("release binding")
artifact_fields={
    "active_caddyfile_sha256": ("Caddyfile", "caddy_sha256"),
    "active_compose_sha256": ("compose.yml", "yawn_compose_sha256"),
    "active_registry_sha256": ("releases.json", "registry_sha256"),
    "active_env_sha256": ("env", "yawn_env_sha256"),
}
for field,(name,release_field) in artifact_fields.items():
    actual=sha(os.path.join(active_bundle,name))
    if receipt.get(field)!=actual or actual!=release.get("rendered",{}).get(release_field):
        raise ValueError("active artifact binding: "+field)
digest=re.compile(r"^[0-9a-f]{64}$")
for field in ("expected_config_sha256","loaded_config_sha256","networks_sha256","probes_sha256"):
    if not digest.fullmatch(receipt.get(field,"")): raise ValueError("runtime digest: "+field)
if receipt["expected_config_sha256"]!=receipt["loaded_config_sha256"]: raise ValueError("loaded config mismatch")
image_digest=release.get("images",{}).get("caddy","")
if not re.fullmatch(r"sha256:[0-9a-f]{64}",image_digest): raise ValueError("release caddy image")
if not receipt.get("running_image_ref","").endswith("@"+image_digest): raise ValueError("running image ref")
if not re.fullmatch(r"sha256:[0-9a-f]{64}",receipt.get("running_image_id","")): raise ValueError("running image id")
checked=datetime.datetime.fromisoformat(receipt["checked_utc"].replace("Z","+00:00"))
now=datetime.datetime.now(datetime.timezone.utc)
if checked>now+datetime.timedelta(seconds=30) or now-checked>datetime.timedelta(minutes=5): raise ValueError("freshness")
PY
    then
        echo "error: caddy reconciliation receipt failed strict release/bundle/freshness validation: ${f}" >&2
        return 1
    fi
    local newest=0 m ev
    for st in "${MENHIR_JOB_DIR}"/caddy-route-*.status; do
        [ -e "${st}" ] || continue
        m="$(stat -c '%Y' "${st}" 2>/dev/null || echo 0)"
        [ "${m}" -gt "${newest}" ] && newest="${m}"
    done
    ev="$(stat -c '%Y' "${f}" 2>/dev/null || echo 0)"
    if [ "${ev}" -lt "${newest}" ]; then
        echo "error: caddy release reconcile/status evidence is stale (older than the latest caddy-route job record): ${f}" >&2
        return 1
    fi
    return 0
}

# Operation-specific, fail-closed recovery decision. The maintenance fence is
# reopened ONLY when every layer of evidence is independently verified:
#   - both locks are free,
#   - no menhir-op transient unit is active,
#   - no recorded worker pid / process-group descendant is alive,
#   - the installed artifacts verify against the immutable release record,
#   - every persisted job is reconciled (done) or an explicitly proven
#     pre-mutation failure (terminal failed, recovery_required=0, detail
#     "pre-mutation failure", no current mutation-started evidence),
#   - caddy-route recovery transitions additionally require fresh caddy release
#     reconcile/status evidence.
# A missing fence at bootstrap ("closed missing-at-init (recovery required)")
# with no operation records may also be opened. Any unknown/running/stale/
# post-mutation state keeps the fence closed and names the required
# operation-specific reconciler; there is no automatic fence_open on ambiguous
# state. SIGKILL/timeout kills are never treated as safe merely because the
# flock or the systemd unit is gone.
recover_main() {
    ensure_status_dirs
    if (flock -n 9) 9>"${MENHIR_LOCK}"; then :; else
        echo "REFUSED: operation lock is still held; cannot recover" >&2
        return 1
    fi
    if (flock -n 8) 8>"${MENHIR_ADMISSION_LOCK}"; then :; else
        echo "REFUSED: admission lock is still held; a submission is in progress; cannot recover" >&2
        return 1
    fi
    if menhir_units_active; then
        echo "REFUSED: a menhir-op transient unit is still active; resolve it before recovery" >&2
        return 1
    fi
    if recorded_worker_alive; then
        echo "REFUSED: a recorded worker pid/process-group descendant is still alive; resolve it before recovery" >&2
        return 1
    fi
    if ! "${VERIFY_ARTIFACTS_BIN}"; then
        echo "REFUSED: artifact verification failed; recovery evidence cannot be established" >&2
        return 1
    fi

    local found=0 refused=0 op safe_ops=() caddy_safe=()
    for st in "${MENHIR_JOB_DIR}"/*.status; do
        [ -e "${st}" ] || continue
        found=1
        op="$(basename "${st}" .status)"
        if recover_classify_op "${op}"; then
            [ "${RECOVER_STATE}" = "safe" ] && safe_ops+=("${op}")
        else
            echo "REFUSED: ${op}: ${RECOVER_REASON}" >&2
            echo "  required reconciler: $(op_reconciler_name "${op}")" >&2
            refused=1
        fi
    done

    if [ "${refused}" = "1" ]; then
        echo "REFUSED: recovery requires operation-specific reconciliation; the maintenance fence stays closed" >&2
        return 1
    fi

    if [ "${found}" = "0" ]; then
        # No operation records at all. Only a pristine bootstrap (missing-at-init)
        # fence may be reopened; anything else is an unknown state.
        local fstate
        fstate="$(fence_state)"
        case "${fstate}" in
            open) ;;
            "closed missing-at-init (recovery required)") ;;
            *)
                echo "REFUSED: no operation records and fence state '${fstate}' is not the bootstrap state; state unknown - cannot reopen" >&2
                return 1
                ;;
        esac
        fence_open
        echo "recovered: maintenance fence opened (bootstrap, no operation records)"
        return 0
    fi

    # Caddy route recovery transitions require successful caddy release
    # reconcile/status evidence before the fence may be reopened.
    for op in "${safe_ops[@]}"; do
        if is_caddy_op "${op}"; then
            caddy_safe+=("${op}")
        fi
    done
    if [ "${#caddy_safe[@]}" -gt 0 ]; then
        if ! caddy_reconcile_evidence; then
            echo "REFUSED: caddy-route recovery transition requires successful caddy-release reconcile/status evidence; fence stays closed" >&2
            echo "  required reconciler: $(op_reconciler_name "caddy-route")" >&2
            return 1
        fi
    fi

    fence_open
    echo "recovered: maintenance fence opened (operation-specific pre-mutation recovery evidence verified)"
    return 0
}
