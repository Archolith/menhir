"""Shadow-composition LLM-judge validation experiment (offline, NOT wired into production).

Third step in the response chain to `.agent/reviews/menhir-shadow-context-composition-facet-
instability-2026-07-16.md`:
  1. facet-instability report: exact-string-match label join never fires on real data.
  2. semantic-similarity lab: embedding similarity separates unrelated content cleanly, but no
     single threshold achieves both perfect precision and recall -- one real same-entity/
     wrong-facet decoy ("boundaries") scores HIGHER than every true positive.
  3. This module: per direct instruction, embeddings are a coarse filter, not a final decision --
     "use embeddings to find candidates worth examining, and use the LLM judge to decide whether
     they truly belong." Tests whether an LLM judge, given a (message, candidate fact) pair,
     preserves every true positive while correctly rejecting the hard negatives similarity alone
     could not separate -- especially the "boundaries" case.

Reuses shadow_semantic_similarity_lab.py's fixture assembly (build_similarity_lab_rows) rather
than rebuilding it -- same 21 Phase-4b-validated eligibility scenarios, same categories, same
cross-family controls, so judge results are directly comparable row-for-row against the
similarity lab's scores.

This module contains only pure logic (prompt construction, response parsing, scoring against
ground truth). The runner that makes real LLM calls is scripts/run_shadow_llm_judge_lab.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from menhir.explorer.shadow_semantic_similarity_lab import SimilarityLabRow

# v1 of this prompt asked "is this the SAME fact" and scored 0/12 recall -- it rejected every
# true positive, because these are knowledge-update fixtures: the correct candidate is
# deliberately the PRIOR value the message is updating (message: "Rachel moved to the
# suburbs" / correct candidate: "Rachel previously moved to Chicago" -- different values, same
# real-world slot). "Same fact" is the wrong axis. v2 asks about the same STATE/SLOT instead,
# matching the eligibility semantics established in Phase 4b (subject+facet+state_family, not
# content equality) -- and must still reject same-entity/near-topic decoys, which is exactly
# the case pure similarity could not handle (the lme-2698e78f "boundaries" decoy scored 0.5794,
# above every true positive's max 0.4537).
JUDGE_SYSTEM_PROMPT = """You are verifying whether a CANDIDATE FACT from a memory graph is about
the SAME specific real-world topic/state that a CURRENT MESSAGE references or updates -- NOT
whether they state the same value.

The CURRENT MESSAGE often describes a NEW or CHANGED value for something the person has
mentioned before (e.g. "Rachel moved to the suburbs" may update an earlier "Rachel moved to
Chicago"). In that case the earlier CANDIDATE FACT is exactly what you're looking for: it does
NOT need to state the same value as the message -- it needs to be about the same specific
topic/state for the same subject (e.g. Rachel's current residence, not Rachel's housing
preferences; a person's therapy SESSION FREQUENCY, not the topics discussed IN therapy).

Two facts can share the same subject (person/entity) and even the same broad topic while being
about completely different specific states -- e.g. "Rachel prefers apartments with a balcony"
and "Rachel moved to a new apartment" are both about Rachel's housing, but one is about
preferences and the other about her actual residence. These do NOT match, even though the
overall topic is similar.

Answer MATCH if the CANDIDATE FACT is about the same subject and the same specific real-world
state/slot the CURRENT MESSAGE is discussing (whether confirming it or updating it with a new
value). Answer NO_MATCH if the candidate is about a different subject, a different specific
state/topic for that subject, or is otherwise unrelated.

Answer with exactly one word: MATCH or NO_MATCH."""


def judge_user_prompt(message: str, candidate_text: str) -> str:
    return (
        f"CURRENT MESSAGE:\n{message}\n\n"
        f"CANDIDATE FACT:\n{candidate_text}\n\n"
        "Does the CANDIDATE FACT represent the same specific fact/state the CURRENT MESSAGE "
        "is about? Answer with exactly one word: MATCH or NO_MATCH."
    )


def parse_judge_response(raw: str | None) -> bool | None:
    """Returns True (MATCH), False (NO_MATCH), or None (unparseable/empty -- treated as a
    judge failure, not silently coerced to either answer). Checks NO_MATCH before MATCH
    since "NO_MATCH" contains "MATCH" as a substring."""
    if not raw:
        return None
    normalized = raw.strip().upper()
    if "NO_MATCH" in normalized or "NO MATCH" in normalized:
        return False
    if "MATCH" in normalized:
        return True
    return None


@dataclass(frozen=True)
class JudgedRow:
    row: SimilarityLabRow
    judgment: bool | None  # None = judge call failed or returned unparseable output


def confusion_counts(judged_rows: list[JudgedRow]) -> dict[str, int]:
    """TP/FP/TN/FN plus a separate `unparseable` bucket -- an unparseable judge response is a
    judge failure, not silently folded into either class (that would hide a real reliability
    problem behind an inflated or deflated accuracy number)."""
    tp = fp = tn = fn = unparseable = 0
    for jr in judged_rows:
        if jr.judgment is None:
            unparseable += 1
            continue
        if jr.row.is_positive and jr.judgment:
            tp += 1
        elif jr.row.is_positive and not jr.judgment:
            fn += 1
        elif not jr.row.is_positive and jr.judgment:
            fp += 1
        else:
            tn += 1
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn, "unparseable": unparseable}


def precision_recall_f1(counts: dict[str, int]) -> dict[str, float]:
    tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def category_breakdown(judged_rows: list[JudgedRow]) -> dict[str, dict[str, int]]:
    """Per-category outcome counts -- for a positive category, counts how many were correctly
    matched vs. missed; for a negative category, how many were correctly rejected vs. wrongly
    accepted. Same shape as the similarity lab's category_summary, for direct comparison."""
    breakdown: dict[str, dict[str, int]] = {}
    for jr in judged_rows:
        cat = jr.row.category
        bucket = breakdown.setdefault(cat, {"correct": 0, "incorrect": 0, "unparseable": 0})
        if jr.judgment is None:
            bucket["unparseable"] += 1
        elif jr.judgment == jr.row.is_positive:
            bucket["correct"] += 1
        else:
            bucket["incorrect"] += 1
    return breakdown
