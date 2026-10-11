"""Speaker role intake: API model, ingest service, backend forwarding, consolidation discovery
(issue #271). The role is structured data supplied by the caller, never inferred from text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from menhir.api.routes_support import MemoryRequest
from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin
from menhir.domain import MemorySession
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.services.ingest_intake import IngestIntakeMixin

pytestmark = pytest.mark.unit

ROLES = ["user", "assistant", "tool", "agent", "system"]


# ---- MemoryRequest ------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_memory_request_accepts_each_allowed_role(role) -> None:
    assert MemoryRequest(episode="hello", role=role).role == role


def test_memory_request_role_defaults_to_none_and_accepts_none() -> None:
    assert MemoryRequest(episode="hello").role is None
    assert MemoryRequest(episode="hello", role=None).role is None


@pytest.mark.parametrize("role", ["Human", "human", "USER ", " user", "User", "", 1, True])
def test_memory_request_role_is_an_exact_literal(role) -> None:
    with pytest.raises(ValidationError):
        MemoryRequest(episode="hello", role=role)


# ---- IngestService.queue_episode_for_enrichment --------------------------------------------------


class _Intake(IngestIntakeMixin):
    def __init__(self, adapter):
        self.graph_adapter = adapter
        self._enrichment_enabled = False


def _session() -> MemorySession:
    return MemorySession(
        session_id="session-1", user_id="user-1",
        started_at=datetime.now(timezone.utc), client_name="opencode",
    )


@pytest.mark.asyncio
async def test_given_role_is_passed_to_create_pending_episode() -> None:
    adapter = MagicMock()
    await _Intake(adapter).queue_episode_for_enrichment(
        episode="You said you moved.", session=_session(), source="claude-code", role="assistant",
    )
    adapter.create_pending_episode.assert_called_once()
    assert adapter.create_pending_episode.call_args.kwargs["turn_role"] == "assistant"


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [" Tool ", "Tool", "USER", ["user"]])
async def test_role_must_match_exactly_like_the_rest_literal(role) -> None:
    # Review F4: every transport accepts exactly the values the REST Literal accepts.
    adapter = MagicMock()
    with pytest.raises(ValueError):
        await _Intake(adapter).queue_episode_for_enrichment(
            episode="x", session=_session(), source="claude-code", role=role,
        )
    adapter.create_pending_episode.assert_not_called()


@pytest.mark.asyncio
async def test_no_role_means_no_turn_role_kwarg_at_all() -> None:
    adapter = MagicMock()
    await _Intake(adapter).queue_episode_for_enrichment(
        episode="user: I moved", session=_session(), source="claude-code",
    )
    adapter.create_pending_episode.assert_called_once()
    assert "turn_role" not in adapter.create_pending_episode.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["human", "", "unknown", 3])
async def test_invalid_role_raises_before_any_write(role) -> None:
    adapter = MagicMock()
    with pytest.raises(ValueError):
        await _Intake(adapter).queue_episode_for_enrichment(
            episode="x", session=_session(), source="claude-code", role=role,
            user_statement="I have 20 coins",
        )
    adapter.create_pending_episode.assert_not_called()
    adapter.record_turn_evidence.assert_not_called()
    adapter.link_episode_admission.assert_not_called()


@pytest.mark.asyncio
async def test_text_prefix_never_becomes_a_role() -> None:
    adapter = MagicMock()
    await _Intake(adapter).queue_episode_for_enrichment(
        episode="assistant: hello there", session=_session(), source="claude-code", role="user",
    )
    assert adapter.create_pending_episode.call_args.kwargs["turn_role"] == "user"


# ---- IngestService.ingest_episode (review F11) ---------------------------------------------------


class _EnabledIntake(IngestIntakeMixin):
    def __init__(self):
        self._enrichment_enabled = True
        self.queue_episode_for_enrichment = AsyncMock(
            return_value=SimpleNamespace(status="rejected")
        )


@pytest.mark.asyncio
async def test_ingest_episode_forwards_role() -> None:
    intake = _EnabledIntake()
    await intake.ingest_episode("I moved", _session(), "smoke", role="user")
    assert intake.queue_episode_for_enrichment.await_args.kwargs["role"] == "user"


@pytest.mark.asyncio
async def test_ingest_episode_omits_role_when_not_set() -> None:
    intake = _EnabledIntake()
    await intake.ingest_episode("I moved", _session(), "smoke")
    assert "role" not in intake.queue_episode_for_enrichment.await_args.kwargs


# ---- RuntimeProviderDataOpsMixin.queue_episode ---------------------------------------------------


class _Backend(RuntimeProviderDataOpsMixin):
    def __init__(self) -> None:
        self.queue = AsyncMock(return_value={"episode_id": "ep-1", "status": "queued"})
        self.built = SimpleNamespace(ingest_service=SimpleNamespace(queue_episode_for_enrichment=self.queue))


@pytest.mark.asyncio
async def test_backend_queue_episode_forwards_role_when_set() -> None:
    backend = _Backend()
    await backend.queue_episode("hello", user_id="u", session_id="s", role="assistant")
    assert backend.queue.call_args.kwargs["role"] == "assistant"


@pytest.mark.asyncio
async def test_backend_queue_episode_omits_role_when_not_set() -> None:
    backend = _Backend()
    await backend.queue_episode("hello", user_id="u", session_id="s")
    assert "role" not in backend.queue.call_args.kwargs


# ---- MemoryGraphAdapter consolidation discovery --------------------------------------------------


def _adapter(evidence_exists: bool):
    adapter = object.__new__(MemoryGraphAdapter)
    turn_evidence = MagicMock()
    turn_evidence.evidence_exists.return_value = evidence_exists
    turn_evidence.list_dirty_evidence_namespaces.return_value = ["from-evidence"]
    turn_evidence.load_user_evidence.return_value = [{"text": "from-evidence"}]
    personal = MagicMock()
    personal.list_dirty_namespaces.return_value = ["from-episodic-prefix"]
    personal.load_user_episodes.return_value = [{"text": "from-episodic-prefix"}]
    adapter._turn_evidence = turn_evidence
    adapter._personal_memory = personal
    return adapter, turn_evidence, personal


def test_list_dirty_namespaces_reads_turn_evidence_even_when_none_exists() -> None:
    adapter, evidence, personal = _adapter(evidence_exists=False)
    assert adapter.list_dirty_namespaces() == ["from-evidence"]
    evidence.list_dirty_evidence_namespaces.assert_called_once()
    personal.list_dirty_namespaces.assert_not_called()


def test_load_user_episodes_reads_turn_evidence_even_when_none_exists() -> None:
    adapter, evidence, personal = _adapter(evidence_exists=False)
    assert adapter.load_user_episodes("ns") == [{"text": "from-evidence"}]
    evidence.load_user_evidence.assert_called_once()
    personal.load_user_episodes.assert_not_called()
