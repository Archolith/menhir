"""End-to-end: the REAL git_log adapter drives the belief gate through recall.

Closes the gap that test_recall_service's gated belief test used a FAKE change-log provider.
Here the graph is stubbed but the change log is produced by the real CachedGitChangeLog /
git_log adapter running `git log` against a throwaway repo created in a tmp dir. No Neo4j,
no network. Skipped if git is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from menhir.domain.retrieval_tuning import RetrievalTuningConfig
from menhir.services import recall_pipeline as rs
from menhir.services.recall_service import RecallService
from menhir.services.scoring_service import ScoringService

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a repo with auth.py committed; return (repo_path, head_sha)."""
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "auth.py").write_text("def login():\n    return 1\n")
    _git(repo, "add", "auth.py")
    _git(repo, "commit", "-q", "-m", "c0")
    return repo, _git(repo, "rev-parse", "HEAD")


def _setup_stub(stub_graphiti_client, stub_memory_graph_adapter, *, belief_commit: str) -> None:
    """One searchable candidate m1, anchored to auth.py in project 'proj', formed at belief_commit."""
    stub_graphiti_client.search_scored_results = [("m1", "Auth memory", 0.9)]
    stub_memory_graph_adapter.candidate_metadata = [
        {
            "uuid": "m1", "name": "Auth memory", "scope": "PERSISTENT", "type": "SEMANTIC",
            "content": "login returns 1", "summary": None,
            "last_accessed": datetime.now(timezone.utc),
            "edge_count": 1, "sharpness": 0.2, "freshness": "ACTIVE", "user_flagged": False,
            "belief_commit": belief_commit,
        }
    ]
    stub_memory_graph_adapter.candidate_provenance_rows = [
        {
            "uuid": "m1", "evidence_node_kinds": ["git"], "anchor_projects": ["proj"],
            "anchor_paths": ["auth.py"], "episode_sources": [],
        }
    ]
    stub_memory_graph_adapter.temporal_fact_rows = []


def _svc(stub_graphiti_client, stub_memory_graph_adapter) -> RecallService:
    return RecallService(
        graphiti_client=stub_graphiti_client,
        graph_adapter=stub_memory_graph_adapter,
        scoring_service=ScoringService(),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_git_staleness_gates_memory_after_real_commit(
    stub_graphiti_client, stub_memory_graph_adapter, tmp_path, monkeypatch
) -> None:
    """Real `git log` over belief_commit..HEAD: a memory whose anchored file changed after it
    was formed is gated by the belief gate (with warden_gate applying the chain)."""
    repo, c0 = _init_repo(tmp_path)
    _setup_stub(stub_graphiti_client, stub_memory_graph_adapter, belief_commit=c0)
    monkeypatch.setattr(rs, "_repo_path_for", lambda project: str(repo))
    svc = _svc(stub_graphiti_client, stub_memory_graph_adapter)
    tuning = RetrievalTuningConfig(enable_belief_gate=True, enable_warden_gate=True)

    # HEAD == belief commit: nothing changed since the belief -> memory present.
    before = await svc.recall("auth", tuning=tuning)
    assert "m1" in {r.uuid for r in before.results}

    # Real change to the anchored file -> a new commit descends from the belief commit.
    (repo / "auth.py").write_text("def login():\n    return 2  # refactored\n")
    _git(repo, "commit", "-q", "-am", "c1")

    # git log c0..HEAD now shows auth.py changed -> LATER_CONTRADICTED -> CurrentnessWarden gates m1.
    after = await svc.recall("auth", tuning=tuning)
    m1 = [r for r in after.results if r.uuid == "m1"]
    assert (not m1) or (m1[0].warden_label is not None), "stale memory should be dropped or flagged"
    assert "belief_gate" in (after.note or "") and "warden_gate" in (after.note or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_git_staleness_surfaces_in_shadow_without_changing_results(
    stub_graphiti_client, stub_memory_graph_adapter, tmp_path, monkeypatch
) -> None:
    """Observe-only: with shadow on and warden_gate OFF, results are unchanged but the
    assertion-shadow trace records the staleness verdict for the stale memory."""
    repo, c0 = _init_repo(tmp_path)
    _setup_stub(stub_graphiti_client, stub_memory_graph_adapter, belief_commit=c0)
    monkeypatch.setattr(rs, "_repo_path_for", lambda project: str(repo))
    svc = _svc(stub_graphiti_client, stub_memory_graph_adapter)

    # change the anchored file so staleness exists
    (repo / "auth.py").write_text("x = 2\n")
    _git(repo, "commit", "-q", "-am", "c1")

    tuning = RetrievalTuningConfig(enable_belief_gate=True, enable_assertion_shadow=True)
    res = await svc.recall("auth", tuning=tuning, trace=True)

    # observe-only: warden_gate off -> m1 still present in results
    assert "m1" in {r.uuid for r in res.results}
    # but the shadow pass saw the staleness and would not admit it as current truth
    assert res.trace is not None and res.trace.assertion_shadow is not None
    shadow = res.trace.assertion_shadow
    m1row = next((r for r in shadow.rows if r.candidate_id == "m1"), None)
    assert m1row is not None
    assert m1row.decision in ("refuse", "flag") or (shadow.refused + shadow.flagged) > 0
