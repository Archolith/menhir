"""Intent x ContentRole affinity matrix + the multi-hit reduction (IntentOracle).

The matrix is the single data-driven extension point: adding a task is a row, adding an
artifact kind is a column, and no consumer code changes (the oracle reads the table). The
SIGNS (PREFER / NEUTRAL / PENALIZE / IGNORE) are the human-authored contract; the WEIGHT
MAGNITUDES are the bench's first calibration (archolith-bench `intent/` graduated them).

Multi-hit (design 4A): for several matched intents and/or several candidate roles, the
affinity is the MAX over the cross-product — "most-helpful-wins", the ranking dual of the
WardenChain's most-restrictive-wins.

Ported from archolith-bench `archolith_bench/intent/matrix.py` (the falsified prototype).
This module consumes the existing temporal lens vocabulary; it does NOT recompute
supersession — the classifier only *selects* which temporal lens the oracle stack uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from menhir.domain.artifact_role import ContentRole
from menhir.domain.query_intent import TaskIntent
from menhir.domain.temporal_lens import TemporalLens


class Affinity(str, Enum):
    PREFER = "prefer"
    NEUTRAL = "neutral"
    PENALIZE = "penalize"
    IGNORE = "ignore"


# Ranking order for the max reduction (PREFER beats NEUTRAL beats PENALIZE beats IGNORE).
_RANK: dict[Affinity, int] = {
    Affinity.IGNORE: 0,
    Affinity.PENALIZE: 1,
    Affinity.NEUTRAL: 2,
    Affinity.PREFER: 3,
}

# Bench-calibrated magnitudes (design left these open; the bench set them). Maps an
# affinity to a relevance weight in [0, 1] the IntentOracle emits as its probability.
_WEIGHT: dict[Affinity, float] = {
    Affinity.PREFER: 1.0,
    Affinity.NEUTRAL: 0.25,
    Affinity.PENALIZE: 0.05,
    Affinity.IGNORE: 0.0,
}

# Shorthand for authoring the table (G = iGnore; avoids the ambiguous bare `I`).
P, N, X, G = Affinity.PREFER, Affinity.NEUTRAL, Affinity.PENALIZE, Affinity.IGNORE

# Column order (must match the row tuples below).
_ROLES: tuple[ContentRole, ...] = (
    ContentRole.FAILURE, ContentRole.INCIDENT, ContentRole.DECISION, ContentRole.EXPERIMENT,
    ContentRole.BENCHMARK, ContentRole.TEST, ContentRole.PLAN, ContentRole.RUNBOOK,
    ContentRole.EVIDENCE, ContentRole.REFERENCE,
)

# The 8 x 10 affinity matrix (design 4). Rows = task intent, columns = _ROLES order.
_ROWS: dict[TaskIntent, tuple[Affinity, ...]] = {
    #                         FAIL INC  DEC  EXP  BEN  TST  PLN  RUN  EVD  REF
    TaskIntent.DEBUG_FAILURE:     (P,  P,   N,   N,   X,   P,   X,   P,   P,   X),
    TaskIntent.AVOID_REPEAT:      (P,  N,   P,   P,   P,   N,   X,   X,   N,   X),
    TaskIntent.EXPLAIN_DECISION:  (N,  N,   P,   P,   P,   X,   X,   X,   P,   N),
    TaskIntent.VERIFY_CURRENTNESS:(N,  X,   N,   N,   P,   P,   X,   N,   P,   P),
    TaskIntent.EVIDENCE_LOOKUP:   (N,  X,   N,   N,   P,   P,   X,   X,   P,   X),
    TaskIntent.CHANGE_ANALYSIS:   (P,  P,   N,   N,   N,   P,   X,   X,   P,   X),
    TaskIntent.PLAN_NEXT_ACTION:  (P,  N,   P,   P,   N,   X,   P,   N,   X,   X),
    # Personal-history recall wants the lived episode/decision, not a plan for the future:
    # prefer INCIDENT/FAILURE/EXPERIMENT/DECISION, stay neutral on reference-ish content
    # (LME facts classify mostly REFERENCE, so never PENALIZE it), ignore PLAN (future).
    TaskIntent.RECALL_PERSONAL_HISTORY:(P, P,  P,   P,   N,   N,   G,   N,   N,   N),
    TaskIntent.UNDERSTAND_SYSTEM: (N,  X,   N,   N,   N,   X,   N,   N,   X,   P),
}

INTENT_ROLE_MATRIX: dict[TaskIntent, dict[ContentRole, Affinity]] = {
    intent: dict(zip(_ROLES, row)) for intent, row in _ROWS.items()
}

# Temporal lens routing (the lens read by QueryContext.intent / TemporalOracle).
#   AVOID_REPEAT       -> TemporalLens.HISTORICAL : the past attempt IS the answer (boost superseded).
#   VERIFY_CURRENTNESS -> TemporalLens.ANY        : surface current + superseded side by side for the
#                                        drift check WITHOUT boosting the stale one. (This is
#                                        the belief.QueryIntent.CONFLICT equivalent; the bench
#                                        found routing verify to "historical" wrongly boosts
#                                        the stale item via TemporalOracle.)
#   everything else    -> TemporalLens.CURRENT    : superseded suppressed as today.
_LENS: dict[TaskIntent, TemporalLens] = {
    TaskIntent.AVOID_REPEAT: TemporalLens.HISTORICAL,
    #   RECALL_PERSONAL_HISTORY -> "historical" : a "what was the ... I had / did I ..."
    #     question asks for the lived past episode; the answer fact is often belief-superseded
    #     (a later turn re-mentioned it), and under "current" the TemporalOracle refuses it as
    #     superseded. The historical lens makes superseded facts SUPPORT the answer instead.
    TaskIntent.RECALL_PERSONAL_HISTORY: TemporalLens.HISTORICAL,
    TaskIntent.VERIFY_CURRENTNESS: TemporalLens.ANY,
}
# History-wanting lens wins when intents disagree (design 4A): historical > any > current.
_LENS_PRIORITY: tuple[TemporalLens, ...] = (
    TemporalLens.HISTORICAL, TemporalLens.ANY, TemporalLens.CURRENT,
)


@dataclass(frozen=True)
class IntentOpinion:
    """The IntentOracle's per-candidate opinion (design 1.2), kept for explainability."""

    affinity: Affinity
    weight: float
    winning_intent: TaskIntent
    winning_role: ContentRole
    lens: str
    reason: str


def affinity(intent: TaskIntent, role: ContentRole) -> Affinity:
    return INTENT_ROLE_MATRIX[intent][role]


def affinity_to_weight(a: Affinity) -> float:
    return _WEIGHT[a]


def resolve_affinity(
    intents: list[TaskIntent], roles: set[ContentRole]
) -> tuple[Affinity, TaskIntent, ContentRole]:
    """Max affinity over (intents x roles) — most-helpful-wins (design 4A).

    Returns the winning affinity and the (intent, role) pair that produced it, for the
    explainable rationale."""
    best: tuple[Affinity, TaskIntent, ContentRole] | None = None
    for intent in intents:
        row = INTENT_ROLE_MATRIX[intent]
        for role in roles:
            a = row[role]
            if best is None or _RANK[a] > _RANK[best[0]]:
                best = (a, intent, role)
    if best is None:
        return Affinity.NEUTRAL, TaskIntent.UNDERSTAND_SYSTEM, ContentRole.REFERENCE
    return best


def task_intents_to_lens(intents: list[TaskIntent]) -> TemporalLens:
    """Select the temporal lens; the history-wanting lens wins on conflict (design 4A).

    Returns one of TemporalLens.CURRENT | HISTORICAL | ANY -- the value QueryContext.intent
    carries into the oracle stack. A TemporalLens member is a ``str``, so callers comparing
    against the plain strings "current"/"historical"/"any" keep working. The classifier picks
    the lens; temporal.py / belief.py still own what the lens does (no second supersession
    rule)."""
    selected = {_LENS.get(i, TemporalLens.CURRENT) for i in intents}
    for lens in _LENS_PRIORITY:
        if lens in selected:
            return lens
    return TemporalLens.CURRENT
