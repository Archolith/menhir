"""The stuck-enrichment backlog must be impossible to miss.

An episode classified `terminal` or `manual_review` is never requeued by
`retry_process_candidate`. It keeps its content but has no `:Entity` nodes, and recall
searches entities -- so the memory is unrecallable while `add_memory` already told the
caller it succeeded. Nothing else reports this: `_run_job` files the queue-health return
value into the telemetry sidecar and never logs it.

That combination hid 196 unrecallable episodes for eight months. These tests pin the
warning that makes the backlog visible.
"""

from __future__ import annotations

import logging

import pytest

from menhir.services.scheduler_tasks import observe_queue_health


class _Ingest:
    def get_queue_depth(self) -> int:
        return 0

    def get_failed_enrichment_count(self) -> int:
        return 0


class _Adapter:
    def __init__(self, signatures: list[dict[str, object]] | None = None, failed_count: int = 0) -> None:
        self._signatures = signatures if signatures is not None else []
        self._failed_count = failed_count

    def fetch_memory_overview(self, namespace=None) -> dict[str, object]:
        return {"pending_count": 0, "enriching_count": 0, "failed_count": self._failed_count}

    def fetch_failed_error_signatures(self, limit: int = 25) -> list[dict[str, object]]:
        return self._signatures


# The real text that went unnoticed for eight months. Falls through every marker list in
# classify_enrichment_failure to the `manual_review` default.
_OPENAI_CONTEXT_400 = (
    "Error code: 400 - {'error': {'message': \"This model's maximum context length is 128000 "
    "tokens. However, your messages resulted in 1278319 tokens.\", "
    "'type': 'invalid_request_error', 'code': 'context_length_exceeded'}}"
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stuck_backlog_warns_loudly(caplog) -> None:
    adapter = _Adapter(
        [{"error": _OPENAI_CONTEXT_400, "count": 196, "oldest_at": "2026-05-31T03:32:57Z"}],
        failed_count=196,
    )

    with caplog.at_level(logging.WARNING):
        result = await observe_queue_health(_Ingest(), adapter)

    # `terminal` since the provider-limit markers were added: still never auto-retried,
    # so it still counts toward the stuck backlog.
    assert result["awaiting_manual_review"] == 196
    assert result["failed_by_classification"] == {"terminal": 196}

    warning = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "STUCK ENRICHMENT BACKLOG" in warning
    assert "196" in warning
    assert "2026-05-31T03:32:57Z" in warning           # names how long it has been rotting
    assert "maximum context length" in warning         # names the cause
    assert "retry_failed_episodes.py" in warning       # names the remedy


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retryable_failures_do_not_warn(caplog) -> None:
    """A retryable failure is between the scheduler and the queue -- not an operator problem."""
    adapter = _Adapter([{"error": "connection refused", "count": 3, "oldest_at": "x"}], failed_count=3)

    with caplog.at_level(logging.WARNING):
        result = await observe_queue_health(_Ingest(), adapter)

    assert result["awaiting_manual_review"] == 0
    assert result["failed_by_classification"] == {"retryable": 3}
    assert "STUCK ENRICHMENT BACKLOG" not in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mixed_causes_report_each_bucket_and_warn_on_the_worst(caplog) -> None:
    adapter = _Adapter(
        [
            {"error": "connection refused", "count": 5, "oldest_at": "a"},
            {"error": _OPENAI_CONTEXT_400, "count": 40, "oldest_at": "b"},
            {"error": "episode_preflight_too_large", "count": 2, "oldest_at": "c"},
        ],
        failed_count=47,
    )

    with caplog.at_level(logging.WARNING):
        result = await observe_queue_health(_Ingest(), adapter)

    assert result["failed_by_classification"] == {"retryable": 5, "terminal": 42}
    assert result["awaiting_manual_review"] == 42  # terminal + manual_review, retryable excluded
    warning = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "maximum context length" in warning  # the 40, not the 2
    assert "episode_preflight_too_large" not in warning


@pytest.mark.unit
@pytest.mark.asyncio
async def test_clean_queue_is_silent() -> None:
    result = await observe_queue_health(_Ingest(), _Adapter([]))

    assert result["awaiting_manual_review"] == 0
    assert result["failed_by_classification"] == {}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_signature_lookup_failure_never_breaks_the_maintenance_loop(caplog) -> None:
    class _Broken(_Adapter):
        def fetch_failed_error_signatures(self, limit: int = 25) -> list[dict[str, object]]:
            raise RuntimeError("neo4j down")

    with caplog.at_level(logging.WARNING):
        result = await observe_queue_health(_Ingest(), _Broken(failed_count=9))

    # Base counts still returned; the loop keeps running.
    assert result["failed_count"] == 9
    assert "awaiting_manual_review" not in result
    assert "could not group failed-episode errors" in caplog.text
