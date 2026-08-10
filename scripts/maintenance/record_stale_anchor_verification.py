"""Record a stale-anchor verification receipt against a running Menhir instance.

Usage:
    python scripts/maintenance/record_stale_anchor_verification.py
        --memory-uuid m1
        --project menhir
        --path src/foo.py
        --outcome still_valid
        --verified-by agent
        --notes "Inspected current file; memory still matches."

Optional:
    --url <base>          Base URL (default: http://127.0.0.1:8080/api)
    --basis <str>         Basis for verification (default: inspected_current_file)
    --hash <str>          Current file hash for provenance
    --verified-at <iso>   Override verification timestamp
    --dry-run             Validate body without sending
    --json                Output raw JSON response

Env:
    MENHIR_AGENT_KEY      Bearer token for authenticated endpoints.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description="Record a stale-anchor verification receipt")
    parser.add_argument("--url", default="http://127.0.0.1:8080/api",
                        help="menhir base URL (default: http://127.0.0.1:8080/api)")
    parser.add_argument("--memory-uuid", required=True)
    parser.add_argument("--project", default=None)
    parser.add_argument("--path", required=True)
    parser.add_argument("--outcome", required=True,
                        choices=["still_valid", "outdated", "needs_review", "superseded"])
    parser.add_argument("--verified-by", default="agent")
    parser.add_argument("--basis", default="inspected_current_file")
    parser.add_argument("--hash", default=None, help="current file hash (provenance)")
    parser.add_argument("--notes", default=None)
    parser.add_argument("--verified-at", default=None,
                        help="ISO timestamp (default: server-assigned)")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate body without sending")
    parser.add_argument("--json", action="store_true",
                        help="output raw JSON response")
    args = parser.parse_args()

    body = {
        "memory_uuid": args.memory_uuid,
        "project": args.project,
        "path": args.path,
        "outcome": args.outcome,
        "verified_by": args.verified_by,
        "basis": args.basis,
    }
    if args.hash:
        body["current_file_hash"] = args.hash
    if args.notes:
        body["notes"] = args.notes
    if args.verified_at:
        body["verified_at"] = args.verified_at

    endpoint = f"{args.url.rstrip('/')}/tool-events/stale-verifications"

    if args.dry_run:
        print(f"DRY RUN — would POST to {endpoint}")
        print(json.dumps(body, indent=2))
        sys.exit(0)

    headers = {"Content-Type": "application/json"}
    agent_key = os.environ.get("MENHIR_AGENT_KEY", "").strip()
    if agent_key:
        headers["Authorization"] = f"Bearer {agent_key}"

    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            if args.json:
                print(raw)
            else:
                parsed = json.loads(raw)
                ver = parsed.get("verification", {})
                print(f"accepted: {parsed.get('accepted')}")
                print(f"outcome:  {ver.get('outcome')}")
                print(f"path:     {ver.get('path')}")
                print(f"at:       {ver.get('verified_at')}")
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
