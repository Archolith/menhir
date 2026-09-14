"""The free GET /v1/models call tells us whether a cloud key is accepted -- without billing.

Three outcomes by design: only a definite 401/403 fails preflight; an unreachable provider
leaves startup as permissive as before.
"""

from __future__ import annotations

import io
from urllib.error import HTTPError, URLError

import pytest

from menhir.cli.up import render_report, tier_report
from menhir.config import MemorySettings
from menhir.core import runtime_preflight as rp

pytestmark = [pytest.mark.unit]


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"data": []}'


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://api.openai.com/v1/models", code, "x", {}, io.BytesIO(b""))


def test_probe_verified_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def _urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr(rp, "urlopen", _urlopen)
    assert rp.probe_openai_credential(api_key="sk-good") == "verified"
    assert seen["url"] == "https://api.openai.com/v1/models"
    assert seen["auth"] == "Bearer sk-good"


@pytest.mark.parametrize("code", [401, 403])
def test_probe_rejected_on_auth_status(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    monkeypatch.setattr(rp, "urlopen", lambda *a, **k: (_ for _ in ()).throw(_http_error(code)))
    assert rp.probe_openai_credential(api_key="sk-bad") == "rejected"


@pytest.mark.parametrize(
    "raiser",
    [
        lambda *a, **k: (_ for _ in ()).throw(URLError("name resolution failed")),
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")),
        lambda *a, **k: (_ for _ in ()).throw(_http_error(503)),
        lambda *a, **k: (_ for _ in ()).throw(_http_error(429)),
    ],
    ids=["dns", "timeout", "503", "429"],
)
def test_probe_unverified_when_provider_unreachable_or_erroring(monkeypatch: pytest.MonkeyPatch, raiser) -> None:
    monkeypatch.setattr(rp, "urlopen", raiser)
    assert rp.probe_openai_credential(api_key="sk-whatever") == "unverified"


def test_empty_key_is_rejected_without_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rp, "urlopen", lambda *a, **k: pytest.fail("must not call the network"))
    assert rp.probe_openai_credential(api_key="   ") == "rejected"


def _openai_settings() -> MemorySettings:
    return MemorySettings(
        graphiti_provider="openai", chat_provider="openai", openai_api_key="sk-x",
        openai_chat_model="gpt-4.1-nano", openai_embed_model="text-embedding-3-small",
    )


def _stub_everything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rp, "check_graphiti_dependency", lambda: True)
    monkeypatch.setattr(rp, "check_neo4j_connectivity", lambda *a, **k: True)
    monkeypatch.setattr(rp, "expected_graphiti_embedding_dimension", lambda settings: None)


def test_collect_probes_once_and_a_rejection_names_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_everything_else(monkeypatch)
    calls: list[str] = []

    def _probe(*, api_key, base_url, timeout_s=None):
        calls.append(api_key)
        return "rejected"

    monkeypatch.setattr(rp, "probe_openai_credential", _probe)

    caps = rp.collect_runtime_capabilities(_openai_settings(), require_venv=False)

    assert calls == ["sk-x"], "one probe per preflight, shared by llm/embed/reranker"
    assert caps.cloud_credential == "rejected"
    assert caps.graphiti_llm_ready is False and caps.embedder_ready is False
    assert any("OpenAI rejected OPENAI_API_KEY" in f for f in caps.failures)
    assert caps.startup_mode == "degraded_queue_only"


def test_collect_treats_unverified_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_everything_else(monkeypatch)
    monkeypatch.setattr(rp, "probe_openai_credential", lambda **k: "unverified")

    caps = rp.collect_runtime_capabilities(_openai_settings(), require_venv=False)

    assert caps.cloud_credential == "unverified"
    assert caps.graphiti_llm_ready is True and caps.embedder_ready is True
    assert caps.failures == ()
    assert caps.startup_mode == "full"


def test_collect_skips_the_probe_for_local_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_everything_else(monkeypatch)
    monkeypatch.setattr(rp, "check_llama_connectivity", lambda **k: True)
    monkeypatch.setattr(rp, "probe_openai_credential", lambda **k: pytest.fail("no cloud probe for local"))

    caps = rp.collect_runtime_capabilities(
        MemorySettings(graphiti_provider="local", local_llm_chat_model="m", local_llm_embed_model="e"),
        require_venv=False,
    )
    assert caps.cloud_credential == "n/a"


def _caps(credential: str, **overrides) -> rp.RuntimeCapabilities:
    base = dict(
        venv_ready=True, graphiti_dependency_ready=True, neo4j_ready=True,
        graphiti_llm_ready=True, embedder_ready=True, reranker_ready=True, failures=(),
        cloud_credential=credential,
    )
    base.update(overrides)
    return rp.RuntimeCapabilities(**base)


def test_report_says_verified_rejected_or_unverifiable() -> None:
    settings = _openai_settings()

    verified = render_report(tier_report(_caps("verified"), settings), "full")
    assert "[ok  ] llm: graphiti extraction (openai: key verified via GET /v1/models)" in verified

    rejected = render_report(
        tier_report(_caps("rejected", graphiti_llm_ready=False, embedder_ready=False), settings),
        "degraded_queue_only",
    )
    assert "[MISS] llm: graphiti extraction (openai: key REJECTED)  <- OPENAI_API_KEY was rejected" in rejected

    unverified = render_report(tier_report(_caps("unverified"), settings), "full")
    assert "could not reach api.openai.com to verify" in unverified

    local = render_report(tier_report(_caps("n/a"), MemorySettings(graphiti_provider="local")), "full")
    assert "openai" not in local
