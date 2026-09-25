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
from menhir.services.structural_scalar_composer import (
    STRUCTURAL_COMPOSER_VERSION,
    STRUCTURAL_REASON_CODES,
    compose_structural_scalar_identity,
)
from menhir.services.typed_scalar_service_activation import (
    ScalarStateNotActivatedError, ensure_scalar_state_activated,
)
from menhir.services.typed_scalar_service_shadow import (
    _COMPOSITIONAL_REASON_CODES, _COMPOSITIONAL_STATUSES, _MISMATCH_DIMENSIONS,
    _aligned_shadow_match, _identity_mismatch_dimensions,
)
from menhir.services.typed_scalar_service_shadow_report import (
    _compare_deterministic_shadow, _compositional_shadow_error_details,
    _proposal_audit_summary, _unique_episode_source_map,
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

    def ensure_activated(self) -> None:
        """Run the activation gate exactly once per service instance, before any record. Raises
        `ScalarStateActivationError` (legacy store) or `ScalarStateNotActivatedError` (DDL not online)
        if activation cannot be established; the caller must not record when this raises."""
        if not self._activated:
            ensure_scalar_state_activated(self._adapter)
            self._activated = True

    def _make_self_seam(self) -> ResolveSelfSubject:
        """Build the canonical-self resolver seam for one job run. First-person subjects bind to the
        namespace's ONE stable self :Entity via `adapter.ensure_self_entity` (idempotent MERGE). The
        per-run cache collapses N per-decision lookups to one MERGE per namespace WITHOUT outliving a
        namespace wipe (a fresh seam per call). Fail-closed: any ensure failure skips self-binding for
        that namespace (the subject falls through to ordinary binding -> advisory), never crashing
        perception."""
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
                        ns, exc_info=True)
                    uuid = ""
                cache[ns] = uuid
            return (uuid, SELF_SUBJECT_DISPLAY) if uuid else None

        return _seam

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
            resolve_self_subject=self._make_self_seam(),
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
            resolve_self_subject=self._make_self_seam(),
            lookup_namespace_entities=getattr(
                self._adapter, "lookup_entities_by_normalized_names", None),
        )
