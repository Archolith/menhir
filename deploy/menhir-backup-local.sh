#!/usr/bin/env bash
# Create one encrypted Menhir generation archive and retain it on the VPS.
#
# Production paths are fixed. Test-only overrides are admitted only when
# MENHIR_BACKUP_LOCAL_ALLOW_NON_ROOT_TEST=1; production callers cannot redirect
# the archive, receipt, release, identity, or generation roots.
set -euo pipefail
umask 077

TEST_MODE="${MENHIR_BACKUP_LOCAL_ALLOW_NON_ROOT_TEST:-0}"
if [ "$TEST_MODE" = 1 ]; then
    INSTALLED_BIN="${MENHIR_BACKUP_LOCAL_TEST_BIN:?test bin path is required}"
    STATUS_DIR="${MENHIR_BACKUP_LOCAL_TEST_STATUS_DIR:?test status path is required}"
    GENERATIONS_ROOT="${MENHIR_BACKUP_LOCAL_TEST_GENERATIONS_ROOT:?test generations path is required}"
    ARCHIVE_ROOT="${MENHIR_BACKUP_LOCAL_TEST_ARCHIVE_ROOT:?test archive path is required}"
    RELEASE_JSON="${MENHIR_BACKUP_LOCAL_TEST_RELEASE_JSON:?test release path is required}"
    AGE_IDENTITY="${MENHIR_BACKUP_LOCAL_TEST_AGE_IDENTITY:?test age identity is required}"
else
    [ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
    INSTALLED_BIN="/srv/menhir/production/bin"
    STATUS_DIR="/var/lib/menhir-production"
    GENERATIONS_ROOT="/srv/menhir/backups/generations"
    ARCHIVE_ROOT="/srv/menhir/backups/encrypted"
    RELEASE_JSON="/srv/menhir/production/release/release.json"
    AGE_IDENTITY="/etc/menhir/backup-restore.agekey"
fi

SCHEMA="${INSTALLED_BIN}/menhir_schema.py"
CLEANUP_TXN="${INSTALLED_BIN}/backup_cleanup_txn.py"
RECEIPT_PATH="${STATUS_DIR}/backup-local-receipt.json"
RECEIPT_ROOT="${STATUS_DIR}/backup-receipts"
CLEANUP_ROOT="${STATUS_DIR}/plaintext-cleanup"
CLEANUP_JOURNAL="${STATUS_DIR}/backup-local-cleanup-journal.json"
RETENTION_TARGET_GENERATIONS=2
OPERATION_JOB_ID="${MENHIR_OPERATION_JOB_ID:-}"

for cmd in age age-keygen sha256sum tar python3 mktemp stat dirname basename date find install; do
    command -v "$cmd" >/dev/null 2>&1 \
        || { echo "required tool not found: $cmd" >&2; exit 1; }
done

require_safe_file() {
    local path="$1" label="$2" mode owner
    [ -f "$path" ] && [ ! -L "$path" ] \
        || { echo "$label must be a regular non-symlink file: $path" >&2; exit 1; }
    if [ "$TEST_MODE" != 1 ]; then
        owner="$(stat -c '%u' "$path")"
        mode="$(stat -c '%a' "$path")"
        [ "$owner" = 0 ] || { echo "$label must be root-owned: $path" >&2; exit 1; }
        (( ((8#${mode}) & 8#077) == 0 )) \
            || { echo "$label must not be accessible by group/other: $path" >&2; exit 1; }
    fi
}

require_safe_file "$AGE_IDENTITY" "backup restore identity"
require_safe_file "$RELEASE_JSON" "release authority"
python3 "$SCHEMA" validate-release "$RELEASE_JSON"

if [ "${1:-}" = "--resume-cleanup" ]; then
    [ "$#" -eq 1 ] || { echo "usage: $0 --resume-cleanup" >&2; exit 2; }
    python3 "$CLEANUP_TXN" complete "$CLEANUP_JOURNAL" "$RECEIPT_PATH" \
        "$GENERATIONS_ROOT" "$CLEANUP_ROOT" "$RECEIPT_ROOT"
    python3 "$SCHEMA" validate-receipt "$RECEIPT_PATH" backup-local
    generation="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["generation"])' "$RECEIPT_PATH")"
    python3 "$SCHEMA" validate-receipt "${RECEIPT_ROOT}/${generation}.json" backup-local
    echo "Local backup plaintext cleanup transaction resumed and finalized."
    exit 0
fi

[[ "$OPERATION_JOB_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] \
    || { echo "MENHIR_OPERATION_JOB_ID is required and invalid" >&2; exit 1; }
[ ! -e "$CLEANUP_JOURNAL" ] \
    || { echo "unfinished backup cleanup journal exists; run $0 --resume-cleanup" >&2; exit 1; }
[ "$#" -eq 1 ] || { echo "usage: $0 <generation-directory>" >&2; exit 2; }

GEN_DIR="$1"
[ -d "$GEN_DIR" ] && [ ! -L "$GEN_DIR" ] \
    || { echo "generation directory must be a real directory: $GEN_DIR" >&2; exit 1; }
case "$GEN_DIR" in /*) ;; *) echo "generation directory must be absolute" >&2; exit 1;; esac
GENERATION="$(basename "$GEN_DIR")"
[[ "$GENERATION" =~ ^generation\.[A-Za-z0-9]+$ ]] \
    || { echo "generation directory name is invalid" >&2; exit 1; }
GEN_PARENT="$(cd "$(dirname "$GEN_DIR")" && pwd)"
GEN_ROOT_CANON="$(cd "$GENERATIONS_ROOT" && pwd)"
[ "$GEN_PARENT" = "$GEN_ROOT_CANON" ] \
    || { echo "generation must be a direct child of the fixed generations root" >&2; exit 1; }
if [ -n "$(find "$GEN_DIR" -mindepth 1 ! -type f ! -type d -print -quit)" ]; then
    echo "generation contains a symlink or special entry" >&2
    exit 1
fi

for marker in MANIFEST.json SHA256SUMS COMPLETE; do
    [ -f "${GEN_DIR}/${marker}" ] || { echo "${marker} missing in generation" >&2; exit 1; }
done
python3 "$SCHEMA" validate-manifest "${GEN_DIR}/MANIFEST.json" "$GEN_DIR"
manifest_generation="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["generation"])' "${GEN_DIR}/MANIFEST.json")"
[ "$manifest_generation" = "$GENERATION" ] \
    || { echo "generation directory does not match manifest" >&2; exit 1; }

MANIFEST_SHA256="$(sha256sum "${GEN_DIR}/MANIFEST.json" | cut -d' ' -f1)"
RELEASE_ID="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["release_id"])' "$RELEASE_JSON")"
RELEASE_MANIFEST_SHA256="$(sha256sum "$RELEASE_JSON" | cut -d' ' -f1)"
MENHIR_DIGEST="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["images"]["menhir"])' "$RELEASE_JSON")"
NEO4J_DIGEST="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["images"]["neo4j"])' "$RELEASE_JSON")"
AGE_RECIPIENT="$(age-keygen -y "$AGE_IDENTITY")"
case "$AGE_RECIPIENT" in age1*) ;; *) echo "invalid age recipient derived from identity" >&2; exit 1;; esac

mkdir -p "$STATUS_DIR" "$ARCHIVE_ROOT" "$RECEIPT_ROOT" "${STATUS_DIR}/staging"
[ ! -L "$ARCHIVE_ROOT" ] && [ ! -L "$RECEIPT_ROOT" ] \
    || { echo "archive and receipt roots must not be symlinks" >&2; exit 1; }
chmod 0700 "$ARCHIVE_ROOT" "$RECEIPT_ROOT" "${STATUS_DIR}/staging"
STAGING_DIR="$(mktemp -d "${STATUS_DIR}/staging/local.XXXXXXXXXX")"
trap 'rm -rf "$STAGING_DIR"' EXIT
PLAINTEXT_ARCHIVE="${STAGING_DIR}/${GENERATION}.tar.gz"
ENCRYPTED_ARCHIVE="${STAGING_DIR}/${GENERATION}.tar.gz.age"
ROUNDTRIP_ARCHIVE="${STAGING_DIR}/${GENERATION}.roundtrip.tar.gz"

( cd "$GEN_ROOT_CANON" && tar czf "$PLAINTEXT_ARCHIVE" "$GENERATION" )
PLAINTEXT_SHA256="$(sha256sum "$PLAINTEXT_ARCHIVE" | cut -d' ' -f1)"
age --encrypt --recipient "$AGE_RECIPIENT" --output "$ENCRYPTED_ARCHIVE" "$PLAINTEXT_ARCHIVE"
age --decrypt --identity "$AGE_IDENTITY" --output "$ROUNDTRIP_ARCHIVE" "$ENCRYPTED_ARCHIVE"
[ "$(sha256sum "$ROUNDTRIP_ARCHIVE" | cut -d' ' -f1)" = "$PLAINTEXT_SHA256" ] \
    || { echo "encrypted backup roundtrip hash mismatch" >&2; exit 1; }
rm -f "$PLAINTEXT_ARCHIVE" "$ROUNDTRIP_ARCHIVE"

nonce="$(date -u +%Y%m%dT%H%M%SZ)-$(python3 -c 'import secrets;print(secrets.token_hex(8))')"
LOCAL_ARCHIVE_PATH="${ARCHIVE_ROOT}/${GENERATION}-${nonce}.tar.gz.age"
install -m 0400 "$ENCRYPTED_ARCHIVE" "$LOCAL_ARCHIVE_PATH"
ARCHIVE_SHA256="$(sha256sum "$LOCAL_ARCHIVE_PATH" | cut -d' ' -f1)"
ARCHIVE_SIZE="$(stat -c '%s' "$LOCAL_ARCHIVE_PATH")"
python3 - "$LOCAL_ARCHIVE_PATH" <<'PYEOF'
import os, sys
path = os.path.abspath(sys.argv[1])
with open(path, "rb") as handle:
    os.fsync(handle.fileno())
fd = os.open(os.path.dirname(path), os.O_RDONLY)
try:
    os.fsync(fd)
finally:
    os.close(fd)
PYEOF

python3 - "$ARCHIVE_ROOT" "$LOCAL_ARCHIVE_PATH" "$RECEIPT_PATH" \
    "$OPERATION_JOB_ID" "$GENERATION" "$MANIFEST_SHA256" "$RELEASE_ID" \
    "$RELEASE_MANIFEST_SHA256" "$MENHIR_DIGEST" "$NEO4J_DIGEST" \
    "$AGE_RECIPIENT" "$PLAINTEXT_SHA256" "$RETENTION_TARGET_GENERATIONS" <<'PYEOF'
import datetime, hashlib, json, os, re, stat, sys, tempfile
(
    archive_root, current_path, receipt_path, operation_job_id, generation,
    manifest_sha256, release_id, release_manifest_sha256, menhir_digest,
    neo4j_digest, recipient, plaintext_sha256, retention_target,
) = sys.argv[1:]
pattern = re.compile(r"^(generation\.[A-Za-z0-9]+)-.+\.tar\.gz\.age$")
archives = []
for entry in os.scandir(archive_root):
    match = pattern.fullmatch(entry.name)
    if match is None or entry.is_symlink():
        continue
    info = entry.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        continue
    digest = hashlib.sha256()
    with open(entry.path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    archives.append({
        "generation": match.group(1),
        "path": os.path.abspath(entry.path),
        "sha256": digest.hexdigest(),
        "size": info.st_size,
    })
archives.sort(key=lambda item: item["path"])
if not any(item["path"] == os.path.abspath(current_path) and item["generation"] == generation for item in archives):
    raise SystemExit("current encrypted archive missing from local archive inventory")
receipt = {
    "schema": 1,
    "kind": "backup-local",
    "operation_job_id": operation_job_id,
    "generation": generation,
    "manifest_sha256": manifest_sha256,
    "release": {
        "release_id": release_id,
        "release_manifest_sha256": release_manifest_sha256,
        "menhir_image_digest": menhir_digest,
        "neo4j_image_digest": neo4j_digest,
    },
    "encryption": {
        "algorithm": "age-x25519",
        "recipient": recipient,
        "plaintext_archive_sha256": plaintext_sha256,
        "roundtrip_verified": True,
    },
    "local_encrypted_archives": {
        "retention_target_generations": int(retention_target),
        "retained_generation_count": len({item["generation"] for item in archives}),
        "current_archive_path": os.path.abspath(current_path),
        "archives": archives,
    },
    "plaintext_removed": False,
    "checked_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
parent = os.path.dirname(receipt_path)
os.makedirs(parent, exist_ok=True)
fd, temporary = tempfile.mkstemp(prefix=".backup-local-receipt.", dir=parent)
try:
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        json.dump(receipt, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o400)
    if os.geteuid() == 0:
        os.chown(temporary, 0, 0)
    os.replace(temporary, receipt_path)
    dir_fd = os.open(parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
except BaseException:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
    raise
PYEOF

python3 "$SCHEMA" validate-receipt-binding "$RECEIPT_PATH" backup-local \
    "$RELEASE_JSON" "$GENERATION" "$MANIFEST_SHA256" "$MENHIR_DIGEST" "$NEO4J_DIGEST"
python3 "$CLEANUP_TXN" begin "$CLEANUP_JOURNAL" "$RECEIPT_PATH" "$GEN_DIR" \
    "$GENERATIONS_ROOT" "$CLEANUP_ROOT" "$RECEIPT_ROOT"
python3 "$CLEANUP_TXN" complete "$CLEANUP_JOURNAL" "$RECEIPT_PATH" \
    "$GENERATIONS_ROOT" "$CLEANUP_ROOT" "$RECEIPT_ROOT"
python3 "$SCHEMA" validate-receipt-binding "$RECEIPT_PATH" backup-local \
    "$RELEASE_JSON" "$GENERATION" "$MANIFEST_SHA256" "$MENHIR_DIGEST" "$NEO4J_DIGEST"
python3 "$SCHEMA" validate-receipt-binding "${RECEIPT_ROOT}/${GENERATION}.json" backup-local \
    "$RELEASE_JSON" "$GENERATION" "$MANIFEST_SHA256" "$MENHIR_DIGEST" "$NEO4J_DIGEST"

echo "Local encrypted backup complete: ${LOCAL_ARCHIVE_PATH}"
