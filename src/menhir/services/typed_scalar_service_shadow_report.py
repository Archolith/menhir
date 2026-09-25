"""Bounded, quote-free deterministic-shadow telemetry payloads, extracted from
`typed_scalar_service`. Names the service's tests monkeypatch on the facade module
(`compose_structural_scalar_identity` and the `_SHADOW_*` bounds) are imported from
`menhir.services.typed_scalar_service` inside the functions that read them, so patching the facade
keeps working exactly as before. Re-exported by `menhir.services.typed_scalar_service`.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from menhir.domain.scalar_identity import CompositionalScalarIdentity
from menhir.domain.typed_assertion import build_source_key, normalize_scalar
from menhir.services.deterministic_scalar_extractor import (
    OUTCOME_ADMITTED,
    OUTCOME_DROPPED,
    DeterministicExtraction,
)
from menhir.services.structural_scalar_composer import STRUCTURAL_COMPOSER_VERSION
from menhir.services.typed_scalar_rules import TypedScalarProposal
from menhir.services.typed_scalar_service_shadow import (
    _COMPOSITION_ERROR,
    _aligned_shadow_match,
    _exact_shadow_match,
    _identity_mismatch_dimensions,
    _matched_llm_indices,
    _matched_shadow_pairs,
    _verified_shadow_alignment,
)


@dataclass(frozen=True)
class _ShadowSidecar:
    identity: CompositionalScalarIdentity | None
    reason_code: str | None


def _compose_shadow_sidecars(
    proposals: list[TypedScalarProposal],
    source_by_episode: Mapping[str, str],
) -> list[_ShadowSidecar]:
    """Compose proposals independently; one failure never erases legacy raw metrics."""
    # Resolved through the facade at call time so patches of
    # typed_scalar_service.compose_structural_scalar_identity keep applying here.
    from menhir.services.typed_scalar_service import compose_structural_scalar_identity
    sidecars: list[_ShadowSidecar] = []
    for proposal in proposals:
        try:
            result = compose_structural_scalar_identity(
                proposal,
                source_by_episode.get(proposal.episode_uuid, ""),
                canonical_self=True,
            )
            sidecars.append(_ShadowSidecar(
                identity=result.identity,
                reason_code=result.receipt.reason_code,
            ))
        except Exception:
            sidecars.append(_ShadowSidecar(identity=None, reason_code=_COMPOSITION_ERROR))
    return sidecars


def _identifier_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _compositional_shadow_details(
    deterministic: list[TypedScalarProposal],
    llm_committed: list[TypedScalarProposal],
    *,
    eligible_uuids: set[str],
    source_by_episode: Mapping[str, str],
) -> dict[str, Any]:
    """Build bounded, quote-free compositional diagnostics; LLM comparison is not gold."""
    from menhir.services.typed_scalar_service import (
        _COMPOSITIONAL_SHADOW_SCHEMA_VERSION,
        _SHADOW_SOURCE_SUMMARY_LIMIT,
    )
    det_sidecars = _compose_shadow_sidecars(deterministic, source_by_episode)
    llm_sidecars = _compose_shadow_sidecars(llm_committed, source_by_episode)
    det_sidecar_by_id = {
        id(proposal): sidecar for proposal, sidecar in zip(deterministic, det_sidecars)
    }
    llm_sidecar_by_id = {
        id(proposal): sidecar for proposal, sidecar in zip(llm_committed, llm_sidecars)
    }

    def _same_semantics(det: TypedScalarProposal, llm: TypedScalarProposal) -> bool:
        det_identity = det_sidecar_by_id[id(det)].identity
        llm_identity = llm_sidecar_by_id[id(llm)].identity
        return bool(
            det_identity is not None
            and llm_identity is not None
            and det_identity.semantic_key == llm_identity.semantic_key
        )

    def _match_remaining(
        det_indices: list[int],
        llm_indices: list[int],
        predicate: Callable[[TypedScalarProposal, TypedScalarProposal], bool],
    ) -> tuple[tuple[int, int], ...]:
        local_pairs = _matched_shadow_pairs(
            [deterministic[index] for index in det_indices],
            [llm_committed[index] for index in llm_indices],
            predicate,
        )
        return tuple(
            (det_indices[det_index], llm_indices[llm_index])
            for det_index, llm_index in local_pairs
        )

    remaining_det = list(range(len(deterministic)))
    remaining_llm = list(range(len(llm_committed)))
    exact_pairs = _match_remaining(
        remaining_det,
        remaining_llm,
        lambda det, llm: det.source_key == llm.source_key and _same_semantics(det, llm),
    )
    exact_det = {det_index for det_index, _llm_index in exact_pairs}
    exact_llm = {llm_index for _det_index, llm_index in exact_pairs}
    remaining_det = [index for index in remaining_det if index not in exact_det]
    remaining_llm = [index for index in remaining_llm if index not in exact_llm]

    semantic_aligned_only = _match_remaining(
        remaining_det,
        remaining_llm,
        lambda det, llm: _verified_shadow_alignment(det, llm) and _same_semantics(det, llm),
    )
    semantic_det = {det_index for det_index, _llm_index in semantic_aligned_only}
    semantic_llm = {llm_index for _det_index, llm_index in semantic_aligned_only}
    remaining_det = [index for index in remaining_det if index not in semantic_det]
    remaining_llm = [index for index in remaining_llm if index not in semantic_llm]

    residual_pairs = _match_remaining(
        remaining_det, remaining_llm, _verified_shadow_alignment)
    semantic_pairs = (*exact_pairs, *semantic_aligned_only)
    aligned_pairs = tuple(sorted(
        (*semantic_pairs, *residual_pairs), key=lambda pair: pair[1]))

    rows: list[dict[str, Any]] = []
    unresolved_pairs = 0
    disagreements = 0
    for det_index, llm_index in aligned_pairs:
        det = deterministic[det_index]
        llm = llm_committed[llm_index]
        det_sidecar = det_sidecars[det_index]
        llm_sidecar = llm_sidecars[llm_index]
        det_identity = det_sidecar.identity
        llm_identity = llm_sidecar.identity
        mismatch_dimensions: tuple[str, ...] = ()
        if det_identity is None or llm_identity is None:
            status = "unresolved"
            unresolved_pairs += 1
        elif det_identity.semantic_key != llm_identity.semantic_key:
            status = "identity_disagreement"
            disagreements += 1
            mismatch_dimensions = _identity_mismatch_dimensions(det_identity, llm_identity)
        elif det.source_key == llm.source_key:
            status = "compositional_exact"
        else:
            status = "compositional_aligned"
        rows.append({
            "status": status,
            "det_source_hash": _identifier_hash(det.source_key),
            "llm_source_hash": _identifier_hash(llm.source_key),
            "det_semantic_hash": det_identity.semantic_key if det_identity else None,
            "llm_semantic_hash": llm_identity.semantic_key if llm_identity else None,
            "det_claim_hash": det_identity.claim_key if det_identity else None,
            "llm_claim_hash": llm_identity.claim_key if llm_identity else None,
            "det_relation": det_identity.relation_type if det_identity else None,
            "llm_relation": llm_identity.relation_type if llm_identity else None,
            "det_reason": det_sidecar.reason_code,
            "llm_reason": llm_sidecar.reason_code,
            "mismatch_dimensions": mismatch_dimensions,
        })

    aligned_det_indices = {det_index for det_index, _llm_index in aligned_pairs}
    aligned_llm_indices = {llm_index for _det_index, llm_index in aligned_pairs}
    diagnostic_llm_router_misses = sum(
        1
        for det_index, proposal in enumerate(deterministic)
        if proposal.episode_uuid in eligible_uuids and det_index not in aligned_det_indices
    )
    truncated = max(0, len(rows) - _SHADOW_SOURCE_SUMMARY_LIMIT)
    return {
        "schema_version": _COMPOSITIONAL_SHADOW_SCHEMA_VERSION,
        "composer_version": STRUCTURAL_COMPOSER_VERSION,
        "evaluation_status": "ok",
        "promotion_status": "not_evaluable",
        "deterministic_composed": sum(sidecar.identity is not None for sidecar in det_sidecars),
        "llm_composed": sum(sidecar.identity is not None for sidecar in llm_sidecars),
        "deterministic_unresolved": sum(sidecar.identity is None for sidecar in det_sidecars),
        "llm_unresolved": sum(sidecar.identity is None for sidecar in llm_sidecars),
        "deterministic_unresolved_reason_counts": dict(Counter(
            sidecar.reason_code for sidecar in det_sidecars if sidecar.identity is None)),
        "llm_unresolved_reason_counts": dict(Counter(
            sidecar.reason_code for sidecar in llm_sidecars if sidecar.identity is None)),
        "diagnostic_vs_llm": {
            "comparison_pairs": len(aligned_pairs),
            "compositional_exact_agreements": len(exact_pairs),
            "compositional_aligned_agreements": len(semantic_pairs),
            "compositional_unresolved_pairs": unresolved_pairs,
            "identity_disagreements": disagreements,
            "unjoinable_deterministic_claims": len(deterministic) - len(aligned_det_indices),
            "unjoinable_llm_claims": len(llm_committed) - len(aligned_llm_indices),
            "diagnostic_llm_router_misses": diagnostic_llm_router_misses,
        },
        "status_counts": dict(Counter(row["status"] for row in rows)),
        "pair_summaries": rows[:_SHADOW_SOURCE_SUMMARY_LIMIT],
        "pair_summaries_truncated": truncated,
    }


def _compositional_shadow_error_details() -> dict[str, Any]:
    """Schema-stable empty compositional section for a failed outer shadow pass."""
    from menhir.services.typed_scalar_service import _COMPOSITIONAL_SHADOW_SCHEMA_VERSION
    return {
        "schema_version": _COMPOSITIONAL_SHADOW_SCHEMA_VERSION,
        "composer_version": STRUCTURAL_COMPOSER_VERSION,
        "evaluation_status": "shadow_error",
        "promotion_status": "not_evaluable",
        "deterministic_composed": 0,
        "llm_composed": 0,
        "deterministic_unresolved": 0,
        "llm_unresolved": 0,
        "deterministic_unresolved_reason_counts": {},
        "llm_unresolved_reason_counts": {},
        "diagnostic_vs_llm": {
            "comparison_pairs": 0,
            "compositional_exact_agreements": 0,
            "compositional_aligned_agreements": 0,
            "compositional_unresolved_pairs": 0,
            "identity_disagreements": 0,
            "unjoinable_deterministic_claims": 0,
            "unjoinable_llm_claims": 0,
            "diagnostic_llm_router_misses": 0,
        },
        "status_counts": {},
        "pair_summaries": [],
        "pair_summaries_truncated": 0,
    }


def _compare_deterministic_shadow(
    deterministic: DeterministicExtraction,
    llm_committed: list[TypedScalarProposal],
    *,
    canonical_self: bool = False,
    source_by_episode: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compare deterministic proposals with committed LLM decisions without transcript text.

    Exact agreement uses the same source key and interpretation. Aligned agreement uses the same
    episode, verifiable common source span, and interpretation. Router misses are counted only for
    episodes the deterministic extractor marked fully eligible. Both agreement metrics are matched
    one-to-one so duplicate claims cannot inflate the counts.
    """
    from menhir.services.typed_scalar_service import (
        _SHADOW_CANDIDATE_SUMMARY_LIMIT,
        _SHADOW_EPISODE_SUMMARY_LIMIT,
        _SHADOW_SCHEMA_VERSION,
        _SHADOW_SOURCE_SUMMARY_LIMIT,
    )
    eligible_uuids = set(deterministic.fully_eligible_episode_uuids)
    det_proposals = list(deterministic.proposals)
    all_candidates = [
        (episode, candidate)
        for episode in deterministic.episode_receipts
        for candidate in episode.candidate_receipts
    ]

    exact_matched = _matched_llm_indices(
        det_proposals,
        llm_committed,
        lambda det, llm: _exact_shadow_match(det, llm, canonical_self=canonical_self),
    )
    aligned_matched = _matched_llm_indices(
        det_proposals,
        llm_committed,
        lambda det, llm: _aligned_shadow_match(det, llm, canonical_self=canonical_self),
    )
    compositional = _compositional_shadow_details(
        det_proposals,
        llm_committed,
        eligible_uuids=eligible_uuids,
        source_by_episode=source_by_episode or {},
    )
    router_missed = 0
    source_summaries: list[dict[str, Any]] = []
    for llm_index, llm in enumerate(llm_committed):
        is_exact = llm_index in exact_matched
        is_aligned = llm_index in aligned_matched
        if llm.episode_uuid in eligible_uuids and not is_aligned:
            router_missed += 1
        source_summaries.append({
            "source_key": llm.source_key,
            "episode_uuid": llm.episode_uuid,
            "span_start": llm.span_start,
            "span_end": llm.span_end,
            "attribute": llm.attribute,
            "scope": llm.scope,
            "value_kind": llm.value_kind,
            "unit": llm.unit,
            "operation": llm.operation,
            "normalized_value": llm.normalized_value,
            # Keep this payload aligned with _proposal_audit_summary: semantic fields and
            # offsets are joinable, but raw subject/quote text is not copied into telemetry.
            "exact_matched": is_exact,
            "aligned_matched": is_aligned,
        })

    episode_summaries = [
        {
            "episode_uuid": episode.episode_uuid,
            "fully_eligible": episode.episode_uuid in eligible_uuids,
            "reason_counts": dict(Counter(episode.reasons)),
            "admitted_count": sum(
                1 for candidate in episode.candidate_receipts
                if candidate.outcome == OUTCOME_ADMITTED),
            "dropped_count": sum(
                1 for candidate in episode.candidate_receipts
                if candidate.outcome == OUTCOME_DROPPED),
        }
        for episode in deterministic.episode_receipts
    ]

    def _candidate_summary(episode: Any, candidate: Any) -> dict[str, Any]:
        proposal = candidate.proposal if candidate.outcome == OUTCOME_ADMITTED else None
        if proposal is not None:
            identity = {
                "episode_uuid": proposal.episode_uuid,
                "source_key": proposal.source_key,
                "span_start": proposal.span_start,
                "span_end": proposal.span_end,
                "attribute": proposal.attribute,
                "scope": proposal.scope,
                "value_kind": proposal.value_kind,
                "unit": proposal.unit,
                "operation": proposal.operation,
                "normalized_value": proposal.normalized_value,
            }
        else:
            identity = {
                "episode_uuid": episode.episode_uuid,
                "source_key": build_source_key(
                    episode.episode_uuid, candidate.source_start, candidate.source_end, 0),
                "span_start": candidate.source_start,
                "span_end": candidate.source_end,
                "attribute": candidate.attribute,
                "scope": candidate.scope,
                "value_kind": candidate.value_kind,
                "unit": candidate.unit,
                "operation": candidate.operation,
                "normalized_value": (
                    normalize_scalar(candidate.value) if candidate.value is not None else None),
            }
        return {
            "template_id": candidate.template_id,
            "class_id": candidate.class_id,
            **identity,
            "outcome": candidate.outcome,
            "drop_reason": candidate.drop_reason,
        }

    candidate_summaries = [
        _candidate_summary(episode, candidate)
        for episode, candidate in all_candidates
    ]

    truncated_episodes = max(0, len(episode_summaries) - _SHADOW_EPISODE_SUMMARY_LIMIT)
    truncated_candidates = max(0, len(candidate_summaries) - _SHADOW_CANDIDATE_SUMMARY_LIMIT)
    truncated_sources = max(0, len(source_summaries) - _SHADOW_SOURCE_SUMMARY_LIMIT)

    return {
        "schema_version": _SHADOW_SCHEMA_VERSION,
        "extractor_version": deterministic.extractor_version,
        "template_version": deterministic.template_version,
        "episodes_total": len(deterministic.episode_receipts),
        "episodes_fully_eligible": len(eligible_uuids),
        "proposals_all": len(det_proposals),
        "proposals_router_eligible": sum(
            1 for proposal in det_proposals if proposal.episode_uuid in eligible_uuids),
        "committed_llm": len(llm_committed),
        "exact_agreements": len(exact_matched),
        "aligned_agreements": len(aligned_matched),
        "router_missed_llm_claims": router_missed,
        "deterministic_outcome_counts": dict(
            Counter(candidate.outcome for _episode, candidate in all_candidates)),
        "deterministic_drop_reason_counts": dict(Counter(
            candidate.drop_reason
            for _episode, candidate in all_candidates
            if candidate.drop_reason is not None)),
        "deterministic_class_counts": dict(Counter(
            candidate.class_id
            for _episode, candidate in all_candidates
            if candidate.outcome == OUTCOME_ADMITTED)),
        "episode_summaries": episode_summaries[:_SHADOW_EPISODE_SUMMARY_LIMIT],
        "episode_summaries_truncated": truncated_episodes,
        "candidate_summaries": candidate_summaries[:_SHADOW_CANDIDATE_SUMMARY_LIMIT],
        "candidate_summaries_truncated": truncated_candidates,
        "source_summaries": source_summaries[:_SHADOW_SOURCE_SUMMARY_LIMIT],
        "source_summaries_truncated": truncated_sources,
        "compositional": compositional,
    }


def _unique_episode_source_map(episodes: list[Any]) -> dict[str, str]:
    """Return source text only for non-blank UUIDs that occur exactly once."""
    rows = [
        (
            str(getattr(episode, "uuid", "") or "").strip(),
            str(getattr(episode, "content", "") or ""),
        )
        for episode in episodes
    ]
    counts = Counter(uuid for uuid, _content in rows if uuid)
    return {
        uuid: content
        for uuid, content in rows
        if uuid and counts[uuid] == 1
    }


def _proposal_audit_summary(proposal: Any) -> dict[str, Any]:
    """Bounded, quote-free proposal detail for diagnosing extraction/gate loss.

    Offsets let an authorized inspector join back to the source episode without duplicating raw
    transcript text in telemetry.
    """
    return {
        "source_key": proposal.source_key,
        "episode_uuid": proposal.episode_uuid,
        "span_start": proposal.span_start,
        "span_end": proposal.span_end,
        "attribute": proposal.attribute,
        "scope": proposal.scope,
        "value_kind": proposal.value_kind,
        "unit": proposal.unit,
        "operation": proposal.operation,
        "value": proposal.normalized_value,
        "when": proposal.when,
    }
