"""Unit tests for runtime preflight checks and the thin main runner."""

from __future__ import annotations

from typing import Any
from types import SimpleNamespace

import json
from pathlib import Path

import pytest

from menhir.config import MemorySettings
from menhir.core import runtime_preflight
from menhir import main


class _FakeNeo4jSession:
    def __init__(self, value: Any):
        self.value = value

    def __enter__(self) -> "_FakeNeo4jSession":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def run(self, _query: str) -> "_FakeNeo4jResult":
        return _FakeNeo4jResult(self.value)


class _FakeNeo4jResult:
    def __init__(self, value: Any):
        self.value = value

    def single(self) -> "_FakeNeo4jRecord":
        return _FakeNeo4jRecord(self.value)


class _FakeNeo4jRecord:
    def __init__(self, value: Any):
        self.value = value

    def get(self, _key: str) -> Any:
        return self.value


class _FakeNeo4jDriver:
    def __init__(self, ok_value: Any = 1):
        self.ok_value = ok_value

    def session(self) -> _FakeNeo4jSession:
        return _FakeNeo4jSession(self.ok_value)

    def close(self) -> None:
        return None


class _FakeHTTPResponse:
    def __init__(self, payload: str):
        self._payload = payload

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def read(self) -> bytes:
        return self._payload.encode("utf-8")


def _patch_graph_database(
    monkeypatch: pytest.MonkeyPatch,
    driver: object,
) -> None:
    monkeypatch.setattr(runtime_preflight, "_NEO4J_IMPORT_ERROR", None)
    monkeypatch.setattr(runtime_preflight, "GraphDatabase", SimpleNamespace(driver=driver))


@pytest.mark.unit
def test_check_neo4j_connectivity_returns_true_for_ok_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_graph_database(monkeypatch, lambda *args, **kwargs: _FakeNeo4jDriver())
    assert runtime_preflight.check_neo4j_connectivity("bolt://localhost:7687", "neo4j", "secret") is True


@pytest.mark.unit
def test_check_neo4j_connectivity_returns_false_for_bad_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_graph_database(monkeypatch, lambda *args, **kwargs: _FakeNeo4jDriver(ok_value=0))
    assert runtime_preflight.check_neo4j_connectivity("bolt://localhost:7687", "neo4j", "secret") is False


@pytest.mark.unit
def test_check_neo4j_connectivity_returns_false_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("neo4j down")

    _patch_graph_database(monkeypatch, _boom)
    assert runtime_preflight.check_neo4j_connectivity("bolt://localhost:7687", "neo4j", "secret") is False


@pytest.mark.unit
def test_check_llama_connectivity_returns_true_when_models_are_present(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        {"data": [{"id": "chat"}, {"id": "embed"}]},
    )
    monkeypatch.setattr(runtime_preflight, "urlopen", lambda *args, **kwargs: _FakeHTTPResponse(payload))
    assert runtime_preflight.check_llama_connectivity(
        base_url="http://localhost:1234/v1",
        api_key="key",
        chat_model="chat",
        embed_model="embed",
    )


@pytest.mark.unit
def test_check_llama_connectivity_omits_auth_for_local_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"data": [{"id": "chat"}]})
    captured_headers: dict[str, str] = {}

    class _FakeRequest:
        def __init__(self, url: str, headers: dict[str, str], method: str) -> None:
            captured_headers.update(headers)

    monkeypatch.setattr(runtime_preflight, "Request", _FakeRequest)
    monkeypatch.setattr(runtime_preflight, "urlopen", lambda *args, **kwargs: _FakeHTTPResponse(payload))

    assert runtime_preflight.check_llama_connectivity(
        base_url="http://localhost:1234/v1",
        api_key="should-not-send",
        chat_model="chat",
        embed_model="",
    )
    assert "Authorization" not in captured_headers


@pytest.mark.unit
def test_check_llama_connectivity_returns_false_when_required_model_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps({"data": [{"id": "other"}]})
    monkeypatch.setattr(runtime_preflight, "urlopen", lambda *args, **kwargs: _FakeHTTPResponse(payload))
    assert (
        runtime_preflight.check_llama_connectivity(
            base_url="http://localhost:1234/v1",
            api_key="key",
            chat_model="chat",
            embed_model="embed",
        )
        is False
    )


@pytest.mark.unit
def test_check_llama_connectivity_returns_false_for_invalid_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_preflight, "urlopen", lambda *args, **kwargs: _FakeHTTPResponse("not-json"))
    assert (
        runtime_preflight.check_llama_connectivity(
            base_url="http://localhost:1234/v1",
            api_key="key",
            chat_model="chat",
            embed_model="embed",
        )
        is False
    )


@pytest.mark.unit
def test_check_expected_python_runtime_returns_true_for_project_venv(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = runtime_preflight.expected_venv_python()
    assert runtime_preflight.check_expected_python_runtime(str(expected)) is True


@pytest.mark.unit
def test_expected_venv_python_resolves_project_root_not_src() -> None:
    expected = runtime_preflight.expected_venv_python()

    assert expected.parts[-3:] == (".venv", "Scripts", "python.exe") if expected.name == "python.exe" else (
        ".venv",
        "bin",
        "python",
    )
    assert expected.parent.parent.name == ".venv"
    assert expected.parent.parent.parent == Path(runtime_preflight.__file__).resolve().parents[3]


@pytest.mark.unit
def test_check_expected_python_runtime_returns_false_for_other_interpreter() -> None:
    assert runtime_preflight.check_expected_python_runtime(str(Path("C:/Python312/python.exe"))) is False


@pytest.mark.unit
def test_check_graphiti_dependency_returns_false_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_preflight.importlib.util, "find_spec", lambda name: None)
    assert runtime_preflight.check_graphiti_dependency() is False


@pytest.mark.unit
def test_collect_runtime_failures_accumulates_preflight_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(
        neo4j_uri="bolt://localhost:7687",
        neo4j_database="neo4j",
        neo4j_user="neo4j",
        neo4j_password="secret",
        local_llm_base_url="http://localhost:1234/v1",
        local_llm_api_key="llama",
        local_llm_chat_model="chat-model",
        local_llm_embed_model="embed-model",
        local_llm_embed_base_url="",
        openai_api_key="openai-key",
        openai_chat_model="gpt-5-mini",
        openai_embed_model="text-embedding-3-small",
        chat_provider="local",
        graphiti_provider="local",
        graphiti_embed_provider="",
        graphiti_embed_model="",
        graphiti_reranker_provider="",
        graphiti_reranker_model="",
    )
    monkeypatch.setattr(runtime_preflight, "check_expected_python_runtime", lambda executable=None: False)
    monkeypatch.setattr(runtime_preflight, "check_graphiti_dependency", lambda: False)
    monkeypatch.setattr(runtime_preflight, "check_neo4j_connectivity", lambda *args, **kwargs: False)
    monkeypatch.setattr(runtime_preflight, "check_llama_connectivity", lambda *args, **kwargs: False)
    monkeypatch.setattr(runtime_preflight, "expected_graphiti_embedding_dimension", lambda settings: None)

    failures = runtime_preflight.collect_runtime_failures(settings, require_venv=True)

    assert "menhir must run from the project .venv interpreter." in failures
    assert "graphiti_core is not installed in the active interpreter." in failures
    assert "Neo4j connectivity check failed." in failures
    assert any(
        item.startswith("Graphiti extraction connectivity/model check failed")
        for item in failures
    )
    assert any(
        item.startswith("Graphiti embed connectivity/model check failed")
        for item in failures
    )


@pytest.mark.unit
def test_collect_runtime_failures_rejects_non_openai_graphiti_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = MemorySettings(
        graphiti_provider="gemini",
        graphiti_embed_provider="openai_compat",
    )
    monkeypatch.setattr(runtime_preflight, "check_expected_python_runtime", lambda executable=None: True)
    monkeypatch.setattr(runtime_preflight, "check_graphiti_dependency", lambda: True)
    monkeypatch.setattr(runtime_preflight, "check_neo4j_connectivity", lambda *args, **kwargs: True)
    monkeypatch.setattr(runtime_preflight, "check_llama_connectivity", lambda *args, **kwargs: True)
    monkeypatch.setattr(runtime_preflight, "expected_graphiti_embedding_dimension", lambda settings: None)

    failures = runtime_preflight.collect_runtime_failures(settings, require_venv=False)

    assert "Graphiti extraction provider must be openai or openai_compat." in failures


@pytest.mark.unit
def test_collect_runtime_failures_rejects_non_openai_graphiti_reranker(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = MemorySettings(
        graphiti_provider="openai_compat",
        graphiti_reranker_provider="gemini",
    )
    monkeypatch.setattr(runtime_preflight, "check_expected_python_runtime", lambda executable=None: True)
    monkeypatch.setattr(runtime_preflight, "check_graphiti_dependency", lambda: True)
    monkeypatch.setattr(runtime_preflight, "check_neo4j_connectivity", lambda *args, **kwargs: True)
    monkeypatch.setattr(runtime_preflight, "check_llama_connectivity", lambda *args, **kwargs: True)
    monkeypatch.setattr(runtime_preflight, "expected_graphiti_embedding_dimension", lambda settings: None)

    failures = runtime_preflight.collect_runtime_failures(settings, require_venv=False)

    assert "Graphiti reranker provider must be openai or openai_compat." in failures


@pytest.mark.unit
def test_collect_runtime_failures_does_not_acquire_scheduler_endpoint_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = MemorySettings(
        graphiti_provider="local",
        local_llm_base_url="http://127.0.0.1:8081/v1",
        local_llm_chat_model="chat-model",
    )
    acquired = {"count": 0}
    seen_base_urls: list[str] = []

    monkeypatch.setattr(runtime_preflight, "check_expected_python_runtime", lambda executable=None: True)
    monkeypatch.setattr(runtime_preflight, "check_graphiti_dependency", lambda: True)
    monkeypatch.setattr(runtime_preflight, "check_neo4j_connectivity", lambda *args, **kwargs: True)
    monkeypatch.setattr(runtime_preflight, "expected_graphiti_embedding_dimension", lambda settings: None)
    monkeypatch.setattr(
        runtime_preflight,
        "acquire_llama_url_sync",
        lambda *args, **kwargs: acquired.__setitem__("count", acquired["count"] + 1) or "http://127.0.0.1:9999/v1",
    )

    def _fake_check_llama_connectivity(*, base_url: str, api_key: str, chat_model: str, embed_model: str) -> bool:
        seen_base_urls.append(base_url)
        return True

    monkeypatch.setattr(runtime_preflight, "check_llama_connectivity", _fake_check_llama_connectivity)

    failures = runtime_preflight.collect_runtime_failures(settings, require_venv=False)

    assert failures == []
    assert acquired["count"] == 0
    assert seen_base_urls == ["http://127.0.0.1:8081/v1"]


@pytest.mark.unit
def test_collect_runtime_failures_acquires_scheduler_endpoint_when_explicitly_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = MemorySettings(
        graphiti_provider="local",
        local_llm_base_url="http://127.0.0.1:8081/v1",
        local_llm_chat_model="chat-model",
    )
    acquire_calls: list[dict[str, object]] = []
    seen_base_urls: list[str] = []

    monkeypatch.setattr(runtime_preflight, "check_expected_python_runtime", lambda executable=None: True)
    monkeypatch.setattr(runtime_preflight, "check_graphiti_dependency", lambda: True)
    monkeypatch.setattr(runtime_preflight, "check_neo4j_connectivity", lambda *args, **kwargs: True)
    monkeypatch.setattr(runtime_preflight, "expected_graphiti_embedding_dimension", lambda settings: None)

    def _fake_acquire_llama_url_sync(*, fallback: str, task: str | None = None, timeout_s: float = 0) -> str:
        acquire_calls.append({"fallback": fallback, "task": task, "timeout_s": timeout_s})
        return "http://127.0.0.1:9091/v1"

    def _fake_check_llama_connectivity(*, base_url: str, api_key: str, chat_model: str, embed_model: str) -> bool:
        seen_base_urls.append(base_url)
        return True

    monkeypatch.setattr(runtime_preflight, "acquire_llama_url_sync", _fake_acquire_llama_url_sync)
    monkeypatch.setattr(runtime_preflight, "check_llama_connectivity", _fake_check_llama_connectivity)

    failures = runtime_preflight.collect_runtime_failures(
        settings,
        require_venv=False,
        acquire_scheduler_endpoints=True,
    )

    assert failures == []
    assert acquire_calls == [
        {
            "fallback": "http://127.0.0.1:8081/v1",
            "task": None,
            "timeout_s": 120.0,
        },
        {
            "fallback": "http://127.0.0.1:8081/v1",
            "task": "memory: graphiti embed bootstrap",
            "timeout_s": 120.0,
        },
        {
            "fallback": "http://127.0.0.1:8081/v1",
            "task": "memory: graphiti reranker bootstrap",
            "timeout_s": 120.0,
        },
    ]
    assert seen_base_urls == ["http://127.0.0.1:9091/v1"]


@pytest.mark.unit
def test_collect_runtime_failures_reports_embedding_dimension_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = MemorySettings(
        graphiti_provider="openai",
        graphiti_embed_provider="openai",
        openai_api_key="openai-key",
        openai_embed_model="text-embedding-3-small",
    )
    monkeypatch.setattr(runtime_preflight, "check_expected_python_runtime", lambda executable=None: True)
    monkeypatch.setattr(runtime_preflight, "check_graphiti_dependency", lambda: True)
    monkeypatch.setattr(runtime_preflight, "check_neo4j_connectivity", lambda *args, **kwargs: True)
    monkeypatch.setattr(runtime_preflight, "check_openai_provider_configuration", lambda **kwargs: True)
    monkeypatch.setattr(
        runtime_preflight,
        "embedding_dimension_health",
        lambda **kwargs: {
            "ok": False,
            "wrong_entity_count": 10,
            "wrong_community_count": 0,
            "wrong_edge_count": 8,
        },
    )

    failures = runtime_preflight.collect_runtime_failures(settings, require_venv=False)

    assert any("Stored graph embeddings do not match the current embedder" in item for item in failures)


@pytest.mark.unit
def test_main_launches_typer_app() -> None:
    """Verify the main() entry point imports and calls the Typer app."""
    assert callable(main.main)
