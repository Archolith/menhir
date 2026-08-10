#!/usr/bin/env python3
"""Phase 1 of the proposer/reviewer vocabulary experiment -- live panel across arms A-D.

Plan: `.agent/plans/menhir-proposer-reviewer-vocabulary-experiment-plan.md` (+ Addendum A2/A3).
MEASURE ONLY: nothing is persisted to the graph.

ARMS (all at production settings, all on a THREE-call budget so cost is comparable):
  A  baseline      -- extract x3, then the exact-`source_key` gate
  B  vocabulary    -- proposer fixes labels; 2 reviewers; exact-span gate unchanged
  C  claim cards   -- proposer emits cards; 2 reviewers verify by claim_id (spans may differ)
  D  B + alignment -- Arm B output, then DETERMINISTIC span alignment (0 extra LLM calls)

Arm D exists because span fragmentation already yields to arithmetic with zero collisions. Arm C
must beat ARM D, not Arm A, to justify claim-card machinery (plan addendum A2).

DECOYS: `--decoy-rate` corrupts a fraction of proposer cards before review (wrong value by digit
transposition, or wrong attribute). Reviewers must reject these. Because samples already agree on
value ~97% of the time when reading the same span, decoy rejection -- NOT agreement rate -- is the
only real evidence reviewers are reading the source (addendum A3). Decoy and real agreement are
reported separately; a pooled number cannot distinguish comprehension from compliance.

Every request, raw completion, parse drop and decision is written to JSONL so the gate can be
changed and replayed without spending another LLM call.

    python scripts/probe_scalar_proposer_reviewer.py --ns <id> [...] --arms A B C D \\
        --trials 20 --decoy-rate 0.25 --jsonl run.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from collections import Counter, defaultdict

from dotenv import load_dotenv
from neo4j import GraphDatabase
from openai import OpenAI

from menhir.services.typed_scalar_perception import (
    _interpretation_label,
    extract_typed_scalars_once,
    gate_typed_scalars,
)
from menhir.services.typed_scalar_proposer_reviewer import (
    SUPPORTED,
    ClaimCard,
    gate_reviewed_cards,
    propose_claim_cards,
    review_claim_cards,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lme_ground_truth as gt  # noqa: E402
from measure_absence_band import EPISODES_Q, build_episodes  # noqa: E402


# ---------------------------------------------------------------- deterministic span alignment
def align(proposals_by_sample: list[list]) -> list[list[tuple[int, object]]]:
    """Group proposals sharing a COMMON span intersection, same episode, <=1 per sample.

    Identical rule to scripts/eval_span_alignment.py: common intersection rather than chained
    pairwise overlap (chaining drags non-overlapping A and C together through B), and ambiguous
    groups abstain so the worst case is the status quo."""
    flat = [(si, p) for si, s in enumerate(proposals_by_sample) for p in s]
    by_ep: dict[str, list[int]] = defaultdict(list)
    for i, (_si, p) in enumerate(flat):
        by_ep[p.episode_uuid].append(i)

    heads: list[list[tuple[int, object]]] = []
    grouped: set[int] = set()
    for _ep, idxs in by_ep.items():
        edges: dict[int, set[int]] = defaultdict(set)
        for a in idxs:
            sa, pa = flat[a]
            for b in idxs:
                if b <= a:
                    continue
                sb, pb = flat[b]
                if sa == sb:
                    continue
                if pa.span_start < pb.span_end and pb.span_start < pa.span_end:
                    edges[a].add(b)
                    edges[b].add(a)
        seen: set[int] = set()
        for n in idxs:
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
            if len(comp) < 2:
                continue
            spans = [(flat[i][1].span_start, flat[i][1].span_end) for i in comp]
            sids = [flat[i][0] for i in comp]
            if max(s for s, _ in spans) >= min(e for _, e in spans):
                continue
            if len(set(sids)) != len(sids):
                continue
            heads.append([(flat[i][0], flat[i][1]) for i in comp])
            grouped.update(comp)
    for i, (si, p) in enumerate(flat):
        if i not in grouped:
            heads.append([(si, p)])
    return heads


def commits_from_heads(heads, k: int) -> int:
    n = 0
    for head in heads:
        labels = {si: _interpretation_label(p) for si, p in head}
        if len(labels) == k and len(set(labels.values())) == 1:
            n += 1
    return n


def make_decoy(card: ClaimCard, rng: random.Random) -> tuple[ClaimCard, str]:
    """Corrupt a card's FACT, plausibly.

    MUST corrupt the fact, never the label. An earlier version renamed the attribute
    (`team_size` -> `team_size_total`) and counted acceptance as acquiescence -- but the reviewer
    prompt instructs reviewers to keep the offered label, and choosing the filing vocabulary is
    exactly what the proposer is DESIGNED to do. Nothing about the fact was wrong, so accepting it
    was correct behaviour. That invalid control showed 59% "acceptance" while the valid one showed
    0/29, and reading the pooled number would have failed the experiment on its own instrument.

    An implausible decoy is rejected by prior rather than by reading (plan addendum A3), so these
    stay in-range: digit transposition where possible, otherwise off-by-one -- the error a reader
    only catches by actually looking at the source.
    """
    from dataclasses import replace
    p = card.proposal
    digits = str(p.value)
    if digits.isdigit() and len(digits) >= 2 and digits[0] != digits[1]:
        bad = int(digits[1] + digits[0] + digits[2:])
        return ClaimCard(card.claim_id, replace(p, value=bad)), "value_transposed"
    try:
        n = float(digits)
        bad_n = int(n) + 1 if float(int(n)) == n else round(n + 1, 4)
        return ClaimCard(card.claim_id, replace(p, value=bad_n)), "value_off_by_one"
    except (TypeError, ValueError):
        # non-numeric kinds (status/weekday/boolean): flip the subject, still a FACT-level error
        return ClaimCard(card.claim_id, replace(p, subject_text="my brother")), "subject_wrong"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ns", nargs="+", required=True)
    ap.add_argument("--arms", nargs="+", default=["A", "B", "C", "D"])
    ap.add_argument("--trials", type=int, default=1, help="repeats per namespace per arm")
    ap.add_argument("--decoy-rate", type=float, default=0.25)
    ap.add_argument("--jsonl", default="")
    ap.add_argument("-k", type=int, default=3)
    ap.add_argument("--temp", type=float, default=0.7)
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--seed", type=int, default=17)
    ap.add_argument("--uri", default="bolt://127.0.0.1:7701")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="lmedata123")
    args = ap.parse_args()

    load_dotenv()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set", file=sys.stderr)
        return 2
    model = os.environ.get("MENHIR_PERSONAL_MEMORY_CHAT_MODEL") or "gpt-4o-mini"
    client = OpenAI(api_key=api_key)
    driver = GraphDatabase.driver(args.uri, auth=(args.user, args.password))
    rng = random.Random(args.seed)
    sink = open(args.jsonl, "w", encoding="utf-8") if args.jsonl else None

    stats: dict[str, Counter] = defaultdict(Counter)
    truncated = 0
    calls = 0
    ctx: dict[str, object] = {}

    def llm(system: str, user: str) -> str:
        nonlocal truncated, calls
        calls += 1
        resp = client.chat.completions.create(
            model=model, temperature=args.temp, max_tokens=args.max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        text = resp.choices[0].message.content or ""
        if resp.choices[0].finish_reason == "length":
            truncated += 1
        if sink:
            sink.write(json.dumps({"type": "call", **ctx, "system": system[:120],
                                   "user_chars": len(user), "completion": text,
                                   "finish": resp.choices[0].finish_reason}) + "\n")
        return text

    with driver.session() as session:
        for suffix in args.ns:
            row = session.run("MATCH (e:Episodic) WHERE e.group_id ENDS WITH $s "
                              "RETURN DISTINCT e.group_id AS ns", {"s": suffix}).data()
            if not row:
                print(f"  !! no namespace {suffix}", file=sys.stderr)
                continue
            episodes = build_episodes(session.run(EPISODES_Q, {"ns": row[0]["ns"]}).data())
            if not episodes:
                continue

            # B and D are the SAME three calls scored two ways (exact-span gate vs deterministic
            # alignment), so the trio is run ONCE under the label "BD" and both arms are scored from
            # it. Two benefits, the second mattering more than the cost:
            #   * 25% fewer calls per namespace-trial (12 -> 9);
            #   * the B-vs-D contrast becomes PAIRED -- identical model output, only the grouping
            #     differs -- which removes between-run sampling noise from the one comparison the
            #     experiment exists to make. Run separately, B and D differ by both the grouping AND
            #     two independent draws at temp=0.7, and the measured draw-to-draw swing (16 -> 12)
            #     is the same size as the effect.
            arm_plan = []
            for a in args.arms:
                if a in ("B", "D"):
                    if "BD" not in arm_plan:
                        arm_plan.append("BD")
                else:
                    arm_plan.append(a)

            for trial in range(args.trials):
                for arm in arm_plan:
                    ctx.clear()
                    ctx.update({"ns": suffix, "trial": trial, "arm": arm})

                    if arm == "A":
                        samples = [extract_typed_scalars_once(episodes, llm)
                                   for _ in range(args.k)]
                        decisions = gate_typed_scalars(samples, threshold=1.0)
                        stats["A"]["commits"] += sum(1 for d in decisions if d.committed)
                        stats["A"]["claims"] += len(decisions)
                        for d in decisions:
                            if d.committed and d.proposal is not None:
                                stats["A"][gt.classify(suffix, d.proposal.value)] += 1
                                # The BASELINE must be captured too. Omitting it left gates 2 and 3
                                # -- both of which compare against A -- uncomputable from a capture.
                                if sink:
                                    sink.write(json.dumps({
                                        "type": "arm_commit", **ctx,
                                        "value": str(d.proposal.value),
                                        "attribute": d.proposal.attribute,
                                        "verdict": gt.classify(suffix, d.proposal.value)}) + "\n")
                        continue

                    # Arms B/C/D all begin with one proposer call.
                    def _pdrop(reason: str, _arm=arm) -> None:
                        for _ra in (["B", "D"] if _arm == "BD" else [_arm]):
                            if _ra in args.arms:
                                stats[_ra][f"drop_{reason}"] += 1

                    cards = propose_claim_cards(episodes, llm, on_drop=_pdrop)
                    if not cards:
                        stats[arm]["no_cards"] += 1
                        continue

                    # Decoys are injected AFTER proposal and BEFORE review.
                    shown, decoy_ids = [], {}
                    for c in cards:
                        if rng.random() < args.decoy_rate:
                            bad, how = make_decoy(c, rng)
                            shown.append(bad)
                            decoy_ids[c.claim_id] = how
                        else:
                            shown.append(c)

                    orders = [list(range(len(shown))) for _ in range(2)]
                    rng.shuffle(orders[1])
                    reviews = [review_claim_cards(episodes, shown, llm, order=o, on_drop=_pdrop)
                               for o in orders]
                    decisions = gate_reviewed_cards(shown, reviews)

                    # When the trio is shared, its review-protocol statistics (decoy rejection,
                    # acquiescence, verdict reasons) belong to BOTH arms: B and D use the SAME
                    # reviewers, so the same evidence supports the same claim about each. Recorded
                    # under both labels rather than under "BD", which neither the report nor the
                    # gate scorer reads. This is not double counting -- it is one body of evidence
                    # about one shared protocol, reported against both arms that rely on it.
                    real_arms = ["B", "D"] if arm == "BD" else [arm]
                    real_arms = [a for a in real_arms if a in args.arms]

                    for d in decisions:
                        is_decoy = d.claim_id in decoy_ids
                        bucket = "decoy" if is_decoy else "real"
                        for ra in real_arms:
                            stats[ra][f"{bucket}_total"] += 1
                            if d.committed:
                                stats[ra][f"{bucket}_committed"] += 1
                            # per-reviewer acquiescence: SUPPORTED on a corrupted card
                            for v in d.verdicts:
                                if v == SUPPORTED:
                                    stats[ra][f"{bucket}_supported_votes"] += 1
                            stats[ra][f"reason_{d.reason}"] += 1
                        if sink:
                          for ra in real_arms:
                            sink.write(json.dumps({
                                "type": "decision", **{**ctx, "arm": ra},
                                "claim_id": d.claim_id,
                                "decoy": decoy_ids.get(d.claim_id), "committed": d.committed,
                                "reason": d.reason, "verdicts": d.verdicts,
                                # Values are REQUIRED for replay: without them the capture can only
                                # answer "did it commit", not "was it right", and truth has to be
                                # re-bought with another live run.
                                "value": None if d.proposal is None else str(d.proposal.value),
                                "attribute": None if d.proposal is None else d.proposal.attribute,
                                }) + "\n")

                    if arm == "BD":
                        # B/D score by the FACT the reviewers independently emitted, so the
                        # exact-span gate (B) or the alignment pass (D) sees real proposals.
                        per_sample = [[r.proposal for r in rv if r.proposal] for rv in reviews]
                        per_sample.insert(0, [c.proposal for c in shown])
                        # NOTE: B/D commits come from THIS computation, not from `decisions` above.
                        # They must be captured here too -- capturing only the card decisions leaves
                        # the arms' actual scored population absent from the replay file.
                        def _emit(val, attr, real_arm) -> None:
                            # MUST re-stamp the arm. `ctx` carries the internal scheduling label
                            # "BD" for the shared trio, and every downstream consumer -- the report
                            # and all four gates -- keys on the arm name. A row left labelled "BD"
                            # is not wrong-looking, it is INVISIBLE, and only discoverable after the
                            # calls are spent.
                            if sink:
                                row = dict(ctx)
                                row["arm"] = real_arm
                                sink.write(json.dumps({
                                    "type": "arm_commit", **row, "value": str(val),
                                    "attribute": attr,
                                    "verdict": gt.classify(suffix, val)}) + "\n")

                        if "B" in args.arms:
                            for d in gate_typed_scalars(per_sample, threshold=1.0):
                                if d.committed and d.proposal is not None:
                                    stats["B"]["commits"] += 1
                                    stats["B"][gt.classify(suffix, d.proposal.value)] += 1
                                    _emit(d.proposal.value, d.proposal.attribute, "B")
                        if "D" in args.arms:
                            for head in align(per_sample):
                                labels = {si: _interpretation_label(pp) for si, pp in head}
                                if len(labels) == len(per_sample) and len(set(labels.values())) == 1:
                                    stats["D"]["commits"] += 1
                                    stats["D"][gt.classify(suffix, head[0][1].value)] += 1
                                    _emit(head[0][1].value, head[0][1].attribute, "D")
                    else:
                        stats[arm]["commits"] += sum(1 for d in decisions if d.committed)
                        for d in decisions:
                            if d.committed and d.proposal is not None:
                                stats[arm][gt.classify(suffix, d.proposal.value)] += 1
                                if sink:
                                    sink.write(json.dumps({
                                        "type": "arm_commit", **ctx,
                                        "value": str(d.proposal.value),
                                        "attribute": d.proposal.attribute,
                                        "verdict": gt.classify(suffix, d.proposal.value)}) + "\n")
            print(f"  done {suffix}", flush=True)

    if sink:
        sink.close()
    print(f"\nmodel {model}  k={args.k}  temp={args.temp}  max_tokens={args.max_tokens}")
    print(f"llm calls {calls}   truncated {truncated}")
    if truncated:
        print("WARNING: truncated completions -- results contaminated, raise --max-tokens")
    for arm in args.arms:
        s = stats[arm]
        print(f"\n=== ARM {arm} ===")
        print(f"  commits: {s['commits']}")
        cur, st, wr = s[gt.CURRENT], s[gt.STALE], s[gt.WRONG]
        scored = cur + st + wr
        if scored:
            print(f"  vs GROUND TRUTH: current={cur}  STALE={st}  wrong={wr}   "
                  f"precision={100 * cur / scored:.1f}%")
        if s.get("real_total"):
            rt, rc = s["real_total"], s["real_committed"]
            print(f"  real cards      : {rc}/{rt} committed ({100 * rc / rt:.1f}%)")
        if s.get("decoy_total"):
            dt, dc = s["decoy_total"], s["decoy_committed"]
            votes = s.get("decoy_supported_votes", 0)
            print(f"  DECOY cards     : {dc}/{dt} committed ({100 * dc / dt:.1f}%)  "
                  f"<-- must be ~0")
            print(f"  decoy SUPPORTED reviewer votes: {votes} of {dt * 2} "
                  f"({100 * votes / (dt * 2):.1f}% acquiescence)")
        drops = {k[len('drop_'):]: v for k, v in s.items() if k.startswith("drop_")}
        if drops:
            print("  PARSE DROPS: " + ", ".join(f"{k}={v}" for k, v in
                                                sorted(drops.items(), key=lambda x: -x[1])))
        reasons = {k[len('reason_'):]: v for k, v in s.items() if k.startswith("reason_")}
        if reasons:
            print("  reasons: " + ", ".join(f"{k}={v}" for k, v in
                                            sorted(reasons.items(), key=lambda x: -x[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
