#!/usr/bin/env python3
"""Re-gate a frozen Phase 1 capture through the REAL parser and the REAL gate, with the two opt-in
relaxations on and off. No LLM calls: the model completions are replayed from the capture.

Why this exists rather than a reconstruction: an earlier hand-rolled model of the gate scored 41/100
where the real system commits 23/100, because it grouped by episode index instead of the durable
`source_key` (episode UUID + exact character offsets + claim ordinal) and skipped span grounding.
The relative cost of each label field was still informative, but no absolute number from a
reimplementation of the gate is admissible. This script imports the production functions instead:

    parse_scalar_row      -- the identical admission rules the live path uses
    gate_typed_scalars    -- the identical k-sample consistency gate

The LME graph is read-only here; nothing is persisted anywhere.

    python scripts/replay_gate_flags.py --jsonl %TEMP%/pr_final_KEEP.jsonl
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

from neo4j import GraphDatabase

from menhir.services.typed_scalar_perception import (
    _parse_json_array,
    gate_typed_scalars,
    parse_scalar_row,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lme_ground_truth as gt  # noqa: E402
from measure_absence_band import EPISODES_Q, build_episodes  # noqa: E402

#: The baseline (production) extraction prompt, identified by its opening. Arms B/C/D used the
#: experimental proposer prompt and are not the subject here -- the question is what the SHIPPING
#: path recovers once the key stops vetoing facts the samples already agree on.
BASELINE_PROMPT_PREFIX = "You convert"


def _numbers(text: str) -> list[float]:
    return [float(m) for m in re.findall(r"-?\d+(?:\.\d+)?", str(text).replace(",", ""))]


def committed_values(decisions) -> list[object]:
    return [d.proposal.value for d in decisions if d.committed and d.proposal is not None]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--uri", default="bolt://127.0.0.1:7701")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="lmedata123")
    args = ap.parse_args()

    completions: dict[tuple[str, int], list[str]] = collections.defaultdict(list)
    with open(args.jsonl, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("type") == "call" and str(row.get("system", "")).startswith(
                    BASELINE_PROMPT_PREFIX):
                completions[(row["ns"], int(row["trial"]))].append(row["completion"])

    suffixes = sorted({ns for ns, _ in completions})
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    episodes_by_ns: dict[str, list] = {}
    with driver.session() as session:
        for suffix in suffixes:
            found = session.run(
                "MATCH (e:Episodic) WHERE e.group_id ENDS WITH $s "
                "RETURN DISTINCT e.group_id AS ns", {"s": suffix}).data()
            if not found:
                print(f"  !! namespace {suffix} absent from source graph", file=sys.stderr)
                driver.close()
                return 2
            episodes_by_ns[suffix] = build_episodes(
                session.run(EPISODES_Q, {"ns": found[0]["ns"]}).data())
    driver.close()

    flags = [
        ("today (both off)", False, False),
        ("reconcile_attribute", True, False),
        ("align_spans", False, True),
        ("both", True, True),
    ]
    hits = collections.Counter()
    commits = collections.Counter()
    cells = 0

    for (suffix, _trial), texts in sorted(completions.items()):
        episodes = episodes_by_ns[suffix]
        samples = []
        for text in texts:
            raw_rows, failure = _parse_json_array(text)
            parsed = [] if failure else [
                p for p in (parse_scalar_row(r, episodes, lambda _r: None) for r in raw_rows)
                if p is not None
            ]
            samples.append(parsed)
        cells += 1
        need = gt.required_values(suffix)[-1]
        for name, reconcile, align in flags:
            decisions = gate_typed_scalars(
                samples, threshold=1.0,
                reconcile_attribute=reconcile, align_spans=align)
            values = committed_values(decisions)
            commits[name] += len(values)
            if any(abs(n - need) < 1e-9 for v in values for n in _numbers(v)):
                hits[name] += 1

    print(f"REAL parser + REAL gate, replayed from {cells} baseline namespace-trials (k=3)\n")
    print(f"{'configuration':<24}{'recovered the asked value':>26}{'total commits':>16}")
    for name, _r, _a in flags:
        print(f"{name:<24}{hits[name]:>19}/{cells:<6}{commits[name]:>16}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
