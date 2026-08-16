"""Source-bound admission authority contracts.

The core rule is deliberately narrow: an untrusted claim may request authority, but it
cannot mint authority. A core-issued :class:`SourceAuthorityGrant` caps that request for
one exact source record, and a domain policy may reject the claim or lower the ceiling
further.

This module intentionally defines no global authority hierarchy. Callers inject
:class:`AuthoritySemantics`, so typed scalars can keep their existing total order while
other domains use different labels or richer comparison rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

__all__ = [
    "AdmissionDecision",
    "AdmissionPolicy",
    "AdmissionPolicyDecision",
    "AdmissionRequest",
    "AdmissionStatus",
    "AuthorityRelation",
    "AuthoritySemantics",
    "OrderedAuthoritySemantics",
    "SourceAuthorityGrant",
    "SourceProvenance",
    "decide_admission",
]


class AuthorityRelation(str, Enum):
    """Relationship of ``left`` authority to ``right`` authority."""

    WEAKER = "weaker"
    EQUAL = "equal"
    STRONGER = "stronger"
    INCOMPARABLE = "incomparable"


class AuthoritySemantics(Protocol):
    """Domain-owned comparison semantics for otherwise opaque authority labels."""

    def compare(self, left: str, right: str) -> AuthorityRelation:
        """Return how ``left`` relates to ``right``."""


@dataclass(frozen=True)
class OrderedAuthoritySemantics:
    """Convenience semantics for a domain that genuinely has one LOW -> HIGH order."""

    levels: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.levels:
            raise ValueError("authority levels must not be empty")
        if any(not str(level).strip() for level in self.levels):
            raise ValueError("authority levels must be non-blank")
        if len(set(self.levels)) != len(self.levels):
            raise ValueError("authority levels must be unique")

    def compare(self, left: str, right: str) -> AuthorityRelation:
        if left not in self.levels or right not in self.levels:
            return AuthorityRelation.INCOMPARABLE
        left_rank = self.levels.index(left)
        right_rank = self.levels.index(right)
        if left_rank < right_rank:
            return AuthorityRelation.WEAKER
        if left_rank > right_rank:
            return AuthorityRelation.STRONGER
        return AuthorityRelation.EQUAL


@dataclass(frozen=True)
class SourceProvenance:
    """Core-observed identity of the durable source record behind a claim."""

    source_id: str
    source_kind: str

    def __post_init__(self) -> None:
        _require_token("source_id", self.source_id)
        _require_token("source_kind", self.source_kind)


@dataclass(frozen=True)
class SourceAuthorityGrant:
    """Core-issued maximum authority for one exact source record."""

    grant_id: str
    source_id: str
    source_kind: str
    provenance_class: str
    authority_ceiling: str

    def __post_init__(self) -> None:
        _require_token("grant_id", self.grant_id)
        _require_token("source_id", self.source_id)
        _require_token("source_kind", self.source_kind)
        _require_token("provenance_class", self.provenance_class)
        _require_token("authority_ceiling", self.authority_ceiling)


@dataclass(frozen=True)
class AdmissionRequest:
    """Untrusted authority request attached to an interpreted claim."""

    requested_authority: str
    purpose: str

    def __post_init__(self) -> None:
        _require_token("requested_authority", self.requested_authority)
        _require_token("purpose", self.purpose)


@dataclass(frozen=True)
class AdmissionPolicyDecision:
    """Trusted domain-policy result.

    ``authority_ceiling`` is optional. When present it may only constrain the core
    grant; an attempted promotion is recorded and ignored.
    """

    accepted: bool
    policy_id: str
    policy_version: str
    authority_ceiling: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        _require_token("policy_id", self.policy_id)
        _require_token("policy_version", self.policy_version)
        if self.authority_ceiling is not None:
            _require_token("authority_ceiling", self.authority_ceiling)


class AdmissionPolicy(Protocol):
    """Domain-owned policy that may reject a claim or lower its authority."""

    def evaluate(
        self,
        *,
        request: AdmissionRequest,
        source: SourceProvenance,
        grant: SourceAuthorityGrant,
    ) -> AdmissionPolicyDecision:
        """Return the domain policy decision for one source-bound claim."""


class AdmissionStatus(str, Enum):
    ADMITTED = "admitted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class AdmissionDecision:
    """Immutable receipt of the authority boundary applied to one claim."""

    status: AdmissionStatus
    source_id: str
    source_kind: str
    provenance_class: str
    grant_id: str
    purpose: str
    requested_authority: str
    admission_ceiling: str
    policy_id: str | None
    policy_version: str | None
    policy_ceiling: str | None
    effective_authority: str | None
    requested_promotion: bool
    policy_attempted_promotion: bool
    reason: str

    @property
    def admitted(self) -> bool:
        return self.status is AdmissionStatus.ADMITTED


def _require_token(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")


def _relation(
    semantics: AuthoritySemantics,
    left: str,
    right: str,
) -> AuthorityRelation:
    relation = semantics.compare(left, right)
    if not isinstance(relation, AuthorityRelation):
        raise TypeError("authority semantics must return AuthorityRelation")
    return relation


def _rejected(
    *,
    source: SourceProvenance,
    grant: SourceAuthorityGrant,
    request: AdmissionRequest,
    policy: AdmissionPolicyDecision | None,
    requested_promotion: bool = False,
    policy_attempted_promotion: bool = False,
    reason: str,
) -> AdmissionDecision:
    return AdmissionDecision(
        status=AdmissionStatus.REJECTED,
        source_id=source.source_id,
        source_kind=source.source_kind,
        provenance_class=grant.provenance_class,
        grant_id=grant.grant_id,
        purpose=request.purpose,
        requested_authority=request.requested_authority,
        admission_ceiling=grant.authority_ceiling,
        policy_id=policy.policy_id if policy else None,
        policy_version=policy.policy_version if policy else None,
        policy_ceiling=policy.authority_ceiling if policy else None,
        effective_authority=None,
        requested_promotion=requested_promotion,
        policy_attempted_promotion=policy_attempted_promotion,
        reason=reason,
    )


def decide_admission(
    *,
    request: AdmissionRequest,
    source: SourceProvenance,
    grant: SourceAuthorityGrant,
    policy: AdmissionPolicy,
    semantics: AuthoritySemantics,
) -> AdmissionDecision:
    """Apply the source grant and domain policy without trusting requested authority.

    Fail-closed cases are represented as ``REJECTED`` decisions: a source/grant
    mismatch, an incomparable authority, or an explicit policy rejection. A stronger
    requested authority is clamped to the core ceiling. A policy ceiling stronger than
    the core ceiling is recorded as an attempted promotion and ignored.
    """

    if source.source_id != grant.source_id or source.source_kind != grant.source_kind:
        return _rejected(
            source=source,
            grant=grant,
            request=request,
            policy=None,
            reason="source provenance does not match authority grant",
        )

    request_relation = _relation(
        semantics,
        request.requested_authority,
        grant.authority_ceiling,
    )
    if request_relation is AuthorityRelation.INCOMPARABLE:
        return _rejected(
            source=source,
            grant=grant,
            request=request,
            policy=None,
            reason="requested authority is incomparable with admission ceiling",
        )

    requested_promotion = request_relation is AuthorityRelation.STRONGER
    effective = (
        grant.authority_ceiling
        if requested_promotion
        else request.requested_authority
    )

    policy_decision = policy.evaluate(
        request=request,
        source=source,
        grant=grant,
    )
    if not isinstance(policy_decision, AdmissionPolicyDecision):
        raise TypeError("admission policy must return AdmissionPolicyDecision")

    if not policy_decision.accepted:
        return _rejected(
            source=source,
            grant=grant,
            request=request,
            policy=policy_decision,
            requested_promotion=requested_promotion,
            reason=policy_decision.reason or "domain policy rejected admission",
        )

    policy_attempted_promotion = False
    policy_ceiling = policy_decision.authority_ceiling
    if policy_ceiling is not None:
        policy_vs_core = _relation(
            semantics,
            policy_ceiling,
            grant.authority_ceiling,
        )
        if policy_vs_core is AuthorityRelation.INCOMPARABLE:
            return _rejected(
                source=source,
                grant=grant,
                request=request,
                policy=policy_decision,
                requested_promotion=requested_promotion,
                reason="policy ceiling is incomparable with admission ceiling",
            )

        policy_attempted_promotion = (
            policy_vs_core is AuthorityRelation.STRONGER
        )
        if not policy_attempted_promotion:
            effective_vs_policy = _relation(
                semantics,
                effective,
                policy_ceiling,
            )
            if effective_vs_policy is AuthorityRelation.INCOMPARABLE:
                return _rejected(
                    source=source,
                    grant=grant,
                    request=request,
                    policy=policy_decision,
                    requested_promotion=requested_promotion,
                    reason="effective authority is incomparable with policy ceiling",
                )
            if effective_vs_policy is AuthorityRelation.STRONGER:
                effective = policy_ceiling

    return AdmissionDecision(
        status=AdmissionStatus.ADMITTED,
        source_id=source.source_id,
        source_kind=source.source_kind,
        provenance_class=grant.provenance_class,
        grant_id=grant.grant_id,
        purpose=request.purpose,
        requested_authority=request.requested_authority,
        admission_ceiling=grant.authority_ceiling,
        policy_id=policy_decision.policy_id,
        policy_version=policy_decision.policy_version,
        policy_ceiling=policy_ceiling,
        effective_authority=effective,
        requested_promotion=requested_promotion,
        policy_attempted_promotion=policy_attempted_promotion,
        reason=policy_decision.reason or "admitted",
    )
