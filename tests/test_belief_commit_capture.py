"""Unit tests for belief_commit/belief_branch capture during episode ingest."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from menhir.infrastructure.episode_stamping import EpisodeStampingRepository
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.paths import repo_root_for_project
from menhir.services.ingest_service import _belief_commit_context


@dataclass
class _StubNeo4jRepository:
    """Stub neo4j for testing stamping queries."""

    responses: list[list[dict[str, object]]] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)

    def execute(self, query: str, params: dict[str, object] | None = None) -> list[dict[str, object]]:
        self.calls.append({"query": query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return [{"nodes_touched": 1}]


@pytest.mark.unit
def test_repo_root_for_project_finds_direct_project(tmp_path: Path) -> None:
    """Test that repo_root_for_project finds a direct project directory with .git."""
    # Create a project directory with .git marker (as a directory)
    project_dir = tmp_path / "p1"
    project_dir.mkdir()
    git_dir = project_dir / ".git"
    git_dir.mkdir()

    # Monkeypatch projects_dir to return our tmp_path
    import menhir.infrastructure.paths as paths_module
    original_projects_dir = paths_module.projects_dir
    paths_module.projects_dir = lambda: tmp_path

    try:
        result = repo_root_for_project("p1")
        assert result == project_dir
    finally:
        paths_module.projects_dir = original_projects_dir


@pytest.mark.unit
def test_repo_root_for_project_returns_none_for_missing_project(tmp_path: Path) -> None:
    """Test that repo_root_for_project returns None for non-existent projects."""
    import menhir.infrastructure.paths as paths_module
    original_projects_dir = paths_module.projects_dir
    paths_module.projects_dir = lambda: tmp_path

    try:
        result = repo_root_for_project("nope")
        assert result is None
    finally:
        paths_module.projects_dir = original_projects_dir


@pytest.mark.unit
def test_repo_root_for_project_returns_none_without_git(tmp_path: Path) -> None:
    """Test that repo_root_for_project returns None if .git does not exist."""
    # Create a project directory without .git
    project_dir = tmp_path / "p1"
    project_dir.mkdir()

    import menhir.infrastructure.paths as paths_module
    original_projects_dir = paths_module.projects_dir
    paths_module.projects_dir = lambda: tmp_path

    try:
        result = repo_root_for_project("p1")
        assert result is None
    finally:
        paths_module.projects_dir = original_projects_dir


@pytest.mark.unit
def test_repo_root_for_project_finds_owner_project(tmp_path: Path) -> None:
    """Test that repo_root_for_project finds owner/project structure."""
    # Create owner/project with .git
    owner_dir = tmp_path / "owner"
    owner_dir.mkdir()
    project_dir = owner_dir / "myproject"
    project_dir.mkdir()
    git_dir = project_dir / ".git"
    git_dir.mkdir()

    import menhir.infrastructure.paths as paths_module
    original_projects_dir = paths_module.projects_dir
    paths_module.projects_dir = lambda: tmp_path

    try:
        result = repo_root_for_project("myproject")
        assert result == project_dir
    finally:
        paths_module.projects_dir = original_projects_dir


@pytest.mark.unit
def test_repo_root_for_project_returns_none_for_empty_project() -> None:
    """Test that repo_root_for_project returns None for empty project name."""
    result = repo_root_for_project("")
    assert result is None


@pytest.mark.unit
def test_belief_commit_context_returns_commit_and_branch() -> None:
    """Test that _belief_commit_context returns SHA and branch."""
    head_fn = lambda r: ("abc123def", "main")
    repo_resolver = lambda p: Path("/x")

    result = _belief_commit_context("myproject", head_fn=head_fn, repo_resolver=repo_resolver)

    assert result == ("abc123def", "main")


@pytest.mark.unit
def test_belief_commit_context_returns_none_on_missing_repo() -> None:
    """Test that _belief_commit_context returns (None, None) when repo not found."""
    repo_resolver = lambda p: None

    result = _belief_commit_context("missing", repo_resolver=repo_resolver)

    assert result == (None, None)


@pytest.mark.unit
def test_belief_commit_context_returns_none_on_head_exception() -> None:
    """Test that _belief_commit_context returns (None, None) when head_fn raises."""
    head_fn = lambda r: (_ for _ in ()).throw(RuntimeError("git failed"))
    repo_resolver = lambda p: Path("/x")

    result = _belief_commit_context("project", head_fn=head_fn, repo_resolver=repo_resolver)

    assert result == (None, None)


@pytest.mark.unit
def test_belief_commit_context_returns_none_for_empty_project() -> None:
    """Test that _belief_commit_context returns (None, None) for empty project."""
    result = _belief_commit_context("", repo_resolver=lambda p: Path("/x"))

    assert result == (None, None)


@pytest.mark.unit
def test_stamp_ingest_metadata_includes_belief_fields_when_provided() -> None:
    """Test that stamp_ingest_metadata includes belief_commit and belief_branch in queries."""
    neo4j = _StubNeo4jRepository(
        responses=[
            [{"nodes_touched": 1}],
            [{"nodes_touched": 1}],
            [{"edges_touched": 1}],
        ]
    )
    stamping_repo = EpisodeStampingRepository()
    stamping_repo.neo4j = neo4j

    result = stamping_repo.stamp_ingest_metadata(
        node_uuids=["u1"],
        edge_uuids=[],
        session_id="session-1",
        user_id="user-1",
        source="test",
        source_confidence=0.5,
        belief_commit="abc123",
        belief_branch="main",
    )

    assert result.nodes_touched == 2
    # Check that at least one query contains belief_commit
    queries_with_belief = [
        call for call in neo4j.calls
        if "belief_commit" in call["query"]
    ]
    assert len(queries_with_belief) > 0, "No queries found with belief_commit"

    # Check that episodic query has belief_commit and belief_branch in params
    episodic_call = neo4j.calls[0]
    assert "MATCH (n:Episodic)" in episodic_call["query"]
    assert episodic_call["params"].get("belief_commit") == "abc123"
    assert episodic_call["params"].get("belief_branch") == "main"

    # Check that entity query also has belief fields with CASE logic
    entity_call = neo4j.calls[1]
    assert "MATCH (n:Entity)" in entity_call["query"]
    assert "CASE WHEN locked THEN n.belief_commit" in entity_call["query"]
    assert entity_call["params"].get("belief_commit") == "abc123"
    assert entity_call["params"].get("belief_branch") == "main"


@pytest.mark.unit
def test_stamp_ingest_metadata_excludes_belief_fields_when_not_provided() -> None:
    """Test that stamp_ingest_metadata does not include belief fields when None."""
    neo4j = _StubNeo4jRepository(
        responses=[
            [{"nodes_touched": 1}],
            [{"nodes_touched": 1}],
            [{"edges_touched": 1}],
        ]
    )
    stamping_repo = EpisodeStampingRepository()
    stamping_repo.neo4j = neo4j

    result = stamping_repo.stamp_ingest_metadata(
        node_uuids=["u1"],
        edge_uuids=[],
        session_id="session-1",
        user_id="user-1",
        source="test",
        source_confidence=0.5,
    )

    assert result.nodes_touched == 2
    # Check that no queries contain belief_commit
    queries_with_belief = [
        call for call in neo4j.calls
        if "belief_commit" in call["query"]
    ]
    assert len(queries_with_belief) == 0, "Unexpected belief_commit in queries"

    # Check that episodic query does not have belief fields in params
    episodic_call = neo4j.calls[0]
    assert "belief_commit" not in episodic_call["params"]
    assert "belief_branch" not in episodic_call["params"]


@pytest.mark.unit
def test_memory_graph_adapter_passes_belief_fields_through() -> None:
    """Test that MemoryGraphAdapter.stamp_ingest_metadata passes belief fields to repository."""
    neo4j = _StubNeo4jRepository(
        responses=[
            [{"nodes_touched": 1}],
            [{"nodes_touched": 1}],
            [{"edges_touched": 1}],
        ]
    )
    adapter = MemoryGraphAdapter(neo4j=neo4j)

    result = adapter.stamp_ingest_metadata(
        node_uuids=["u1"],
        edge_uuids=[],
        session_id="session-1",
        user_id="user-1",
        source="test",
        source_confidence=0.5,
        belief_commit="xyz789",
        belief_branch="develop",
    )

    assert result.nodes_touched == 2
    # Verify that belief fields are in the executed queries
    episodic_call = neo4j.calls[0]
    assert episodic_call["params"].get("belief_commit") == "xyz789"
    assert episodic_call["params"].get("belief_branch") == "develop"
