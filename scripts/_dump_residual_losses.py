#!/usr/bin/env python3
"""Print every proposal in every claim group that still loses the asked value at the best gate
configuration (threshold 2/3, reconcile_attribute, align_spans).

Only ~12 cells survive as gate losses once extraction losses are excluded, which is few enough to
READ rather than bucket by heuristic. A classifier here would be inventing the answer; the point is
to see whether the residual disagreement is cosmetic (two names for one fact) or semantic (two
genuinely different facts that must not be merged).

Read-only. No LLM calls, no writes, no container.

    PYTHONPATH=src python scripts/_dump_residual_losses.py --jsonl %TEMP%/pr_final_KEEP.jsonl
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

from neo4j import GraphDatabase

from menhir.services.typed_scalar_rules import (
    _claim_groups,
    _parse_json_array,
    gate_typed_scalars,
    parse_scalar_row,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lme_ground_truth as gt  # noqa: E402
from measure_absence_band import EPISODES_Q, build_episodes  # noqa: E402

BASELINE_PROMPT_PREFIX = "You convert"
THRESHOLD, RECONCILE, ALIGN = 2.0 / 3.0, True, True


def _numbers(value: object) -> list[float]:
    return [float(m) for m in re.findall(r"-?\d+(?:\.\d+)?", str(value).replace(",", ""))]


def _carries(p, need: float) -> bool:
    return any(abs(n - need) < 1e-9 for n in _numbers(p.value))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--uri", default="bolt://127.0.0.1:7701")
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
    driver = GraphDatabase.driver(args.uri, auth=("neo4j", args.password))
    episodes_by_ns = {}
    with driver.session() as session:
        for suffix in suffixes:
            found = session.run(
                "MATCH (e:Episodic) WHERE e.group_id ENDS WITH $s "
                "RETURN DISTINCT e.group_id AS ns", {"s": suffix}).data()
            episodes_by_ns[suffix] = build_episodes(
                session.run(EPISODES_Q, {"ns": found[0]["ns"]}).data())
    driver.close()

    shown = 0
    for (suffix, trial), texts in sorted(completions.items()):
        episodes = episodes_by_ns[suffix]
        samples = []
        for text in texts:
            raw_rows, failure = _parse_json_array(text)
            samples.append([] if failure else [
                p for p in (parse_scalar_row(r, episodes, lambda _r: None) for r in raw_rows)
                if p is not None
            ])

        need = gt.required_values(suffix)[-1]
        n_carrying_samples = sum(1 for s in samples if any(_carries(p, need) for p in s))
        if n_carrying_samples < 2:
            continue  # extraction loss, not a gate loss

        decisions = gate_typed_scalars(
            samples, threshold=THRESHOLD, reconcile_attribute=RECONCILE, align_spans=ALIGN)
        if any(d.committed and d.proposal is not None and _carries(d.proposal, need)
               for d in decisions):
            continue

        groups = _claim_groups(samples, align_spans=ALIGN)
        carrying = [(gi, g) for gi, g in enumerate(groups)
                    if any(_carries(p, need) for _si, p in g)]
        if not carrying:
            print(f"\n=== {suffix} t{trial:02d}   need={need}   VALUE FORMED NO CLAIM GROUP")
            shown += 1
            continue
        gi, group = max(carrying,
                        key=lambda t: sum(1 for _si, p in t[1] if _carries(p, need)))
        d = decisions[gi]
        shown += 1
        print(f"\n=== {suffix} t{trial:02d}   need={need}   "
              f"{d.veto} modal {d.agreement:.2f}  ({n_carrying_samples}/3 samples had the value)")
        for si, p in sorted(group, key=lambda t: t[0]):
            mark = "*" if _carries(p, need) else " "
            print(f"  {mark} s{si}  subject={p.subject_text!r:<28} attr={p.attribute!r:<26} "
                  f"scope={p.scope!r:<16} op={p.operation:<9} value={p.value!r:<8} "
                  f"unit={p.unit!r} when={p.when!r}")
            print(f"        span[{p.span_start}:{p.span_end}] {p.stated_span!r}")

    print(f"\n{shown} residual gate losses at threshold=2/3, reconcile_attribute, align_spans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
