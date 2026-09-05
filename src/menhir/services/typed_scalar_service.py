"""Stateful coordinator for typed-scalar perception and projection repair."""

from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from menhir.domain.scalar_identity import CompositionalScalarIdentity
from menhir.domain.typed_assertion import build_source_key, normalize_scalar
from menhir.infrastructure import consolidation_audit as _audit
from menhir.services.deterministic_scalar_extractor import (
    EXTRACTOR_VERSION,
    OUTCOME_ADMITTED,
    OUTCOME_DROPPED,
    TEMPLATE_VERSION,
    DeterministicExtraction,
    DeterministicScalarExtractor,
)
from menhir.services.deterministic_scalar_router import (
    ROUTE_LLM_REVIEW,
    ROUTER_VERSION,
    DeterministicScalarRouter,
)
from menhir.services.typed_scalar_persistence import (
    bind_and_persist_typed_scalars,
    repair_pending_bindings,
)
from menhir.services.typed_scalar_rules import (
    LlmComplete,
    ResolveSelfSubject,
    SELF_SUBJECT_DISPLAY,
    TypedScalarDecision,
    TypedScalarProposal,
    _interpretation_label,
    _utc_now_iso,
    _validate_threshold,
    extract_typed_scalars_once,
    gate_typed_scalars,
)
from menhir.infrastructure.self_binding import SelfBindMode, resolve_bind_mode
from menhir.services.structural_scalar_composer import (
    STRUCTURAL_COMPOSER_VERSION,
    STRUCTURAL_REASON_CODES,
    compose_structural_scalar_identity,
)

logger = logging.getLogger(__name__)

_AUDIT_PROPOSALS_PER_SAMPLE = 64

# Schema version for the deterministic shadow payload. Bump this when the audit contract changes.
_SHADOW_SCHEMA_VERSION = 2
_COMPOSITIONAL_SHADOW_SCHEMA_VERSION = 1
# Keep shadow telemetry bounded even for large consolidation batches.
_SHADOW_EPISODE_SUMMARY_LIMIT = 100
_SHADOW_CANDIDATE_SUMMARY_LIMIT = 200
_SHADOW_SOURCE_SUMMARY_LIMIT = 200

_COMPOSITION_ERROR = "struct.composer_error"
_COMPOSITIONAL_STATUSES = frozenset({
    "compositional_exact",
    "compositional_aligned",
    "identity_disagreement",
    "unresolved",
})
_MISMATCH_DIMENSIONS = (
    "subject",
    "relation",
    "target",
    "scope",
    "value_kind",
    "value",
    "unit",
    "operation",
    "effective_time",
)
_COMPOSITIONAL_REASON_CODES = STRUCTURAL_REASON_CODES | {_COMPOSITION_ERROR}


@dataclass(frozen=True)
class _ShadowSidecar:
    identity: CompositionalScalarIdentity | None
    reason_code: str | None


def _spans_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Return whether two non-empty located spans share at least one character."""
    return a_start < b_end and b_start < a_end


def _verified_shadow_alignment(
    det: TypedScalarProposal,
    llm: TypedScalarProposal,
) -> bool:
    """Verify that two proposal locators share the same real source substring."""
    if det.episode_uuid != llm.episode_uuid:
        return False
    if not _spans_overlap(det.span_start, det.span_end, llm.span_start, llm.span_end):
        return False
    common_start = max(det.span_start, llm.span_start)
    common_end = min(det.span_end, llm.span_end)
    spans: list[str] = []
    for proposal in (det, llm):
        if len(proposal.stated_span) != proposal.span_end - proposal.span_start:
            return False
        offset = common_start - proposal.span_start
        spans.append(proposal.stated_span[offset:offset + (common_end - common_start)])
    return spans[0] == spans[1] and bool(re.search(r"\w", spans[0], re.UNICODE))


def _exact_shadow_match(
    det: TypedScalarProposal,
    llm: TypedScalarProposal,
    *,
    canonical_self: bool = False,
) -> bool:
    """Exact agreement requires the same source locator and full interpretation."""
    return (
        det.source_key == llm.source_key
        and _interpretation_label(det, canonical_self=canonical_self)
        == _interpretation_label(llm, canonical_self=canonical_self)
    )


def _aligned_shadow_match(
    det: TypedScalarProposal,
    llm: TypedScalarProposal,
    *,
    canonical_self: bool = False,
) -> bool:
    """Aligned agreement tolerates quote-boundary drift through a verifiable common span.

    Pairwise overlap alone is too permissive: two claims can touch only at punctuation, or have
    synthetic offsets that do not describe the quoted text. The gate's span-alignment contract
    requires the common intersection to be a real, equal source substring containing a word.
    """
    if not _verified_shadow_alignment(det, llm) or (
        _interpretation_label(det, canonical_self=canonical_self)
        != _interpretation_label(llm, canonical_self=canonical_self)
    ):
        return False
    return True


def _matched_shadow_pairs(
    deterministic: list[TypedScalarProposal],
    llm_committed: list[TypedScalarProposal],
    predicate: Callable[[TypedScalarProposal, TypedScalarProposal], bool],
) -> tuple[tuple[int, int], ...]:
    """Return stable maximum one-to-one ``(det_index, llm_index)`` pairs."""
    matched_deterministic: dict[int, int] = {}

    def _augment(llm_index: int, seen: set[int]) -> bool:
        llm = llm_committed[llm_index]
        for det_index, det in enumerate(deterministic):
            if det_index in seen or not predicate(det, llm):
                continue
            seen.add(det_index)
            previous_llm = matched_deterministic.get(det_index)
            if previous_llm is None or _augment(previous_llm, seen):
                matched_deterministic[det_index] = llm_index
                return True
        return False

    for llm_index in range(len(llm_committed)):
        _augment(llm_index, set())
    return tuple(sorted(matched_deterministic.items(), key=lambda pair: pair[1]))


def _matched_llm_indices(
    deterministic: list[TypedScalarProposal],
    llm_committed: list[TypedScalarProposal],
    predicate: Callable[[TypedScalarProposal, TypedScalarProposal], bool],
) -> set[int]:
    """Return a stable maximum one-to-one match between deterministic and LLM claims.

    Agreement is a per-claim metric. Without one-to-one matching, one deterministic proposal could
    make several duplicate LLM claims look like agreements and inflate the shadow result.
    """
    return {
        llm_index
        for _det_index, llm_index in _matched_shadow_pairs(
            deterministic, llm_committed, predicate)
    }


def _compose_shadow_sidecars(
    proposals: list[TypedScalarProposal],
    source_by_episode: Mapping[str, str],
) -> list[_ShadowSidecar]:
    """Compose proposals independently; one failure never erases legacy raw metrics."""
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


def _identity_mismatch_dimensions(
    deterministic: CompositionalScalarIdentity,
    llm: CompositionalScalarIdentity,
) -> tuple[str, ...]:
    det_target, det_scope = deterministic.target_or_scope
    llm_target, llm_scope = llm.target_or_scope
    values = (
        (deterministic.subject, llm.subject),
        (deterministic.relation_type, llm.relation_type),
        (det_target, llm_target),
        (det_scope, llm_scope),
        (deterministic.value_kind, llm.value_kind),
        (deterministic.value, llm.value),
        (deterministic.unit, llm.unit),
        (deterministic.operation, llm.operation),
        (deterministic.effective_time, llm.effective_time),
    )
    return tuple(
        name
        for name, (det_value, llm_value) in zip(_MISMATCH_DIMENSIONS, values)
        if det_value != llm_value
    )


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


class ScalarStateNotActivatedError(RuntimeError):
    """Raised when scalar-state activation ran without leaving the required DDL online — the
    identity-versioned indexes are not ready, so recording an assertion is unsafe. Distinct from the
    repository's `ScalarStateActivationError` (which fires when activation is REFUSED over a
    legacy/incompatible store): this fires when activation was ATTEMPTED but the schema did not come
    up, so we still fail closed rather than record into an unindexed store."""


def ensure_scalar_state_activated(adapter: Any) -> dict[str, Any]:
    """Enforce the C.4.3 activation ordering: bring scalar-state online (and PASS) before any
    assertion is recorded. ALWAYS calls `adapter.activate_scalar_state()` — never short-circuits on a
    `scalar_state_schema_ready()` precheck — so a rolled-back or hand-altered store still hits the
    repository's exact-match identity-version gate (which raises `ScalarStateActivationError` over any
    incompatible node). Then verifies `scalar_state_schema_ready()` AFTER activation and fails closed
    (`ScalarStateNotActivatedError`) if the DDL is not online. Idempotent. Returns the activation
    result plus `schema_ready`."""
    result = dict(adapter.activate_scalar_state())
    if not adapter.scalar_state_schema_ready():
        raise ScalarStateNotActivatedError(
            "scalar-state activation ran but the required DDL is not ONLINE; refusing to record "
            "assertions into an unindexed store")
    result["schema_ready"] = True
    return result


class TypedScalarPerceptionService:
    """End-to-end typed-scalar perception coordinator (C.4.3): extract k -> gate -> bind -> persist
    -> rebuild, behind the caller's `enable_scalar_state` flag. It is the ONLY entry that records
    assertions, and it enforces activation ordering: `activate_scalar_state()` MUST run and pass
    before the first record. The counter perception path (`perception.py`) is never touched."""

    def __init__(
        self, adapter: Any, scalar_state_service: Any, *, perceiver_version: str = "v1",
        embed: "Callable[[str], list[float] | None] | None" = None,
        embed_version: str | None = None,
        scalar_history_enabled: bool = False,
        deterministic_shadow_enabled: bool = False,
        deterministic_router_enabled: bool = False,
        deterministic_router_promoted_classes: tuple[str, ...] = (),
    ) -> None:
        self._adapter = adapter
        self._service = scalar_state_service
        self._perceiver_version = perceiver_version
        # 4a.1 write-time observation embedding seams (optional): None -> observations are embedded only
        # by the resumable backfill (correctness), never at write time (latency).
        self._embed = embed
        self._embed_version = embed_version
        self._activated = False
        self._scalar_history_enabled = scalar_history_enabled
        # Observe-only Phase 2A: the deterministic extractor runs over the same episodes for
        # comparison telemetry, but never substitutes for or persists ahead of LLM decisions.
        self._deterministic_shadow_enabled = deterministic_shadow_enabled
        self._deterministic_router_enabled = deterministic_router_enabled
        self._deterministic_router_promoted_classes = tuple(deterministic_router_promoted_classes)
        self._canonical_self_binding_mode = resolve_bind_mode(
            getattr(adapter, "canonical_self_binding_mode", "off")
        )

    def ensure_activated(self) -> None:
        """Run the activation gate exactly once per service instance, before any record. Raises
        `ScalarStateActivationError` (legacy store) or `ScalarStateNotActivatedError` (DDL not online)
        if activation cannot be established; the caller must not record when this raises."""
        if not self._activated:
            ensure_scalar_state_activated(self._adapter)
            self._activated = True

    def _make_legacy_self_seam(self) -> ResolveSelfSubject:
        """Build the pre-authority-boundary resolver used outside active enforcement."""

        cache: dict[str, str] = {}

        def _seam(ns: str | None) -> "tuple[str, str] | None":
            if not ns:
                return None
            uuid = cache.get(ns)
            if uuid is None:
                try:
                    uuid = str(self._adapter.ensure_self_entity(ns) or "")
                except Exception:
                    logger.warning(
                        "ensure_self_entity failed for namespace=%s; self-binding skipped this run",
                        ns,
                        exc_info=True,
                    )
                    uuid = ""
                cache[ns] = uuid
            return (uuid, SELF_SUBJECT_DISPLAY) if uuid else None

        return _seam

    def _make_self_seam(self) -> ResolveSelfSubject:
        """Compatibility seam retained for isolated callers and test doubles."""

        return self._make_legacy_self_seam()

    def _self_resolver_for_rollout(self) -> ResolveSelfSubject | None:
        """Preserve behavior in off/observe; only enforce withholds unconfirmed promotion."""

        if getattr(self, "_canonical_self_binding_mode", SelfBindMode.OFF) is SelfBindMode.ENFORCE:
            return None
        return self._make_self_seam()

    def perceive_and_persist(
        self, episodes: list[Any], llm_complete: LlmComplete, *, k: int = 3,
        threshold: float = 1.0, namespace: str | None = None,
        episode_reference_time: Callable[[str], str | None] | None = None,
        reconcile_attribute: bool = False,
        reconcile_scope: bool = False,
        reconcile_subject: bool = False,
        canonical_self: bool = False,
    ) -> dict[str, Any]:
        """Perceive typed scalars from `episodes` and durably persist the committed, bound ones.
        Activation is enforced FIRST â€” if it is refused (legacy store) or the schema is not ready, we
        raise and record nothing. `k` must be a real integer >= 1 (bool/zero/negative fail closed
        rather than silently collapsing to one sample); k samples require temp>0 in `llm_complete` to
        be meaningful.

        `reconcile_attribute` / `reconcile_scope` / `reconcile_subject` forward to
        `gate_typed_scalars`: samples vote WITHOUT the named free-text identity fields and the
        winning combination is chosen modally afterwards, as a vote on the identity TUPLE (never
        field-by-field, which could synthesize a slot no sample proposed). `canonical_self` folds
        first-person subjects to the bound self display before the vote. All default off; see that
        function for the measurement. Safe span alignment is always enabled here: overlapping quote
        variants with the same constrained value semantics are grounded to their deterministic
        common source substring before persistence, so sample wording cannot fork one source claim."""
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ValueError(f"k must be a positive integer (>= 1), got {k!r}")
        _validate_threshold(threshold)
        self.ensure_activated()
        # Capture ONE as_of for this live run and thread it into every rebuild, so a future-dated
        # assertion (valid_at > now) does NOT become the current View live (when-discipline). Live and
        # rebuild use the same fold; a later rebuild activates future values as their valid_at passes.
        as_of = datetime.now(timezone.utc)
        # Parse-stage attribution (auditability Gap B). A row the model DID emit but that we then
        # discarded is invisible downstream -- it looks identical to a claim never proposed. Collect
        # the reasons here and emit ONE event per pass rather than one per discarded row, so the
        # attribution is preserved without flooding the trail.
        drops: Counter[str] = Counter()

        def _note_drop(reason: str) -> None:
            drops[reason] += 1

        deterministic_extraction: DeterministicExtraction | None = None
        router_audit_extraction: DeterministicExtraction | None = None
        deterministic_decisions: list[TypedScalarDecision] = []
        llm_episodes = episodes
        router_result = None
        router_failure: str | None = None
        router_failure_details: dict[str, str] | None = None
        # A few compatibility tests construct this service with ``__new__`` to isolate audit
        # behavior. Treat an absent new flag exactly like its public default (off), just as the
        # older deterministic-shadow flag does below.
        router_enabled = getattr(self, "_deterministic_router_enabled", False)
        raw_promoted_classes = getattr(self, "_deterministic_router_promoted_classes", ())
        try:
            promoted_classes = tuple(raw_promoted_classes or ())
        except TypeError:
            promoted_classes = (raw_promoted_classes,)
        if router_enabled:
            try:
                router = DeterministicScalarRouter(promoted_classes)
                deterministic_extraction, router_result = router.extract_and_route(episodes)
                router_audit_extraction = deterministic_extraction
                router_failure = router_result.failure
                if router_failure:
                    # Router failures are contract labels, not episode text. Keep the audit
                    # detail bounded in case a future implementation adds more context.
                    router_failure_details = {
                        "kind": "router_contract",
                        "reason": str(router_failure)[:128],
                    }
                if router_failure is None:
                    deterministic_decisions = DeterministicScalarRouter._to_decisions(
                        router_result)
                    if len(deterministic_decisions) != len(router_result.deterministic_proposals):
                        router_failure = "deterministic_router_contract_failure:decision_conversion_mismatch"
                        router_failure_details = {
                            "kind": "router_contract",
                            "reason": router_failure[:128],
                        }
                        router_result = None
                        deterministic_decisions = []
                        llm_episodes = episodes
                    else:
                        reviewed = set(router_result.reviewed_episodes)
                        llm_episodes = [episode for episode in episodes
                                        if str(getattr(episode, "uuid", "") or "").strip() in reviewed]
                else:
                    llm_episodes = episodes
                    deterministic_extraction = DeterministicExtraction(
                        EXTRACTOR_VERSION, TEMPLATE_VERSION, (), (), (), ())
            except Exception as exc:
                logger.exception("deterministic scalar router failed; falling back to legacy LLM path")
                router_failure = f"{type(exc).__name__}"
                router_failure_details = {
                    "kind": "exception",
                    "exception_type": type(exc).__name__,
                }
                deterministic_extraction = DeterministicExtraction(
                    EXTRACTOR_VERSION, TEMPLATE_VERSION, (), (), (), ())
                deterministic_decisions = []
                router_result = None
                llm_episodes = episodes
            if _audit.is_enabled():
                try:
                    route_counts = (
                        dict(router_result.route_counts)
                        if router_result is not None
                        else {ROUTE_LLM_REVIEW: len(episodes)}
                    )
                    route_class_counts: dict[str, dict[str, int]] = {}
                    if router_result is not None:
                        for route, class_id, count in router_result.route_class_counts:
                            route_class_counts.setdefault(route, {})[class_id] = count
                    promoted_class_ids = (
                        tuple(router_result.promoted_class_ids)
                        if router_result is not None
                        else tuple(sorted({
                            value.strip().lower()
                            for value in promoted_classes
                            if isinstance(value, str) and value.strip()
                        }))[:32]
                    )
                    _audit.audit(
                        "deterministic_router",
                        "error" if router_failure else "ok",
                        namespace=namespace,
                        details={
                            "router_version": ROUTER_VERSION,
                            "extractor_version": router_audit_extraction.extractor_version if router_audit_extraction else EXTRACTOR_VERSION,
                            "template_version": router_audit_extraction.template_version if router_audit_extraction else TEMPLATE_VERSION,
                            "route_counts": route_counts,
                            "route_class_counts": route_class_counts,
                            "episodes_total": len(episodes),
                            "reviewed_episodes": len(router_result.reviewed_episodes) if router_result else len(episodes),
                            "deterministic_decisions": len(deterministic_decisions),
                            "class_counts": dict(router_result.class_counts) if router_result else {},
                            "eligible_unpromoted": router_result.eligible_unpromoted if router_result else 0,
                            "mixed_class_blocked": router_result.mixed_class_blocked if router_result else 0,
                            "promoted_class_ids": promoted_class_ids,
                            "failure": router_failure,
                            "failure_details": router_failure_details,
                        },
                    )
                except Exception:
                    logger.exception("deterministic router audit emit failed (best-effort)")

        samples = [
            extract_typed_scalars_once(llm_episodes, llm_complete, on_drop=_note_drop)
            for _ in range(k)
        ] if llm_episodes or not router_enabled else []
        kept = sum(len(s) for s in samples)
        sample_details: list[dict[str, Any]] = []
        if _audit.is_enabled():
            sample_details = [
                {
                    "sample": index,
                    "kept": len(sample),
                    "proposals": [
                        _proposal_audit_summary(proposal)
                        for proposal in sample[:_AUDIT_PROPOSALS_PER_SAMPLE]
                    ],
                    "truncated": max(0, len(sample) - _AUDIT_PROPOSALS_PER_SAMPLE),
                }
                for index, sample in enumerate(samples)
            ]
        _audit.audit(
            "extract", "rows_dropped" if drops else "rows_clean",
            namespace=namespace,
            details={
                "kept": kept,
                "dropped": sum(drops.values()),
                "by_reason": dict(drops),
                "k": k,
                "episodes": len(llm_episodes),
                "samples": sample_details,
            },
        )
        llm_decisions = gate_typed_scalars(
            samples, threshold=threshold, reconcile_attribute=reconcile_attribute,
            reconcile_scope=reconcile_scope, reconcile_subject=reconcile_subject,
            canonical_self=canonical_self, align_spans=True) if samples else []
        decisions = self._merge_router_decisions(
            episodes, deterministic_decisions, llm_decisions
        ) if router_enabled and router_failure is None else llm_decisions
        # Keep the deterministic shadow after the LLM gate so it cannot affect extraction,
        # binding, persistence, projection, or the returned result.
        if getattr(self, "_deterministic_shadow_enabled", False):
            self._run_deterministic_shadow(
                episodes,
                llm_decisions if router_enabled and router_failure is None else decisions,
                namespace,
                canonical_self=canonical_self,
                deterministic_extraction=deterministic_extraction,
                comparison_scope=(
                    "llm_reviewed_subset"
                    if router_enabled and router_failure is None
                    else "all_llm_committed"
                ),
                comparison_episode_uuids=(
                    set(router_result.reviewed_episodes)
                    if router_enabled and router_failure is None and router_result
                    else None
                ),
            )
        # When scalar_history is enabled, the rebuild lambda calls the coordinator
        # (which rebuilds both state and history projections) instead of state-only.
        if self._scalar_history_enabled:
            _rebuild = lambda u: self._service.rebuild_scalar_projections(
                u, namespace=namespace, as_of=as_of, history_enabled=True)
        else:
            _rebuild = lambda u: self._service.rebuild_scalar_state(
                u, namespace=namespace, as_of=as_of)
        out = bind_and_persist_typed_scalars(
            decisions,
            linked_entities_for_episode=self._adapter.fetch_linked_entities_for_episode,
            record_assertion=self._adapter.record_typed_assertion,
            rebuild_scalar_state=_rebuild,
            episode_reference_time=episode_reference_time,
            namespace=namespace, perceiver_version=self._perceiver_version,
            mark_projection_complete=lambda ids: self._adapter.mark_projection_complete(ids),
            resolve_self_subject=self._self_resolver_for_rollout(),
            lookup_namespace_entities=getattr(
                self._adapter, "lookup_entities_by_normalized_names", None),
            embed=self._embed, embed_version=self._embed_version,
        )
        out["decisions"] = len(decisions)
        out["committed"] = sum(1 for d in decisions if d.committed)
        return out

    def _run_deterministic_shadow(
        self,
        episodes: list[Any],
        decisions: list[TypedScalarDecision],
        namespace: str | None,
        *,
        canonical_self: bool = False,
        deterministic_extraction: DeterministicExtraction | None = None,
        comparison_scope: str = "all_llm_committed",
        comparison_episode_uuids: set[str] | None = None,
    ) -> None:
        """Run the pure deterministic extractor and emit best-effort comparison telemetry.

        This is deliberately fail-open: a shadow failure records an error event when auditing is
        enabled, then leaves the existing LLM gate and persistence path untouched.
        """
        status = "ok"
        try:
            deterministic = deterministic_extraction or DeterministicScalarExtractor().extract(episodes)
            if comparison_episode_uuids is not None:
                deterministic = replace(
                    deterministic,
                    episode_receipts=tuple(
                        receipt for receipt in deterministic.episode_receipts
                        if receipt.episode_uuid in comparison_episode_uuids),
                    proposals=tuple(
                        proposal for proposal in deterministic.proposals
                        if proposal.episode_uuid in comparison_episode_uuids),
                    fully_eligible_episode_uuids=tuple(
                        uuid for uuid in deterministic.fully_eligible_episode_uuids
                        if uuid in comparison_episode_uuids),
                )
            committed = [
                decision.proposal for decision in decisions
                if decision.committed and decision.proposal is not None
            ]
            details = _compare_deterministic_shadow(
                deterministic,
                committed,
                canonical_self=canonical_self,
                source_by_episode=_unique_episode_source_map(episodes),
            )
            if comparison_scope != "all_llm_committed":
                details["comparison_scope"] = comparison_scope
        except Exception:
            logger.exception(
                "deterministic scalar shadow failed; LLM gate/persistence path continues "
                "unchanged (fail-open)")
            status = "error"
            details = {
                "schema_version": _SHADOW_SCHEMA_VERSION,
                "extractor_version": EXTRACTOR_VERSION,
                "template_version": TEMPLATE_VERSION,
                "error": "deterministic_shadow_failed",
                "comparison_scope": comparison_scope,
                "compositional": _compositional_shadow_error_details(),
            }
        if not _audit.is_enabled():
            return
        try:
            _audit.audit("deterministic_shadow", status, namespace=namespace, details=details)
        except Exception:
            logger.exception("deterministic_shadow audit emit failed (best-effort)")

    @staticmethod
    def _merge_router_decisions(
        episodes: list[Any],
        deterministic: list[TypedScalarDecision],
        llm: list[TypedScalarDecision],
    ) -> list[TypedScalarDecision]:
        order = {
            str(getattr(episode, "uuid", "") or "").strip(): index
            for index, episode in enumerate(episodes)
        }
        return sorted(
            list(deterministic) + list(llm),
            key=lambda decision: (
                order.get(getattr(decision.proposal, "episode_uuid", ""), len(order)),
                getattr(decision.proposal, "span_start", 0),
                decision.source_key,
            ),
        )

    def repair_pending_bindings(
        self, *, namespaces: list[str] | None = None, limit: int = 200,
    ) -> dict[str, Any]:
        """Run the explicit pending-binding repair pass (C.4.4): resolve current advisories that have
        become uniquely bindable AND finish any crashed View projections, then rebuild. Enforces
        activation FIRST (same gate as recording), so it never writes into an unactivated or legacy
        store. LLM-free. `namespaces` is an ALLOWLIST under a SINGLE global `limit` (deduped;
        fail-closed — a row outside it is never touched), so targeting cannot leak across tenants and
        the bound is never multiplied per namespace. Each View is rebuilt in the ROW's own namespace,
        and the projection marker is cleared only after a successful rebuild. Returns the
        `repair_pending_bindings` summary."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError(f"limit must be a non-negative integer, got {limit!r}")
        self.ensure_activated()
        allow: set[str] | None = None
        ns_arg: list[str] | None = None
        if namespaces is not None:
            allow = set(namespaces)
            ns_arg = list(allow)                 # deduped allowlist for the single fair query
        rows = self._adapter.pending_advisory_assertions(namespaces=ns_arg, limit=limit)
        now = _utc_now_iso()
        as_of = datetime.now(timezone.utc)   # one captured evaluation time for every rebuild this pass
        if self._scalar_history_enabled:
            _rebuild = lambda u, ns: self._service.rebuild_scalar_projections(
                u, namespace=ns, as_of=as_of, history_enabled=True)
        else:
            _rebuild = lambda u, ns: self._service.rebuild_scalar_state(
                u, namespace=ns, as_of=as_of)
        return repair_pending_bindings(
            rows,
            linked_entities_for_episode=self._adapter.fetch_linked_entities_for_episode,
            record_assertion=self._adapter.record_typed_assertion,
            rebuild_scalar_state=_rebuild,
            mark_attempted=lambda ids: self._adapter.mark_binding_repair_attempted(ids, at=now),
            mark_projection_complete=lambda ids: self._adapter.mark_projection_complete(ids),
            allowed_namespaces=allow,
            resolve_self_subject=self._self_resolver_for_rollout(),
            lookup_namespace_entities=getattr(
                self._adapter, "lookup_entities_by_normalized_names", None),
        )
