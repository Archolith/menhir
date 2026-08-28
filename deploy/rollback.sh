#!/usr/bin/env bash
# Rollback. Pre-mutation route rollback and post-mutation recovery are distinct.
#
#   * Before the first production mutation, rollback restores the candidate
#     (the prior authority route) - a reversible route rollback.
#   * After the first production mutation (durable first-mutation marker),
#     a naive route rollback is REFUSED. Recovery requires either a verified
#     reverse-generation receipt or an explicit owner data-loss acceptance; the
#     authoritative path is restoring the newest complete generation via
#     restore-generation.sh, never re-attaching the stale candidate.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=release-lib.sh
. "${SCRIPT_DIR}/release-lib.sh"

[ "$#" -eq 0 ] || { echo "rollback accepts no arguments" >&2; exit 2; }
load_production_env
validate_release_authority

generation="$(read_generation "${STATUS_DIR}/candidate-generation" "candidate generation")"

acquire_release_lock

if first_mutation_occurred; then
    reverse_receipt="${STATUS_DIR}/reverse-generation-receipt.json"
    if [ -f "$reverse_receipt" ] && [ ! -L "$reverse_receipt" ] \
        && [ "$(stat -c '%u' "$reverse_receipt")" = 0 ]; then
        validate_receipt_file "$reverse_receipt" rehearsal \
            || { echo "reverse-generation receipt failed validation" >&2; exit 1; }
        rev_gen="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["generation"])' "$reverse_receipt")"
        echo "post-mutation recovery: restore verified reverse generation ${rev_gen} via" \
             "restore-generation.sh; a naive candidate rollback is refused." >&2
    else
        echo "REFUSING post-mutation rollback: the first-mutation marker is set and no" >&2
        echo "verified reverse-generation / data-loss owner receipt is present." >&2
        echo "Prefer roll-forward remediation, or restore the newest complete authority" >&2
        echo "generation via restore-generation.sh after a successful reverse rehearsal." >&2
    fi
    exit 1
fi

# Pre-mutation route rollback: restore the candidate (prior route) exactly.
production_down
candidate_up "$generation"
printf '%s\n' "$generation" > "${STATUS_DIR}/rolled-back-generation"
chmod 0400 "${STATUS_DIR}/rolled-back-generation"
printf 'rolled_back_to_candidate=%s\n' "$generation"
