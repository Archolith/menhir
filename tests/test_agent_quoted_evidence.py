"""Agent-quoted evidence (`source_kind='agent_quoted'`): scalar perception only.

Evidence an agent supplied via add_memory's `user_statement` must never be treated as proof
the user said something. It may feed typed-scalar perception; it must NOT grant user-tier
admission, must NOT count as user foundation for recall authority, and must NOT feed the
counter or event lanes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from menhir.domain import MemorySession
from menhir.domain.truth.admission_gate import evaluate_user_tier_claim
from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.infrastructure.scalar_view_repository import ScalarViewRepositoryMixin
from menhir.infrastructure.turn_evidence_repository import TurnEvidenceRepository
from menhir.services.ingest_intake import IngestIntakeMixin


# ---- admission gate ---------------------------------------------------------------------------


def _evidence(**overrides):
    row = {
        "turn_id": "turn-1",
        "role": "user",
        "declarant": "user",
        "text": "I have 20 coins",
        "session_id": "session-1",
        "namespace": None,
        "source_kind": "claude_code_hook",
    }
    row.update(overrides)
    return row


class TestAdmissionGate:
    def test_deny_agent_quoted_evidence(self):
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence=_evidence(source_kind="agent_quoted"),
            claimed_text="I have 20 coins",
            session_id="session-1",
            namespace=None,
        )
        assert verdict.granted is False
        assert verdict.effective_source == "agent_inference"
        assert "agent_quoted" in verdict.reason

    def test_control_normal_user_evidence_still_grants(self):
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence=_evidence(),
            claimed_text="I have 20 coins",
            session_id="session-1",
            namespace=None,
        )
        assert verdict.granted is True
        assert verdict.effective_source == "user"

    def test_null_source_kind_is_not_agent_quoted(self):
        verdict = evaluate_user_tier_claim(
            requested_source="user",
            turn_evidence=_evidence(source_kind=None),
            claimed_text="I have 20 coins",
            session_id="session-1",
            namespace=None,
        )
        assert verdict.granted is True


# ---- queue_episode_for_enrichment -------------------------------------------------------------


class _Intake(IngestIntakeMixin):
    def __init__(self, adapter):
        self.graph_adapter = adapter
        self._enrichment_enabled = False


def _session():
    return MemorySession(
        session_id="session-1",
        user_id="user-1",
        started_at=datetime.now(timezone.utc),
        client_name="opencode",
    )


def _intake_adapter():
    adapter = MagicMock()
    adapter.record_turn_evidence.return_value = {"turn_id": "agent-quoted-turn-1", "created": True}
    return adapter


@pytest.mark.unit
@pytest.mark.asyncio
async def test_user_statement_records_agent_quoted_evidence_and_links_provenance_only():
    adapter = _intake_adapter()
    result = await _Intake(adapter).queue_episode_for_enrichment(
        episode="User mentioned having 20 coins",
        session=_session(),
        source="claude-code",
        user_statement="I have 20 coins",
    )
    assert result.episode_id
    adapter.record_turn_evidence.assert_called_once()
    kwargs = adapter.record_turn_evidence.call_args.kwargs
    assert kwargs["text"] == "I have 20 coins"
    assert kwargs["role"] == "user"
    assert kwargs["declarant"] == "user"
    assert kwargs["source_kind"] == "agent_quoted"
    assert kwargs["session_id"] == "session-1"
    assert kwargs["source_client"] == "opencode"
    # Provenance-only link through the non-gated branch.
    adapter.link_episode_admission.assert_called_once()
    assert adapter.link_episode_admission.call_args.kwargs["turn_evidence_uuid"] == (
        "agent-quoted-turn-1"
    )
    # The gate is never consulted on the agent path.
    adapter.fetch_turn_evidence.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("statement", [None, "", "   "])
async def test_whitespace_statement_records_nothing(statement):
    adapter = _intake_adapter()
    await _Intake(adapter).queue_episode_for_enrichment(
        episode="something happened",
        session=_session(),
        source="claude-code",
        user_statement=statement,
    )
    adapter.record_turn_evidence.assert_not_called()
    adapter.link_episode_admission.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_failure_does_not_fail_the_ingest():
    adapter = _intake_adapter()
    adapter.record_turn_evidence.side_effect = RuntimeError("neo4j down")
    result = await _Intake(adapter).queue_episode_for_enrichment(
        episode="User mentioned having 20 coins",
        session=_session(),
        source="claude-code",
        user_statement="I have 20 coins",
    )
    assert result.episode_id
    adapter.create_pending_episode.assert_called_once()
    adapter.link_episode_admission.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_user_source_with_user_statement_does_not_get_the_user_tier():
    """source='user' + user_statement: the agent-quoted evidence is never fed to the gate, so the
    claim is ungrounded and fails closed to agent_inference. The agent-quoted turn still draws a
    provenance-only edge (non-load-bearing) so scalar binding can reach the memory."""
    adapter = _intake_adapter()
    await _Intake(adapter).queue_episode_for_enrichment(
        episode="I have 20 coins",
        session=_session(),
        source="user",
        user_statement="I have 20 coins",
    )
    adapter.record_turn_evidence.assert_called_once()
    adapter.fetch_turn_evidence.assert_not_called()
    assert adapter.create_pending_episode.call_args.kwargs["source"] == "agent_inference"
    adapter.link_episode_admission.assert_called_once()
    assert adapter.link_episode_admission.call_args.kwargs["turn_evidence_uuid"] == (
        "agent-quoted-turn-1"
    )


# ---- repo lane exclusions (query text) --------------------------------------------------------


class FakeNeo4j:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.executed: list[tuple[str, dict]] = []

    def execute(self, query, params=None):
        self.executed.append((query, params or {}))
        return self.rows


def _repo():
    return TurnEvidenceRepository(FakeNeo4j())


class TestLaneExclusions:
    def test_counter_lane_excludes_agent_quoted(self):
        repo = _repo()
        repo.list_dirty_evidence_namespaces()
        repo.load_user_evidence("ns")
        for query, _ in repo._neo4j.executed:
            assert "coalesce(t.source_kind, '') <> 'agent_quoted'" in query

    def test_event_lane_excludes_agent_quoted(self):
        repo = _repo()
        repo.list_event_dirty_evidence_namespaces(perceiver_version="v1")
        repo.load_next_event_evidence_batch("ns", perceiver_version="v1")
        for query, _ in repo._neo4j.executed:
            assert "coalesce(t.source_kind, '') <> 'agent_quoted'" in query

    def test_scalar_lane_keeps_agent_quoted(self):
        repo = _repo()
        repo.list_scalar_dirty_evidence_namespaces(perceiver_version="v1")
        repo.load_next_scalar_evidence_batch("ns", perceiver_version="v1")
        for query, _ in repo._neo4j.executed:
            assert "agent_quoted" not in query

    def test_fetch_by_uuid_returns_source_kind(self):
        fake = FakeNeo4j([{"turn_id": "t1", "source_kind": "agent_quoted"}])
        repo = TurnEvidenceRepository(fake)
        row = repo.fetch_by_uuid("t1")
        assert row is not None
        assert "source_kind" in fake.executed[-1][0]
        assert row["source_kind"] == "agent_quoted"


# ---- foundation checks -------------------------------------------------------------------------


class TestFoundationExclusions:
    def test_scalar_view_foundation_excludes_agent_quoted(self):
        fake = FakeNeo4j([{"founded": True}])
        repo = ScalarViewRepositoryMixin.__new__(ScalarViewRepositoryMixin)
        repo.neo4j = fake
        assert repo.scalar_view_has_user_foundation(view_uuid="v1") is True
        query = fake.executed[-1][0]
        assert "coalesce(te.source_kind, '') <> 'agent_quoted'" in query

    def test_assertions_foundation_excludes_agent_quoted(self):
        fake = FakeNeo4j([{"founded": True}])
        repo = ScalarViewRepositoryMixin.__new__(ScalarViewRepositoryMixin)
        repo.neo4j = fake
        assert repo.assertions_have_user_foundation(assertion_ids=["a1"]) is True
        query = fake.executed[-1][0]
        assert "coalesce(te.source_kind, '') <> 'agent_quoted'" in query


# ---- evidence projection stays reachable for agent-quoted turns --------------------------------


class TestEvidenceProjectionKeepsAgentQuoted:
    def test_projection_query_does_not_exclude_agent_quoted(self):
        """create_evidence_projection is the entity-vocabulary mechanism scalar perception relies
        on, and ingest_intake feeds it agent-quoted turn ids. It must NOT exclude agent_quoted:
        it projects the verbatim text, it does not certify user authorship (the gate and the
        foundation checks above own that boundary)."""
        repo = EpisodeLifecycleRepository()
        repo.neo4j = MagicMock()
        repo.neo4j.execute.return_value = [{"uuid": "p1", "created": True}]
        assert repo.create_evidence_projection(
            turn_evidence_uuid="agent-quoted-turn-1",
            projection_uuid="p1",
            name="evidence-projection-agent-quoted-turn-1",
            session_id="session-1",
            user_id="user-1",
            namespace=None,
        ) == "p1"
        cypher = repo.neo4j.execute.call_args.args[0]
        assert "agent_quoted" not in cypher
