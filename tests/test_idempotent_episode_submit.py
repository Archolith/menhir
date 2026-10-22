"""Idempotent episode submit (client idempotency_key): offline behaviour.

Covers key derivation, the intake pre-check / replay / conflict / lost-race paths, the repository
query shape, the REST surface (409 + replay fields), backend forwarding, and the schema constraint.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from menhir.api.routes_support import MemoryRequest
from menhir.core.backend_client_ops import BackendClientOpsMixin
from menhir.core.backend_runtime_data_ops import RuntimeProviderDataOpsMixin
from menhir.domain import IngestStatus, MemorySession
from menhir.domain.ingest import (
    IdempotencyKeyConflict,
    episode_ingest_key,
    episode_request_digest,
)
from menhir.infrastructure.episode_lifecycle import EpisodeLifecycleRepository
from menhir.infrastructure.memory_graph_adapter import MemoryGraphAdapter
from menhir.infrastructure.schema import (
    PHASE_ONE_REQUIRED_CONSTRAINTS,
    get_phase1_bootstrap_queries,
)
from menhir.services.ingest_intake import IngestIntakeMixin

# Fixtures for the route tests (imported so pytest registers them in this module).
from tests.test_api_routes import fake_backend, fake_runtime_ctx, client  # noqa: F401

pytestmark = pytest.mark.unit

CONSTRAINT_CODE = "Neo.ClientError.Schema.ConstraintValidationFailed"


class _ConstraintError(Exception):
    """Mimics neo4j.exceptions.ClientError: the server code lives on `.code`."""

    code = CONSTRAINT_CODE


class _Intake(IngestIntakeMixin):
    def __init__(self, adapter, *, enrichment: bool = False):
        self.graph_adapter = adapter
        self._enrichment_enabled = enrichment
        self._ensure_enrichment_worker = AsyncMock()
        self._enqueue_pending_episode = AsyncMock()


def _session() -> MemorySession:
    return MemorySession(
        session_id="session-1", user_id="user-1",
        started_at=datetime.now(timezone.utc), client_name="opencode",
    )


_BASE_REQUEST = dict(
    content="body", diff=None, source="claude-code", flagged=False, bootstrap_scope=None,
    occurred_at=None, role=None, turn_evidence_uuid=None, user_statement=None,
)


def _digest(**overrides) -> str:
    return episode_request_digest(**{**_BASE_REQUEST, **overrides})


def _anchor(
    content: str, key_uuid: str = "ep-existing", state: str = "READY", **request_fields
) -> dict:
    """A stored anchor whose digest is that of a request with the given fields (defaults match
    the keyword defaults of queue_episode_for_enrichment plus source='claude-code')."""
    return {
        "uuid": key_uuid,
        "processing_state": state,
        "namespace": "default",
        "ingest_request_sha256": _digest(content=content, **request_fields),
    }


# ---- 1. key derivation ----------------------------------------------------------------------


def test_key_is_stable_for_same_inputs() -> None:
    assert episode_ingest_key("k", "ns-a") == episode_ingest_key("k", "ns-a")


def test_key_differs_by_namespace_and_by_key() -> None:
    assert episode_ingest_key("k", "ns-a") != episode_ingest_key("k", "ns-b")
    assert episode_ingest_key("k", "ns-a") != episode_ingest_key("k2", "ns-a")


def test_key_material_is_delimited_so_boundaries_cannot_collide() -> None:
    # "ab"+"c" vs "a"+"bc" must not collide.
    assert episode_ingest_key("c", "ab") != episode_ingest_key("bc", "a")


@pytest.mark.asyncio
async def test_none_namespace_and_default_namespace_derive_the_same_key_at_intake() -> None:
    keys = []
    for ns in (None, "default"):
        adapter = MagicMock()
        adapter.find_episode_by_ingest_key.return_value = None
        await _Intake(adapter).queue_episode_for_enrichment(
            episode="hello", session=_session(), source="claude-code",
            namespace=ns, idempotency_key="k1",
        )
        keys.append(adapter.create_pending_episode.call_args.kwargs["ingest_key"])
    assert keys[0] == keys[1]
    assert keys[0] == episode_ingest_key("k1", "default")


@pytest.mark.asyncio
async def test_distinct_namespaces_derive_distinct_keys_at_intake() -> None:
    keys = []
    for ns in ("ns-a", "ns-b"):
        adapter = MagicMock()
        adapter.find_episode_by_ingest_key.return_value = None
        await _Intake(adapter).queue_episode_for_enrichment(
            episode="hello", session=_session(), source="claude-code",
            namespace=ns, idempotency_key="k1",
        )
        keys.append(adapter.create_pending_episode.call_args.kwargs["ingest_key"])
    assert keys[0] != keys[1]


# ---- 2. no key: unchanged ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_key_create_called_without_ingest_kwargs_and_no_lookup() -> None:
    adapter = MagicMock()
    result = await _Intake(adapter).queue_episode_for_enrichment(
        episode="hello", session=_session(), source="claude-code",
    )
    kwargs = adapter.create_pending_episode.call_args.kwargs
    assert "ingest_key" not in kwargs
    assert "ingest_request_sha256" not in kwargs
    adapter.find_episode_by_ingest_key.assert_not_called()
    assert result.status == IngestStatus.QUEUED
    assert result.idempotent_replay is False
    assert result.processing_state is None


class _RecordingNeo4j:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query, params=None, **kwargs):
        self.calls.append((query, dict(params or {})))
        return []


def _create(repo: EpisodeLifecycleRepository, **extra) -> tuple[str, dict]:
    repo.neo4j = _RecordingNeo4j()
    repo.create_pending_episode(
        episode_uuid="u1", name="n", content="c", session_id="s", user_id="u",
        source="agent_inference", source_confidence=0.5, **extra,
    )
    return repo.neo4j.calls[0]


def test_repo_query_identical_with_and_without_explicit_none_ingest_kwargs() -> None:
    plain = _create(EpisodeLifecycleRepository())
    explicit_none = _create(
        EpisodeLifecycleRepository(), ingest_key=None, ingest_request_sha256=None
    )
    assert plain == explicit_none
    assert "ingest_key" not in plain[0]
    assert "ingest_key" not in plain[1]
    assert "ingest_request_sha256" not in plain[1]


def test_repo_query_stamps_key_and_digest_only_when_key_given() -> None:
    query, params = _create(
        EpisodeLifecycleRepository(), ingest_key="K", ingest_request_sha256="D"
    )
    assert "n.ingest_key = $ingest_key" in query
    assert "n.ingest_request_sha256 = $ingest_request_sha256" in query
    assert params["ingest_key"] == "K"
    assert params["ingest_request_sha256"] == "D"


def test_adapter_forwards_ingest_kwargs_only_when_key_set() -> None:
    adapter = object.__new__(MemoryGraphAdapter)
    adapter._episodes = MagicMock()
    base = dict(
        episode_uuid="u1", name="n", content="c", session_id="s", user_id="u",
        source="x", source_confidence=0.5,
    )
    adapter.create_pending_episode(**base)
    assert "ingest_key" not in adapter._episodes.create_pending_episode.call_args.kwargs
    assert "ingest_request_sha256" not in adapter._episodes.create_pending_episode.call_args.kwargs
    adapter.create_pending_episode(**base, ingest_key="K", ingest_request_sha256="D")
    kw = adapter._episodes.create_pending_episode.call_args.kwargs
    assert kw["ingest_key"] == "K" and kw["ingest_request_sha256"] == "D"


# ---- 3. first submit with key -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_submit_with_key_creates_with_key_and_digest_and_enqueues_once() -> None:
    adapter = MagicMock()
    adapter.find_episode_by_ingest_key.return_value = None
    intake = _Intake(adapter, enrichment=True)
    result = await intake.queue_episode_for_enrichment(
        episode="I moved to Lisbon", session=_session(), source="claude-code",
        namespace="ns-a", idempotency_key="k1",
    )
    kwargs = adapter.create_pending_episode.call_args.kwargs
    assert kwargs["ingest_key"] == episode_ingest_key("k1", "ns-a")
    assert kwargs["ingest_request_sha256"] == _digest(content="I moved to Lisbon")
    adapter.find_episode_by_ingest_key.assert_called_once_with(kwargs["ingest_key"])
    intake._enqueue_pending_episode.assert_awaited_once_with(kwargs["episode_uuid"])
    assert result.episode_id == kwargs["episode_uuid"]
    assert result.idempotent_replay is False


# ---- 4. replay -------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["PENDING", "READY"])
async def test_replay_returns_existing_anchor_and_does_no_other_writes(state) -> None:
    adapter = MagicMock()
    adapter.find_episode_by_ingest_key.return_value = _anchor(
        "same", "ep-old", state, source="user", turn_evidence_uuid="te-1",
        user_statement="I said this",
    )
    intake = _Intake(adapter, enrichment=True)
    result = await intake.queue_episode_for_enrichment(
        episode="same", session=_session(), source="user",
        idempotency_key="k1", user_statement="I said this", turn_evidence_uuid="te-1",
    )
    assert result.episode_id == "ep-old"
    assert result.idempotent_replay is True
    assert result.processing_state == state
    adapter.create_pending_episode.assert_not_called()
    adapter.record_turn_evidence.assert_not_called()
    adapter.link_episode_admission.assert_not_called()
    adapter.create_evidence_projection.assert_not_called()
    adapter.record_admission_audit.assert_not_called()
    intake._enqueue_pending_episode.assert_not_awaited()
    intake._ensure_enrichment_worker.assert_not_awaited()


# ---- 5. conflict -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_anchor_with_different_content_raises_conflict_not_failed() -> None:
    adapter = MagicMock()
    adapter.find_episode_by_ingest_key.return_value = _anchor("original")
    with pytest.raises(IdempotencyKeyConflict):
        await _Intake(adapter).queue_episode_for_enrichment(
            episode="changed", session=_session(), source="claude-code", idempotency_key="k1",
        )
    adapter.create_pending_episode.assert_not_called()


@pytest.mark.asyncio
async def test_conflict_on_lost_race_with_different_content_propagates() -> None:
    adapter = MagicMock()
    adapter.find_episode_by_ingest_key.side_effect = [None, _anchor("winner content")]
    adapter.create_pending_episode.side_effect = _ConstraintError("dup")
    with pytest.raises(IdempotencyKeyConflict):
        await _Intake(adapter).queue_episode_for_enrichment(
            episode="loser content", session=_session(), source="claude-code",
            idempotency_key="k1",
        )


# ---- 6. lost race ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_constraint_violation_on_create_returns_winner_as_replay() -> None:
    adapter = MagicMock()
    adapter.find_episode_by_ingest_key.side_effect = [None, _anchor("body", "ep-winner", "PENDING")]
    adapter.create_pending_episode.side_effect = _ConstraintError("dup")
    intake = _Intake(adapter, enrichment=True)
    result = await intake.queue_episode_for_enrichment(
        episode="body", session=_session(), source="claude-code", idempotency_key="k1",
    )
    assert result.episode_id == "ep-winner"
    assert result.idempotent_replay is True
    assert result.status == IngestStatus.QUEUED
    assert adapter.find_episode_by_ingest_key.call_count == 2
    intake._enqueue_pending_episode.assert_not_awaited()


@pytest.mark.asyncio
async def test_constraint_violation_without_key_is_todays_failed_handling() -> None:
    adapter = MagicMock()
    adapter.create_pending_episode.side_effect = _ConstraintError("dup")
    result = await _Intake(adapter).queue_episode_for_enrichment(
        episode="body", session=_session(), source="claude-code",
    )
    assert result.status == IngestStatus.FAILED
    assert result.episode_id == ""
    adapter.find_episode_by_ingest_key.assert_not_called()


@pytest.mark.asyncio
async def test_constraint_violation_but_reread_finds_nothing_is_failed() -> None:
    adapter = MagicMock()
    adapter.find_episode_by_ingest_key.return_value = None
    adapter.create_pending_episode.side_effect = _ConstraintError("dup")
    result = await _Intake(adapter).queue_episode_for_enrichment(
        episode="body", session=_session(), source="claude-code", idempotency_key="k1",
    )
    assert result.status == IngestStatus.FAILED
    assert result.idempotent_replay is False
    assert adapter.find_episode_by_ingest_key.call_count == 2


@pytest.mark.asyncio
async def test_non_constraint_error_with_key_is_failed_without_reread() -> None:
    adapter = MagicMock()
    adapter.find_episode_by_ingest_key.return_value = None
    adapter.create_pending_episode.side_effect = RuntimeError("boom")
    result = await _Intake(adapter).queue_episode_for_enrichment(
        episode="body", session=_session(), source="claude-code", idempotency_key="k1",
    )
    assert result.status == IngestStatus.FAILED
    assert adapter.find_episode_by_ingest_key.call_count == 1


# ---- 7. route + request model ----------------------------------------------------------------


def test_route_maps_conflict_to_409(client, fake_backend) -> None:  # noqa: F811
    fake_backend.queue_episode.side_effect = IdempotencyKeyConflict("different content")
    resp = client.post("/api/memory", json={"episode": "x", "idempotency_key": "k1"})
    assert resp.status_code == 409
    assert "different content" in resp.json()["detail"]


def test_route_passes_key_and_surfaces_replay_fields(client, fake_backend) -> None:  # noqa: F811
    fake_backend.queue_episode.return_value = {
        "episode_id": "ep-old", "status": "queued",
        "idempotent_replay": True, "processing_state": "READY",
    }
    resp = client.post("/api/memory", json={"episode": "x", "idempotency_key": "k1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["episode_id"] == "ep-old"
    assert data["idempotent_replay"] is True
    assert data["processing_state"] == "READY"
    assert fake_backend.queue_episode.await_args.kwargs["idempotency_key"] == "k1"


def test_route_without_key_omits_kwarg_and_reports_no_replay(client, fake_backend) -> None:  # noqa: F811
    resp = client.post("/api/memory", json={"episode": "x"})
    assert resp.status_code == 200
    assert resp.json()["idempotent_replay"] is False
    assert resp.json()["processing_state"] is None
    assert "idempotency_key" not in fake_backend.queue_episode.await_args.kwargs


def test_memory_request_key_length_bounds() -> None:
    assert MemoryRequest(episode="x").idempotency_key is None
    assert MemoryRequest(episode="x", idempotency_key="k").idempotency_key == "k"
    assert MemoryRequest(episode="x", idempotency_key="k" * 256).idempotency_key == "k" * 256
    with pytest.raises(ValidationError):
        MemoryRequest(episode="x", idempotency_key="")
    with pytest.raises(ValidationError):
        MemoryRequest(episode="x", idempotency_key="k" * 257)


# ---- 8. backend forwarding -------------------------------------------------------------------


class _Runtime(RuntimeProviderDataOpsMixin):
    def __init__(self) -> None:
        self.queue = AsyncMock(return_value={"episode_id": "ep-1", "status": "queued"})
        self.built = SimpleNamespace(
            ingest_service=SimpleNamespace(queue_episode_for_enrichment=self.queue)
        )


class _Client(BackendClientOpsMixin):
    def __init__(self) -> None:
        self._request = AsyncMock(return_value={"episode_id": "ep-1"})


@pytest.mark.asyncio
async def test_runtime_ops_omit_key_when_none_and_send_when_set() -> None:
    backend = _Runtime()
    await backend.queue_episode("hello", user_id="u", session_id="s")
    assert "idempotency_key" not in backend.queue.call_args.kwargs
    await backend.queue_episode("hello", user_id="u", session_id="s", idempotency_key="k1")
    assert backend.queue.call_args.kwargs["idempotency_key"] == "k1"


@pytest.mark.asyncio
async def test_client_ops_omit_key_when_none_and_send_when_set() -> None:
    backend = _Client()
    await backend.queue_episode("hello", user_id="u", session_id="s")
    op, params = backend._request.await_args.args
    assert op == "queue_episode"
    assert "idempotency_key" not in params
    await backend.queue_episode("hello", user_id="u", session_id="s", idempotency_key="k1")
    assert backend._request.await_args.args[1]["idempotency_key"] == "k1"


# ---- 9. schema -------------------------------------------------------------------------------


def test_unique_constraint_is_bootstrapped_and_required() -> None:
    ddl = [q for q in get_phase1_bootstrap_queries() if "episodic_ingest_key_unique" in q]
    assert len(ddl) == 1
    assert "IF NOT EXISTS" in ddl[0]
    assert "(n:Episodic)" in ddl[0] and "n.ingest_key IS UNIQUE" in ddl[0]
    assert (
        "episodic_ingest_key_unique", "UNIQUENESS", "NODE", ("Episodic",), ("ingest_key",),
    ) in PHASE_ONE_REQUIRED_CONSTRAINTS


# ---- 10. request digest: every caller-supplied field is part of the replay identity ----------

# (field changed, request fields of the STORED anchor, request fields of the REPLAY). Everything
# not listed is the same on both sides, so exactly one caller-supplied field differs.
_DIGEST_FIELD_CASES = [
    ("diff", dict(diff="--- a\n+++ b"), dict(diff="--- a\n+++ c")),
    ("source", dict(source="claude-code"), dict(source="codex")),
    ("flagged", dict(flagged=False), dict(flagged=True)),
    (
        "bootstrap_scope",
        dict(flagged=True, bootstrap_scope="general"),
        dict(flagged=True, bootstrap_scope="workspace:alpha"),
    ),
    (
        "occurred_at",
        dict(occurred_at="2026-01-01T00:00:00Z"),
        dict(occurred_at="2026-01-02T00:00:00Z"),
    ),
    ("role", dict(role="user"), dict(role="assistant")),
    ("turn_evidence_uuid", dict(turn_evidence_uuid="te-1"), dict(turn_evidence_uuid="te-2")),
    ("user_statement", dict(user_statement="I said A"), dict(user_statement="I said B")),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored,replayed", [c[1:] for c in _DIGEST_FIELD_CASES], ids=[c[0] for c in _DIGEST_FIELD_CASES]
)
async def test_changing_any_one_caller_supplied_field_with_the_same_key_conflicts(
    stored, replayed
) -> None:
    """Fails if episode_request_digest (or the intake call into it) ignores that field: the
    replay would be accepted as a no-op and the caller's changed request silently dropped."""
    adapter = MagicMock()
    adapter.find_episode_by_ingest_key.return_value = _anchor("body", **stored)
    request = {**dict(source="claude-code"), **replayed}
    with pytest.raises(IdempotencyKeyConflict):
        await _Intake(adapter).queue_episode_for_enrichment(
            episode="body", session=_session(), idempotency_key="k1", **request,
        )
    adapter.create_pending_episode.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_fields", [c[1] for c in _DIGEST_FIELD_CASES], ids=[c[0] for c in _DIGEST_FIELD_CASES]
)
async def test_identical_request_with_each_field_set_is_a_replay_not_a_conflict(
    request_fields,
) -> None:
    """Control for the test above (same field values on both sides -> replay), so a conflict
    there is attributable to the one field and not to a mismatched anchor builder."""
    adapter = MagicMock()
    adapter.find_episode_by_ingest_key.return_value = _anchor("body", "ep-old", **request_fields)
    request = {**dict(source="claude-code"), **request_fields}
    result = await _Intake(adapter).queue_episode_for_enrichment(
        episode="body", session=_session(), idempotency_key="k1", **request,
    )
    assert result.idempotent_replay is True
    assert result.episode_id == "ep-old"


@pytest.mark.asyncio
async def test_same_request_from_a_different_session_and_user_is_a_replay() -> None:
    """Fails if session_id/user_id leak into the digest: the route mints a fresh session when the
    caller names none, so every honest retry would become a 409."""
    adapter = MagicMock()
    adapter.find_episode_by_ingest_key.return_value = _anchor("body", "ep-old")
    other = MemorySession(
        session_id="a-completely-different-session", user_id="someone-else",
        started_at=datetime.now(timezone.utc), client_name="other-client",
    )
    assert other.session_id != _session().session_id and other.user_id != _session().user_id
    result = await _Intake(adapter).queue_episode_for_enrichment(
        episode="body", session=other, source="claude-code", idempotency_key="k1",
    )
    assert result.idempotent_replay is True
    adapter.create_pending_episode.assert_not_called()


def test_digest_function_is_sensitive_to_each_field_and_blind_to_session_arguments() -> None:
    """Direct pin on the digest: each field moves it; it accepts no session/user input."""
    base = _digest()
    assert base == _digest()
    for name, stored, replayed in _DIGEST_FIELD_CASES:
        assert _digest(**stored) != _digest(**replayed), name
    with pytest.raises(TypeError):
        episode_request_digest(**_BASE_REQUEST, session_id="s")  # type: ignore[call-arg]


# ---- 11. namespace spellings share one key ----------------------------------------------------


@pytest.mark.asyncio
async def test_empty_blank_none_and_default_namespace_all_derive_one_key_at_intake() -> None:
    """Fails if intake hashes the raw namespace instead of normalize_namespace(namespace):
    the same silo reached by a different spelling would get a different key and double-write."""
    keys = []
    for ns in ("", "  ", None, "default"):
        adapter = MagicMock()
        adapter.find_episode_by_ingest_key.return_value = None
        await _Intake(adapter).queue_episode_for_enrichment(
            episode="hello", session=_session(), source="claude-code",
            namespace=ns, idempotency_key="k1",
        )
        keys.append(adapter.create_pending_episode.call_args.kwargs["ingest_key"])
    assert len(set(keys)) == 1, keys
    assert keys[0] == episode_ingest_key("k1", "default")
    # A real, different namespace must still be a different key.
    adapter = MagicMock()
    adapter.find_episode_by_ingest_key.return_value = None
    await _Intake(adapter).queue_episode_for_enrichment(
        episode="hello", session=_session(), source="claude-code",
        namespace="other", idempotency_key="k1",
    )
    assert adapter.create_pending_episode.call_args.kwargs["ingest_key"] != keys[0]
