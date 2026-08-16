"""Reference purpose-sensitive trust resolvers for the mutation-kernel spike.

The same admitted source can legitimately support different trust outcomes for different questions.
These resolvers are deliberately small and categorical. They test the extension boundary rather than
claiming to encode final evidentiary science.
"""

from __future__ import annotations

from dataclasses import dataclass

from .trust import TrustProfile, TrustResolutionProposal


@dataclass(frozen=True)
class LegacyAuthorityResolver:
    """Compatibility path: preserve the admission/legacy total authority for every purpose."""

    resolver_id: str = "legacy.authority"
    version: int = 1

    def resolve(self, profile: TrustProfile, purpose: str) -> TrustResolutionProposal:
        return TrustResolutionProposal(
            authority=profile.admission_authority,
            reason="legacy_total_order",
            used_facets=("core.admission_authority",),
        )


@dataclass(frozen=True)
class InvestigationPurposeTrustResolver:
    """Interpret trusted source facets according to the investigative question being answered."""

    resolver_id: str = "investigation.purpose_trust"
    version: int = 1

    def resolve(self, profile: TrustProfile, purpose: str) -> TrustResolutionProposal:
        source_kinds = set(profile.source_kinds)
        provenance = set(profile.provenance_classes)
        used = ("core.provenance_classes", "core.source_kinds")

        if purpose == "recorded_title":
            if source_kinds & {"deed", "court_record", "official_filing"}:
                return TrustResolutionProposal(
                    authority="trusted_tool",
                    reason="official_record_directly_addresses_recorded_title",
                    used_facets=used,
                )
            if "investigator_note" in source_kinds and "operator_manual" in provenance:
                return TrustResolutionProposal(
                    authority="manual",
                    reason="operator_endorsed_recorded_title_conclusion",
                    used_facets=used,
                )
            return TrustResolutionProposal(
                authority="agent",
                reason="source_does_not_directly_establish_recorded_title",
                used_facets=used,
            )

        if purpose == "beneficial_owner":
            # A deed strongly establishes recorded title but can be indirect evidence of ultimate
            # beneficial ownership. Manual investigative synthesis can be stronger for this purpose.
            if "investigator_note" in source_kinds and "operator_manual" in provenance:
                return TrustResolutionProposal(
                    authority="manual",
                    reason="operator_endorsed_beneficial_owner_synthesis",
                    used_facets=used,
                )
            return TrustResolutionProposal(
                authority="agent",
                reason="source_is_indirect_for_beneficial_ownership",
                used_facets=used,
            )

        if purpose == "external_fact":
            if source_kinds & {"deed", "court_record", "official_filing"}:
                return TrustResolutionProposal(
                    authority="trusted_tool",
                    reason="verified_official_record_for_external_fact",
                    used_facets=used,
                )
            return TrustResolutionProposal(
                authority="agent",
                reason="statement_is_not_authoritative_for_external_fact",
                used_facets=used,
            )

        if purpose == "lead_priority":
            # This purpose is intentionally not truth authority. A weak source can still be a useful
            # lead, but the legacy authority result remains conservative because the generic engine
            # will also clamp to the admission ceiling.
            if "anonymous_tip" in source_kinds:
                return TrustResolutionProposal(
                    authority="trusted_tool",
                    reason="anonymous_tip_may_be_actionable_as_a_lead_not_as_truth",
                    used_facets=used,
                )
            return TrustResolutionProposal(
                authority=profile.admission_authority,
                reason="use_admission_authority_for_lead_priority",
                used_facets=("core.admission_authority", "core.source_kinds"),
            )

        return TrustResolutionProposal(
            authority="agent",
            reason="unknown_investigation_trust_purpose",
            used_facets=used,
        )


@dataclass(frozen=True)
class PersonalityPurposeTrustResolver:
    resolver_id: str = "personality.purpose_trust"
    version: int = 1

    def resolve(self, profile: TrustProfile, purpose: str) -> TrustResolutionProposal:
        provenance = set(profile.provenance_classes)
        source_kinds = set(profile.source_kinds)
        used = ("core.provenance_classes", "core.source_kinds")

        if purpose == "explicit_preference":
            if "grounded_user_statement" in provenance:
                return TrustResolutionProposal(
                    authority="user",
                    reason="authenticated_user_statement_about_own_preference",
                    used_facets=used,
                )
            if "operator_manual" in provenance:
                return TrustResolutionProposal(
                    authority="manual",
                    reason="operator_pinned_personality_configuration",
                    used_facets=used,
                )
            return TrustResolutionProposal(
                authority="agent",
                reason="preference_is_inferred_not_explicit",
                used_facets=used,
            )

        if purpose in {"inferred_trait", "learned_behavior"}:
            return TrustResolutionProposal(
                authority="agent",
                reason="personality_interpretation_remains_inferential",
                used_facets=used,
            )

        if purpose == "external_fact":
            # Authenticity as the user's own statement does not make it authoritative about third
            # parties or external reality.
            return TrustResolutionProposal(
                authority="agent",
                reason="personal_statement_not_authoritative_for_external_fact",
                used_facets=used,
            )

        return TrustResolutionProposal(
            authority="agent",
            reason="unknown_personality_trust_purpose",
            used_facets=used,
        )
