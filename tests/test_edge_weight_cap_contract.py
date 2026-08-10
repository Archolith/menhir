"""Unit contract test for edge-weight cap: reinforcement-loop truth pinning.

This module enforces the edge-weight reinforcement contract WITHOUT a live Neo4j
dependency: the edge-weight increment is capped at 5.0, incremented by +0.1 per
traversal, with NO decay (edge lifecycle tied to endpoints by design, 2026-07-03).

A silent uncap would reopen retrieval-scale-contract-and-gap-remediation.md §4b
(self-reinforcing relevance bounds). This test holds that decision constant by
asserting the query text contains both the cap literal and the step literal.
"""

from __future__ import annotations

import inspect

import pytest

from menhir.infrastructure.consolidation_queries import ConsolidationRepository


@pytest.mark.unit
def test_increment_edge_weight_cap_contract() -> None:
    """Assert edge weight cap and step are present in the Cypher query."""
    method = ConsolidationRepository.increment_edge_weight
    source = inspect.getsource(method)

    # Contract: capped ratchet at 5.0, +0.1 per traversal
    assert "5.0" in source, (
        "Edge weight cap of 5.0 missing from increment_edge_weight query. "
        "A silent uncap would reopen retrieval doc §4b (self-reinforcing relevance bounds). "
        "Decision: edge lifecycle tied to endpoints, decay is v1 non-goal (2026-07-03)."
    )
    assert "0.1" in source, (
        "Edge weight step of +0.1 missing from increment_edge_weight query. "
        "Step changes require review of the reinforcement-loop time constant."
    )

    # Contract: touches last_traversed (decay protection is via last_accessed on returned nodes)
    assert "last_traversed" in source, (
        "Edge traversal timestamp missing from increment_edge_weight query. "
        "The decay-protection loop relies on last_accessed touches on returned nodes; "
        "last_traversed is the edge-side witness to that mechanism."
    )
