"""Accept today's unaddressable sidecar residue so erasure stops reporting it (CF-165).

The legacy `mcp_events` rows written before the CF-165 Phase C lineage migration carry memory
text with no subject key. Nothing identifies whose content they are, so no erasure can ever
reach them, and until this is run every erasure reports ``erased_incomplete`` on their account.

This does NOT erase anything. It records a decision: that this specific, already-existing batch
is accepted residue rather than an open question. The rows keep their content.

**It is bounded on purpose.** The waiver stores the current maximum row id per column, and only
rows at or below it are excluded. `mcp_events.id` is INTEGER PRIMARY KEY AUTOINCREMENT, so
SQLite never reuses an id -- any row written after this point gets a strictly higher one and is
still reported. A blanket "ignore rows with a NULL key" rule would have been simpler and would
have silently re-opened CF-165 for every future stranding.

Re-running widens the ceiling to cover anything stranded since, which is why it is an explicit
operator action and never automatic.

    python scripts/accept_erasure_residue.py --dry-run
    python scripts/accept_erasure_residue.py --note "legacy pre-Phase-C mcp_events, ticket X"
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from menhir.infrastructure.telemetry import default_telemetry_db_path
from menhir.infrastructure.telemetry.erasure_purge import (
    count_unaddressable_content,
    record_residue_waiver,
    waived_ceilings,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="sidecar path (default: the telemetry db)")
    parser.add_argument(
        "--note", default="", help="why this residue is accepted; recorded with the waiver"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be accepted, write nothing"
    )
    args = parser.parse_args()

    db = args.db or default_telemetry_db_path()
    with sqlite3.connect(db) as conn:
        existing = waived_ceilings(conn)
        outstanding = count_unaddressable_content(conn, apply_waivers=False)
        still_reported = count_unaddressable_content(conn)

        print(f"sidecar: {db}")
        print(f"unaddressable rows, ignoring waivers: {outstanding or '{}'}")
        print(f"currently still reported:            {still_reported or '{}'}")
        print(f"existing waiver ceilings:            {existing or '{}'}")

        if not outstanding:
            print("\nnothing unaddressable; no waiver needed.")
            return 0
        if args.dry_run:
            print("\n--dry-run: nothing written.")
            return 0
        if not args.note:
            print("\nrefusing to record an unexplained waiver; pass --note.", file=sys.stderr)
            return 2

        recorded = record_residue_waiver(conn, note=args.note)
        conn.commit()

    print("\naccepted:")
    for key, info in sorted(recorded.items()):
        print(f"  {key}: {info['row_count']} rows at or below id {info['max_id']}")
    print("\nThese rows still hold their content. Erasure will stop counting them as an open")
    print("question; anything stranded after this point is still reported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
