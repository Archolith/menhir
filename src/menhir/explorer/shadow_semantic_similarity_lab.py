"""Shadow-composition semantic-similarity lab (validation experiment, NOT yet wired into
production shadow_context_composition.py).

Follow-up to `.agent/reviews/menhir-shadow-context-composition-facet-instability-2026-07-16.md`,
which found that Stage 1's exact-string-match join between LLM-generated `shadow_facet` labels
never fires on real data (9/9 real runs abstained) because the grounding prompt deliberately
avoids a fixed vocabulary while the eligibility filter assumes one exists.

Proposed alternative (direct instruction, 2026-07-16): score each candidate by
    similarity(embed(current_message), embed(candidate_fact_text))
instead of asking an LLM to invent short labels and then comparing those labels. This module is
the offline validation step ("First, make Stage 1 produce a useful precision/recall curve...
before proceeding to Stage 2") -- it must demonstrate the approach separates real matches from
near-miss negatives BEFORE any production wiring is attempted.

Ground truth is NOT hand-authored from scratch. It reuses the 21 already-validated eligibility
scenarios from Phase 4b (`extraction_lab_eligibility_fixtures.py`), which carry a real
`correct_candidate_id` and a real `DecoyType` per candidate (WRONG_STATE_FAMILY, WRONG_SCOPE,
WRONG_SUBJECT, STALE, MISSING_METADATA) -- exactly the negative categories requested
(same-entity-wrong-facet, temporally-invalid) plus a genuine positive class, all already reviewed
and committed ground truth rather than freshly-invented labels. "Unrelated control" is
constructed by cross-pairing each fixture family's message against every OTHER family's candidate
pool -- three real, topically distinct message/graph-content pairs, not synthetic filler.

This module contains only pure logic (fixture assembly, cosine similarity, precision/recall
curve). The runner that makes real embedding calls is
scripts/run_shadow_semantic_similarity_lab.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from menhir.explorer.extraction_lab_eligibility_fixtures import (
    _830_scenarios,
    _852_scenarios,
    _2698_scenarios,
)
from menhir.explorer.extraction_lab_eligibility_selection import DecoyType, EligibilityScenario

Category = Literal[
    "positive",
    "wrong_state_family",
    "wrong_scope",
    "wrong_subject",
    "stale",
    "missing_metadata",
    "cross_family_control",
]

_DECOY_TO_CATEGORY: dict[DecoyType, Category] = {
    DecoyType.WRONG_STATE_FAMILY: "wrong_state_family",
    DecoyType.WRONG_SCOPE: "wrong_scope",
    DecoyType.WRONG_SUBJECT: "wrong_subject",
    DecoyType.STALE: "stale",
    DecoyType.MISSING_METADATA: "missing_metadata",
}


@dataclass(frozen=True)
class SimilarityLabRow:
    """One (message, candidate) pair with its ground-truth label. embed_message_text /
    embed_candidate_text are the exact strings to embed -- kept separate from the raw
    fixture fields so the runner never has to re-derive prompt-shaping logic."""

    scenario_id: str
    fixture_qid: str
    category: Category
    is_positive: bool
    candidate_id: str
    embed_message_text: str
    embed_candidate_text: str


def _family_scenarios() -> dict[str, list[EligibilityScenario]]:
    """Scenarios grouped by fixture family, for cross-family control construction."""
    return {
        "lme-830ce83f": _830_scenarios(),
        "lme-852ce960": _852_scenarios(),
        "lme-2698e78f": _2698_scenarios(),
    }


def build_similarity_lab_rows() -> list[SimilarityLabRow]:
    """Assemble every within-family (positive/negative) row plus cross-family control rows.

    Within-family: for every candidate in every one of the 21 real eligibility scenarios,
    label it positive iff it's that scenario's correct_candidate_id, else map its DecoyType
    to a negative category (candidates with DecoyType.NONE that are NOT the correct answer
    do not occur in the fixture set -- every non-correct candidate carries an explicit decoy
    type -- so this mapping is total over the real data).

    Cross-family: each family's own message paired against every OTHER family's candidates
    -- three real, unrelated (message, graph content) pairs per candidate, all true negatives
    by construction (a Rachel/Housing message has no legitimate business matching a Mortgage
    or Therapy-schedule candidate).
    """
    families = _family_scenarios()
    rows: list[SimilarityLabRow] = []

    # Within-family: dedupe by candidate id within a scenario is unnecessary here, but the
    # SAME candidate id can recur across scenarios within a family (e.g. "c1" is the fixture's
    # designated correct candidate in every 830/852/2698 scenario) -- scenario_id + candidate_id
    # is the real identity, not candidate_id alone.
    for qid, scenarios in families.items():
        for scenario in scenarios:
            for candidate in scenario.candidates:
                is_positive = candidate.id == scenario.correct_candidate_id
                category: Category
                if is_positive:
                    category = "positive"
                else:
                    category = _DECOY_TO_CATEGORY.get(candidate.decoy_type, "wrong_state_family")
                rows.append(SimilarityLabRow(
                    scenario_id=scenario.scenario_id,
                    fixture_qid=qid,
                    category=category,
                    is_positive=is_positive,
                    candidate_id=candidate.id,
                    embed_message_text=scenario.current_message,
                    embed_candidate_text=candidate.text,
                ))

    # Cross-family controls: one representative message per family (the "1_one_correct"
    # scenario's message -- identical message text across every scenario in a family, so any
    # one scenario's current_message is representative) against every candidate belonging to
    # a DIFFERENT family.
    family_message: dict[str, str] = {
        qid: scenarios[0].current_message for qid, scenarios in families.items()
    }
    for qid, scenarios in families.items():
        other_candidates: list[tuple[str, str]] = []
        for other_qid, other_scenarios in families.items():
            if other_qid == qid:
                continue
            seen: set[str] = set()
            for scenario in other_scenarios:
                for candidate in scenario.candidates:
                    key = f"{other_qid}:{candidate.id}"
                    if key in seen:
                        continue
                    seen.add(key)
                    other_candidates.append((candidate.id, candidate.text))
        for candidate_id, candidate_text in other_candidates:
            rows.append(SimilarityLabRow(
                scenario_id=f"cross_family_{qid}",
                fixture_qid=qid,
                category="cross_family_control",
                is_positive=False,
                candidate_id=candidate_id,
                embed_message_text=family_message[qid],
                embed_candidate_text=candidate_text,
            ))

    return rows


def unique_embed_texts(rows: list[SimilarityLabRow]) -> list[str]:
    """Distinct strings that need embedding -- messages repeat heavily (3 unique across 21
    within-family scenarios; cross-family reuses the same 3), candidates repeat less but a
    few (like "c1") recur verbatim across scenarios in the same family. Dedup keeps the
    real embedding-API call count small and keeps costs/time predictable."""
    seen: set[str] = set()
    ordered: list[str] = []
    for row in rows:
        for text in (row.embed_message_text, row.embed_candidate_text):
            if text not in seen:
                seen.add(text)
                ordered.append(text)
    return ordered


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass(frozen=True)
class ScoredRow:
    row: SimilarityLabRow
    similarity: float


def score_rows(
    rows: list[SimilarityLabRow], embeddings: dict[str, list[float]],
) -> list[ScoredRow]:
    """Combine fixture rows with a text->embedding lookup (built by the runner from real
    embedding-API calls) into similarity-scored rows. Missing embeddings score 0.0 rather
    than raising -- a lab tool should degrade to an obviously-wrong data point, not crash the
    whole sweep over one failed embedding call."""
    scored = []
    for row in rows:
        a = embeddings.get(row.embed_message_text)
        b = embeddings.get(row.embed_candidate_text)
        similarity = cosine_similarity(a, b) if a is not None and b is not None else 0.0
        scored.append(ScoredRow(row=row, similarity=similarity))
    return scored


@dataclass(frozen=True)
class ThresholdPoint:
    threshold: float
    precision: float
    recall: float
    f1: float
    true_positives: int
    false_positives: int
    false_negatives: int


def precision_recall_curve(
    scored_rows: list[ScoredRow], *, num_thresholds: int = 41,
) -> list[ThresholdPoint]:
    """Sweep similarity thresholds from 0.0 to 1.0 (inclusive, num_thresholds points).
    A candidate is treated as SELECTED at threshold t iff similarity >= t; is_positive is
    the fixture's ground truth. Standard precision/recall/F1 over that binary decision."""
    total_positives = sum(1 for sr in scored_rows if sr.row.is_positive)
    points: list[ThresholdPoint] = []
    for i in range(num_thresholds):
        threshold = i / (num_thresholds - 1)
        tp = fp = 0
        for sr in scored_rows:
            selected = sr.similarity >= threshold
            if selected and sr.row.is_positive:
                tp += 1
            elif selected and not sr.row.is_positive:
                fp += 1
        fn = total_positives - tp
        precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
        recall = tp / total_positives if total_positives > 0 else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
        points.append(ThresholdPoint(
            threshold=threshold, precision=precision, recall=recall, f1=f1,
            true_positives=tp, false_positives=fp, false_negatives=fn,
        ))
    return points


def best_f1_point(points: list[ThresholdPoint]) -> ThresholdPoint:
    return max(points, key=lambda p: p.f1)


def category_summary(scored_rows: list[ScoredRow]) -> dict[str, dict[str, float]]:
    """Mean/min/max similarity per category -- the headline diagnostic for whether direct
    message<->fact embedding similarity actually separates positives from each negative
    category, independent of any single threshold choice."""
    buckets: dict[str, list[float]] = {}
    for sr in scored_rows:
        buckets.setdefault(sr.row.category, []).append(sr.similarity)
    summary: dict[str, dict[str, float]] = {}
    for category, values in buckets.items():
        summary[category] = {
            "count": len(values),
            "mean": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
        }
    return summary
