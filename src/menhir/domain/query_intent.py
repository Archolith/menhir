"""Deterministic task-intent classification for recall (IntentOracle producer).

A query carries a *task* intent ("which candidate best helps THIS task?") that is
orthogonal to the temporal intent (`temporal_intent.py`) and the belief QueryIntent
(`belief.py`). The same knowledge is a great hit for one task and mediocre for another:
"why did we choose the floor" wants a DECISION, "have we tried this" wants an EXPERIMENT,
"is it still accurate" wants a BENCHMARK/REFERENCE.

This is the deterministic keyword baseline — an ordered cue cascade that returns the SET
of task intents whose signature cue matched (multi-hit, design 4A), each with the literal
cue for explainability. No LLM. Mirrors the proven `classify_temporal_intent` shape.

Ported from archolith-bench `archolith_bench/intent/classifier.py` (the falsified
prototype that graduated the promotion gate). Bench-gated: the IntentOracle that consumes
this stays out of the production oracle set until a real-embedder run re-confirms the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskIntent(str, Enum):
    """What the caller is trying to accomplish (the task axis)."""

    DEBUG_FAILURE = "debug_failure"
    AVOID_REPEAT = "avoid_repeat"
    EXPLAIN_DECISION = "explain_decision"
    VERIFY_CURRENTNESS = "verify_currentness"
    EVIDENCE_LOOKUP = "evidence_lookup"
    CHANGE_ANALYSIS = "change_analysis"
    PLAN_NEXT_ACTION = "plan_next_action"
    # A first-person past-experience recall ("what was the first issue I had", "which
    # restaurant did I go to", "how much did I pay") — the LongMemEval / anecdotal-memory
    # question shape. The answer is a HISTORICAL episode/fact (often belief-superseded by a
    # later mention), so this routes to the "historical" lens; otherwise the default "current"
    # lens has the TemporalOracle refuse the dated answer as superseded. Deliberately gated on
    # a first-person + past cue ("did i ", "i had ", "i bought") so a bare past-tense code query
    # ("what was the command", "what was the function name") does NOT flip the lens.
    RECALL_PERSONAL_HISTORY = "recall_personal_history"
    UNDERSTAND_SYSTEM = "understand_system"  # default / orientation


class IntentConfidence(str, Enum):
    HIGH = "high"   # a signature cue matched
    LOW = "low"     # bare default; do not distort ranking


# Precedence orders only the PRIMARY label (most specific first). Affinity maxes over the
# whole matched set downstream, so this ordering does not change rankings.
_PRECEDENCE: tuple[TaskIntent, ...] = (
    TaskIntent.AVOID_REPEAT,
    TaskIntent.RECALL_PERSONAL_HISTORY,
    TaskIntent.DEBUG_FAILURE,
    TaskIntent.EXPLAIN_DECISION,
    TaskIntent.VERIFY_CURRENTNESS,
    TaskIntent.EVIDENCE_LOOKUP,
    TaskIntent.CHANGE_ANALYSIS,
    TaskIntent.PLAN_NEXT_ACTION,
    TaskIntent.UNDERSTAND_SYSTEM,
)

# Signature cues per intent (lowercase substring match). Authored to be specific to the
# task, not the topic vocabulary.
_CUES: dict[TaskIntent, tuple[str, ...]] = {
    TaskIntent.AVOID_REPEAT: (
        "already tried", "have we tried", "did we try", "have we already",
        "tried before", "previously attempted", "already done", "tried again",
    ),
    # First-person PAST personal experience. Each cue pairs a first-person marker with a
    # past-tense verb, so it fires on anecdotal recall ("the first issue i had", "how much
    # did i pay", "which car i bought") but not on a bare past-tense code lookup ("what was
    # the command / function name"). "did i " subsumes "what/when/where/how much did i ...".
    TaskIntent.RECALL_PERSONAL_HISTORY: (
        "did i ", "i had ", "i bought", "i purchased", "i ordered", "i paid",
        "i spent", "i chose", "i picked", "i decided", "i went ", "i visited",
        "i stayed", "i installed", "i tried", "i used ", "when i ", "after i ",
        "before i ",
    ),
    TaskIntent.DEBUG_FAILURE: (
        "failing", "failed", "fails", "error", "errors", "crash", "broken",
        "not working", "stack trace", "traceback", "exception", "why is", "throwing",
    ),
    TaskIntent.EXPLAIN_DECISION: (
        "why did we", "why do we", "why was", "rationale", "reason for",
        "reason we", "chose", "choose", "decided", "decision to",
    ),
    TaskIntent.VERIFY_CURRENTNESS: (
        "still", "up to date", "out of date", "outdated", "stale",
        "still accurate", "still valid", "still the case",
    ),
    TaskIntent.EVIDENCE_LOOKUP: (
        "which benchmark", "which test", "what verifies", "what proves",
        "proof of", "evidence for", "what covers", "backed by",
    ),
    TaskIntent.CHANGE_ANALYSIS: (
        "what changed", "blast radius", "what depends", "depends on",
        "impact of", "since commit", "what broke after", "diff",
    ),
    TaskIntent.PLAN_NEXT_ACTION: (
        "what next", "next step", "what should i", "what do i do",
        "how do i", "do next", "what now",
    ),
    TaskIntent.UNDERSTAND_SYSTEM: (
        "how does", "what is", "explain how", "how do", "what are", "overview of",
    ),
}


@dataclass(frozen=True)
class IntentHit:
    intent: TaskIntent
    cue: str


def classify_intent(text: str) -> tuple[list[IntentHit], IntentConfidence]:
    """Return every task intent whose cue matched, plus a confidence.

    Multi-hit: a query can carry several intents (design 4A). The list is ordered by
    precedence so the first element is a stable primary label. If nothing matches, the
    default is UNDERSTAND_SYSTEM at LOW confidence (the oracle then stays neutral)."""
    lowered = text.lower()
    hits: list[IntentHit] = []
    for intent in _PRECEDENCE:
        for cue in _CUES[intent]:
            if cue in lowered:
                hits.append(IntentHit(intent, cue))
                break  # one cue per intent is enough to flag it
    if not hits:
        return [IntentHit(TaskIntent.UNDERSTAND_SYSTEM, "")], IntentConfidence.LOW
    return hits, IntentConfidence.HIGH


def primary_intent(hits: list[IntentHit]) -> TaskIntent:
    """The highest-precedence matched intent (the display/log label)."""
    return hits[0].intent if hits else TaskIntent.UNDERSTAND_SYSTEM
