"""CF-80: a failed admission-provenance link must be visible when it was load-bearing.

The episode is durable before the `:ADMITTED_ON` edge is drawn. When that edge records WHY an
elevated tier was granted (the gated user/manual verdict), losing it is an auditability gap -- an
operator must be able to see it, not read a DEBUG line. The caller-supplied case (any other
source) is provenance only: the tier is already agent and does not depend on the edge, so a
failure there stays non-fatal and quiet. Either way the ingest must not fail.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from menhir.domain import MemorySession
from menhir.services.ingest_intake import IngestIntakeMixin

pytestmark = pytest.mark.unit


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


def _grounded_evidence():
    return {
        "turn_id": "turn-1",
        "role": "user",
        "text": _GROUNDED_TEXT,
        "session_id": "session-1",
        "namespace": None,
    }


@pytest.mark.asyncio
async def test_granted_verdict_link_failure_is_logged_at_warning(caplog):
    """The finding: a failed link on a granted verdict is visible at WARNING+ and names the episode
    uuid and the turn-evidence uuid."""
    adapter = _adapter(evidence=_grounded_evidence())
    adapter.link_episode_admission.side_effect = RuntimeError("neo4j down")
    with caplog.at_level(logging.WARNING, logger="menhir.services.ingest_intake"):
        await _Intake(adapter).queue_episode_for_enrichment(
            episode=_GROUNDED_TEXT,
            session=_session(),
            source="user",
            turn_evidence_uuid="turn-1",
        )
    episode_uuid = adapter.create_pending_episode.call_args.kwargs["episode_uuid"]
    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert records, "expected at least one WARNING-or-above record"
    assert episode_uuid in caplog.text
    assert "turn-1" in caplog.text
    assert "granted tier" in caplog.text


@pytest.mark.asyncio
async def test_link_failure_never_fails_the_ingest():
    """POSITIVE CONTROL: even a load-bearing link failure does not fail the write -- the episode is
    already durable and the call returns normally."""
    adapter = _adapter(evidence=_grounded_evidence())
    adapter.link_episode_admission.side_effect = RuntimeError("neo4j down")
    result = await _Intake(adapter).queue_episode_for_enrichment(
        episode=_GROUNDED_TEXT,
        session=_session(),
        source="user",
        turn_evidence_uuid="turn-1",
    )
    assert result.episode_id
    assert adapter.create_pending_episode.call_count == 1


@pytest.mark.asyncio
async def test_success_path_logs_nothing_at_warning(caplog):
    """POSITIVE CONTROL: on the success path nothing is logged at WARNING or above."""
    adapter = _adapter(evidence=_grounded_evidence())
    with caplog.at_level(logging.WARNING, logger="menhir.services.ingest_intake"):
        await _Intake(adapter).queue_episode_for_enrichment(
            episode=_GROUNDED_TEXT,
            session=_session(),
            source="user",
            turn_evidence_uuid="turn-1",
        )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_caller_supplied_link_failure_stays_debug(caplog):
    """The two cases are distinguishable: the caller-supplied (agent-source) edge is provenance only,
    so its failure must stay DEBUG, not escalate to a WARNING audit gap."""
    adapter = _adapter(evidence=_grounded_evidence())
    adapter.link_episode_admission.side_effect = RuntimeError("neo4j down")
    with caplog.at_level(logging.WARNING, logger="menhir.services.ingest_intake"):
        await _Intake(adapter).queue_episode_for_enrichment(
            episode=_GROUNDED_TEXT,
            session=_session(),
            source="claude-code",
            turn_evidence_uuid="turn-1",
        )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert adapter.create_pending_episode.call_count == 1
