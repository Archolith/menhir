#!/usr/bin/env python3
"""LongMemEval knowledge-update ground truth, and value matching against it.

Shared by the experiment probes. Exists because commit COUNTS are not evidence: an arm that commits
more claims has not improved anything unless the extra claims are TRUE. The plan says this outright
("do not infer production value from proposal/decision counts alone"), and this investigation has
already produced one confident wrong answer by counting movement instead of truth.

Ground truth lives in the archolith-bench fixture, keyed by `question_id`. The LME namespaces are
named `lme-scalar-ku-<date>-<question_id>`, so the namespace suffix IS the question id.

Answers are free text ("25 minutes and 50 seconds (or 25:50)", "$400,000"), so matching is numeric
rather than textual.

THE TRAP THAT MAKES A NAIVE MATCHER WORSE THAN USELESS HERE
-----------------------------------------------------------
These are KNOWLEDGE-UPDATE questions, so the recorded answer routinely states BOTH the old and the
new value:

    031748ae  "When you just started ... you led 4 engineers. Now, you lead 5 engineers"
    c6853660  "You increased the limit (from one cup to two cups)"

An "any number appearing in the answer" match accepts 4 and accepts 1 -- i.e. it scores COMMITTING
THE STALE VALUE, the precise failure this investigation exists to fix, as a SUCCESS. A stale-value
regression would then appear as an improvement, and the metric would actively point the wrong way.

So the current value is taken as the LAST number in the answer, and earlier numbers are reported
separately as STALE rather than folded into either correct or wrong. The plan's primary metrics
already ask for stale to be its own bucket; this makes that possible.

Still generous in one respect: a right number found for a wrong reason counts as correct. Compare
arms against each other on the same corpus; do not quote these as absolute correctness rates.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache

_FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "..", "archolith-bench", "fixtures", "longmemeval", "knowledge_update_subset.json",
)

_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)*")


#: Spelled-out numbers appear in a large minority of LME answers ("four", "Three times a week").
#: Without this the matcher scores every one of them WRONG, which does not merely add noise -- it
#: biases against exactly the spelled-out values that span-anchoring also struggles with, so two
#: independent measurements would degrade on a correlated subset and look like a real effect.
_WORD_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
    "once": 1, "twice": 2, "daily": 1, "half": 0.5,
}


def _numbers_ordered(value) -> list[float]:
    """Numbers in the order they appear -- ORDER IS LOAD-BEARING. For a knowledge-update answer the
    last number is the current value and any earlier one is the superseded value."""
    text = "" if value is None else str(value)
    hits: list[tuple[int, float]] = []
    for m in re.finditer(r"[a-z]+", text.lower()):
        if m.group(0) in _WORD_NUM:
            hits.append((m.start(), float(_WORD_NUM[m.group(0)])))
    for m in _NUM_RE.finditer(text):
        raw = m.group(0)
        cleaned = raw.replace(",", "") if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", raw) else raw
        try:
            hits.append((m.start(), float(cleaned)))
        except ValueError:
            continue
    return [v for _pos, v in sorted(hits)]


def _numbers(value) -> set[float]:
    """Unordered convenience view over `_numbers_ordered`. Accepts non-str (some fixture answers
    are bare ints)."""
    text = "" if value is None else str(value)
    out: set[float] = set()
    for word in re.findall(r"[a-z]+", text.lower()):
        if word in _WORD_NUM:
            out.add(float(_WORD_NUM[word]))
    for m in _NUM_RE.finditer(text):
        raw = m.group(0)
        # "25:50" is captured as two numbers, which is what we want -- either may be the answer.
        cleaned = raw.replace(",", "") if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", raw) else raw
        try:
            out.add(float(cleaned))
        except ValueError:
            continue
    return out


@lru_cache(maxsize=1)
def load() -> dict[str, dict]:
    """question_id -> record. Empty dict (not an exception) when the fixture is absent, so a probe
    can still run and simply report truth as unavailable."""
    path = os.path.normpath(_FIXTURE)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    return {str(r["question_id"]): r for r in rows}


def question_id(namespace_or_suffix: str) -> str:
    """`lme-scalar-ku-20260722-031748ae` -> `031748ae`; a bare suffix passes through. Trailing
    variant markers like `_abs` are stripped -- they denote a corpus variant, not a new question."""
    tail = namespace_or_suffix.rsplit("-", 1)[-1]
    return tail.split("_", 1)[0]


def answer_for(namespace_or_suffix: str) -> str | None:
    rec = load().get(question_id(namespace_or_suffix))
    return str(rec["answer"]) if rec else None


_TRUEISH = {"true", "yes", "y"}
_FALSEISH = {"false", "no", "n"}


#: verdicts from `classify`
CURRENT = "current"      # matches the answer's most-recent value
UNKNOWN = "unknown"      # no ground truth for this namespace -> exclude, never score as wrong

#: Matches an EARLIER value the answer also mentions.
#:
#: THIS IS NOT A DEFECT AT THE ASSERTION LEVEL, and calling it "stale" there was wrong.
#: ScalarStateView preserves full history by design: superseded versions are KEPT with
#: `view_current=false` and linked by `SUPERSEDES` (view_repository.py:5, :694-709). An assertion
#: carrying an earlier value is the raw material for that history, not an error -- and some LME
#: questions REQUIRE it. `031748ae` asks "How many engineers do I lead when I just started my new
#: role? How many engineers do I lead now?" -- 4 AND 5 are both correct answers, and an arm that
#: recorded only 5 has answered half the question.
#:
#: The genuine defect lives one level down, at the fold: a view with `view_current=true` holding a
#: superseded value, or a missing SUPERSEDES chain. In production `031748ae` has exactly one view,
#: `team_size=4`, `view_current=true`, and ZERO supersession chains -- the update to 5 was never
#: perceived. THAT is the defect, and it is only observable after folding (Phase 2), never from
#: assertion counts.
HISTORICAL = "historical"

#: retained for older call sites; do NOT treat as a defect bucket at assertion level.
STALE = HISTORICAL

#: NOT "wrong". LME labels exactly ONE fact per namespace, but a namespace states many, and a memory
#: system SHOULD record all of them. This bucket therefore mixes two unlike things:
#:
#:   (a) a wrong value for the fact that WAS asked        -- a real defect
#:   (b) a CORRECT fact the benchmark does not label      -- correct behaviour, unscoreable
#:
#: Measured on the 21 existing LME Views, (b) is the majority: 7 of 12 are facts like
#: `miles_ridden` (question asked about bikes owned), `wake_time`/`bed_time`/`lunch_break`
#: (question asked about episodes completed), `commute=convenient` (question asked about apartment
#: tenure). Every one of those is true and worth storing.
#:
#: One more sits in here as a pure scoring artifact: `eggs_stocked=240` against answer "20", where
#: the question asks "how many DOZEN eggs" -- 240 eggs IS 20 dozen. Factually correct, stored in a
#: different unit than the question wanted. That is a unit-normalization gap, not a wrong value.
#:
#: So: never report this as precision, and never let an arm be penalised for growing it. Use the
#: PER-NAMESPACE metric (`namespace_verdicts`) as primary -- "did we record the asked fact" is
#: answerable from this data; "is everything we recorded correct" is not.
UNMATCHED = "unmatched"

#: retained so older call sites keep working; identical to UNMATCHED.
WRONG = UNMATCHED


def classify(namespace_or_suffix: str, value) -> str:
    """CURRENT / HISTORICAL / UNMATCHED / UNKNOWN for a candidate value.

    HISTORICAL is NOT a defect at this level -- see the constant's docstring. It exists because
    matcher that cannot tell them apart rewards the very defect under investigation. See module
    docstring.

    A third of these answers are non-numeric ("Friday", "Yes", "the suburbs") and correspond to the
    weekday/boolean/status kinds, so they are matched too -- excluding them would quietly restrict
    the panel to numeric kinds and change what the experiment measures."""
    ans = answer_for(namespace_or_suffix)
    if ans is None:
        return UNKNOWN
    raw = str(value).strip().lower()
    if not raw:
        return WRONG

    if raw in _TRUEISH | _FALSEISH:
        head = re.sub(r"[^a-z]", "", ans.lower().split()[0]) if ans.split() else ""
        if head in _TRUEISH | _FALSEISH:
            return CURRENT if (raw in _TRUEISH) == (head in _TRUEISH) else WRONG

    ordered = _numbers_ordered(ans)
    try:
        v = float(raw.replace(",", ""))
    except (TypeError, ValueError):
        v = None
    if v is not None and ordered:
        if abs(v - ordered[-1]) < 1e-9:
            return CURRENT
        if any(abs(v - n) < 1e-9 for n in ordered[:-1]):
            return STALE
        return WRONG

    low = ans.lower()
    return CURRENT if (raw in low or low.strip(" .") in raw) else WRONG


def is_truth(namespace_or_suffix: str, value) -> bool | None:
    """Back-compat boolean: True only for CURRENT. None when there is no ground truth."""
    verdict = classify(namespace_or_suffix, value)
    if verdict == UNKNOWN:
        return None
    return verdict == CURRENT


def required_values(namespace_or_suffix: str) -> list[float]:
    """Every value the question actually needs answered, in order.

    Most LME knowledge-update questions want only the latest value. Some want BOTH -- `031748ae`
    literally asks two questions ("...when I just started my new role? ...now?") and `c6853660` asks
    for a DIRECTION of change, which is unanswerable without both endpoints. For those, an arm that
    recorded only the latest value has answered half the question, and an arm that recorded both is
    strictly better -- which is exactly what ScalarStateView's history model is for.
    """
    ans = answer_for(namespace_or_suffix)
    if ans is None:
        return []
    ordered = _numbers_ordered(ans)
    rec = load().get(question_id(namespace_or_suffix)) or {}
    q = str(rec.get("question", ""))
    wants_both = (
        q.count("?") > 1                                   # literally two questions
        or re.search(r"\bincrease[d]?\s+or\s+decrease[d]?\b", q, re.I)  # direction of change
        or re.search(r"\bmore\s+.*\bthan\b.*\bpreviously\b", q, re.I)
    )
    if wants_both and len(ordered) > 1:
        return ordered
    return ordered[-1:] if ordered else []


def coverage(namespace_or_suffix: str, values) -> tuple[int, int]:
    """(values the question needs that were recorded, values it needs) -- history counts FOR you.

    This is the metric that matches the architecture. A scalar view keeps superseded versions
    (view_current=false + SUPERSEDES), so recording an earlier value alongside the latest is the
    system working as designed, not a precision loss.
    """
    need = required_values(namespace_or_suffix)
    if not need:
        return (0, 0)
    got = set()
    for v in values:
        try:
            got.add(float(str(v).replace(",", "")))
        except (TypeError, ValueError):
            continue
    return (sum(1 for n in need if any(abs(n - g) < 1e-9 for g in got)), len(need))


def namespace_verdict(namespace_or_suffix: str, values) -> str:
    """THE PRIMARY METRIC. Did this namespace record the fact that was asked?

    Per-View precision cannot be computed from LME: it labels one fact per namespace while a
    namespace states many, so recording a true unasked fact is indistinguishable from recording a
    false one (see UNMATCHED). Per-namespace answerability has no such problem -- either one of the
    namespace's values matches the asked answer or none does.

    CURRENT beats STALE beats UNMATCHED: a namespace that produced the right value somewhere has
    answered the question, even if it also stored other facts. A namespace whose ONLY match is a
    superseded value is a genuine, and specific, failure -- it is the defect under investigation and
    must not be pooled with "recorded nothing relevant"."""
    verdicts = {classify(namespace_or_suffix, v) for v in values}
    if not verdicts or verdicts == {UNKNOWN}:
        return UNKNOWN
    for rank in (CURRENT, STALE, UNMATCHED):
        if rank in verdicts:
            return rank
    return UNKNOWN


if __name__ == "__main__":
    gt = load()
    print(f"loaded {len(gt)} ground-truth records from {os.path.normpath(_FIXTURE)}")
    for qid, rec in list(gt.items())[:5]:
        print(f"  {qid}  answer={rec['answer'][:60]!r}  numbers={sorted(_numbers(rec['answer']))}")
