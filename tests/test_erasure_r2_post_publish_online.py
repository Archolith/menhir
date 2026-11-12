"""R2 CF-337 online counterexamples: post-publish writes (ingest stamp, RETENTION_SOURCE, self
tiers) run in the claim-fence transaction, so a seal that lands after the worker's last check
cannot be followed by the write.

The harness is test_menhir_publish_fence_online's seed (a claimed queue episode in an
EvidenceNamespaceFence namespace) plus one extracted semantic entity.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.publish_fence import RECHECK_CYPHER, FenceLost, GateFence
from menhir.services import enrichment_steps
from tests.test_menhir_publish_fence_online import Seed, seed  # noqa: F401 (fixture)

pytestmark = pytest.mark.online


class _SealAfterOutsideCheck:
    """neo4j proxy: right after a claim re-check made OUTSIDE a write transaction (a check-only
    ``check_fence``), an erase seals the queue episode. A write that re-checks inside its own
    transaction never triggers it."""

    def __init__(self, repo: Any, s: Seed) -> None:
        self._repo, self._s = repo, s
        self.sealed = False

    def execute(self, query: str, params: dict[str, Any] | None = None, **kw: Any) -> Any:
        rows = self._repo.execute(query, params=params, **kw)
        if query == RECHECK_CYPHER and not self.sealed:
            self._repo.execute(
                "CREATE (:ErasureMarker {namespace_key:$ns, subject_uuid:$q, op_id:$op})",
                params={"ns": self._s.ns, "q": self._s.q, "op": f"{self._s.tag}-op"},
            )
            self.sealed = True
        return rows

    def execute_write(self, work: Any) -> Any:
        return self._repo.execute_write(work)


def _entity(repo: Any, s: Seed) -> str:
    e = f"{s.tag}-entity"
    repo.execute(
        "CREATE (:Entity {uuid:$e, name:'probe', group_id:$ns, namespace:$ns, scope:'SESSION'})",
        params={"e": e, "ns": s.ns},
    )
    return e


def _ctx(neo4j: Any, s: Seed) -> SimpleNamespace:
    adapter = MemoryGraphAdapter(neo4j=neo4j)
    return SimpleNamespace(
        graph_adapter=adapter, claim_fence=s.claim(), gate_fence=GateFence(epoch=0),
        episode_uuid=s.q, claimed={"namespace": s.ns},
    )


def _retention_edges(repo: Any, s: Seed) -> int:
    return int(repo.execute(
        "MATCH (:Episodic {uuid:$q})-[r:RETENTION_SOURCE]->() RETURN count(r) AS c",
        params={"q": s.q},
    )[0]["c"])


def test_cf337_seal_after_the_last_check_leaves_no_retention_edge(test_neo4j_repo, seed) -> None:  # noqa: F811
    """Property: a RETENTION_SOURCE edge exists only if no seal committed before it. Fails if the
    write goes back to check-then-act (``_fence_check`` then an unfenced write)."""
    repo = test_neo4j_repo
    e = _entity(repo, seed)
    proxy = _SealAfterOutsideCheck(repo, seed)

    try:
        enrichment_steps._record_retention_fenced(_ctx(proxy, seed), [e])
        refused = False
    except FenceLost:
        refused = True

    edges = _retention_edges(repo, seed)
    assert not (edges and proxy.sealed), "RETENTION_SOURCE written after the queue was sealed"
    assert refused or edges == 1


def test_cf337_a_sealed_queue_gets_no_stamp_or_retention(test_neo4j_repo, seed) -> None:  # noqa: F811
    """A seal committed before the writes refuses both, with FenceLost (requeue) rather
    than an enrichment failure. Fails if any of them runs unfenced."""
    repo = test_neo4j_repo
    e = _entity(repo, seed)
    repo.execute(
        "CREATE (:ErasureMarker {namespace_key:$ns, subject_uuid:$q, op_id:$op})",
        params={"ns": seed.ns, "q": seed.q, "op": f"{seed.tag}-op"},
    )
    ctx = _ctx(repo, seed)

    with pytest.raises(FenceLost):
        enrichment_steps._record_retention_fenced(ctx, [e])
    with pytest.raises(FenceLost):
        enrichment_steps._fenced_write(ctx, lambda ex: ctx.graph_adapter.stamp_ingest_metadata(
            node_uuids=[seed.q, e], edge_uuids=[], session_id="s", user_id="u",
            source="claude-code", source_confidence=1.0, namespace=seed.ns, executor=ex,
        ))

    assert _retention_edges(repo, seed) == 0
    row = repo.execute("MATCH (n {uuid:$e}) RETURN n.source AS src", params={"e": e})[0]
    assert row["src"] is None


def test_cf337_writes_land_when_the_claim_holds(test_neo4j_repo, seed) -> None:  # noqa: F811
    """Positive control: the fenced writes still stamp and link."""
    repo = test_neo4j_repo
    e = _entity(repo, seed)
    ctx = _ctx(repo, seed)

    enrichment_steps._record_retention_fenced(ctx, [e])
    stamped = enrichment_steps._fenced_write(ctx, lambda ex: ctx.graph_adapter.stamp_ingest_metadata(
        node_uuids=[seed.q, e], edge_uuids=[], session_id="s", user_id="u",
        source="claude-code", source_confidence=1.0, namespace=seed.ns, executor=ex,
    ))

    assert _retention_edges(repo, seed) == 1
    assert stamped.nodes_touched == 2


def test_cf337_tenancy_error_after_a_fence_loss_is_a_fence_loss(
    test_neo4j_repo, seed, monkeypatch  # noqa: F811
) -> None:
    """The source is gone by the time the write runs and the claim no longer holds: the worker
    must see FenceLost (requeue, ``_failed_enrichments`` untouched), not ValueError. Fails if
    the ValueError is re-raised without re-checking the fence."""
    repo = test_neo4j_repo
    e = _entity(repo, seed)
    ctx = _ctx(repo, seed)

    def unfenced_then_seal(_ctx: Any, call: Any) -> Any:
        # The source vanished mid-flight; the erase's seal follows.
        repo.execute(
            "CREATE (:ErasureMarker {namespace_key:$ns, subject_uuid:$q, op_id:$op})",
            params={"ns": seed.ns, "q": seed.q, "op": f"{seed.tag}-op"},
        )
        raise ValueError("Retention source episode is missing or outside the requested tenant")

    monkeypatch.setattr(enrichment_steps, "_fenced_write", unfenced_then_seal)

    with pytest.raises(FenceLost):
        enrichment_steps._record_retention_fenced(ctx, [e])
