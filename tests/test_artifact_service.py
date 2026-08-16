"""Unit tests for the R9-lite ArtifactService facade and read-only MemoryOracleService.

A fake graph adapter stands in for Neo4j so the invariant enforcement (trust policy on
capture, fail-closed promote, never-self-supersede) and the oracle ranking are exercised
without a graph. Live behavior is checked at home per the commit-6 verification doc."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from menhir.domain.artifacts import ArtifactSource, ArtifactType, Evidence
from menhir.services.artifact_service import ArtifactService
from menhir.services.memory_oracle_service import MemoryOracleService


@dataclass
class _FakeAdapter:
    """In-memory stand-in for MemoryGraphAdapter's artifact delegates."""

    store: dict[str, dict[str, Any]] = field(default_factory=dict)

    def create_artifact(self, *, artifact_id, artifact_type, summary, source, status,
                         body="", evidence=None, anchors=None, source_confidence=0.5):
        self.store[artifact_id] = {
            "artifact_id": artifact_id, "type": artifact_type, "summary": summary,
            "status": status, "source": source, "anchors": list(anchors or []),
            "body": body, "evidence": list(evidence or []), "superseded_by": None,
            "source_confidence": source_confidence,
        }
        return {"uuid": f"u-{artifact_id}", "scope": "PERSISTENT" if status == "trusted" else "CANDIDATE",
                "status": status, "created": True}

    def fetch_artifact(self, artifact_id):
        return self.store.get(artifact_id)

    def promote_artifact(self, artifact_id, *, trusted_confidence=0.9):
        a = self.store.get(artifact_id)
        # mirror the Cypher guard: CANDIDATE + a non-agent_inference evidence
        promotable = any(e.get("kind") != "agent_inference" for e in (a or {}).get("evidence", []))
        if not a or a["status"] != "candidate" or not promotable:
            return False
        a["status"] = "trusted"
        a["source_confidence"] = trusted_confidence
        return True

    def supersede_l4_artifact(self, old_id, new_id):
        if old_id not in self.store or new_id not in self.store:
            return False
        self.store[old_id]["status"] = "historical"
        self.store[old_id]["superseded_by"] = new_id
        return True

    def find_artifacts(self, *, tokens=None, anchors=None, limit=50):
        anchors = set(anchors or [])
        toks = set(tokens or [])
        out = []
        for a in self.store.values():
            if anchors & set(a["anchors"]) or any(t in (a["summary"] or "").lower() for t in toks):
                out.append(a)
        return out[:limit]


def _run(coro):
    return asyncio.run(coro)


_EV = [Evidence(kind="git", ref="e8da67d")]


@pytest.mark.unit
def test_capture_human_with_evidence_is_trusted() -> None:
    svc = ArtifactService(graph_adapter=_FakeAdapter())
    out = _run(svc.capture(artifact_id="f1", artifact_type=ArtifactType.FAILURE, summary="x",
                           source=ArtifactSource.HUMAN, evidence=_EV))
    assert out["status"] == "trusted"


@pytest.mark.unit
def test_capture_llm_is_candidate_even_with_evidence() -> None:
    svc = ArtifactService(graph_adapter=_FakeAdapter())
    out = _run(svc.capture(artifact_id="f1", artifact_type=ArtifactType.FAILURE, summary="x",
                           source=ArtifactSource.LLM, evidence=_EV))
    assert out["status"] == "candidate"  # invariant 4


@pytest.mark.unit
def test_promote_refuses_without_evidence_then_succeeds_with() -> None:
    adapter = _FakeAdapter()
    svc = ArtifactService(graph_adapter=adapter)
    _run(svc.capture(artifact_id="d1", artifact_type=ArtifactType.DECISION, summary="x",
                     source=ArtifactSource.HUMAN))  # no evidence -> candidate
    refused = _run(svc.promote("d1"))
    assert refused["status"] == "refused" and refused["reason"] == "no_promotable_evidence"  # invariant 3
    # add evidence via re-capture, then promote
    _run(svc.capture(artifact_id="d1", artifact_type=ArtifactType.DECISION, summary="x",
                     source=ArtifactSource.LLM, evidence=_EV))  # llm -> candidate but now has evidence
    promoted = _run(svc.promote("d1"))
    assert promoted["status"] == "trusted"
    assert adapter.store["d1"]["source_confidence"] == 0.9  # Fix 4: confidence bumps on promote


@pytest.mark.unit
def test_agent_inference_only_cannot_promote_and_confidence_by_level() -> None:
    # Fix 1 + Fix 4 at the service boundary.
    adapter = _FakeAdapter()
    svc = ArtifactService(graph_adapter=adapter)
    ai = [Evidence(kind="agent_inference", ref="scoring_service.py", directness=0.3)]
    out = _run(svc.capture(artifact_id="g1", artifact_type=ArtifactType.FAILURE, summary="hunch",
                           source=ArtifactSource.LLM, evidence=ai))
    assert out["status"] == "candidate"
    assert adapter.store["g1"]["source_confidence"] == 0.4  # llm candidate
    refused = _run(svc.promote("g1"))
    assert refused["status"] == "refused" and refused["reason"] == "no_promotable_evidence"
    # a trusted human capture lands at 0.9
    _run(svc.capture(artifact_id="t1", artifact_type=ArtifactType.DECISION, summary="x",
                     source=ArtifactSource.HUMAN, evidence=_EV))
    assert adapter.store["t1"]["source_confidence"] == 0.9


@pytest.mark.unit
def test_supersede_refuses_self_and_marks_historical() -> None:
    adapter = _FakeAdapter()
    svc = ArtifactService(graph_adapter=adapter)
    _run(svc.capture(artifact_id="old", artifact_type=ArtifactType.DECISION, summary="old",
                     source=ArtifactSource.HUMAN, evidence=_EV))
    _run(svc.capture(artifact_id="new", artifact_type=ArtifactType.DECISION, summary="new",
                     source=ArtifactSource.HUMAN, evidence=_EV))
    assert _run(svc.supersede("old", "old"))["status"] == "refused"
    assert _run(svc.supersede("old", "new"))["status"] == "superseded"
    assert adapter.store["old"]["status"] == "historical"
    # a superseded artifact can no longer be promoted (invariant 7)
    assert _run(svc.promote("old"))["status"] == "refused"


@pytest.mark.unit
def test_oracle_ranks_anchor_above_topic_and_never_writes() -> None:
    adapter = _FakeAdapter()
    svc = ArtifactService(graph_adapter=adapter)
    _run(svc.capture(artifact_id="floor_fail", artifact_type=ArtifactType.FAILURE,
                     summary="cosine similarity floor dropped facets", source=ArtifactSource.HUMAN,
                     evidence=_EV, anchors=["scoring_service.py"]))
    oracle = MemoryOracleService(graph_adapter=adapter)
    hits = _run(oracle.find(text="tighten the similarity floor", anchors=["scoring_service.py"]))
    assert hits and hits[0].artifact["artifact_id"] == "floor_fail"
    assert "anchor" in hits[0].matched_on
    for forbidden in ("create", "promote", "supersede"):
        assert not hasattr(oracle, forbidden)
