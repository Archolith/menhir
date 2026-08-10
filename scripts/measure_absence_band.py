#!/usr/bin/env python3
"""Absence-band anatomy (MEASURE ONLY -- no production change, nothing persisted).

At threshold=1.0 a claim commits only when all k samples agree. The dominant failure is a claim that
reached fewer than k samples. That band is NOT one thing, and the whole D3 argument rests on the split:

    span_fragmentation_same_value  -- samples DID read it, but quoted different extents, so the
                                      span-derived `source_key` filed them as separate claims and
                                      each looks like a lone 1/k. A BUCKETING defect: repairable.
    span_overlap_different_value   -- overlapping spans, genuinely different values. REAL
                                      disagreement; must never be merged away.
    true_silence                   -- no other sample proposed anything overlapping. The model
                                      simply did not emit it.

WHY THIS EXISTS AS A SCRIPT: the first version of this measurement was run inline and could not be
re-run. Its numbers (48.4/3.3/48.4) were taken at `max_tokens=512`, where 3/19 namespaces lost whole
samples to truncation -- a truncated sample contributes silence to every claim in its namespace, so
the `true_silence` share was inflated by an unknown amount. That measurement must be re-derived at
the corrected budget (93889d1) before any D3 decision rests on it.

Read-only against the LME container. Requires OPENAI_API_KEY.

    python scripts/measure_absence_band.py --ns 031748ae c6853660 ... [--k 3] [--max-tokens 2048]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

from dotenv import load_dotenv
from neo4j import GraphDatabase
from openai import OpenAI

from menhir.services.perception import Episode
from menhir.services.typed_scalar_perception import extract_typed_scalars_once

EPISODES_Q = """
MATCH (e:Episodic)
WHERE e.content STARTS WITH 'user:' AND e.group_id = $ns
RETURN e.uuid AS uuid, toString(coalesce(e.valid_at, e.created_at)) AS valid_at,
       e.content AS content
ORDER BY coalesce(e.created_at, e.valid_at), e.uuid
"""


def build_episodes(rows: list[dict]) -> list[Episode]:
    """Mirror of scheduler_tasks._build_episodes -- same prefix strip, 2000-char body cap, minimum
    length, and (body, valid_at) exact-duplicate collapse (a661390). Kept in sync deliberately: if
    this diverges, the probe measures a corpus production never sees."""
    eps: list[Episode] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        raw = str(r.get("content") or "")
        body = (raw[len("user:"):] if raw[:5].lower() == "user:" else raw).strip()[:2000]
        if len(body) < 8:
            continue
        stamp = str(r.get("valid_at") or "")
        if (body, stamp) in seen:
            continue
        seen.add((body, stamp))
        eps.append(Episode(uuid=str(r["uuid"]), content=f"[{stamp[:10]}] {body}"))
    return eps


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Half-open interval intersection. Touching-but-not-overlapping ([0,5) vs [5,9)) is NOT an
    overlap -- adjacent spans are different quotes, not two readings of one clause."""
    return a_start < b_end and b_start < a_end


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ns", nargs="+", required=True, help="namespace suffixes (8-char ids)")
    ap.add_argument("-k", type=int, default=3)
    ap.add_argument("--temp", type=float, default=0.7)
    ap.add_argument("--max-tokens", type=int, default=2048)
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

    truncated = 0
    calls = 0

    def llm_complete(system: str, user: str) -> str:
        nonlocal truncated, calls
        calls += 1
        resp = client.chat.completions.create(
            model=model, temperature=args.temp, max_tokens=args.max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
        )
        if resp.choices[0].finish_reason == "length":
            truncated += 1
        return resp.choices[0].message.content or ""

    totals: Counter[str] = Counter()
    reached = 0
    short = 0

    with driver.session() as session:
        for suffix in args.ns:
            ns_rows = session.run(
                "MATCH (e:Episodic) WHERE e.group_id ENDS WITH $sfx "
                "RETURN DISTINCT e.group_id AS ns", {"sfx": suffix}).data()
            if not ns_rows:
                print(f"  !! no namespace ending {suffix}", file=sys.stderr)
                continue
            ns = ns_rows[0]["ns"]
            rows = session.run(EPISODES_Q, {"ns": ns}).data()
            episodes = build_episodes(rows)
            if not episodes:
                continue

            samples = [extract_typed_scalars_once(episodes, llm_complete) for _ in range(args.k)]

            # source_key -> which sample indices proposed it, plus one representative proposal.
            by_key: dict[str, set[int]] = {}
            rep: dict[str, object] = {}
            for i, sample in enumerate(samples):
                for p in sample:
                    by_key.setdefault(p.source_key, set()).add(i)
                    rep.setdefault(p.source_key, p)

            for key, sample_ids in by_key.items():
                if len(sample_ids) >= args.k:
                    reached += 1
                    continue
                short += 1
                p = rep[key]
                # Look for a proposal from a DIFFERENT sample, same episode, overlapping span.
                frag = False
                conflict = False
                for j, other in enumerate(samples):
                    if j in sample_ids:
                        continue
                    for q in other:
                        if q.episode_uuid != p.episode_uuid:
                            continue
                        if not overlaps(p.span_start, p.span_end, q.span_start, q.span_end):
                            continue
                        if q.normalized_value == p.normalized_value:
                            frag = True
                        else:
                            conflict = True
                if frag:
                    totals["span_fragmentation_same_value"] += 1
                elif conflict:
                    totals["span_overlap_different_value"] += 1
                else:
                    totals["true_silence"] += 1
            print(f"  scanned {suffix}")

    print()
    print(f"model {model}  k={args.k}  temp={args.temp}  max_tokens={args.max_tokens}")
    print(f"llm calls {calls}   TRUNCATED {truncated}"
          + ("   <-- band split is UNRELIABLE, raise --max-tokens" if truncated else ""))
    print()
    print(f"claims reaching all k samples : {reached}")
    print(f"claims SHORT of k (absence)   : {short}")
    if short:
        print()
        print("ANATOMY OF THAT BAND:")
        for label in ("span_fragmentation_same_value", "span_overlap_different_value",
                      "true_silence"):
            n = totals[label]
            print(f"   {label:<34} {n:>4}  ({100 * n / short:5.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
