#!/usr/bin/env bash
# Validate the immutable root-owned release authority record (release.json).
# Fail-closed and fixed-path: the record must be a regular, non-symlink,
# root-owned, non-group/other-writable file at MENHIR_RELEASE_JSON.
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCHEMA="${SCRIPT_DIR}/lib/menhir_schema.py"

MENHIR_RELEASE_JSON="${MENHIR_RELEASE_JSON:-/srv/menhir/production/release/release.json}"

[ -f "$MENHIR_RELEASE_JSON" ] || { echo "release.json not found: $MENHIR_RELEASE_JSON" >&2; exit 1; }
[ ! -L "$MENHIR_RELEASE_JSON" ] || { echo "release.json must not be a symlink" >&2; exit 1; }
[ "$(stat -c '%u' "$MENHIR_RELEASE_JSON")" = 0 ] || { echo "release.json must be root-owned" >&2; exit 1; }
# shellcheck disable=SC2016
mode="$(stat -c '%a' "$MENHIR_RELEASE_JSON")"
(( ((8#${mode}) & 8#022) == 0 )) || { echo "release.json must not be group/other writable" >&2; exit 1; }

python3 "$SCHEMA" validate-release "$MENHIR_RELEASE_JSON"
echo "release.json valid: $(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["release_id"])' "$MENHIR_RELEASE_JSON")"
