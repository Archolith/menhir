"""Conservative legacy source-link repair decisions and manifest integrity."""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path
from typing import Any

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1] / "scripts" / "backfill_legacy_retention.py"
)
SPEC = importlib.util.spec_from_file_location("backfill_legacy_retention", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _candidate(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source_uuid": "source",
        "source_name": "episode",
        "source_state": "READY",
        "source_namespace": "default",
        "source_group_id": None,
        "resolved_episode_uuid": "graphiti",
        "graphiti_uuid": "graphiti",
        "graphiti_name": "episode",
        "source_content": "same text",
        "graphiti_content": "same text",
        "source_created_at": "2026-01-01T00:00:00Z",
        "graphiti_created_at": "2026-01-01T00:00:01Z",
        "content_matches": True,
        "chronology_valid": True,
        "graphiti_namespace": "default",
        "graphiti_group_id": None,
        "entity_uuid": "entity",
        "entity_namespace": "default",
        "entity_group_id": None,
        "structure_role": None,
        "is_evidence_projection": None,
        "legacy_structural": False,
        "merge_audit_count": 0,
        "already_linked": False,
        "existing_direct": None,
    }
    row.update(changes)
    return row


class FakeRepo:
    database = "neo4j"

    def __init__(
        self, flags: list[dict[str, Any]], sources: list[dict[str, Any]]
    ) -> None:
        self.flags = flags
        self.sources = sources
        self.writes: list[dict[str, Any]] = []

    def execute(
        self, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if query == MODULE.FLAGS:
            return self.flags[: params["limit"]]
        if query == MODULE.SOURCES:
            return self.sources[: params["limit"]]
        self.writes.append({"query": query, "params": params})
        return [{"matched": 1, "direct_values": [True]}]


def _read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_inventory_includes_all_flags_and_unflagged_source_candidates(
    tmp_path: Path,
) -> None:
    flags = [
        {
            "uuid": "episodic-retention-only",
            "labels": ["Episodic"],
            "bootstrap_scope": None,
        },
        {"uuid": "entity-general", "labels": ["Entity"], "bootstrap_scope": "general"},
    ]
    repo = FakeRepo(flags, [_candidate()])
    path = tmp_path / "inventory.jsonl"

    MODULE.plan(repo, "bolt://test", path, 10)
    rows = _read(path)

    assert [row["uuid"] for row in rows if row["kind"] == "flag"] == [
        "episodic-retention-only",
        "entity-general",
    ]
    assert rows[-1]["reason"] == "REVIEW_REQUIRED"
    assert rows[-1]["decision"] == "UNREVIEWED"
    assert "entity_content" not in rows[-1]


def test_inventory_refuses_duplicate_source_rows() -> None:
    repo = FakeRepo([], [_candidate(), _candidate()])
    with pytest.raises(ValueError, match="duplicate source/Graphiti/entity"):
        MODULE._inventory(repo, limit=10)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"resolved_episode_uuid": None}, "MISSING_BINDING"),
        ({"graphiti_uuid": None}, "MISSING_GRAPHITI_EPISODE"),
        ({"content_matches": False}, "CONTENT_MISMATCH"),
        ({"chronology_valid": False}, "CHRONOLOGY_UNTRUSTED"),
        ({"entity_namespace": "other"}, "ENTITY_TENANT_MISMATCH"),
        ({"structure_role": "file"}, "STRUCTURAL_ENTITY"),
        ({"is_evidence_projection": True}, "EVIDENCE_PROJECTION"),
        ({"merge_audit_count": 1}, "MERGE_LINEAGE_UNKNOWN"),
        ({"already_linked": True}, "ALREADY_LINKED"),
    ],
)
def test_unsafe_candidates_are_not_automatically_linked(
    change: dict[str, Any], reason: str
) -> None:
    assert MODULE._reason(_candidate(**change)) == reason


def test_review_requires_explicit_proof_and_detects_graph_drift(tmp_path: Path) -> None:
    repo = FakeRepo([], [_candidate()])
    path = tmp_path / "inventory.jsonl"
    MODULE.plan(repo, "bolt://test", path, 10)
    rows = _read(path)
    rows[-1]["decision"] = "APPROVE"
    _write(path, rows)

    with pytest.raises(ValueError, match="explicit provenance proof"):
        MODULE._approved(repo, "bolt://test", "neo4j", path, 10)

    rows[-1]["proof"] = "Reviewed the exact source/Graphiti receipt and merge history"
    _write(path, rows)
    approved = MODULE._approved(repo, "bolt://test", "neo4j", path, 10)
    assert len(approved) == 1
    assert MODULE.apply(repo, approved) == 1
    assert "ON CREATE SET r.direct = true" in repo.writes[0]["query"]
    assert "SET e.user_flagged" not in repo.writes[0]["query"]

    repo.sources[0]["entity_namespace"] = "other"
    with pytest.raises(ValueError, match="graph inventory changed"):
        MODULE._approved(repo, "bolt://test", "neo4j", path, 10)


def test_legacy_merge_backfill_unmerge_reflag_stays_quarantined(tmp_path: Path) -> None:
    """An old merge lacks retention snapshot fields, so its survivor cannot gain a guessed link."""
    repo = FakeRepo([], [_candidate(merge_audit_count=1)])
    path = tmp_path / "legacy-merge.jsonl"
    MODULE.plan(repo, "bolt://test", path, 10)
    rows = _read(path)
    assert rows[-1]["reason"] == "MERGE_LINEAGE_UNKNOWN"
    rows[-1]["decision"] = "APPROVE"
    rows[-1]["proof"] = "source was flagged"
    _write(path, rows)
    with pytest.raises(ValueError, match="eligible row"):
        MODULE._approved(repo, "bolt://test", "neo4j", path, 10)
    assert repo.writes == []


@pytest.mark.online
def test_reviewed_link_is_live_and_preserves_legacy_flag(
    test_neo4j_repo: Any, tmp_path: Path
) -> None:
    from menhir.domain.retention import source_retention_protected_cypher

    repo = test_neo4j_repo
    tag = f"legacy-retention-{uuid.uuid4()}"
    source, graphiti, entity = (
        f"{tag}-{suffix}" for suffix in ("source", "graphiti", "entity")
    )
    repo.execute(
        """
        CREATE (s:Episodic {uuid:$source, name:'same', content:'same text',
                            created_at:datetime('2026-01-01T00:00:00Z'), processing_state:'READY',
                            resolved_episode_uuid:$graphiti, namespace:'default',
                            user_flagged:true, test_tag:$tag})
        CREATE (g:Episodic {uuid:$graphiti, name:'same', content:'same text',
                            created_at:datetime('2026-01-01T00:00:01Z'),
                            namespace:'default', test_tag:$tag})
        CREATE (e:Entity {uuid:$entity, namespace:'default', user_flagged:false,
                          content:'semantic memory', test_tag:$tag})
        CREATE (g)-[:MENTIONS]->(e)
        """,
        {"source": source, "graphiti": graphiti, "entity": entity, "tag": tag},
    )
    path = tmp_path / "review.jsonl"
    try:
        MODULE.plan(repo, repo.uri, path, 100)
        rows = _read(path)
        target = next(row for row in rows if row.get("source_uuid") == source)
        assert target["reason"] == "REVIEW_REQUIRED"
        target["decision"] = "APPROVE"
        target["proof"] = (
            "synthetic test: exact source was created with this Graphiti UUID"
        )
        _write(path, rows)

        def work(tx: Any) -> int:
            reviewed = MODULE._approved(tx, repo.uri, repo.database, path, 100)
            return MODULE.apply(tx, reviewed)

        assert repo.execute_write(work) == 1
        assert MODULE.verify(repo, repo.uri, repo.database, path, 100) == 1
        repo.execute(
            "MATCH (s:Episodic {uuid:$source}) SET s.user_flagged=false",
            {"source": source},
        )
        with pytest.raises(ValueError, match="exact reviewed delta"):
            MODULE.verify(repo, repo.uri, repo.database, path, 100)
        for flagged, expected in ((True, True), (False, False), (True, True)):
            repo.execute(
                "MATCH (s:Episodic {uuid:$source}) SET s.user_flagged=$flagged",
                {"source": source, "flagged": flagged},
            )
            result = repo.execute(
                f"MATCH (e:Entity {{uuid:$entity}}) RETURN "
                f"{source_retention_protected_cypher('e')} AS protected, "
                "e.user_flagged AS direct, "
                "[(s:Episodic)-[r:RETENTION_SOURCE]->(e) | r.direct] AS link_direct",
                {"entity": entity},
            )[0]
            assert result == {
                "protected": expected,
                "direct": False,
                "link_direct": [True],
            }
    finally:
        repo.execute("MATCH (n {test_tag:$tag}) DETACH DELETE n", {"tag": tag})


@pytest.mark.online
def test_legacy_merge_backfill_unmerge_reflag_requires_lineage_review(
    test_neo4j_repo: Any, tmp_path: Path
) -> None:
    """A survivor with an old audit entry is quarantined through a simulated unmerge."""
    repo = test_neo4j_repo
    tag = f"legacy-merge-{uuid.uuid4()}"
    ids = {
        name: f"{tag}-{name}" for name in ("source", "graphiti", "survivor", "absorbed")
    }
    ids["tag"] = tag
    repo.execute(
        """
        CREATE (s:Episodic {uuid:$source, name:'same', content:'same text',
                            created_at:datetime('2026-01-01T00:00:00Z'), processing_state:'READY',
                            resolved_episode_uuid:$graphiti, namespace:'default',
                            user_flagged:false, test_tag:$tag})
        CREATE (g:Episodic {uuid:$graphiti, name:'same', content:'same text',
                            created_at:datetime('2026-01-01T00:00:01Z'),
                            namespace:'default', test_tag:$tag})
        CREATE (v:Entity {uuid:$survivor, namespace:'default', user_flagged:true,
                          content:'legacy survivor', test_tag:$tag})
        CREATE (a:Entity {uuid:$absorbed, namespace:'default',
                          content:'legacy absorbed', test_tag:$tag})
        CREATE (g)-[:MENTIONS]->(a)
        """,
        ids,
    )
    try:
        # Old merge: MENTIONS moves to the survivor; the snapshot has no retention fields.
        repo.execute(
            """
            MATCH (g:Episodic {uuid:$graphiti})-[m:MENTIONS]->(a:Entity {uuid:$absorbed})
            MATCH (v:Entity {uuid:$survivor})
            CREATE (g)-[:MENTIONS]->(v)
            SET v.merge_audit = [$legacy_audit]
            DELETE m
            DETACH DELETE a
            """,
            {
                **ids,
                "legacy_audit": json.dumps(
                    {
                        "survivor_uuid": ids["survivor"],
                        "absorbed_uuid": ids["absorbed"],
                        "mentioned_by_episodes": [ids["graphiti"]],
                    }
                ),
            },
        )
        path = tmp_path / "merged.jsonl"
        MODULE.plan(repo, repo.uri, path, 100)
        rows = _read(path)
        target = next(row for row in rows if row.get("entity_uuid") == ids["survivor"])
        assert target["reason"] == "MERGE_LINEAGE_UNKNOWN"
        target["decision"] = "APPROVE"
        target["proof"] = "source was once flagged"
        _write(path, rows)
        with pytest.raises(ValueError, match="eligible row"):
            MODULE._approved(repo, repo.uri, repo.database, path, 100)

        # Simulate the old snapshot's unmerge and a later source reflag. The repair made no
        # guessed survivor edge that would remain stranded on the wrong identity.
        repo.execute(
            """
            MATCH (g:Episodic {uuid:$graphiti})-[m:MENTIONS]->(v:Entity {uuid:$survivor})
            MATCH (s:Episodic {uuid:$source})
            DELETE m
            CREATE (a:Entity {uuid:$absorbed, namespace:'default', test_tag:$tag})
            CREATE (g)-[:MENTIONS]->(a)
            SET s.user_flagged = true
            """,
            ids,
        )
        result = repo.execute(
            """
            MATCH (s:Episodic {uuid:$source})
            OPTIONAL MATCH (s)-[r:RETENTION_SOURCE]->(e:Entity)
            RETURN count(r) AS guessed_links, s.user_flagged AS source_flagged
            """,
            ids,
        )[0]
        assert result == {"guessed_links": 0, "source_flagged": True}
    finally:
        repo.execute("MATCH (n {test_tag:$tag}) DETACH DELETE n", {"tag": tag})
