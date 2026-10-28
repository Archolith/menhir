"""Stateful coordinator for typed-scalar perception and projection repair."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable

from menhir.infrastructure import consolidation_audit as _audit
from menhir.services.typed_scalar_persistence import (
    bind_and_persist_typed_scalars,
    repair_pending_bindings,
)
from menhir.services.typed_scalar_rules import (
    LlmComplete,
    ResolveSelfSubject,
    SELF_SUBJECT_DISPLAY,
    TypedScalarProposal,
    _utc_now_iso,
    _validate_threshold,
    extract_typed_scalars_once,
    gate_typed_scalars,
)

logger = logging.getLogger(__name__)

_AUDIT_PROPOSALS_PER_SAMPLE = 64


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
        threshold: float = 2 / 3, namespace: str | None = None,
        episode_reference_time: Callable[[str], str | None] | None = None,
        reconcile_attribute: bool = True,
        reconcile_scope: bool = True,
        reconcile_subject: bool = True,
        canonical_self: bool = False,
        system_prompt: str | None = None,
        allowed_operations: frozenset[str] | None = None,
        claim_ordinal: int = 0,
        context: str = "",
        write_guard: Callable[[], None] | None = None,
        counting_v2: bool = False,
        counting_match: bool = False,
        defer_rebuild: bool = False,
    ) -> dict[str, Any]:
        """Perceive typed scalars from `episodes` and durably persist the committed, bound ones.

        `counting_v2` (counting lane only, unit 3a.9) selects the verb-anchored occurrence guard and
        the per-attribute claim split; it is forwarded only when True, so every other caller is
        unchanged. `counting_match` (counting lane under 4R match) lets the gate set aside bridge
        quotes (`resolve_bridges`); off everywhere else.

        Counting re-version (default off): `defer_rebuild` skips the per-row rebuild, so bound rows
        keep `projection_pending` until the caller rebuilds and marks them; the result then also
        carries `spans` = {"clean_turns": turns every sample read in full, "proposed": every
        sample's (episode, start, end), "committed": the gate's}.

        `system_prompt` / `allowed_operations` / `claim_ordinal` / `context` select the lane: the
        defaults are the scalar lane; the counting lane passes its own prompt, occurrence-only
        operations, claim ordinal 1 and the page's already-stored scalar claims as context.
        Activation is enforced FIRST â€” if it is refused (legacy store) or the schema is not ready, we
        raise and record nothing. `k` must be a real integer >= 1 (bool/zero/negative fail closed
        rather than silently collapsing to one sample); k samples require temp>0 in `llm_complete` to
        be meaningful.

        `reconcile_attribute` / `reconcile_scope` / `reconcile_subject` forward to
        `gate_typed_scalars`: samples vote WITHOUT the named free-text identity fields and the
        winning combination is chosen modally afterwards, as a vote on the identity TUPLE (never
        field-by-field, which could synthesize a slot no sample proposed). `canonical_self` folds
        first-person subjects to the bound self display before the vote. Defaults follow the
        lenient-agreement policy (threshold 2/3, all three reconciles on, 2026-09); see that
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

        v2_kwargs: dict[str, Any] = {"counting_v2": True} if counting_v2 else {}
        # Per sample, the episode indexes it did not fully read (None = the whole sample); only
        # collected for the counting re-version (defer_rebuild).
        unclean: list[set[int | None]] = []

        def _extract_once() -> list[TypedScalarProposal]:
            extra: dict[str, Any] = {}
            if defer_rebuild:
                marks: set[int | None] = set()
                unclean.append(marks)
                extra["on_unclean"] = marks.add
            return extract_typed_scalars_once(
                episodes, llm_complete, on_drop=_note_drop,
                system_prompt=system_prompt, allowed_operations=allowed_operations,
                claim_ordinal=claim_ordinal, context=context, **v2_kwargs, **extra)

        samples = [_extract_once() for _ in range(k)]
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
                "episodes": len(episodes),
                "samples": sample_details,
            },
        )
        llm_decisions = gate_typed_scalars(
            samples, threshold=threshold, reconcile_attribute=reconcile_attribute,
            reconcile_scope=reconcile_scope, reconcile_subject=reconcile_subject,
            canonical_self=canonical_self, align_spans=True, resolve_bridges=counting_match,
            **v2_kwargs) if samples else []
        decisions = llm_decisions
        # When scalar_history is enabled, the rebuild lambda calls the coordinator
        # (which rebuilds both state and history projections) instead of state-only.
        if defer_rebuild:
            # The caller rebuilds once per subject after its own retire step; an incomplete
            # result keeps projection_pending set, so a crash before that rebuild is repairable.
            _rebuild = lambda u: {"complete": False, "deferred": True}
        elif self._scalar_history_enabled:
            _rebuild = lambda u: self._service.rebuild_scalar_projections(
                u, namespace=namespace, as_of=as_of, history_enabled=True)
        else:
            _rebuild = lambda u: self._service.rebuild_scalar_state(
                u, namespace=namespace, as_of=as_of)
        record_assertion = self._adapter.record_typed_assertion
        if write_guard is not None:
            # Counting lane: re-check the namespace fence before every assertion write, so a run
            # whose lease or watermark was lost during the LLM step stores nothing.
            def record_assertion(*args: Any, _record=record_assertion, **kwargs: Any) -> Any:
                write_guard()
                return _record(*args, **kwargs)
        out = bind_and_persist_typed_scalars(
            decisions,
            linked_entities_for_episode=self._adapter.fetch_linked_entities_for_episode,
            record_assertion=record_assertion,
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
        if defer_rebuild:
            # Turns every sample read in full: only there is "no sample proposed anything" evidence.
            uuids = [str(getattr(e, "uuid", "") or "") for e in episodes]
            clean = set(uuids) if samples and len(unclean) == len(samples) else set()
            for marks in unclean:
                clean = set() if None in marks else clean - {
                    uuids[i] for i in marks if i is not None and 0 <= i < len(uuids)}
            out["spans"] = {
                "clean_turns": sorted(clean - {""}),
                "proposed": [(p.episode_uuid, p.span_start, p.span_end)
                             for sample in samples for p in sample],
                "committed": [(d.proposal.episode_uuid, d.proposal.span_start, d.proposal.span_end)
                              for d in decisions if d.committed and d.proposal is not None],
            }
        return out

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
