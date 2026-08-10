"""Pure value objects for non-persisted compositional scalar identity."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from menhir.domain.typed_assertion import OPERATIONS, VALUE_KINDS, build_source_key

__all__ = [
    "COMPOSITION_VERSION",
    "RELATION_TYPES",
    "CompositionalScalarIdentity",
    "ScalarIdentityProvenance",
]

COMPOSITION_VERSION = "composition-v1"

# Relations describe fold-relevant families. Open nouns belong in target_or_scope, not here.
RELATION_TYPES: frozenset[str] = frozenset({
    "quantity",
    "balance",
    "measurement",
    "schedule_time",
    "duration",
    "frequency",
    "state",
})


def _stable_hash(parts: tuple[str, ...]) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def _require_canonical_text(name: str, value: object, *, allow_blank: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    canonical = value.strip().lower()
    if not allow_blank and not canonical:
        raise ValueError(f"{name} must be non-blank")
    if value != canonical:
        raise ValueError(f"{name} must already be canonical trim/lower text")
    return canonical


@dataclass(frozen=True)
class ScalarIdentityProvenance:
    """Source locator plus the pure derivation receipt for one sidecar identity."""

    episode_uuid: str
    span_start: int
    span_end: int
    claim_ordinal: int
    source_key: str
    derivation_kind: str = "proposal_fields"
    derivation_version: str = COMPOSITION_VERSION
    rule_id: str = "fallback_state"

    def __post_init__(self) -> None:
        if not isinstance(self.episode_uuid, str) or not self.episode_uuid.strip():
            raise ValueError("scalar identity provenance requires an episode_uuid")
        if self.span_start < 0 or self.span_end < self.span_start:
            raise ValueError("scalar identity provenance requires valid grounded offsets")
        if self.claim_ordinal < 0:
            raise ValueError("scalar identity provenance claim_ordinal must be non-negative")
        if not isinstance(self.source_key, str) or not self.source_key.strip():
            raise ValueError("scalar identity provenance requires a source_key")
        expected_source_key = build_source_key(
            self.episode_uuid, self.span_start, self.span_end, self.claim_ordinal)
        if self.source_key != expected_source_key:
            raise ValueError("scalar identity provenance source_key does not match its locator")
        if not isinstance(self.derivation_kind, str) or not self.derivation_kind.strip():
            raise ValueError("scalar identity provenance requires a derivation kind")
        if not isinstance(self.derivation_version, str) or not self.derivation_version.strip():
            raise ValueError("scalar identity provenance requires a derivation version")
        if not isinstance(self.rule_id, str) or not self.rule_id.strip():
            raise ValueError("scalar identity provenance requires a rule_id")


@dataclass(frozen=True)
class CompositionalScalarIdentity:
    """Non-persisted semantic identity for one grounded typed-scalar proposal.

    Provenance is excluded from ``semantic_key`` and included in ``claim_key``. Composition
    version is included in both hashes so different normalizer contracts never compare silently.
    """

    subject: str
    relation_type: str
    target_or_scope: tuple[str, str]
    value_kind: str
    value: str
    unit: str
    operation: str
    effective_time: str | None
    provenance: ScalarIdentityProvenance
    composition_version: str = COMPOSITION_VERSION

    def __post_init__(self) -> None:
        _require_canonical_text("subject", self.subject)
        _require_canonical_text("relation_type", self.relation_type)
        if self.relation_type not in RELATION_TYPES:
            raise ValueError(
                f"relation_type {self.relation_type!r} not in {sorted(RELATION_TYPES)}")
        if not isinstance(self.target_or_scope, tuple) or len(self.target_or_scope) != 2:
            raise ValueError("target_or_scope must be a (target, scope) pair")
        target = _require_canonical_text("target", self.target_or_scope[0], allow_blank=True)
        scope = _require_canonical_text("scope", self.target_or_scope[1], allow_blank=True)
        if not target and not scope:
            raise ValueError("target_or_scope requires a non-blank target or scope")
        _require_canonical_text("value_kind", self.value_kind)
        if self.value_kind not in VALUE_KINDS:
            raise ValueError(f"value_kind {self.value_kind!r} not in {sorted(VALUE_KINDS)}")
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("value must be a non-blank normalized scalar string")
        _require_canonical_text("unit", self.unit, allow_blank=True)
        _require_canonical_text("operation", self.operation)
        if self.operation not in OPERATIONS:
            raise ValueError(f"operation {self.operation!r} not in {sorted(OPERATIONS)}")
        if self.effective_time is not None and (
            not isinstance(self.effective_time, str) or not self.effective_time.strip()
        ):
            raise ValueError("effective_time must be None or a non-blank normalized time string")
        if not isinstance(self.composition_version, str) or not self.composition_version.strip():
            raise ValueError("composition_version must be non-blank")

    @property
    def semantic_components(self) -> tuple[str, ...]:
        target, scope = self.target_or_scope
        return (
            self.composition_version,
            self.subject,
            self.relation_type,
            target,
            scope,
            self.value_kind,
            self.value,
            self.unit,
            self.operation,
            self.effective_time or "",
        )

    @property
    def semantic_key(self) -> str:
        """Stable hash of semantics only; safe to emit without open target/subject text."""
        return _stable_hash(self.semantic_components)

    @property
    def claim_key(self) -> str:
        """Stable hash of semantic identity plus the binding-stable source locator."""
        return _stable_hash((*self.semantic_components, self.provenance.source_key))
