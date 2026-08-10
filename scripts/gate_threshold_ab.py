#!/usr/bin/env python3
"""Gate threshold shadow A/B (3/3 vs 2/3) -- SAME-SAMPLES, offline, NO production change.

Isolates the k-sample consistency GATE from extraction noise: for each fixture prompt it extracts k
samples ONCE with the real LLM, then gates the IDENTICAL samples at threshold=1.0 (production) and
threshold=2/3 (candidate), and classifies every committed decision against ground truth. This answers
the owner's decision criterion -- "does 2/3 create enough additional CORRECT, useful admissions without
materially reducing precision or causing wrongful authority?" -- rather than "2/3 admits more".

Measured per threshold: accepted / correct / wrong-kind / wrong-value / wrong-unit / abstained, plus the
DELTA that 2/3 admits over 3/3 split into correct vs wrong (the actual decision signal). Nothing is
persisted; menhir's own `extract_typed_scalars_once` + `gate_typed_scalars` are used unchanged.

Usage: run from the menhir venv with OPENAI_API_KEY in the environment (or .env):
    .venv/Scripts/python.exe scripts/gate_threshold_ab.py --k 3 --runs 3
"""

from __future__ import annotations

import argparse
import os
from collections import Counter
from dataclasses import dataclass

from menhir.services.typed_scalar_perception import (
    extract_typed_scalars_once,
    gate_typed_scalars,
)


# --------------------------------------------------------------------------- fixtures + ground truth


@dataclass(frozen=True)
class Fix:
    fid: str
    prompt: str
    kind: str | None       # expected value_kind; None = a control that should yield NOTHING
    value: object | None   # expected value (normalized loosely); None for controls
    unit: str | None = None


FIXTURES: list[Fix] = [
    Fix("coins", "I own 37 rare coins.", "count", 37),
    Fix("headphones", "I paid $250 for my new headphones.", "money", 250, "usd"),
    Fix("height", "My height is 180 centimeters.", "measurement", 180, "cm"),
    Fix("commute", "My commute takes 45 minutes.", "duration", 45, "minutes"),
    Fix("gym", "I go to the gym 3 times a week.", "frequency", 3, "week"),
    Fix("wake", "I wake up at 7:30 every morning.", "clock_time", "07:30"),
    Fix("dayoff", "My day off is Wednesday.", "weekday", "Wednesday"),
    Fix("book", "I have finished reading Dune.", "boolean", True),
    # control: a one-off past event -> should NOT yield a standing assertion at EITHER threshold.
    Fix("event", "I went for a walk this morning.", None, None),
]

# ADVERSARIAL: value-ambiguous statements whose SAFE reading is abstention (the source does not state a
# single definite value). These are the precision tripwire: any COMMITTED value here is over-confident,
# so `kind=None` -> a commit classifies as `wrong_control`. If 2/3 admits values 3/3 abstained on, the
# threshold is trading precision for yield on under-determined inputs.
ADVERSARIAL: list[Fix] = [
    Fix("adv_time", "I usually wake up around 7, sometimes 7:30.", None, None),
    Fix("adv_count", "I have about 30 or 40 rare coins.", None, None),
    Fix("adv_money", "My rent is 1500 or maybe 1600 dollars.", None, None),
    Fix("adv_freq", "I go to the gym a few times a week.", None, None),
]


class _Ep:
    def __init__(self, uuid: str, content: str) -> None:
        self.uuid = uuid
        self.content = content


# --------------------------------------------------------------------------- llm


def make_llm(model: str, temperature: float):
    """A menhir `LlmComplete` (system,user)->text backed by the real OpenAI chat model. temp>0 so the
    k samples are genuinely independent draws (the whole point of the consistency gate)."""
    from openai import OpenAI

    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    def complete(system: str, user: str) -> str:
        resp = client.chat.completions.create(
            model=model, temperature=temperature,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return resp.choices[0].message.content or "[]"

    return complete


# --------------------------------------------------------------------------- classification


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _value_ok(expected, actual) -> bool:
    if isinstance(expected, bool):
        return str(actual).strip().lower() in ({"true", "yes", "1", "1.0"} if expected
                                               else {"false", "no", "0", "0.0"})
    ef = _num(expected)
    if ef is not None:
        af = _num(actual)
        return af is not None and abs(af - ef) <= 1e-6
    return str(expected).strip().lower() in str(actual).strip().lower()


def classify(fix: Fix, decision) -> str:
    """Classify ONE committed decision for this fixture against ground truth."""
    p = decision.proposal
    if fix.kind is None:
        return "wrong_control"   # a control committed anything -> over-perception
    if str(p.value_kind) != fix.kind:
        return "wrong_kind"
    if not _value_ok(fix.value, p.value):
        return "wrong_value"
    if fix.unit and fix.unit.lower() not in (str(p.unit or "").lower() or fix.unit.lower()):
        # unit mismatch only flagged when we expected a specific unit and the perceiver gave a different
        # non-empty one; an empty unit is tolerated (canonicalization varies) -> not a hard wrong.
        if p.unit and fix.unit.lower() not in str(p.unit).lower():
            return "wrong_unit"
    return "correct"


def gate_and_classify(fix: Fix, samples, threshold: float) -> list[str]:
    """Gate this fixture's samples at `threshold` and classify every COMMITTED decision."""
    decisions = gate_typed_scalars(samples, threshold=threshold)
    return [classify(fix, d) for d in decisions if d.committed and d.proposal is not None]


# --------------------------------------------------------------------------- driver


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--runs", type=int, default=3, help="independent extraction rounds to average over")
    ap.add_argument("--model", default=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini"))
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--set", choices=["clean", "adversarial", "both"], default="clean",
                    help="clean = single-value fixtures; adversarial = value-ambiguous (abstain-expected)")
    args = ap.parse_args()
    fixtures = {"clean": FIXTURES, "adversarial": ADVERSARIAL, "both": FIXTURES + ADVERSARIAL}[args.set]

    if not os.getenv("OPENAI_API_KEY"):
        from dotenv import load_dotenv
        load_dotenv()
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY not set (env or .env)")

    llm = make_llm(args.model, args.temperature)
    thresholds = {"3/3": 1.0, "2/3": 2.0 / 3.0}

    # aggregate class counts per threshold, and the per-fixture admission delta (2/3 minus 3/3)
    totals = {t: Counter() for t in thresholds}
    delta_correct = 0   # admissions 2/3 adds over 3/3 that are CORRECT
    delta_wrong = 0     # admissions 2/3 adds over 3/3 that are WRONG (the precision cost)
    per_fixture: list[str] = []

    print(f"== Gate threshold shadow A/B (k={args.k}, runs={args.runs}, model={args.model}, "
          f"temp={args.temperature}) ==\n")
    for run in range(1, args.runs + 1):
        for fix in fixtures:
            ep = _Ep(f"ep-{fix.fid}-{run}", f"user: {fix.prompt}")
            samples = [extract_typed_scalars_once([ep], llm) for _ in range(args.k)]
            cls = {t: gate_and_classify(fix, samples, thr) for t, thr in thresholds.items()}
            for t in thresholds:
                if cls[t]:
                    totals[t].update(cls[t])
                else:
                    totals[t].update(["abstained"])
            # admission delta on the CORRECT-answer slot: did 2/3 admit a correct value 3/3 didn't?
            c3 = "correct" in cls["3/3"]
            c2 = "correct" in cls["2/3"]
            if c2 and not c3:
                delta_correct += 1
            # new WRONG admissions under 2/3 (wrong_* present in 2/3 but not 3/3)
            wrong2 = [c for c in cls["2/3"] if c.startswith("wrong")]
            wrong3 = [c for c in cls["3/3"] if c.startswith("wrong")]
            if len(wrong2) > len(wrong3):
                delta_wrong += 1
            per_fixture.append(
                f"  run{run} {fix.fid:11} 3/3={sorted(cls['3/3']) or ['abstain']}  "
                f"2/3={sorted(cls['2/3']) or ['abstain']}")

    print("\n".join(per_fixture))
    print("\n-- class totals per threshold (over all fixtures x runs) --")
    keys = ["correct", "wrong_kind", "wrong_value", "wrong_unit", "wrong_control", "abstained"]
    hdr = f"   {'threshold':10} " + " ".join(f"{k:>13}" for k in keys)
    print(hdr)
    for t in ("3/3", "2/3"):
        print(f"   {t:10} " + " ".join(f"{totals[t].get(k, 0):>13}" for k in keys))

    print("\n-- DECISION SIGNAL (2/3 relative to 3/3) --")
    print(f"   fixtures where 2/3 added a CORRECT admission 3/3 missed: {delta_correct}")
    print(f"   fixtures where 2/3 added a WRONG admission 3/3 avoided:   {delta_wrong}")
    verdict = ("2/3 net-positive" if delta_correct > delta_wrong else
               "2/3 net-negative" if delta_wrong > delta_correct else "2/3 net-neutral")
    print(f"   -> {verdict} on this fixture set (correctness-weighted, NOT View-count-weighted)")
    print("\n(shadow only -- production gate stays 3/3 / threshold=1.0)")


if __name__ == "__main__":
    main()
