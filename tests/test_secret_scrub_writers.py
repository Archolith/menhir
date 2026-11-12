"""#577 Phase 2: every memory-text writer scrubs credentials before they reach storage."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.domain import MemorySession
from menhir.infrastructure.artifact_repository import ArtifactRepository
from menhir.infrastructure.candidate_repository import CandidateRepository
from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.infrastructure.episode_maintenance import EpisodeMaintenanceRepository
from menhir.infrastructure.structure_queries import StructureGraphWriter
from menhir.infrastructure.temporal_repository import TemporalRepository
from menhir.infrastructure.todo_repository import TodoRepository
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository
from menhir.services.enrichment_steps import compose_episode_body
from menhir.services.ingest_intake import IngestIntakeMixin

pytestmark = pytest.mark.unit

AWS = "AKIAIOSFODNN7EXAMPLE"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


def _flatten(obj) -> str:
    """Every string reachable in call args/kwargs, joined."""
    if isinstance(obj, dict):
        return " ".join(_flatten(k) + " " + _flatten(v) for k, v in obj.items())
    if isinstance(obj, (list, tuple, set)):
        return " ".join(_flatten(v) for v in obj)
    return str(obj)


def _executed(neo4j: MagicMock) -> str:
    # Skip the query text (arg 0); only the params can carry the value.
    parts = []
    for call in neo4j.execute.call_args_list:
        parts.append(_flatten(list(call.args[1:])))
        parts.append(_flatten(call.kwargs))
    return " ".join(parts)


def _assert_scrubbed(neo4j: MagicMock) -> None:
    seen = _executed(neo4j)
    assert AWS not in seen
    assert GITHUB not in seen
    assert "[REDACTED:" in seen


def _neo4j() -> MagicMock:
    neo4j = MagicMock()
    neo4j.execute.return_value = []
    # G-copy writers (R2 CF-327) run their statements through a transaction on the same mock.
    neo4j.execute_write.side_effect = lambda work: work(neo4j)
    return neo4j


def test_create_pending_episode_scrubs_content_and_diff() -> None:
    repo = EpisodeLifecycleRepository()
    repo.neo4j = _neo4j()
    repo.create_pending_episode(
        episode_uuid="e1", name="n", content=f"use {AWS} here", session_id="s", user_id="u",
        source="claude-code", source_confidence=0.5, diff=f"+ token {GITHUB}",
    )
    _assert_scrubbed(repo.neo4j)


def test_record_turn_evidence_scrubs_text_and_metadata() -> None:
    neo4j = _neo4j()
    neo4j.execute.return_value = [{"turn_id": "t", "created": True}]
    repo = TurnEvidenceRepository(neo4j)
    try:
        repo.record_turn_evidence(text=f"my key is {AWS}", metadata={"note": f"tok {GITHUB}"})
    except Exception:
        pass  # row shape after the write is not under test; the params already went out
    _assert_scrubbed(neo4j)


def test_record_turn_evidence_hash_and_key_use_scrubbed_text() -> None:
    from menhir.infrastructure.turn_evidence_repository import derive_prompt_hash

    neo4j = _neo4j()
    try:
        TurnEvidenceRepository(neo4j).record_turn_evidence(text=f"my key is {AWS}")
    except Exception:
        pass
    params = neo4j.execute.call_args.kwargs.get("params") or neo4j.execute.call_args.args[1]
    assert params["prompt_hash"] != derive_prompt_hash(f"my key is {AWS}")
    assert AWS not in params["text"]


def test_create_temporal_scrubs_content_and_name() -> None:
    repo = TemporalRepository(_neo4j())
    repo.create_temporal(content=f"renew {AWS}", target_date="2027-01-01", name=f"n {GITHUB}")
    _assert_scrubbed(repo.neo4j)


def test_create_todo_scrubs_content() -> None:
    repo = TodoRepository(_neo4j())
    try:
        repo.create_todo(content=f"rotate {AWS} and {GITHUB}")
    except Exception:
        pass
    _assert_scrubbed(repo.neo4j)


def test_create_candidate_scrubs_content_and_notes() -> None:
    repo = CandidateRepository(_neo4j())
    try:
        repo.create_candidate(
            content=f"friction {AWS}", source="painscan", cluster_id="c1", label="",
            notes=[f"note {GITHUB}"],
        )
    except Exception:
        pass
    _assert_scrubbed(repo.neo4j)


def test_create_artifact_scrubs_summary_and_body() -> None:
    repo = ArtifactRepository(_neo4j())
    try:
        repo.create_artifact(
            artifact_id="a1", artifact_type="doc", summary=f"key {AWS}",
            source="test", status="candidate", body=f"token {GITHUB}",
        )
    except Exception:
        pass
    _assert_scrubbed(repo.neo4j)


def test_write_document_scrubs_content() -> None:
    writer = object.__new__(StructureGraphWriter)
    writer.neo4j = _neo4j()
    writer.write_document(
        "/x/readme.md", f"key {AWS} and {GITHUB}", project="p", structure_path="doc:r",
        structure_project_id="pid", session_id="s", user_id="u",
    )
    _assert_scrubbed(writer.neo4j)


def test_create_raw_capture_entity_scrubs_content_and_name() -> None:
    repo = object.__new__(EpisodeMaintenanceRepository)
    repo.neo4j = _neo4j()
    repo.create_raw_capture_entity(
        "e1", f"name {GITHUB}", f"content {AWS}", "default", "s", "u", "claude-code",
    )
    _assert_scrubbed(repo.neo4j)


# ---- intake ---------------------------------------------------------------------------------


class _Intake(IngestIntakeMixin):
    def __init__(self) -> None:
        self.graph_adapter = MagicMock()
        self.graph_adapter.find_episode_by_ingest_key.return_value = None
        self.graph_adapter.record_turn_evidence.return_value = {"turn_id": "t1"}
        self._enrichment_enabled = False
        self._ensure_enrichment_worker = AsyncMock()
        self._enqueue_pending_episode = AsyncMock()


def _session() -> MemorySession:
    return MemorySession(
        session_id="session-1", user_id="user-1",
        started_at=datetime.now(timezone.utc), client_name="opencode",
    )


def _queue(intake: _Intake, **kwargs):
    return asyncio.run(
        intake.queue_episode_for_enrichment(
            episode=f"deploy with {AWS}", session=_session(), source="claude-code",
            diff=f"+ {GITHUB}", user_statement=f"my key {AWS}", **kwargs,
        )
    )


def test_intake_scrubs_before_persisting() -> None:
    intake = _Intake()
    _queue(intake)
    create = intake.graph_adapter.create_pending_episode.call_args.kwargs
    assert AWS not in create["content"] and GITHUB not in create["diff"]
    assert "[REDACTED:" in create["content"] and "[REDACTED:" in create["diff"]
    evidence = intake.graph_adapter.record_turn_evidence.call_args.kwargs
    assert AWS not in evidence["text"]
    assert "[REDACTED:" in evidence["text"]


def test_intake_refuse_policy_raises_before_persisting(monkeypatch) -> None:
    monkeypatch.setenv("MENHIR_SECRET_POLICY", "refuse")
    intake = _Intake()
    with pytest.raises(ValueError):
        _queue(intake)
    intake.graph_adapter.create_pending_episode.assert_not_called()
    intake.graph_adapter.record_turn_evidence.assert_not_called()


def test_intake_idempotency_digest_is_stable_on_scrubbed_text() -> None:
    digests = []
    for _ in range(2):
        intake = _Intake()
        _queue(intake, idempotency_key="k1")
        digests.append(
            intake.graph_adapter.create_pending_episode.call_args.kwargs["ingest_request_sha256"]
        )
    assert digests[0] == digests[1]


# ---- worker ---------------------------------------------------------------------------------


def test_compose_episode_body_has_no_secret() -> None:
    plain = compose_episode_body({"content": f"use {AWS}"})
    with_diff = compose_episode_body({"content": "c", "diff": f"+ {GITHUB}"})
    assert AWS not in plain and "[REDACTED:" in plain
    assert GITHUB not in with_diff and "[REDACTED:" in with_diff


def test_off_policy_worker_body_keeps_text(monkeypatch) -> None:
    from menhir.services.enrichment_steps import compose_episode_body

    monkeypatch.setenv("MENHIR_SECRET_POLICY", "off")
    assert "AKIAIOSFODNN7EXAMPLE" in compose_episode_body({"content": "k AKIAIOSFODNN7EXAMPLE"})
    monkeypatch.setenv("MENHIR_SECRET_POLICY", "refuse")
    assert "AKIAIOSFODNN7EXAMPLE" not in compose_episode_body({"content": "k AKIAIOSFODNN7EXAMPLE"})
