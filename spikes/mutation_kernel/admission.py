"""Generic authority/admission contracts for the mutation-kernel research spike.

An assertion producer may *request* any authority label, so that label cannot itself be trusted. The
admission boundary therefore combines two independent ceilings:

* a core-issued ``SourceAuthorityGrant`` bound to the exact evidence source and admission mechanism;
* an extension-owned ``ExtensionAdmission`` policy decision for the domain/assertion type.

The extension may reject evidence or lower authority. It cannot raise authority above the trusted
source grant because the generic engine computes the weakest of requested, ingress, and policy
ceilings.

Accepted assertions are sealed under the admission-policy version. The source key stays unchanged,
but the durable assertion interpretation ID includes ``admission:<policy>@<version>`` so a later
policy version cannot silently collide with the earlier immutable assertion envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from typing import Protocol, Sequence

from .kernel import (
    AUTHORITY_ORDER,
    Assertion,
    EvidenceRef,
    authority_rank,
    build_assertion_id,
)


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash_parts(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _require_authority(value: str, *, field: str) -> str:
    authority = str(value).strip()
    if authority not in AUTHORITY_ORDER:
        raise ValueError(f"{field} must be one of {AUTHORITY_ORDER!r}")
    return authority


def weakest_authority(authorities: Sequence[str]) -> str:
    values = tuple(_require_authority(value, field="authority") for value in authorities)
    if not values:
        raise ValueError("weakest_authority requires at least one authority")
    return min(values, key=authority_rank)


@dataclass(frozen=True)
class SourceAuthorityGrant:
    """Core-issued capability for one evidence source/admission mechanism.

    ``provenance_class`` is trusted mechanism metadata such as ``model_inference``,
    ``grounded_user_statement``, ``trusted_record``, or ``operator_manual``. A model/extractor does
    not get to mint this field merely by emitting a source_kind string.
    """

    grant_id: str
    source_id: str
    verified_source_kind: str
    provenance_class: str
    authority_ceiling: str
    issuer: str = "core"

    def __post_init__(self) -> None:
        for name in (
            "grant_id",
            "source_id",
            "verified_source_kind",
            "provenance_class",
            "issuer",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"SourceAuthorityGrant.{name} must be non-empty")
        _require_authority(self.authority_ceiling, field="authority_ceiling")

    @property
    def evidence_key(self) -> tuple[str, str]:
        return (self.source_id, self.verified_source_kind)


@dataclass(frozen=True)
class ExtensionAdmission:
    """Domain policy response. The policy can reject or lower authority, never raise core's grant."""

    admitted: bool
    authority_ceiling: str
    reason: str

    def __post_init__(self) -> None:
        _require_authority(self.authority_ceiling, field="policy authority_ceiling")
        if not self.reason.strip():
            raise ValueError("ExtensionAdmission.reason must be non-empty")


class AdmissionPolicy(Protocol):
    policy_id: str
    version: int

    def evaluate(
        self,
        assertion: Assertion,
        grants: tuple[SourceAuthorityGrant, ...],
    ) -> ExtensionAdmission:
        ...


@dataclass(frozen=True)
class AdmissionDecision:
    decision_id: str
    status: str
    reason: str
    proposal_assertion_id: str
    source_key: str
    policy_id: str
    policy_version: int
    requested_authority: str
    ingress_ceiling: str | None
    policy_ceiling: str | None
    effective_authority: str | None
    attempted_promotion: bool
    grant_ids: tuple[str, ...]
    admitted_assertion: Assertion | None = None

    def __post_init__(self) -> None:
        if self.status not in {"admitted", "rejected"}:
            raise ValueError("AdmissionDecision.status must be admitted or rejected")
        for name in (
            "decision_id",
            "reason",
            "proposal_assertion_id",
            "source_key",
            "policy_id",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"AdmissionDecision.{name} must be non-empty")
        if self.policy_version < 1:
            raise ValueError("policy_version must be >= 1")
        _require_authority(self.requested_authority, field="requested_authority")
        for field, value in (
            ("ingress_ceiling", self.ingress_ceiling),
            ("policy_ceiling", self.policy_ceiling),
            ("effective_authority", self.effective_authority),
        ):
            if value is not None:
                _require_authority(value, field=field)
        if self.status == "admitted":
            if self.admitted_assertion is None or self.effective_authority is None:
                raise ValueError("admitted decision requires an admitted assertion and authority")
            if self.admitted_assertion.authority != self.effective_authority:
                raise ValueError("admitted assertion authority must equal decision authority")
        elif self.admitted_assertion is not None:
            raise ValueError("rejected decision cannot contain an admitted assertion")


class AdmissionEngine:
    """Generic enforcement: validate grants, call extension policy, then clamp/seal authority."""

    def _decision_id(
        self,
        *,
        assertion: Assertion,
        policy: AdmissionPolicy,
        grants: Sequence[SourceAuthorityGrant],
        status: str,
        reason: str,
        ingress_ceiling: str | None,
        policy_ceiling: str | None,
        effective_authority: str | None,
    ) -> str:
        return _hash_parts(
            assertion.assertion_id,
            policy.policy_id,
            str(policy.version),
            status,
            reason,
            ingress_ceiling or "",
            policy_ceiling or "",
            effective_authority or "",
            _stable_json(
                [
                    {
                        "grant_id": grant.grant_id,
                        "source_id": grant.source_id,
                        "source_kind": grant.verified_source_kind,
                        "provenance_class": grant.provenance_class,
                        "ceiling": grant.authority_ceiling,
                    }
                    for grant in sorted(grants, key=lambda row: row.grant_id)
                ]
            ),
        )

    def _reject(
        self,
        assertion: Assertion,
        policy: AdmissionPolicy,
        grants: Sequence[SourceAuthorityGrant],
        *,
        reason: str,
        ingress_ceiling: str | None = None,
        policy_ceiling: str | None = None,
    ) -> AdmissionDecision:
        requested = _require_authority(assertion.authority, field="assertion requested authority")
        decision_id = self._decision_id(
            assertion=assertion,
            policy=policy,
            grants=grants,
            status="rejected",
            reason=reason,
            ingress_ceiling=ingress_ceiling,
            policy_ceiling=policy_ceiling,
            effective_authority=None,
        )
        return AdmissionDecision(
            decision_id=decision_id,
            status="rejected",
            reason=reason,
            proposal_assertion_id=assertion.assertion_id,
            source_key=assertion.source_key,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            requested_authority=requested,
            ingress_ceiling=ingress_ceiling,
            policy_ceiling=policy_ceiling,
            effective_authority=None,
            attempted_promotion=False,
            grant_ids=tuple(sorted(grant.grant_id for grant in grants)),
        )

    def admit(
        self,
        assertion: Assertion,
        *,
        grants: Sequence[SourceAuthorityGrant],
        policy: AdmissionPolicy,
    ) -> AdmissionDecision:
        if not str(policy.policy_id).strip() or int(policy.version) < 1:
            raise ValueError("admission policy requires non-empty policy_id and version >= 1")
        requested = _require_authority(assertion.authority, field="assertion requested authority")
        grants = tuple(grants)
        if not assertion.evidence:
            return self._reject(assertion, policy, grants, reason="missing_evidence")

        grant_by_key: dict[tuple[str, str], SourceAuthorityGrant] = {}
        for grant in grants:
            if grant.evidence_key in grant_by_key:
                return self._reject(
                    assertion,
                    policy,
                    grants,
                    reason="duplicate_source_authority_grant",
                )
            grant_by_key[grant.evidence_key] = grant

        evidence_keys = {(row.source_id, row.source_kind) for row in assertion.evidence}
        grant_keys = set(grant_by_key)
        if evidence_keys != grant_keys:
            return self._reject(
                assertion,
                policy,
                grants,
                reason="evidence_grant_mismatch",
            )

        # Grants are now source-bound. Extra spans/notes on the same evidence source do not mint
        # additional authority; multiple distinct sources contribute the weakest ingress ceiling.
        ingress_ceiling = weakest_authority(
            tuple(grant.authority_ceiling for grant in grant_by_key.values())
        )
        ordered_grants = tuple(
            grant_by_key[key] for key in sorted(grant_by_key)
        )
        extension = policy.evaluate(assertion, ordered_grants)
        if not extension.admitted:
            return self._reject(
                assertion,
                policy,
                grants,
                reason=extension.reason,
                ingress_ceiling=ingress_ceiling,
                policy_ceiling=extension.authority_ceiling,
            )

        policy_ceiling = _require_authority(
            extension.authority_ceiling,
            field="extension policy ceiling",
        )
        effective = weakest_authority((requested, ingress_ceiling, policy_ceiling))
        attempted_promotion = authority_rank(requested) > authority_rank(effective)

        sealed_interpreter = (
            f"{assertion.interpreter_version}|admission:{policy.policy_id}@{policy.version}"
        )
        sealed_id = build_assertion_id(
            source_key=assertion.source_key,
            assertion_type=assertion.assertion_type,
            subject_id=assertion.subject_id,
            scope=assertion.scope,
            key=assertion.key,
            value=assertion.value,
            interpreter_version=sealed_interpreter,
            dimensions=assertion.dimensions,
        )
        admission_metadata = tuple(
            sorted(
                (
                    *assertion.metadata,
                    ("admission.policy_id", policy.policy_id),
                    ("admission.policy_version", str(policy.version)),
                    ("admission.requested_authority", requested),
                    ("admission.ingress_ceiling", ingress_ceiling),
                    ("admission.policy_ceiling", policy_ceiling),
                    ("admission.attempted_promotion", str(attempted_promotion).lower()),
                )
            )
        )
        admitted = replace(
            assertion,
            assertion_id=sealed_id,
            interpreter_version=sealed_interpreter,
            authority=effective,
            metadata=admission_metadata,
        )
        decision_id = self._decision_id(
            assertion=assertion,
            policy=policy,
            grants=grants,
            status="admitted",
            reason=extension.reason,
            ingress_ceiling=ingress_ceiling,
            policy_ceiling=policy_ceiling,
            effective_authority=effective,
        )
        return AdmissionDecision(
            decision_id=decision_id,
            status="admitted",
            reason=extension.reason,
            proposal_assertion_id=assertion.assertion_id,
            source_key=assertion.source_key,
            policy_id=policy.policy_id,
            policy_version=policy.version,
            requested_authority=requested,
            ingress_ceiling=ingress_ceiling,
            policy_ceiling=policy_ceiling,
            effective_authority=effective,
            attempted_promotion=attempted_promotion,
            grant_ids=tuple(sorted(grant.grant_id for grant in grants)),
            admitted_assertion=admitted,
        )


def make_grant(
    *,
    grant_id: str,
    evidence: EvidenceRef,
    provenance_class: str,
    authority_ceiling: str,
    issuer: str = "core",
) -> SourceAuthorityGrant:
    """Convenience constructor binding a trusted authority grant to one exact evidence source."""
    return SourceAuthorityGrant(
        grant_id=grant_id,
        source_id=evidence.source_id,
        verified_source_kind=evidence.source_kind,
        provenance_class=provenance_class,
        authority_ceiling=authority_ceiling,
        issuer=issuer,
    )
