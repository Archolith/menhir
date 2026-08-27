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

# Promotion is the only step that can create a second writable authority. It
# therefore requires a separate root-owned receipt proving the old/local writer
# was stopped and a mutation probe against it was denied. Candidate acceptance
# happens earlier and is intentionally not allowed to assert this condition.
#
# Source-writer fencing is adversarial: the probe must prove BOTH that the
# frozen receipt names the exact expected source identity AND that the old/local
# writer, right now, authenticates a token from a fixed root-owned path and
# answers a safe mutation with the explicit fenced 503 contract. Transport
# failure (timeout / DNS / connection refused) and any identity/terminal status
# (401/403/404/wrong identity) FAIL — they can never be mistaken for "fenced".
source_fence="${MENHIR_SOURCE_FENCE_RECEIPT:-${STATUS_DIR}/source-writer-fence.json}"
source_fence_token_file="${MENHIR_SOURCE_FENCE_TOKEN_FILE:-${STATUS_DIR}/source-writer-fence-token}"
source_fence_token=""
source_probe_base="${MENHIR_SOURCE_FENCE_PROBE_URL:?MENHIR_SOURCE_FENCE_PROBE_URL is required}"
source_probe_ca="${MENHIR_SOURCE_FENCE_CA_FILE:?MENHIR_SOURCE_FENCE_CA_FILE is required}"
source_probe_cert="${MENHIR_SOURCE_FENCE_CLIENT_CERT_FILE:?MENHIR_SOURCE_FENCE_CLIENT_CERT_FILE is required}"
source_probe_key="${MENHIR_SOURCE_FENCE_CLIENT_KEY_FILE:?MENHIR_SOURCE_FENCE_CLIENT_KEY_FILE is required}"

python3 - "$source_probe_base" <<'PYEOF'
import sys, urllib.parse
url=urllib.parse.urlsplit(sys.argv[1])
if url.scheme != "https" or not url.hostname or url.username or url.password \
        or url.query or url.fragment or url.path not in ("", "/"):
    raise SystemExit("MENHIR_SOURCE_FENCE_PROBE_URL must be an HTTPS origin")
PYEOF
for tls_file in "$source_probe_ca" "$source_probe_cert" "$source_probe_key"; do
    require_root_file "$tls_file" "source-fence mTLS material"
done
expected_ca_sha="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["source_fence_tls_ca_sha256"])' "$RELEASE_JSON")"
[ "$(sha256sum "$source_probe_ca" | cut -d' ' -f1)" = "$expected_ca_sha" ] \
    || { echo "source-fence TLS CA differs from release authority" >&2; exit 1; }

load_source_fence_token() {
    require_root_file "$source_fence_token_file" "source-writer-fence token"
    [ "$(wc -l < "$source_fence_token_file")" -eq 1 ] \
        || { echo "source-writer-fence token must contain exactly one line" >&2; exit 1; }
    IFS= read -r source_fence_token < "$source_fence_token_file"
    [ -n "$source_fence_token" ] \
        || { echo "source-writer-fence token must not be empty" >&2; exit 1; }
}

assert_source_fence_identity() {
    require_root_file "$source_fence" "source-writer-fence receipt"
    load_source_fence_token
    schema_py verify-source-fence "$source_fence" "$RELEASE_JSON"
    local release_sha
    release_sha="$(sha256sum "$RELEASE_JSON" | cut -d' ' -f1)"
    SOURCE_FENCE_ID="$(python3 - "$source_fence" "$RELEASE_JSON" "$release_sha" <<'PYEOF'
import json, sys

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

receipt, release = strict(sys.argv[1]), strict(sys.argv[2])
if receipt["release_id"] != release["release_id"]:
    raise SystemExit("source-fence release_id mismatch")
if receipt["release_manifest_sha256"] != sys.argv[3]:
    raise SystemExit("source-fence release digest mismatch")
source_id = receipt["source_id"]
if not isinstance(source_id, str) or not source_id:
    raise SystemExit("source-fence source_id missing")
print(source_id)
PYEOF
    )"
    [ -n "$SOURCE_FENCE_ID" ] || { echo "source-fence source_id missing" >&2; exit 1; }
}

probe_source_fenced() {
    # Live, authenticated, safe-mutation fence probe: transport success is
    # required and only the explicit fenced 503 contract is accepted. Anything
    # else (including 401/403/404/timeout/DNS) means the source is NOT proven
    # fenced and promotion must stop.
    local code body tmp live_identity challenge fence_key_id release_id
    challenge="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    fence_key_id="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["source_fence_key_id"])' "$RELEASE_JSON")"
    release_id="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["release_id"])' "$RELEASE_JSON")"
    tmp="$(mktemp)"
    code="$(curl -sS -o "$tmp" -w '%{http_code}' --max-time 15 \
        --cacert "$source_probe_ca" --cert "$source_probe_cert" --key "$source_probe_key" \
        -X POST "${source_probe_base}/internal/source-fence" \
        -H "Authorization: Bearer ${source_fence_token}" \
        -H "X-Menhir-Fence-Challenge: ${challenge}" 2>"$tmp.e" || true)"
    [ "$code" = "200" ] \
        || { echo "source-writer-fence authenticated challenge failed (status=${code:-none}): $(cat "$tmp.e")" >&2; rm -f "$tmp" "$tmp.e"; return 1; }
    live_identity="$(python3 - "$tmp" "$RELEASE_JSON" "$challenge" \
        "$fence_key_id" "$release_id" <<'PYEOF'
import base64,json,sys
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
body=json.load(open(sys.argv[1],encoding="utf-8"))
release=json.load(open(sys.argv[2],encoding="utf-8"))
challenge,key_id,release_id=sys.argv[3:6]
signature=body.pop("signature","")
expected={
    "challenge":challenge,
    "instance_id":body.get("instance_id"),
    "key_id":key_id,
    "mutation_fence":True,
    "release_id":release_id,
    "runtime_mode":"candidate-readonly",
}
if body!=expected or not isinstance(body.get("instance_id"),str) or not body["instance_id"]:
    raise SystemExit("source-fence challenge claims mismatch")
payload=json.dumps(body,sort_keys=True,separators=(",", ":")).encode()
public=base64.urlsafe_b64decode(release["source_fence_public_key"]+"=")
sig=base64.urlsafe_b64decode(signature+"="*(-len(signature)%4))
try: Ed25519PublicKey.from_public_bytes(public).verify(sig,payload)
except Exception as exc: raise SystemExit("source-fence challenge signature mismatch") from exc
print(body["instance_id"])
PYEOF
    )" || { rm -f "$tmp" "$tmp.e"; return 1; }
    rm -f "$tmp" "$tmp.e"
    [ "$live_identity" = "$SOURCE_FENCE_ID" ] \
        || { echo "source-writer-fence live identity mismatch: expected ${SOURCE_FENCE_ID}, got ${live_identity:-missing}" >&2; return 1; }
    tmp="$(mktemp)"
    code="$(curl -sS -o "$tmp" -w '%{http_code}' --max-time 15 \
        --cacert "$source_probe_ca" --cert "$source_probe_cert" --key "$source_probe_key" \
        -X POST "${source_probe_base}/oauth/token" \
        -H "Authorization: Bearer ${source_fence_token}" \
        -H 'content-type: application/x-www-form-urlencoded' \
        -d 'grant_type=client_credentials&client_id=source-fence-probe-wrong-identity' \
        2>"$tmp.e" || true)"
    if [ -z "$code" ] || [ "$code" = "000" ]; then
        echo "source-writer-fence probe: transport failed (timeout/DNS/connection): $(cat "$tmp.e")" >&2
        rm -f "$tmp" "$tmp.e"; return 1
    fi
    body="$(cat "$tmp" 2>/dev/null || true)"
    rm -f "$tmp" "$tmp.e"
    if [ "$code" != "503" ]; then
        echo "old/local Menhir source returned ${code} (not the fenced 503) for a source-fence mutation" >&2
        return 1
    fi
    printf '%s' "$body" | grep -q '"temporarily_unavailable"' \
        || { echo "old/local Menhir source 503 did not carry the fenced mutation contract" >&2; return 1; }
    printf '%s' "$body" | grep -q 'does not admit authority mutations' \
        || { echo "old/local Menhir source 503 did not carry the fenced mutation description" >&2; return 1; }
    return 0
}

assert_source_fence_identity
probe_source_fenced

candidate="$(read_generation "${STATUS_DIR}/candidate-generation" "candidate generation")"
accepted="$(read_generation "${STATUS_DIR}/candidate-accepted" "candidate acceptance")"
restored="$(read_generation "${STATUS_DIR}/restored-generation" "restored generation")"
[ "$candidate" = "$accepted" ] && [ "$candidate" = "$restored" ] \
    || { echo "candidate/restored/accepted generations do not match" >&2; exit 1; }

# Candidate acceptance receipt must exact-match the candidate generation.
accept_receipt="$(accept_receipt_path)"
validate_receipt_file "$accept_receipt" candidate-accept
acc_gen="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["generation"])' "$accept_receipt")"
[ "$acc_gen" = "$candidate" ] || { echo "acceptance receipt generation mismatch" >&2; exit 1; }

# A fresh, complete, uploaded backup generation must exist (exact receipt, no mtime).
backup_receipt="$(backup_receipt_path)"
validate_receipt_file "$backup_receipt" backup-upload
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
validate_receipt_binding "$backup_receipt" backup-upload "$candidate" \
    "$manifest_sha" "$menhir_digest" "$neo4j_digest"

# The irreversible boundary is recorded durably BEFORE writable production can
# start. Any failure after this point is roll-forward/recovery only; stale
# candidate reattachment is forbidden even if the app never becomes healthy.
# Revalidate the source-writer fence immediately before the first target
# mutation: the frozen receipt must still name the exact identity and the live
# authenticated mutation probe must still return the explicit fenced 503.
assert_source_fence_identity
probe_source_fenced
record_first_mutation "$candidate"
candidate_down "$candidate"
if ! production_up; then
    production_down >/dev/null 2>&1 || true
    echo "production promotion failed after the durable mutation boundary; recovery required" >&2
    exit 1
fi
schema_py verify-source-fence "$source_fence" "$RELEASE_JSON"
probe_source_fenced
write_generation_record "${STATUS_DIR}/current-generation" "$candidate"
printf 'current_generation=%s\n' "$candidate"
