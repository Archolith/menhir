"""Truth attestation — trust tier, source provenance, and assertion policy for a claim.

A :class:`TruthAttestation` is the canonical truth envelope for any piece of content
produced or recalled by menhir.  It replaces the scattered pattern of
``(source_confidence: float | None, source: str | None, warden_label: str | None)`` with
one first-class, inspectable object.

Vocabulary comes from the existing doc design — no new names are invented:

* :class:`ReviewState` — from ``docs/research/schemas/layer4-knowledge-artifacts.md``.
  Formalises the existing ``source_confidence`` float tiers (1.0 / 0.9 / 0.5) into
  named levels that match the oracle-architecture combiner's own output sections:
  *Deterministic facts / Trusted semantic knowledge / AI hypotheses*.
* :class:`~menhir.domain.belief.RecallBucket` — the existing LLM-facing assertion
  policy (``domain/belief.py``).
* :class:`~menhir.domain.warden.WardenDecision` — the existing gate verdict
  (``domain/warden.py``).

The :class:`TruthClaim` Protocol is the abstract interface so future truth-bearing types
(e.g. a ``TemporalTruth`` or ``ConflictTruth``) can slot in without changing consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from menhir.domain.belief import RecallBucket
from menhir.domain.truth.kinds import (
    SOURCE_CONFIDENCE_AGENT,
    SOURCE_CONFIDENCE_AGENT_REVIEWED,
    SOURCE_CONFIDENCE_STRUCTURAL,
)
from menhir.domain.truth.labels import WardenLabel
from menhir.domain.warden import WardenDecision


# ---------------------------------------------------------------------------
# Trust tier
# ---------------------------------------------------------------------------

class ReviewState(str, Enum):
    """Source-based trust tier — who established this claim.

    Vocabulary from ``docs/research/schemas/layer4-knowledge-artifacts.md``.

    Maps to the existing ``MemoryNode.source_confidence`` float values:

    =================  ===================  ==============================================
    ReviewState        source_confidence    When to use
    =================  ===================  ==============================================
    HUMAN_REVIEWED     1.0 or 0.9           User statement; structural/deterministic
                                            ground truth (git, file, test, project-scan).
                                            Structural data is deterministic — verifiable
                                            without a human reviewer — so it earns the
                                            same tier as an explicit user confirmation.
    AGENT_REVIEWED     ~0.7                 Agent-validated claim awaiting human sign-off.
    UNREVIEWED         0.5                  Raw LLM inference; unvalidated agent discovery.
    =================  ===================  ==============================================
    """

    HUMAN_REVIEWED = "human_reviewed"
    AGENT_REVIEWED = "agent_reviewed"
    UNREVIEWED     = "unreviewed"


def review_state_from_confidence(confidence: float | None) -> ReviewState:
    """Map an existing ``source_confidence`` float to a :class:`ReviewState` tier.

    Thresholds are derived from the named constants in :mod:`menhir.domain.truth.kinds`
    so they stay in sync if the constants are recalibrated:

    * ``>= SOURCE_CONFIDENCE_STRUCTURAL`` (0.9)  → :attr:`ReviewState.HUMAN_REVIEWED`
    * ``>= SOURCE_CONFIDENCE_AGENT_REVIEWED`` (0.7) → :attr:`ReviewState.AGENT_REVIEWED`
    * below 0.7, or ``None``                      → :attr:`ReviewState.UNREVIEWED`

    An unknown / absent confidence defaults to UNREVIEWED (conservative rule-zero).
    """
    if confidence is None:
        return ReviewState.UNREVIEWED
    if confidence >= SOURCE_CONFIDENCE_STRUCTURAL:
        return ReviewState.HUMAN_REVIEWED
    if confidence >= SOURCE_CONFIDENCE_AGENT_REVIEWED:
        return ReviewState.AGENT_REVIEWED
    return ReviewState.UNREVIEWED


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

@runtime_checkable
class TruthClaim(Protocol):
    """Abstract interface for any truth-bearing object in menhir.

    Concrete implementations (e.g. :class:`TruthAttestation`) satisfy this
    structurally.  Future types (``TemporalTruth``, ``ConflictTruth``) can add
    extra fields and still slot into formatters and warden consumers unchanged.
    """

    @property
    def content(self) -> str: ...

    @property
    def review_state(self) -> ReviewState: ...

    @property
    def source(self) -> str: ...

    @property
    def assertion_policy(self) -> RecallBucket: ...

    @property
    def is_current(self) -> bool: ...


# ---------------------------------------------------------------------------
# Concrete implementation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TruthAttestation:
    """A menhir truth claim: content + trust + source + assertion policy.

    This is the single object that flows out of the warden/belief pipeline and
    into formatters and recall result serialisation.  All fields are drawn from
    existing domain vocabulary so callers that already understand
    :class:`~menhir.domain.belief.RecallBucket` and
    :class:`~menhir.domain.warden.WardenDecision` need no new concepts.

    Satisfies :class:`TruthClaim` structurally (no explicit inheritance needed).
    """

    content: str
    review_state: ReviewState        # trust tier derived from source
    source: str                      # raw source label: "user", "git", "project-scan", …
    source_confidence: float         # original float kept for continuity (0.5 / 0.9 / 1.0)
    assertion_policy: RecallBucket   # LLM-facing policy bucket (SAFE_TO_ASSERT, …)
    warden_decision: WardenDecision  # gate verdict (ADMIT / FLAG / ATTENUATE / REFUSE)
    is_superseded: bool = False
    label: WardenLabel | None = None  # warden FLAG qualifier — always a WardenLabel value
    rationale: tuple[str, ...] = field(default_factory=tuple)  # belief-scorer rationale lines

    # --- derived properties ---------------------------------------------------

    @property
    def is_current(self) -> bool:
        return not self.is_superseded

    @property
    def is_admitted(self) -> bool:
        """True when the warden gate allows this claim into current-truth context."""
        return self.warden_decision in (WardenDecision.ADMIT, WardenDecision.ATTENUATE)

    # --- factory classmethods -------------------------------------------------

    @classmethod
    def from_source_confidence(
        cls,
        content: str,
        *,
        source: str,
        source_confidence: float | None,
        assertion_policy: RecallBucket,
        warden_decision: WardenDecision,
        is_superseded: bool = False,
        label: WardenLabel | None = None,
        rationale: tuple[str, ...] = (),
    ) -> TruthAttestation:
        """Build from the existing ``MemoryNode.source_confidence`` float.

        This is the migration path from the old scattered fields: pass the values
        you already have and get a properly typed :class:`TruthAttestation` back.
        """
        return cls(
            content=content,
            review_state=review_state_from_confidence(source_confidence),
            source=source,
            source_confidence=source_confidence if source_confidence is not None else SOURCE_CONFIDENCE_AGENT,
            assertion_policy=assertion_policy,
            warden_decision=warden_decision,
            is_superseded=is_superseded,
            label=label,
            rationale=rationale,
        )

    @classmethod
    def unscored(
        cls,
        content: str,
        *,
        source: str,
        source_confidence: float | None = None,
    ) -> TruthAttestation:
        """Build for content that has not passed through the warden/belief pipeline.

        Defaults to :attr:`~RecallBucket.SAFE_TO_ASSERT` /
        :attr:`~WardenDecision.ADMIT` — the same permissive behaviour that all
        recall took before the frontier warden gate was introduced.  Use this for
        non-frontier paths so they emit a typed object rather than a bare string.
        """
        return cls(
            content=content,
            review_state=review_state_from_confidence(source_confidence),
            source=source,
            source_confidence=source_confidence if source_confidence is not None else SOURCE_CONFIDENCE_AGENT,
            assertion_policy=RecallBucket.SAFE_TO_ASSERT,
            warden_decision=WardenDecision.ADMIT,
        )
