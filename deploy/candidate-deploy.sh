#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=release-lib.sh
. "${SCRIPT_DIR}/release-lib.sh"

[ "$#" -eq 0 ] || { echo "candidate-deploy accepts no arguments" >&2; exit 2; }
load_production_env
validate_release_authority
generation="$(read_generation "${STATUS_DIR}/restore-selection" "restore selection")"
source_root="${BACKUP_ROOT}/decrypted/${generation}"
source_manifest="${source_root}/MANIFEST.json"
schema_py validate-manifest "$source_manifest" "$source_root"
manifest_sha="$(sha256sum "$source_manifest" | cut -d' ' -f1)"
menhir_digest="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"]["menhir_image_digest"])' "$source_manifest")"
neo4j_digest="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"]["neo4j_image_digest"])' "$source_manifest")"
validate_receipt_binding "${STATUS_DIR}/rehearsal-receipt.json" rehearsal "$generation" \
    "$manifest_sha" "$menhir_digest" "$neo4j_digest"
marker="${BACKUP_ROOT}/candidate/${generation}/REHEARSAL-PASSED"
[ "$(read_generation "$marker" "candidate rehearsal marker")" = "$generation" ] \
    || { echo "candidate rehearsal marker mismatch" >&2; exit 1; }
backup_receipt="$(backup_receipt_path)"
validate_receipt_binding "$backup_receipt" backup-local "$generation" \
    "$manifest_sha" "$menhir_digest" "$neo4j_digest"
same_host_fence="${STATUS_DIR}/same-host-writer-fence.json"
require_root_file "$same_host_fence" "same-host writer-fence receipt"
acquire_release_lock
same_host_helper="${SCRIPT_DIR}/lib/same_host_fence.py"
[ -f "$same_host_helper" ] || same_host_helper="${SCRIPT_DIR}/same_host_fence.py"
census="$(mktemp)"
trap 'rm -f "$census"' EXIT
ids="$(docker ps -aq)"
if [ -n "$ids" ]; then
    # shellcheck disable=SC2086
    docker inspect $ids > "$census"
else
    printf '[]\n' > "$census"
fi
python3 "$same_host_helper" verify "$RELEASE_JSON" "$same_host_fence" "$census" \
    || { echo "candidate deployment refused: a legacy or competing app/database remains" >&2; exit 1; }
rm -f "$census"; trap - EXIT
candidate_down "$generation" >/dev/null 2>&1 || true
candidate_neo4j_up "$generation"

# Establish immutable acceptance authority before the Menhir application starts.
# The candidate points at the exact restored production OAuth, secrets, policy,
# telemetry, and Neo4j authority that promotion will reuse. Only its probe
# telemetry is redirected to the disposable candidate directory.
prestart_digest="$(candidate_authority_digest "$generation")"
release_sha="$(sha256sum "$RELEASE_JSON" | cut -d' ' -f1)"
prestart_receipt="${STATUS_DIR}/candidate-prestart-authority.json"
install -d -o root -g root -m 0755 "$STATUS_DIR"
python3 - "$prestart_receipt" "$generation" "$manifest_sha" "$release_sha" \
    "$prestart_digest" <<'PYEOF'
import datetime, json, os, sys, tempfile
path,generation,manifest_sha,release_sha,authority_sha=sys.argv[1:6]
value={
    "schema":1,
    "kind":"candidate-prestart-authority",
    "generation":generation,
    "manifest_sha256":manifest_sha,
    "release_manifest_sha256":release_sha,
    "authority_sha256":authority_sha,
    "checked_utc":datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
parent=os.path.dirname(path)
fd,tmp=tempfile.mkstemp(prefix=".candidate-prestart-",dir=parent,text=True)
try:
    with os.fdopen(fd,"w",encoding="ascii") as handle:
        json.dump(value,handle,sort_keys=True); handle.write("\n")
        handle.flush(); os.fsync(handle.fileno())
    os.chmod(tmp,0o400); os.replace(tmp,path)
    d=os.open(parent,os.O_RDONLY)
    try: os.fsync(d)
    finally: os.close(d)
finally:
    if os.path.exists(tmp): os.unlink(tmp)
PYEOF

candidate_app_up "$generation"
install -d -o root -g root -m 0755 "$STATUS_DIR"
printf '%s\n' "$generation" > "${STATUS_DIR}/candidate-generation"
chmod 0400 "${STATUS_DIR}/candidate-generation"
printf 'candidate_generation=%s\n' "$generation"
