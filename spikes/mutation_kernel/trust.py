"""Purpose-sensitive trust profiles for the mutation-kernel research spike.

Experiment 13 proved that authority must be granted at admission rather than trusted from the
Assertion producer. This experiment asks whether one global total authority order is expressive
enough after admission.

``TrustProfile`` keeps a small protected core namespace plus opaque extension facets. Core does not
rank extension facets. An extension-owned resolver interprets the profile for a named purpose, while
the generic ``PurposeTrustEngine`` clamps the requested resolution to the admission-time hard ceiling.

This intentionally avoids a universal numeric confidence model. A deed can be strong evidence of
recorded title and weak evidence of beneficial ownership without either use rewriting the source or
changing its admission receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Protocol, Sequence

from .admission import AdmissionDecision, SourceAuthorityGrant, weakest_authority
from .kernel import AUTHORITY_ORDER, authority_rank


CORE_PREFIX = "core."


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash_parts(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _require_authority(value: str, *, field: str) -> str:
    text = str(value).strip()
    if text not in AUTHORITY_ORDER:
        raise ValueError(f"{field} must be one of {AUTHORITY_ORDER!r}")
    return text


def _canonical_facets(
    facets: Sequence[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    normalized = tuple(sorted((str(name).strip(), str(value)) for name, value in facets))
    names = [name for name, _ in normalized]
    if any(not name for name in names):
        raise ValueError("trust facet names must be non-empty")
    if len(names) != len(set(names)):
        raise ValueError("trust facet names must be unique")
    return normalized


@dataclass(frozen=True)
class TrustProfile:
    """Immutable governance sidecar for one admitted Assertion interpretation."""

    profile_id: str
    assertion_id: str
    source_key: str
    profile_version: int
    facets: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        for name in ("profile_id", "assertion_id", "source_key"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"TrustProfile.{name} must be non-empty")
        if self.profile_version < 1:
            raise ValueError("TrustProfile.profile_version must be >= 1")
        if self.facets != _canonical_facets(self.facets):
            raise ValueError("TrustProfile.facets must be canonical")
        required = {
            "core.admission_authority",
            "core.policy_id",
            "core.policy_version",
            "core.provenance_classes",
            "core.source_kinds",
        }
        if not required.issubset({name for name, _ in self.facets}):
            raise ValueError("TrustProfile is missing required protected core facets")
        _require_authority(
            self.facet("core.admission_authority"),
            field="core.admission_authority",
        )

    def facet(self, name: str, default: str | None = None) -> str:
        for facet_name, value in self.facets:
            if facet_name == name:
                return value
        if default is not None:
            return default
        raise KeyError(name)

    @property
    def admission_authority(self) -> str:
        return self.facet("core.admission_authority")

    @property
    def provenance_classes(self) -> tuple[str, ...]:
        return tuple(json.loads(self.facet("core.provenance_classes")))

    @property
    def source_kinds(self) -> tuple[str, ...]:
        return tuple(json.loads(self.facet("core.source_kinds")))


@dataclass(frozen=True)
class TrustResolutionProposal:
    """Extension resolver's purpose-specific interpretation before the core ceiling is enforced."""

    authority: str
    reason: str
    used_facets: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_authority(self.authority, field="TrustResolutionProposal.authority")
        if not self.reason.strip():
            raise ValueError("TrustResolutionProposal.reason must be non-empty")
        canonical = tuple(sorted(str(value).strip() for value in self.used_facets))
        if any(not value for value in canonical) or len(canonical) != len(set(canonical)):
            raise ValueError("used_facets must be unique non-empty names")
        if self.used_facets != canonical:
            raise ValueError("used_facets must be canonical")


@dataclass(frozen=True)
class TrustResolution:
    purpose: str
    requested_authority: str
    effective_authority: str
    admission_ceiling: str
    clamped: bool
    reason: str
    used_facets: tuple[str, ...]
    profile_id: str

    def __post_init__(self) -> None:
        if not self.purpose.strip() or not self.reason.strip() or not self.profile_id.strip():
            raise ValueError("TrustResolution purpose/reason/profile_id must be non-empty")
        for field, value in (
            ("requested_authority", self.requested_authority),
            ("effective_authority", self.effective_authority),
            ("admission_ceiling", self.admission_ceiling),
        ):
            _require_authority(value, field=field)


class PurposeTrustResolver(Protocol):
    resolver_id: str
    version: int

    def resolve(self, profile: TrustProfile, purpose: str) -> TrustResolutionProposal:
        ...


class PurposeTrustEngine:
    """Generic core mechanism: extension interprets; admission-time ceiling remains authoritative."""

    def resolve(
        self,
        profile: TrustProfile,
        *,
        purpose: str,
        resolver: PurposeTrustResolver,
    ) -> TrustResolution:
        if not purpose.strip():
            raise ValueError("purpose must be non-empty")
        if not str(resolver.resolver_id).strip() or int(resolver.version) < 1:
            raise ValueError("trust resolver requires non-empty resolver_id and version >= 1")
        proposal = resolver.resolve(profile, purpose)
        ceiling = profile.admission_authority
        effective = weakest_authority((proposal.authority, ceiling))
        return TrustResolution(
            purpose=purpose,
            requested_authority=proposal.authority,
            effective_authority=effective,
            admission_ceiling=ceiling,
            clamped=authority_rank(proposal.authority) > authority_rank(effective),
            reason=proposal.reason,
            used_facets=proposal.used_facets,
            profile_id=profile.profile_id,
        )


def build_trust_profile(
    decision: AdmissionDecision,
    *,
    grants: Sequence[SourceAuthorityGrant],
    extension_facets: Sequence[tuple[str, str]] = (),
    profile_version: int = 1,
) -> TrustProfile:
    """Create one immutable profile from an admitted decision plus trusted source grants."""
    if decision.status != "admitted" or decision.admitted_assertion is None:
        raise ValueError("trust profiles require an admitted Assertion")
    if decision.effective_authority is None:
        raise ValueError("admitted decision is missing effective authority")
    grants = tuple(grants)
    if tuple(sorted(grant.grant_id for grant in grants)) != decision.grant_ids:
        raise ValueError("trust-profile grants do not match the admission decision")
    for name, _value in extension_facets:
        if str(name).strip().startswith(CORE_PREFIX):
            raise ValueError("extensions cannot write protected core trust facets")

    core_facets = (
        ("core.admission_authority", decision.effective_authority),
        ("core.attempted_promotion", str(decision.attempted_promotion).lower()),
        ("core.grant_ids", _stable_json(list(decision.grant_ids))),
        ("core.policy_id", decision.policy_id),
        ("core.policy_version", str(decision.policy_version)),
        (
            "core.provenance_classes",
            _stable_json(sorted(grant.provenance_class for grant in grants)),
        ),
        (
            "core.source_kinds",
            _stable_json(sorted(grant.verified_source_kind for grant in grants)),
        ),
    )
    facets = _canonical_facets((*core_facets, *extension_facets))
    profile_id = _hash_parts(
        decision.admitted_assertion.assertion_id,
        str(profile_version),
        _stable_json(facets),
    )
    return TrustProfile(
        profile_id=profile_id,
        assertion_id=decision.admitted_assertion.assertion_id,
        source_key=decision.source_key,
        profile_version=profile_version,
        facets=facets,
    )


def build_legacy_trust_profile(
    *,
    assertion_id: str,
    source_key: str,
    authority: str,
    source_kind: str = "legacy",
    profile_version: int = 1,
) -> TrustProfile:
    """Compatibility profile for domains that intentionally keep the legacy total authority order."""
    authority = _require_authority(authority, field="legacy authority")
    facets = _canonical_facets(
        (
            ("core.admission_authority", authority),
            ("core.attempted_promotion", "false"),
            ("core.grant_ids", "[]"),
            ("core.policy_id", "legacy.authority"),
            ("core.policy_version", "1"),
            ("core.provenance_classes", '["legacy"]'),
            ("core.source_kinds", _stable_json([source_kind])),
        )
    )
    return TrustProfile(
        profile_id=_hash_parts(assertion_id, str(profile_version), _stable_json(facets)),
        assertion_id=assertion_id,
        source_key=source_key,
        profile_version=profile_version,
        facets=facets,
    )
