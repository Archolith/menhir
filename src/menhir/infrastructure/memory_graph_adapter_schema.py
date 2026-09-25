"""Phase-one schema bootstrap plus typed-assertion and typed-event delegates.

Methods moved verbatim from ``memory_graph_adapter.py`` (facade split); this
mixin is composed into :class:`~menhir.infrastructure.memory_graph_adapter.MemoryGraphAdapter`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from menhir.infrastructure.schema import (
    GRAPHITI_OWNED_INDEXES,
    PHASE_ONE_REQUIRED_CONSTRAINTS,
    PHASE_ONE_REQUIRED_INDEXES,
    SCALAR_STATE_REQUIRED_INDEXES,
    get_phase1_bootstrap_queries,
)


@dataclass
class PhaseOneSchemaResult:
    """Result shape for phase-1 schema bootstrap."""

    success: bool
    queries_executed: int
    failures: list[str]

    @property
    def query_failures(self) -> list[str]:
        """Compatibility alias for callers that expect a descriptive failure list."""
        return self.failures


class MemoryGraphSchemaMixin:
    """Phase-one schema bootstrap plus typed-assertion and typed-event delegates (mixin for MemoryGraphAdapter)."""

    # -------------------------------------------------------------------------
    # Schema bootstrap (stays here — uses SHOW INDEXES admin query)
    # -------------------------------------------------------------------------

    def phase_one_schema_ready(self, *, require_graphiti: bool = True) -> bool:
        """Return True when the core phase-one indexes already exist and are online.

        ``require_graphiti=False`` drops the three indexes Graphiti creates
        (:data:`GRAPHITI_OWNED_INDEXES`) from the requirement. Callers pass it when they have
        already established that the Graphiti client is unavailable.

        Why the parameter exists: `prepare_memory_runtime` deliberately skips Graphiti's
        `build_indices_and_constraints` when there is no usable LLM/embedder, so the server can
        "start against Neo4j alone (graph adapter works; Graphiti features degrade) instead of
        crashing" -- its words. But this check then required the very indexes that skipped call
        creates, and nothing else creates them, so the degraded path raised at startup every time
        and was unreachable. A first install with no AI provider could not start at all.

        The default stays strict. Nothing is relaxed for an instance that HAS Graphiti: a missing
        Graphiti index there still refuses the writer, because there it means the build failed
        rather than never ran.
        """
        required = [
            name
            for name in PHASE_ONE_REQUIRED_INDEXES
            if require_graphiti or name not in GRAPHITI_OWNED_INDEXES
        ]
        rows = self.neo4j.execute(
            """
            SHOW INDEXES YIELD name, state
            WHERE name IN $names AND state = 'ONLINE'
            RETURN collect(name) AS names
            """,
            params={"names": required},
        )
        online = {str(name) for name in (rows[0].get("names", []) if rows else [])}
        if not all(name in online for name in required):
            return False

        required_constraints = {
            name: (constraint_type, entity_type, labels, properties)
            for name, constraint_type, entity_type, labels, properties
            in PHASE_ONE_REQUIRED_CONSTRAINTS
        }
        constraint_rows = self.neo4j.execute(
            """
            SHOW CONSTRAINTS
            YIELD name, type, entityType, labelsOrTypes, properties
            WHERE name IN $names
            RETURN name, type, entityType, labelsOrTypes, properties
            """,
            params={"names": list(required_constraints)},
        )
        actual_constraints = {
            str(row.get("name") or ""): (
                str(row.get("type") or ""),
                str(row.get("entityType") or ""),
                tuple(str(value) for value in (row.get("labelsOrTypes") or [])),
                tuple(str(value) for value in (row.get("properties") or [])),
            )
            for row in constraint_rows
        }
        return all(
            actual_constraints.get(name) == expected
            for name, expected in required_constraints.items()
        )

    def scalar_state_schema_ready(self) -> bool:
        """Return True when the ScalarStateView typed-assertion DDL is online. Feature-scoped: a
        deploy with `enable_scalar_state` should gate on this in addition to phase_one. These
        constraints/indexes are ACTIVATION-created (via `activate_scalar_state`, behind the
        identity-version gate) — NOT created by `bootstrap_phase_one` — and are excluded from
        PHASE_ONE_REQUIRED_INDEXES. So this returns False until activation has run successfully."""
        rows = self.neo4j.execute(
            """
            SHOW INDEXES YIELD name, state
            WHERE name IN $names AND state = 'ONLINE'
            RETURN collect(name) AS names
            """,
            params={"names": list(SCALAR_STATE_REQUIRED_INDEXES)},
        )
        online = {str(name) for name in (rows[0].get("names", []) if rows else [])}
        return all(name in online for name in SCALAR_STATE_REQUIRED_INDEXES)

    def activate_scalar_state(self) -> dict[str, Any]:
        """Gated scalar-state activation (ScalarStateView Piece C.3). Refuses over a legacy store
        (raising ScalarStateActivationError from the typed-assertion repo) and otherwise brings the
        source_key-anchored typed-assertion DDL online. This is the ONLY path that creates the
        SCALAR_STATE_REQUIRED_INDEXES — they are deliberately excluded from bootstrap_phase_one so
        the identity space is never established silently over mixed identities. C.4 calls this when
        `enable_scalar_state` is set, in addition to (not instead of) bootstrap_phase_one."""
        return self._typed_assertions.activate_scalar_state()

    def record_typed_assertion(self, assertion: Any) -> dict[str, Any]:
        """Persist one durable :TypedAssertion (ScalarStateView Piece C.1 store). Delegates to the
        typed-assertion repository; used by the C.4.3 typed-scalar perception path AFTER activation."""
        return self._typed_assertions.record_assertion(assertion)

    def pending_advisory_assertions(
        self, *, namespaces: list[str] | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Current :TypedAssertion rows needing binding repair — binding_pending advisories OR
        projection_pending (bound, View not yet rebuilt) — in fairness order (unattempted-first, then
        least-recently attempted), optionally restricted to a namespace allowlist under one global
        limit (ScalarStateView C.4.4). See TypedAssertionRepository."""
        return self._typed_assertions.pending_advisory_assertions(namespaces=namespaces, limit=limit)

    def mark_binding_repair_attempted(self, assertion_ids: list[str], *, at: str) -> int:
        """Stamp `binding_repair_attempted_at` on the rows the repair pass examined, advancing the
        fairness frontier so a bounded repair is eventually complete (ScalarStateView C.4.4)."""
        return self._typed_assertions.mark_binding_repair_attempted(assertion_ids, at=at)

    def mark_projection_complete(self, assertion_ids: list[str]) -> int:
        """Clear the projection_pending crash-recovery marker after a successful View rebuild
        (ScalarStateView C.4.4)."""
        return self._typed_assertions.mark_projection_complete(assertion_ids)

    def fetch_assertion_contributors(self, **kwargs: Any) -> dict[str, Any]:
        """Hydrate bounded assertion provenance for synthetic scalar authority verdicts."""
        return self._typed_assertions.fetch_assertion_contributors(**kwargs)

    def entity_exists(self, uuid: str) -> bool:
        """True if an :Entity with this uuid exists (UUID-global, matching binding semantics) — the
        fail-closed survivor check for the C.4.4 orphan pass."""
        return self._typed_assertions.entity_exists(uuid)

    def namespaces_for_operation(
        self, *, merge_op_id: str, absorbed_uuid: str | None = None
    ) -> list[str]:
        """Namespaces a lifecycle op AFFECTS (its rebind records + assertions still on the absorbed
        uuid) — derives the op's scope for namespace-keyed reconciliation (ScalarStateView C.4.4)."""
        return self._typed_assertions.namespaces_for_operation(
            merge_op_id=merge_op_id, absorbed_uuid=absorbed_uuid)

    def namespaces_for_unmerge(self, *, unmerge_op_id: str, merge_op_id: str) -> list[str]:
        """Namespaces an unmerge affects: the forward merge's surviving rebind records UNION this
        unmerge's own pending/complete reconcile markers. The marker source keeps the namespace
        discoverable after restore deleted the rebind records but rebuild failed (ScalarStateView
        C.4.4)."""
        return self._typed_assertions.namespaces_for_unmerge(
            unmerge_op_id=unmerge_op_id, merge_op_id=merge_op_id)

    def orphaned_assertions(
        self, *, namespaces: list[str] | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Distinct (subject_uuid, namespace) orphan work items in fairness order, optionally
        restricted to a namespace allowlist (ScalarStateView C.4.4). See TypedAssertionRepository."""
        return self._typed_assertions.orphaned_assertions(namespaces=namespaces, limit=limit)

    def mark_orphan_repair_attempted(self, work_items: list[dict[str, Any]], *, at: str) -> int:
        """Stamp orphan_repair_attempted_at on examined (subject_uuid, namespace) orphans, advancing
        the fairness frontier (ScalarStateView C.4.4)."""
        return self._typed_assertions.mark_orphan_repair_attempted(work_items, at=at)

    def scalar_state_service(self, *, scalar_history_enabled: bool = False) -> Any:
        """Build a `ScalarStateService` bound to this adapter's typed-assertion store (fold input) and
        its scalar-state View sink. Used by the C.4.3 perception path to rebuild Views after persist."""
        from menhir.services.scalar_state_service import ScalarStateService
        return ScalarStateService(
            self._typed_assertions, self,
            scalar_history_enabled=scalar_history_enabled)

    # -------------------------------------------------------------------------
    # Typed-event assertion delegates → TypedEventAssertionRepository
    # -------------------------------------------------------------------------

    def activate_event_history(self) -> dict[str, Any]:
        """Bring the event-assertion DDL online (idempotent, IF NOT EXISTS)."""
        return self._typed_event_assertions.activate()

    def record_typed_event_assertion(self, assertion: Any) -> dict[str, Any]:
        """Persist one :TypedEventAssertion to the durable event log."""
        return self._typed_event_assertions.record_event_assertion(assertion)

    def event_assertions_for_lane(
        self,
        lane: Any,
        *,
        include_superseded: bool = False,
        materializable_only: bool = False,
    ) -> list[Any]:
        """Reconstructed assertions in one event lane (exact namespace/subject/predicate/domain)."""
        return self._typed_event_assertions.assertions_for_lane(
            lane,
            include_superseded=include_superseded,
            materializable_only=materializable_only,
        )

    def event_assertions_for_subject_predicate(
        self,
        subject_uuid: str,
        predicate: str,
        *,
        namespace: str | None = None,
        include_superseded: bool = False,
        materializable_only: bool = True,
    ) -> list[Any]:
        """Reconstructed assertions for an exact normalized subject_uuid + predicate across ALL actual
        domain lanes (optionally namespace-scoped); no domain filter/override. Default excludes
        superseded and binding-pending (safe for recall)."""
        return self._typed_event_assertions.assertions_for_subject_predicate(
            subject_uuid,
            predicate,
            namespace=namespace,
            include_superseded=include_superseded,
            materializable_only=materializable_only,
        )

    def typed_event_assertion_by_key(self, assertion_key: str) -> Any:
        """Reconstruct the event assertion with `assertion_key`, or None."""
        return self._typed_event_assertions.assertion_by_key(assertion_key)

    def current_typed_event_assertion_for_source(self, source_key: str) -> Any:
        """Reconstruct the CURRENT event assertion for a binding-stable source_key, or None."""
        return self._typed_event_assertions.current_for_source(source_key)

    def event_history_service(self) -> Any:
        """Build an `EventHistoryService` bound to this adapter's event-assertion log (source) and
        to the adapter itself as the event-lane timeline View sink."""
        from menhir.services.event_history_service import EventHistoryService
        return EventHistoryService(self._typed_event_assertions, self)

    def bootstrap_phase_one(self) -> PhaseOneSchemaResult:
        """Apply phase-1 schema fields and indexes in an idempotent manner."""
        queries = get_phase1_bootstrap_queries()
        failures: list[str] = []
        for query in queries:
            try:
                self.neo4j.execute(query)
            except Exception as exc:  # pragma: no cover - external dependency behavior
                failures.append(str(exc))
        return PhaseOneSchemaResult(
            success=len(failures) == 0,
            queries_executed=len(queries),
            failures=failures,
        )
