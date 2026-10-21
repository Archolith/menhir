"""Two-tier self Phase 4: the per-fork review report is read-only and lists only ordinary forks."""

from __future__ import annotations

import uuid as uuidlib

import pytest

from menhir.domain.namespace import namespace_to_group_id
from menhir.domain.self_identity import inferred_self_uuid_for_namespace, self_uuid_for_namespace
from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from scripts.report_self_forks import build_report, manifest_template, render_markdown


def _counts(repo, tag: str) -> tuple[list[str], list[str]]:
    """Every tagged node's and incident relationship's full property map, order-independent."""
    rows = repo.execute(
        "MATCH (n) WHERE n.test_tag = $tag OPTIONAL MATCH (n)-[r]-() "
        "RETURN collect(DISTINCT properties(n)) AS nodes, "
        "collect(DISTINCT properties(r)) AS rels",
        params={"tag": tag},
    )
    return (sorted(repr(sorted(p.items())) for p in rows[0]["nodes"]),
            sorted(repr(sorted(p.items())) for p in rows[0]["rels"]))


@pytest.mark.online
def test_report_lists_ordinary_forks_only_and_writes_nothing(test_neo4j_repo):
    repo = test_neo4j_repo
    tag = f"test-fork-report-{uuidlib.uuid4()}"
    ns = f"ns-forks-{uuidlib.uuid4().hex[:8]}"
    group = namespace_to_group_id(ns)
    anchored = self_uuid_for_namespace(ns)
    inferred = inferred_self_uuid_for_namespace(ns)
    fork_a, fork_b, other, ep = (f"{tag}-{s}" for s in ("a", "b", "other", "ep"))
    try:
        repo.execute(
            """
            CREATE (s:Entity {uuid: $anchored, name: 'user', is_self: true, entity_role: 'self',
                              group_id: $group, namespace: $ns, test_tag: $tag})
            CREATE (i:Entity {uuid: $inferred, name: 'user-inferred', entity_role: 'inferred_self',
                              self_tier: 'inferred', group_id: $group, namespace: $ns, test_tag: $tag})
            CREATE (a:Entity {uuid: $fork_a, name: 'user', group_id: $group, namespace: $ns,
                              test_tag: $tag})
            CREATE (b:Entity {uuid: $fork_b, name: 'User', group_id: $group, namespace: $ns,
                              test_tag: $tag})
            CREATE (o:Entity {uuid: $other, name: 'postcards', group_id: $group, namespace: $ns,
                              test_tag: $tag})
            CREATE (a)-[:RELATES_TO {uuid: $tag + '-e1', name: 'SENT', fact: 'user sent postcards',
                                     created_at: datetime(), episodes: [$ep], group_id: $group}]->(o)
            CREATE (o)-[:RELATES_TO {uuid: $tag + '-e2', name: 'LINK', created_at: datetime(),
                                     group_id: $group}]->(a)
            CREATE (e:Episodic {uuid: $ep, content: 'I sent postcards', source: 'agent',
                                created_at: datetime(), test_tag: $tag})
            CREATE (e)-[:MENTIONS]->(a)
            CREATE (s)-[:RELATES_TO {uuid: $tag + '-e3', name: 'LIKES', fact: 'user likes tea',
                                     created_at: datetime(), group_id: $group}]->(o)
            """,
            params={"anchored": anchored, "inferred": inferred, "fork_a": fork_a, "fork_b": fork_b,
                    "other": other, "ep": ep, "group": group, "ns": ns, "tag": tag},
        )
        before = _counts(repo, tag)
        lifecycle = EpisodeLifecycleRepository()
        lifecycle.neo4j = repo

        report = build_report(repo, lifecycle, ns, limit=50)

        assert _counts(repo, tag) == before  # read-only
        by_uuid = {c["uuid"]: c for c in report["candidates"]}
        assert set(by_uuid) == {fork_a, fork_b}  # never the anchored or inferred self
        assert report["anchored_self_uuid"] == anchored
        assert report["inferred_self_uuid"] == inferred
        assert report["fold_status"].startswith("blocked")
        facts = by_uuid[fork_a]["facts"]
        assert [f["fact"] for f in facts] == ["user sent postcards"]  # fact-less link excluded
        assert facts[0]["direction"] == "out" and facts[0]["other"]["uuid"] == other
        assert facts[0]["episodes"] == [ep]
        assert [e["episode_uuid"] for e in by_uuid[fork_a]["source_episodes"]] == [ep]
        assert by_uuid[fork_b]["facts"] == [] and by_uuid[fork_b]["source_episodes"] == []

        manifest = manifest_template(report)
        assert manifest["target"] == "inferred" and manifest["target_uuid"] == inferred
        assert {f["uuid"] for f in manifest["forks"]} == {fork_a, fork_b}
        assert all(f["approved"] is False for f in manifest["forks"])
        assert "user sent postcards" in render_markdown(report)
    finally:
        repo.execute("MATCH (n) WHERE n.test_tag = $tag DETACH DELETE n", params={"tag": tag})
