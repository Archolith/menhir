"""Self-reinforcement guards (R8 / retrieval-control-rails.md) — anti-spiral rails.

Rule zero: *a memory being retrieved is evidence of attention, not evidence that it is true,
current, or useful.* These guards stop retrieval from reinforcing itself without external or
productive evidence (the RetrievalGravityWell / MetaMemoryRecursion / SyntheticSupportLoop /
StaleHeatLeak failure modes).

The guard set (Guard 6 RetrievalExhaustionPenalty already lives in domain/exhaustion.py):
- Guard 1 ProductiveTouchGate  -- a retrieval touch only increases durable heat on a
                                  productive outcome (user/test/compile/accepted answer).
- Guard 2 SyntheticSupportCap  -- a support chain of only agent-generated memories cannot
                                  back a current-truth assertion.
- Guard 5 EvidenceAnchorGate   -- every current-truth assertion needs >=1 non-self anchor
                                  (user/log/test/git/file/external), not LLM-said-it-before.
- Guard 3 MetaMemoryDepthBudget -- cap memory-call-hint chains.

Pure functions over transparent signals; no graph access. The assertion-boundary guards
(2 + 5) are surfaced as Wardens in domain/warden.py; the touch gate (1) is a write/heat
policy consumed by the mutator side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# Evidence-kind constants — SSOT lives in domain/truth/kinds.py.
from menhir.domain.truth.kinds import (
    ANCHOR_KINDS,
    SELF_SOURCE_KINDS,
    SOURCE_LABEL_TO_KIND as _SOURCE_LABEL_TO_KIND,
)

# Productive outcomes that justify increasing a memory's durable heat (Guard 1).
PRODUCTIVE_OUTCOMES: frozenset[str] = frozenset(
    {"user_confirmed", "test_passed", "code_compiled", "external_supported",
     "answer_accepted", "contradiction_resolved", "manual_approved"}
)

_DEFAULT_META_DEPTH_BUDGET = 2


def evidence_kind_for_source(source: str | None) -> str:
    """Map a freeform episode ``source`` label onto an evidence kind (rule-zero conservative).

    Known external-anchor labels pass through; a label that is already a self kind is kept;
    everything else (agent harness names, unknown provenance) collapses to ``agent_inference``
    so an agent-authored memory does not masquerade as externally anchored."""
    s = (source or "").strip().lower()
    if s in _SOURCE_LABEL_TO_KIND:
        return _SOURCE_LABEL_TO_KIND[s]
    if s in SELF_SOURCE_KINDS:
        return s
    return "agent_inference"


def has_external_anchor(support_kinds: list[str] | tuple[str, ...]) -> bool:
    """Guard 5 core: is there at least one non-self anchor in the support set?"""
    return any(kind in ANCHOR_KINDS for kind in support_kinds)


def is_synthetic_only(support_kinds: list[str] | tuple[str, ...]) -> bool:
    """Guard 2 core: is the support chain made ONLY of self/agent-generated sources?

    Empty support counts as synthetic-only (nothing external backs it)."""
    kinds = list(support_kinds)
    if not kinds:
        return True
    return all(kind in SELF_SOURCE_KINDS for kind in kinds)


class TouchOutcome(str, Enum):
    """What happened to a pending retrieval touch (Guard 1)."""

    PRODUCTIVE = "productive"      # a productive outcome occurred -> durable heat may rise
    PENDING = "pending"           # retrieved, no outcome yet -> no durable heat
    UNPRODUCTIVE = "unproductive"  # retrieved, no progress -> no durable heat (maybe penalty)


def resolve_touch(*, outcome: str | None) -> TouchOutcome:
    """Guard 1 (ProductiveTouchGate): classify a retrieval touch.

    A touch is PRODUCTIVE only if a recognized productive outcome fired; otherwise it is
    PENDING (no outcome) or UNPRODUCTIVE. Only PRODUCTIVE may increase durable heat —
    retrieval alone never does."""
    if outcome is None:
        return TouchOutcome.PENDING
    return TouchOutcome.PRODUCTIVE if outcome in PRODUCTIVE_OUTCOMES else TouchOutcome.UNPRODUCTIVE


def durable_heat_allowed(*, outcome: str | None) -> bool:
    """Convenience: may this touch increase durable retrieval weight? (Guard 1.)"""
    return resolve_touch(outcome=outcome) is TouchOutcome.PRODUCTIVE


def within_meta_depth_budget(meta_depth: int, *, budget: int = _DEFAULT_META_DEPTH_BUDGET) -> bool:
    """Guard 3 (MetaMemoryDepthBudget): is a memory-call-hint chain within the depth cap?"""
    return meta_depth <= budget


@dataclass(frozen=True)
class SupportProfile:
    """The provenance of a candidate's support, for the assertion-boundary guards.

    ``support_kinds`` are the evidence-source kinds backing the candidate; ``meta_depth`` is
    the memory-call-hint chain length that surfaced it."""

    support_kinds: tuple[str, ...] = ()
    meta_depth: int = 0


class ReinforcementVerdict(str, Enum):
    """The anti-spiral decision on whether a candidate may be asserted as current truth."""

    OK = "ok"                       # has external anchor, real support, within depth
    NEEDS_EVIDENCE = "needs_evidence"  # synthetic-only / no anchor -> not current truth
    OVER_DEPTH = "over_depth"        # meta-memory chain too deep -> suspicious


def assess_reinforcement(
    profile: SupportProfile, *, meta_budget: int = _DEFAULT_META_DEPTH_BUDGET
) -> ReinforcementVerdict:
    """Combine Guards 2/3/5 into one assertion-boundary verdict.

    Order: an over-budget meta chain is flagged first (the chain itself is suspect); then a
    synthetic-only / unanchored support set means the claim needs external evidence before it
    can be current truth; otherwise OK."""
    if not within_meta_depth_budget(profile.meta_depth, budget=meta_budget):
        return ReinforcementVerdict.OVER_DEPTH
    if is_synthetic_only(profile.support_kinds) or not has_external_anchor(profile.support_kinds):
        return ReinforcementVerdict.NEEDS_EVIDENCE
    return ReinforcementVerdict.OK
