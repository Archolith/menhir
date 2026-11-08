"""Live: the HISTORY_ENTRY listing shows an approximate entry; provenance never does (#519 Phase 1).

The listing filters are Cypher built from the operation table, so only a real database proves the
page and the total agree on the new operation.
"""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.infrastructure.view_repository import ViewRepository

pytestmark = pytest.mark.online


@pytest.fixture
def env(test_neo4j_repo):
    ns = f"apx-{uuidlib.uuid4().hex[:10]}"
    yield test_neo4j_repo, ns
    test_neo4j_repo.execute(
        "MATCH (n) WHERE n.namespace STARTS WITH $ns OR n.group_id STARTS WITH $ns "
        "DETACH DELETE n", {"ns": ns})


def _seed(repo, ns):
    """One View: an absolute anchor, then a newer approximate and an unknown-operation row, each
    with a HISTORY_ENTRY edge; the two non-anchors are also CONTRIBUTED_TO edges."""
    view = f"{ns}-view"
    repo.execute(
        """
        CREATE (v:Entity {uuid: $view, group_id: $ns, name: 'v'})
        CREATE (ab:TypedAssertion {assertion_id: $ns + ':abs', operation: 'absolute',
                                   namespace: $ns, value: 1250,
                                   valid_at: datetime('2026-07-01T00:00:00Z')})
        CREATE (ap:TypedAssertion {assertion_id: $ns + ':approx', operation: 'approximate',
                                   namespace: $ns, value: 1300,
                                   valid_at: datetime('2026-07-02T00:00:00Z')})
        CREATE (zz:TypedAssertion {assertion_id: $ns + ':zz', operation: 'zz_future',
                                   namespace: $ns, value: 9,
                                   valid_at: datetime('2026-07-03T00:00:00Z')})
        CREATE (v)-[:CURRENT_ANCHOR]->(ab)
        CREATE (v)-[:CONTRIBUTED_TO]->(ap)
        CREATE (v)-[:CONTRIBUTED_TO]->(zz)
        CREATE (v)-[:HISTORY_ENTRY {ordinal: 0, operation: 'absolute'}]->(ab)
        CREATE (v)-[:HISTORY_ENTRY {ordinal: 1, operation: 'approximate'}]->(ap)
        CREATE (v)-[:HISTORY_ENTRY {ordinal: 2, operation: 'zz_future'}]->(zz)
        """,
        {"view": view, "ns": ns},
    )
    return view


def _ids(entries):
    return [e["assertion_id"].split(":", 1)[1] for e in entries]


@pytest.mark.parametrize("include_occurrences", [False, True])
def test_history_lists_the_approximate_entry_and_not_the_unknown_one(env, include_occurrences):
    repo, ns = env
    view = _seed(repo, ns)
    page = ViewRepository(repo).list_scalar_history_entries(
        view_uuid=view, namespace=ns, include_occurrences=include_occurrences)
    assert _ids(page["entries"]) == ["abs", "approx"]
    assert page["total"] == 2 and page["next_offset"] is None


@pytest.mark.parametrize("include_occurrences", [False, True])
def test_provenance_never_lists_an_approximate_contributor(env, include_occurrences):
    repo, ns = env
    view = _seed(repo, ns)
    page = ViewRepository(repo).fetch_scalar_authority_contributors(
        view_uuid=view, namespace=ns, include_occurrences=include_occurrences)
    assert _ids(page["contributors"]) == ["abs"]
    assert page["total"] == 1
