#!/usr/bin/env bash
# Decrypt and stage the latest release-bound local backup for restore/rehearsal.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=release-lib.sh
. "${SCRIPT_DIR}/release-lib.sh"

[ "$#" -eq 0 ] || { echo "stage-generation accepts no arguments" >&2; exit 2; }
load_production_env
acquire_release_lock
receipt="$(backup_receipt_path)"
validate_receipt_file "$receipt" backup-local
schema_py validate-backup-promotion "$receipt"

identity="/etc/menhir/backup-restore.agekey"
require_root_file "$identity" "backup restore age identity"
mode="$(stat -c '%a' "$identity")"
[ "$mode" = 400 ] || [ "$mode" = 600 ] \
    || { echo "backup restore age identity must be mode 0400 or 0600" >&2; exit 1; }

generation="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["generation"])' "$receipt")"
archive="$(python3 - "$receipt" <<'PYEOF'
import json,sys
r=json.load(open(sys.argv[1],encoding="utf-8"))
g=r["generation"]
rows=[x for x in r["local_encrypted_archives"]["archives"] if x["generation"]==g]
if len(rows)!=1: raise SystemExit("backup receipt must identify exactly one current local archive")
print(rows[0]["path"])
PYEOF
)"
case "$archive" in
    /srv/menhir/backups/encrypted/*) ;;
    *) echo "current encrypted archive is outside the fixed backup root: $archive" >&2; exit 1 ;;
esac
require_root_file "$archive" "current encrypted backup archive"
expected_sha="$(python3 - "$receipt" "$archive" <<'PYEOF'
import json,sys
r=json.load(open(sys.argv[1],encoding="utf-8")); p=sys.argv[2]
rows=[x for x in r["local_encrypted_archives"]["archives"] if x["path"]==p]
if len(rows)!=1: raise SystemExit("encrypted archive path is not unique in receipt")
print(rows[0]["sha256"])
PYEOF
)"
[ "$(sha256sum "$archive" | cut -d' ' -f1)" = "$expected_sha" ] \
    || { echo "current encrypted archive digest differs from its backup receipt" >&2; exit 1; }

staging_root="${STATUS_DIR}/staging"
install -d -o root -g root -m 0700 "$staging_root"
plaintext="$(mktemp "${staging_root}/generation.XXXXXXXX.tar.gz")"
trap 'rm -f "$plaintext"' EXIT
age --decrypt --identity "$identity" --output "$plaintext" "$archive" \
    || { echo "backup decryption failed; /etc/menhir/backup-restore.agekey does not match the configured recipient" >&2; exit 1; }

extractor="${SCRIPT_DIR}/lib/stage_generation.py"
[ -f "$extractor" ] || extractor="${SCRIPT_DIR}/stage_generation.py"
destination="$(python3 "$extractor" "$plaintext" "$generation" "${BACKUP_ROOT}/decrypted")"
schema_py validate-manifest "${destination}/MANIFEST.json" "$destination"
manifest_sha="$(sha256sum "${destination}/MANIFEST.json" | cut -d' ' -f1)"
menhir_digest="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"]["menhir_image_digest"])' "${destination}/MANIFEST.json")"
neo4j_digest="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"]["neo4j_image_digest"])' "${destination}/MANIFEST.json")"
validate_receipt_binding "$receipt" backup-local "$generation" "$manifest_sha" "$menhir_digest" "$neo4j_digest"
write_generation_record "${STATUS_DIR}/restore-selection" "$generation"
rm -f "$plaintext"
trap - EXIT
printf 'staged_generation=%s\n' "$generation"
