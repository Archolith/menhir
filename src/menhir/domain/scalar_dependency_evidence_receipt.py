"""Quote-free, bounded receipt for offline persistence of scalar dependency evidence."""

from __future__ import annotations

from dataclasses import dataclass

from menhir.domain.scalar_dependency_evidence_limits import (
    MAX_CHECK_NAME_LENGTH,
    MAX_CHECK_NAMES,
    MAX_EDGES,
    MAX_MARKERS,
    MAX_OUTCOME_LENGTH,
    MAX_REASON_LENGTH,
    MAX_SOURCE_LENGTH,
    MAX_TOKENS,
)
from menhir.domain.scalar_dependency_evidence_validation import (
    _bounded_tuple,
    _hash,
    _nonnegative,
    _text,
    _validate_bounds,
    _version_token,
)


@dataclass(frozen=True, slots=True)
class ScalarDependencyEvidenceReceipt:
    """Quote-free, bounded receipt suitable for offline persistence."""

    schema_version: str
    evidence_version: str
    bridge_version: str
    source_hash: str
    candidate_hash: str
    evidence_sha256: str
    clause_start: int
    clause_end: int
    token_count: int
    edge_count: int
    marker_count: int
    outcome: str
    reason: str
    rule_version: str
    composer_version: str
    check_names: tuple[str, ...]

    def __post_init__(self) -> None:
        _version_token("schema_version", self.schema_version)
        _version_token("evidence_version", self.evidence_version)
        _version_token("bridge_version", self.bridge_version)
        _version_token("rule_version", self.rule_version)
        _version_token("composer_version", self.composer_version)
        _text("outcome", self.outcome, MAX_OUTCOME_LENGTH)
        _text("reason", self.reason, MAX_REASON_LENGTH)
        for name, value in (
            ("source_hash", self.source_hash),
            ("candidate_hash", self.candidate_hash),
            ("evidence_sha256", self.evidence_sha256),
        ):
            _hash(name, value)
        _validate_bounds("clause", self.clause_start, self.clause_end, MAX_SOURCE_LENGTH)
        for name, value in (
            ("token_count", self.token_count),
            ("edge_count", self.edge_count),
            ("marker_count", self.marker_count),
        ):
            _nonnegative(name, value)
        if self.token_count > MAX_TOKENS or self.edge_count > MAX_EDGES or self.marker_count > MAX_MARKERS:
            raise ValueError("receipt count exceeds transport bounds")
        checks = _bounded_tuple("check_names", self.check_names, MAX_CHECK_NAMES)
        for index, check in enumerate(checks):
            _text(f"check_names[{index}]", check, MAX_CHECK_NAME_LENGTH)
