#!/usr/bin/env python3
"""Check the LME corpus `valid_at` against the benchmark fixture BEFORE repairing anything.

62.4% of episodes in the scalar-ku corpus carry an ingest timestamp (2026-07-2x) as `valid_at`
instead of the conversation time. Supersession decides which value is current by `valid_at`, so for
most of the corpus "which value is current" is being decided by ingestion order. The eggs case:
a duplicated '30 dozen' turn stamped 2026-07-23 beats the genuinely later '20 dozen' from 2023-03-15.

The fixture carries the authoritative times as parallel arrays:

    haystack_session_ids: ['answer_a25d4a91_1', 'answer_a25d4a91_2']
    haystack_dates:       ['2023/05/25 (Thu) 20:21', '2023/05/27 (Sat) 10:20']

and each episode's `name` embeds its session id (`episode-answer_a25d4a91_1-<uuid>`), so the mapping
is exact rather than inferred.

This script WRITES NOTHING. It reports, for every episode, whether the fixture time agrees with the
stored one. The episodes that already look correct are the control: if the mapping disagrees with
THOSE, the mapping is wrong and no repair should be run.

    PYTHONPATH=src python scripts/_verify_valid_at_repair.py
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
from datetime import datetime, timezone

from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lme_ground_truth as gt  # noqa: E402

#: '2023/05/25 (Thu) 20:21' -- the weekday is decorative and ignored.
_DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})\s*\([^)]*\)\s*(\d{2}):(\d{2})")

INGEST_CUTOFF = datetime(2026, 1, 1, tzinfo=timezone.utc)


def parse_haystack_date(raw: str) -> datetime | None:
    m = _DATE_RE.match(str(raw).strip())
    if not m:
        return None
    y, mo, d, h, mi = (int(g) for g in m.groups())
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def session_of(name: str) -> str | None:
    """Extract the fixture session id from an episode name.

    Two layouts exist and both must parse, or the verifier silently reports every episode as
    unmappable and a broken build looks like a clean one:

        episode-answer_a25d4a91_1-644c0e5a-...            (older builds)
        episode-lme-cc5ded98-answer_a5b68517_1-7ab...     (current: ingest.py namespace-qualifies
                                                           session ids, so the ns is in the name)

    Scanning for the `answer_`-prefixed component handles both instead of pinning an index.
    """
    for part in str(name or "").split("-"):
        if part.startswith("answer_"):
            return part
    return None


def build_session_times() -> dict[str, datetime]:
    """session_id -> authoritative start time, from the benchmark fixture."""
    out: dict[str, datetime] = {}
    for record in gt.load().values():
        ids = record.get("haystack_session_ids") or []
        dates = record.get("haystack_dates") or []
        if len(ids) != len(dates):
            print(f"  !! {record.get('question_id')}: {len(ids)} session ids vs "
                  f"{len(dates)} dates -- skipped", file=sys.stderr)
            continue
        for sid, raw in zip(ids, dates):
            parsed = parse_haystack_date(raw)
            if parsed is not None:
                out[str(sid)] = parsed
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uri", default="bolt://127.0.0.1:7701")
    ap.add_argument("--password", default="lmedata123")
    args = ap.parse_args()

    session_times = build_session_times()
    print(f"fixture: {len(gt.load())} questions -> {len(session_times)} session timestamps\n")
    if not session_times:
        print("no fixture data; cannot verify", file=sys.stderr)
        return 2

    driver = GraphDatabase.driver(args.uri, auth=("neo4j", args.password))
    tally = collections.Counter()
    disagreements: list[str] = []
    unmapped_sessions: collections.Counter = collections.Counter()

    with driver.session() as session:
        rows = session.run(
            "MATCH (e:Episodic) RETURN e.uuid AS uuid, e.name AS name, "
            "e.group_id AS ns, e.valid_at AS valid_at").data()

    for row in rows:
        sid = session_of(row["name"])
        if sid is None:
            tally["no session id in name"] += 1
            continue
        fixture_time = session_times.get(sid)
        if fixture_time is None:
            tally["session id absent from fixture"] += 1
            unmapped_sessions[sid] += 1
            continue

        stored = row["valid_at"]
        stored_dt = stored.to_native() if hasattr(stored, "to_native") else stored
        if stored_dt is None:
            tally["stored valid_at is null"] += 1
            continue
        if stored_dt.tzinfo is None:
            stored_dt = stored_dt.replace(tzinfo=timezone.utc)

        looks_ingested = stored_dt >= INGEST_CUTOFF
        agrees = abs((stored_dt - fixture_time).total_seconds()) < 60

        if looks_ingested:
            tally["INGEST-STAMPED -> repairable"] += 1
        elif agrees:
            tally["already correct, fixture AGREES (control)"] += 1
        else:
            tally["already correct, fixture DISAGREES"] += 1
            if len(disagreements) < 10:
                disagreements.append(
                    f"  {row['ns'][-8:]} {sid:<22} stored={stored_dt:%Y-%m-%d %H:%M} "
                    f"fixture={fixture_time:%Y-%m-%d %H:%M}")

    driver.close()

    for label, n in tally.most_common():
        print(f"  {n:>5}  {label}")
    if disagreements:
        print("\nDISAGREEMENTS on episodes that already looked correct "
              "-- if this list is non-empty the mapping is WRONG, do not repair:")
        for line in disagreements:
            print(line)
    if unmapped_sessions:
        print(f"\nsession ids not in fixture ({len(unmapped_sessions)} distinct):")
        for sid, n in unmapped_sessions.most_common(8):
            print(f"  {n:>4}  {sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
