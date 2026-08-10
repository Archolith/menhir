"""L4 institutional artifacts — domain model + R9-lite trust policy.

This is the menhir-side projection of the bench-first L4 slice (`archolith_bench/l4`,
spec in `.agent/plans/l4-artifact-loop-v0.md`). Decision / Failure / Incident artifacts
backed by FIRST-CLASS Evidence. The trust rules live here as pure functions so the single
writer (ArtifactRepository, fronted by ArtifactService) stays thin and the rules stay
testable without a live graph.

It maps onto existing menhir scope semantics rather than inventing a parallel one
(domain.models.NodeScope):

    ArtifactStatus.TRUSTED    -> scope PERSISTENT   (recallable as fact)
    ArtifactStatus.CANDIDATE  -> scope CANDIDATE    (review tier, never recalled as fact)
    ArtifactStatus.HISTORICAL -> scope PERSISTENT, artifact_status=historical
                                 (superseded — kept, never deleted; invariant 7)

The four hard rules this module encodes (the L4 invariants the bench pinned down):
  - invariant 4: an LLM-sourced artifact is NEVER trusted on create (review-gated).
  - invariant 5: a human artifact is trusted on create iff it has >= 1 evidence anchor.
  - invariant 3: promotion to TRUSTED is refused without evidence (fail-closed).
  - invariant 7: supersession marks HISTORICAL; it never deletes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from menhir.domain.models import NodeScope

# Evidence kinds; the structural ones are deterministic anchors, never LLM-derived
# (invariant 6). These mirror archolith_bench/l4/models.py exactly.
EVIDENCE_KINDS: frozenset[str] = frozenset({"git", "test", "user", "log", "agent_inference"})
STRUCTURAL_EVIDENCE: frozenset[str] = frozenset({"git", "test"})

# agent_inference is LLM self-evidence: it can back a CANDIDATE hypothesis but can NEVER, on
# its own, justify TRUST ("not trusted because an LLM said so" — invariant 4 extended). Trust
# requires at least one piece of non-LLM-self evidence (human / git / test / log).
NON_PROMOTING_EVIDENCE: frozenset[str] = frozenset({"agent_inference"})
PROMOTABLE_EVIDENCE: frozenset[str] = EVIDENCE_KINDS - NON_PROMOTING_EVIDENCE

# source_confidence stored on the node, by trust level. A trusted (reviewed/evidence-backed)
# artifact should not sit at the neutral 0.5 default — it would be under-weighted by scoring.
_CONFIDENCE_TRUSTED = 0.9
_CONFIDENCE_CANDIDATE_HUMAN = 0.5
_CONFIDENCE_CANDIDATE_LLM = 0.4


class ArtifactType(str, Enum):
    DECISION = "decision"
    FAILURE = "failure"
    INCIDENT = "incident"


class ArtifactStatus(str, Enum):
    CANDIDATE = "candidate"    # low-trust review tier; not asserted as fact
    TRUSTED = "trusted"        # evidence-backed/reviewed; recallable as fact
    HISTORICAL = "historical"  # superseded — kept, never deleted (invariant 7)


class ArtifactSource(str, Enum):
    HUMAN = "human"
    LLM = "llm"


@dataclass(frozen=True)
class Evidence:
    """A first-class piece of evidence backing an artifact (not a loose note)."""

    kind: str
    ref: str
    directness: float = 1.0
    note: str | None = None

    @property
    def is_structural(self) -> bool:
        """Deterministic anchor (git/test) — never LLM-derived (invariant 6)."""
        return self.kind in STRUCTURAL_EVIDENCE

    @property
    def is_promotable(self) -> bool:
        """Can this evidence justify TRUST? LLM self-inference (agent_inference) cannot."""
        return self.kind not in NON_PROMOTING_EVIDENCE

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "directness": float(self.directness),
            "note": self.note,
            "is_structural": self.is_structural,
        }


def scope_for_status(status: ArtifactStatus) -> str:
    """Map an artifact status onto the NodeScope value the graph stores.

    CANDIDATE artifacts live in the review tier; TRUSTED and HISTORICAL are PERSISTENT
    (HISTORICAL stays in the graph, distinguished by artifact_status, never deleted)."""
    if status is ArtifactStatus.CANDIDATE:
        return NodeScope.CANDIDATE.value
    return NodeScope.PERSISTENT.value


def has_promotable_evidence(evidence_kinds: Iterable[str]) -> bool:
    """True iff at least one evidence kind can justify trust (i.e. is not agent_inference)."""
    return any(kind not in NON_PROMOTING_EVIDENCE for kind in evidence_kinds)


def decide_status(*, source: ArtifactSource, has_promotable_evidence: bool) -> ArtifactStatus:
    """The create-time trust decision (R9-lite). There is deliberately no way to pass a
    status in: an LLM artifact can never be born TRUSTED (invariant 4), and a human one is
    TRUSTED only with at least one PROMOTABLE evidence anchor — agent_inference alone never
    suffices (invariant 5 + 4 extended)."""
    if source is ArtifactSource.LLM:
        return ArtifactStatus.CANDIDATE
    return ArtifactStatus.TRUSTED if has_promotable_evidence else ArtifactStatus.CANDIDATE


def can_promote(*, has_promotable_evidence: bool, is_historical: bool) -> bool:
    """Promotion to TRUSTED is allowed only with PROMOTABLE evidence (invariant 3; LLM
    self-inference can't justify trust) and never for a superseded artifact (invariant 7)."""
    return has_promotable_evidence and not is_historical


def confidence_for(*, status: ArtifactStatus, source: ArtifactSource) -> float:
    """source_confidence to store for a freshly written/promoted artifact, by trust level."""
    if status is ArtifactStatus.TRUSTED:
        return _CONFIDENCE_TRUSTED
    if source is ArtifactSource.LLM:
        return _CONFIDENCE_CANDIDATE_LLM
    return _CONFIDENCE_CANDIDATE_HUMAN
