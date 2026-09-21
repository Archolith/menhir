"""Integration tests for BackendClient -> HTTP -> RuntimeProvider round-trip.

Verifies that BackendClient methods correctly proxy through the
/api/internal/backend/{operation} endpoint to a RuntimeProvider on the server
side, using FastAPI TestClient + httpx transport (no real network).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from menhir.api.auth import BearerAuthMiddleware
from menhir.api.routes import router
from menhir.config.settings import MemorySettings
from menhir.core.backend_impl import (
    BackendClient,
    _push_background_error,
    _push_client_warning,
    drain_client_warnings,
)
from menhir.domain.recall import InvalidQueryPresetError


def _build_fake_runtime_ctx(backend_overrides: dict | None = None):
    """Build a minimal fake RuntimeContext with a mock backend."""
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
    built = SimpleNamespace(
        ingest_service=SimpleNamespace(
            enrichment_enabled=lambda: True,
            get_queue_depth=lambda: 3,
            get_failed_enrichment_count=lambda: 1,
            get_max_enrichment_attempts=lambda: 5,
        ),
        graph_adapter=SimpleNamespace(
            flag_memory=MagicMock(return_value=True),
            unflag_memory=MagicMock(return_value=True),
            delete_memory=MagicMock(return_value=True),
            fetch_flagged_memories=MagicMock(return_value=[]),
            fetch_recent_memories=MagicMock(return_value=[]),
            fetch_memory_by_uuid=MagicMock(return_value=None),
            fetch_flagged_memory_bootstrap_version=MagicMock(return_value="abc123"),
            fetch_memory_overview=MagicMock(return_value={"nodes": 100, "edges": 200}),
            fetch_memories_by_scope=MagicMock(return_value=[]),
            fetch_memories_by_type=MagicMock(return_value=[]),
            fetch_episode_processing=MagicMock(return_value=None),
            list_episode_processing=MagicMock(return_value=[]),
            get_scan_fingerprint=MagicMock(return_value="fp-1234"),
            count_namespace=MagicMock(return_value=5),
        ),
        graphiti_client=SimpleNamespace(
            circuit_breaker_snapshots=MagicMock(return_value={
                "llm": {"state": "closed", "failures": 0},
                "embed": {"state": "closed", "failures": 0},
                "reranker": {"state": "closed", "failures": 0},
            }),
            embedding_cache_stats=MagicMock(return_value={"hits": 10, "misses": 5, "size": 15}),
        ),
        recall_service=SimpleNamespace(
            recall=AsyncMock(return_value={
                "query": "test",
                "preset": "knowledge",
                "results": [],
                "candidates_evaluated": 0,
            }),
        ),
        context_builder=SimpleNamespace(
            build_context=AsyncMock(return_value={
                "query": "test",
                "context": "some context",
                "token_estimate": 50,
                "memory_count": 1,
                "truncated": False,
                "preset": "knowledge",
            }),
        ),
        settings=SimpleNamespace(
            neo4j_uri="bolt://localhost:7687",
            neo4j_database="neo4j",
            chat_provider="local",
            graphiti_provider="local",
            graphiti_embed_provider="",
            graphiti_reranker_provider="",
            local_llm_base_url="http://localhost:1234/v1",
            local_llm_api_key="not-needed",
            local_llm_embed_base_url="",
            openai_chat_model="",
            local_llm_chat_model="test-model",
                local_llm_embed_model="test-embed",
            graphiti_embed_model="",
            backend_url="",
        ),
    )
    session = SimpleNamespace(session_id="test-session", user_id="test-user")
    return SimpleNamespace(built=built, session=session, capabilities=capabilities)


@pytest.fixture
def server_app():
    """FastAPI app with fake runtime context for integration testing."""
    ctx = _build_fake_runtime_ctx()
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = ctx
    return app, ctx


@pytest.fixture
def backend_client(server_app):
    """BackendClient wired to the test server via httpx transport."""
    app, ctx = server_app
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    return BackendClient("http://testserver", client=client), ctx


@pytest.fixture
def backend_client_like_uvicorn(server_app):
    """Same wiring, but an unhandled server exception becomes a 500 RESPONSE.

    ``ASGITransport`` re-raises app exceptions by default, which is exactly the in-process
    behaviour #132 hides behind: a backend ValueError would reach the client as a ValueError
    whether or not the dispatch mapped it. Real uvicorn answers 500 instead. Tests that
    assert on what crosses the HTTP boundary must use this fixture.
    """
    app, ctx = server_app
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    return BackendClient("http://testserver", client=client), ctx


class TestBackendRoundTrip:
    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_flag_memory(self, backend_client):
        bc, ctx = backend_client
        result = await bc.flag_memory("node-uuid-1")
        assert result is True
        ctx.built.graph_adapter.flag_memory.assert_called_once_with("node-uuid-1")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_unflag_memory(self, backend_client):
        bc, ctx = backend_client
        result = await bc.unflag_memory("node-uuid-1")
        assert result is True
        ctx.built.graph_adapter.unflag_memory.assert_called_once_with("node-uuid-1")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_unflag_memory_nonexistent(self, backend_client):
        bc, ctx = backend_client
        ctx.built.graph_adapter.unflag_memory = MagicMock(return_value=False)
        result = await bc.unflag_memory("no-such-uuid")
        assert result is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_delete_memory(self, backend_client):
        bc, ctx = backend_client
        result = await bc.delete_memory("node-uuid-2")
        assert result is True
        ctx.built.graph_adapter.delete_memory.assert_called_once_with("node-uuid-2")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_fetch_flagged_memories(self, backend_client):
        bc, ctx = backend_client
        result = await bc.fetch_flagged_memories(limit=10)
        assert result == []
        ctx.built.graph_adapter.fetch_flagged_memories.assert_called_once_with(limit=10)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_fetch_flagged_memory_bootstrap_version(self, backend_client):
        bc, ctx = backend_client
        result = await bc.fetch_flagged_memory_bootstrap_version()
        assert result == "abc123"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_fetch_memory_overview(self, backend_client):
        bc, ctx = backend_client
        result = await bc.fetch_memory_overview()
        assert result["nodes"] == 100
        assert result["edges"] == 200

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_get_queue_depth(self, backend_client):
        bc, ctx = backend_client
        result = await bc.get_queue_depth()
        assert result == 3

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_get_failed_enrichment_count(self, backend_client):
        bc, ctx = backend_client
        result = await bc.get_failed_enrichment_count()
        assert result == 1

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_get_max_enrichment_attempts(self, backend_client):
        bc, ctx = backend_client
        result = await bc.get_max_enrichment_attempts()
        assert result == 5

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_circuit_breaker_snapshots(self, backend_client):
        bc, ctx = backend_client
        result = await bc.circuit_breaker_snapshots()
        assert result["llm"]["state"] == "closed"
        assert result["embed"]["state"] == "closed"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_embedding_cache_stats(self, backend_client):
        bc, ctx = backend_client
        result = await bc.embedding_cache_stats()
        assert result["hits"] == 10
        assert result["size"] == 15

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_get_scan_fingerprint(self, backend_client):
        bc, ctx = backend_client
        result = await bc.get_scan_fingerprint("cth.mcp.memory")
        assert result == "fp-1234"
        ctx.built.graph_adapter.get_scan_fingerprint.assert_called_once_with("cth.mcp.memory")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_recall(self, backend_client):
        bc, ctx = backend_client
        result = await bc.recall("test query", preset="knowledge", limit=5)
        assert result["query"] == "test"
        assert result["results"] == []
        ctx.built.recall_service.recall.assert_awaited_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_recall_accepts_include_invalidated(self, backend_client):
        # SSOT-01 regression: BackendClient.recall previously had no
        # include_invalidated parameter, so any caller passing it (e.g.
        # RecallMemoriesTool, which always supplies it) raised TypeError
        # before making the HTTP request at all.
        bc, ctx = backend_client
        result = await bc.recall("test query", include_invalidated=True)
        assert result["query"] == "test"
        _, kwargs = ctx.built.recall_service.recall.call_args
        assert kwargs["include_invalidated"] is True

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_recall_include_invalidated_default_false(self, backend_client):
        bc, ctx = backend_client
        await bc.recall("test query", include_invalidated=False)
        _, kwargs = ctx.built.recall_service.recall.call_args
        assert kwargs["include_invalidated"] is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_recall_omits_none_event_authority_layer(self, backend_client):
        # Flag-off wire shape: when recall returns no event verdict, the structured
        # event_authority_layer must be omitted exactly like the scalar authority_layer.
        bc, ctx = backend_client
        ctx.built.recall_service.recall = AsyncMock(return_value={
            "query": "test", "preset": "knowledge", "results": [],
            "candidates_evaluated": 0,
            "authority_layer": None, "event_authority_layer": None,
        })
        result = await bc.recall("test query")
        assert "authority_layer" not in result
        assert "event_authority_layer" not in result

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_recall_retains_event_authority_layer_verdict(self, backend_client):
        # Flag-on wire shape: a real event verdict must be preserved through serialization.
        bc, ctx = backend_client
        verdict = {
            "predicate": "acquired", "object_key": "notebook-b",
            "valid_at": "2026-02-05T00:00:00Z", "status": "leads",
            "gate": "pass", "reason": "unique grounded lead",
            "subject_uuid": "self", "has_foundation": True, "kind": "latest",
        }
        ctx.built.recall_service.recall = AsyncMock(return_value={
            "query": "test", "preset": "knowledge", "results": [],
            "candidates_evaluated": 0,
            "event_authority_layer": (verdict,),
        })
        result = await bc.recall("test query")
        assert result["event_authority_layer"][0]["status"] == "leads"
        assert result["event_authority_layer"][0]["object_key"] == "notebook-b"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_build_context(self, backend_client):
        bc, ctx = backend_client
        result = await bc.build_context("test query", max_tokens=2000)
        assert result["context"] == "some context"
        assert result["token_estimate"] == 50
        ctx.built.context_builder.build_context.assert_awaited_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_build_context_invalid_preset_raises_value_error(self, backend_client):
        bc, ctx = backend_client

        with pytest.raises(
            InvalidQueryPresetError,
            match="Invalid preset 'brief'. Use: recent, knowledge, emotional, connected, conflict.",
        ):
            await bc.build_context("test query", max_tokens=2000, preset="brief")

        ctx.built.context_builder.build_context.assert_not_awaited()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_get_provider_config(self, backend_client):
        bc, ctx = backend_client
        result = await bc.get_provider_config()
        assert result["neo4j_uri"] == "bolt://localhost:7687"
        assert result["chat_model"] == "test-model"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_unknown_operation_returns_error(self, backend_client):
        bc, ctx = backend_client
        with pytest.raises(Exception):
            await bc._request("not_a_real_method", {})

    # #132. The three tests below run against the REAL /api/internal/backend dispatch over
    # ASGI. The in-process fake backend is why 299 unit tests never saw the refusal turn
    # into a 500: the tools' `except ValueError` only ever ran in-process. Verified to
    # fail on the pre-fix code with the uvicorn-like transport.

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_delete_namespace_cap_refusal_is_a_value_error_over_http(self, backend_client_like_uvicorn):
        bc, ctx = backend_client_like_uvicorn
        ctx.built.graph_adapter.count_namespace.return_value = 5

        with pytest.raises(ValueError, match=r"exceeding the safety limit of 1.*force=true"):
            await bc.delete_namespace("scratch", max_nodes=1, force=False)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_delete_namespace_pin_refusal_is_a_permission_error_over_http(self, backend_client_like_uvicorn):
        bc, ctx = backend_client_like_uvicorn
        with patch(
            "menhir.core.backend_runtime_data_ops.pinned_namespace", return_value="mine"
        ):
            with pytest.raises(PermissionError, match=r"pinned to namespace 'mine'"):
                await bc.delete_namespace("theirs", max_nodes=1, force=False)

        ctx.built.graph_adapter.count_namespace.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_delete_namespace_tool_returns_documented_json_error_over_http(self, backend_client_like_uvicorn):
        """The MCP tool, not just the client: the refusal must come back as the tool's
        documented `{"error": ..., "namespace": ...}` payload with the force/dry_run guidance."""
        import json

        from menhir.mcp.tools.ops.delete_namespace import DeleteNamespaceTool

        bc, ctx = backend_client_like_uvicorn
        ctx.built.graph_adapter.count_namespace.return_value = 5
        tool = DeleteNamespaceTool()

        with patch.object(DeleteNamespaceTool, "get_backend", return_value=bc):
            rendered = await tool.endpoint("scratch", max_nodes=1, force=False, dry_run=False)

        payload = json.loads(rendered)
        assert payload["namespace"] == "scratch"
        assert "force=true" in payload["error"] and "dry_run=true" in payload["error"]
        assert "500" not in rendered and "HTTPStatusError" not in rendered

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_flag_memory_tool_reports_structural_refusal_over_http(self, backend_client_like_uvicorn):
        """flag_memory shares the #132 pattern: memory_queries.flag_memory raises ValueError
        for a structural node, and the tool's `except ValueError` renders "Cannot flag: ...".
        Over HTTP that used to be a 500."""
        from menhir.mcp.tools.ingest.flag_memory import FlagMemoryTool

        bc, ctx = backend_client_like_uvicorn
        ctx.built.graph_adapter.flag_memory.side_effect = ValueError(
            "Cannot flag structural graph node (role=file). Flag semantic memories only."
        )

        with patch.object(FlagMemoryTool, "get_backend", return_value=bc):
            rendered = await FlagMemoryTool().endpoint("node-uuid-1")

        assert rendered.startswith("Cannot flag: Cannot flag structural graph node (role=file)")
        assert "HTTPStatusError" not in rendered

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_unflag_memory_ownership_refusal_is_a_permission_error_over_http(self, backend_client_like_uvicorn):
        """unflag_memory has no ValueError on its backend path (its `except ValueError` is
        inert in both modes); what it CAN raise is the ownership PermissionError, which must
        cross the boundary as a PermissionError rather than a 500."""
        bc, ctx = backend_client_like_uvicorn

        async def refuse(**kwargs):
            raise PermissionError("Refused: memory node-uuid-1 belongs to another namespace.")

        with patch("menhir.core.backend_runtime_data_ops.require_own_object", side_effect=refuse):
            with pytest.raises(PermissionError, match="belongs to another namespace"):
                await bc.unflag_memory("node-uuid-1")

        ctx.built.graph_adapter.unflag_memory.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_fetch_memory_by_uuid_returns_none(self, backend_client_like_uvicorn):
        bc, ctx = backend_client_like_uvicorn
        result = await bc.fetch_memory_by_uuid("nonexistent")
        assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_fetch_episode_processing_returns_none(self, backend_client_like_uvicorn):
        bc, ctx = backend_client_like_uvicorn
        result = await bc.fetch_episode_processing("ep-000")
        assert result is None

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_list_episode_processing_empty(self, backend_client_like_uvicorn):
        bc, ctx = backend_client_like_uvicorn
        result = await bc.list_episode_processing(states=["PENDING"], limit=10)
        assert result == []

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_backend_client_reuses_owned_async_client_across_requests(self):
        created_clients: list[object] = []

        class _FakeResponse:
            status_code = 200
            headers = {}
            content = b'{"ok": true}'

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, bool]:
                return {"ok": True}

        class _FakeAsyncClient:
            def __init__(self, *, base_url: str, timeout: float) -> None:
                self.base_url = base_url
                self.timeout = timeout
                self.post_calls: list[tuple[str, dict[str, object], dict[str, str] | None]] = []
                self.closed = False
                created_clients.append(self)

            async def post(
                self,
                path: str,
                json: dict[str, object],
                headers: dict[str, str] | None = None,
            ) -> _FakeResponse:
                self.post_calls.append((path, json, headers))
                return _FakeResponse()

            async def aclose(self) -> None:
                self.closed = True

        with patch("menhir.core.backend_client.httpx.AsyncClient", _FakeAsyncClient):
            client = BackendClient(
                "http://testserver",
                settings=MemorySettings(
                    api_key="",
                    mcp_client_user_id="",
                    mcp_client_id="",
                    mcp_client_name="",
                ),
            )
            first = await client._request("op-one", {"a": 1})
            second = await client._request("op-two", {"b": 2})

            assert first == {"ok": True}
            assert second == {"ok": True}
            assert len(created_clients) == 1
            # Paths and payloads are what this test is about, alongside the single reused client.
            assert [(path, body) for path, body, _headers in created_clients[0].post_calls] == [
                ("/api/internal/backend/op-one", {"a": 1}),
                ("/api/internal/backend/op-two", {"b": 2}),
            ]
            # Headers used to be asserted as exactly empty for a client with nothing configured.
            # A named User-Agent is now sent unconditionally -- deliberately, because a request
            # with no agent is refused at a Cloudflare edge with BIC on before the origin sees
            # it. Pin that it is present on EVERY request rather than dropping the header
            # assertion, so a regression to "no agent" still fails here.
            for _path, _body, headers in created_clients[0].post_calls:
                assert headers is not None
                assert headers["User-Agent"].startswith("menhir/")
                assert set(headers) == {"User-Agent"}

            await client.aclose()
            assert created_clients[0].closed is True

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_backend_client_sends_bearer_auth_and_client_headers(self):
        ctx = _build_fake_runtime_ctx()
        app = FastAPI()
        app.include_router(router)
        app.state.runtime_ctx = ctx
        protected_app = BearerAuthMiddleware(app, api_key="test-api-key")

        transport = httpx.ASGITransport(app=protected_app)
        http_client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
        settings = MemorySettings(
            api_key="test-api-key",
            backend_url="http://testserver",
            mcp_client_user_id="codex",
            mcp_client_id="codex-client",
            mcp_client_name="codex",
        )
        client = BackendClient("http://testserver", client=http_client, settings=settings)

        result = await client.get_queue_depth()

        assert result == 3

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_backend_invoke_drains_background_warnings_for_current_session_only(self, server_app):
        app, ctx = server_app
        _push_background_error("session-a", "warning-a")
        _push_background_error("session-b", "warning-b")

        headers = {"x-menhir-session-id": "session-a", "x-menhir-user-id": "user-a"}
        with TestClient(app) as client:
            resp = client.post("/api/internal/backend/get_queue_depth", json={}, headers=headers)

        assert resp.status_code == 200
        assert resp.headers["x-menhir-bg-warnings"] == '["warning-a"]'
        assert "x-yawn-bg-warnings" not in resp.headers

        headers_b = {"x-menhir-session-id": "session-b", "x-menhir-user-id": "user-b"}
        with TestClient(app) as client:
            resp_b = client.post("/api/internal/backend/get_queue_depth", json={}, headers=headers_b)

        assert resp_b.status_code == 200
        assert resp_b.headers["x-menhir-bg-warnings"] == '["warning-b"]'
        assert "x-yawn-bg-warnings" not in resp_b.headers

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_client_warning_drain_is_context_scoped(self):
        _push_client_warning("warning-one")
        assert drain_client_warnings() == ["warning-one"]
        assert drain_client_warnings() == []
