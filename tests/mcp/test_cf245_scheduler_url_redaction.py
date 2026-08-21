"""CF-245: `scheduler_url` must flow through the `get_provider_config` redaction choke point.

The resource emitted `scheduler_url` from the process environment at the emission site, which
is exactly the shape CF-35's choke point exists to prevent -- every sibling URL arrives already
redacted from the provider-config dict. The fix routes it through that dict via the canonical
resolver (`scheduler_url_from_env`).

Note on the default value: the canonical resolver rewrites a `localhost` host to `127.0.0.1`, so
the emitted default is `http://127.0.0.1:8082`, not the raw `SCHEDULER_URL_DEFAULT` literal.
The key is still always present and equal to the canonical default when `SCHEDULER_URL` is unset.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from menhir.core.backend_runtime_admin_ops import RuntimeProviderAdminOpsMixin
from menhir.infrastructure.llama_endpoint import scheduler_url_from_env
from menhir.mcp import resources as resources_module
from menhir.mcp.resources import SystemMetadataResource


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        neo4j_uri="bolt://localhost:7687",
        neo4j_database="neo4j",
        local_llm_base_url="http://localhost:8081/v1",
        local_llm_embed_base_url="",
        local_llm_api_key="",
        local_llm_chat_model="qwen",
        local_llm_embed_model="",
        backend_url="http://localhost:8000",
        gemini_chat_model="",
        chat_provider="local",
        graphiti_provider="local",
        graphiti_embed_provider=None,
        graphiti_reranker_provider=None,
    )


class _Runtime(RuntimeProviderAdminOpsMixin):
    def __init__(self, built: object) -> None:
        self.built = built

    async def _off_loop(self, fn, *args: object, **kwargs: object) -> object:
        return fn(*args, **kwargs)


def _runtime() -> _Runtime:
    built = SimpleNamespace(graph_adapter=object(), settings=_settings())
    return _Runtime(built)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_url_credentials_are_redacted(monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_URL", "http://user:SECRET@sched.example:8082/x")

    config = await _runtime().get_provider_config()

    assert "SECRET" not in repr(config)
    url = config["scheduler_url"]
    assert url == "http://sched.example:8082/x"
    assert "sched.example" in url
    assert ":8082" in url


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduler_url_uses_canonical_default_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("SCHEDULER_URL", raising=False)

    config = await _runtime().get_provider_config()

    # Positive control: the key is always present and equals the canonical resolver's default;
    # the fix must not change the default behaviour. (The resolver rewrites localhost to
    # 127.0.0.1, so the emitted value is the normalized form of SCHEDULER_URL_DEFAULT.)
    assert config["scheduler_url"] == scheduler_url_from_env()
    assert config["scheduler_url"] == "http://127.0.0.1:8082"


class _FakeBackend:
    def __init__(self, scheduler_url: str) -> None:
        self.scheduler_url = scheduler_url

    async def fetch_memory_overview(self, namespace=None) -> dict[str, object]:
        return {}

    async def get_provider_config(self) -> dict[str, object]:
        return {"scheduler_url": self.scheduler_url}

    async def get_queue_depth(self) -> int:
        return 0

    async def get_failed_enrichment_count(self) -> int:
        return 0

    async def scheduler_status_snapshot(self) -> dict[str, object]:
        return {}


class _Resource(SystemMetadataResource):
    def __init__(self, backend: _FakeBackend) -> None:
        self._backend = backend

    def get_backend(self) -> object:
        return self._backend


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resource_payload_uses_backend_value_not_environment(monkeypatch) -> None:
    monkeypatch.setenv("SCHEDULER_URL", "http://env-SENTINEL.example:1111")
    backend = _FakeBackend(scheduler_url="http://backend.example:9999")
    resource = _Resource(backend)

    monkeypatch.setattr(
        resources_module,
        "get_mcp_session",
        lambda: SimpleNamespace(session_id="sess", user_id="u"),
    )
    monkeypatch.setattr(resources_module, "_resolve_build_id", lambda: "test-build")

    payload = await resource.build_payload()

    # The resource must show the backend's value, not the environment's sentinel -- proving the
    # resource no longer reads SCHEDULER_URL directly and the choke point is in the path.
    assert payload["runtime"]["scheduler_url"] == "http://backend.example:9999"
    assert payload["runtime"]["scheduler_url"] != "http://env-SENTINEL.example:1111"


@pytest.mark.unit
def test_resources_no_longer_reference_scheduler_url_env() -> None:
    source = resources_module.__file__
    with open(source, "r", encoding="utf-8") as handle:
        text = handle.read()
    assert "SCHEDULER_URL" not in text
