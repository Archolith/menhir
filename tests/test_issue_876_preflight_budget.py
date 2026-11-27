"""#876: intake-accepted episodes must not be parked by preflight; blank failure text retries."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from menhir.domain.session import new_session
from menhir.services.ingest_service import IngestService
from menhir.config.settings_model import MemorySettings
from menhir.services.enrichment_failures import classify_enrichment_failure
from menhir.services.enrichment_steps import (
    build_episode_preflight_rejection,
    compose_episode_body,
)
from menhir.services.ingest_limits import (
    CHARS_PER_ESTIMATED_TOKEN,
    MAX_DIFF_CHARS,
    MAX_EPISODE_CHARS,
    validate_memory_payload,
)

LIMIT = MemorySettings.graphiti_episode_max_estimated_tokens


def test_intake_content_limit_is_the_default_preflight_budget():
    assert MAX_EPISODE_CHARS == CHARS_PER_ESTIMATED_TOKEN * LIMIT


@pytest.mark.parametrize("content_len", [MAX_EPISODE_CHARS, MAX_EPISODE_CHARS - 10, 40_000, 10, 0])
@pytest.mark.parametrize("diff_len", [1, 500, 20_000, MAX_DIFF_CHARS])
def test_intake_accepted_payload_passes_preflight(content_len, diff_len):
    content, diff = "c" * content_len, "d" * diff_len
    validate_memory_payload(content, diff)  # accepted at intake

    body = compose_episode_body({"content": content, "diff": diff}, LIMIT)

    assert build_episode_preflight_rejection(body, LIMIT) is None


def test_diff_truncated_when_content_leaves_little_room():
    body = compose_episode_body({"content": "c" * 47_000, "diff": "d" * 30_000}, LIMIT)
    assert "... [diff truncated]" in body
    assert body.startswith("c" * 47_000)


def test_diff_dropped_when_content_fills_budget():
    body = compose_episode_body({"content": "c" * MAX_EPISODE_CHARS, "diff": "d" * 100}, LIMIT)
    assert body == "c" * MAX_EPISODE_CHARS


def test_small_diff_kept_intact_under_budget():
    body = compose_episode_body({"content": "note", "diff": "+x"}, LIMIT)
    assert body.endswith("--- git diff ---\n+x")


@pytest.mark.parametrize("message", ["", "   ", "\n\t", None])
def test_blank_failure_message_is_retryable(message):
    assert classify_enrichment_failure(message) == "retryable"


# --- review follow-ups: redaction growth, configured budget, blank typed errors -------------

SECRET_LINE = 'password = "abcd1234"'  # 21 chars; redacts to 38


def _intake(stub_memory_graph_adapter, stub_graphiti_client, stub_llm_adapter, monkeypatch):
    svc = IngestService(
        graph_adapter=stub_memory_graph_adapter,
        graphiti_client=stub_graphiti_client,
        llm=stub_llm_adapter,
    )
    monkeypatch.setattr(svc, "_ensure_enrichment_worker", AsyncMock())
    monkeypatch.setattr(svc, "_enqueue_pending_episode", AsyncMock())
    return svc


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redaction_lengthened_content_is_rejected_at_intake_not_parked(
    stub_memory_graph_adapter, stub_graphiti_client, stub_llm_adapter, monkeypatch
):
    svc = _intake(stub_memory_graph_adapter, stub_graphiti_client, stub_llm_adapter, monkeypatch)
    content = "x" * (MAX_EPISODE_CHARS - 10 - len(SECRET_LINE)) + "\n" + SECRET_LINE
    validate_memory_payload(content)  # passes the raw check

    with pytest.raises(ValueError, match="exceeds"):
        await svc.queue_episode_for_enrichment(content, new_session("u876"), "unit-test")
    assert not stub_memory_graph_adapter.pending_episode_rows


@pytest.mark.unit
@pytest.mark.asyncio
async def test_low_configured_budget_rejects_content_over_four_times_budget(
    stub_memory_graph_adapter, stub_graphiti_client, stub_llm_adapter, monkeypatch
):
    svc = _intake(stub_memory_graph_adapter, stub_graphiti_client, stub_llm_adapter, monkeypatch)
    svc._graphiti_episode_max_estimated_tokens = 1000

    with pytest.raises(ValueError, match="enrichment budget"):
        await svc.queue_episode_for_enrichment("x" * 4001, new_session("u876"), "unit-test")
    assert not stub_memory_graph_adapter.pending_episode_rows

    result = await svc.queue_episode_for_enrichment(
        "x" * 4000, new_session("u876"), "unit-test", diff="d" * 5000
    )
    assert result.episode_id in stub_memory_graph_adapter.pending_episode_rows


def _blank_exc(name):
    return type(name, (Exception,), {"__str__": lambda self: ""})()


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("EnrichmentDeadlineExceeded", "manual_review"),
        ("LlmBudgetExceeded", "manual_review"),
        ("JSONDecodeError", "manual_review"),
        ("ValidationError", "manual_review"),
        ("RateLimitError", "retryable"),
    ],
)
def test_typed_exception_with_empty_message_keeps_type_based_grade(type_name, expected):
    exc = _blank_exc(type_name)
    assert str(exc) == ""
    assert classify_enrichment_failure(exc) == expected
    assert classify_enrichment_failure("", error_type=type_name) == expected


def test_untyped_blank_exception_is_retryable():
    assert classify_enrichment_failure(_blank_exc("SomeOtherError")) == "retryable"


def test_every_production_compose_call_passes_the_preflight_budget():
    """An unbudgeted call sends a body preflight never measured; tests at budget 0 can't see it."""
    import ast
    from pathlib import Path as _Path

    import menhir

    root = _Path(menhir.__file__).resolve().parent
    unbudgeted = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "compose_episode_body":
                continue
            budgeted = len(node.args) >= 2 or any(
                kw.arg == "max_estimated_tokens" for kw in node.keywords
            )
            if not budgeted:
                unbudgeted.append(f"{path.relative_to(root)}:{node.lineno}")
    assert unbudgeted == []
