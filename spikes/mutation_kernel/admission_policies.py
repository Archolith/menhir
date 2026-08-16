"""Reference extension admission policies for personality and investigation.

These are deliberately small policy examples, not final trust science. They exist to prove the
boundary: extensions decide whether evidence is usable for their domain and may lower authority, but
the generic admission engine independently enforces the core-issued source ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .admission import ExtensionAdmission, SourceAuthorityGrant, weakest_authority
from .investigation import OWNERSHIP_ASSERTION
from .kernel import Assertion
from .personality import POLICY_ASSERTION, TRAIT_ASSERTION, VALUE_ASSERTION


PERSONALITY_EXPLICIT_PREFERENCE = "personality.explicit_preference"
INVESTIGATION_SOURCE_CLAIM = "investigation.source_claim"


@dataclass(frozen=True)
class PersonalityAdmissionPolicy:
    policy_id: str = "personality.admission"
    version: int = 1

    def evaluate(
        self,
        assertion: Assertion,
        grants: tuple[SourceAuthorityGrant, ...],
    ) -> ExtensionAdmission:
        if assertion.assertion_type == PERSONALITY_EXPLICIT_PREFERENCE:
            allowed = {"grounded_user_statement", "operator_manual"}
            if not all(grant.provenance_class in allowed for grant in grants):
                return ExtensionAdmission(
                    admitted=False,
                    authority_ceiling="agent",
                    reason="explicit_preference_requires_verified_explicit_source",
                )
            # The extension permits a strong explicit preference, but core's ingress grant still
            # decides whether this attempt can actually reach manual or user authority.
            return ExtensionAdmission(
                admitted=True,
                authority_ceiling="user",
                reason="verified_explicit_preference",
            )

        if assertion.assertion_type in {
            TRAIT_ASSERTION,
            VALUE_ASSERTION,
            POLICY_ASSERTION,
        }:
            # Personality traits/learned behavior are interpretive even when grounded in a user
            # incident. The reference policy intentionally caps them at agent authority.
            return ExtensionAdmission(
                admitted=True,
                authority_ceiling="agent",
                reason="interpretive_personality_evidence",
            )

        return ExtensionAdmission(
            admitted=False,
            authority_ceiling="agent",
            reason="unsupported_personality_assertion_type",
        )


@dataclass(frozen=True)
class InvestigationAdmissionPolicy:
    policy_id: str = "investigation.admission"
    version: int = 1

    _SOURCE_CEILINGS: ClassVar[dict[str, str]] = {
        "deed": "trusted_tool",
        "official_filing": "trusted_tool",
        "court_record": "trusted_tool",
        "anonymous_tip": "agent",
        "user_statement": "agent",
        "investigator_note": "manual",
    }

    def evaluate(
        self,
        assertion: Assertion,
        grants: tuple[SourceAuthorityGrant, ...],
    ) -> ExtensionAdmission:
        if assertion.assertion_type not in {OWNERSHIP_ASSERTION, INVESTIGATION_SOURCE_CLAIM}:
            return ExtensionAdmission(
                admitted=False,
                authority_ceiling="agent",
                reason="unsupported_investigation_assertion_type",
            )

        ceilings: list[str] = []
        for grant in grants:
            ceiling = self._SOURCE_CEILINGS.get(grant.verified_source_kind)
            if ceiling is None:
                return ExtensionAdmission(
                    admitted=False,
                    authority_ceiling="agent",
                    reason="unsupported_investigation_source_kind",
                )
            if (
                grant.verified_source_kind == "investigator_note"
                and grant.provenance_class != "operator_manual"
            ):
                ceiling = "agent"
            ceilings.append(ceiling)

        policy_ceiling = weakest_authority(tuple(ceilings))
        return ExtensionAdmission(
            admitted=True,
            authority_ceiling=policy_ceiling,
            reason="investigation_source_policy",
        )
