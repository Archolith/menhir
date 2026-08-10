#!/usr/bin/env python3
"""Score a Phase 1 capture against the experiment's PRE-REGISTERED go/no-go gates.

Plan: `.agent/plans/menhir-proposer-reviewer-vocabulary-experiment-plan.md`, "Metrics and
Acceptance Gates", as revised by Addendum A2 (gate 6 is scored against Arm D, not Arm A).

Reads the probe's JSONL only -- no LLM calls, no database. The point of pre-registering gates is
that the verdict is computed, not argued, so this deliberately prints PASS/FAIL per gate rather
than a narrative.

    python scripts/score_proposer_reviewer_gates.py --jsonl run.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from math import comb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lme_ground_truth as gt  # noqa: E402


def _hypergeom(k: int, K: int, n: int, N: int) -> float:
    """P(X = k) for X ~ Hypergeometric(N, K, n)."""
    if k < 0 or k > K or (n - k) > (N - K):
        return 0.0
    return comb(K, k) * comb(N - K, n - k) / comb(N, n)


def fisher(a: int, b: int, c: int, d: int, *, tail: str) -> float:
    """One-tailed Fisher exact on [[a, b], [c, d]] = [[hit, miss], [hit, miss]].

    `tail='greater'` tests whether row 1 has a HIGHER hit rate (improvement); `tail='less'` tests
    whether it has a LOWER one (regression). The plan requires BOTH directions to be run with the
    hypothesis direction named -- running the improvement tail on a regression question returns a
    meaningless p near 1.0, which this investigation has already done once."""
    N, K, n = a + b + c + d, a + c, a + b
    lo, hi = max(0, n - (N - K)), min(K, n)
    if tail == "greater":
        return sum(_hypergeom(k, K, n, N) for k in range(a, hi + 1))
    return sum(_hypergeom(k, K, n, N) for k in range(lo, a + 1))


def wilson(hits: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval -- behaves sanely at 0/n and n/n, unlike the normal approximation."""
    if total == 0:
        return (0.0, 0.0)
    p = hits / total
    d = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / d
    half = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def load(path: str) -> tuple[dict, dict, dict]:
    """-> (arm_commits, decoys, reasons)."""
    commits: dict[str, list[dict]] = defaultdict(list)
    decoys: dict[str, Counter] = defaultdict(Counter)
    reasons: dict[str, Counter] = defaultdict(Counter)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            t = r.get("type")
            if t == "arm_commit":
                commits[r["arm"]].append(r)
            elif t == "decision":
                reasons[r["arm"]][r["reason"]] += 1
                if r.get("decoy"):
                    decoys[r["arm"]]["total"] += 1
                    decoys[r["arm"]][r["decoy"] + "_total"] += 1
                    if r["committed"]:
                        decoys[r["arm"]]["committed"] += 1
                        decoys[r["arm"]][r["decoy"] + "_committed"] += 1
                elif r["committed"]:
                    # arms whose scored population IS the card decisions (Arm C)
                    commits.setdefault(r["arm"] + "_cards", []).append(r)
    return commits, decoys, reasons


def verdicts(rows: list[dict]) -> Counter:
    c: Counter = Counter()
    for r in rows:
        v = r.get("verdict") or gt.classify(r["ns"], r.get("value"))
        c[v] += 1
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    args = ap.parse_args()
    commits, decoys, reasons = load(args.jsonl)

    arms = ["A", "B", "C", "D"]
    v = {}
    for arm in arms:
        rows = commits.get(arm) or commits.get(arm + "_cards") or []
        v[arm] = verdicts(rows)

    # PRIMARY METRIC, per the pre-registration: per namespace-trial, did the arm record the ASKED
    # fact? Rate-over-commits is NOT precision on this corpus -- LME labels one fact per namespace
    # while a namespace states many, so an arm that records MORE true facts is punished by it. That
    # metric was retracted before this run; scoring G2/G3 on it contradicted the pre-registration.
    ns_v: dict[str, Counter] = defaultdict(Counter)
    trials: dict[str, set] = defaultdict(set)
    for arm in arms:
        vals: dict[tuple, list] = defaultdict(list)
        for r in (commits.get(arm) or commits.get(arm + "_cards") or []):
            vals[(r["ns"], r["trial"])].append(r.get("value"))
            trials[arm].add((r["ns"], r["trial"]))
        for key, vs in vals.items():
            ns_v[arm][gt.namespace_verdict(key[0], vs)] += 1
    n_cells = max((len(t) for t in trials.values()), default=0)
    print("PRIMARY (pre-registered): per namespace-trial, did the arm record the ASKED fact?")
    print(f"{'arm':<5}{'ANSWERED':>10}{'stale-only':>12}{'unmatched':>11}{'produced':>10}")
    for arm in arms:
        c = ns_v[arm]
        print(f"{arm:<5}{c[gt.CURRENT]:>10}{c[gt.STALE]:>12}{c[gt.UNMATCHED]:>11}"
              f"{sum(c.values()):>10}")
    print(f"(of {n_cells} namespace-trial cells; arms differ because an arm can produce nothing)\n")
    print("SECONDARY -- per-commit buckets. `unmatched` mixes wrong values with CORRECT facts the")
    print("benchmark does not label, so this is NOT a precision figure. Do not rank arms on it.")
    print(f"{'arm':<5}{'CURRENT':>9}{'stale':>7}{'unmatched':>11}{'commits':>9}  95% CI on current-rate")
    for arm in arms:
        c = v[arm]
        tot = sum(c.values())
        lo, hi = wilson(c[gt.CURRENT], tot) if tot else (0, 0)
        print(f"{arm:<5}{c[gt.CURRENT]:>9}{c[gt.STALE]:>7}{c[gt.UNMATCHED]:>11}{tot:>9}"
              f"   [{lo:.2f}, {hi:.2f}]")

    print("\n" + "=" * 78)
    print("PRE-REGISTERED GATES")
    print("=" * 78)
    results: list[tuple[str, bool, str]] = []

    # gate 4 / 8 -- controls
    for arm in ("B", "C", "D"):
        d = decoys[arm]
        if not d["total"]:
            continue
        rej = 1 - d["committed"] / d["total"]
        results.append((f"G4/G8 arm {arm}: decoy rejection >= 95%", rej >= 0.95,
                        f"{d['committed']}/{d['total']} committed -> {100 * rej:.1f}% rejected"))

    # Gates 2/3/6 are scored on the PRIMARY metric with the panel as denominator. A namespace-trial
    # that produced nothing is a MISS, not an exclusion -- scoring only cells that produced output
    # would reward an arm for staying silent.
    def answered(arm: str) -> int:
        return ns_v[arm][gt.CURRENT]

    def stale_only(arm: str) -> int:
        return ns_v[arm][gt.STALE]

    # gate 2 -- improvement tail, per namespace-trial
    for arm in ("B", "C", "D"):
        p = fisher(answered(arm), n_cells - answered(arm),
                   answered("A"), n_cells - answered("A"), tail="greater")
        results.append((f"G2 arm {arm} > A: records the asked fact (improvement tail)", p < 0.05,
                        f"{answered(arm)}/{n_cells} vs {answered('A')}/{n_cells}  p={p:.4f}"))

    # gate 3 -- regression tail, run SEPARATELY on stale (the defect under investigation)
    for arm in ("B", "C", "D"):
        p = fisher(stale_only(arm), n_cells - stale_only(arm),
                   stale_only("A"), n_cells - stale_only("A"), tail="greater")
        results.append((f"G3 arm {arm} does NOT regress on stale-only", p >= 0.05,
                        f"stale-only {stale_only(arm)}/{n_cells} vs {stale_only('A')}/{n_cells}  "
                        f"p={p:.4f} (p<0.05 would be a real stale regression)"))

    # gate 6 (addendum A2) -- Arm C must beat Arm D, not Arm A
    p = fisher(answered("C"), n_cells - answered("C"),
               answered("D"), n_cells - answered("D"), tail="greater")
    results.append(("G6 (A2) arm C > arm D -- claim cards earn their complexity", p < 0.05,
                    f"C {answered('C')}/{n_cells} vs D {answered('D')}/{n_cells}  p={p:.4f}"))

    # addendum A2 companion: does alignment add anything over vocabulary alone? PAIRED comparison --
    # B and D share one trio, so this isolates the grouping rule from sampling noise entirely.
    p = fisher(answered("D"), n_cells - answered("D"),
               answered("B"), n_cells - answered("B"), tail="greater")
    results.append(("A2 arm D > arm B -- alignment adds to vocabulary (PAIRED)", p < 0.05,
                    f"D {answered('D')}/{n_cells} vs B {answered('B')}/{n_cells}  p={p:.4f}"))

    width = max(len(n) for n, _, _ in results)
    for name, ok, detail in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<{width}}  {detail}")

    print("\nREASON HISTOGRAM (surface-drift proxy = factual_core_disagreement)")
    for arm in ("B", "C", "D"):
        r = reasons[arm]
        tot = sum(r.values())
        if tot:
            print(f"  {arm}: " + ", ".join(f"{k}={n}" for k, n in r.most_common()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
