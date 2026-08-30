#!/usr/bin/env bash
# Stop, disable, remove, and durably identify the legacy same-host Menhir writer.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=release-lib.sh
. "${SCRIPT_DIR}/release-lib.sh"

[ "$#" -eq 0 ] || { echo "same-host-fence accepts no arguments" >&2; exit 2; }
load_production_env
acquire_release_lock
validate_release_authority
backup_receipt="$(backup_receipt_path)"
validate_receipt_file "$backup_receipt" backup-upload
schema_py validate-backup-promotion "$backup_receipt" \
    || { echo "same-host writer retirement requires a fresh verified off-host backup" >&2; exit 1; }

helper="${SCRIPT_DIR}/lib/same_host_fence.py"
[ -f "$helper" ] || helper="${SCRIPT_DIR}/same_host_fence.py"
intent="${STATUS_DIR}/same-host-writer-fence-intent.json"
receipt="${STATUS_DIR}/same-host-writer-fence.json"
inspect_tmp="$(mktemp)"
census_tmp="$(mktemp)"
trap 'rm -f "$inspect_tmp" "$census_tmp"' EXIT

docker_census() {
    local ids
    ids="$(docker ps -aq)"
    if [ -n "$ids" ]; then
        # shellcheck disable=SC2086
        docker inspect $ids > "$census_tmp"
    else
        printf '[]\n' > "$census_tmp"
    fi
}

if [ ! -e "$intent" ]; then
    docker inspect menhir-prod-app menhir-prod-neo4j > "$inspect_tmp" 2>/dev/null \
        || { echo "legacy app/database pair is absent and no fence intent exists; cannot prove which stack was retired" >&2; exit 1; }
    python3 "$helper" capture-intent "$RELEASE_JSON" "$inspect_tmp" "$intent"
fi

readarray -t legacy_ids < <(python3 - "$intent" <<'PYEOF'
import json,sys
v=json.load(open(sys.argv[1],encoding="utf-8"))
print(v["legacy"]["app"]["container_id"])
print(v["legacy"]["database"]["container_id"])
PYEOF
)
for legacy_id in "${legacy_ids[@]}"; do
    if docker inspect "$legacy_id" >/dev/null 2>&1; then
        docker stop --time 30 "$legacy_id"
        docker update --restart=no "$legacy_id" >/dev/null
        docker rm "$legacy_id" >/dev/null
    fi
done

docker_census
python3 "$helper" finalize "$RELEASE_JSON" "$intent" "$census_tmp" "$receipt"
printf 'same_host_writer_fenced=%s,%s\n' "${legacy_ids[0]}" "${legacy_ids[1]}"
