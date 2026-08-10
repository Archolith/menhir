"""ViewClass{FACT,METRIC} seam (Plan 2 step 2).

Unit tests pin the closed label interface and the core guard (no Neo4j). Online tests prove the
graph-level separation against the stood-up test instance: a FACT and a METRIC with the same key
are independent, Metrics are excluded from fact listings, and Metric nodes carry no semantic
features. See workspace-root .agent/plans/menhir-metric-provenance-redesign.md (Part A).
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.view_repository import ViewClass, ViewRepository, _label_for


# ----------------------------------------------------------------- unit: closed label interface


class _RaisingNeo4j:
    """A neo4j stub whose execute() must never be reached (guard fires first)."""

    def execute(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("Neo4j must not be touched -- the METRIC guard should fire first")


@pytest.mark.unit
def test_label_for_is_a_closed_allowlist():
    assert _label_for(ViewClass.FACT) == "Entity"
    assert _label_for(ViewClass.METRIC) == "Metric"


@pytest.mark.unit
def test_label_for_rejects_unknown():
    with pytest.raises(ValueError):
        _label_for("Entity")  # a raw string is not a ViewClass


def _write_version_kwargs(**overrides):
    base = dict(
        kind="counter", key="ns::s::c", subject="s", name="n", summary="sum", sig="1",
        extra_props={}, namespace="ns", valid_at="2026-01-01T00:00:00+00:00",
        source="instrumentation", source_confidence=0.6, episode_uuids=[], name_embedding=None,
    )
    base.update(overrides)
    return base


@pytest.mark.unit
def test_metric_write_rejects_name_embedding():
    repo = ViewRepository(_RaisingNeo4j())
    with pytest.raises(ValueError, match="must not carry a name_embedding"):
        repo._write_version(view_class=ViewClass.METRIC,
                            **_write_version_kwargs(name_embedding=[0.1, 0.2]))


@pytest.mark.unit
def test_metric_write_rejects_episode_provenance():
    repo = ViewRepository(_RaisingNeo4j())
    with pytest.raises(ValueError, match="episode provenance"):
        repo._write_version(view_class=ViewClass.METRIC,
                            **_write_version_kwargs(episode_uuids=["ep-1"]))


@pytest.mark.unit
@pytest.mark.parametrize("bad_op_id", [None, "", "   "])
def test_record_metric_requires_receipt_op_id(bad_op_id):
    """The locked Metric contract (plan A3/C2): a Metric must reference its immutable
    metric_receipts row. Omitting the identity/provenance stamp must fail BEFORE Neo4j is
    touched -- a provenance-less instrumentation node is never written."""
    repo = ViewRepository(_RaisingNeo4j())
    with pytest.raises(ValueError, match="receipt_op_id"):
        repo.record_metric(subject="enrichment", counter="failures", value=1.0,
                           receipt_op_id=bad_op_id, namespace="ns")


# ----------------------------------------------------------------- online: graph-level separation


@pytest.mark.online
def test_fact_and_metric_same_key_are_independent(test_neo4j_repo):
    """record_counter (FACT) and record_metric (METRIC) with the same (subject,counter) must be
    two independent nodes -- different labels, different values, no cross-supersession."""
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    ns = f"vc-{uuidlib.uuid4().hex[:8]}"

    adapter.record_counter(subject="user", counter="widgets", value=7.0, namespace=ns)
    adapter.record_metric(subject="user", counter="widgets", value=99.0, namespace=ns,
                          receipt_op_id="op-abc")

    fact = adapter.fetch_counter(subject="user", counter="widgets", namespace=ns)
    metric = adapter.fetch_metric(subject="user", counter="widgets", namespace=ns)
    assert fact is not None and float(fact["value"]) == 7.0
    assert metric is not None and float(metric["value"]) == 99.0

    # Two distinct nodes with distinct labels.
    rows = test_neo4j_repo.execute(
        "MATCH (n) WHERE n.view_key = $k RETURN labels(n) AS labels, n.view_value AS v",
        {"k": f"{ns}::user::widgets"},
    )
    labelsets = sorted(tuple(sorted(r["labels"])) for r in rows)
    assert any("Metric" in ls for ls in labelsets)
    assert any("Entity" in ls and "Metric" not in ls for ls in labelsets)


@pytest.mark.online
def test_metric_excluded_from_fact_listings_and_recall_stamps(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    ns = f"vc-{uuidlib.uuid4().hex[:8]}"

    adapter.record_counter(subject="user", counter="reads", value=3.0, namespace=ns)
    adapter.record_metric(subject="enrichment", counter="failures", value=11.0, namespace=ns,
                          receipt_op_id="op-1")

    # Generic fact listings must NOT include the Metric.
    counters = adapter.list_counters(namespace=ns)
    assert all(c["counter"] != "failures" for c in counters)
    views = adapter.list_views(namespace=ns)
    assert all(v["subject"] != "enrichment" for v in views)

    # list_metrics returns the Metric and not the fact.
    metrics = adapter.list_metrics(namespace=ns)
    assert any(m["counter"] == "failures" and m["receipt_op_id"] == "op-1" for m in metrics)
    assert all(m["counter"] != "reads" for m in metrics)

    # The Metric node carries type=METRIC, no name_embedding, and no MENTIONS.
    row = test_neo4j_repo.execute(
        "MATCH (n:Metric {view_key:$k}) "
        "RETURN n.type AS type, n.name_embedding IS NULL AS no_emb, "
        "NOT EXISTS { MATCH (:Episodic)-[:MENTIONS]->(n) } AS no_mentions",
        {"k": f"{ns}::enrichment::failures"},
    )
    assert row and row[0]["type"] == "METRIC"
    assert row[0]["no_emb"] is True
    assert row[0]["no_mentions"] is True


@pytest.mark.online
def test_metric_projection_carries_every_required_stamp(test_neo4j_repo):
    """Projection contract (plan A3/A4/C2): a written Metric node must carry the COMPLETE required
    stamp set. This fails if record_metric ever stops writing any one of them -- the enforcement
    half of the class contract, complementary to the unit rejection tests above.

    Required stamps: identity/provenance (type='METRIC', metric_last_receipt_op_id), view identity
    (is_view, view_kind, view_key, view_subject), versioning (view_current, view_sig), and the
    NEGATIVE stamps that keep a Metric out of the recall surface (no name_embedding, no
    supporting_event_count / episode_uuids)."""
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    ns = f"vc-{uuidlib.uuid4().hex[:8]}"

    adapter.record_metric(subject="enrichment", counter="failures", value=42.0, namespace=ns,
                          receipt_op_id="op-stamp-1")

    row = test_neo4j_repo.execute(
        "MATCH (n:Metric {view_key:$k}) WHERE coalesce(n.view_current, true) "
        "RETURN n.type AS type, n.metric_last_receipt_op_id AS receipt, "
        "n.is_view AS is_view, n.view_kind AS view_kind, n.view_key AS view_key, "
        "n.view_subject AS view_subject, n.view_current AS view_current, "
        "n.view_sig AS view_sig, n.name_embedding IS NULL AS no_emb, "
        "n.supporting_event_count AS supp, n.episode_uuids AS eps",
        {"k": f"{ns}::enrichment::failures"},
    )
    assert row, "current :Metric node not found"
    r = row[0]
    # Required positive stamps -- each must be present and non-null.
    assert r["type"] == "METRIC"
    assert r["receipt"] == "op-stamp-1"
    assert r["is_view"] is True
    assert r["view_kind"] == "counter"
    assert r["view_key"] == f"{ns}::enrichment::failures"
    assert r["view_subject"] == "enrichment"
    assert r["view_current"] is True
    assert r["view_sig"] is not None and str(r["view_sig"]).strip()
    # Required NEGATIVE stamps -- a Metric is instrumentation, never a recallable memory.
    assert r["no_emb"] is True
    assert r["supp"] is None
    assert r["eps"] is None
