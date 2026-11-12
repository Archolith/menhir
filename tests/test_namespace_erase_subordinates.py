"""Namespace erase removes :Todo / :WorkArtifact and their subordinate nodes (#476).

Todo and WorkArtifact carry ``namespace``; their TodoLocation / ArtifactSource / ArtifactLocation /
OpenQuestion / ArtifactDeclaration children carry neither namespace nor group_id and used to be
orphaned by a namespace delete. NAMESPACE_SUBORDINATES_CALL expands the doomed set to children with
no owner outside the doomed set. Delete, count, count-by-label and uuid capture share that logic.

Seeded with Cypher in the shape NAMESPACE_SUBORDINATES_CALL matches. Online only.
"""

from __future__ import annotations

import uuid as uuidlib
from typing import Any

import pytest

from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.memory_queries import MemoryQueryRepository


# ------------------------------------------------------------------ helpers
def _seed_todo(repo: Any, tag: str, ns: str, key: str) -> dict[str, str]:
    """Todo(namespace=ns) -HAS_LOCATION-> two TodoLocation (children carry no namespace)."""
    ids = {"todo": f"{tag}-{key}-todo", "loc1": f"{tag}-{key}-tloc1", "loc2": f"{tag}-{key}-tloc2"}
    repo.execute(
        """
        CREATE (t:Todo {uuid:$todo, namespace:$ns, test_tag:$tag})
        CREATE (t)-[:HAS_LOCATION]->(:TodoLocation {uuid:$loc1, test_tag:$tag})
        CREATE (t)-[:HAS_LOCATION]->(:TodoLocation {uuid:$loc2, test_tag:$tag})
        """,
        params={**ids, "ns": ns, "tag": tag},
    )
    return ids


def _seed_artifact(repo: Any, tag: str, ns: str, key: str) -> dict[str, str]:
    """WorkArtifact(namespace=ns) with one child of each subordinate label, edges as matched by
    NAMESPACE_SUBORDINATES_CALL."""
    ids = {
        "wa": f"{tag}-{key}-wa",
        "src": f"{tag}-{key}-src",
        "loc": f"{tag}-{key}-aloc",
        "oq": f"{tag}-{key}-oq",
        "decl": f"{tag}-{key}-decl",
    }
    repo.execute(
        """
        CREATE (w:WorkArtifact {uuid:$wa, namespace:$ns, test_tag:$tag})
        CREATE (w)-[:EMBODIED_IN]->(:ArtifactSource {uuid:$src, test_tag:$tag})
        CREATE (w)-[:HAS_LOCATION]->(:ArtifactLocation {uuid:$loc, test_tag:$tag})
        CREATE (w)-[:HAS_OPEN_QUESTION]->(:OpenQuestion {uuid:$oq, test_tag:$tag})
        CREATE (w)-[:DECLARES]->(:ArtifactDeclaration {uuid:$decl, test_tag:$tag})
        """,
        params={**ids, "ns": ns, "tag": tag},
    )
    return ids


def _seed_entity(repo: Any, tag: str, gid: str) -> str:
    """A direct group_id-partition node, so the count mixes direct and subordinate nodes."""
    uid = f"{tag}-entity"
    repo.execute(
        "CREATE (:Entity {uuid:$u, name:'e', group_id:$g, test_tag:$tag})",
        params={"u": uid, "g": gid, "tag": tag},
    )
    return uid


def _exists(repo: Any, node_uuid: str) -> bool:
    rows = repo.execute("MATCH (n) WHERE n.uuid = $u RETURN count(n) AS c", params={"u": node_uuid})
    return int(rows[0]["c"]) > 0


def _cleanup(repo: Any, tag: str, ns: str) -> None:
    repo.execute("MATCH (n) WHERE n.test_tag = $t DETACH DELETE n", params={"t": tag})
    repo.execute(
        "MATCH (n) WHERE n.namespace_key = $ns OR n.namespace = $ns OR n.group_id = $ns "
        "DETACH DELETE n",
        params={"ns": ns},
    )
    repo.execute(
        "MATCH (r:ScalarProjectionRepair) WHERE r.operation_id CONTAINS $t DETACH DELETE r",
        params={"t": tag},
    )


def _delete(repo: Any, gid: str, ns: str, tag: str) -> dict[str, Any]:
    # group_id differs from the namespace so only the namespace-keyed terms (and subordinate
    # expansion) can account for Todo/WorkArtifact and their children.
    return MemoryQueryRepository(repo).delete_namespace_with_scalar_cascade(
        gid, ns, operation_id=f"op-{tag}"
    )


# ------------------------------------------------------------------ tests
@pytest.mark.online
def test_namespace_delete_removes_todo_workartifact_and_all_children(test_neo4j_repo) -> None:
    """Breaks if Todo/WorkArtifact leave NAMESPACE_ERASE_PREDICATE, or any subordinate label or
    edge type (HAS_LOCATION, EMBODIED_IN, HAS_OPEN_QUESTION, DECLARES) is dropped from
    NAMESPACE_SUBORDINATES_CALL: the children would be orphaned holding the erased data."""
    repo = test_neo4j_repo
    tag = f"test-nse-delete-{uuidlib.uuid4()}"
    ns, gid = f"{tag}-ns", f"{tag}-gid"
    try:
        todo = _seed_todo(repo, tag, ns, "a")
        art = _seed_artifact(repo, tag, ns, "a")
        every = [*todo.values(), *art.values()]
        assert all(_exists(repo, u) for u in every)

        result = _delete(repo, gid, ns, tag)

        for u in every:
            assert _exists(repo, u) is False, f"{u} survived namespace delete"
        assert result["deleted"] == len(every)
    finally:
        _cleanup(repo, tag, ns)


@pytest.mark.online
def test_subordinate_owned_by_other_namespace_artifact_survives(test_neo4j_repo) -> None:
    """A child also owned by a WorkArtifact in ANOTHER namespace must survive, as must that
    owner. Breaks if the `NOT EXISTS { (o)-[...]->(s) WHERE NOT o IN direct }` owner guard is
    removed (the other namespace would lose its declaration). Unshared siblings still go."""
    repo = test_neo4j_repo
    tag = f"test-nse-shared-{uuidlib.uuid4()}"
    ns, gid = f"{tag}-ns", f"{tag}-gid"
    other_ns = f"{tag}-other-ns"
    try:
        art = _seed_artifact(repo, tag, ns, "a")
        other_wa = f"{tag}-other-wa"
        repo.execute(
            """
            MATCH (d:ArtifactDeclaration {uuid:$decl})
            CREATE (w:WorkArtifact {uuid:$ow, namespace:$other_ns, test_tag:$tag})
            CREATE (w)-[:DECLARES]->(d)
            """,
            params={"decl": art["decl"], "ow": other_wa, "other_ns": other_ns, "tag": tag},
        )

        result = _delete(repo, gid, ns, tag)

        assert _exists(repo, art["wa"]) is False
        assert _exists(repo, art["decl"]) is True, "a child owned from another namespace was deleted"
        assert _exists(repo, other_wa) is True
        for key in ("src", "loc", "oq"):
            assert _exists(repo, art[key]) is False, key
        assert result["deleted"] == 4  # wa + src + loc + oq; the shared decl is spared
        rows = repo.execute(
            "MATCH (:WorkArtifact {uuid:$ow})-[:DECLARES]->(d:ArtifactDeclaration {uuid:$decl}) "
            "RETURN count(d) AS c",
            params={"ow": other_wa, "decl": art["decl"]},
        )
        assert rows[0]["c"] == 1
    finally:
        _cleanup(repo, tag, ns)
        _cleanup(repo, tag, other_ns)


@pytest.mark.online
def test_count_namespace_equals_deleted_and_by_label_is_correct(test_neo4j_repo) -> None:
    """count_namespace is computed BEFORE the delete and must equal the nodes the delete removes;
    count_namespace_by_label must break the same set down by label. Breaks if the count/by-label
    queries stop using NAMESPACE_SUBORDINATES_CALL or the predicate (dry-run under-reports the
    blast radius), or NAMESPACE_PRIMARY_LABEL_CASE loses a subordinate label."""
    repo = test_neo4j_repo
    tag = f"test-nse-count-{uuidlib.uuid4()}"
    ns, gid = f"{tag}-ns", f"{tag}-gid"
    try:
        todo = _seed_todo(repo, tag, ns, "a")  # Todo + 2 TodoLocation
        art = _seed_artifact(repo, tag, ns, "a")  # WorkArtifact + 4 children
        _seed_entity(repo, tag, gid)  # direct group_id partition node
        adapter = MemoryGraphAdapter(neo4j=repo)
        expected_total = len(todo) + len(art) + 1

        counted = adapter.count_namespace(gid, namespace=ns)
        by_label = adapter.count_namespace_by_label(gid, namespace=ns)
        before = repo.execute(
            "MATCH (n) WHERE n.test_tag = $t RETURN count(n) AS c", params={"t": tag}
        )[0]["c"]

        result = _delete(repo, gid, ns, tag)

        after = repo.execute(
            "MATCH (n) WHERE n.test_tag = $t RETURN count(n) AS c", params={"t": tag}
        )[0]["c"]
        assert after == 0
        assert counted == before - after == result["deleted"] == expected_total
        assert by_label == {
            "Todo": 1,
            "TodoLocation": 2,
            "WorkArtifact": 1,
            "ArtifactSource": 1,
            "ArtifactLocation": 1,
            "OpenQuestion": 1,
            "ArtifactDeclaration": 1,
            "Entity": 1,
        }
        assert sum(by_label.values()) == counted
    finally:
        _cleanup(repo, tag, ns)


@pytest.mark.online
def test_count_excludes_shared_subordinate_consistently_with_delete(test_neo4j_repo) -> None:
    """The count must not include a child the delete will spare. Breaks if count_namespace
    diverges from delete (e.g. counts subs without the owner guard), so the safety gate would
    report a different blast radius than the delete."""
    repo = test_neo4j_repo
    tag = f"test-nse-countshared-{uuidlib.uuid4()}"
    ns, gid = f"{tag}-ns", f"{tag}-gid"
    other_ns = f"{tag}-other-ns"
    try:
        art = _seed_artifact(repo, tag, ns, "a")
        repo.execute(
            "MATCH (d:ArtifactDeclaration {uuid:$decl}) "
            "CREATE (w:WorkArtifact {uuid:$ow, namespace:$o, test_tag:$tag})-[:DECLARES]->(d)",
            params={"decl": art["decl"], "ow": f"{tag}-ow", "o": other_ns, "tag": tag},
        )
        adapter = MemoryGraphAdapter(neo4j=repo)

        counted = adapter.count_namespace(gid, namespace=ns)
        by_label = adapter.count_namespace_by_label(gid, namespace=ns)
        result = _delete(repo, gid, ns, tag)

        assert counted == result["deleted"] == 4
        assert "ArtifactDeclaration" not in by_label
    finally:
        _cleanup(repo, tag, ns)
        _cleanup(repo, tag, other_ns)


@pytest.mark.online
def test_todo_in_another_namespace_is_untouched(test_neo4j_repo) -> None:
    """A Todo (and its location) in another namespace is neither deleted nor counted. Breaks if
    the Todo/WorkArtifact predicate terms stop being scoped to `n.namespace = $namespace`."""
    repo = test_neo4j_repo
    tag = f"test-nse-isolation-{uuidlib.uuid4()}"
    ns, gid = f"{tag}-ns", f"{tag}-gid"
    other_ns = f"{tag}-other-ns"
    try:
        mine = _seed_todo(repo, tag, ns, "mine")
        theirs = _seed_todo(repo, tag, other_ns, "theirs")
        their_art = _seed_artifact(repo, tag, other_ns, "theirs")
        adapter = MemoryGraphAdapter(neo4j=repo)

        assert adapter.count_namespace(gid, namespace=ns) == len(mine)
        assert adapter.count_namespace_by_label(gid, namespace=ns) == {"Todo": 1, "TodoLocation": 2}
        assert not set(theirs.values()) & set(adapter.capture_namespace_uuids(gid, namespace=ns))

        result = _delete(repo, gid, ns, tag)

        assert result["deleted"] == len(mine)
        for u in mine.values():
            assert _exists(repo, u) is False
        for u in [*theirs.values(), *their_art.values()]:
            assert _exists(repo, u) is True, f"{u} in another namespace was deleted"
        rows = repo.execute(
            "MATCH (:Todo {uuid:$t})-[:HAS_LOCATION]->(l:TodoLocation) RETURN count(l) AS c",
            params={"t": theirs["todo"]},
        )
        assert rows[0]["c"] == 2
    finally:
        _cleanup(repo, tag, ns)
        _cleanup(repo, tag, other_ns)


@pytest.mark.online
def test_capture_namespace_uuids_includes_todo_workartifact_and_children(test_neo4j_repo) -> None:
    """capture runs BEFORE erasure to key sidecar purges, so it must name everything the delete
    destroys. Breaks if Todo/WorkArtifact uuids (or the expanded subordinates) are omitted, or if
    another namespace's uuids leak in."""
    repo = test_neo4j_repo
    tag = f"test-nse-capture-{uuidlib.uuid4()}"
    ns, gid = f"{tag}-ns", f"{tag}-gid"
    other_ns = f"{tag}-other-ns"
    try:
        todo = _seed_todo(repo, tag, ns, "a")
        art = _seed_artifact(repo, tag, ns, "a")
        theirs = _seed_todo(repo, tag, other_ns, "theirs")
        adapter = MemoryGraphAdapter(neo4j=repo)

        captured = adapter.capture_namespace_uuids(gid, namespace=ns)

        assert todo["todo"] in captured
        assert art["wa"] in captured
        assert set(todo.values()) | set(art.values()) <= set(captured)
        assert len(captured) == len(set(captured))
        assert not set(theirs.values()) & set(captured)
    finally:
        _cleanup(repo, tag, ns)
        _cleanup(repo, tag, other_ns)
