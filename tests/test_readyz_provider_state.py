"""#631: /readyz tells an authenticated operator check about provider billing and auth refusals.

The provider block never changes readiness, the status code or `status`: every deploy consumer
requires status == "ready", and a restart does not fix an empty account.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from starlette.datastructures import Headers

from menhir.api import production_routes
from menhir.api.production_routes import readyz
from menhir.infrastructure.observability import (
    LLMCallHandle,
    clear_provider_auth_failure,
    clear_provider_quota_exhaustion,
    fail_llm_usage_call,
)

pytestmark = pytest.mark.unit

_REQ = httpx.Request("POST", "https://api.openai.com/v1/embeddings")


@pytest.fixture(autouse=True)
def _clean_flags():
    clear_provider_quota_exhaustion()
    clear_provider_auth_failure()
    yield
    clear_provider_quota_exhaustion()
    clear_provider_auth_failure()


def _handle(kind: str = "embedding") -> LLMCallHandle:
    return LLMCallHandle(
        call_id="c", kind=kind, model="text-embedding-3-small", endpoint="embeddings.create",
        operation=None, started_at=0.0, report_only=False,
    )


def _quota_429() -> openai.RateLimitError:
    return openai.RateLimitError(
        "insufficient_quota",
        response=httpx.Response(429, request=_REQ),
        body={"code": "insufficient_quota", "type": "insufficient_quota", "message": "x"},
    )


def _auth_401() -> openai.AuthenticationError:
    return openai.AuthenticationError(
        "invalid key", response=httpx.Response(401, request=_REQ), body=None
    )


def _breakers(embed: dict | None = None):
    snaps = {
        "llm": {"state": "closed", "failures": 0},
        "embed": embed or {"state": "closed", "failures": 0},
        "reranker": {"state": "closed", "failures": 0},
    }
    return SimpleNamespace(circuit_breaker_snapshots=lambda: snaps)


async def _readyz(monkeypatch, *, authenticated: bool, graphiti_client=None):
    async def gate(_request: object) -> bool:
        return authenticated

    async def digest(_instance: object) -> str:
        return "0" * 64

    monkeypatch.setattr(production_routes, "_caller_may_see_build", gate)
    monkeypatch.setattr(production_routes, "served_mcp_metadata_digest", digest)
    state = SimpleNamespace(
        settings=SimpleNamespace(runtime_mode="production", instance_id="x"),
        runtime_ctx=SimpleNamespace(
            capabilities=SimpleNamespace(neo4j_ready=True, enrichment_ready=True),
            scheduler=object(),
            built=SimpleNamespace(graphiti_client=graphiti_client or _breakers()),
        ),
        oauth_signing_key=object(),
    )
    response = await readyz(SimpleNamespace(headers=Headers(), app=SimpleNamespace(state=state)))
    return response.status_code, json.loads(response.body)


async def test_quota_exhaustion_is_reported_without_changing_readiness(monkeypatch) -> None:
    fail_llm_usage_call(_handle(), _quota_429())

    code, body = await _readyz(monkeypatch, authenticated=True)

    assert code == 200 and body["status"] == "ready" and body["failures"] == []
    provider = body["provider"]
    assert provider["degraded"] == ["provider_quota_exhausted"]
    assert "provider_quota_exhausted" in provider["provider_quota_exhausted"]
    assert provider["provider_auth_failure"] is None


async def test_a_billing_hard_open_breaker_alone_reports_quota(monkeypatch) -> None:
    embed = {"state": "open", "failures": 1, "cooldown_remaining_s": 200.0,
             "reason": "provider_quota_exhausted"}

    code, body = await _readyz(monkeypatch, authenticated=True, graphiti_client=_breakers(embed))

    assert code == 200 and body["status"] == "ready"
    assert body["provider"]["degraded"] == ["provider_quota_exhausted"]
    assert body["provider"]["breakers_hard_open"] == ["embed"]


async def test_auth_failure_is_reported(monkeypatch) -> None:
    fail_llm_usage_call(_handle("chat"), _auth_401())

    code, body = await _readyz(monkeypatch, authenticated=True)

    assert code == 200 and body["status"] == "ready"
    assert body["provider"]["degraded"] == ["provider_auth_failure"]
    assert body["provider"]["provider_auth_failure"]


async def test_healthy_provider_block_is_empty(monkeypatch) -> None:
    _code, body = await _readyz(monkeypatch, authenticated=True)
    assert body["provider"] == {
        "degraded": [],
        "provider_quota_exhausted": None,
        "provider_auth_failure": None,
        "breakers_hard_open": [],
    }


async def test_public_caller_sees_no_provider_block(monkeypatch) -> None:
    fail_llm_usage_call(_handle(), _quota_429())

    code, body = await _readyz(monkeypatch, authenticated=False)

    assert code == 200 and body["status"] == "ready"
    assert "provider" not in body
    assert "quota" not in json.dumps(body)


async def test_a_broken_breaker_read_does_not_break_readiness(monkeypatch) -> None:
    def boom():
        raise RuntimeError("x")

    code, body = await _readyz(
        monkeypatch, authenticated=True,
        graphiti_client=SimpleNamespace(circuit_breaker_snapshots=boom),
    )
    assert code == 200 and body["provider"]["degraded"] == []
