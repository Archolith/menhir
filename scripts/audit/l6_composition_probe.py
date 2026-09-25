"""L6 -- production caller composition probe (composition-boundary audit).

The composition audit's other five lanes have mechanical probes; L6 was hand-run, and its
own results file says so: *"L6 has no mechanical probe yet ... the highest-yield lane, but it
was run by hand here."* This is that probe.

**What L6 asks.** Not "is this helper correct?" but:

    Does any test enter through the production caller that CHOOSES this helper's arguments,
    or do the tests only construct ideal arguments by hand?

CF-230 is the canonical miss: `link_episode_admission` filtered correctly and had its own
green test; the production caller passed `namespace=None`, which means "do not filter". A
helper tested only with hand-built arguments cannot fail the way CF-230 failed.

**Scope is deliberately narrow.** Requiring an integration test per helper is not the claim.
The corpus is restricted to helpers that carry a consequence -- tenancy/ownership/namespace,
destructive mutation, provenance/admission/trust, scheduler/saga ownership, resource limits
and budgets, control-flow callbacks, ContextVar-derived policy, security normalization. A
formatting helper composed wrongly is a bug; a namespace helper composed wrongly is a tenant
boundary crossing.

**What this probe can and cannot decide.**

It is a recall instrument. Callee resolution is by NAME, so overloads across layers collapse
together and dynamic dispatch is invisible; test "coverage" is a name reference in a test
file, not an execution trace. It therefore over-reports, and it cannot prove a call is safe.
What it does is rank (helper, caller) pairs by how much of the L6 shape they carry, so the
hand-verification pass reads call sites in the order most likely to contain a defect.

Read the ranked pairs; decide by reading the call site. Every ARGUMENT_GAP row is a question,
not a finding: does a caller that HAS the value omit it, and is the param a filter (pinning is
right) or the target of a mutation (pinning is wrong)?

Usage:
    python scripts/audit/l6_composition_probe.py [--json OUT] [--category CAT] [--all]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import defaultdict

# Keep the sibling parts package importable even when this module is loaded by
# path (importlib spec) rather than imported as a script or package member.
_SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.append(str(_SCRIPT_DIR))

from l6_composition_probe_parts import (  # noqa: E402
    CATEGORIES, CONTROL_SIGNALS, LOSSY_BOUNDARIES, SKIP_DIRS, WEAKENING_PARAMS,
    ROOT, SRC, TESTS, CallSite, FuncDef, ModuleIndex, Pair,
    _annotation, _callee_name, _handler_names, _rel,
    analyse, classify, context_boundaries, index_source, index_tests, iter_py,
    propagate_control_signals,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=pathlib.Path, help="write full results as JSON")
    parser.add_argument("--category", action="append", help="restrict to these categories")
    parser.add_argument("--top", type=int, default=40, help="pairs to print (default 40)")
    parser.add_argument("--all", action="store_true", help="print every scored pair")
    args = parser.parse_args()

    defs, calls, module_src = index_source()
    propagate_control_signals(defs)
    for fd in defs:
        classify(fd)
    test_refs = index_tests()
    pairs = analyse(defs, calls, test_refs)

    if args.category:
        wanted = set(args.category)
        pairs = [p for p in pairs if p.helper.categories & wanted]

    corpus = [fd for fd in defs if fd.categories]
    print(f"L6 composition probe -- {len(defs)} defs, {len(calls)} call sites, "
          f"{len(corpus)} consequential helpers, {len(pairs)} scored pairs\n")

    counts: dict[str, int] = defaultdict(int)
    for fd in corpus:
        for category in fd.categories:
            counts[category] += 1
    print("corpus by category: " + ", ".join(
        f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))

    signal_counts: dict[str, int] = defaultdict(int)
    for pair in pairs:
        for signal in pair.signals:
            signal_counts[signal.split(":")[0]] += 1
    print("signals: " + ", ".join(
        f"{k}={v}" for k, v in sorted(signal_counts.items(), key=lambda kv: -kv[1])) + "\n")

    shown = pairs if args.all else pairs[:args.top]
    for pair in shown:
        cats = ",".join(sorted(pair.helper.categories))
        print(f"[{pair.score:>2}] {pair.helper.name}  ({cats})")
        print(f"     callee {pair.helper.loc}")
        print(f"     caller {pair.site.loc} in `{pair.site.enclosing}`")
        for signal in pair.signals:
            print(f"       - {signal}")
        print()

    boundaries = context_boundaries(module_src)
    print(f"context boundaries that do NOT copy the context: {len(boundaries)}")
    for hit in boundaries:
        print(f"  {hit}")

    if args.json:
        payload = {
            "summary": {
                "defs": len(defs), "calls": len(calls),
                "corpus": len(corpus), "pairs": len(pairs),
                "by_category": dict(counts), "signals": dict(signal_counts),
            },
            "pairs": [
                {
                    "score": p.score,
                    "helper": p.helper.name,
                    "helper_loc": p.helper.loc,
                    "categories": sorted(p.helper.categories),
                    "body_only_match": p.helper.body_only,
                    "caller_loc": p.site.loc,
                    "caller_func": p.site.enclosing,
                    "signals": p.signals,
                    "helper_tests": sorted(p.helper_tests),
                    "caller_tests": sorted(p.caller_tests),
                }
                for p in pairs
            ],
            "lossy_context_boundaries": boundaries,
        }
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
