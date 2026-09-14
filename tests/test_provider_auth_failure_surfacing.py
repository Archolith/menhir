"""A rejected LLM credential must reach the user, not only get_enrichment_status.

Found by a fresh-install walkthrough: with a bogus OPENAI_API_KEY the tier report said
"ok", /api/ready said "ready", add_memory said "Queued", and the episode then FAILED with a
401 that nothing surfaced.
"""

from __future__ import annotations

import pytest

from menhir.cli.up import tier_report, render_report
from menhir.config import MemorySettings
from menhir.core.runtime_preflight import RuntimeCapabilities
from menhir.infrastructure import observability as obs

pytestmark = [pytest.mark.unit]


class _Status401(Exception):
    status_code = 401


@pytest.fixture(autouse=True)
def _clean_marker():
    obs.clear_provider_auth_failure()
    yield
    obs.clear_provider_auth_failure()


def _handle() -> obs.LLMCallHandle:
    return obs.start_llm_usage_call(kind="chat", model="gpt-4.1-nano", endpoint="chat.completions.create", operation="x")


def test_auth_error_classifier_matches_status_and_message_not_transport() -> None:
    assert obs.is_provider_auth_error(_Status401("nope"))
    assert obs.is_provider_auth_error(RuntimeError("Error code: 401 - {'error': {'code': 'invalid_api_key'}}"))
    assert obs.is_provider_auth_error(RuntimeError("Incorrect API key provided: sk-***"))
    assert not obs.is_provider_auth_error(RuntimeError("connection reset by peer"))
    assert not obs.is_provider_auth_error(TimeoutError("timed out"))
    assert not obs.is_provider_auth_error(RuntimeError("Error code: 429 - rate limited"))


def test_failed_call_records_marker_and_success_clears_it() -> None:
    assert obs.last_provider_auth_failure() is None

    obs.fail_llm_usage_call(_handle(), RuntimeError("Error code: 401 - Incorrect API key provided: sk-abc"))
    failure = obs.last_provider_auth_failure()
    assert failure is not None
    assert failure.kind == "chat" and failure.model == "gpt-4.1-nano"
    assert "Incorrect API key" in failure.detail
    assert "rejected our credentials" in failure.summary()

    obs.fail_llm_usage_call(_handle(), RuntimeError("connection reset"))  # transport: does not overwrite
    assert obs.last_provider_auth_failure() is failure

    obs.complete_llm_usage_call(_handle(), result=None)
    assert obs.last_provider_auth_failure() is None


@pytest.mark.asyncio
async def test_add_memory_queue_summary_names_the_credential_problem() -> None:
    from menhir.mcp.formatters import _queue_summary

    obs.fail_llm_usage_call(_handle(), RuntimeError("Error code: 401 - Incorrect API key provided"))

    class _Backend:
        async def get_queue_depth(self) -> int:
            return 0

        async def list_episode_processing(self, **kwargs) -> list:
            return []

        async def scheduler_status_snapshot(self) -> dict:
            return {"running": True}

        async def fetch_memory_overview(self) -> dict:
            return {"failed_count": 0}

    text = await _queue_summary(_Backend())
    assert "WARNING" in text
    assert "rejected our credentials" in text
    assert "OPENAI_API_KEY" in text


def test_health_and_ready_expose_the_rejection() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace

    from menhir.api import routes as api_routes
    from menhir.api.routes import router

    caps = RuntimeCapabilities(
        venv_ready=True, graphiti_dependency_ready=True, neo4j_ready=True,
        graphiti_llm_ready=True, embedder_ready=True, reranker_ready=True, failures=(),
    )
    ctx = SimpleNamespace(
        capabilities=caps,
        built=SimpleNamespace(ingest_service=SimpleNamespace(enrichment_enabled=lambda: True)),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.runtime_ctx = ctx
    client = TestClient(app)

    def _ctx(_request):
        return ctx

    import unittest.mock as um

    with um.patch.object(api_routes, "_get_runtime_context", _ctx):
        before = client.get("/api/ready").json()
        assert before["status"] == "ready" and before["provider_auth_failure"] is None

        obs.fail_llm_usage_call(_handle(), RuntimeError("Error code: 401 - Incorrect API key provided"))

        health = client.get("/api/health").json()
        ready = client.get("/api/ready").json()

    assert health["services"]["llm_auth"] == "rejected"
    assert "rejected our credentials" in health["provider_auth_failure"]
    assert ready["status"] == "degraded"
    assert ready["capabilities"]["enrichment_ready"] is True  # the config is fine; the key is not
    assert "Incorrect API key" in ready["provider_auth_failure"]


def test_tier_report_never_shows_a_bare_ok_for_a_cloud_provider() -> None:
    """Cloud lines always say how the key was judged: verified, rejected, or unverifiable."""
    for credential in ("verified", "rejected", "unverified"):
        caps = RuntimeCapabilities(
            venv_ready=True, graphiti_dependency_ready=True, neo4j_ready=True,
            graphiti_llm_ready=credential != "rejected", embedder_ready=credential != "rejected",
            reranker_ready=True, failures=(), cloud_credential=credential,
        )
        text = render_report(tier_report(caps, MemorySettings(graphiti_provider="openai")), "full")
        llm_line = next(l for l in text.splitlines() if "llm: graphiti extraction" in l)
        assert "(openai:" in llm_line, llm_line
