#!/usr/bin/env python3
"""Span-consistency diagnostic (MEASURE FIRST -- no production change).

The unanimous gate is source-claim-first and groups by exact `source_key` (episode + span offsets +
ordinal). When the model quotes slightly different boundaries for the SAME claim ("but now I have 37."
vs "now I have 37"), the offsets differ -> different source_key -> each looks like a separate 1/3 claim
-> the gate abstains. This probe classifies the SHAPE of that disagreement so the owner decision rule
can fire:

    boundary-only/containment variation dominates  -> canonical span + identity v3 is worth it
    non-overlapping/paraphrased spans dominate      -> do NOT broaden identity; prompt-only

For each fixture it extracts k samples once, groups proposals by INTERPRETATION (episode + subject +
slot + operation + value + when -- NOT source_key), and within each group classifies the distinct spans:
    exact | ws_punct | leading_conjunction | other_containment | partial_overlap | disjoint
`ws_punct` and `leading_conjunction` are the SAFE-to-canonicalize categories. It also reports whether the
narrow v0 canonicalization (strip ws + terminal punctuation + leading inert conjunction) would UNIFY the
group's spans into one canonical form (and thus let the gate commit).

Nothing is persisted. Run from the menhir venv with OPENAI_API_KEY set.
"""

from __future__ import annotations

import argparse
import os
import re
from collections import Counter, defaultdict

from menhir.services.typed_scalar_perception import extract_typed_scalars_once

# --- fixtures ---------------------------------------------------------------------------------------
# COMPOUND: a past clause + a current clause -- the fragmentation case (current clause boundary wobbles).
COMPOUND = [
    "I used to wake up at 9:00, but now I wake up at 7:30.",
    "My day off was Monday, but now it is Wednesday.",
    "I weighed 180 kg, and now I weigh 175 kg.",
    "Yesterday I had 20 coins, but now I have 37 coins.",
    "I lived in Dallas, but currently I live in Austin.",
]
# CLEAN: single-value -- should be `exact` (control; canonicalization must not be needed).
CLEAN = [
    "I own 37 rare coins.",
    "I wake up at 7:30 every morning.",
    "My day off is Wednesday.",
]
# DISTINCT: two different slots in one episode -- must remain TWO groups (never collapsed).
DISTINCT = [
    "I have 37 coins, and I need 37 more coins.",
]

SETS = {"compound": COMPOUND, "clean": CLEAN, "distinct": DISTINCT,
        "all": COMPOUND + CLEAN + DISTINCT}


class _Ep:
    def __init__(self, uuid, content):
        self.uuid = uuid
        self.content = content


def make_llm(model, temperature):
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    def complete(system, user):
        r = client.chat.completions.create(
            model=model, temperature=temperature,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return r.choices[0].message.content or "[]"
    return complete


# --- narrow v0 canonicalization (DIAGNOSTIC ONLY here -- not wired into production) -----------------
_LEADING_CONJ = ("but", "and", "however", "so", "then", "yet")
_TERMINAL_PUNCT = ".,;:!?—–-"


def canonical_text(span: str) -> str:
    """Strip surrounding whitespace, terminal sentence punctuation, and a leading inert conjunction.
    Does NOT touch semantic tokens (now/actually/not/used to/about/between/starting/...)."""
    s = (span or "").strip().strip(_TERMINAL_PUNCT).strip()
    m = re.match(r"^(?:%s)\b[\s,]*" % "|".join(_LEADING_CONJ), s, re.I)
    if m:
        s = s[m.end():].strip()
    return s.lower()


def _ws_punct_only(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[\s%s]+" % re.escape(_TERMINAL_PUNCT), "", s.lower())  # noqa: E731
    return norm(a) == norm(b)


def classify_group(spans: list[str]) -> tuple[str, bool]:
    """Classify the distinct spans of one interpretation group. Returns (category, canonicalizable)."""
    distinct = list(dict.fromkeys(spans))
    if len(distinct) == 1:
        return ("exact", True)
    canon = {canonical_text(s) for s in distinct}
    canonicalizable = len(canon) == 1
    # ws/punct-only vs leading-conjunction: does stripping ONLY ws+punct already unify?
    if all(_ws_punct_only(distinct[0], s) for s in distinct[1:]):
        return ("ws_punct", True)
    if canonicalizable:
        return ("leading_conjunction", True)
    # canonical forms differ -> containment / overlap / disjoint (NOT safely canonicalizable)
    lo = [s.lower() for s in distinct]
    if any(all(x in y or y in x for y in lo) for x in lo):
        return ("other_containment", False)
    # pairwise token overlap?
    toks = [set(re.findall(r"\w+", s)) for s in lo]
    if any(toks[i] & toks[j] for i in range(len(toks)) for j in range(i + 1, len(toks))):
        return ("partial_overlap", False)
    return ("disjoint", False)


def interp_key(p) -> tuple:
    """The SEMANTIC claim identity (NOT source_key): episode + subject + slot + op + value + when."""
    return (p.episode_uuid, p.subject_text.strip().lower(), p.attribute, p.scope, p.value_kind,
            p.unit, p.operation, p.normalized_value, p.when or "")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--runs", type=int, default=6)
    ap.add_argument("--set", choices=sorted(SETS), default="all")
    ap.add_argument("--model", default=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"))
    ap.add_argument("--temperature", type=float, default=0.7)
    args = ap.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        from dotenv import load_dotenv
        load_dotenv()
    llm = make_llm(args.model, args.temperature)
    fixtures = SETS[args.set]

    cat_totals: Counter = Counter()
    multi_span_groups = 0
    canon_would_fix = 0
    fragmenting_fixtures = 0
    per_fixture: list[str] = []

    print(f"== Span-consistency probe (k={args.k}, runs={args.runs}, set={args.set}, "
          f"model={args.model}) ==\n")
    for run in range(1, args.runs + 1):
        for fx in fixtures:
            ep = _Ep(f"ep-{abs(hash(fx))%9999}-{run}", f"user: {fx}")
            samples = [extract_typed_scalars_once([ep], llm) for _ in range(args.k)]
            groups: dict[tuple, list[str]] = defaultdict(list)
            for s in samples:
                for p in s:
                    groups[interp_key(p)].append(p.stated_span)
            fx_fragmented = False
            for key, spans in groups.items():
                cat, canonicalizable = classify_group(spans)
                cat_totals[cat] += 1
                distinct = len(set(spans))
                if distinct > 1:
                    multi_span_groups += 1
                    fx_fragmented = True
                    if canonicalizable:
                        canon_would_fix += 1
                    per_fixture.append(
                        f"  run{run} {fx[:38]!r:40} attr={key[2]:14} cat={cat:18} "
                        f"spans={sorted(set(spans))}")
            if fx_fragmented:
                fragmenting_fixtures += 1

    print("\n".join(per_fixture) or "  (no multi-span groups observed)")
    print("\n-- category totals (over all interpretation groups) --")
    for c in ("exact", "ws_punct", "leading_conjunction", "other_containment",
              "partial_overlap", "disjoint"):
        print(f"   {c:20} {cat_totals.get(c, 0)}")
    print("\n-- fragmentation summary --")
    print(f"   multi-span groups (would fragment the gate): {multi_span_groups}")
    print(f"   ... that narrow v0 canonicalization WOULD unify: {canon_would_fix}")
    safe = cat_totals.get("ws_punct", 0) + cat_totals.get("leading_conjunction", 0)
    unsafe = (cat_totals.get("other_containment", 0) + cat_totals.get("partial_overlap", 0)
              + cat_totals.get("disjoint", 0))
    print(f"\n-- DECISION SIGNAL --")
    print(f"   boundary-only/containment (SAFE to canonicalize): {safe}")
    print(f"   non-overlapping/paraphrased (NOT safe):           {unsafe}")
    verdict = ("canonicalize + identity v3" if safe > unsafe and multi_span_groups > 0
               else "prompt-only / keep failing safe" if multi_span_groups > 0
               else "no fragmentation observed")
    print(f"   -> {verdict}")
    print("\n(diagnostic only -- no canonicalization is wired into production)")


if __name__ == "__main__":
    main()
