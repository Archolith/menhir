#!/usr/bin/env python3
"""Stale Verification Diagnostics Pack v1 — report stale verification receipt coverage.

Fetches stale file-anchored memories and their verification receipts, then produces
a diagnostics report showing which stale anchors have valid post-dirty same-path
receipts, which receipts are ignored (and why), and the latest valid outcome per anchor.

Report-only: never reads file contents, never clears dirty flags, never writes receipts.

Usage:
    python scripts/maintenance/report_stale_verifications.py --project smoke-hook-center-stale-lane
    python scripts/maintenance/report_stale_verifications.py --project smoke-hook-center-stale-lane --json
    python scripts/maintenance/report_stale_verifications.py --project smoke-hook-center-stale-lane --url http://127.0.0.1:8099

Config (env):
    MENHIR_TOOL_EVENTS_URL   Tool-events URL (may end with /dirty, /stale, or the base)
    MENHIR_URL               Base URL for the menhir server
    MENHIR_READONLY_KEY      Bearer token for readonly endpoints
    MENHIR_AGENT_KEY         Fallback Bearer token (used if READONLY_KEY is not set)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _parse_iso_utc(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return dt if dt.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------

_TOOL_EVENTS_SUFFIXES = ("/dirty", "/stale", "/stale-verifications")


def _normalize_tool_events_url(raw: str) -> str:
    """Normalize a URL to the tool-events base: strip endpoint suffixes or append /api/tool-events."""
    base = raw.rstrip("/")
    # Strip full endpoint suffix (/api/tool-events/dirty, /api/tool-events/stale, etc.)
    for suffix in _TOOL_EVENTS_SUFFIXES:
        full_suffix = "/api/tool-events" + suffix
        if base.endswith(full_suffix):
            return base[: -len(full_suffix)] + "/api/tool-events"
    # Already a tool-events base
    if base.endswith("/api/tool-events"):
        return base
    # Strip bare endpoint suffix (e.g. http://x/dirty, http://x/stale)
    for suffix in _TOOL_EVENTS_SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    # Server root — append /api/tool-events
    return base + "/api/tool-events"


def _resolve_tool_events_base(url_override: str | None = None) -> str:
    """Resolve the tool-events base URL from --url, MENHIR_TOOL_EVENTS_URL, or MENHIR_URL.

    Returns the URL prefix before the specific endpoint path (e.g.
    ``http://host:port/api/tool-events``), so callers append ``/stale`` or
    ``/stale-verifications``.
    """
    if url_override:
        return _normalize_tool_events_url(url_override)
    te = (os.environ.get("MENHIR_TOOL_EVENTS_URL") or "").strip()
    if te:
        return _normalize_tool_events_url(te)
    base = (os.environ.get("MENHIR_URL") or "").strip()
    if base:
        return base.rstrip("/") + "/api/tool-events"
    return "http://127.0.0.1:8090/api/tool-events"


def _resolve_stale_url(base: str, project: str | None) -> str:
    import urllib.parse
    url = base + "/stale"
    if project:
        url += f"?project={urllib.parse.quote(project, safe='')}"
    return url


def _resolve_verifications_url(base: str, memory_uuid: str) -> str:
    import urllib.parse
    return (base + "/stale-verifications"
            + f"?memory_uuid={urllib.parse.quote(memory_uuid, safe='')}&limit=200")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _build_request(url: str) -> urllib.request.Request:
    headers = {"Accept": "application/json"}
    key = (os.environ.get("MENHIR_READONLY_KEY") or "").strip()
    if not key:
        key = (os.environ.get("MENHIR_AGENT_KEY") or "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return urllib.request.Request(url, headers=headers, method="GET")


def _fetch_json(url: str) -> dict | list:
    req = _build_request(url)
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"Error: failed to fetch from {url}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _fetch_json_list(url: str) -> list:
    result = _fetch_json(url)
    if isinstance(result, dict):
        return result.get("stale_anchors") or result.get("verifications") or []
    if isinstance(result, list):
        return result
    return []


# ---------------------------------------------------------------------------
# Receipt classification (pure, testable)
# ---------------------------------------------------------------------------

IGNORED_REASON_WRONG_MEMORY = "wrong_memory_uuid"
IGNORED_REASON_WRONG_PROJECT = "wrong_project"
IGNORED_REASON_WRONG_PATH = "wrong_path"
IGNORED_REASON_PRE_DIRTY = "pre_dirty"
IGNORED_REASON_TIMESTAMP_ERROR = "timestamp_error"
IGNORED_REASON_ANCHOR_TIMESTAMP_ERROR = "anchor_timestamp_error"


def classify_receipt(receipt: dict, anchor: dict) -> tuple[bool, str | None]:
    """Classify a verification receipt against a stale anchor.

    Returns (is_valid, ignored_reason_or_None).
    A valid receipt must match memory_uuid, project, path, and have
    verified_at >= dirty_at with parseable timestamps for both.
    Missing or malformed dirty_at is conservative — we cannot prove
    verified_at >= dirty_at, so the receipt is ignored.
    """
    if receipt.get("memory_uuid") != anchor.get("memory_uuid"):
        return False, IGNORED_REASON_WRONG_MEMORY

    if receipt.get("project") != anchor.get("project"):
        return False, IGNORED_REASON_WRONG_PROJECT

    if receipt.get("path") != anchor.get("path"):
        return False, IGNORED_REASON_WRONG_PATH

    v_dt = _parse_iso_utc(receipt.get("verified_at"))
    if v_dt is None:
        return False, IGNORED_REASON_TIMESTAMP_ERROR

    d_dt = _parse_iso_utc(anchor.get("dirty_at"))
    if d_dt is None:
        return False, IGNORED_REASON_ANCHOR_TIMESTAMP_ERROR
    if v_dt < d_dt:
        return False, IGNORED_REASON_PRE_DIRTY

    return True, None


def _sort_key(receipt: dict) -> datetime:
    dt = _parse_iso_utc(receipt.get("verified_at", ""))
    return dt or datetime.min.replace(tzinfo=timezone.utc)


def _slim_receipt(receipt: dict, *, reason: str | None = None) -> dict:
    out = {
        "outcome": receipt.get("outcome", ""),
        "path": receipt.get("path", ""),
        "verified_at": receipt.get("verified_at", ""),
    }
    if reason:
        out["reason"] = reason
    return out


def _slim_valid_receipt(receipt: dict) -> dict:
    return {
        "outcome": receipt.get("outcome", ""),
        "verified_at": receipt.get("verified_at", ""),
        "verified_by": receipt.get("verified_by", ""),
        "basis": receipt.get("basis", ""),
    }


# ---------------------------------------------------------------------------
# Report building (pure, testable)
# ---------------------------------------------------------------------------

def _derive_status(valid_receipts: list[dict], ignored_receipts: list[dict]) -> str:
    if not valid_receipts:
        return "only_ignored_receipts" if ignored_receipts else "no_receipt"
    latest = max(valid_receipts, key=_sort_key)
    outcome = (latest.get("outcome") or "").lower()
    if outcome == "still_valid":
        return "valid_still_valid"
    if outcome == "outdated":
        return "valid_outdated"
    return "valid_unclear"


def build_report_items(stale_anchors: list[dict],
                       verifications_map: dict[str, list[dict]]) -> list[dict]:
    items: list[dict] = []
    for anchor in stale_anchors:
        memory_uuid = anchor.get("memory_uuid", "") or ""
        path = anchor.get("path", "") or ""
        receipts = verifications_map.get(memory_uuid, [])

        valid_receipts: list[dict] = []
        ignored_receipts: list[dict] = []

        for receipt in receipts:
            is_valid, reason = classify_receipt(receipt, anchor)
            if is_valid:
                valid_receipts.append(receipt)
            else:
                ignored_receipts.append(_slim_receipt(receipt, reason=reason))

        valid_receipts.sort(key=_sort_key, reverse=True)
        latest = valid_receipts[0] if valid_receipts else None

        items.append({
            "memory_uuid": memory_uuid,
            "path": path,
            "dirty_at": anchor.get("dirty_at", ""),
            "anchored_at": anchor.get("anchored_at", ""),
            "latest_valid_receipt": _slim_valid_receipt(latest) if latest else None,
            "status": _derive_status(valid_receipts, ignored_receipts),
            "ignored_receipts": ignored_receipts,
        })
    return items


def build_counts(items: list[dict]) -> dict[str, int]:
    stale_anchors = len(items)
    with_valid = sum(1 for it in items if it["latest_valid_receipt"] is not None)
    without_valid = stale_anchors - with_valid
    ignored_total = sum(len(it["ignored_receipts"]) for it in items)
    valid_still_valid = sum(
        1 for it in items
        if it["status"] == "valid_still_valid"
    )
    valid_outdated = sum(
        1 for it in items
        if it["status"] == "valid_outdated"
    )

    return {
        "stale_anchors": stale_anchors,
        "with_valid_receipt": with_valid,
        "without_valid_receipt": without_valid,
        "ignored_receipts": ignored_total,
        "valid_still_valid": valid_still_valid,
        "valid_outdated": valid_outdated,
    }


def build_full_report(stale_anchors: list[dict],
                      verifications_map: dict[str, list[dict]]) -> dict:
    items = build_report_items(stale_anchors, verifications_map)
    counts = build_counts(items)
    return {"counts": counts, "items": items}


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_human(report: dict, project: str | None) -> None:
    counts = report["counts"]
    items = report["items"]

    print("Stale verification diagnostics")
    if project:
        print(f"Project: {project}")
    print()
    print("Counts:")
    print(f"  stale anchors:              {counts['stale_anchors']}")
    print(f"  with valid receipt:         {counts['with_valid_receipt']}")
    print(f"  without valid receipt:      {counts['without_valid_receipt']}")
    print(f"  ignored receipts:           {counts['ignored_receipts']}")
    print(f"  valid/still_valid:          {counts['valid_still_valid']}")
    print(f"  valid/outdated:             {counts['valid_outdated']}")
    print()

    if items:
        print("Items:")
        for it in items:
            pth = it["path"] or "?"
            muid = it["memory_uuid"] or "?"
            print(f"  {pth} :: {muid}")
            print(f"    status: {it['status']}")
            print(f"    dirty_at: {it['dirty_at']}")
            lvr = it.get("latest_valid_receipt")
            if lvr:
                print(f"    latest valid receipt: {lvr['outcome']} @ {lvr['verified_at']}"
                      f" by {lvr['verified_by']}")
            print(f"    ignored receipts: {len(it['ignored_receipts'])}")
            print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    project: str | None = None
    url: str | None = None
    json_mode = False

    args = list(sys.argv[1:])
    i = 0
    while i < len(args):
        if args[i] == "--project" and i + 1 < len(args):
            project = args[i + 1]
            i += 2
        elif args[i] == "--url" and i + 1 < len(args):
            url = args[i + 1]
            i += 2
        elif args[i] == "--json":
            json_mode = True
            i += 1
        else:
            i += 1

    base = _resolve_tool_events_base(url_override=url)
    stale_url = _resolve_stale_url(base, project)

    # Fetch stale anchors
    stale_data = _fetch_json(stale_url)
    if isinstance(stale_data, dict):
        stale_anchors = stale_data.get("stale_anchors") or []
        if not stale_anchors and "count" in stale_data:
            stale_anchors = stale_data.get("stale_anchors", [])
    elif isinstance(stale_data, list):
        stale_anchors = stale_data
    else:
        stale_anchors = []

    if not stale_anchors:
        report = {"counts": build_counts([]), "items": []}
        if json_mode:
            print(json.dumps(report, indent=2))
        else:
            print_human(report, project)
        return 0

    # Collect unique memory_uuids and fetch verifications
    memory_uuids = list(dict.fromkeys(
        a.get("memory_uuid", "") for a in stale_anchors if a.get("memory_uuid")
    ))
    verifications_map: dict[str, list[dict]] = {}
    for muid in memory_uuids:
        vurl = _resolve_verifications_url(base, muid)
        verifications_map[muid] = _fetch_json_list(vurl)

    report = build_full_report(stale_anchors, verifications_map)
    report["project"] = project or ""

    if json_mode:
        print(json.dumps(report, indent=2))
    else:
        print_human(report, project)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
