#!/usr/bin/env bash
# One-command, resumable same-host Menhir production release.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=release-lib.sh
. "${SCRIPT_DIR}/release-lib.sh"

[ "$#" -eq 0 ] || { echo "release-run accepts no arguments" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "release-run must run as root" >&2; exit 1; }
load_production_env
validate_release_authority
same_host_helper="${SCRIPT_DIR}/lib/same_host_fence.py"
[ -f "$same_host_helper" ] || same_host_helper="${SCRIPT_DIR}/same_host_fence.py"

run_lock="/run/lock/menhir-release-run.lock"
exec 8>"$run_lock"
flock -n 8 || { echo "another Menhir release-run is active: $run_lock" >&2; exit 75; }

state="${STATUS_DIR}/release-run.json"
release_sha="$(sha256sum "$RELEASE_JSON" | cut -d' ' -f1)"
release_id="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["release_id"])' "$RELEASE_JSON")"

write_stage() { # stage generation
    local stage="$1" generation="${2:-}"
    install -d -o root -g root -m 0755 "$STATUS_DIR"
    python3 - "$state" "$release_id" "$release_sha" "$stage" "$generation" <<'PYEOF'
import datetime,json,os,sys,tempfile
path,release_id,release_sha,stage,generation=sys.argv[1:6]
value={"schema":1,"kind":"menhir-release-run","release_id":release_id,
       "release_manifest_sha256":release_sha,"stage":stage,"generation":generation,
       "updated_utc":datetime.datetime.now(datetime.timezone.utc).isoformat()}
parent=os.path.dirname(path); fd,tmp=tempfile.mkstemp(prefix=".release-run-",dir=parent,text=True)
try:
    with os.fdopen(fd,"w",encoding="ascii") as f:
        json.dump(value,f,sort_keys=True); f.write("\n"); f.flush(); os.fsync(f.fileno())
    os.chmod(tmp,0o400); os.replace(tmp,path)
    d=os.open(parent,os.O_RDONLY)
    try: os.fsync(d)
    finally: os.close(d)
finally:
    if os.path.exists(tmp): os.unlink(tmp)
PYEOF
}

stage_index() {
    case "$1" in
        start) echo 0;; backup) echo 1;; staged) echo 2;; rehearsal) echo 3;;
        candidate) echo 4;; accepted) echo 5;; routed) echo 6;; promoted) echo 7;;
        complete) echo 8;; *) echo -1;;
    esac
}
at_least() { [ "$(stage_index "$stage")" -ge "$(stage_index "$1")" ]; }
advance() { stage="$1"; write_stage "$stage" "$generation"; }

# release-run.json is observability, never authorization. Validate any prior
# copy so an unprivileged or malformed marker cannot be silently blessed, then
# reconstruct progress from the immutable receipts and live runtime every run.
if [ -e "$state" ]; then
    require_root_file "$state" "release-run state"
    python3 - "$state" <<'PYEOF'
import json,re,sys
v=json.load(open(sys.argv[1],encoding="utf-8"))
keys={"schema","kind","release_id","release_manifest_sha256","stage","generation","updated_utc"}
if set(v)!=keys or v.get("schema")!=1 or v.get("kind")!="menhir-release-run":
    raise SystemExit("release-run state schema mismatch")
if v["stage"] not in {"start","backup","staged","rehearsal","candidate","accepted","routed","promoted","complete"}:
    raise SystemExit("release-run state stage is invalid")
if not re.fullmatch(r"[0-9a-f]{64}",v["release_manifest_sha256"]):
    raise SystemExit("release-run state release digest is malformed")
if v["generation"] and not re.fullmatch(r"generation\.[A-Za-z0-9]+",v["generation"]):
    raise SystemExit("release-run state generation is malformed")
PYEOF
fi

stage="start"
generation=""
backup_receipt="$(backup_receipt_path)"
if [ -f "$backup_receipt" ] && python3 - "$backup_receipt" "$release_id" "$release_sha" <<'PYEOF'
import json,sys
v=json.load(open(sys.argv[1],encoding="utf-8")); r=v.get("release") or {}
raise SystemExit(0 if r.get("release_id")==sys.argv[2] and r.get("release_manifest_sha256")==sys.argv[3] else 1)
PYEOF
then
    validate_receipt_file "$backup_receipt" backup-local
    schema_py validate-backup-promotion "$backup_receipt"
    desktop_receipt="${STATUS_DIR}/desktop-archive-receipt.json"
    require_root_file "$desktop_receipt" "desktop archive receipt"
    schema_py validate-desktop-archive "$desktop_receipt" "$backup_receipt" "$RELEASE_JSON"
    generation="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["generation"])' "$backup_receipt")"

    # Complete an interrupted retirement from its durable exact-identity intent.
    if [ ! -f "${STATUS_DIR}/same-host-writer-fence.json" ] \
            && [ -f "${STATUS_DIR}/same-host-writer-fence-intent.json" ]; then
        "${SCRIPT_DIR}/same-host-fence.sh"
    fi
    require_root_file "${STATUS_DIR}/same-host-writer-fence.json" "same-host writer-fence receipt"
    census="$(mktemp)"; ids="$(docker ps -aq)"
    if [ -n "$ids" ]; then
        # shellcheck disable=SC2086
        docker inspect $ids > "$census"
    else
        printf '[]\n' > "$census"
    fi
    fence_args=(verify "$RELEASE_JSON" "${STATUS_DIR}/same-host-writer-fence.json" "$census")
    if [ -f "$mutation_marker" ] && [ "$(sed -n '1p' "$mutation_marker")" = "$generation" ] \
            && docker inspect menhir-prod-app menhir-prod-neo4j >/dev/null 2>&1; then
        fence_args+=(--allow-production)
    fi
    python3 "$same_host_helper" "${fence_args[@]}"
    rm -f "$census"
    advance backup

    source_root="${BACKUP_ROOT}/decrypted/${generation}"
    manifest="${source_root}/MANIFEST.json"
    if [ -f "${STATUS_DIR}/restore-selection" ] \
            && [ "$(read_generation "${STATUS_DIR}/restore-selection" "restore selection")" = "$generation" ] \
            && [ -f "$manifest" ]; then
        schema_py validate-manifest "$manifest" "$source_root"
        manifest_sha="$(sha256sum "$manifest" | cut -d' ' -f1)"
        menhir_digest="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"]["menhir_image_digest"])' "$manifest")"
        neo4j_digest="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"]["neo4j_image_digest"])' "$manifest")"
        validate_receipt_binding "$backup_receipt" backup-local "$generation" \
            "$manifest_sha" "$menhir_digest" "$neo4j_digest"
        advance staged

        rehearsal="${STATUS_DIR}/rehearsal-receipt.json"
        rehearsal_marker="${BACKUP_ROOT}/candidate/${generation}/REHEARSAL-PASSED"
        if [ -f "$rehearsal" ] && [ -f "$rehearsal_marker" ] \
                && [ "$(read_generation "$rehearsal_marker" "rehearsal marker")" = "$generation" ]; then
            validate_receipt_binding "$rehearsal" rehearsal "$generation" \
                "$manifest_sha" "$menhir_digest" "$neo4j_digest"
            advance rehearsal

            if [ -f "${STATUS_DIR}/candidate-generation" ] \
                    && [ "$(read_generation "${STATUS_DIR}/candidate-generation" "candidate generation")" = "$generation" ] \
                    && [ "$(docker inspect -f '{{.State.Health.Status}}' menhir-candidate-app 2>/dev/null || true)" = healthy ] \
                    && [ "$(docker inspect -f '{{.State.Health.Status}}' menhir-candidate-neo4j 2>/dev/null || true)" = healthy ]; then
                advance candidate
            fi

            accepted="${STATUS_DIR}/candidate-accept-receipt.json"
            if [ -f "$accepted" ] && [ -f "${STATUS_DIR}/candidate-accepted" ] \
                    && [ "$(read_generation "${STATUS_DIR}/candidate-accepted" "candidate acceptance")" = "$generation" ]; then
                validate_receipt_binding "$accepted" candidate-accept "$generation" \
                    "$manifest_sha" "$menhir_digest" "$neo4j_digest"
                if at_least candidate || { [ -f "$mutation_marker" ] \
                        && [ "$(sed -n '1p' "$mutation_marker")" = "$generation" ]; }; then
                    advance accepted
                fi
            fi
        fi
    fi
fi

# Reconcile Caddy's own crash journal, then recognize an already-active bundle
# only when its frozen authority is byte-for-byte this release.
if at_least accepted && ! at_least routed; then
    caddy_release="${SCRIPT_DIR}/caddy-release.sh"
    [ -x "$caddy_release" ] || { echo "fixed Caddy release script is missing: $caddy_release" >&2; exit 1; }
    "$caddy_release" reconcile
    active_authority="/srv/menhir/production/caddy/current/release-authority.json"
    if [ -f "$active_authority" ] && [ ! -L "$active_authority" ]; then
        require_root_file "$active_authority" "active Caddy release authority"
    fi
    if [ -f "$active_authority" ] \
            && [ "$(sha256sum "$active_authority" | cut -d' ' -f1)" = "$release_sha" ]; then
        advance routed
    fi
fi

# Promotion is also reconstructible: the marker and both healthy reviewed
# production containers are required before progress is recognized.
if at_least routed && ! at_least promoted \
        && [ -f "${STATUS_DIR}/current-generation" ] \
        && [ "$(read_generation "${STATUS_DIR}/current-generation" "current generation")" = "$generation" ] \
        && [ "$(docker inspect -f '{{.State.Health.Status}}' menhir-prod-app 2>/dev/null || true)" = healthy ] \
        && [ "$(docker inspect -f '{{.State.Health.Status}}' menhir-prod-neo4j 2>/dev/null || true)" = healthy ]; then
    census="$(mktemp)"; ids="$(docker ps -aq)"
    if [ -n "$ids" ]; then
        # shellcheck disable=SC2086
        docker inspect $ids > "$census"
    else
        printf '[]\n' > "$census"
    fi
    python3 "$same_host_helper" verify "$RELEASE_JSON" \
        "${STATUS_DIR}/same-host-writer-fence.json" "$census" --allow-production
    rm -f "$census"
    advance promoted
fi

if ! at_least backup; then
    echo "release-run requires a completed local backup and verified desktop archive receipt" >&2
    echo "run the backup operation, archive it from the desktop, then retry release-run" >&2
    exit 1
fi

if ! at_least staged; then
    echo "[2/8] Decrypting and validating the release backup"
    staged="$("${SCRIPT_DIR}/stage-generation.sh" | sed -n 's/^staged_generation=//p' | tail -n 1)"
    [ "$staged" = "$generation" ] || { echo "staged generation does not match backup generation" >&2; exit 1; }
    write_stage staged "$generation"; stage=staged
fi

if ! at_least rehearsal; then
    echo "[3/8] Rehearsing the exact backup restore"
    "${SCRIPT_DIR}/restore-generation.sh" "$generation"
    write_stage rehearsal "$generation"; stage=rehearsal
fi

if ! at_least candidate; then
    echo "[4/8] Starting the mutation-fenced candidate"
    "${SCRIPT_DIR}/candidate-deploy.sh"
    write_stage candidate "$generation"; stage=candidate
fi

if ! at_least accepted; then
    echo "[5/8] Running candidate acceptance"
    acceptance_probe="${SCRIPT_DIR}/mcp_acceptance_probe.py"
    require_root_file "$acceptance_probe" "release-owned candidate acceptance probe"
    recall_token="$(python3 "$acceptance_probe" mint-candidate)"
    [ -n "$recall_token" ] || { echo "candidate probe token mint returned empty output" >&2; exit 1; }
    MENHIR_RECALL_TOKEN="$recall_token" "${SCRIPT_DIR}/candidate-accept.sh"
    unset recall_token
    write_stage accepted "$generation"; stage=accepted
fi

if ! at_least routed; then
    echo "[6/8] Applying the immutable Caddy route transaction"
    caddy_release="${SCRIPT_DIR}/caddy-release.sh"
    [ -x "$caddy_release" ] || { echo "fixed Caddy release script is missing: $caddy_release" >&2; exit 1; }
    "$caddy_release" release /srv/yawn/releases/menhir-route-candidate
    write_stage routed "$generation"; stage=routed
fi

if ! at_least promoted; then
    echo "[7/8] Promoting under the revalidated same-host writer fence"
    "${SCRIPT_DIR}/promote.sh"
    write_stage promoted "$generation"; stage=promoted
fi

if ! at_least complete; then
    echo "[8/8] Running public production acceptance"
    production_lock="/run/lock/menhir-production.lock"
    exec 9>"$production_lock"
    flock -n 9 || { echo "another Menhir production operation is active: $production_lock" >&2; exit 75; }
    acceptance_probe="${SCRIPT_DIR}/mcp_acceptance_probe.py"
    client_policy="${MENHIR_ROOT}/policy/client-policy.json"
    require_root_file "$acceptance_probe" "release-owned production acceptance probe"
    require_root_file "$client_policy" "production client policy"
    "${SCRIPT_DIR}/verify-artifacts"
    python3 "$acceptance_probe" production "$MENHIR_PUBLIC_BASE_URL" "$RELEASE_JSON" "$client_policy"
    write_stage complete "$generation"; stage=complete
fi

printf 'release_complete=%s generation=%s\n' "$release_id" "$generation"
