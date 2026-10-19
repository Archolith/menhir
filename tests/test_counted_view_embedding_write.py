"""Counted-View selection (write side): text helper, ScalarStateService audit keys, adapter wiring,
flag-marker plumbing and the Cypher shape of the read query. Offline except the `online` tests."""

from __future__ import annotations

import uuid as uuidlib
from datetime import UTC, datetime

import pytest

from menhir.config.feature_flags import FEATURES
from menhir.domain.count_view_text import count_view_text
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.memory_queries import MemoryQueryRepository
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository
from menhir.infrastructure.view_embedder import counting_view_selection_adapter_kwargs
from menhir.services.counting_consolidation import sweep_counting_flag_state
from menhir.services.scalar_state_service import ScalarStateService
from tests.test_count_match_fold import _Assertions, _audit_rows, _row, _Views

KEYS = ("count_view_text", "count_view_embedding", "count_view_embed_version")


class _Embed:
    def __init__(self, result=(0.5, 0.25), exc=None):
        self.result, self.exc, self.texts = result, exc, []

    def __call__(self, text):
        self.texts.append(text)
        if self.exc is not None:
            raise self.exc
        return None if self.result is None else list(self.result)


def _rebuild(embed=None, *, rows=None, version="v-embed", match=True, views=None):
    views = views or _Views()
    svc = ScalarStateService(
        _Assertions(rows or _audit_rows()), views, count_match=match,
        view_embed=embed, view_embed_version=version if embed is not None else None)
    svc.rebuild_scalar_state("ent-self")
    return svc, views


# ------------------------------------------------------------------ text helper

@pytest.mark.unit
@pytest.mark.parametrize("attr,items,actions,expected", [
    ("mattress_count", ["queen mattress"], {"bought": 1}, "mattress: queen mattress; bought"),
    ("model_kit_count", ["Spitfire", "Hurricane"], {"finished": 1, "bought": 1},
     "model kit: Spitfire, Hurricane; finished, bought"),
    ("dining_spend", [], {}, "dining: dining"),
    ("plain", None, None, "plain: plain"),
    ("kit_count", '["a", "b"]', '{"bought": 1, "sold": 2}', "kit: a, b; bought, sold"),
    ("kit_count", "not json", "{bad", "kit: kit"),
    ("kit_count", ["a"], [], "kit: a"),
    ("", [], [], ":"),
])
def test_count_view_text_cases(attr, items, actions, expected):
    assert count_view_text(attr, items, actions) == expected


@pytest.mark.unit
def test_count_view_text_is_deterministic_and_keeps_order():
    a = count_view_text("x_count", ["b", "a"], {"z": 1, "y": 2})
    assert a == count_view_text("x_count", ["b", "a"], {"z": 1, "y": 2})
    assert a == "x: b, a; z, y"


# ------------------------------------------------------------------ service write

@pytest.mark.unit
def test_counted_view_gets_text_embedding_and_version():
    embed = _Embed()
    _, views = _rebuild(embed)
    audit = views.records[0]["audit"]
    assert audit["count_view_text"] == "model kit: Spitfire, Hurricane; bought, finished"
    assert audit["count_view_embedding"] == [0.5, 0.25]
    assert audit["count_view_embed_version"] == "v-embed"
    assert embed.texts == [audit["count_view_text"]]


@pytest.mark.unit
def test_embed_failure_and_none_write_three_nulls():
    for embed in (_Embed(result=None), _Embed(exc=RuntimeError("boom"))):
        _, views = _rebuild(embed)
        audit = views.records[0]["audit"]
        assert all(k in audit and audit[k] is None for k in KEYS)
        assert audit["scalar_basis"] == "counted"  # the View itself is still written


@pytest.mark.unit
def test_view_embed_none_writes_no_new_keys():
    _, views = _rebuild(None)
    assert not any(k in views.records[0]["audit"] for k in KEYS)


@pytest.mark.unit
def test_non_counted_view_is_untouched():
    rows = [_row(1, 1, operation="absolute", attribute="coin_count", value=5, count_object="")]
    embed = _Embed()
    _, views = _rebuild(embed, rows=rows, match=False)
    rec = views.records[0]
    assert rec["audit"]["scalar_basis"] is None
    assert not any(k in rec["audit"] for k in KEYS)
    assert embed.texts == []


@pytest.mark.unit
def test_cache_avoids_a_second_embed_call_and_failures_are_not_cached():
    embed = _Embed()
    svc, views = _rebuild(embed)
    svc.rebuild_scalar_state("ent-self")
    assert len(embed.texts) == 1
    assert views.records[1]["audit"]["count_view_embedding"] == [0.5, 0.25]

    failing = _Embed(result=None)
    svc2, _ = _rebuild(failing)
    svc2.rebuild_scalar_state("ent-self")
    assert len(failing.texts) == 2  # a failure is retried, not remembered


@pytest.mark.unit
def test_audit_never_carries_name_embedding():
    _, views = _rebuild(_Embed())
    assert "name_embedding" not in views.records[0]
    assert "name_embedding" not in views.records[0]["audit"]


# ------------------------------------------------------------------ adapter / settings wiring

class _Settings:
    def __init__(self, **kw):
        self.personal_memory_counting_view_selection_enabled = kw.get("sel", True)
        self.personal_memory_counting_enabled = kw.get("counting", True)
        self.personal_memory_scalar_state_enabled = kw.get("state", True)
        self.personal_memory_scalar_view_authority_enabled = kw.get("auth", True)


@pytest.mark.unit
@pytest.mark.parametrize("off", ["sel", "counting", "state", "auth"])
def test_adapter_kwargs_empty_unless_every_prerequisite_is_on(off):
    assert counting_view_selection_adapter_kwargs(_Settings(**{off: False})) == {}


@pytest.mark.unit
def test_adapter_kwargs_when_on(monkeypatch):
    import menhir.infrastructure.view_embedder as ve

    monkeypatch.setattr(ve, "make_view_embedder", lambda s: (lambda t: [1.0]))
    monkeypatch.setattr(ve, "view_embedder_version", lambda s: "v9")
    kw = counting_view_selection_adapter_kwargs(_Settings())
    assert kw["counting_view_embed_version"] == "v9" and kw["counting_view_embed"]("x") == [1.0]
    monkeypatch.setattr(ve, "make_view_embedder", lambda s: None)
    assert counting_view_selection_adapter_kwargs(_Settings()) == {}


@pytest.mark.unit
def test_adapter_factory_passes_embedder_to_the_service():
    embed = _Embed()
    adapter = MemoryGraphAdapter(neo4j=object(), counting_enabled=True,
                                 counting_view_embed=embed, counting_view_embed_version="v1")
    svc = adapter.scalar_state_service()
    assert svc._view_embed is embed and svc._view_embed_version == "v1"
    plain = MemoryGraphAdapter(neo4j=object()).scalar_state_service()
    assert plain._view_embed is None and plain._view_embed_version is None


@pytest.mark.unit
def test_flag_is_registered_requires_counting_and_authority_and_is_not_versioned():
    flag = FEATURES["personal_memory_counting_view_selection_enabled"]
    assert flag.default is False
    assert flag.env_var == "MENHIR_PERSONAL_MEMORY_COUNTING_VIEW_SELECTION_ENABLED"
    assert set(flag.requires) == {
        "personal_memory_counting_enabled", "personal_memory_scalar_view_authority_enabled"}
    assert not flag.version_bump


# ------------------------------------------------------------------ marker + sweep plumbing

class _RecNeo4j:
    def __init__(self, rows=None):
        self.calls, self.rows = [], rows or []

    def execute(self, query, params=None, **kw):
        self.calls.append((query, dict(params or {})))
        return self.rows


@pytest.mark.unit
def test_marker_write_and_sweep_targets_carry_view_selection():
    neo = _RecNeo4j(rows=[{"namespace": "ns-a"}])
    repo = TurnEvidenceRepository(neo)
    repo.mark_counting_flag_state("ns-a", enabled=True, at="2026-01-01T00:00:00Z",
                                  view_selection=True)
    query, params = neo.calls[-1]
    assert "m.view_selection = $view_selection" in query and params["view_selection"] is True
    repo.mark_counting_flag_state("ns-a", enabled=True, at="2026-01-01T00:00:00Z")
    assert neo.calls[-1][1]["view_selection"] is False
    assert repo.counting_flag_sweep_targets(
        enabled=True, namespaces=None, limit=5, view_selection=True) == ["ns-a"]
    query, params = neo.calls[-1]
    assert "coalesce(m.view_selection, false) <> $view_selection" in query
    assert params["view_selection"] is True


class _SweepGraph:
    def __init__(self):
        self.target_kw, self.mark_kw, self.svc_kw = None, None, None

    def counting_flag_sweep_targets(self, **kw):
        self.target_kw = kw
        return ["ns-a"]

    def scalar_state_service(self, **kw):
        self.svc_kw = kw
        return object()

    def counting_sweep_subjects(self, namespace, *, limit, after=""):
        return []

    def mark_counting_flag_state(self, namespace, **kw):
        self.mark_kw = kw


@pytest.mark.unit
def test_sweep_threads_view_selection_only_when_on():
    g = _SweepGraph()
    sweep_counting_flag_state(g, enabled=True, namespaces=None, scalar_history_enabled=False,
                              view_selection=True)
    assert g.target_kw["view_selection"] is True and g.mark_kw["view_selection"] is True
    g = _SweepGraph()
    sweep_counting_flag_state(g, enabled=True, namespaces=None, scalar_history_enabled=False)
    assert "view_selection" not in g.target_kw and "view_selection" not in g.mark_kw
    g = _SweepGraph()
    sweep_counting_flag_state(g, enabled=False, namespaces=None, scalar_history_enabled=False,
                              view_selection=True)
    assert "view_selection" not in g.target_kw


@pytest.mark.unit
def test_read_query_shape_and_params():
    neo = _RecNeo4j(rows=[{"cosine": 0.9}])
    out = MemoryQueryRepository(neo).search_counted_view_embeddings(
        [0.1, 0.2], "proj", embed_version="v1", limit=3)
    assert out == [{"cosine": 0.9}]
    query, params = neo.calls[0]
    from menhir.domain.namespace import tenant_scope_cypher, tenant_scope_params
    for needle in (tenant_scope_cypher("v"), "v.scalar_basis = 'counted'",
                   "coalesce(v.view_current, true)", "v.expired_at IS NULL",
                   "v.count_view_embedding IS NOT NULL",
                   "v.count_view_embed_version = $version",
                   "vector.similarity.cosine(v.count_view_embedding, $query_vector)",
                   "ORDER BY cosine DESC"):
        assert needle in query
    assert "name_embedding" not in query
    assert params == {"version": "v1", "query_vector": [0.1, 0.2], "limit": 3,
                      **tenant_scope_params("proj")}


@pytest.mark.unit
def test_read_query_refuses_an_unscoped_namespace():
    # Fails if an empty namespace turns the View search into a cross-tenant scan.
    neo = _RecNeo4j(rows=[{"cosine": 0.9}])
    out = MemoryQueryRepository(neo).search_counted_view_embeddings(
        [0.1, 0.2], None, embed_version="v1", limit=3)  # type: ignore[arg-type]
    assert out == [] and neo.calls == []


# ------------------------------------------------------------------ online

@pytest.fixture
def env(test_neo4j_repo):
    ns = f"cvsel-{uuidlib.uuid4().hex[:10]}"
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    yield test_neo4j_repo, adapter, ns
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.group_id = $ns OR n.namespace_key = $ns DETACH DELETE n", {"ns": ns})


@pytest.mark.online
def test_view_selection_marker_mismatch_sweeps_the_namespace(env):
    repo, adapter, ns = env
    at = datetime(2026, 7, 1, tzinfo=UTC).isoformat()
    adapter.mark_counting_flag_state(ns, enabled=True, at=at)
    assert ns in adapter.counting_flag_sweep_targets(
        enabled=True, namespaces=[ns], limit=10, view_selection=True)
    assert ns not in adapter.counting_flag_sweep_targets(enabled=True, namespaces=[ns], limit=10)

    adapter.mark_counting_flag_state(ns, enabled=True, at=at, view_selection=True)
    assert ns not in adapter.counting_flag_sweep_targets(
        enabled=True, namespaces=[ns], limit=10, view_selection=True)
    assert ns in adapter.counting_flag_sweep_targets(enabled=True, namespaces=[ns], limit=10)

    # A marker written before this flag has no property: it reads as off.
    repo.execute("MATCH (m:CountingFlagState {namespace_key: $ns}) REMOVE m.view_selection",
                 {"ns": ns})
    assert ns not in adapter.counting_flag_sweep_targets(enabled=True, namespaces=[ns], limit=10)


@pytest.mark.online
def test_search_counted_view_embeddings_filters_and_orders(env):
    repo, adapter, ns = env
    other = f"{ns}-other"
    rows = [
        ("good", ns, "counted", True, None, "v1", [1.0, 0.0]),
        ("close", ns, "counted", True, None, "v1", [0.9, 0.1]),
        ("old-version", ns, "counted", True, None, "v0", [1.0, 0.0]),
        ("not-counted", ns, None, True, None, "v1", [1.0, 0.0]),
        ("noncurrent", ns, "counted", False, None, "v1", [1.0, 0.0]),
        ("other-ns", other, "counted", True, None, "v1", [1.0, 0.0]),
    ]
    for name, group, basis, current, _, version, vec in rows:
        repo.execute(
            "CREATE (v:Entity {uuid: $u, group_id: $g, view_kind: 'scalar_state', "
            "view_subject_uuid: 'ent', ss_attribute: $a, ss_kind: 'count', view_current: $cur, "
            "view_key: $u, count_view_embed_version: $ver, count_view_embedding: $vec}) "
            "FOREACH (_ IN CASE WHEN $basis IS NULL THEN [] ELSE [1] END | "
            "SET v.scalar_basis = $basis)",
            {"u": f"{ns}-{name}", "g": group, "a": name, "cur": current, "ver": version,
             "vec": vec, "basis": basis})
    try:
        hits = adapter.search_counted_view_embeddings([1.0, 0.0], ns, embed_version="v1")
        assert [h["ss_attribute"] for h in hits] == ["good", "close"]
        assert hits[0]["cosine"] >= hits[1]["cosine"]
        assert hits[0]["ss_scope"] == "" and hits[0]["ss_unit"] == ""
    finally:
        repo.execute("MATCH (n) WHERE n.group_id = $ns DETACH DELETE n", {"ns": other})
