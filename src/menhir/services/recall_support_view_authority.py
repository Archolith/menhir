"""Provenance-linked current-state View authority suppression planning."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class RecallSupportViewAuthorityMixin:
    def _plan_view_authority_suppression(
        self, query: str, namespace: str, candidate_inputs: list[dict[str, object]]
    ) -> frozenset[str]:
        """Provenance-linked current-state View suppression (Step 7 canary).

        Returns the candidate uuids a current scalar_state View is authorised to suppress: empty
        unless an EXPLICIT current-state query has provenance-linked stale predecessors that pass all
        six authority gates. The candidate uuids passed here are RECALL candidate `:Entity` semantic-
        memory nodes; `suppressible_provenance` bridges each to the typed-assertion log via its shared
        source episode `(:Episodic)-[:MENTIONS]->(:Entity)` and returns suppression in that SAME entity
        space (Step 7c fix: the join was formerly against `TypedAssertion.episode_uuid`, a disjoint
        uuid space, so it matched nothing and the gate never fired). Read-only; never mutates the graph."""
        from menhir.domain.scalar_view_authority import QueryIntent
        from menhir.domain.scalar_view_suppression import (
            authority_query_intent,
            plan_view_suppression,
        )
        from menhir.infrastructure.audit_trail import RECALL as _audit
        from menhir.infrastructure.typed_assertion_repository import TypedAssertionRepository

        # Audit every decision point so a replay can tell WHY suppression did or did not fire:
        # non-current intent vs no suppressible provenance surfaced (a "nothing to suppress" quirk)
        # vs a gate that advisory'd on a surfaced row (a real gap). Behavior-neutral, toggle-gated.
        _audit.begin()
        uuids = [str(c["uuid"]) for c in candidate_inputs]
        intent = authority_query_intent(query)
        _audit.audit("intent", intent.value, namespace=namespace,
                     details={"query": query[:120], "candidates": len(uuids), "candidate_uuids": uuids})
        if intent is not QueryIntent.CURRENT_STATE:
            _audit.audit("skip", "not_current_state", namespace=namespace)
            return frozenset()
        neo4j = getattr(self.graph_adapter, "neo4j", None)
        if neo4j is None:
            _audit.audit("skip", "no_neo4j", namespace=namespace)
            return frozenset()
        rows = TypedAssertionRepository(neo4j).suppressible_provenance(
            namespace=namespace, candidate_uuids=uuids
        )
        _audit.audit("provenance", "fetched", namespace=namespace,
                     details={"row_count": len(rows),
                              "row_candidate_uuids": [str(r.get("candidate_uuid")) for r in rows]})
        if not rows:
            # No surfaced candidate is the source episode of a superseded predecessor for a current
            # View -> nothing suppressible was surfaced (the "quirk" branch, not a gate failure).
            _audit.audit("result", "nothing_suppressible", namespace=namespace,
                         details={"candidates": len(uuids)})
            return frozenset()
        plan = plan_view_suppression(query, rows, uuids)
        # Per-row gate verdict: an advisory here on a surfaced row is where a real gap would show.
        for d in plan.decisions:
            _audit.audit("gate", d.outcome, namespace=namespace,
                         details={"candidate_uuid": d.candidate_uuid, "attribute": d.slot_attribute,
                                  "gate": d.gate, "reason": d.reason})
        _audit.audit("result", "suppressed" if plan.any_suppressed else "no_suppression",
                     namespace=namespace,
                     details={"suppressed": sorted(plan.suppressed_uuids), "rows": len(rows)})
        if plan.any_suppressed:
            logger.info(
                "View authority suppressed %d candidate(s) for current-state query=%r: %s",
                len(plan.suppressed_uuids),
                query[:60],
                sorted(plan.suppressed_uuids),
            )
        return plan.suppressed_uuids
