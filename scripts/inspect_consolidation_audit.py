#!/usr/bin/env python3
"""Replay the consolidation audit trail from a run's telemetry DB.

The consolidation job, when MENHIR_PERSONAL_MEMORY_CONSOLIDATION_AUDIT_ENABLED is on, records a
structured event per decision (pass -> perceive -> bind_repair -> fold -> view_write ->
reconcile_retire -> merge/counter) to the telemetry lifecycle_events store under
component='consolidation_audit'. This reads them back, grouped by consolidation pass, oldest-first,
so you can see EXACTLY why a scalar View was written, retired, or superseded -- no log spelunking.

Usage:
  inspect_consolidation_audit.py --db <telemetry.db> [--pass <pass_id>] [--limit N]
      no --pass  -> list passes (newest first) with a one-line summary
      --pass ID  -> full oldest-first replay of that pass
The DB path in the throwaway e2e harness is $MENHIR_MCP_TELEMETRY_DB (see run_scalar_state_e2e.sh).
"""

from __future__ import annotations

import argparse
import json
import os
from collections import OrderedDict
from pathlib import Path

from menhir.infrastructure.consolidation_audit import COMPONENT
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
    ap.add_argument("--pass", dest="pass_id", default=None, help="replay one consolidation pass id")
    ap.add_argument("--limit", type=int, default=200)
    args = ap.parse_args()
    if not args.db:
        ap.error("no --db and $MENHIR_MCP_TELEMETRY_DB is unset")
    if not Path(args.db).exists():
        ap.error(f"telemetry DB not found: {args.db}")

    store = McpTelemetryStore(Path(args.db))
    rows = store.fetch_recent_lifecycle_events(
        limit=max(1, min(args.limit, 200)), component=COMPONENT, episode_uuid=args.pass_id)
    rows = list(reversed(rows))  # oldest-first

    if not rows:
        print(f"no consolidation_audit events{' for pass ' + args.pass_id if args.pass_id else ''}. "
              "Was MENHIR_PERSONAL_MEMORY_CONSOLIDATION_AUDIT_ENABLED=1 for the run?")
        return

    if args.pass_id:
        print(f"== consolidation pass {args.pass_id} ({len(rows)} events, oldest-first) ==")
        for r in rows:
            d = _details(r)
            slot = d.get("slot")
            slot_s = f" slot={slot}" if slot else ""
            subj = d.get("subject_uuid")
            subj_s = f" subj={str(subj)[:8]}" if subj else ""
            extra = {k: v for k, v in d.items() if k not in ("slot", "subject_uuid", "namespace")}
            print(f"  {r.get('recorded_at','')[:23]}  {r.get('event'):16} {r.get('state'):14}"
                  f"{subj_s}{slot_s}  {json.dumps(extra, default=str)}")
        return

    # summary: group by pass id (the episode_uuid column)
    passes: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in rows:
        passes.setdefault(str(r.get("episode_uuid")), []).append(r)
    print(f"== {len(passes)} consolidation pass(es) ==")
    for pid, evs in passes.items():
        counts: dict[str, int] = {}
        for e in evs:
            key = f"{e.get('event')}:{e.get('state')}"
            counts[key] = counts.get(key, 0) + 1
        summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"  {pid}  ({len(evs)} events)  {summary}")
    print("\nreplay one with:  --pass <pass_id>")


if __name__ == "__main__":
    main()
