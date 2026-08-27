#!/usr/bin/env bash
# Candidate acceptance verifier (blocker 5).
#
# Proves a mutation-fenced candidate-readonly deployment is acceptable for
# promotion and writes an atomic, structured acceptance receipt. It covers:
#   /readyz (mode-aware readiness + mutation fence), OAuth discovery,
#   existing-token recall (when a token is supplied), bounded mutation 503s,
#   tier/tool identity, authority before/after digests (no authoritative
#   mutation), the source-writer fence, and a required external prerequisite
#   receipt (Cloudflare/Caddy/firewall, produced by the external worker).
#
# Fail-closed: any probe failure or missing external receipt aborts and leaves
# no receipt. The receipt schema is validated by deploy/lib/menhir_schema.py;
# promotion re-parses the exact fields and never reads mtime.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=release-lib.sh
. "${SCRIPT_DIR}/release-lib.sh"
# shellcheck source=secrets-map.sh
. "${SCRIPT_DIR}/secrets-map.sh"
HELPER_DIR="${SCRIPT_DIR}/lib"
[ -d "$HELPER_DIR" ] || HELPER_DIR="$SCRIPT_DIR"
SCHEMA="${HELPER_DIR}/menhir_schema.py"

[ "$#" -eq 0 ] || { echo "candidate-accept accepts no arguments" >&2; exit 2; }
load_production_env
acquire_release_lock

generation="$(read_generation "${STATUS_DIR}/candidate-generation" "candidate generation")"
candidate_root="${BACKUP_ROOT}/candidate/${generation}"
source_root="${BACKUP_ROOT}/decrypted/${generation}"
manifest="${source_root}/MANIFEST.json"
base_url="${MENHIR_CANDIDATE_BASE_URL:-${MENHIR_PUBLIC_BASE_URL}}"
external_receipt="${MENHIR_EXTERNAL_RECEIPT:-${STATUS_DIR}/external-prerequisite.json}"
recall_token="${MENHIR_RECALL_TOKEN:-}"
if [ -z "$recall_token" ]; then
    token_file="${MENHIR_ACCEPTANCE_TOKEN_FILE:-${MENHIR_PROD_ROOT}/secrets/menhir/acceptance-token}"
    require_root_file "$token_file" "short-lived candidate acceptance token"
    [ "$(wc -l < "$token_file")" -eq 1 ] \
        || { echo "candidate acceptance token must contain exactly one line" >&2; exit 1; }
    IFS= read -r recall_token < "$token_file"
fi

for cmd in curl docker python3 sha256sum find sort xargs; do
    command -v "$cmd" >/dev/null 2>&1 || { echo "required tool not found: $cmd" >&2; exit 1; }
done

# --- Static authority: manifest + release + rehearsal marker ---
[ -f "$manifest" ] || { echo "candidate manifest missing: $manifest" >&2; exit 1; }
python3 "$SCHEMA" validate-manifest "$manifest" "$source_root" \
    || { echo "candidate manifest validation failed" >&2; exit 1; }
manifest_sha256="$( (cd "$source_root" && sha256sum MANIFEST.json | cut -d' ' -f1) )"
release_id="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["release_id"])' \
    "${MENHIR_PROD_ROOT}/release/release.json")"
python3 "$SCHEMA" validate-release "${MENHIR_PROD_ROOT}/release/release.json"

[ "$(read_generation "${candidate_root}/REHEARSAL-PASSED" "rehearsal marker")" = "$generation" ] \
    || { echo "candidate rehearsal marker mismatch" >&2; exit 1; }
[ "$(read_generation "${STATUS_DIR}/restored-generation" "restored production generation")" = "$generation" ] \
    || { echo "candidate is not running the restored production generation" >&2; exit 1; }

# --- External prerequisite receipt (Cloudflare/Caddy/firewall) ---
[ -n "$external_receipt" ] || { echo "MENHIR_EXTERNAL_RECEIPT is required" >&2; exit 1; }
[ -f "$external_receipt" ] && [ ! -L "$external_receipt" ] \
    || { echo "external prerequisite receipt must be a regular file" >&2; exit 1; }
validate_external_prerequisite_binding "$external_receipt" \
    || { echo "external prerequisite receipt failed validation or binding" >&2; exit 1; }
external_sha256="$(sha256sum "$external_receipt" | cut -d' ' -f1)"

# Acceptance baseline is authored by candidate-deploy after canonical rehearsal
# state is restored and Neo4j is queryable, but before the Menhir app starts.
# The path and authority roots are fixed; callers cannot substitute a root.
prestart_receipt="${STATUS_DIR}/candidate-prestart-authority.json"
require_root_file "$prestart_receipt" "candidate pre-start authority receipt"
release_manifest_sha256="$(sha256sum "$RELEASE_JSON" | cut -d' ' -f1)"
authority_before="$(python3 - "$prestart_receipt" "$generation" "$manifest_sha256" \
    "$release_manifest_sha256" <<'PYEOF'
import datetime,json,re,sys
path,generation,manifest_sha,release_sha=sys.argv[1:5]
def hook(pairs):
    out={}
    for key,value in pairs:
        if key in out: raise ValueError("duplicate key: "+key)
        out[key]=value
    return out
with open(path,encoding="utf-8") as handle:
    value=json.load(handle,object_pairs_hook=hook)
keys={"schema","kind","generation","manifest_sha256","release_manifest_sha256","authority_sha256","checked_utc"}
if set(value)!=keys or value.get("schema")!=1 or value.get("kind")!="candidate-prestart-authority":
    raise SystemExit("candidate pre-start authority schema mismatch")
if value["generation"]!=generation or value["manifest_sha256"]!=manifest_sha or value["release_manifest_sha256"]!=release_sha:
    raise SystemExit("candidate pre-start authority binding mismatch")
if not re.fullmatch(r"[0-9a-f]{64}",value.get("authority_sha256","")):
    raise SystemExit("candidate pre-start authority digest malformed")
checked=datetime.datetime.fromisoformat(value["checked_utc"].replace("Z","+00:00"))
now=datetime.datetime.now(datetime.timezone.utc)
if checked>now+datetime.timedelta(seconds=60) or now-checked>datetime.timedelta(minutes=30):
    raise SystemExit("candidate pre-start authority receipt is stale")
print(value["authority_sha256"])
PYEOF
)"

# --- /readyz (mode-aware readiness + mutation fence) ---
readyz="$(curl -fsS "${base_url}/readyz")"
printf '%s' "$readyz" | grep -q '"mode":"candidate-readonly"' \
    || { echo "/readyz did not report candidate-readonly" >&2; exit 1; }
printf '%s' "$readyz" | grep -q '"status":"ready"' \
    || { echo "/readyz did not report ready" >&2; exit 1; }
printf '%s' "$readyz" | grep -q '"mutation_fence":true' \
    || { echo "/readyz mutation fence not active" >&2; exit 1; }

# --- OAuth discovery ---
curl -fsS "${base_url}/.well-known/jwks.json" >/dev/null \
    || { echo "OAuth discovery (jwks) failed" >&2; exit 1; }

# --- Existing-token recall (mandatory for production acceptance) ---
[ -n "$recall_token" ] || { echo "MENHIR_RECALL_TOKEN is required for acceptance" >&2; exit 1; }
python3 "${HELPER_DIR}/mcp_acceptance_probe.py" "$base_url" "$recall_token" \
    || { echo "existing-token MCP recall or mutation-fence probe failed" >&2; exit 1; }
recall="ok"

# --- Bounded mutation refusal: explicit fenced 503 contract ---
# Transport success is required; only status 503 carrying the fenced contract
# ("temporarily_unavailable" / "does not admit authority mutations") is accepted.
assert_candidate_mutation_fenced() { # label url content-type data
    local label="$1" url="$2" ctype="$3" payload="$4" code body tmp
    tmp="$(mktemp)"
    code="$(curl -sS -o "$tmp" -w '%{http_code}' --max-time 15 \
        -X POST "$url" -H "content-type: $ctype" -d "$payload" || true)"
    if [ -z "$code" ] || [ "$code" = "000" ]; then
        echo "candidate mutation probe ($label) had no HTTP transport response" >&2
        rm -f "$tmp"; return 1
    fi
    body="$(cat "$tmp" 2>/dev/null || true)"
    rm -f "$tmp"
    if [ "$code" != "503" ]; then
        echo "candidate mutation ($label) returned ${code}, expected explicit 503" >&2
        return 1
    fi
    printf '%s' "$body" | grep -q '"temporarily_unavailable"' \
        || { echo "candidate mutation ($label) 503 lacked the fenced contract" >&2; return 1; }
    printf '%s' "$body" | grep -q 'does not admit authority mutations' \
        || { echo "candidate mutation ($label) 503 lacked the fenced description" >&2; return 1; }
}

assert_candidate_mutation_fenced "oauth/register" \
    "${base_url}/oauth/register" 'application/json' \
    '{"client_name":"accept-probe"}'

# Candidate mode fences every token mutation endpoint before client policy is
# consulted. Tier/tool identity was proved by tools/list + recall above.
assert_candidate_mutation_fenced "oauth/token" \
    "${base_url}/oauth/token" 'application/x-www-form-urlencoded' \
    'grant_type=client_credentials&client_id=unknown-not-in-policy'

authority_after="$(candidate_authority_digest "$generation")"
[ "$authority_before" = "$authority_after" ] \
    || { echo "authoritative state changed during candidate acceptance" >&2; exit 1; }

# --- Write the atomic structured receipt ---
release_json="$RELEASE_JSON"
# Already bound to the pre-start receipt above; recompute to detect replacement.
[ "$(sha256sum "$release_json" | cut -d' ' -f1)" = "$release_manifest_sha256" ] \
    || { echo "release authority changed during candidate acceptance" >&2; exit 1; }
manifest_menhir_digest="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"]["menhir_image_digest"])' "$manifest")"
manifest_neo4j_digest="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["build"]["neo4j_image_digest"])' "$manifest")"

# The candidate generation must itself be from this exact immutable release.
python3 - "$manifest" "$release_json" "$release_manifest_sha256" <<'PYEOF'
import json, os, sys

def strict(path):
    def hook(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate key: %s" % key)
            out[key] = value
        return out
    with open(path, encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=hook)

manifest, release = strict(sys.argv[1]), strict(sys.argv[2])
if manifest["release"]["release_id"] != release["release_id"]:
    raise SystemExit("candidate manifest release_id mismatch")
if manifest["release"]["release_manifest_sha256"] != sys.argv[3]:
    raise SystemExit("candidate manifest release digest mismatch")
if manifest["build"]["menhir_image_digest"] != release["images"]["menhir"]:
    raise SystemExit("candidate Menhir image mismatch")
if manifest["build"]["neo4j_image_digest"] != release["images"]["neo4j"]:
    raise SystemExit("candidate Neo4j image mismatch")
PYEOF

receipt="${STATUS_DIR}/candidate-accept-receipt.json"
receipt_tmp="$(mktemp "${STATUS_DIR}/.accept.XXXXXXXX")"
python3 - "$receipt_tmp" "$generation" "$manifest_sha256" "$release_id" \
    "$release_manifest_sha256" "$manifest_menhir_digest" "$manifest_neo4j_digest" \
    "$external_sha256" "$authority_before" "$authority_after" "$recall" <<'PYEOF'
import json, os, sys
(path, generation, manifest_sha256, release_id, release_manifest_sha256,
 menhir_digest, neo4j_digest, external, before, after, recall) = sys.argv[1:12]
receipt = {
    "schema": 1,
    "kind": "candidate-accept",
    "generation": generation,
    "manifest_sha256": manifest_sha256,
    "release": {
        "release_id": release_id,
        "release_manifest_sha256": release_manifest_sha256,
        "menhir_image_digest": menhir_digest,
        "neo4j_image_digest": neo4j_digest,
    },
    "readyz": "ok",
    "oauth_discovery": "ok",
    "recall": recall,
    "mutation_503": "ok",
    "tier_tool_identity": "ok",
    "authority_before_digest": before,
    "authority_after_digest": after,
    "external_prerequisite_receipt": external,
    "checked_utc": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
}
with open(path, "w", encoding="ascii") as f:
    json.dump(receipt, f, indent=2, sort_keys=True)
    f.write("\n")
    f.flush()
    os.fsync(f.fileno())
PYEOF
chown 0:0 "$receipt_tmp" && chmod 0400 "$receipt_tmp"
mv -f "$receipt_tmp" "$receipt"
python3 - "$STATUS_DIR" <<'PYEOF'
import os, sys
fd=os.open(sys.argv[1], os.O_RDONLY)
try: os.fsync(fd)
finally: os.close(fd)
PYEOF
python3 "$SCHEMA" validate-receipt "$receipt" candidate-accept \
    || { echo "candidate acceptance receipt failed validation" >&2; exit 1; }

write_generation_record "${STATUS_DIR}/candidate-accepted" "$generation"
printf 'candidate_accepted=%s\n' "$generation"
