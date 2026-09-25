"""Receipt dataclasses for the deterministic scalar extractor.

Moved verbatim from `deterministic_scalar_extractor.py` (facade split): per-candidate,
per-episode, and per-extraction result records. The facade module re-exports all three, so
every existing import site keeps working unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from menhir.services.typed_scalar_rules import TypedScalarProposal

# ------------------------------------------------------------------------------------------------ #
# Receipts
# ------------------------------------------------------------------------------------------------ #


@dataclass(frozen=True)
class CandidateReceipt:
    """One template match and its outcome: admitted proposal or explicit abstention reason."""

    template_id: str
    class_id: str
    episode_index: int
    source_start: int
    source_end: int
    stated_span: str
    subject_text: str
    attribute: str
    scope: str
    value_kind: str
    unit: str
    operation: str
    value: Any
    outcome: str
    drop_reason: str | None
    checks: tuple[str, ...]
    proposal: TypedScalarProposal | None


@dataclass(frozen=True)
class EpisodeReceipt:
    """Per-episode outcome: coverage status plus candidate-level receipts and abstention codes."""

    episode_uuid: str
    episode_index: int
    sentence_count: int
    fully_covered: bool
    reasons: tuple[str, ...]
    candidate_receipts: tuple[CandidateReceipt, ...]


@dataclass(frozen=True)
class DeterministicExtraction:
    """Complete result of one deterministic extraction pass (pure; nothing persisted)."""

    extractor_version: str
    template_version: str
    episode_receipts: tuple[EpisodeReceipt, ...]
    proposals: tuple[TypedScalarProposal, ...]
    fully_eligible_episode_uuids: tuple[str, ...]
    reason_counts: tuple[tuple[str, int], ...]
