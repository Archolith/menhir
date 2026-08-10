"""Unit tests for the L4 ArtifactRepository write/read path.

No live Neo4j: a stub captures Cypher + params and echoes rows, so the create/promote/
supersede/find logic and the invariant guards baked into the Cypher are exercised
deterministically. The live graph behavior (nodes/edges actually materialize) is checked
at home per .agent/plans/l4-commit6-live-verification.md."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from menhir.infrastructure.artifact_repository import ArtifactRepository, _evidence_rows


@dataclass
class _EchoNeo4j:
    """Stub that records calls and echoes a create-shaped row (created_at echoes now)."""

    echo_created_at: str | None = None
    count_key: str | None = None
    count: int = 1
    calls: list[dict[str, object]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        p = params or {}
        self.calls.append({"query": query, "params": p})
        if self.count_key is not None:
            return [{self.count_key: self.count}]
        return [{
            "uuid": p.get("uuid"),
            "scope": p.get("scope"),
            "status": p.get("status"),
            "created_at": self.echo_created_at or p.get("now"),
            "evidence_count": len(p.get("evidence") or []),
        }]


@pytest.mark.unit
def test_evidence_rows_drops_incomplete_and_flags_structural() -> None:
    rows = _evidence_rows([
        {"kind": "git", "ref": "e8da67d"},
        {"kind": "agent_inference", "ref": "foo.py", "directness": 0.3},
        {"kind": "", "ref": "x"},        # dropped: no kind
        {"kind": "test", "ref": ""},     # dropped: no ref
    ])
    assert len(rows) == 2
    assert rows[0]["is_structural"] is True
    assert rows[1]["is_structural"] is False
    assert all("uuid" in r for r in rows)


@pytest.mark.unit
def test_create_artifact_writes_artifact_fields_and_evidence_edge() -> None:
    neo4j = _EchoNeo4j()
    repo = ArtifactRepository(neo4j)
    out = repo.create_artifact(
        artifact_id="floor_fail", artifact_type="failure", summary="cosine floor dropped facets",
        source="human", status="trusted", evidence=[{"kind": "git", "ref": "e8da67d"}],
        anchors=["scoring_service.py"],
    )
    assert out["created"] is True
    q = neo4j.calls[0]["query"]
    # idempotency on artifact_id; first-class evidence via SUPPORTED_BY; reuses SEMANTIC.
    assert "MERGE (a:Entity {artifact_id: $artifact_id})" in q
    assert "MERGE (e:Evidence {artifact_id: $artifact_id, kind: ev.kind, ref: ev.ref})" in q
    assert "MERGE (a)-[:SUPPORTED_BY]->(e)" in q
    assert "a.type = 'SEMANTIC'" in q
    assert neo4j.calls[0]["params"]["scope"] == "PERSISTENT"  # trusted -> PERSISTENT
    # Fix 2: ON MATCH must NOT overwrite status/scope (no resurrection / silent re-trust).
    on_match = q.split("ON MATCH SET", 1)[1].split("FOREACH", 1)[0]
    assert "artifact_status" not in on_match
    assert "scope" not in on_match


@pytest.mark.unit
def test_raw_create_cannot_forge_trust_for_llm_source() -> None:
    """Graphify boundary guard: a RAW caller passing status='trusted' for an LLM artifact
    is clamped to candidate/CANDIDATE (inv. 4) — trust is not forgeable via the repository."""
    neo4j = _EchoNeo4j()
    out = ArtifactRepository(neo4j).create_artifact(
        artifact_id="llm_forge", artifact_type="failure", summary="llm hunch",
        source="llm", status="trusted", evidence=[{"kind": "git", "ref": "abc"}],
    )
    assert neo4j.calls[0]["params"]["status"] == "candidate"
    assert neo4j.calls[0]["params"]["scope"] == "CANDIDATE"
    assert out["status"] == "candidate"


@pytest.mark.unit
def test_raw_create_cannot_forge_trust_without_evidence() -> None:
    """Human + status='trusted' but NO evidence is clamped to candidate (inv. 5)."""
    neo4j = _EchoNeo4j()
    ArtifactRepository(neo4j).create_artifact(
        artifact_id="no_ev", artifact_type="decision", summary="hunch",
        source="human", status="trusted", evidence=[],
    )
    assert neo4j.calls[0]["params"]["status"] == "candidate"
    assert neo4j.calls[0]["params"]["scope"] == "CANDIDATE"


@pytest.mark.unit
def test_raw_create_agent_inference_only_cannot_forge_trust() -> None:
    """agent_inference is LLM self-evidence: it can't justify trust even for a human caller."""
    neo4j = _EchoNeo4j()
    ArtifactRepository(neo4j).create_artifact(
        artifact_id="ai_only", artifact_type="failure", summary="maybe",
        source="human", status="trusted",
        evidence=[{"kind": "agent_inference", "ref": "x", "directness": 0.3}],
    )
    assert neo4j.calls[0]["params"]["status"] == "candidate"
    assert neo4j.calls[0]["params"]["scope"] == "CANDIDATE"


@pytest.mark.unit
def test_raw_create_legitimate_trust_is_not_over_clamped() -> None:
    """Human + promotable (git) evidence keeps status='trusted'/PERSISTENT — the guard only
    downgrades; it never blocks a legitimate trusted write."""
    neo4j = _EchoNeo4j()
    ArtifactRepository(neo4j).create_artifact(
        artifact_id="legit", artifact_type="failure", summary="real",
        source="human", status="trusted", evidence=[{"kind": "git", "ref": "e8da67d"}],
    )
    assert neo4j.calls[0]["params"]["status"] == "trusted"
    assert neo4j.calls[0]["params"]["scope"] == "PERSISTENT"


@pytest.mark.unit
def test_raw_create_explicit_candidate_is_honored() -> None:
    """An explicit status='candidate' is never upgraded, even with promotable evidence."""
    neo4j = _EchoNeo4j()
    ArtifactRepository(neo4j).create_artifact(
        artifact_id="cand", artifact_type="failure", summary="x",
        source="human", status="candidate", evidence=[{"kind": "git", "ref": "abc"}],
    )
    assert neo4j.calls[0]["params"]["status"] == "candidate"
    assert neo4j.calls[0]["params"]["scope"] == "CANDIDATE"


@pytest.mark.unit
def test_promote_passes_trusted_confidence() -> None:
    # Fix 4: promotion lifts source_confidence off the candidate default.
    neo4j = _EchoNeo4j(count_key="promoted", count=1)
    ArtifactRepository(neo4j).promote_artifact("floor_fail", trusted_confidence=0.9)
    q = neo4j.calls[0]["query"]
    assert "a.source_confidence = $trusted_confidence" in q
    assert neo4j.calls[0]["params"]["trusted_confidence"] == 0.9


def test_create_candidate_status_maps_to_candidate_scope() -> None:
    neo4j = _EchoNeo4j()
    ArtifactRepository(neo4j).create_artifact(
        artifact_id="g1", artifact_type="failure", summary="hunch", source="llm", status="candidate",
    )
    assert neo4j.calls[0]["params"]["scope"] == "CANDIDATE"


@pytest.mark.unit
def test_promote_artifact_guards_scope_status_and_evidence() -> None:
    neo4j = _EchoNeo4j(count_key="promoted", count=1)
    assert ArtifactRepository(neo4j).promote_artifact("floor_fail") is True
    q = neo4j.calls[0]["query"]
    assert "a.scope = 'CANDIDATE'" in q
    assert "a.scope = 'PERSISTENT'" in q
    # Fix 1: the guard requires a NON-agent_inference evidence (LLM self-evidence can't trust).
    assert "EXISTS { MATCH (a)-[:SUPPORTED_BY]->(e:Evidence) WHERE e.kind <> 'agent_inference' }" in q
    neo4j0 = _EchoNeo4j(count_key="promoted", count=0)
    assert ArtifactRepository(neo4j0).promote_artifact("no_evidence") is False


@pytest.mark.unit
def test_supersede_marks_historical_links_and_never_deletes() -> None:
    neo4j = _EchoNeo4j(count_key="superseded", count=1)
    assert ArtifactRepository(neo4j).supersede_artifact("old", "new") is True
    q = neo4j.calls[0]["query"]
    assert "old.artifact_status = 'historical'" in q
    assert "MERGE (new)-[:SUPERSEDES]->(old)" in q
    assert "DELETE" not in q.upper()  # invariant 7: never deletes
    assert "old.artifact_id <> new.artifact_id" in q  # no self-supersede


@pytest.mark.unit
def test_find_artifacts_matches_anchor_or_token_and_collects_evidence() -> None:
    neo4j = _EchoNeo4j(count_key="x", count=0)  # returns [] shape; we only inspect the query
    neo4j.count_key = None
    repo = ArtifactRepository(neo4j)
    repo.find_artifacts(tokens=["floor"], anchors=["scoring_service.py"], limit=10)
    q = neo4j.calls[0]["query"]
    assert "a.is_artifact = true" in q
    assert "any(x IN $anchors WHERE x IN a.artifact_anchors)" in q
    assert "any(tok IN $tokens WHERE toLower(a.summary) CONTAINS tok)" in q
    assert "SUPPORTED_BY" in q
    assert neo4j.calls[0]["params"]["limit"] == 10
