#!/usr/bin/env python3
"""Hook Center Actionability Pack v1: report dirty files and stale anchored memories.

Fetches diagnostic data from `GET /api/tool-events/dirty` and prints a human-readable
summary or raw JSON. Report-only: never clears dirty flags or refreshes structure.

Usage:
    python scripts/maintenance/report_dirty_files.py
    python scripts/maintenance/report_dirty_files.py --json

Config (env):
    MENHIR_TOOL_EVENTS_URL   Exact URL (default http://127.0.0.1:8090/api/tool-events/dirty)
    MENHIR_URL               Base URL for the menhir server
    MENHIR_AGENT_KEY         Bearer token for authenticated endpoints
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request


def _resolve_url() -> str:
    tool_events = (os.environ.get("MENHIR_TOOL_EVENTS_URL") or "").strip()
    if tool_events:
        base = tool_events.rstrip("/")
        return base + "/dirty" if not base.endswith("/dirty") else base
    base = (os.environ.get("MENHIR_URL") or "").strip()
    if base:
        return base.rstrip("/") + "/api/tool-events/dirty"
    return "http://127.0.0.1:8090/api/tool-events/dirty"


def _build_request(url: str) -> urllib.request.Request:
    headers = {"Accept": "application/json"}
    key = (os.environ.get("MENHIR_AGENT_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return urllib.request.Request(url, headers=headers, method="GET")


def fetch_dirty(url: str | None = None) -> dict:
    url = url or _resolve_url()
    req = _build_request(url)
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"Error: failed to fetch diagnostics from {url}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def print_summary(data: dict) -> None:
    counts = data.get("counts", {})
    dirty_files = data.get("dirty_files", [])
    stale_anchors = data.get("stale_anchors", [])

    print(f"Dirty files count:      {counts.get('dirty_files', len(dirty_files))}")
    print(f"Stale anchors count:    {counts.get('stale_anchors', len(stale_anchors))}")
    print()

    if dirty_files:
        print("Dirty files:")
        for f in dirty_files:
            proj = f.get("project", "?")
            pth = f.get("path", "?")
            op = f.get("operation", "")
            print(f"  [{proj}] {pth}  ({op})")

    if stale_anchors:
        print("Stale anchored memories:")
        for a in stale_anchors:
            name = a.get("name") or a.get("memory_uuid", "?")
            pth = a.get("path", "?")
            op = a.get("operation", "")
            print(f"  {name}  ->  {pth}  ({op})")


def main() -> int:
    url = None
    if "--url" in sys.argv[1:]:
        idx = sys.argv.index("--url") + 1
        if idx < len(sys.argv):
            url = sys.argv[idx]
    data = fetch_dirty(url)
    if "--json" in sys.argv[1:]:
        print(json.dumps(data, indent=2))
    else:
        print_summary(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
