#!/usr/bin/env python3
"""Score the D3 span-alignment design against frozen samples (MEASURE ONLY -- nothing persisted).

THE DESIGN UNDER TEST (see .agent/plans/menhir-scalar-investigation-register.md):

`source_key` serves two contracts that conflict -- TOLERANT alignment of stochastic re-readings, and
EXACT durable identity. Every previous attempt tried to engineer a better span and failed. This
separates them: an alignment pass groups proposals into a claim head BEFORE the gate, and the exact
span is retained as evidence.

Grouping rule -- a group is valid only when ALL hold:
  * same episode
  * every member shares a COMMON span intersection (not merely pairwise/chained overlap -- chaining
    would let A-B and B-C drag non-overlapping A and C into one claim)
  * at most ONE proposal per sample
Anything else ABSTAINS and falls through to today's per-span behaviour, so the worst case is the
status quo.

Value is deliberately NOT part of the grouping key. It stays in the interpretation label, so two
overlapping spans with DIFFERENT values land in one head, produce different labels, scatter the vote
and abstain -- rather than one being silently chosen. 14 of 77 overlaps in the measured corpus are
exactly this case, so the distinction is load-bearing, not theoretical.

GATES (from the register): buckets short of k must fall, claims reaching all k must rise, and
cross-claim collisions must be ZERO.

    python scripts/eval_span_alignment.py --frozen frozen.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict

from menhir.services.typed_scalar_perception import (
    TypedScalarProposal,
    _interpretation_label,
)


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def common_intersection(spans: list[tuple[int, int]]) -> bool:
    """True when EVERY span shares at least one character position. Stricter than pairwise overlap
    and immune to transitive chaining."""
    return max(s for s, _ in spans) < min(e for _, e in spans)


def components(nodes: list[int], edges: dict[int, set[int]]) -> list[list[int]]:
    """Connected components over the overlap graph (union-find would do; this is small and clearer)."""
    seen: set[int] = set()
    out: list[list[int]] = []
    for n in nodes:
        if n in seen:
            continue
        stack, comp = [n], []
        seen.add(n)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nb in edges.get(cur, ()):
                if nb not in seen:
                    seen.add(nb)
                    stack.append(nb)
        out.append(sorted(comp))
    return out


def gate(labels_by_sample: list[str | None], k: int) -> tuple[bool, str]:
    """Mirror of the production gate at threshold=1.0: a sample with no reading is ABSENT, a sample
    with >=2 distinct readings is CONFLICTED, and neither can win but both stay in the denominator.
    Commit iff one real label holds all k votes and no other real label ties it."""
    votes = [lbl if lbl is not None else "\x00absent" for lbl in labels_by_sample]
    dist = Counter(votes)
    real = [(l, c) for l, c in dist.most_common() if not l.startswith("\x00")]
    if not real:
        return False, "no_real_vote"
    top_label, top_n = real[0]
    if top_n < k:
        return False, f"scattered_{top_n}of{k}"
    if len(real) >= 2 and real[1][1] == top_n:
        return False, "tie"
    return True, "commit"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frozen", required=True)
    ap.add_argument("--show", type=int, default=6, help="example merged heads to print")
    args = ap.parse_args()

    data = load(args.frozen)
    k = int(data["settings"]["k"])
    if data["settings"].get("truncated_completions"):
        print("REFUSING: frozen set contains truncated completions and is contaminated.",
              file=sys.stderr)
        return 2

    base_buckets = base_commits = 0
    algn_buckets = algn_commits = 0
    abstained_groups = 0
    collisions = 0
    conflict_heads = 0
    conflict_heads_committed = 0
    head_fail: Counter[str] = Counter()
    rescued: list[tuple[str, str]] = []

    for ns, payload in data["namespaces"].items():
        samples = [[TypedScalarProposal(**p) for p in s] for s in payload["samples"]]

        # ---- BASELINE: bucket by exact source_key, exactly as production does today -------------
        by_key: dict[str, list[tuple[int, TypedScalarProposal]]] = defaultdict(list)
        for si, sample in enumerate(samples):
            for p in sample:
                by_key[p.source_key].append((si, p))
        for key, members in by_key.items():
            base_buckets += 1
            per_sample: list[str | None] = [None] * k
            conflicted = [False] * k
            for si, p in members:
                lbl = _interpretation_label(p)
                if per_sample[si] is not None and per_sample[si] != lbl:
                    conflicted[si] = True
                per_sample[si] = lbl
            votes = ["\x00conflicted" if conflicted[i] else per_sample[i] for i in range(k)]
            ok, _ = gate(votes, k)
            base_commits += int(ok)

        # ---- ALIGNED: group by common span intersection across DIFFERENT samples ---------------
        flat: list[tuple[int, TypedScalarProposal]] = [
            (si, p) for si, sample in enumerate(samples) for p in sample]
        by_ep: dict[str, list[int]] = defaultdict(list)
        for idx, (_si, p) in enumerate(flat):
            by_ep[p.episode_uuid].append(idx)

        grouped_idx: set[int] = set()
        heads: list[list[int]] = []
        for _ep, idxs in by_ep.items():
            edges: dict[int, set[int]] = defaultdict(set)
            for a in idxs:
                sa, pa = flat[a]
                for b in idxs:
                    if b <= a:
                        continue
                    sb, pb = flat[b]
                    if sa == sb:
                        continue  # never group two readings from the SAME sample
                    if pa.span_start < pb.span_end and pb.span_start < pa.span_end:
                        edges[a].add(b)
                        edges[b].add(a)
            for comp in components(idxs, edges):
                if len(comp) == 1:
                    continue
                spans = [(flat[i][1].span_start, flat[i][1].span_end) for i in comp]
                sample_ids = [flat[i][0] for i in comp]
                if not common_intersection(spans) or len(set(sample_ids)) != len(sample_ids):
                    abstained_groups += 1   # ambiguous -> fall through to per-span behaviour
                    continue
                heads.append(comp)
                grouped_idx.update(comp)

        # aligned buckets = merged heads + every proposal that stayed on its own source_key
        leftover: dict[str, list[tuple[int, TypedScalarProposal]]] = defaultdict(list)
        for idx, (si, p) in enumerate(flat):
            if idx not in grouped_idx:
                leftover[p.source_key].append((si, p))

        for comp in heads:
            algn_buckets += 1
            sample_ids = [flat[i][0] for i in comp]
            if len(set(sample_ids)) != len(sample_ids):
                collisions += 1  # must be impossible by construction; counted as a self-check
            per_sample = [None] * k
            for i in comp:
                si, p = flat[i]
                per_sample[si] = _interpretation_label(p)
            vals = {flat[i][1].normalized_value for i in comp}
            is_conflict = len(vals) > 1
            conflict_heads += int(is_conflict)
            ok, _why = gate(per_sample, k)
            algn_commits += int(ok)
            if not ok:
                # A head that merged correctly but STILL fails tells us what the NEXT axis is.
                # Decompose by which label field the surviving samples disagree on -- the fields are
                # \x1f-joined in a fixed order, so a positional diff names the axis exactly.
                present = [l for l in per_sample if l]
                if len(present) < k:
                    head_fail["short_of_k_after_merge"] += 1
                else:
                    fields = [l.split("\x1f") for l in present]
                    names = ("subject", "attribute", "scope", "value_kind",
                             "unit", "operation", "value", "when")
                    diffs = [names[i] for i in range(len(names))
                             if len({f[i] for f in fields}) > 1]
                    head_fail["+".join(diffs) if diffs else "unknown"] += 1
            if ok and is_conflict:
                conflict_heads_committed += 1
            if ok:
                p0 = flat[comp[0]][1]
                if p0.source_key not in by_key or len(by_key[p0.source_key]) < k:
                    rescued.append((ns, f"{p0.attribute}={p0.normalized_value} "
                                        f"[{len(comp)}/{k} spans merged]"))

        for key, members in leftover.items():
            algn_buckets += 1
            per_sample = [None] * k
            conflicted = [False] * k
            for si, p in members:
                lbl = _interpretation_label(p)
                if per_sample[si] is not None and per_sample[si] != lbl:
                    conflicted[si] = True
                per_sample[si] = lbl
            votes = ["\x00conflicted" if conflicted[i] else per_sample[i] for i in range(k)]
            ok, _ = gate(votes, k)
            algn_commits += int(ok)

    print(f"frozen settings: {data['settings']}")
    print(f"namespaces: {len(data['namespaces'])}\n")
    print(f"{'':<26}{'baseline':>10}{'aligned':>10}{'delta':>10}")
    print(f"{'buckets':<26}{base_buckets:>10}{algn_buckets:>10}{algn_buckets - base_buckets:>+10}")
    print(f"{'COMMITS (reach all k)':<26}{base_commits:>10}{algn_commits:>10}"
          f"{algn_commits - base_commits:>+10}")
    print()
    print(f"groups abstained (ambiguous)      : {abstained_groups}")
    print(f"heads with CONFLICTING values     : {conflict_heads}")
    print(f"  ...of which COMMITTED (must be 0): {conflict_heads_committed}")
    print(f"COLLISIONS (must be 0)            : {collisions}")
    if head_fail:
        print("\nMERGED HEADS THAT STILL FAIL -- which axis blocks them:")
        for axis, n in head_fail.most_common():
            print(f"   {axis:<38} {n:>4}")
    if rescued:
        print(f"\nRESCUED claims (sample of {min(args.show, len(rescued))} of {len(rescued)}):")
        for ns, desc in rescued[:args.show]:
            print(f"   {ns}  {desc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
