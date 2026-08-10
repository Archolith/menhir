"""Evidence projections: enrich the USER's verbatim turn so entities exist in their vocabulary.

WHY: a typed scalar assertion is extracted from the user's words ("I have 25 postcards") while the
entities it must bind to come from the AGENT's memory text -- a paraphrase. The two vocabularies need
not agree, so the bind fails. `:TurnEvidence` holds the user's actual words but is deliberately never
enriched (ADR 0001), so it yields no entities at all. The projection is the missing step.

THE RISK BEING GUARDED IS ADR 0001's. That ADR rejected mixing raw turns into `:Episodic` memory
because raw chat would pollute recall and decay. A projection IS an `:Episodic`, so the exclusion
tests below are the primary contract -- getting entities out is worth nothing if raw chat leaks into
recall alongside them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from menhir.domain.session import MemorySession
from menhir.domain.structural_memory import non_structural_memory_cypher
from menhir.services.ingest_intake import IngestIntakeMixin


# --------------------------------------------------------------- the ADR 0001 boundary (primary)

def test_the_shared_memory_filter_excludes_projections():
    """Every listing/lookup shape in memory_queries composes this one predicate. If a projection is
    not excluded HERE it is visible in all of them, which is precisely the raw-chat-in-recall outcome
    ADR 0001 rejected."""
    cypher = non_structural_memory_cypher("n")
    assert "is_evidence_projection" in cypher
    assert "NOT coalesce(n.is_evidence_projection, false)" in cypher


def test_the_filter_treats_an_absent_flag_as_not_a_projection():
    """coalesce, not a bare truth test: every pre-existing episode lacks the property entirely, and
    `n.is_evidence_projection = false` would evaluate NULL and drop the whole corpus from recall."""
    assert "coalesce(" in non_structural_memory_cypher("n")


# ------------------------------------------------------------------------- the repository contract

class _Repo:
    """The real repository with a stubbed driver, so the CYPHER is what is under test."""

    def __init__(self, rows):
        self.neo4j = MagicMock()
        self.neo4j.execute.return_value = rows


def _repo(rows):
    from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
    repo = EpisodeLifecycleRepository.__new__(EpisodeLifecycleRepository)
    repo.neo4j = MagicMock()
    repo.neo4j.execute.return_value = rows
    return repo


def _project(repo, **overrides):
    kwargs = {
        "turn_evidence_uuid": "turn-1",
        "projection_uuid": "proj-1",
        "name": "evidence-projection-turn-1",
        "session_id": "session-1",
        "user_id": "user-1",
        "namespace": "ns",
    }
    kwargs.update(overrides)
    return repo.create_evidence_projection(**kwargs)


def test_projects_a_user_turn():
    repo = _repo([{"uuid": "proj-1", "created": True}])
    assert _project(repo) == "proj-1"


def test_refuses_a_turn_that_is_not_user_declared():
    """The declarant boundary is load-bearing (ADR 0001): an assistant restating a user's fact must
    never become evidence for it. Enforced in the MATCH, so a non-user turn returns no rows."""
    cypher = _repo([]).neo4j.execute.return_value  # noqa: F841  (readability)
    repo = _repo([])
    assert _project(repo) is None
    sent = repo.neo4j.execute.call_args.args[0]
    assert "t.role = 'user'" in sent and "t.declarant = 'user'" in sent


def test_is_idempotent_on_the_turn():
    """N memories citing one turn must yield ONE projection. The second call MERGEs onto the existing
    node, whose uuid is NOT the one we just passed, so `created` is false and nothing is enqueued."""
    repo = _repo([{"uuid": "proj-original", "created": False}])
    assert _project(repo, projection_uuid="proj-second") is None


def test_creation_is_detected_without_a_stored_flag():
    """A stored `created = true` would survive on the node and report true on every later call,
    silently defeating idempotency. Creation is inferred from the uuid instead."""
    repo = _repo([{"uuid": "proj-1", "created": True}])
    _project(repo)
    sent = repo.neo4j.execute.call_args.args[0]
    assert "(p.uuid = $projection_uuid) AS created" in sent
    assert "p.created = true" not in sent


def test_the_projection_carries_the_turn_text_not_the_memory_text():
    """The whole point: content comes from `t.text`, never from a caller-supplied string. A caller
    could otherwise put anything into a node stamped source='user', confidence 1.0."""
    repo = _repo([{"uuid": "proj-1", "created": True}])
    _project(repo)
    sent = repo.neo4j.execute.call_args.args[0]
    assert "p.content = t.text" in sent
    assert "p.source = 'user'" in sent
    assert "p.is_evidence_projection = true" in sent


def test_projection_prefers_source_time_over_receive_time():
    repo = _repo([{"uuid": "proj-1", "created": True}])
    _project(repo)
    sent = repo.neo4j.execute.call_args.args[0]
    assert "p.reference_time = coalesce(t.occurred_at, t.recorded_at)" in sent


def test_confidence_comes_from_the_ssot_not_a_literal():
    """`source_confidence_for`'s docstring records that hardcoding this value was drift (SSOT-09).
    A literal would keep passing tests while silently disagreeing with every other writer the moment
    the tier ladder is tuned."""
    from menhir.domain.utils import source_confidence_for
    repo = _repo([{"uuid": "proj-1", "created": True}])
    _project(repo)
    sent = repo.neo4j.execute.call_args.args[0]
    params = repo.neo4j.execute.call_args.kwargs["params"]
    assert "p.source_confidence = $source_confidence" in sent
    assert params["source_confidence"] == source_confidence_for("user")


def test_the_projection_is_linked_to_its_turn():
    repo = _repo([{"uuid": "proj-1", "created": True}])
    _project(repo)
    assert "MERGE (p)-[:ADMITTED_ON]->(t)" in repo.neo4j.execute.call_args.args[0]


def test_the_projection_is_queued_for_enrichment():
    """A projection that never enriches produces none of the entities it exists to produce."""
    repo = _repo([{"uuid": "proj-1", "created": True}])
    _project(repo)
    assert "p.processing_state = 'PENDING'" in repo.neo4j.execute.call_args.args[0]


# --------------------------------------------------------------------- wiring through the intake

class _Intake(IngestIntakeMixin):
    def __init__(self, adapter, *, enrichment=False):
        self.graph_adapter = adapter
        self._enrichment_enabled = enrichment


def _session():
    return MemorySession(session_id="session-1", user_id="user-1",
                         started_at=datetime.now(timezone.utc))


def _adapter(projection="proj-1"):
    adapter = MagicMock()
    adapter.fetch_turn_evidence.return_value = None
    adapter.create_evidence_projection.return_value = projection
    return adapter


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_agent_memory_citing_a_turn_projects_it():
    adapter = _adapter()
    await _Intake(adapter).queue_episode_for_enrichment(
        episode="User owns 25 postcards", session=_session(),
        source="claude-code", turn_evidence_uuid="turn-1",
    )
    assert adapter.create_evidence_projection.call_count == 1
    assert adapter.create_evidence_projection.call_args.kwargs["turn_evidence_uuid"] == "turn-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_turn_cited_means_no_projection():
    adapter = _adapter()
    await _Intake(adapter).queue_episode_for_enrichment(
        episode="User owns 25 postcards", session=_session(), source="claude-code",
    )
    assert adapter.create_evidence_projection.call_count == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_projection_never_fails_the_write():
    """Strictly additive: the memory is already durable by this point. Losing the entity source is a
    degraded bind, not a lost memory."""
    adapter = _adapter()
    adapter.create_evidence_projection.side_effect = RuntimeError("neo4j down")
    result = await _Intake(adapter).queue_episode_for_enrichment(
        episode="User owns 25 postcards", session=_session(),
        source="claude-code", turn_evidence_uuid="turn-1",
    )
    assert result.episode_id
    assert adapter.create_pending_episode.call_count == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_denied_user_claim_projects_nothing():
    """The gate's veto covers the projection too. A refused claim must not mint a node stamped
    source='user' with confidence 1.0 out of the evidence the gate just rejected."""
    adapter = _adapter()
    adapter.fetch_turn_evidence.return_value = {
        "turn_id": "turn-1", "role": "user", "text": "something else entirely",
        "session_id": "session-1", "namespace": None,
    }
    await _Intake(adapter).queue_episode_for_enrichment(
        episode="User owns 25 postcards", session=_session(),
        source="user", turn_evidence_uuid="turn-1",
    )
    assert adapter.create_evidence_projection.call_count == 0
