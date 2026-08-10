"""`(:Episodic)-[:ADMITTED_ON]->(:TurnEvidence)` — the evidence->memory join.

The admission gate resolves an episode against the `:TurnEvidence` it cites in order to decide the
claim's tier, and used to drop the pair on the floor. That left the same user turn in the graph twice
under two identities with nothing joining them, so a scalar View founded on a turn could prove a human
said something without being able to reach the memory built from it.

These pin the two rules that make the edge trustworthy:
  * it is drawn ONLY on a GRANT (a denial means the evidence did not support the claim), and
  * it never invents a node, so a stale uuid links nothing rather than fabricating evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from menhir.domain import MemorySession
from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.services.ingest_intake import IngestIntakeMixin


def _make_repo(execute_return=None):
    repo = EpisodeLifecycleRepository()
    repo.neo4j = MagicMock()
    repo.neo4j.execute.return_value = execute_return if execute_return is not None else []
    return repo


class TestLinkEpisodeAdmissionCypher:
    def test_links_episode_to_turn_evidence(self):
        repo = _make_repo([{"linked": 1}])
        assert repo.link_episode_admission(
            episode_uuid="ep-1", turn_evidence_uuid="turn-1"
        ) is True
        params = repo.neo4j.execute.call_args.kwargs["params"]
        assert params == {"episode_uuid": "ep-1", "turn_evidence_uuid": "turn-1"}

    def test_never_creates_a_node(self):
        """MERGE the relationship, MATCH the endpoints. A MERGE on either node would let a stale uuid
        fabricate a `:TurnEvidence` that no one ever said, which the basis gate would then read as a
        genuine user admission."""
        repo = _make_repo([{"linked": 1}])
        repo.link_episode_admission(episode_uuid="ep-1", turn_evidence_uuid="turn-1")
        cypher = repo.neo4j.execute.call_args.args[0]
        assert "MATCH (e:Episodic {uuid: $episode_uuid})" in cypher
        assert "MATCH (t:TurnEvidence {turn_id: $turn_evidence_uuid})" in cypher
        assert "MERGE (e)-[:ADMITTED_ON]->(t)" in cypher
        assert "MERGE (t:TurnEvidence" not in cypher
        assert "MERGE (e:Episodic" not in cypher
        assert "CREATE" not in cypher

    def test_missing_endpoint_links_nothing(self):
        """Either node absent -> the MATCH yields no rows -> False, no edge, no stub."""
        repo = _make_repo([])
        assert repo.link_episode_admission(
            episode_uuid="ep-gone", turn_evidence_uuid="turn-gone"
        ) is False


class _Intake(IngestIntakeMixin):
    """Minimal host for the mixin: intake only touches `graph_adapter` and the enrichment switch."""

    def __init__(self, adapter):
        self.graph_adapter = adapter
        self._enrichment_enabled = False


def _adapter(*, evidence):
    adapter = MagicMock()
    adapter.fetch_turn_evidence.return_value = evidence
    return adapter


def _session():
    return MemorySession(
        session_id="session-1",
        user_id="user-1",
        started_at=datetime.now(timezone.utc),
    )


_GROUNDED_TEXT = "I have 20 coins"


def _evidence(**overrides):
    row = {
        "turn_id": "turn-1",
        "role": "user",
        "text": _GROUNDED_TEXT,
        "session_id": "session-1",
        "namespace": None,
    }
    row.update(overrides)
    return row


@pytest.mark.unit
@pytest.mark.asyncio
async def test_granted_user_claim_links_the_memory_to_its_evidence():
    adapter = _adapter(evidence=_evidence())
    await _Intake(adapter).queue_episode_for_enrichment(
        episode=_GROUNDED_TEXT,
        session=_session(),
        source="user",
        turn_evidence_uuid="turn-1",
    )
    assert adapter.link_episode_admission.call_count == 1
    assert adapter.link_episode_admission.call_args.kwargs["turn_evidence_uuid"] == "turn-1"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence, why",
    [
        (_evidence(role="assistant"), "role is not user"),
        (_evidence(text="something else entirely"), "claimed text is not grounded"),
        (_evidence(session_id="a-different-session"), "session mismatch"),
        (None, "no evidence resolved"),
    ],
)
async def test_denied_claim_draws_no_edge(evidence, why):
    """A denial downgrades the claim to agent_inference. Drawing ADMITTED_ON anyway would assert the
    foundation the gate just refused, and `scalar_view_has_user_foundation` would read it as a real
    user admission."""
    adapter = _adapter(evidence=evidence)
    await _Intake(adapter).queue_episode_for_enrichment(
        episode=_GROUNDED_TEXT,
        session=_session(),
        source="user",
        turn_evidence_uuid="turn-1",
    )
    assert adapter.link_episode_admission.call_count == 0, why


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["claude-code", "codex", "opencode"])
async def test_agent_tier_memory_links_when_the_caller_supplies_evidence(source):
    """INVERTED 2026-07-27. This previously asserted call_count == 0, on the reading that ADMITTED_ON
    records an apex-tier ADMISSION and a non-user source has no admission to record.

    That reading conflated two things. The edge is PROVENANCE -- this memory was written in the
    context of this turn -- which is true of every source; the gate decides TIER, which is a separate
    question. Welding them cost production the whole edge: G14's scalar loader hands out
    `:TurnEvidence.turn_id`s and `fetch_linked_entities_for_episode` resolves them back through this
    edge, so with no edge there are no entity candidates and every non-self subject abstains. All
    production sources are agent sources, so the entire fleet was on the dead path.

    Drawing it here forges nothing: the tier is already agent and this edge cannot raise it."""
    adapter = _adapter(evidence=_evidence())
    await _Intake(adapter).queue_episode_for_enrichment(
        episode=_GROUNDED_TEXT,
        session=_session(),
        source=source,
        turn_evidence_uuid="turn-1",
    )
    assert adapter.link_episode_admission.call_count == 1
    assert adapter.link_episode_admission.call_args.kwargs["turn_evidence_uuid"] == "turn-1"


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("supplied", [None, "", "   "])
async def test_agent_tier_memory_draws_no_edge_without_an_evidence_uuid(supplied):
    """The ungated path takes the caller's id at face value, so absence must stay absence -- nothing
    infers a turn from proximity or recency. Most memories legitimately have no turn."""
    adapter = _adapter(evidence=_evidence())
    await _Intake(adapter).queue_episode_for_enrichment(
        episode=_GROUNDED_TEXT,
        session=_session(),
        source="claude-code",
        turn_evidence_uuid=supplied,
    )
    assert adapter.link_episode_admission.call_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_tier_never_consults_the_admission_gate():
    """Opening the provenance path must not drag the tier gate along with it. A non-user source still
    makes no user-tier claim, so nothing should be fetched or evaluated -- the id is used only to draw
    the edge."""
    adapter = _adapter(evidence=_evidence())
    await _Intake(adapter).queue_episode_for_enrichment(
        episode=_GROUNDED_TEXT,
        session=_session(),
        source="claude-code",
        turn_evidence_uuid="turn-1",
    )
    assert adapter.fetch_turn_evidence.call_count == 0
    assert adapter.create_pending_episode.call_args.kwargs["source"] == "claude-code"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_denied_user_claim_still_beats_the_caller_supplied_id():
    """The gate's veto must not be routable around. A denied user claim is downgraded to
    agent_inference, but that must NOT then fall through to the ungated branch and draw the edge from
    the raw caller argument -- that would let any client turn a refusal into a foundation by simply
    claiming source='user'."""
    adapter = _adapter(evidence=_evidence(text="something else entirely"))
    await _Intake(adapter).queue_episode_for_enrichment(
        episode=_GROUNDED_TEXT,
        session=_session(),
        source="user",
        turn_evidence_uuid="turn-1",
    )
    assert adapter.link_episode_admission.call_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_link_failure_never_fails_the_ingest():
    """The episode is already durable when the edge is drawn; provenance is strictly additive."""
    adapter = _adapter(evidence=_evidence())
    adapter.link_episode_admission.side_effect = RuntimeError("neo4j down")
    result = await _Intake(adapter).queue_episode_for_enrichment(
        episode=_GROUNDED_TEXT,
        session=_session(),
        source="user",
        turn_evidence_uuid="turn-1",
    )
    assert result.episode_id
    assert adapter.create_pending_episode.call_count == 1
