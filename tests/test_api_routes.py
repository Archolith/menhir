"""Tests for REST API routes against the backend/provider seam."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api import routes as api_routes
from menhir.api.routes import router
from menhir.domain.recall import InvalidQueryPresetError


@pytest.fixture
def fake_backend():
    backend = SimpleNamespace()
    backend.recall = AsyncMock(
        return_value={
            "query": "test",
            "preset": "knowledge",
            "results": [
                {
                    "uuid": "node-1",
                    "name": "Miso",
                    "content": "Dog named Miso",
                    "scope": "PERSISTENT",
                    "memory_type": "pet",
                    "final_score": 0.98,
                }
            ],
            "candidates_evaluated": 1,
        }
    )
    backend.build_context = AsyncMock(
        return_value={
            "query": "test",
            "context": "Some context",
            "token_estimate": 50,
            "memory_count": 2,
            "truncated": False,
            "preset": "knowledge",
        }
    )
    backend.queue_episode = AsyncMock(
        return_value={
            "episode_id": "ep-123",
            "status": "queued",
            "nodes_touched": 0,
            "edges_touched": 0,
        }
    )
    backend.delete_memory = AsyncMock(return_value=True)
    backend.flag_memory = AsyncMock(return_value=True)
    backend.unflag_memory = AsyncMock(return_value=True)
    backend.fetch_operation_stats = AsyncMock(return_value=[])
    backend.fetch_enrichment_rate = AsyncMock(return_value={"total": 10, "successes": 8})
    backend.get_queue_depth = AsyncMock(return_value=5)
    backend.scheduler_status_snapshot = AsyncMock(return_value={"running": True})
    backend.delete_namespace = AsyncMock(return_value={"namespace": "ns-bench", "deleted": 7})
    backend.fetch_flagged_memories = AsyncMock(
        return_value=[{"uuid": "pin-1", "content": "general pin", "bootstrap_scope": "general"}]
    )
    backend.fetch_flagged_memory_bootstrap_version = AsyncMock(
        return_value="workspace:alpha:1:digest"
    )
    backend.fetch_recent_memories = AsyncMock(
        return_value=[{"uuid": "recent-1", "content": "alpha recent", "user_flagged": False}]
    )
    return backend


@pytest.fixture
def fake_runtime_ctx():
    capabilities = SimpleNamespace(
        startup_mode="full",
        neo4j_ready=True,
        embedder_ready=True,
        llm_ready=True,
        scheduler_ready=True,
        reads_ready=True,
        queue_writes_ready=True,
        enrichment_ready=True,
        graphiti_ready=True,
        failures=[],
    )
    graph_adapter = SimpleNamespace(
        list_dirty_namespaces=lambda *, limit=500: ["ns-bench"],
        count_turn_evidence=lambda ns: 2,
        list_counters=lambda *, namespace=None, limit=100: [
            {"subject": "movies", "counter": "watchlist", "value": 20,
             "valid_at": "2026-07-01T00:00:00Z"},
            {"subject": "perception", "counter": "abstain_bikes", "value": 1,
             "valid_at": "2026-07-01T00:00:00Z"},
        ],
        counter_history=lambda *, subject, counter, namespace=None: [
            {"value": 20, "current": True, "valid_at": "2026-07-02T00:00:00Z", "expired_at": None},
            {"value": 25, "current": False, "valid_at": "2026-07-01T00:00:00Z",
             "expired_at": "2026-07-02T00:00:00Z"},
        ],
        purge_turn_evidence=lambda ns: 3,
        fetch_scalar_authority_contributors=lambda **kwargs: {
            "contributors": [{"assertion_id": "a1", "relation": "CURRENT_ANCHOR"}],
            "total": 1, "next_offset": None,
        },
        record_turn_evidence=Mock(return_value={
            "turn_id": "te-1", "created": True, "recorded_at": "2026-07-07T00:00:00Z",
        }),
    )
    built = SimpleNamespace(
        ingest_service=SimpleNamespace(
            enrichment_enabled=lambda: True,
            wait_for_episode_processing=AsyncMock(return_value={"processing_state": "READY"}),
        ),
        scheduler=SimpleNamespace(status_snapshot=lambda: {"running": True}),
        graph_adapter=graph_adapter,
        settings=SimpleNamespace(
            personal_memory_consolidation_chat_model="pm-model",
            personal_memory_scalar_state_enabled=True,
            personal_memory_scalar_state_perceiver_version="v2",
            personal_memory_scalar_history_enabled=True,
        ),
    )
    return SimpleNamespace(
        built=built,
        session=SimpleNamespace(session_id="process-session", user_id="claude-code"),
        capabilities=capabilities,
    )


@pytest.fixture
def client(fake_backend, fake_runtime_ctx):
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = fake_runtime_ctx
    with patch.object(api_routes, "_get_backend", return_value=fake_backend):
        yield TestClient(app)


class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["services"]["neo4j"] == "ok"
        assert data["services"]["scheduler"] == "ok"

    def test_health_starting(self):
        app = FastAPI()
        app.include_router(router)
        c = TestClient(app)
        resp = c.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "starting"


class TestRecall:
    def test_recall_basic(self, client, fake_backend):
        resp = client.post("/api/recall", json={"query": "test query"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "test"
        assert data["preset"] == "knowledge"
        assert data["results"][0]["uuid"] == "node-1"
        assert data["results"][0]["temporal_facts"] == []
        fake_backend.recall.assert_awaited_once_with(
            "test query", preset="knowledge", limit=10, include_session=False,
            include_superseded=False, include_invalidated=False, namespace=None,
        )

    def test_recall_can_request_historical_source_time_evidence(self, client, fake_backend):
        response = client.post(
            "/api/recall",
            json={"query": "What changed?", "include_invalidated": True},
        )

        assert response.status_code == 200
        assert fake_backend.recall.await_args.kwargs["include_invalidated"] is True

    def test_recall_threads_source_and_belief_time_without_conflating_them(
        self, client, fake_backend
    ):
        fake_backend.recall.return_value["results"][0]["temporal_facts"] = [{
            "fact": "French press ratio uses 5 oz of water",
            "valid_at": "2023-06-30T11:33:00Z",
            "invalid_at": None,
            "created_at": "2026-07-30T05:00:00Z",
            "expired_at": None,
            "is_current_belief": True,
            "temporal_role": "current_belief",
        }]

        data = client.post("/api/recall", json={"query": "French press ratio"}).json()
        temporal_fact = data["results"][0]["temporal_facts"][0]

        assert temporal_fact["fact"] == "French press ratio uses 5 oz of water"
        assert temporal_fact["valid_at"] == "2023-06-30T11:33:00Z"
        assert temporal_fact["created_at"] == "2026-07-30T05:00:00Z"

    def test_recall_threads_structured_authority_layer(self, client, fake_backend):
        fake_backend.recall.return_value["authority_layer"] = [{
            "kind": "current", "status": "leads", "subject_uuid": "ent-self",
            "attribute": "owned", "scope": "", "value_kind": "count", "unit": "",
            "value": 37, "valid_at": "2026-07-02T00:00:00Z", "view_uuid": "view-37",
            "has_foundation": True, "contributors": [], "contributors_total": 0,
            "contributors_truncated": False, "next_offset": None,
        }]

        data = client.post("/api/recall", json={"query": "how many now"}).json()

        assert data["authority_layer"][0]["status"] == "leads"
        assert data["authority_layer"][0]["value"] == 37

    def test_recall_threads_leading_event_authority_verdict(self, client, fake_backend):
        fake_backend.recall.return_value["event_authority_layer"] = [{
            "predicate": "acquired", "object_key": "notebook-b",
            "object_display": "a red notebook", "valid_at": "2026-02-05T00:00:00Z",
            "stated_span": "I bought a red notebook.", "assertion_key": "a1",
            "episode_uuid": "ep-1", "turn_evidence_uuid": "te-1",
            "domain": "acquisition", "time_basis": "explicit", "status": "leads",
            "gate": "pass", "reason": "unique grounded lead",
            "subject_uuid": "self", "has_foundation": True, "kind": "latest",
        }]

        data = client.post(
            "/api/recall",
            json={"query": "which notebook did I buy most recently?"},
        ).json()

        verdict = data["event_authority_layer"][0]
        assert verdict["status"] == "leads"
        assert verdict["gate"] == "pass"
        assert verdict["object_key"] == "notebook-b"
        assert verdict["valid_at"] == "2026-02-05T00:00:00Z"
        assert verdict["stated_span"] == "I bought a red notebook."
        assert verdict["kind"] == "latest"
        assert verdict["time_basis"] == "explicit"

    def test_scalar_authority_contributor_expansion(self, client):
        response = client.get(
            "/api/scalar-authority/view-37/contributors",
            params={"namespace": "proj", "limit": 1, "offset": 0},
        )

        assert response.status_code == 200
        assert response.json()["contributors"][0]["relation"] == "CURRENT_ANCHOR"

    def test_bootstrap_context_requires_matching_flagged_receipt(self, client):
        response = client.post(
            "/api/bootstrap/context",
            json={"reader_id": "fresh-reader", "workspace": "alpha", "namespace": "alpha"},
        )

        assert response.status_code == 409

    def test_bootstrap_flagged_receipt_unlocks_scoped_context(self, client):
        flagged = client.get(
            "/api/bootstrap/flagged",
            params={"reader_id": "scoped-reader", "workspace": "alpha"},
        )
        context = client.post(
            "/api/bootstrap/context",
            json={"reader_id": "scoped-reader", "workspace": "alpha", "namespace": "alpha"},
        )

        assert flagged.status_code == 200
        assert flagged.json()["bootstrap_selection"] == "workspace:alpha"
        assert context.status_code == 200
        assert context.json()["bootstrap_verified"] is True
        assert context.json()["recent"][0]["uuid"] == "recent-1"

    def test_bootstrap_context_defensively_excludes_legacy_structure(
        self, client, fake_backend
    ):
        fake_backend.fetch_recent_memories.return_value = [
            {
                "uuid": "legacy-directory",
                "content": "Directory: src/legacy",
                "source": "project-scan",
                "structure_role": None,
                "user_flagged": False,
            },
            {
                "uuid": "recent-1",
                "content": "semantic recent",
                "source": "codex",
                "structure_role": None,
                "user_flagged": False,
            },
        ]
        flagged = client.get(
            "/api/bootstrap/flagged",
            params={"reader_id": "legacy-filter-reader", "workspace": "alpha"},
        )
        context = client.post(
            "/api/bootstrap/context",
            json={
                "reader_id": "legacy-filter-reader",
                "workspace": "alpha",
                "namespace": "alpha",
            },
        )

        assert flagged.status_code == 200
        assert context.status_code == 200
        assert [row["uuid"] for row in context.json()["recent"]] == ["recent-1"]

    def test_recall_with_preset(self, client, fake_backend):
        resp = client.post("/api/recall", json={"query": "test", "preset": "recent", "limit": 5})
        assert resp.status_code == 200
        fake_backend.recall.assert_awaited_once_with(
            "test", preset="recent", limit=5, include_session=False,
            include_superseded=False, include_invalidated=False, namespace=None,
        )

    def test_recall_invalid_preset_returns_422(self, client, fake_backend):
        fake_backend.recall.side_effect = InvalidQueryPresetError(
            "Invalid preset 'brief'. Use: recent, knowledge, emotional, connected, conflict."
        )

        resp = client.post("/api/recall", json={"query": "test", "preset": "brief"})

        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "Invalid preset 'brief'. Use: recent, knowledge, emotional, connected, conflict."
        )


class TestContext:
    def test_context_basic(self, client, fake_backend):
        resp = client.post("/api/context", json={"query": "how does X work"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["context"] == "Some context"
        assert data["memory_count"] == 2
        fake_backend.build_context.assert_awaited_once_with(
            "how does X work",
            max_tokens=2000,
            preset="knowledge",
            include_scores=False,
            namespace=None,
        )

    def test_context_invalid_preset_returns_422(self, client, fake_backend):
        fake_backend.build_context.side_effect = InvalidQueryPresetError(
            "Invalid preset 'brief'. Use: recent, knowledge, emotional, connected, conflict."
        )

        resp = client.post("/api/context", json={"query": "how", "preset": "brief"})

        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "Invalid preset 'brief'. Use: recent, knowledge, emotional, connected, conflict."
        )


class TestIngest:
    def test_ingest_memory_uses_caller_session_defaults(self, client, fake_backend):
        resp = client.post("/api/memory", json={"episode": "user said hello"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["episode_id"] == "ep-123"
        assert data["status"] == "queued"
        assert "session_id" in data
        fake_backend.queue_episode.assert_awaited_once()
        assert fake_backend.queue_episode.await_args.kwargs["user_id"] == "remote-api"

    def test_ingest_with_explicit_session(self, client, fake_backend):
        resp = client.post(
            "/api/memory",
            json={
                "episode": "test",
                "session_id": "my-session",
                "user_id": "claude",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "my-session"
        fake_backend.queue_episode.assert_awaited_once_with(
            "test",
            user_id="claude",
            session_id="my-session",
            source="remote-api",
            diff=None,
            namespace=None,
            occurred_at=None,
            turn_evidence_uuid=None,
        )

    def test_ingest_wait_uses_episode_processing_wait(self, client, fake_runtime_ctx):
        resp = client.post("/api/memory?wait=true", json={"episode": "user said hello"})
        assert resp.status_code == 200
        fake_runtime_ctx.built.ingest_service.wait_for_episode_processing.assert_awaited_once_with(
            "ep-123",
            timeout_s=60.0,
        )


class TestDeleteAndFlag:
    def test_delete_memory(self, client, fake_backend):
        resp = client.delete("/api/memory/some-uuid")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        fake_backend.delete_memory.assert_awaited_once_with("some-uuid")

    def test_flag_memory(self, client, fake_backend):
        resp = client.post("/api/memory/some-uuid/flag")
        assert resp.status_code == 200
        assert resp.json()["flagged"] is True
        fake_backend.flag_memory.assert_awaited_once_with("some-uuid")

    def test_unflag_memory(self, client, fake_backend):
        resp = client.post("/api/memory/some-uuid/unflag")
        assert resp.status_code == 200
        assert resp.json()["unflagged"] is True
        fake_backend.unflag_memory.assert_awaited_once_with("some-uuid")


class TestTurnEvidence:
    def _record_mock(self, fake_runtime_ctx):
        return fake_runtime_ctx.built.graph_adapter.record_turn_evidence

    def test_accepts_old_payload_shape(self, client, fake_runtime_ctx):
        # Back-compat: a pre-provenance client sends no source_client/hook_version and still works;
        # those kwargs are forwarded as None.
        resp = client.post("/api/turn-evidence", json={
            "text": "I have 25 movies on my watch list.",
            "role": "user", "declarant": "user", "session_id": "s1", "namespace": "proj",
            "source_kind": "claude_code_hook", "triage_reason": ["number", "i_have"],
            "triage_version": "claude-hook-v1",
        })
        assert resp.status_code == 200
        assert resp.json()["turn_id"] == "te-1" and resp.json()["created"] is True
        kwargs = self._record_mock(fake_runtime_ctx).call_args.kwargs
        assert kwargs["source_client"] is None and kwargs["hook_version"] is None
        assert kwargs["source_kind"] == "claude_code_hook"

    def test_accepts_new_metadata_fields(self, client, fake_runtime_ctx):
        resp = client.post("/api/turn-evidence", json={
            "text": "I have 25 movies on my watch list.",
            "session_id": "s1", "namespace": "proj",
            "source_kind": "claude_code_hook", "source_client": "claude_code",
            "hook_version": "menhir-turn-evidence-hook-v1",
            "triage_reason": ["number"], "triage_version": "claude-hook-v1",
            "metadata": {"project_root": "/repo", "git_branch": "main",
                         "git_commit": "abc1234", "prompt_hash": "deadbeef"},
        })
        assert resp.status_code == 200
        kwargs = self._record_mock(fake_runtime_ctx).call_args.kwargs
        assert kwargs["source_client"] == "claude_code"
        assert kwargs["hook_version"] == "menhir-turn-evidence-hook-v1"
        assert kwargs["metadata"]["git_branch"] == "main"
        assert kwargs["metadata"]["project_root"] == "/repo"

    def test_accepts_replay_world_time(self, client, fake_runtime_ctx):
        self._record_mock(fake_runtime_ctx).return_value = {
            "turn_id": "te-historic",
            "created": True,
            "recorded_at": "2026-07-29T13:00:00Z",
            "occurred_at": "2023-11-30T20:25:00Z",
        }
        resp = client.post("/api/turn-evidence", json={
            "text": "I have added 25 postcards.",
            "namespace": "proj",
            "occurred_at": "2023-11-30T20:25:00Z",
        })

        assert resp.status_code == 200
        assert resp.json()["occurred_at"] == "2023-11-30T20:25:00Z"
        assert self._record_mock(fake_runtime_ctx).call_args.kwargs["occurred_at"] == (
            "2023-11-30T20:25:00Z"
        )

    def test_missing_text_returns_400(self, client):
        resp = client.post("/api/turn-evidence", json={"text": "   "})
        assert resp.status_code == 400


class TestStats:
    def test_stats(self, client, fake_backend):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["queue_depth"] == 5
        assert data["enrichment_enabled"] is True
        assert data["scheduler"]["running"] is True
        fake_backend.fetch_operation_stats.assert_awaited_once_with(since_hours=24)
        fake_backend.fetch_enrichment_rate.assert_awaited_once_with(since_hours=24)
        fake_backend.get_queue_depth.assert_awaited_once()
        fake_backend.scheduler_status_snapshot.assert_awaited_once()


class TestPhase3:
    def test_run_invokes_consolidation_and_reports_metrics(self, client):
        consolidate = AsyncMock(
            return_value={
                "namespaces_dirty": 1,
                "namespaces_processed": 1,
                "views_written": 2,
                "abstained": 0,
                "corrections_applied": 0,
                "llm_calls": 6,
                "scalar_namespaces_processed": 1,
                "scalar_states_written": 2,
                "scalar_advisory": 1,
                "scalar_repair_pending": 0,
            }
        )
        with patch(
            "menhir.infrastructure.sync_llm.make_sync_chat",
            return_value=lambda prompt, user: "x",
        ), patch(
            "menhir.infrastructure.view_embedder.make_view_embedder", return_value=None
        ), patch(
            "menhir.services.scheduler_tasks.consolidate_personal_memory", new=consolidate
        ):
            resp = client.post("/api/phase3/run", json={"namespace": "ns-bench", "k": 3})
        assert resp.status_code == 200
        data = resp.json()
        assert data["phase3_selected"] is True
        assert data["views_written"] == 2
        assert data["llm_calls"] == 6
        assert data["scalar_enabled"] is True
        assert data["scalar_states_written"] == 2
        assert consolidate.await_args.kwargs["namespaces"] == ["ns-bench"]
        assert consolidate.await_args.kwargs["k"] == 3
        assert consolidate.await_args.kwargs["enable_scalar_state"] is True
        assert consolidate.await_args.kwargs["scalar_state_perceiver_version"] == "v2"
        assert consolidate.await_args.kwargs["scalar_history_enabled"] is True

    def test_run_can_skip_counter_state_for_scalar_only_build(self, client):
        consolidate = AsyncMock(return_value={"scalar_namespaces_processed": 1, "llm_calls": 3})
        with patch(
            "menhir.infrastructure.sync_llm.make_sync_chat",
            return_value=lambda prompt, user: "x",
        ), patch(
            "menhir.infrastructure.view_embedder.make_view_embedder", return_value=None
        ), patch(
            "menhir.services.scheduler_tasks.consolidate_personal_memory", new=consolidate
        ):
            resp = client.post(
                "/api/phase3/run",
                json={"namespace": "ns-bench", "counter_state": False},
            )
        assert resp.status_code == 200
        assert consolidate.await_args.kwargs["enable_counter_state"] is False

    def test_run_503_when_no_chat_provider(self, client):
        with patch(
            "menhir.infrastructure.sync_llm.make_sync_chat", return_value=None
        ), patch(
            "menhir.infrastructure.view_embedder.make_view_embedder", return_value=None
        ):
            resp = client.post("/api/phase3/run", json={"namespace": "ns-bench"})
        assert resp.status_code == 503

    def test_run_requires_namespace(self, client):
        resp = client.post("/api/phase3/run", json={"namespace": ""})
        assert resp.status_code == 422

    def test_run_off_event_history_defaults_forwarded_and_metrics_zero(self, client):
        consolidate = AsyncMock(return_value={})
        with patch(
            "menhir.infrastructure.sync_llm.make_sync_chat",
            return_value=lambda prompt, user: "x",
        ), patch(
            "menhir.infrastructure.view_embedder.make_view_embedder", return_value=None
        ), patch(
            "menhir.services.scheduler_tasks.consolidate_personal_memory", new=consolidate
        ):
            resp = client.post("/api/phase3/run", json={"namespace": "ns-bench"})
        assert resp.status_code == 200
        kwargs = consolidate.await_args.kwargs
        assert kwargs["enable_event_history"] is False
        assert kwargs["event_history_perceiver_version"] == "v1"
        assert kwargs["event_batch_size"] == 500
        data = resp.json()
        assert data["event_history_enabled"] is False
        for key in (
            "event_namespaces_processed",
            "event_namespaces_failed",
            "event_assertions_recorded",
            "event_assertions_created",
            "event_views_rebuilt",
            "event_llm_calls",
        ):
            assert data[key] == 0

    def test_run_event_history_on_forwards_and_reports_bounded_metrics(
        self, client, fake_runtime_ctx
    ):
        fake_runtime_ctx.built.settings.personal_memory_event_history_enabled = True
        fake_runtime_ctx.built.settings.personal_memory_event_history_perceiver_version = "v9"
        fake_runtime_ctx.built.settings.personal_memory_event_history_batch_size = 42
        consolidate = AsyncMock(return_value={
            "event_namespaces_processed": 3,
            "event_namespaces_failed": 1,
            "event_assertions_recorded": 11,
            "event_assertions_created": 7,
            "event_views_rebuilt": 2,
            "event_llm_calls": 4,
        })
        with patch(
            "menhir.infrastructure.sync_llm.make_sync_chat",
            return_value=lambda prompt, user: "x",
        ), patch(
            "menhir.infrastructure.view_embedder.make_view_embedder", return_value=None
        ), patch(
            "menhir.services.scheduler_tasks.consolidate_personal_memory", new=consolidate
        ):
            resp = client.post("/api/phase3/run", json={"namespace": "ns-bench"})
        assert resp.status_code == 200
        kwargs = consolidate.await_args.kwargs
        assert kwargs["enable_event_history"] is True
        assert kwargs["event_history_perceiver_version"] == "v9"
        assert kwargs["event_batch_size"] == 42
        data = resp.json()
        assert data["event_history_enabled"] is True
        assert data["event_namespaces_processed"] == 3
        assert data["event_namespaces_failed"] == 1
        assert data["event_assertions_recorded"] == 11
        assert data["event_assertions_created"] == 7
        assert data["event_views_rebuilt"] == 2
        assert data["event_llm_calls"] == 4

    def test_status_reports_dirty_and_evidence(self, client):
        resp = client.get("/api/phase3/status", params={"namespace": "ns-bench"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["dirty"] is True
        assert data["turn_evidence"] == 2

    def test_views_splits_user_views_from_perception_receipts(self, client):
        resp = client.get("/api/views", params={"namespace": "ns-bench"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 1
        view = data["views"][0]
        assert view["subject"] == "movies"
        assert view["value"] == 20
        # history carries the superseded 25 with expired_at set
        assert len(view["superseded"]) == 1
        assert view["superseded"][0]["value"] == 25
        assert view["superseded"][0]["expired_at"] is not None
        assert len(data["receipts"]) == 1
        assert data["receipts"][0]["subject"] == "perception"

    def test_reset_purges_partition_and_turn_evidence(self, client, fake_backend):
        resp = client.post("/api/phase3/reset", params={"namespace": "ns-bench"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["nodes_deleted"] == 7
        assert data["turn_evidence_deleted"] == 3
        fake_backend.delete_namespace.assert_awaited_once_with("ns-bench")

    def test_reset_refuses_default_namespace(self, client, fake_backend):
        fake_backend.delete_namespace.side_effect = ValueError(
            "refusing to delete the default/shared namespace: 'default'"
        )
        resp = client.post("/api/phase3/reset", params={"namespace": "default"})
        assert resp.status_code == 400
