"""Source-bound admission authority contracts.

The core rule is deliberately narrow: an untrusted claim may request authority, but it
cannot mint authority. Trusted source grants cap that request for the complete set of
durable source records behind the claim, and a domain policy may reject the claim or
lower the ceiling further.

This module intentionally defines no global authority hierarchy. Callers supply trusted
:class:`AuthoritySemantics` from the registered domain/deployment contract, so typed
scalars can keep their existing total order while other domains use different labels or
richer comparison rules. The comparator is governance configuration, not claim payload
and not a per-decision policy response.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence

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
    "weakest_authority",
]


class AuthorityRelation(str, Enum):
    """Relationship of ``left`` authority to ``right`` authority."""

    WEAKER = "weaker"
    EQUAL = "equal"
    STRONGER = "stronger"
    INCOMPARABLE = "incomparable"


class AuthoritySemantics(Protocol):
    """Trusted comparison semantics for otherwise opaque authority labels."""

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


@dataclass(frozen=True, order=True)
class SourceProvenance:
    """Core-observed identity of one durable source record behind a claim."""

    source_id: str
    source_kind: str

    def __post_init__(self) -> None:
        _require_token("source_id", self.source_id)
        _require_token("source_kind", self.source_kind)

    @property
    def source_key(self) -> tuple[str, str]:
        return (self.source_id, self.source_kind)


@dataclass(frozen=True)
class SourceAuthorityGrant:
    """Trusted maximum authority for one exact source record.

    Grant issuance is deliberately outside this module. The admission engine treats a
    supplied grant as trusted governance input and verifies only that the complete grant
    set is bound exactly to the complete source-provenance set for the claim.
    """

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

    @property
    def source_key(self) -> tuple[str, str]:
        return (self.source_id, self.source_kind)


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

    ``authority_ceiling`` is optional. When present it may only constrain the trusted
    ingress ceiling; an attempted promotion is recorded and ignored.
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
        sources: tuple[SourceProvenance, ...],
        grants: tuple[SourceAuthorityGrant, ...],
        ingress_ceiling: str,
    ) -> AdmissionPolicyDecision:
        """Return the domain policy decision for one fully source-bound claim."""


class AdmissionStatus(str, Enum):
    ADMITTED = "admitted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class AdmissionDecision:
    """Immutable receipt of the authority boundary applied to one claim."""

    status: AdmissionStatus
    sources: tuple[SourceProvenance, ...]
    grants: tuple[SourceAuthorityGrant, ...]
    purpose: str
    requested_authority: str
    ingress_ceiling: str | None
    policy_id: str | None
    policy_version: str | None
    policy_ceiling: str | None
    effective_authority: str | None
    requested_promotion: bool
    policy_attempted_promotion: bool
    reason: str

    def __post_init__(self) -> None:
        if self.status is AdmissionStatus.ADMITTED:
            if not self.sources or not self.grants or self.ingress_ceiling is None:
                raise ValueError("admitted decision requires sources, grants, and ingress ceiling")
            if self.effective_authority is None:
                raise ValueError("admitted decision requires effective authority")
        elif self.effective_authority is not None:
            raise ValueError("rejected decision cannot carry effective authority")

    @property
    def admitted(self) -> bool:
        return self.status is AdmissionStatus.ADMITTED

    @property
    def grant_ids(self) -> tuple[str, ...]:
        return tuple(grant.grant_id for grant in self.grants)



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



def weakest_authority(
    authorities: Sequence[str],
    *,
    semantics: AuthoritySemantics,
) -> str:
    """Return the weakest authority when every encountered label is comparable.

    Raises ``ValueError`` for an empty sequence or an incomparable pair. The caller may
    convert that failure into an explicit rejection receipt at a governance boundary.
    """

    values = tuple(authorities)
    if not values:
        raise ValueError("weakest_authority requires at least one authority")
    for value in values:
        _require_token("authority", value)

    weakest = values[0]
    for value in values[1:]:
        relation = _relation(semantics, value, weakest)
        if relation is AuthorityRelation.INCOMPARABLE:
            raise ValueError(f"authority labels are incomparable: {value!r} and {weakest!r}")
        if relation is AuthorityRelation.WEAKER:
            weakest = value
    return weakest



def _ordered_sources(
    sources: Sequence[SourceProvenance],
) -> tuple[SourceProvenance, ...]:
    return tuple(sorted(tuple(sources), key=lambda source: source.source_key))



def _ordered_grants(
    grants: Sequence[SourceAuthorityGrant],
) -> tuple[SourceAuthorityGrant, ...]:
    return tuple(
        sorted(tuple(grants), key=lambda grant: (grant.source_key, grant.grant_id))
    )



def _rejected(
    *,
    sources: Sequence[SourceProvenance],
    grants: Sequence[SourceAuthorityGrant],
    request: AdmissionRequest,
    policy: AdmissionPolicyDecision | None,
    ingress_ceiling: str | None = None,
    requested_promotion: bool = False,
    policy_attempted_promotion: bool = False,
    reason: str,
) -> AdmissionDecision:
    return AdmissionDecision(
        status=AdmissionStatus.REJECTED,
        sources=_ordered_sources(sources),
        grants=_ordered_grants(grants),
        purpose=request.purpose,
        requested_authority=request.requested_authority,
        ingress_ceiling=ingress_ceiling,
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
    sources: Sequence[SourceProvenance],
    grants: Sequence[SourceAuthorityGrant],
    policy: AdmissionPolicy,
    semantics: AuthoritySemantics,
) -> AdmissionDecision:
    """Apply complete source grants and domain policy without trusting requested authority.

    Source and grant inputs are treated as sets keyed by ``(source_id, source_kind)``.
    Admission requires a non-empty source set, no duplicate source/grant bindings, unique
    grant IDs, and exact equality between source keys and grant keys. The ingress ceiling
    is the weakest ceiling across every distinct source. Missing, extra, duplicate, or
    incomparable grants fail closed before domain policy executes.
    """

    sources = tuple(sources)
    grants = tuple(grants)
    if not sources:
        return _rejected(
            sources=sources,
            grants=grants,
            request=request,
            policy=None,
            reason="missing source provenance",
        )

    source_keys = tuple(source.source_key for source in sources)
    if len(set(source_keys)) != len(source_keys):
        return _rejected(
            sources=sources,
            grants=grants,
            request=request,
            policy=None,
            reason="duplicate source provenance",
        )

    grant_keys = tuple(grant.source_key for grant in grants)
    if len(set(grant_keys)) != len(grant_keys):
        return _rejected(
            sources=sources,
            grants=grants,
            request=request,
            policy=None,
            reason="duplicate source authority grant",
        )

    grant_ids = tuple(grant.grant_id for grant in grants)
    if len(set(grant_ids)) != len(grant_ids):
        return _rejected(
            sources=sources,
            grants=grants,
            request=request,
            policy=None,
            reason="duplicate source authority grant id",
        )

    if set(source_keys) != set(grant_keys):
        return _rejected(
            sources=sources,
            grants=grants,
            request=request,
            policy=None,
            reason="source provenance and authority grant sets do not match",
        )

    grant_by_key = {grant.source_key: grant for grant in grants}
    ordered_sources = _ordered_sources(sources)
    ordered_grants = tuple(grant_by_key[source.source_key] for source in ordered_sources)

    try:
        ingress_ceiling = weakest_authority(
            tuple(grant.authority_ceiling for grant in ordered_grants),
            semantics=semantics,
        )
    except ValueError:
        return _rejected(
            sources=ordered_sources,
            grants=ordered_grants,
            request=request,
            policy=None,
            reason="source authority ceilings are incomparable",
        )

    request_relation = _relation(
        semantics,
        request.requested_authority,
        ingress_ceiling,
    )
    if request_relation is AuthorityRelation.INCOMPARABLE:
        return _rejected(
            sources=ordered_sources,
            grants=ordered_grants,
            request=request,
            policy=None,
            ingress_ceiling=ingress_ceiling,
            reason="requested authority is incomparable with ingress ceiling",
        )

    requested_promotion = request_relation is AuthorityRelation.STRONGER
    effective = (
        ingress_ceiling
        if requested_promotion
        else request.requested_authority
    )

    policy_decision = policy.evaluate(
        request=request,
        sources=ordered_sources,
        grants=ordered_grants,
        ingress_ceiling=ingress_ceiling,
    )
    if not isinstance(policy_decision, AdmissionPolicyDecision):
        raise TypeError("admission policy must return AdmissionPolicyDecision")

    if not policy_decision.accepted:
        return _rejected(
            sources=ordered_sources,
            grants=ordered_grants,
            request=request,
            policy=policy_decision,
            ingress_ceiling=ingress_ceiling,
            requested_promotion=requested_promotion,
            reason=policy_decision.reason or "domain policy rejected admission",
        )

    policy_attempted_promotion = False
    policy_ceiling = policy_decision.authority_ceiling
    if policy_ceiling is not None:
        policy_vs_ingress = _relation(
            semantics,
            policy_ceiling,
            ingress_ceiling,
        )
        if policy_vs_ingress is AuthorityRelation.INCOMPARABLE:
            return _rejected(
                sources=ordered_sources,
                grants=ordered_grants,
                request=request,
                policy=policy_decision,
                ingress_ceiling=ingress_ceiling,
                requested_promotion=requested_promotion,
                reason="policy ceiling is incomparable with ingress ceiling",
            )

        policy_attempted_promotion = (
            policy_vs_ingress is AuthorityRelation.STRONGER
        )
        if not policy_attempted_promotion:
            effective_vs_policy = _relation(
                semantics,
                effective,
                policy_ceiling,
            )
            if effective_vs_policy is AuthorityRelation.INCOMPARABLE:
                return _rejected(
                    sources=ordered_sources,
                    grants=ordered_grants,
                    request=request,
                    policy=policy_decision,
                    ingress_ceiling=ingress_ceiling,
                    requested_promotion=requested_promotion,
                    reason="effective authority is incomparable with policy ceiling",
                )
            if effective_vs_policy is AuthorityRelation.STRONGER:
                effective = policy_ceiling

    return AdmissionDecision(
        status=AdmissionStatus.ADMITTED,
        sources=ordered_sources,
        grants=ordered_grants,
        purpose=request.purpose,
        requested_authority=request.requested_authority,
        ingress_ceiling=ingress_ceiling,
        policy_id=policy_decision.policy_id,
        policy_version=policy_decision.policy_version,
        policy_ceiling=policy_ceiling,
        effective_authority=effective,
        requested_promotion=requested_promotion,
        policy_attempted_promotion=policy_attempted_promotion,
        reason=policy_decision.reason or "admitted",
    )
