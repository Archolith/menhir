"""End-to-end parity test for the L4 artifact loop, menhir service stack.

This drives the *real* menhir service layer (ArtifactService -> adapter delegates ->
MemoryOracleService) over a faithful in-memory stand-in for the artifact graph, using the
same corpus as the bench fixture `archolith_bench/fixtures/l4_failure_demo.json`. It proves
the menhir port and the bench agree on the behavior that matters before the live-graph walk
(.agent/plans/l4-commit6-live-verification.md):

  - a TRUSTED human Failure surfaces as a fact-eligible artifact (status intact);
  - an LLM artifact is captured CANDIDATE even with evidence (invariant 4);
  - an evidence-less human note is CANDIDATE and un-promotable (invariants 5, 3);
  - supersession reads HISTORICAL, never deleted (invariant 7);
  - the oracle ranks the anchored artifacts above the off-anchor incident.

The in-memory adapter emulates exactly the guards the ArtifactRepository Cypher enforces;
the live repository is checked separately at home. The bench is the spec — if this diverges
from `python scripts/run_l4_bench.py` (with_l4), the port is wrong, not the bench."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from menhir.domain.artifacts import ArtifactSource, ArtifactType, Evidence
from menhir.services.artifact_service import ArtifactService
from menhir.services.memory_oracle_service import MemoryOracleService


@dataclass
class _InMemoryArtifactGraph:
    """Faithful in-memory emulation of the ArtifactRepository delegate contract.

    Mirrors the Cypher guards: status->scope mapping, evidence dedup on (kind, ref),
    fail-closed promote (CANDIDATE + has evidence), supersede marks historical and never
    deletes, find matches anchor-overlap OR summary-token and returns status intact."""

    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def create_artifact(self, *, artifact_id, artifact_type, summary, source, status,
                        body="", evidence=None, anchors=None, source_confidence=0.5):
        existing = self.nodes.get(artifact_id)
        ev_index = {(e["kind"], e["ref"]): e for e in (existing["evidence"] if existing else [])}
        for e in evidence or []:
            ev_index[(e["kind"], e["ref"])] = e  # dedup on (kind, ref)
        if existing is None:
            node = {
                "artifact_id": artifact_id, "type": artifact_type, "summary": summary,
                "body": body, "status": status,
                "scope": "PERSISTENT" if status == "trusted" else "CANDIDATE",
                "source": source, "anchors": list(anchors or []),
                "evidence": list(ev_index.values()), "superseded_by": None,
                "source_confidence": source_confidence,
            }
        else:
            # Fix 2: ON MATCH refreshes provenance only — status/scope/source_confidence
            # are preserved, so a re-capture can't resurrect or silently re-trust.
            node = dict(existing)
            node.update(summary=summary, body=body, anchors=list(anchors or []),
                        evidence=list(ev_index.values()))
        self.nodes[artifact_id] = node
        return {"uuid": f"u-{artifact_id}", "scope": node["scope"], "status": node["status"],
                "created": existing is None}

    def fetch_artifact(self, artifact_id):
        return self.nodes.get(artifact_id)

    def promote_artifact(self, artifact_id, *, trusted_confidence=0.9):
        n = self.nodes.get(artifact_id)
        # Fix 1: requires a non-agent_inference evidence (LLM self-evidence can't trust).
        promotable = any(e.get("kind") != "agent_inference" for e in (n or {}).get("evidence", []))
        if not n or n["scope"] != "CANDIDATE" or n["status"] != "candidate" or not promotable:
            return False
        n["status"] = "trusted"
        n["scope"] = "PERSISTENT"
        n["source_confidence"] = trusted_confidence
        return True

    def supersede_artifact(self, old_id, new_id):
        if old_id == new_id or old_id not in self.nodes or new_id not in self.nodes:
            return False
        self.nodes[old_id]["status"] = "historical"   # never deleted (invariant 7)
        self.nodes[old_id]["superseded_by"] = new_id
        self.nodes[new_id]["supersedes"] = old_id
        return True

    def find_artifacts(self, *, tokens=None, anchors=None, limit=50):
        anchors = set(anchors or [])
        toks = set(tokens or [])
        out = []
        for n in self.nodes.values():
            if anchors & set(n["anchors"]) or any(t in (n["summary"] or "").lower() for t in toks):
                out.append(n)
        return out[:limit]


def _run(coro):
    return asyncio.run(coro)


# The bench fixture corpus, mirrored. Anchored on the real cosine-floor recall regression.
_TASK = "Recall is dropping low-cosine candidates — tighten the similarity floor in scoring."
_ANCHOR = ["scoring_service.py"]
_GIT = [Evidence(kind="git", ref="e8da67d")]


async def _seed(svc: ArtifactService) -> None:
    await svc.capture(artifact_id="floor_fail", artifact_type=ArtifactType.FAILURE,
                      summary="fixed cosine floor dropped facet candidates",
                      source=ArtifactSource.HUMAN, evidence=_GIT, anchors=_ANCHOR)
    await svc.capture(artifact_id="floor_fix", artifact_type=ArtifactType.DECISION,
                      summary="rank candidates, apply a source-aware floor after ranking",
                      source=ArtifactSource.HUMAN, evidence=_GIT, anchors=_ANCHOR)
    await svc.capture(artifact_id="floor_guess", artifact_type=ArtifactType.FAILURE,
                      summary="maybe recency interacts with the floor",
                      source=ArtifactSource.LLM,
                      evidence=[Evidence(kind="agent_inference", ref="scoring_service.py", directness=0.3)],
                      anchors=_ANCHOR)
    await svc.capture(artifact_id="old_floor", artifact_type=ArtifactType.DECISION,
                      summary="use a fixed cosine floor on similarity",
                      source=ArtifactSource.HUMAN, evidence=_GIT, anchors=_ANCHOR)
    await svc.capture(artifact_id="floor_hunch", artifact_type=ArtifactType.DECISION,
                      summary="consider lowering the floor to 0.05",
                      source=ArtifactSource.HUMAN, anchors=_ANCHOR)  # no evidence
    await svc.capture(artifact_id="lease_inc", artifact_type=ArtifactType.INCIDENT,
                      summary="force_acquire overwrote an active scheduler lease",
                      source=ArtifactSource.HUMAN, evidence=[Evidence(kind="log", ref="inc-1")],
                      anchors=["scheduler_lease.py"])
    await svc.supersede("old_floor", "floor_fix")  # old decision is superseded by the fix


def _graph() -> tuple[ArtifactService, MemoryOracleService, _InMemoryArtifactGraph]:
    g = _InMemoryArtifactGraph()
    return ArtifactService(graph_adapter=g), MemoryOracleService(graph_adapter=g), g


@pytest.mark.unit
def test_capture_assigns_bench_parity_statuses() -> None:
    svc, _, g = _graph()
    _run(_seed(svc))
    status = {k: v["status"] for k, v in g.nodes.items()}
    assert status["floor_fail"] == "trusted"      # human + evidence (inv. 5)
    assert status["floor_fix"] == "trusted"
    assert status["floor_guess"] == "candidate"   # LLM, even with evidence (inv. 4)
    assert status["floor_hunch"] == "candidate"   # human, no evidence (inv. 5)
    assert status["old_floor"] == "historical"    # superseded (inv. 7)
    assert status["lease_inc"] == "trusted"


@pytest.mark.unit
def test_evidence_less_human_note_is_unpromotable() -> None:
    svc, _, _ = _graph()
    _run(_seed(svc))
    out = _run(svc.promote("floor_hunch"))
    assert out["status"] == "refused" and out["reason"] == "no_promotable_evidence"  # invariant 3


@pytest.mark.unit
def test_llm_agent_inference_only_cannot_be_promoted() -> None:
    # Fix 1: floor_guess is LLM-sourced with only agent_inference evidence.
    svc, _, _ = _graph()
    _run(_seed(svc))
    out = _run(svc.promote("floor_guess"))
    assert out["status"] == "refused" and out["reason"] == "no_promotable_evidence"


@pytest.mark.unit
def test_superseded_decision_cannot_be_promoted_or_resurrected() -> None:
    # invariant 7: promote refuses, AND a re-capture cannot flip it back to trusted (Fix 2).
    svc, _, g = _graph()
    _run(_seed(svc))
    assert _run(svc.promote("old_floor"))["status"] == "refused"
    # try to resurrect via re-capture as a trusted human artifact
    _run(svc.capture(artifact_id="old_floor", artifact_type=ArtifactType.DECISION,
                     summary="use a fixed cosine floor (re-asserted)",
                     source=ArtifactSource.HUMAN, evidence=_GIT, anchors=_ANCHOR))
    assert g.nodes["old_floor"]["status"] == "historical"   # still historical — not resurrected
    assert g.nodes["old_floor"]["scope"] == "PERSISTENT"


@pytest.mark.unit
def test_source_confidence_tracks_trust_level() -> None:
    # Fix 4: trusted artifacts aren't stored at the neutral 0.5 default.
    svc, _, g = _graph()
    _run(_seed(svc))
    assert g.nodes["floor_fail"]["source_confidence"] == 0.9   # trusted human
    assert g.nodes["floor_guess"]["source_confidence"] == 0.4  # llm candidate
    assert g.nodes["floor_hunch"]["source_confidence"] == 0.5  # human candidate, no evidence


@pytest.mark.unit
def test_oracle_surfaces_anchored_artifacts_status_intact() -> None:
    svc, oracle, _ = _graph()
    _run(_seed(svc))
    hits = _run(oracle.find(text=_TASK, anchors=_ANCHOR, limit=10))
    by_id = {h.artifact["artifact_id"]: h for h in hits}
    # the five scoring_service-anchored artifacts surface; the off-anchor incident does not
    assert {"floor_fail", "floor_fix", "floor_guess", "old_floor", "floor_hunch"} <= set(by_id)
    assert "lease_inc" not in by_id
    # statuses are intact — bucketing fact/hypothesis/stale is the brief's job, not the oracle's
    assert by_id["floor_fail"].artifact["status"] == "trusted"
    assert by_id["floor_guess"].artifact["status"] == "candidate"
    assert by_id["old_floor"].artifact["status"] == "historical"
    assert "anchor" in by_id["floor_fail"].matched_on
