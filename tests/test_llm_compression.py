"""Tests for LLM-based memory compression.

Unit tests mock the OpenAI client. Integration tests hit llama.cpp.
"""

from __future__ import annotations

import asyncio

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from menhir.config import MemorySettings
from menhir.infrastructure.llm import (
    LLMAdapter,
    _COMPRESS_MAX_RETRIES,
    _COMPRESS_SYSTEM_PROMPT,
    _strip_thinking_tags,
)
from menhir.infrastructure.providers import (
    OpenAIStyleChatBackend,
    ProviderConfig,
    ProviderKind,
    ProviderRuntimeDependencies,
)
from menhir.infrastructure.pending_actions import PendingActionStore
from menhir.services.lifecycle_service import DecayResult, LifecycleService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_chat_response(content: str) -> SimpleNamespace:
    """Build a minimal OpenAI-shaped chat completion response."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
    )
def _client_with_create(mock_create: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=mock_create)))


def _build_llm_adapter(
    *,
    dependencies: ProviderRuntimeDependencies | None = None,
) -> LLMAdapter:
    dependencies = dependencies or _fake_dependencies()
    return LLMAdapter(
        base_url="http://127.0.0.1:8081/v1",
        api_key="local",
        chat_model="test-model",
        embed_model="test-embed",
        backend=OpenAIStyleChatBackend(
            provider=ProviderConfig(
                kind=ProviderKind.LOCAL,
                base_url="http://127.0.0.1:8081/v1",
                api_key="local",
                chat_model="test-model",
                embed_model="test-embed",
            ),
            settings=MemorySettings(),
            dependencies=dependencies,
        ),
        dependencies=dependencies,
    )


def _fake_dependencies(
    *,
    client: object | None = None,
    acquired_url: str | None = None,
    acquire_error: Exception | None = None,
    timeout_s: float = 1.0,
    sleep_delays: list[float] | None = None,
    acquire_calls: list[dict[str, object]] | None = None,
    client_calls: list[dict[str, object]] | None = None,
) -> ProviderRuntimeDependencies:
    async def acquire(*, fallback: str, task: str | None = None, timeout_s: float = 30.0) -> str:
        if acquire_calls is not None:
            acquire_calls.append({"fallback": fallback, "task": task, "timeout_s": timeout_s})
        if acquire_error is not None:
            raise acquire_error
        return acquired_url or fallback

    def build_client(
        *,
        base_url: str,
        api_key: str,
        settings: MemorySettings,
        request_timeout_s: float | None = None,
    ):
        if client_calls is not None:
            client_calls.append({"base_url": base_url, "api_key": api_key, "timeout_s": request_timeout_s})
        if client is None:
            raise AssertionError("unit test attempted to build a real OpenAI client")
        return client

    async def sleep(delay: float) -> None:
        if sleep_delays is not None:
            sleep_delays.append(delay)

    return ProviderRuntimeDependencies(
        scheduler_url_acquire=acquire,
        openai_client_factory=build_client,
        request_timeout_s=timeout_s,
        retry_sleep=sleep,
    )


# ===========================================================================
# Unit tests â€” LLMAdapter.compress_content
# ===========================================================================

pytestmark = pytest.mark.unit


class TestCompressContentUnit:
    """Mock-based tests for LLMAdapter.compress_content."""

    def _adapter_for_create(
        self,
        mock_create: AsyncMock,
        **dependency_kwargs,
    ) -> LLMAdapter:
        client = _client_with_create(mock_create)
        return _build_llm_adapter(
            dependencies=_fake_dependencies(client=client, **dependency_kwargs),
        )

    @pytest.mark.asyncio
    async def test_returns_llm_summary(self):
        mock_create = AsyncMock(return_value=_mock_chat_response("Alice prefers tea."))
        adapter = self._adapter_for_create(mock_create)

        result = await adapter.compress_content("Alice said she really likes tea over coffee.")

        assert result == "Alice prefers tea."
        mock_create.assert_awaited_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["model"] == "test-model"
        assert call_kwargs["messages"][0]["content"] == _COMPRESS_SYSTEM_PROMPT
        assert "Alice" in call_kwargs["messages"][1]["content"]

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_input(self):
        adapter = _build_llm_adapter()
        result = await adapter.compress_content("   ")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_response(self):
        mock_create = AsyncMock(return_value=_mock_chat_response(""))
        adapter = self._adapter_for_create(mock_create)

        result = await adapter.compress_content("some content")

        assert result is None

    @pytest.mark.asyncio
    async def test_strips_leaked_thinking_tags(self):
        mock_create = AsyncMock(
            return_value=_mock_chat_response(
                "<think>Need to reason privately.</think>Alice prefers tea."
            )
        )
        adapter = self._adapter_for_create(mock_create)

        result = await adapter.compress_content("some content")

        assert result == "Alice prefers tea."

    @pytest.mark.asyncio
    async def test_returns_none_after_all_retries_exhausted(self):
        mock_create = AsyncMock(side_effect=RuntimeError("connection refused"))
        delays: list[float] = []
        adapter = self._adapter_for_create(mock_create, sleep_delays=delays)

        result = await adapter.compress_content("some content")

        assert result is None
        assert mock_create.await_count == _COMPRESS_MAX_RETRIES + 1
        assert delays == [2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]

    @pytest.mark.asyncio
    async def test_retry_sleep_is_injectable(self):
        mock_create = AsyncMock(side_effect=RuntimeError("temporary failure"))
        adapter = self._adapter_for_create(mock_create)
        delays: list[float] = []

        async def record_sleep(delay: float) -> None:
            delays.append(delay)

        result = await adapter._chat_text(
            system_prompt="system",
            user_prompt="content",
            operation="test",
            max_retries=2,
            retry_base_delay_s=0.0,
            retry_sleep=record_sleep,
        )

        assert result is None
        assert mock_create.await_count == 3
        assert delays == [0.0, 0.0]

    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self):
        mock_create = AsyncMock(
            side_effect=[
                RuntimeError("crash"),
                _mock_chat_response("recovered summary"),
            ]
        )
        delays: list[float] = []
        adapter = self._adapter_for_create(mock_create, sleep_delays=delays)

        result = await adapter.compress_content("some content")

        assert result == "recovered summary"
        assert mock_create.await_count == 2
        assert delays == [2.0]

    @pytest.mark.asyncio
    async def test_timeout_stops_retrying(self):
        mock_create = AsyncMock(side_effect=TimeoutError("request timed out"))
        delays: list[float] = []
        adapter = self._adapter_for_create(mock_create, sleep_delays=delays)

        result = await adapter.compress_content("some content")

        assert result is None
        assert mock_create.await_count == 1
        assert delays == []

    @pytest.mark.asyncio
    async def test_cancellation_stops_retrying(self):
        mock_create = AsyncMock(side_effect=asyncio.CancelledError())
        delays: list[float] = []
        adapter = self._adapter_for_create(mock_create, sleep_delays=delays)

        with pytest.raises(asyncio.CancelledError):
            await adapter.compress_content("some content")

        assert mock_create.await_count == 1
        assert delays == []

    @pytest.mark.asyncio
    async def test_uses_low_temperature(self):
        mock_create = AsyncMock(return_value=_mock_chat_response("summary"))
        adapter = self._adapter_for_create(mock_create)

        await adapter.compress_content("content")

        assert mock_create.call_args.kwargs["temperature"] == 0.3

    @pytest.mark.asyncio
    async def test_caps_max_tokens(self):
        mock_create = AsyncMock(return_value=_mock_chat_response("summary"))
        adapter = self._adapter_for_create(mock_create)

        await adapter.compress_content("content")

        assert mock_create.call_args.kwargs["max_tokens"] == 256

    @pytest.mark.asyncio
    async def test_merge_content_returns_updated_summary(self):
        mock_create = AsyncMock(return_value=_mock_chat_response("Updated memory"))
        adapter = self._adapter_for_create(mock_create)

        result = await adapter.merge_content("Old summary", "New context")

        assert result == "Updated memory"
        call_kwargs = mock_create.call_args.kwargs
        assert "Existing memory: Old summary" in call_kwargs["messages"][1]["content"]
        assert "New context: New context" in call_kwargs["messages"][1]["content"]

    @pytest.mark.asyncio
    async def test_merge_content_strips_thinking_tags(self):
        mock_create = AsyncMock(
            return_value=_mock_chat_response("<think>hidden</think>Updated memory")
        )
        adapter = self._adapter_for_create(mock_create)

        result = await adapter.merge_content("Old summary", "New context")

        assert result == "Updated memory"

    @pytest.mark.asyncio
    async def test_scheduler_acquisition_succeeds(self):
        mock_create = AsyncMock(return_value=_mock_chat_response("summary"))
        acquire_calls: list[dict[str, object]] = []
        client_calls: list[dict[str, object]] = []
        adapter = self._adapter_for_create(
            mock_create,
            acquired_url="http://127.0.0.1:8089/v1",
            timeout_s=0.25,
            acquire_calls=acquire_calls,
            client_calls=client_calls,
        )

        result = await adapter.compress_content("some content")

        assert result == "summary"
        assert acquire_calls == [
            {
                "fallback": "http://127.0.0.1:8081/v1",
                "task": "memory: llm compression",
                "timeout_s": 0.25,
            }
        ]
        assert client_calls[0] == {
            "base_url": "http://127.0.0.1:8089/v1",
            "api_key": "local",
            "timeout_s": 0.25,
        }

    @pytest.mark.asyncio
    async def test_scheduler_acquisition_timeout_falls_back(self):
        mock_create = AsyncMock(return_value=_mock_chat_response("summary"))
        client_calls: list[dict[str, object]] = []
        adapter = self._adapter_for_create(
            mock_create,
            acquire_error=TimeoutError("scheduler timed out"),
            client_calls=client_calls,
        )

        result = await adapter.compress_content("some content")

        assert result == "summary"
        assert client_calls[0]["base_url"] == "http://127.0.0.1:8081/v1"

    @pytest.mark.asyncio
    async def test_scheduler_acquisition_failure_falls_back(self):
        mock_create = AsyncMock(return_value=_mock_chat_response("summary"))
        client_calls: list[dict[str, object]] = []
        adapter = self._adapter_for_create(
            mock_create,
            acquire_error=RuntimeError("scheduler unavailable"),
            client_calls=client_calls,
        )

        result = await adapter.compress_content("some content")

        assert result == "summary"
        assert client_calls[0]["base_url"] == "http://127.0.0.1:8081/v1"

def test_strip_thinking_tags_collapses_whitespace():
    assert _strip_thinking_tags("A <think>hidden</think>  summary\nhere") == "A summary here"


# ===========================================================================
# Unit tests â€” Decay pipeline wiring (LLM vs fallback)
# ===========================================================================

@dataclass
class _FakeLLM:
    """Controllable fake for testing LLM wiring in decay."""
    compress_calls: list[str] = field(default_factory=list)
    compress_return: str | None = "LLM summary"

    async def compress_content(self, content: str) -> str | None:
        self.compress_calls.append(content)
        return self.compress_return


class TestDecayLLMWiring:
    """Verify _run_decay uses LLM when available and falls back otherwise."""

    def _make_decay_service(
        self, stub_adapter, stub_graphiti, llm=None, *, tmp_path=None
    ) -> LifecycleService:
        store = PendingActionStore(db_path=tmp_path / "test.db") if tmp_path else PendingActionStore()
        return LifecycleService(
            graph_adapter=stub_adapter,
            graphiti_client=stub_graphiti,
            llm=llm,
            pending_actions=store,
        )

    def _setup_compressible_candidate(self, stub_adapter, stub_graphiti):
        old = datetime.now(timezone.utc) - timedelta(days=45)
        stub_adapter.edge_counts_synced = 1
        stub_adapter.decay_candidates_by_freshness = {
            "ACTIVE": [
                {
                    "uuid": "node-1",
                    "name": "Test Node",
                    "content": "A" * 300,
                    "summary": None,
                    "scope": "PERSISTENT",
                    "sharpness": 0.0,
                    "edge_count": 2,
                    "freshness": "ACTIVE",
                    "user_flagged": False,
                    "last_accessed": old,
                    "created_at": old,
                },
            ],
        }
        # 4 similar results -> sharpness = 1/(1+4) = 0.2 < 0.3
        #
        # Regression note: this used to set `search_scored_results`, which was
        # the similarity-count source before the F2 "lawful sharpness" rewrite
        # switched _count_similar_nodes to call count_similar_by_cosine
        # instead. That left this fixture silently stale: count_similar_by_cosine
        # defaults to 0 when count_similar_by_cosine_results is unset, so
        # similar_count was always 0 -> sharpness 1.0 -> should_compress()
        # always False -> the LLM was never even called. Set the field the
        # current production code actually reads.
        stub_graphiti.search_scored_results = [
            (f"other-{i}", f"Other {i}", 0.9) for i in range(4)
        ]
        stub_graphiti.count_similar_by_cosine_results = 4

    @pytest.mark.asyncio
    async def test_decay_uses_llm_when_available(
        self, stub_memory_graph_adapter, stub_graphiti_client, tmp_path
    ):
        self._setup_compressible_candidate(stub_memory_graph_adapter, stub_graphiti_client)
        fake_llm = _FakeLLM(compress_return="LLM compressed this")

        svc = self._make_decay_service(
            stub_memory_graph_adapter, stub_graphiti_client, llm=fake_llm, tmp_path=tmp_path
        )
        result = await svc.apply_decay()

        assert result.compressed == 1
        assert len(fake_llm.compress_calls) == 1
        assert fake_llm.compress_calls[0] == "A" * 300
        assert stub_memory_graph_adapter.compressed_nodes[0]["summary"] == "LLM compressed this"
        # Completed action should be removed from pending
        assert svc.pending_actions.count() == 0

    @pytest.mark.asyncio
    async def test_decay_queues_pending_when_llm_fails(
        self, stub_memory_graph_adapter, stub_graphiti_client, tmp_path
    ):
        self._setup_compressible_candidate(stub_memory_graph_adapter, stub_graphiti_client)
        fake_llm = _FakeLLM(compress_return=None)

        svc = self._make_decay_service(
            stub_memory_graph_adapter, stub_graphiti_client, llm=fake_llm, tmp_path=tmp_path
        )
        result = await svc.apply_decay()

        assert result.compressed == 0
        assert len(fake_llm.compress_calls) == 1
        assert stub_memory_graph_adapter.compressed_nodes == []
        # Should be queued in pending_actions
        pending = svc.pending_actions.fetch_pending("compress")
        assert len(pending) == 1
        assert pending[0]["node_uuid"] == "node-1"
        assert pending[0]["action"] == "compress"
        assert pending[0]["failure_reason"] == "llm_failed"
        assert pending[0]["attempts"] == 1

    @pytest.mark.asyncio
    async def test_decay_queues_pending_when_no_llm(
        self, stub_memory_graph_adapter, stub_graphiti_client, tmp_path
    ):
        self._setup_compressible_candidate(stub_memory_graph_adapter, stub_graphiti_client)

        svc = self._make_decay_service(
            stub_memory_graph_adapter, stub_graphiti_client, llm=None, tmp_path=tmp_path
        )
        result = await svc.apply_decay()

        assert result.compressed == 0
        assert stub_memory_graph_adapter.compressed_nodes == []
        pending = svc.pending_actions.fetch_pending("compress")
        assert len(pending) == 1
        assert pending[0]["node_uuid"] == "node-1"
        assert pending[0]["failure_reason"] == "no_llm"

    @pytest.mark.asyncio
    async def test_pending_action_increments_attempts_on_repeated_failure(
        self, stub_memory_graph_adapter, stub_graphiti_client, tmp_path
    ):
        self._setup_compressible_candidate(stub_memory_graph_adapter, stub_graphiti_client)
        fake_llm = _FakeLLM(compress_return=None)

        svc = self._make_decay_service(
            stub_memory_graph_adapter, stub_graphiti_client, llm=fake_llm, tmp_path=tmp_path
        )
        await svc.apply_decay()
        # Simulate second decay cycle hitting the same node
        self._setup_compressible_candidate(stub_memory_graph_adapter, stub_graphiti_client)
        await svc.apply_decay()

        pending = svc.pending_actions.fetch_pending("compress")
        assert len(pending) == 1
        assert pending[0]["attempts"] == 2


# ===========================================================================
# Integration tests â€” requires llama.cpp running on localhost:1234
# ===========================================================================

# needs_llm: this class has no skip-guard, so with no LLM reachable it does not fail -- it HANGS
# in the chat-completion retry/backoff path until pytest-timeout kills the whole run. CI supplies
# Neo4j but no LLM, so it must be deselected there rather than merely expected to fail.
@pytest.mark.online
@pytest.mark.needs_llm
class TestCompressContentLive:
    """Live tests against llama.cpp. Run with: pytest -m online"""

    @pytest.mark.asyncio
    async def test_llm_produces_shorter_summary(self):
        adapter = LLMAdapter(
            base_url="http://localhost:1234/v1",
            api_key="local",
            chat_model="qwen3-8b",
            embed_model="nomic-embed-text-v1.5",
        )
        original = (
            "Yesterday I had a long conversation with my friend Alice about her "
            "new job at the robotics lab. She mentioned that she started three weeks "
            "ago and is working on autonomous navigation systems for warehouse robots. "
            "She seemed really excited about the team culture and said the pay was "
            "significantly better than her previous position at the university. "
            "We also talked about getting dinner next Friday at the Italian place "
            "near the park."
        )

        result = await adapter.compress_content(original)

        assert result is not None, "LLM returned no summary"
        assert len(result) < len(original), (
            f"Summary ({len(result)} chars) should be shorter than original ({len(original)} chars)"
        )
        # Key facts should survive compression
        result_lower = result.lower()
        assert "alice" in result_lower, "Summary should mention Alice"

    @pytest.mark.asyncio
    async def test_llm_handles_short_content_gracefully(self):
        adapter = LLMAdapter(
            base_url="http://localhost:1234/v1",
            api_key="local",
            chat_model="qwen3-8b",
            embed_model="nomic-embed-text-v1.5",
        )

        result = await adapter.compress_content("Alice likes tea.")

        assert result is not None, "LLM returned no summary for short content"
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_llm_preserves_entities_in_complex_content(self):
        adapter = LLMAdapter(
            base_url="http://localhost:1234/v1",
            api_key="local",
            chat_model="qwen3-8b",
            embed_model="nomic-embed-text-v1.5",
        )
        original = (
            "The user prefers Python 3.12 with type hints for all new projects. "
            "They use Neo4j as their graph database, hosted in Docker on port 7687. "
            "The embedding model is nomic-embed-text running in llama.cpp on port 1234. "
            "Their IDE is IntelliJ IDEA and they follow conventional commits with "
            "explicit file staging (never git add -A)."
        )

        result = await adapter.compress_content(original)

        assert result is not None
        result_lower = result.lower()
        # Core technical entities should survive
        assert "python" in result_lower or "py" in result_lower, "Should mention Python"
        assert "neo4j" in result_lower, "Should mention Neo4j"

