#!/usr/bin/env python3
"""Restore conversation-time `valid_at` on the LME scalar corpus from the benchmark fixture.

62.4% of episodes in `lme-scalar-ku-20260722-*` carry the INGEST timestamp (2026-07-2x) as
`valid_at` instead of the time the conversation turn actually happened. Supersession decides which
value is current by `valid_at`, so on this corpus "which value is current" was largely being decided
by ingestion order. Concretely (namespace ed4ddc30): the '30 dozen eggs' turn is duplicated, one copy
stamped 2026-07-23, and it therefore beats the genuinely later '20 dozen' turn from 2023-03-15 --
the fold picks 30, the benchmark expects 20, and the fold was right about its input.

The fixture holds the authoritative times as parallel arrays, and each episode's `name` embeds its
session id, so the mapping is EXACT rather than inferred:

    haystack_session_ids: ['answer_a25d4a91_1', ...]   <-> name 'episode-answer_a25d4a91_1-<uuid>'
    haystack_dates:       ['2023/05/25 (Thu) 20:21', ...]

VALIDATION (scripts/_verify_valid_at_repair.py): of the 1076 episodes whose stored time already
looked like a real conversation time, the fixture agrees with 100% and disagrees with none. That
control is what licenses rewriting the other 1707.

REVERSIBLE: the original value is copied to `valid_at_ingest_original` before the first overwrite.
Originally missing values have a separate marker so `--revert` can remove the repaired timestamp.
IDEMPOTENT: the target is recomputed from the fixture every run, so applying twice changes nothing.

    PYTHONPATH=src python scripts/repair_lme_valid_at.py             # dry run, writes nothing
    PYTHONPATH=src python scripts/repair_lme_valid_at.py --apply
    PYTHONPATH=src python scripts/repair_lme_valid_at.py --revert
"""

from __future__ import annotations

import argparse
import collections
import os
import sys
from datetime import timezone

from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lme_ground_truth as gt  # noqa: E402
from _verify_valid_at_repair import (  # noqa: E402
    INGEST_CUTOFF,
    NAMESPACE_PREFIX,
    build_session_times,
    session_of,
)


_REVERT = """
MATCH (e:Episodic)
WHERE e.group_id STARTS WITH $p
  AND (e.valid_at_ingest_original IS NOT NULL OR e.valid_at_ingest_was_missing = true)
SET e.valid_at = e.valid_at_ingest_original
REMOVE e.valid_at_ingest_original, e.valid_at_ingest_was_missing
RETURN count(e) AS n
"""

_APPLY_BATCH = """
UNWIND $rows AS row
MATCH (e:Episodic {uuid: row.uuid})
WHERE e.group_id STARTS WITH $p
SET e._lme_valid_at_repair_lock = true
WITH e, row,
     (e.group_id STARTS WITH $p AND e.group_id = row.namespace AND e.name = row.name
      AND e.valid_at_ingest_original IS NULL AND NOT coalesce(e.valid_at_ingest_was_missing, false)
      AND (e.valid_at = row.expected_valid_at
           OR (e.valid_at IS NULL AND row.expected_valid_at IS NULL))) AS unchanged
FOREACH (_ IN CASE WHEN unchanged THEN [1] ELSE [] END |
    SET e.valid_at_ingest_original = e.valid_at,
        e.valid_at_ingest_was_missing = CASE WHEN e.valid_at IS NULL THEN true END,
        e.valid_at = datetime(row.target)
)
REMOVE e._lme_valid_at_repair_lock
RETURN count(CASE WHEN unchanged THEN 1 END) AS n
"""


class _RepairConflict(Exception):
    """An episode changed after the dry-run premises were read."""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default="bolt://127.0.0.1:7701")
    ap.add_argument("--password", default=os.environ.get("MENHIR_BENCH_NEO4J_PASSWORD", ""))
    ap.add_argument("--prefix", default=NAMESPACE_PREFIX)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    mode.add_argument("--revert", action="store_true",
                      help="restore valid_at from valid_at_ingest_original and clear the backup")
    args = ap.parse_args()

    if args.revert:
        driver = GraphDatabase.driver(args.uri, auth=("neo4j", args.password))
        with driver.session() as session:
            n = session.run(_REVERT, {"p": args.prefix}).data()[0]["n"]
        driver.close()
        print(f"reverted {n} episodes to their original ingest timestamps")
        return 0

    try:
        session_times = build_session_times()
    except ValueError as exc:
        print(f"invalid fixture: {exc}", file=sys.stderr)
        return 2
    if not session_times:
        print("no fixture data; refusing to run", file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(args.uri, auth=("neo4j", args.password))
    with driver.session() as session:
        rows = session.run(
            "MATCH (e:Episodic) WHERE e.group_id STARTS WITH $p "
            "RETURN e.uuid AS uuid, e.name AS name, e.group_id AS ns, e.valid_at AS valid_at, "
            "e.valid_at_ingest_original AS original, e.valid_at_ingest_was_missing AS was_missing",
            {"p": args.prefix}).data()

    tally = collections.Counter()
    updates: list[dict[str, object]] = []
    for row in rows:
        sid = session_of(row["name"])
        if sid is None:
            tally["skipped: no session id in name"] += 1
            continue
        target = session_times.get((gt.question_id(row["ns"]), sid))
        if target is None:
            tally["skipped: session id absent from fixture"] += 1
            continue

        stored = row["valid_at"]
        stored_dt = stored.to_native() if hasattr(stored, "to_native") else stored
        if stored_dt is not None and stored_dt.tzinfo is None:
            stored_dt = stored_dt.replace(tzinfo=timezone.utc)

        repaired = row.get("original") is not None or row.get("was_missing") is True
        if stored_dt is not None and abs((stored_dt - target).total_seconds()) < 60:
            tally["already repaired" if repaired else "already correct"] += 1
            continue
        if repaired:
            tally["REPAIR (backup mismatch)"] += 1
        else:
            tally["REPAIR" if (stored_dt is None or stored_dt >= INGEST_CUTOFF)
                  else "REPAIR (non-ingest mismatch)"] += 1
        updates.append({
            "uuid": row["uuid"], "namespace": row["ns"], "name": row["name"],
            "expected_valid_at": stored, "target": target.isoformat(),
        })

    for label, n in sorted(tally.items()):
        print(f"  {n:>5}  {label}")
    print(f"\n{len(updates)} episodes would be rewritten")

    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply to repair.")
        driver.close()
        return 0

    if tally["REPAIR (non-ingest mismatch)"] or tally["REPAIR (backup mismatch)"]:
        print(
            "REFUSING --apply: timestamp mismatches on control or repaired episodes. "
            "Review the dry run before writing.",
            file=sys.stderr,
        )
        driver.close()
        return 2

    try:
        if updates:
            with driver.session() as session, session.begin_transaction() as tx:
                # Keep all batches in one transaction so conflicts roll back earlier writes.
                for i in range(0, len(updates), 500):
                    batch = updates[i:i + 500]
                    matched = tx.run(_APPLY_BATCH, {"rows": batch, "p": args.prefix}).data()[0]["n"]
                    if matched != len(batch):
                        raise _RepairConflict(f"matched {matched} of {len(batch)} planned episodes in a batch")
                tx.commit()
    except _RepairConflict as exc:
        print(f"REFUSING --apply: {exc}. No changes committed. Run the dry run again.", file=sys.stderr)
        return 2
    finally:
        driver.close()
    print(f"repaired {len(updates)} episodes with reversible backups")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
