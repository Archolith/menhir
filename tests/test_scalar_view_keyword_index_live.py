"""Scalar Views stay out of Graphiti's shared :Entity full-text index (node_name_and_summary).

Lucene term weights are index-wide, so every scalar View version written with its surface text in
`name`/`summary` shifted the BM25 ranks of ordinary entities in every namespace. In the LME counting
A/B (2026-10-17) that pushed a BM25-only summary entity from rank 5 to 7, under the similarity floor.
Scalar kinds now carry their text in view_name/view_summary; these tests pin that ordinary BM25
scores do not move when scalar Views are written, that readers still get the text, and that the
activation backfill moves text written by pre-change code.

The online tests run only with `--run-online` against the test Neo4j.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.view_models import CounterKind, ScalarHistoryKind, ScalarStateKind
from menhir.infrastructure.view_repository import ViewRepository

# Graphiti's own DDL for the node full-text index (graphiti_core graph_queries).
_GRAPHITI_NODE_FULLTEXT = (
    "CREATE FULLTEXT INDEX node_name_and_summary IF NOT EXISTS "
    "FOR (n:Entity) ON EACH [n.name, n.summary, n.group_id]"
)


def test_scalar_kinds_are_not_keyword_indexed():
    assert ScalarStateKind.keyword_indexed is False
    assert ScalarHistoryKind.keyword_indexed is False
    # Other kinds keep their retrieval surface in the index.
    assert CounterKind.keyword_indexed is True


@pytest.fixture
def live(test_neo4j_repo):
    adapter = MemoryGraphAdapter(neo4j=test_neo4j_repo)
    adapter.bootstrap_phase_one()
    adapter.activate_scalar_state()
    test_neo4j_repo.execute(_GRAPHITI_NODE_FULLTEXT)
    test_neo4j_repo.execute("CALL db.awaitIndexes(300)")
    return adapter, ViewRepository(test_neo4j_repo), test_neo4j_repo


def _bm25(repo, ns: str, terms: str) -> dict[str, float]:
    """Graphiti's query shape: the namespace clause AND the free-text terms."""
    rows = repo.execute(
        "CALL db.index.fulltext.queryNodes('node_name_and_summary', $q) YIELD node, score "
        "RETURN node.uuid AS uuid, score",
        {"q": f'group_id:"{ns}" AND ({terms})'},
    )
    return {str(r["uuid"]): float(r["score"]) for r in rows}


def _text_scores(repo, terms: str, uuids: set[str]) -> dict[str, float]:
    """Free-text terms only: scores that depend on name/summary statistics alone. (Any node in a
    namespace adds to its group_id term, which shifts every score in that namespace equally.)"""
    rows = repo.execute(
        "CALL db.index.fulltext.queryNodes('node_name_and_summary', $q) YIELD node, score "
        "WHERE node.uuid IN $us RETURN node.uuid AS uuid, score",
        {"q": terms, "us": sorted(uuids)},
    )
    return {str(r["uuid"]): float(r["score"]) for r in rows}


def _ranked(scores: dict[str, float]) -> list[str]:
    return sorted(scores, key=lambda u: (-scores[u], u))


def _entity(repo, ns: str, name: str, summary: str) -> str:
    uid = str(uuidlib.uuid4())
    repo.execute(
        "CREATE (n:Entity {uuid: $u, group_id: $ns, name: $name, summary: $summary})",
        {"u": uid, "ns": ns, "name": name, "summary": summary},
    )
    return uid


@pytest.mark.online
def test_scalar_view_writes_leave_ordinary_bm25_scores_unchanged(live):
    adapter, views, repo = live
    ns = f"kwidx-{uuidlib.uuid4().hex[:8]}"
    terms = "How many model kits have I worked on or bought"
    building = _entity(repo, ns, "model building", "The user has been getting back into model building.")
    summary = _entity(repo, ns, "user", "Finished a Revell F-15 kit and a Spitfire kit; bought a B-29 kit.")
    before = _bm25(repo, ns, terms)
    assert set(before) == {building, summary}
    text_before = _text_scores(repo, terms, set(before))

    subj = f"ent-{uuidlib.uuid4().hex[:8]}"
    slot = dict(attribute="model_kit_count", scope="", value_kind="count", unit="")
    first = views.record_scalar_state(subject="user", subject_uuid=subj, value=2, namespace=ns,
                                      valid_at="2026-07-01T00:00:00+00:00", **slot)
    second = views.record_scalar_state(subject="user", subject_uuid=subj, value=4, namespace=ns,
                                       valid_at="2026-07-02T00:00:00+00:00", **slot)
    assert first.get("created") is True, first
    assert second.get("superseded") is True, second

    after = _bm25(repo, ns, terms)
    # No View version is a keyword hit, the namespace's rank order is unchanged, and the free-text
    # scores (name/summary statistics) are exactly as before.
    assert set(after) == set(before)
    assert _ranked(after) == _ranked(before)
    assert _text_scores(repo, terms, set(before)) == text_before

    # The text moved, not vanished: the node carries it and the readers return it.
    rows = repo.execute(
        "MATCH (n:Entity {view_kind:'scalar_state', view_subject_uuid:$u}) "
        "RETURN n.name AS name, n.summary AS summary, n.view_name AS view_name, "
        "n.view_summary AS view_summary, n.content AS content, "
        "coalesce(n.view_current, true) AS current",
        {"u": subj},
    )
    assert len(rows) == 2
    for row in rows:
        assert row["name"] == "" and row["summary"] == ""
        assert "model kit count" in row["view_name"]
        assert row["view_summary"] and row["content"] == row["view_summary"]
    current = adapter.fetch_current_scalar_view_for_slot(
        subject_uuid=subj, namespace=ns, **slot)
    assert current is not None and "model kit count: 4" in str(current["name"]), current

    # Positive control: the same text as an indexed :Entity does move the ordinary scores, so the
    # equality above is a real check, not an insensitive probe.
    _entity(repo, ns, "user's model kit count: 4. current model kit count = 4.",
            "user - model kit count = 4")
    assert _text_scores(repo, terms, set(before)) != text_before


@pytest.mark.online
def test_activation_backfill_moves_indexed_text_out(live):
    adapter, _views, repo = live
    ns = f"kwidx-{uuidlib.uuid4().hex[:8]}"
    token = f"zq{uuidlib.uuid4().hex[:10]}"
    # A View written by pre-change code: text in name/summary, no view_name.
    legacy = str(uuidlib.uuid4())
    repo.execute(
        "CREATE (n:Entity {uuid: $u, group_id: $ns, is_view: true, view_kind: 'scalar_history', "
        "name: $name, summary: $summary})",
        {"u": legacy, "ns": ns, "name": f"user's {token} history", "summary": f"{token} entries"},
    )
    # Rolling deploy: newer code moved the text, then an older writer re-set name/summary.
    mixed = str(uuidlib.uuid4())
    repo.execute(
        "CREATE (n:Entity {uuid: $u, group_id: $ns, is_view: true, view_kind: 'scalar_state', "
        "name: $name, summary: 'newer summary', view_name: 'older name', "
        "view_summary: 'older summary'})",
        {"u": mixed, "ns": ns, "name": f"user's {token} count: 3. current {token} count = 3."},
    )
    assert set(_bm25(repo, ns, token)) == {legacy, mixed}

    adapter.activate_scalar_state()

    rows = {
        r["uuid"]: r for r in repo.execute(
            "MATCH (n:Entity) WHERE n.uuid IN $us RETURN n.uuid AS uuid, n.name AS name, "
            "n.summary AS summary, n.view_name AS view_name, n.view_summary AS view_summary",
            {"us": [legacy, mixed]},
        )
    }
    assert rows[legacy]["name"] == "" and rows[legacy]["summary"] == ""
    assert rows[legacy]["view_name"] == f"user's {token} history"
    assert rows[legacy]["view_summary"] == f"{token} entries"
    # The older writer's text is the newer one, so it wins over the stale view_name.
    assert rows[mixed]["view_name"] == f"user's {token} count: 3. current {token} count = 3."
    assert rows[mixed]["view_summary"] == "newer summary"
    assert rows[mixed]["name"] == "" and rows[mixed]["summary"] == ""
    assert _bm25(repo, ns, token) == {}

    # Idempotent: a second activation changes nothing.
    adapter.activate_scalar_state()
    again = repo.execute(
        "MATCH (n:Entity) WHERE n.uuid IN $us RETURN n.view_name AS view_name ORDER BY n.uuid",
        {"us": [legacy, mixed]},
    )
    assert sorted(r["view_name"] for r in again) == sorted(
        [rows[legacy]["view_name"], rows[mixed]["view_name"]])
