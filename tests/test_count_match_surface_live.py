"""Live proof of the unit 4R match surface Cypher: the observation search returns the object/family
columns and the slot View lookup returns the match-audit properties. Every test uses a fresh
namespace and removes everything it created.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter

_VEC = [1.0, 0.0, 0.0]


@pytest.fixture
def env(test_neo4j_repo):
    ns = f"cmsurf-{uuidlib.uuid4().hex[:10]}"
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    yield test_neo4j_repo, adapter, ns
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.namespace = $ns OR n.group_id = $ns DETACH DELETE n", {"ns": ns})


@pytest.mark.online
def test_search_assertion_embeddings_returns_count_object_and_family_coalesced(env):
    repo, adapter, ns = env
    repo.execute(
        """
        UNWIND $rows AS r
        CREATE (a:TypedAssertion {assertion_id: r.id, namespace: $ns, group_id: $ns,
                                  name_embedding: $vec, stated_span: r.id, operation: 'occurrence',
                                  subject_uuid: 'self', attribute: 'kit_count', scope: '',
                                  value_kind: 'count', unit: '', value: 1, superseded: false,
                                  binding_pending: false})
        FOREACH (_ IN CASE WHEN r.obj IS NULL THEN [] ELSE [1] END | SET a.count_object = r.obj)
        FOREACH (_ IN CASE WHEN r.fam IS NULL THEN [] ELSE [1] END | SET a.count_family = r.fam)
        """,
        {"ns": ns, "vec": _VEC, "rows": [
            {"id": f"{ns}:full", "obj": "model kits", "fam": "model kit"},
            {"id": f"{ns}:objonly", "obj": "model kit", "fam": None},
            {"id": f"{ns}:neither", "obj": None, "fam": None},
        ]},
    )
    hits = adapter.search_assertion_embeddings(_VEC, limit=10, namespaces=[ns])
    got = {h["assertion_id"].split(":", 1)[1]: (h["count_object"], h["count_family"])
           for h in hits}
    assert got == {
        "full": ("model kits", "model kit"),
        "objonly": ("model kit", ""),
        "neither": ("", ""),
    }


def _seed_view(repo, ns, **props):
    repo.execute(
        """
        CREATE (n:Entity {uuid: $uuid, name: 'model_kit_count: 3', group_id: $ns, namespace: $ns,
                          view_kind: 'scalar_state', view_subject_uuid: $subj,
                          ss_attribute: 'model_kit_count', ss_kind: 'count', ss_scope: '',
                          ss_unit: '', ss_value: 3, view_current: true, view_key: $vk})
        SET n += $props
        """,
        {"uuid": f"{ns}:{props.pop('_id')}", "ns": ns, "subj": props.pop("_subj"),
         "vk": f"vk-{uuidlib.uuid4().hex[:6]}", "props": props},
    )


@pytest.mark.online
def test_fetch_current_scalar_view_for_slot_returns_match_audit_properties(env):
    repo, adapter, ns = env
    _seed_view(repo, ns, _id="with", _subj=f"{ns}-with", count_uncertain=2.0,
               count_actions='{"bought": 2}', count_items='["Zaku"]')
    row = adapter.fetch_current_scalar_view_for_slot(
        subject_uuid=f"{ns}-with", attribute="model_kit_count", scope="", value_kind="count",
        unit="", namespace=ns)
    assert row is not None and row["attribute"] == "model_kit_count"
    assert row["count_uncertain"] == 2.0
    assert row["count_actions"] == '{"bought": 2}'
    assert row["count_items"] == '["Zaku"]'


@pytest.mark.online
def test_fetch_current_scalar_view_for_slot_audit_properties_none_when_absent(env):
    repo, adapter, ns = env
    _seed_view(repo, ns, _id="without", _subj=f"{ns}-without")
    row = adapter.fetch_current_scalar_view_for_slot(
        subject_uuid=f"{ns}-without", attribute="model_kit_count", scope="", value_kind="count",
        unit="", namespace=ns)
    assert row is not None and row["uuid"] == f"{ns}:without"
    assert row["count_uncertain"] is None
    assert row["count_actions"] is None
    assert row["count_items"] is None
