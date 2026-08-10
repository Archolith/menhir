"""Guards against oversized assembled Graphiti requests.

Embeddings are stripped before prompt serialization, the local estimate is deliberately
conservative for code-heavy JSON, and provider context-limit errors are normalized to the
same exception as local guard failures so callers can apply an adaptive fallback.
"""

from __future__ import annotations

import json

import pytest

from menhir.infrastructure import graphiti_llm_patches
from menhir.infrastructure.graphiti_llm_patches import (
    GraphitiRequestTooLargeError,
    _enforce_request_size,
    _is_context_length_error,
)
from menhir.infrastructure.graphiti_model_patches import (
    _is_embedding_value,
    _safe_to_prompt_json,
    _strip_embeddings,
)


@pytest.mark.unit
def test_strip_embeddings_drops_content_embedding_by_key_name() -> None:
    candidate = {"name": "menhir", "content_embedding": [0.1] * 1536, "summary": "a project"}

    result = _strip_embeddings(candidate)

    assert result == {"name": "menhir", "summary": "a project"}


@pytest.mark.unit
def test_strip_embeddings_drops_unknown_vector_by_shape() -> None:
    """The key-name rule alone would miss the next vector property someone adds."""
    candidate = {"name": "menhir", "some_future_vector": [0.1] * 512}

    assert _strip_embeddings(candidate) == {"name": "menhir"}


@pytest.mark.unit
def test_strip_embeddings_preserves_real_prompt_content() -> None:
    """Short numeric lists are legitimate content and must survive."""
    context = {
        "existing_nodes": [{"name": "a", "scores": [1, 2, 3]}],
        "episode_content": "ctharvey prefers conventional commits",
        "previous_episodes": [],
        "counts": [0] * 64,
    }

    assert _strip_embeddings(context) == context


@pytest.mark.unit
def test_strip_embeddings_recurses_into_nested_candidates() -> None:
    context = {"existing_nodes": [{"name": "a", "content_embedding": [0.0] * 1536}]}

    assert _strip_embeddings(context) == {"existing_nodes": [{"name": "a"}]}


@pytest.mark.unit
def test_is_embedding_value_rejects_bool_and_string_lists() -> None:
    assert not _is_embedding_value([True] * 128)
    assert not _is_embedding_value(["a"] * 128)
    assert _is_embedding_value([0.5] * 128)


@pytest.mark.unit
def test_prompt_json_omits_embeddings_and_stays_small() -> None:
    """End-to-end: the serializer every Graphiti prompt goes through."""
    candidates = [{"name": f"n{i}", "content_embedding": [0.123456] * 1536} for i in range(15)]

    serialized = _safe_to_prompt_json({"existing_nodes": candidates})

    assert "content_embedding" not in serialized
    assert json.loads(serialized) == {"existing_nodes": [{"name": f"n{i}"} for i in range(15)]}
    # Before the fix this same payload was ~470,000 chars.
    assert len(serialized) < 1000


@pytest.mark.unit
def test_oversized_assembled_request_is_rejected_before_send(monkeypatch) -> None:
    monkeypatch.setattr(graphiti_llm_patches, "_MAX_REQUEST_ESTIMATED_TOKENS", 1000)
    messages = [{"role": "user", "content": "x" * 40_000}]

    with pytest.raises(GraphitiRequestTooLargeError) as excinfo:
        _enforce_request_size(messages, "gpt-4.1-mini", "https://api.example")

    message = str(excinfo.value)
    assert "13,334" in message
    assert "1,000" in message
    assert "MEMORY_GRAPHITI_REQUEST_MAX_ESTIMATED_TOKENS" in message


@pytest.mark.unit
def test_request_within_ceiling_returns_estimate(monkeypatch) -> None:
    monkeypatch.setattr(graphiti_llm_patches, "_MAX_REQUEST_ESTIMATED_TOKENS", 1000)

    assert _enforce_request_size([{"role": "user", "content": "x" * 400}], "m", None) == 134


@pytest.mark.unit
def test_zero_ceiling_disables_the_check(monkeypatch) -> None:
    monkeypatch.setattr(graphiti_llm_patches, "_MAX_REQUEST_ESTIMATED_TOKENS", 0)

    assert _enforce_request_size([{"role": "user", "content": "x" * 40_000}], "m", None) == 13_334


@pytest.mark.unit
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("context_length_exceeded"),
        RuntimeError("This model's maximum context length is 128000 tokens"),
    ],
)
def test_context_length_error_is_classified_for_adaptive_fallback(error: Exception) -> None:
    assert _is_context_length_error(error)


@pytest.mark.unit
def test_unrelated_provider_error_is_not_classified_as_context_length() -> None:
    assert not _is_context_length_error(RuntimeError("invalid response schema"))
