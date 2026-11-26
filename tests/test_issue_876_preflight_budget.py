"""#876: intake-accepted episodes must not be parked by preflight; blank failure text retries."""

from __future__ import annotations

import pytest

from menhir.config.settings_model import MemorySettings
from menhir.services.enrichment_failures import classify_enrichment_failure
from menhir.services.enrichment_steps import (
    build_episode_preflight_rejection,
    compose_episode_body,
)
from menhir.services.ingest_limits import (
    MAX_DIFF_CHARS,
    MAX_EPISODE_CHARS,
    validate_memory_payload,
)

LIMIT = MemorySettings.graphiti_episode_max_estimated_tokens


def test_intake_content_limit_is_the_default_preflight_budget():
    assert MAX_EPISODE_CHARS == 4 * LIMIT


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


@pytest.mark.parametrize("message", ["", "   ", "\n\t"])
def test_blank_failure_message_is_retryable(message):
    assert classify_enrichment_failure(message) == "retryable"
    assert classify_enrichment_failure(None) == "retryable"
