#!/usr/bin/env bash
# Promote the accepted candidate to one-writer production (the point of no
# return). Promotion consumes the exact, validated structured receipts and never
# consults mtime. It validates the immutable release authority first.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=release-lib.sh
. "${SCRIPT_DIR}/release-lib.sh"

[ "$#" -eq 0 ] || { echo "promote accepts no arguments" >&2; exit 2; }
load_production_env
acquire_release_lock
validate_release_authority

# Promotion trusts only the exact same-host Docker authority. The legacy writer
# was captured before mutation, then stopped, restart-disabled, and removed.
# Revalidate the current all-container census under the same host-wide lock;
# absence alone is never accepted without the release-bound receipt.
same_host_fence="${STATUS_DIR}/same-host-writer-fence.json"
same_host_helper="${SCRIPT_DIR}/lib/same_host_fence.py"
[ -f "$same_host_helper" ] || same_host_helper="${SCRIPT_DIR}/same_host_fence.py"
require_root_file "$same_host_fence" "same-host writer-fence receipt"

verify_same_host_fence() { # [allow-production]
    local mode="${1:-}" census ids
    census="$(mktemp)"
    ids="$(docker ps -aq)"
    if [ -n "$ids" ]; then
        # shellcheck disable=SC2086
        docker inspect $ids > "$census"
    else
        printf '[]\n' > "$census"
    fi
    local -a verify_args=(verify "$RELEASE_JSON" "$same_host_fence" "$census")
    [ "$mode" != "allow-production" ] || verify_args+=(--allow-production)
    python3 "$same_host_helper" "${verify_args[@]}" \
        || { rm -f "$census"; echo "same-host writer fence is open: a legacy or competing writer remains" >&2; return 1; }
    rm -f "$census"
}

candidate="$(read_generation "${STATUS_DIR}/candidate-generation" "candidate generation")"
accepted="$(read_generation "${STATUS_DIR}/candidate-accepted" "candidate acceptance")"
[ "$candidate" = "$accepted" ] \
    || { echo "candidate and accepted generations do not match" >&2; exit 1; }

# Candidate acceptance receipt must exact-match the candidate generation.
accept_receipt="$(accept_receipt_path)"
validate_receipt_file "$accept_receipt" candidate-accept
acc_gen="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["generation"])' "$accept_receipt")"
[ "$acc_gen" = "$candidate" ] || { echo "acceptance receipt generation mismatch" >&2; exit 1; }

# A fresh, complete, uploaded backup generation must exist (exact receipt, no mtime).
backup_receipt="$(backup_receipt_path)"
validate_receipt_file "$backup_receipt" backup-local
schema_py validate-backup-promotion "$backup_receipt"
bak_gen="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["generation"])' "$backup_receipt")"
[ "$bak_gen" = "$candidate" ] || { echo "backup receipt generation mismatch" >&2; exit 1; }

candidate_manifest="${BACKUP_ROOT}/decrypted/${candidate}/MANIFEST.json"
schema_py validate-manifest "$candidate_manifest" "${BACKUP_ROOT}/decrypted/${candidate}"
manifest_sha="$(sha256sum "$candidate_manifest" | cut -d' ' -f1)"
menhir_digest="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"]["menhir_image_digest"])' "$candidate_manifest")"
neo4j_digest="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"]["neo4j_image_digest"])' "$candidate_manifest")"
validate_receipt_binding "$accept_receipt" candidate-accept "$candidate" \
    "$manifest_sha" "$menhir_digest" "$neo4j_digest"
validate_receipt_binding "$backup_receipt" backup-local "$candidate" \
    "$manifest_sha" "$menhir_digest" "$neo4j_digest"

# A durable first-mutation marker turns every retry into roll-forward recovery.
# It is written before the candidate is stopped, so a crash at any subsequent
# instruction cannot make the operator guess whether the irreversible boundary
# was crossed.
if first_mutation_occurred; then
    marker_generation="$(sed -n '1p' "$mutation_marker")"
    [ "$marker_generation" = "$candidate" ] \
        || { echo "first-mutation marker belongs to another generation" >&2; exit 1; }
    candidate_down "$candidate" >/dev/null 2>&1 || true
    if ! production_up; then
        production_down >/dev/null 2>&1 || true
        echo "production roll-forward recovery failed; keep the writer stopped and retry promote" >&2
        exit 1
    fi
    verify_same_host_fence allow-production
    write_generation_record "${STATUS_DIR}/current-generation" "$candidate"
    printf 'current_generation=%s recovered=true\n' "$candidate"
    exit 0
fi

# The irreversible boundary is recorded durably BEFORE writable production can
# start. Any failure after this point is roll-forward/recovery only; stale
# candidate reattachment is forbidden even if the app never becomes healthy.
# Revalidate the exact Docker census immediately before the first writable
# target mutation while still holding the host-wide maintenance lock.
verify_same_host_fence
record_first_mutation "$candidate"
candidate_down "$candidate"
if ! production_up; then
    production_down >/dev/null 2>&1 || true
    echo "production promotion failed after the durable mutation boundary; recovery required" >&2
    exit 1
fi
verify_same_host_fence allow-production
write_generation_record "${STATUS_DIR}/current-generation" "$candidate"
printf 'current_generation=%s\n' "$candidate"
