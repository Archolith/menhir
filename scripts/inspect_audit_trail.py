#!/usr/bin/env python3
"""Replay any audit trail (consolidation, recall, ...) from a run's telemetry DB.

Audit channels record a structured event per decision to the telemetry lifecycle_events store under
a per-domain ``component`` (consolidation_audit, recall_audit, ...), correlated by a unit-of-work id
in the episode_uuid column. This reads them back, grouped by unit, oldest-first (replay order), so
you can see EXACTLY why a decision went the way it did -- no log spelunking.

Usage:
  inspect_audit_trail.py --db <telemetry.db> [--component recall_audit] [--id <corr_id>] [--limit N]
      no --id  -> list units of work (newest first) with a one-line summary
      --id ID  -> full oldest-first replay of that unit
The DB path in the throwaway e2e harness is $MENHIR_MCP_TELEMETRY_DB (see run_scalar_state_e2e.sh).
"""

from __future__ import annotations

import argparse
import json
import os
from collections import OrderedDict
from pathlib import Path

from menhir.infrastructure.telemetry.store import McpTelemetryStore


def _details(row: dict) -> dict:
    raw = row.get("details_json")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {"_raw": raw}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.getenv("MENHIR_MCP_TELEMETRY_DB"),
                    help="path to the telemetry sqlite DB (default: $MENHIR_MCP_TELEMETRY_DB)")
    ap.add_argument("--component", default="consolidation_audit",
                    help="audit channel component (e.g. consolidation_audit, recall_audit)")
    ap.add_argument("--id", dest="corr_id", default=None, help="replay one unit-of-work correlation id")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()
    if not args.db:
        ap.error("no --db and $MENHIR_MCP_TELEMETRY_DB is unset")
    if not Path(args.db).exists():
        ap.error(f"telemetry DB not found: {args.db}")

    store = McpTelemetryStore(Path(args.db))
    rows = store.fetch_recent_lifecycle_events(
        limit=max(1, min(args.limit, 200)), component=args.component, episode_uuid=args.corr_id)
    rows = list(reversed(rows))  # oldest-first

    if not rows:
        print(f"no {args.component} events{' for id ' + args.corr_id if args.corr_id else ''}. "
              f"Was the channel toggled on for the run?")
        return

    if args.corr_id:
        print(f"== {args.component} unit {args.corr_id} ({len(rows)} events, oldest-first) ==")
        for r in rows:
            d = _details(r)
            extra = {k: v for k, v in d.items() if k not in ("namespace",)}
            print(f"  {r.get('recorded_at', '')[:23]}  {r.get('event'):14} {r.get('state'):20}"
                  f"  {json.dumps(extra, default=str)}")
        return

    units: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in rows:
        units.setdefault(str(r.get("episode_uuid")), []).append(r)
    print(f"== {len(units)} {args.component} unit(s) ==")
    for uid, evs in units.items():
        counts: dict[str, int] = {}
        for e in evs:
            key = f"{e.get('event')}:{e.get('state')}"
            counts[key] = counts.get(key, 0) + 1
        summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"  {uid}  ({len(evs)} events)  {summary}")
    print("\nreplay one with:  --id <corr_id>")


if __name__ == "__main__":
    main()
