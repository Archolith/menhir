"""Tests for truncation-aware retry escalation in the Graphiti LLM patch."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from menhir.infrastructure.graphiti_llm_patches import GraphitiRequestTooLargeError
from menhir.infrastructure.graphiti_patches import (
    _looks_like_truncation,
    _truncation_offset,
)


# ---------------------------------------------------------------------------
# _truncation_offset
# ---------------------------------------------------------------------------


class TestTruncationOffset:
    def test_extracts_char_offset_from_wrapped_message(self) -> None:
        exc = ValueError(
            "Your response was not valid JSON (Expecting ',' delimiter: "
            "line 1 column 3110 (char 3109)). You MUST respond with ONLY "
            "a valid JSON object."
        )
        assert _truncation_offset(exc) == 3109

    def test_extracts_from_raw_json_decode_error(self) -> None:
        try:
            json.loads('{"a": 1, "b": 2, "c": ')
        except json.JSONDecodeError as exc:
            assert _truncation_offset(exc) is not None
            assert _truncation_offset(exc) == exc.pos

    def test_extracts_from_chained_cause(self) -> None:
        try:
            json.loads('{"truncated": "at char 100')
        except json.JSONDecodeError as inner:
            outer = ValueError("parse failed")
            outer.__cause__ = inner
            assert _truncation_offset(outer) == inner.pos

    def test_returns_none_for_unrelated_error(self) -> None:
        assert _truncation_offset(ValueError("something else")) is None

    def test_returns_none_for_key_error(self) -> None:
        assert _truncation_offset(KeyError("missing_field")) is None


# ---------------------------------------------------------------------------
# _looks_like_truncation
# ---------------------------------------------------------------------------


class TestLooksLikeTruncation:
    def test_high_offset_is_truncation(self) -> None:
        exc = ValueError(
            "Your response was not valid JSON (Expecting ',' delimiter: "
            "line 1 column 3110 (char 3109))."
        )
        assert _looks_like_truncation(exc) is True

    def test_low_offset_is_not_truncation(self) -> None:
        exc = ValueError(
            "Your response was not valid JSON (Expecting value: "
            "line 1 column 1 (char 0))."
        )
        assert _looks_like_truncation(exc) is False

    def test_offset_at_threshold_is_truncation(self) -> None:
        exc = ValueError(
            "Your response was not valid JSON (Expecting ',' delimiter: "
            "line 1 column 501 (char 500))."
        )
        assert _looks_like_truncation(exc) is True

    def test_offset_below_threshold_is_not_truncation(self) -> None:
        exc = ValueError(
            "Your response was not valid JSON (Expecting ',' delimiter: "
            "line 1 column 100 (char 99))."
        )
        assert _looks_like_truncation(exc) is False

    def test_non_json_error_is_not_truncation(self) -> None:
        assert _looks_like_truncation(RuntimeError("timeout")) is False


# ---------------------------------------------------------------------------
# Retry escalation integration
# ---------------------------------------------------------------------------

pytest.importorskip("graphiti_core")


class TestRetryEscalation:
    """Verify that generate_response escalates max_tokens on truncation."""

    @pytest.fixture
    def patched_client(self, monkeypatch: pytest.MonkeyPatch):
        """Build a minimal OpenAIGenericClient with patched _generate_response."""
        import menhir.infrastructure.graphiti_patches as patches
        from graphiti_core.llm_client.openai_generic_client import (
            OpenAIGenericClient,
        )

        # Ensure patch is applied (idempotent)
        patches._patch_graphiti_openai_generic_client(OpenAIGenericClient)

        client = MagicMock(spec=OpenAIGenericClient)
        client.max_tokens = 4096
        client.model = "gpt-4o-mini"
        client._clean_input = lambda self_or_text, text=None: text if text is not None else self_or_text
        # Bind the patched methods
        client.generate_response = OpenAIGenericClient.generate_response.__get__(client)
        client._generate_response = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_truncation_doubles_max_tokens_on_retry(
        self, patched_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the first call truncates, retry 1 should double max_tokens."""
        from graphiti_core.prompts.models import Message

        truncation_error = ValueError(
            "Your response was not valid JSON (Expecting ',' delimiter: "
            "line 1 column 3110 (char 3109))."
        )
        # First call truncates, second succeeds
        patched_client._generate_response.side_effect = [
            truncation_error,
            {"extracted_entities": [], "edges": []},
        ]

        messages = [Message(role="system", content="test"), Message(role="user", content="test")]
        result = await patched_client.generate_response(messages)

        assert result == {"extracted_entities": [], "edges": []}
        # Check the second call used doubled max_tokens
        calls = patched_client._generate_response.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["max_tokens"] == 4096
        assert calls[1].kwargs["max_tokens"] == 8192

    @pytest.mark.asyncio
    async def test_non_truncation_error_keeps_same_max_tokens(
        self, patched_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-truncation errors should not escalate max_tokens."""
        from graphiti_core.prompts.models import Message

        parse_error = ValueError(
            "Your response was not valid JSON (Expecting value: "
            "line 1 column 1 (char 0))."
        )
        patched_client._generate_response.side_effect = [
            parse_error,
            {"extracted_entities": [], "edges": []},
        ]

        messages = [Message(role="system", content="test"), Message(role="user", content="test")]
        result = await patched_client.generate_response(messages)

        calls = patched_client._generate_response.call_args_list
        assert len(calls) == 2
        # max_tokens should stay at the original value
        assert calls[0].kwargs["max_tokens"] == 4096
        assert calls[1].kwargs["max_tokens"] == 4096

    @pytest.mark.asyncio
    async def test_context_length_error_is_not_retried(self, patched_client) -> None:
        """An unchanged oversized payload cannot succeed on a later attempt."""
        from graphiti_core.prompts.models import Message

        patched_client._generate_response.side_effect = RuntimeError(
            "context_length_exceeded: maximum context length is 128000 tokens"
        )
        messages = [Message(role="system", content="test"), Message(role="user", content="test")]

        with pytest.raises(GraphitiRequestTooLargeError):
            await patched_client.generate_response(messages)

        assert patched_client._generate_response.await_count == 1

    @pytest.mark.asyncio
    async def test_persistent_truncation_requests_reduced_extraction(
        self, patched_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two consecutive truncations should ask for reduced extraction on retry 2."""
        from graphiti_core.prompts.models import Message

        truncation_error = ValueError(
            "Your response was not valid JSON (Expecting ',' delimiter: "
            "line 1 column 3110 (char 3109))."
        )
        patched_client._generate_response.side_effect = [
            truncation_error,
            truncation_error,
            {"extracted_entities": [], "edges": []},
        ]

        messages = [Message(role="system", content="test"), Message(role="user", content="test")]
        result = await patched_client.generate_response(messages)

        assert result == {"extracted_entities": [], "edges": []}
        calls = patched_client._generate_response.call_args_list
        assert len(calls) == 3
        assert calls[0].kwargs["max_tokens"] == 4096
        assert calls[1].kwargs["max_tokens"] == 8192
        assert calls[2].kwargs["max_tokens"] == 16384
        # The third retry prompt should mention reducing extraction
        retry_msg = messages[-1].content
        assert "Reduce" in retry_msg or "reduce" in retry_msg.lower()
