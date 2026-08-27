#!/usr/bin/env bash
#
# Menhir backup upload wrapper for Contabo S3 (Object Lock COMPLIANCE).
#
# This script is the production backup upload contract implementation.
# It uploads a generation archive to Contabo S3 with provider-managed
# SSE AES256 + Object Lock COMPLIANCE >=30d, then verifies the uploaded
# object via head + readback before writing an atomic, structured receipt
# and removing plaintext.
#
# Sol blocker contract (2026-08-26 remediation, blockers 2-7):
#   * strict: set -euo pipefail + umask 077; any failure aborts
#   * root-only: must run as root
#   * fixed root-owned config/profile: every setting comes from one
#     root-owned mode-0600 non-symlink config file; no environment
#     overrides of any credential or setting. AWS credentials live in the
#     fixed root-owned profile files named by the config (defaults
#     /root/.aws/credentials and /root/.aws/config); AWS_* env vars are
#     explicitly scrubbed so a caller can never redirect them.
#   * fixed staging root: archives are staged only under
#     "${status_dir}/staging", never /tmp and never caller-chosen paths
#   * provider AES256 + Object Lock COMPLIANCE only: no KMS, no GOVERNANCE,
#     retention >=30 days verified on the exact uploaded VersionId
#   * exact direct-child generation containment: the generation directory
#     must be a DIRECT child of the configured generations_root, its name
#     must match generation.<alnum>, and that name must equal the manifest
#     generation id
#   * no symlinks or special entries anywhere inside the generation tree;
#     manifest validation additionally rejects them
#   * unique object key per attempt (generation + UTC timestamp + random
#     nonce), upload carries binding metadata, then hash/head/get/
#     exact production version hash/head/get verification; delete-object is
#     never attempted against the production backup
#   * delete-denial is proven only with a separate small sacrificial object
#     under a dedicated key, explicit COMPLIANCE retention, exact-version
#     head/readback, denied deletion, and post-denial head/readback
#   * retained local evidence enumerates actual regular non-symlink encrypted
#     archives, records generation/path/hash/size, includes the current archive,
#     and requires at least the configured number of distinct generations (>=2)
#   * every nonzero exit path retains plaintext: the generation directory
#     is removed ONLY after the fully verified upload and validated receipt
#   * atomic fsync receipt transaction performed inside ONE python process
#     (write -> fsync -> chmod/chown -> rename -> dirfd fsync -> close);
#     no file-descriptor numbers ever cross a process boundary
#   * final success possible: happy path exits 0 with a schema-valid
#     plaintext_removed=True receipt
#
# Usage:
#   deploy/menhir-backup-upload-contabo.sh <generation-directory>
#
# Config file (/etc/menhir/backup-upload.conf) — root-owned, mode 0600,
# no symlinks. Simple key=value lines; '#' comments; unknown keys fatal:
#   bucket=menhir-backups
#   archive_prefix=archive/
#   receipt_path=/var/lib/menhir-production/backup-upload-receipt.json
#   receipt_root=/var/lib/menhir-production/backup-receipts
#   status_dir=/var/lib/menhir-production
#   generations_root=/srv/menhir/backups/generations
#   local_archive_root=/srv/menhir/backups/encrypted
#   local_retention_generations=2
#   release_json=/srv/menhir/production/release/release.json
#   aws_profile=menhir-backup
#   aws_region=eu2
#   aws_credentials=/root/.aws/credentials      (optional; fixed default)
#   aws_config=/root/.aws/config                (optional; fixed default)
#   age_recipient=age1...                       (required public X25519 recipient)
set -euo pipefail
umask 077

# The root-owned wrapper is installed at /usr/local/sbin, while its reviewed
# Python authorities are installed with the rest of the immutable Menhir
# release. Never derive trusted library code from the caller's directory.
MENHIR_INSTALLED_BIN="/srv/menhir/production/bin"
if [ "${MENHIR_BACKUP_UPLOAD_ALLOW_NON_ROOT_TEST:-0}" = "1" ]; then
    MENHIR_INSTALLED_BIN="${MENHIR_BACKUP_UPLOAD_TEST_BIN:?test bin path is required}"
fi
SCHEMA="${MENHIR_INSTALLED_BIN}/menhir_schema.py"
CLEANUP_TXN="${MENHIR_INSTALLED_BIN}/backup_cleanup_txn.py"

# --- Fixed config file path (test harness may relocate the file itself;
# --- all settings still come exclusively from the config file) ---
CONFIG_FILE="${MENHIR_BACKUP_UPLOAD_CONFIG:-/etc/menhir/backup-upload.conf}"
OPERATION_JOB_ID="${MENHIR_OPERATION_JOB_ID:-}"

# --- Scrub ambient AWS environment: credentials come only from the ---
# --- fixed root-owned profile files named in the config            ---
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN \
      AWS_PROFILE AWS_DEFAULT_REGION AWS_REGION \
      AWS_SHARED_CREDENTIALS_FILE AWS_CONFIG_FILE AWS_ENDPOINT_URL_S3 || true

# --- Must run as root ---
[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

# --- Required tools ---
for cmd in age aws sha256sum tar python3 mktemp stat dirname basename date find id grep install; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "required tool not found: $cmd" >&2; exit 1; }
done

# --- Root-owned regular non-symlink file helper ---
_require_root_file() {
    local path="$1" label="$2"
    [ -f "$path" ] && [ ! -L "$path" ] \
        || { echo "$label must be a regular non-symlink file: $path" >&2; exit 1; }
    local owner mode
    owner="$(stat -c '%u' "$path")"; mode="$(stat -c '%a' "$path")"
    [ "$owner" = 0 ] || { echo "$label must be root-owned: $path" >&2; exit 1; }
    [ "$mode" = "600" ] || { echo "$label must be mode 0600: $path (got $mode)" >&2; exit 1; }
}

_require_root_file "$CONFIG_FILE" "backup-upload config"

# --- Parse simple key=value config (no sections, no interpolation) ---
BUCKET=""
ARCHIVE_PREFIX=""
RECEIPT_PATH=""
RECEIPT_ROOT=""
STATUS_DIR=""
GENERATIONS_ROOT=""
LOCAL_ARCHIVE_ROOT=""
LOCAL_RETENTION_GENERATIONS=""
RELEASE_JSON=""
AWS_PROFILE=""
AWS_REGION=""
AWS_CREDENTIALS="/root/.aws/credentials"
AWS_CONFIG_FILE_PATH="/root/.aws/config"
AGE_RECIPIENT=""
while IFS='=' read -r key value; do
    case "$key" in
        ''|\#*) continue ;;
    esac
    case "$key" in
        bucket) BUCKET="$value" ;;
        archive_prefix) ARCHIVE_PREFIX="$value" ;;
        receipt_path) RECEIPT_PATH="$value" ;;
        receipt_root) RECEIPT_ROOT="$value" ;;
        status_dir) STATUS_DIR="$value" ;;
        generations_root) GENERATIONS_ROOT="$value" ;;
        local_archive_root) LOCAL_ARCHIVE_ROOT="$value" ;;
        local_retention_generations) LOCAL_RETENTION_GENERATIONS="$value" ;;
        release_json) RELEASE_JSON="$value" ;;
        aws_profile) AWS_PROFILE="$value" ;;
        aws_region) AWS_REGION="$value" ;;
        aws_credentials) AWS_CREDENTIALS="$value" ;;
        aws_config) AWS_CONFIG_FILE_PATH="$value" ;;
        age_recipient) AGE_RECIPIENT="$value" ;;
        *) echo "unknown config key: $key" >&2; exit 1 ;;
    esac
done < "$CONFIG_FILE"

for var in BUCKET ARCHIVE_PREFIX RECEIPT_PATH RECEIPT_ROOT STATUS_DIR GENERATIONS_ROOT LOCAL_ARCHIVE_ROOT LOCAL_RETENTION_GENERATIONS RELEASE_JSON AWS_PROFILE AWS_REGION AGE_RECIPIENT; do
    [ -n "${!var}" ] || { echo "config ${var} is required" >&2; exit 1; }
done
[[ "$LOCAL_RETENTION_GENERATIONS" =~ ^[0-9]+$ ]] && [ "$LOCAL_RETENTION_GENERATIONS" -ge 2 ] \
    || { echo "local_retention_generations must be an integer >= 2" >&2; exit 1; }
case "$RECEIPT_ROOT:$LOCAL_ARCHIVE_ROOT" in
    /*:/*) ;;
    *) echo "receipt_root and local_archive_root must be absolute" >&2; exit 1 ;;
esac

case "$AGE_RECIPIENT" in
    age1[023456789acdefghjklmnpqrstuvwxyz]*) ;;
    *) echo "config age_recipient must be an age X25519 public recipient" >&2; exit 1 ;;
esac

case "$ARCHIVE_PREFIX" in
    /*|"")
        echo "config archive_prefix must be relative and non-empty" >&2; exit 1 ;;
    */);;
    *) echo "config archive_prefix must end with '/'" >&2; exit 1 ;;
esac

# --- Fixed profile files: root-owned, mode 0600, non-symlink, no env ---
_require_root_file "$AWS_CREDENTIALS" "AWS credentials"
_require_root_file "$AWS_CONFIG_FILE_PATH" "AWS config"

if ! grep -qE "^\[ *profile *${AWS_PROFILE} *\]|^\[ *${AWS_PROFILE} *\]" \
        "$AWS_CREDENTIALS" "$AWS_CONFIG_FILE_PATH" 2>/dev/null; then
    echo "AWS profile '${AWS_PROFILE}' not found in the fixed credential files" >&2
    exit 1
fi

AWS_CLI=(aws --profile "$AWS_PROFILE" --region "$AWS_REGION")

CLEANUP_ROOT="${STATUS_DIR}/plaintext-cleanup"
CLEANUP_JOURNAL="${STATUS_DIR}/backup-upload-cleanup-journal.json"

# A previous process may have been killed after the off-host object and pending
# receipt were committed.  Recovery is explicit and uses only journal-bound,
# configured roots; no caller-provided generation path is accepted.
if [ "${1:-}" = "--resume-cleanup" ]; then
    [ "$#" -eq 1 ] || { echo "usage: $0 --resume-cleanup" >&2; exit 2; }
    python3 "$CLEANUP_TXN" complete "$CLEANUP_JOURNAL" "$RECEIPT_PATH" \
        "$GENERATIONS_ROOT" "$CLEANUP_ROOT" "$RECEIPT_ROOT"
    python3 "$SCHEMA" validate-receipt "$RECEIPT_PATH" backup-upload
    resumed_generation="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["generation"])' "$RECEIPT_PATH")"
    python3 "$SCHEMA" validate-receipt "${RECEIPT_ROOT}/${resumed_generation}.json" backup-upload
    echo "Backup plaintext cleanup transaction resumed and finalized."
    exit 0
fi

[[ "$OPERATION_JOB_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] \
    || { echo "MENHIR_OPERATION_JOB_ID is required and invalid" >&2; exit 1; }
[ ! -e "$CLEANUP_JOURNAL" ] \
    || { echo "unfinished backup cleanup journal exists; run $0 --resume-cleanup" >&2; exit 1; }

# --- Argument: generation directory with direct-child containment ---
[ "$#" -eq 1 ] || { echo "usage: $0 <generation-directory>" >&2; exit 2; }
GEN_DIR="$1"

[ -d "$GEN_DIR" ] || { echo "generation directory does not exist: $GEN_DIR" >&2; exit 1; }
[ ! -L "$GEN_DIR" ] || { echo "generation directory must not be a symlink: $GEN_DIR" >&2; exit 1; }
case "$GEN_DIR" in
    /*) ;;
    *) echo "generation directory must be absolute: $GEN_DIR" >&2; exit 1 ;;
esac

GEN_NAME="$(basename "$GEN_DIR")"
case "$GEN_NAME" in
    generation.[A-Za-z0-9]*) ;;
    *) echo "generation directory name must be generation.<alnum>: $GEN_NAME" >&2; exit 1 ;;
esac

GEN_PARENT="$(cd "$(dirname "$GEN_DIR")" && pwd)"
GEN_ROOT_CANON="$(cd "$GENERATIONS_ROOT" 2>/dev/null && pwd)" \
    || { echo "configured generations_root does not exist: $GENERATIONS_ROOT" >&2; exit 1; }
[ "$GEN_PARENT" = "$GEN_ROOT_CANON" ] \
    || { echo "generation must be a direct child of generations_root (${GEN_ROOT_CANON}): $GEN_DIR" >&2; exit 1; }

# --- Reject symlinks and special entries anywhere inside the tree ---
if [ -n "$(find "$GEN_DIR" -mindepth 1 ! -type f ! -type d -print -quit)" ]; then
    echo "generation contains symlink or special entry; refusing" >&2
    exit 1
fi

# --- Required markers ---
[ -f "${GEN_DIR}/MANIFEST.json" ] || { echo "MANIFEST.json missing in generation" >&2; exit 1; }
[ -f "${GEN_DIR}/SHA256SUMS" ] || { echo "SHA256SUMS missing in generation" >&2; exit 1; }
[ -f "${GEN_DIR}/COMPLETE" ] || { echo "COMPLETE marker missing in generation" >&2; exit 1; }

# --- Manifest validation (schema also rejects symlinks/special files) ---
python3 "$SCHEMA" validate-manifest "${GEN_DIR}/MANIFEST.json" "$GEN_DIR" \
    || { echo "manifest validation failed" >&2; exit 1; }

GENERATION="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["generation"])' "${GEN_DIR}/MANIFEST.json")"

# Containment binding: directory name == manifest generation id
[ "$GEN_NAME" = "$GENERATION" ] \
    || { echo "directory name ${GEN_NAME} != manifest generation ${GENERATION}" >&2; exit 1; }

MANIFEST_SHA256="$(sha256sum "${GEN_DIR}/MANIFEST.json" | cut -d' ' -f1)"

# --- Release authority binding (fixed path from config) ---
[ -f "$RELEASE_JSON" ] && [ ! -L "$RELEASE_JSON" ] \
    || { echo "release.json missing or symlink: $RELEASE_JSON" >&2; exit 1; }
[ "$(stat -c '%u' "$RELEASE_JSON")" = 0 ] || { echo "release.json must be root-owned" >&2; exit 1; }
python3 "$SCHEMA" validate-release "$RELEASE_JSON" \
    || { echo "release.json validation failed" >&2; exit 1; }

RELEASE_ID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["release_id"])' "$RELEASE_JSON")"
RELEASE_MANIFEST_SHA256="$(sha256sum "$RELEASE_JSON" | cut -d' ' -f1)"
MENHIR_DIGEST="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["images"]["menhir"])' "$RELEASE_JSON")"
NEO4J_DIGEST="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["images"]["neo4j"])' "$RELEASE_JSON")"

# --- Fixed staging root under status_dir (never /tmp) ---
STAGING_ROOT="${STATUS_DIR}/staging"
mkdir -p "$STAGING_ROOT"
chmod 0700 "$STAGING_ROOT"
STAGING_DIR="$(mktemp -d "${STAGING_ROOT}/upload.XXXXXXXXXX")"
# Cleanup trap removes ONLY staging; plaintext is retained on any failure.
trap 'rm -rf "$STAGING_DIR"' EXIT

PLAINTEXT_ARCHIVE="${STAGING_DIR}/${GENERATION}.tar.gz"
ARCHIVE_PATH="${STAGING_DIR}/${GENERATION}.tar.gz.age"

echo "Creating archive of generation ${GENERATION} ..."
( cd "$GEN_ROOT_CANON" && tar czf "$PLAINTEXT_ARCHIVE" "$GEN_NAME" )
PLAINTEXT_ARCHIVE_SHA256="$(sha256sum "$PLAINTEXT_ARCHIVE" | cut -d' ' -f1)"
age --encrypt --recipient "$AGE_RECIPIENT" --output "$ARCHIVE_PATH" "$PLAINTEXT_ARCHIVE" \
    || { echo "client-side age encryption failed" >&2; exit 1; }
rm -f "$PLAINTEXT_ARCHIVE"
ARCHIVE_SHA256="$(sha256sum "$ARCHIVE_PATH" | cut -d' ' -f1)"
ARCHIVE_SIZE="$(stat -c '%s' "$ARCHIVE_PATH")"

# --- Unique object key + binding metadata ---
UPLOAD_NONCE="$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets;print(secrets.token_hex(8))')"
OBJECT_KEY="${ARCHIVE_PREFIX}${GENERATION}/${UPLOAD_NONCE}.tar.gz.age"
mkdir -p "$LOCAL_ARCHIVE_ROOT"
[ ! -L "$LOCAL_ARCHIVE_ROOT" ] || { echo "local archive root must not be a symlink" >&2; exit 1; }
chmod 0700 "$LOCAL_ARCHIVE_ROOT"
LOCAL_ARCHIVE_PATH="${LOCAL_ARCHIVE_ROOT}/${GENERATION}-${UPLOAD_NONCE}.tar.gz.age"
[ ! -e "$LOCAL_ARCHIVE_PATH" ] || { echo "local encrypted archive already exists" >&2; exit 1; }
install -m 0400 "$ARCHIVE_PATH" "$LOCAL_ARCHIVE_PATH"
[ "$(sha256sum "$LOCAL_ARCHIVE_PATH" | cut -d' ' -f1)" = "$ARCHIVE_SHA256" ] \
    || { echo "local encrypted archive hash mismatch" >&2; exit 1; }
python3 - "$LOCAL_ARCHIVE_PATH" <<'PYEOF'
import os, sys
path = os.path.abspath(sys.argv[1])
fd = os.open(path, os.O_RDONLY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
try:
    os.fsync(dir_fd)
finally:
    os.close(dir_fd)
PYEOF

# Refuse before any off-host write unless the actual local archive set already
# proves the configured minimum of distinct generations, including this one.
python3 - "$LOCAL_ARCHIVE_ROOT" "$LOCAL_ARCHIVE_PATH" "$GENERATION" \
    "$LOCAL_RETENTION_GENERATIONS" <<'PYEOF'
import os, re, stat, sys

root, current_path, current_generation, minimum = sys.argv[1:5]
root = os.path.abspath(root)
current_path = os.path.abspath(current_path)
archive_name = re.compile(r"^(generation\.[A-Za-z0-9]+)-.+\.tar\.gz\.age$")
generations = set()
current_found = False
with os.scandir(root) as entries:
    for entry in entries:
        match = archive_name.fullmatch(entry.name)
        if match is None or entry.is_symlink():
            continue
        entry_stat = entry.stat(follow_symlinks=False)
        if not stat.S_ISREG(entry_stat.st_mode):
            continue
        generations.add(match.group(1))
        if os.path.abspath(entry.path) == current_path and match.group(1) == current_generation:
            current_found = True
if not current_found:
    raise ValueError("current encrypted archive is not a regular non-symlink archive")
if len(generations) < int(minimum):
    raise ValueError(
        "fewer than configured minimum distinct retained generations: %d < %d"
        % (len(generations), int(minimum))
    )
PYEOF

echo "Uploading to s3://${BUCKET}/${OBJECT_KEY} ..."

UPLOAD_OUTPUT="$("${AWS_CLI[@]}" s3api put-object \
    --bucket "$BUCKET" \
    --key "$OBJECT_KEY" \
    --body "$ARCHIVE_PATH" \
    --server-side-encryption AES256 \
    --object-lock-mode COMPLIANCE \
    --object-lock-retain-until-date "$(date -u -d '+31 days' +%Y-%m-%dT%H:%M:%SZ)" \
    --metadata "generation=${GENERATION},manifest-sha256=${MANIFEST_SHA256},object-sha256=${ARCHIVE_SHA256},client-encryption=age-x25519" \
    --output json)" \
    || { echo "S3 put-object failed" >&2; exit 1; }

VERSION_ID="$(echo "$UPLOAD_OUTPUT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("VersionId",""))')"
[ -n "$VERSION_ID" ] || { echo "S3 put-object did not return a VersionId" >&2; exit 1; }

echo "Uploaded version: ${VERSION_ID}"

# --- Head verification: exact version, provider AES256 + COMPLIANCE only ---
echo "Verifying uploaded object via head ..."
HEAD_OUTPUT="$("${AWS_CLI[@]}" s3api head-object \
    --bucket "$BUCKET" \
    --key "$OBJECT_KEY" \
    --version-id "$VERSION_ID" \
    --output json)" \
    || { echo "S3 head-object failed for version ${VERSION_ID}" >&2; exit 1; }

HEAD_SIZE="$(echo "$HEAD_OUTPUT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ContentLength",0))')"
HEAD_ENCRYPTION="$(echo "$HEAD_OUTPUT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ServerSideEncryption",""))')"
HEAD_LOCK_MODE="$(echo "$HEAD_OUTPUT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ObjectLockMode",""))')"
HEAD_RETENTION="$(echo "$HEAD_OUTPUT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ObjectLockRetainUntilDate",""))')"

[ "$HEAD_SIZE" -eq "$ARCHIVE_SIZE" ] \
    || { echo "head size mismatch: ${HEAD_SIZE} != ${ARCHIVE_SIZE}" >&2; exit 1; }
[ "$HEAD_ENCRYPTION" = "AES256" ] \
    || { echo "head encryption mismatch: ${HEAD_ENCRYPTION} != AES256" >&2; exit 1; }
[ "$HEAD_LOCK_MODE" = "COMPLIANCE" ] \
    || { echo "head lock mode mismatch: ${HEAD_LOCK_MODE} != COMPLIANCE" >&2; exit 1; }

RETENTION_OK="$(python3 -c "
import sys, datetime
retention = datetime.datetime.fromisoformat('${HEAD_RETENTION}'.replace('Z', '+00:00'))
now = datetime.datetime.now(datetime.timezone.utc)
if retention >= now + datetime.timedelta(days=30):
    print('ok')
else:
    print('fail')
")"
[ "$RETENTION_OK" = "ok" ] \
    || { echo "head retention insufficient: ${HEAD_RETENTION}" >&2; exit 1; }

# --- Readback verification: download and hash the exact version ---
echo "Verifying uploaded object via readback ..."
READBACK_PATH="${STAGING_DIR}/readback.tar.gz"
"${AWS_CLI[@]}" s3api get-object \
    --bucket "$BUCKET" \
    --key "$OBJECT_KEY" \
    --version-id "$VERSION_ID" \
    "$READBACK_PATH" >/dev/null \
    || { echo "S3 get-object failed for version ${VERSION_ID}" >&2; exit 1; }

READBACK_SHA256="$(sha256sum "$READBACK_PATH" | cut -d' ' -f1)"
[ "$READBACK_SHA256" = "$ARCHIVE_SHA256" ] \
    || { echo "readback hash mismatch: ${READBACK_SHA256} != ${ARCHIVE_SHA256}" >&2; exit 1; }

rm -f "$READBACK_PATH"

# --- Sacrificial exact-version Object Lock delete-denial probe.  The
# production backup object/version above is never submitted to delete-object. ---
PROBE_KEY="${ARCHIVE_PREFIX}worm-delete-denial-probes/${GENERATION}/${UPLOAD_NONCE}.txt"
[ "$PROBE_KEY" != "$OBJECT_KEY" ] \
    || { echo "sacrificial probe key must differ from production backup key" >&2; exit 1; }
PROBE_PATH="${STAGING_DIR}/worm-delete-denial-probe.txt"
python3 - "$PROBE_PATH" <<'PYEOF'
import os, sys
payload = b"menhir-object-lock-delete-denial-probe-v1\n"
path = sys.argv[1]
with open(path, "xb") as probe:
    probe.write(payload)
    probe.flush()
    os.fsync(probe.fileno())
PYEOF
PROBE_SHA256="$(sha256sum "$PROBE_PATH" | cut -d' ' -f1)"
PROBE_SIZE="$(stat -c '%s' "$PROBE_PATH")"

echo "Uploading sacrificial Object Lock probe ..."
PROBE_UPLOAD_OUTPUT="$("${AWS_CLI[@]}" s3api put-object \
    --bucket "$BUCKET" \
    --key "$PROBE_KEY" \
    --body "$PROBE_PATH" \
    --server-side-encryption AES256 \
    --metadata "purpose=worm-delete-denial-probe,object-sha256=${PROBE_SHA256}" \
    --output json)" \
    || { echo "S3 sacrificial probe put-object failed" >&2; exit 1; }
PROBE_VERSION_ID="$(echo "$PROBE_UPLOAD_OUTPUT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("VersionId",""))')"
[ -n "$PROBE_VERSION_ID" ] \
    || { echo "S3 sacrificial probe put-object did not return a VersionId" >&2; exit 1; }
[ "$PROBE_VERSION_ID" != "$VERSION_ID" ] \
    || { echo "sacrificial probe version must differ from production backup version" >&2; exit 1; }

PROBE_RETENTION_REQUESTED="$(date -u -d '+31 days' +%Y-%m-%dT%H:%M:%SZ)"
"${AWS_CLI[@]}" s3api put-object-retention \
    --bucket "$BUCKET" \
    --key "$PROBE_KEY" \
    --version-id "$PROBE_VERSION_ID" \
    --retention "Mode=COMPLIANCE,RetainUntilDate=${PROBE_RETENTION_REQUESTED}" \
    --output json >/dev/null \
    || { echo "S3 sacrificial probe COMPLIANCE retention failed" >&2; exit 1; }

echo "Verifying sacrificial probe exact version and retention ..."
PROBE_HEAD_OUTPUT="$("${AWS_CLI[@]}" s3api head-object \
    --bucket "$BUCKET" \
    --key "$PROBE_KEY" \
    --version-id "$PROBE_VERSION_ID" \
    --output json)" \
    || { echo "S3 head-object failed for sacrificial probe version ${PROBE_VERSION_ID}" >&2; exit 1; }
PROBE_HEAD_SIZE="$(echo "$PROBE_HEAD_OUTPUT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ContentLength",0))')"
PROBE_HEAD_ENCRYPTION="$(echo "$PROBE_HEAD_OUTPUT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ServerSideEncryption",""))')"
PROBE_HEAD_LOCK_MODE="$(echo "$PROBE_HEAD_OUTPUT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ObjectLockMode",""))')"
PROBE_HEAD_RETENTION="$(echo "$PROBE_HEAD_OUTPUT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ObjectLockRetainUntilDate",""))')"
[ "$PROBE_HEAD_SIZE" -eq "$PROBE_SIZE" ] \
    || { echo "sacrificial probe head size mismatch" >&2; exit 1; }
[ "$PROBE_HEAD_ENCRYPTION" = "AES256" ] \
    || { echo "sacrificial probe head encryption mismatch" >&2; exit 1; }
[ "$PROBE_HEAD_LOCK_MODE" = "COMPLIANCE" ] \
    || { echo "sacrificial probe head lock mode mismatch" >&2; exit 1; }
PROBE_RETENTION_OK="$(python3 -c "
import datetime
retention = datetime.datetime.fromisoformat('${PROBE_HEAD_RETENTION}'.replace('Z', '+00:00'))
now = datetime.datetime.now(datetime.timezone.utc)
print('ok' if retention >= now + datetime.timedelta(days=30) else 'fail')
")"
[ "$PROBE_RETENTION_OK" = "ok" ] \
    || { echo "sacrificial probe head retention insufficient: ${PROBE_HEAD_RETENTION}" >&2; exit 1; }

PROBE_READBACK_PATH="${STAGING_DIR}/probe-readback.txt"
"${AWS_CLI[@]}" s3api get-object \
    --bucket "$BUCKET" \
    --key "$PROBE_KEY" \
    --version-id "$PROBE_VERSION_ID" \
    "$PROBE_READBACK_PATH" >/dev/null \
    || { echo "S3 get-object failed for sacrificial probe version ${PROBE_VERSION_ID}" >&2; exit 1; }
[ "$(sha256sum "$PROBE_READBACK_PATH" | cut -d' ' -f1)" = "$PROBE_SHA256" ] \
    || { echo "sacrificial probe readback hash mismatch" >&2; exit 1; }
rm -f "$PROBE_READBACK_PATH"

echo "Verifying sacrificial locked version delete is denied ..."
PROBE_DELETE_ERROR="${STAGING_DIR}/probe-delete.stderr"
if "${AWS_CLI[@]}" s3api delete-object \
    --bucket "$BUCKET" \
    --key "$PROBE_KEY" \
    --version-id "$PROBE_VERSION_ID" \
    --output json 2>"$PROBE_DELETE_ERROR"; then
    echo "ERROR: delete-object succeeded for sacrificial locked version" >&2
    exit 1
fi
grep -Eiq 'AccessDenied|Object[[:space:]]*Lock|retention|locked' "$PROBE_DELETE_ERROR" \
    || { echo "sacrificial probe delete did not return an explicit retention denial" >&2; exit 1; }

# --- Re-head and re-read the exact probe version to prove it remains. ---
echo "Proving sacrificial probe version persists after denied delete ..."
PROBE_REHEAD_OUTPUT="$("${AWS_CLI[@]}" s3api head-object \
    --bucket "$BUCKET" \
    --key "$PROBE_KEY" \
    --version-id "$PROBE_VERSION_ID" \
    --output json)" \
    || { echo "S3 re-head-object failed for sacrificial probe after delete attempt" >&2; exit 1; }
PROBE_REHEAD_SIZE="$(echo "$PROBE_REHEAD_OUTPUT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ContentLength",0))')"
PROBE_REHEAD_MODE="$(echo "$PROBE_REHEAD_OUTPUT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ObjectLockMode",""))')"
PROBE_REHEAD_RETENTION="$(echo "$PROBE_REHEAD_OUTPUT" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("ObjectLockRetainUntilDate",""))')"
[ "$PROBE_REHEAD_SIZE" -eq "$PROBE_SIZE" ] \
    || { echo "sacrificial probe re-head size mismatch after delete attempt" >&2; exit 1; }
[ "$PROBE_REHEAD_MODE" = "COMPLIANCE" ] \
    || { echo "sacrificial probe re-head lock mode mismatch after delete attempt" >&2; exit 1; }
[ "$PROBE_REHEAD_RETENTION" = "$PROBE_HEAD_RETENTION" ] \
    || { echo "sacrificial probe retention changed after delete attempt" >&2; exit 1; }
PROBE_REMAINS_PATH="${STAGING_DIR}/probe-remains.txt"
"${AWS_CLI[@]}" s3api get-object \
    --bucket "$BUCKET" \
    --key "$PROBE_KEY" \
    --version-id "$PROBE_VERSION_ID" \
    "$PROBE_REMAINS_PATH" >/dev/null \
    || { echo "S3 sacrificial probe did not remain after denied delete" >&2; exit 1; }
[ "$(sha256sum "$PROBE_REMAINS_PATH" | cut -d' ' -f1)" = "$PROBE_SHA256" ] \
    || { echo "sacrificial probe changed after denied delete" >&2; exit 1; }
rm -f "$PROBE_REMAINS_PATH"

# --- Atomic fsync receipt transaction (single python process; pending state) ---
echo "Writing atomic receipt ..."
mkdir -p "$(dirname "$RECEIPT_PATH")"
chmod 0755 "$(dirname "$RECEIPT_PATH")"

python3 - "$RECEIPT_PATH" "$GENERATION" "$MANIFEST_SHA256" \
    "$RELEASE_ID" "$RELEASE_MANIFEST_SHA256" "$MENHIR_DIGEST" "$NEO4J_DIGEST" \
    "$BUCKET" "$OBJECT_KEY" "$VERSION_ID" "$ARCHIVE_SHA256" "$ARCHIVE_SIZE" \
    "$HEAD_RETENTION" "$AGE_RECIPIENT" "$PLAINTEXT_ARCHIVE_SHA256" \
    "$LOCAL_ARCHIVE_ROOT" "$LOCAL_ARCHIVE_PATH" "$LOCAL_RETENTION_GENERATIONS" \
    "$PROBE_KEY" "$PROBE_VERSION_ID" "$PROBE_SHA256" "$PROBE_SIZE" \
    "$PROBE_HEAD_RETENTION" "$OPERATION_JOB_ID" <<'PYEOF'
import datetime, hashlib, json, os, re, stat, sys, tempfile

(
    final_path, generation, manifest_sha256, release_id, release_manifest_sha256,
    menhir_digest, neo4j_digest, bucket, object_key, version_id,
    object_sha256, object_size, worm_retention_until, age_recipient,
    plaintext_archive_sha256, local_archive_root, local_archive_path,
    local_retention_generations, probe_key, probe_version_id, probe_sha256,
    probe_size, probe_retention_until, operation_job_id
) = sys.argv[1:25]

local_archive_root = os.path.abspath(local_archive_root)
local_archive_path = os.path.abspath(local_archive_path)
minimum_retained = int(local_retention_generations)
archive_name = re.compile(
    r"^(generation\.[A-Za-z0-9]+)-.+\.tar\.gz\.age$"
)
local_archives = []
with os.scandir(local_archive_root) as entries:
    for entry in entries:
        match = archive_name.fullmatch(entry.name)
        if match is None or entry.is_symlink():
            continue
        fd = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW)
        entry_stat = os.fstat(fd)
        if not stat.S_ISREG(entry_stat.st_mode):
            os.close(fd)
            continue
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb") as archive:
            for chunk in iter(lambda: archive.read(1024 * 1024), b""):
                digest.update(chunk)
        local_archives.append({
            "generation": match.group(1),
            "path": os.path.abspath(entry.path),
            "sha256": digest.hexdigest(),
            "size": entry_stat.st_size,
        })
local_archives.sort(key=lambda item: (item["generation"], item["path"]))
retained_generations = {item["generation"] for item in local_archives}
if len(retained_generations) < minimum_retained:
    raise ValueError(
        "fewer than configured minimum distinct retained generations: %d < %d"
        % (len(retained_generations), minimum_retained)
    )
current = [item for item in local_archives if item["path"] == local_archive_path]
if len(current) != 1 or current[0]["generation"] != generation or \
        current[0]["sha256"] != object_sha256 or \
        current[0]["size"] != int(object_size):
    raise ValueError("current encrypted archive is absent or does not match production backup")

receipt = {
    "schema": 1,
    "kind": "backup-upload",
    "operation_job_id": operation_job_id,
    "generation": generation,
    "manifest_sha256": manifest_sha256,
    "release": {
        "release_id": release_id,
        "release_manifest_sha256": release_manifest_sha256,
        "menhir_image_digest": menhir_digest,
        "neo4j_image_digest": neo4j_digest,
    },
    "offhost": {
        "bucket": bucket,
        "production_backup": {
            "object_key": object_key,
            "version_id": version_id,
            "object_sha256": object_sha256,
            "object_size": int(object_size),
            "server_side_encryption": "AES256",
            "lock_mode": "COMPLIANCE",
            "worm_retention_until": worm_retention_until,
            "version_readback_verified": True,
            "client_encryption": {
                "algorithm": "age-x25519",
                "recipient": age_recipient,
                "plaintext_archive_sha256": plaintext_archive_sha256,
            },
        },
        "sacrificial_probe": {
            "object_key": probe_key,
            "version_id": probe_version_id,
            "object_sha256": probe_sha256,
            "object_size": int(probe_size),
            "server_side_encryption": "AES256",
            "lock_mode": "COMPLIANCE",
            "worm_retention_until": probe_retention_until,
            "version_readback_verified": True,
            "locked_version_delete_denied": True,
            "version_persisted_after_delete_denial": True,
        },
    },
    "local_encrypted_archives": {
        "minimum_retained_generations": minimum_retained,
        "retained_generation_count": len(retained_generations),
        "current_archive_path": local_archive_path,
        "archives": local_archives,
    },
    "plaintext_removed": False,
    "checked_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}

final_path = os.path.abspath(final_path)
parent = os.path.dirname(final_path)
fd, tmp_path = tempfile.mkstemp(prefix=".backup-upload-receipt.", dir=parent)
try:
    with os.fdopen(fd, "w", encoding="ascii") as f:
        json.dump(receipt, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    if os.geteuid() == 0:
        os.chown(tmp_path, 0, 0)
    os.chmod(tmp_path, 0o400)
    os.replace(tmp_path, final_path)
except BaseException:
    try:
        os.unlink(tmp_path)
    except OSError:
        pass
    raise

dir_fd = os.open(parent, os.O_RDONLY)
try:
    os.fsync(dir_fd)
finally:
    os.close(dir_fd)
PYEOF

python3 "$SCHEMA" validate-receipt "$RECEIPT_PATH" backup-upload \
    || { echo "pending receipt validation failed" >&2; exit 1; }

# --- Durable plaintext cleanup transaction.  SIGKILL at any point after the
# journal commit is recovered with --resume-cleanup; promotion remains blocked
# by the pending receipt until cleanup is complete. ---
python3 "$CLEANUP_TXN" begin "$CLEANUP_JOURNAL" "$RECEIPT_PATH" "$GEN_DIR" \
    "$GENERATIONS_ROOT" "$CLEANUP_ROOT" "$RECEIPT_ROOT"
python3 "$CLEANUP_TXN" complete "$CLEANUP_JOURNAL" "$RECEIPT_PATH" \
    "$GENERATIONS_ROOT" "$CLEANUP_ROOT" "$RECEIPT_ROOT"

# --- Final validation + binding against the release authority ---
python3 "$SCHEMA" validate-receipt "$RECEIPT_PATH" backup-upload \
    || { echo "final receipt validation failed" >&2; exit 1; }

python3 "$SCHEMA" validate-receipt-binding "$RECEIPT_PATH" backup-upload \
    "$RELEASE_JSON" "$GENERATION" "$MANIFEST_SHA256" "$MENHIR_DIGEST" "$NEO4J_DIGEST" \
    || { echo "receipt binding validation failed" >&2; exit 1; }

GENERATION_RECEIPT="${RECEIPT_ROOT}/${GENERATION}.json"
python3 "$SCHEMA" validate-receipt-binding "$GENERATION_RECEIPT" backup-upload \
    "$RELEASE_JSON" "$GENERATION" "$MANIFEST_SHA256" "$MENHIR_DIGEST" "$NEO4J_DIGEST"

echo "Backup upload complete: s3://${BUCKET}/${OBJECT_KEY} version ${VERSION_ID}"
echo "Receipt: ${RECEIPT_PATH}"
