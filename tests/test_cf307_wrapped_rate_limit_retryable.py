"""CF-307: a provider 429 rewrapped by Graphiti must requeue, not park in manual_review.

Graphiti's OpenAI client turns ``openai.RateLimitError`` into its own ``RateLimitError`` whose
text is "Rate limit exceeded. Please try again later." -- no "429". A raw openai 429 already
classified retryable; the wrapped one fell through to manual_review, which the scheduler never
retries. Billing must still stop: an insufficient_quota 429 under the same wrapper stays parked.
"""

from __future__ import annotations

import httpx
import openai
import pytest

from menhir.infrastructure.openai_rate_limit import quota_exhausted_error
from menhir.services.enrichment_failures import classify_enrichment_failure

graphiti_errors = pytest.importorskip("graphiti_core.llm_client.errors")


def _openai_429(code: str | None) -> openai.RateLimitError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    body = {"error": {"message": "throttled", "type": code or "requests", "code": code}}
    response = httpx.Response(429, request=request, json=body)
    return openai.RateLimitError("Error code: 429", response=response, body=body["error"])


def _wrapped(cause: BaseException) -> BaseException:
    try:
        raise graphiti_errors.RateLimitError() from cause
    except graphiti_errors.RateLimitError as exc:
        return exc


@pytest.mark.parametrize("code", [None, "rate_limit_exceeded", "resource_unavailable"])
def test_wrapped_throttle_or_flex_429_is_retryable(code: str | None) -> None:
    exc = _wrapped(_openai_429(code))

    assert classify_enrichment_failure(exc, error_type=type(exc).__name__) == "retryable"


def test_persisted_wrapped_429_text_is_retryable() -> None:
    """The scheduler retry pass re-classifies the stored message, with no exception type."""
    assert classify_enrichment_failure("Rate limit exceeded. Please try again later.") == "retryable"


def test_wrapped_insufficient_quota_still_parks() -> None:
    """The live path converts quota first (handle_enrichment_failure), then classifies."""
    raw = _wrapped(_openai_429("insufficient_quota"))
    exc = quota_exhausted_error(raw) or raw

    assert classify_enrichment_failure(exc, error_type=type(exc).__name__) == "manual_review"
    # And the message the live path persists keeps parking on the scheduler's re-read.
    assert classify_enrichment_failure(str(exc)) == "manual_review"


def test_wrapped_insufficient_quota_parks_even_unconverted() -> None:
    """Defence in depth: the cause-chain quota check runs before any retryable marker."""
    exc = _wrapped(_openai_429("insufficient_quota"))

    assert classify_enrichment_failure(exc, error_type=type(exc).__name__) == "manual_review"
