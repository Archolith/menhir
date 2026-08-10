"""domain.truth — the truth package for menhir.

Re-exports all public symbols from the sub-modules so callers can use:

    from menhir.domain.truth import ReviewState, TruthAttestation, WardenLabel, ANCHOR_KINDS

Sub-modules:
- :mod:`.attestation` — ReviewState, TruthClaim, TruthAttestation, review_state_from_confidence
- :mod:`.kinds`       — evidence-kind constants (ANCHOR_KINDS, SELF_SOURCE_KINDS, KIND_TO_SIGNAL,
                        DIVERSITY_FAMILY, SOURCE_CONFIDENCE_* constants)
- :mod:`.labels`      — WardenLabel enum (replaces bare warden label strings)
"""

from .attestation import (
    ReviewState,
    TruthAttestation,
    TruthClaim,
    review_state_from_confidence,
)
from .kinds import (
    ANCHOR_KINDS,
    DIVERSITY_FAMILY,
    KIND_TO_SIGNAL,
    SELF_SOURCE_KINDS,
    SOURCE_CONFIDENCE_AGENT,
    SOURCE_CONFIDENCE_AGENT_REVIEWED,
    SOURCE_CONFIDENCE_STRUCTURAL,
    SOURCE_CONFIDENCE_USER,
    SOURCE_LABEL_TO_KIND,
)
from .labels import WardenLabel

__all__ = [
    # attestation
    "ReviewState",
    "TruthAttestation",
    "TruthClaim",
    "review_state_from_confidence",
    # kinds
    "ANCHOR_KINDS",
    "DIVERSITY_FAMILY",
    "KIND_TO_SIGNAL",
    "SELF_SOURCE_KINDS",
    "SOURCE_CONFIDENCE_AGENT",
    "SOURCE_CONFIDENCE_AGENT_REVIEWED",
    "SOURCE_CONFIDENCE_STRUCTURAL",
    "SOURCE_CONFIDENCE_USER",
    "SOURCE_LABEL_TO_KIND",
    # labels
    "WardenLabel",
]
