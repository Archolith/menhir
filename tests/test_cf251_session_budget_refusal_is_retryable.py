"""CF-251 -- the two LLM budget refusals have opposite retry semantics and were treated alike.

`_BUDGET_REFUSAL_MARKERS` matched both refusals and `classify_enrichment_failure` sent both to
`manual_review`, which `_NEVER_RETRIED_CLASSIFICATIONS` parks permanently. The comment justifying
that reasoned entirely about the per-job case:

    "A per-job overrun is deterministic -- the same episode re-extracts roughly the same entities
     and re-runs the same judge fan-out -- so retrying it spends the budget again to reach the
     same refusal."

True for per-job. **False for the session window**, which is `N calls per W seconds` -- a refusal
there means "too many calls recently" and stops being true on its own. Both raise
`LlmBudgetExceeded`, so the exception type cannot separate them; only the message can.

Observed in production 2026-08-23, which is what makes this a defect rather than a theory: 26
episodes were parked unrecallable, and episode `02d9d306` -- parked on the SESSION window -- later
succeeded unchanged, with no difference but a fresh window. Nothing would ever have retried it.

**The half that makes the retry real.** Reclassifying alone would have been worse than useless:
the ordinary backoff is `30 * 2^(attempts-1)`, starting at 30s against a 900s window, so every
attempt would land inside the same exhausted window, fail identically, and burn the attempt
ceiling -- parking the episode exactly as before, having spent calls to get there. The retry is
floored at the configured window length.

The error strings below are verbatim from the production failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

PER_JOB = "episode 15633d9f-0216-46e4-89a0-b94089f9807f exceeded its per-job LLM budget (14 calls, limit 10)"
SESSION = "session remote-mcp-2ef0e52c382886fe exhausted its LLM call budget (limit 50 calls per 900s)"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_session_window_refusal_is_retryable_and_the_per_job_one_is_not() -> None:
    """THE FINDING. One clears itself; the other reaches the identical refusal."""
    from menhir.services.enrichment_failures import classify_enrichment_failure

    assert classify_enrichment_failure(SESSION) == "retryable"
    assert classify_enrichment_failure(PER_JOB) == "manual_review"


@pytest.mark.unit
def test_both_are_still_recognised_as_budget_refusals() -> None:
    """The split must not cost the shared identity: `parked_on_budget` and the operator-facing
    "the cap is too low" signal both key off `is_budget_refusal`, not off the classification."""
    from menhir.services.enrichment_failures import is_budget_refusal

    assert is_budget_refusal(SESSION)
    assert is_budget_refusal(PER_JOB)


@pytest.mark.unit
def test_an_unrecognised_budget_message_stays_parked_rather_than_retried() -> None:
    """FAIL CLOSED. Wrongly retrying a deterministic overrun turns a cost control into a cost
    amplifier; wrongly parking a transient one costs an operator one requeue. When the message
    cannot be identified, take the second failure."""
    from menhir.services.enrichment_failures import (
        classify_enrichment_failure,
        is_session_window_refusal,
    )

    class LlmBudgetExceeded(Exception):
        pass

    reworded = LlmBudgetExceeded("budget refused this call for reasons not spelled out")

    assert not is_session_window_refusal(reworded)
    assert classify_enrichment_failure(reworded) == "manual_review"


@pytest.mark.unit
def test_the_exception_type_alone_does_not_make_a_refusal_session_scoped() -> None:
    """Both refusals raise the same type. If the type were ever accepted as evidence here, a
    per-job overrun would be silently promoted to retryable -- the amplifier case."""
    from menhir.services.enrichment_failures import is_session_window_refusal

    assert not is_session_window_refusal(PER_JOB, error_type="LlmBudgetExceeded")
    assert is_session_window_refusal(SESSION, error_type="LlmBudgetExceeded")


@pytest.mark.unit
def test_a_non_budget_failure_is_not_swept_into_the_session_class() -> None:
    from menhir.services.enrichment_failures import is_session_window_refusal

    for unrelated in ("connection refused", "graphiti unavailable", "zero_extraction", ""):
        assert not is_session_window_refusal(unrelated), unrelated


# ---------------------------------------------------------------------------
# The retry actually defers past the window
# ---------------------------------------------------------------------------


@dataclass
class _Adapter:
    def find_completed_episode_artifact(self, *, anchor_uuid: str, anchor_name: str) -> None:
        return None


class _Ingest:
    def __init__(self, window_s: int = 900) -> None:
        self.window_s = window_s
        self.requeued: list[str] = []

    def get_queue_depth(self) -> int:
        return 0

    def get_context_window_retry_attempts(self) -> int:
        return 6

    def get_llm_session_window_seconds(self) -> int:
        return self.window_s

    async def requeue_failed_episode(self, episode_uuid: str) -> bool:
        self.requeued.append(episode_uuid)
        return True


def _row(error: str, *, completed_ago_s: int, attempts: int = 1) -> dict:
    completed = datetime.now(timezone.utc) - timedelta(seconds=completed_ago_s)
    return {
        "uuid": "ep-1",
        "name": "anchor-1",
        "processing_attempts": attempts,
        "processing_error": error,
        "processing_completed_at": completed.isoformat(),
    }


async def _run(row: dict, ingest: _Ingest) -> str:
    from menhir.services.scheduler_tasks import retry_process_candidate

    with patch("menhir.services.scheduler_tasks.record_failure_event"):
        return await retry_process_candidate(
            _Adapter(), ingest, row, max_attempts=5, now=datetime.now(timezone.utc),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_session_refusal_inside_the_window_waits_instead_of_burning_an_attempt() -> None:
    """The ordinary backoff would have fired at 30s, into the same exhausted window."""
    ingest = _Ingest(window_s=900)

    result = await _run(_row(SESSION, completed_ago_s=60), ingest)

    assert result == "waiting", "retried inside the window it was refused by"
    assert ingest.requeued == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_session_refusal_is_requeued_once_the_window_has_rolled() -> None:
    """THE REPAIR. This is the episode that sat parked forever before."""
    ingest = _Ingest(window_s=900)

    result = await _run(_row(SESSION, completed_ago_s=1000), ingest)

    assert result == "requeued"
    assert ingest.requeued == ["ep-1"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_floor_follows_the_CONFIGURED_window_not_a_constant() -> None:
    """A hardcoded 900 would pass the two tests above and be wrong for any other setting."""
    ingest = _Ingest(window_s=3600)

    waited = await _run(_row(SESSION, completed_ago_s=1000), ingest)
    assert waited == "waiting", "deferred by a constant rather than the configured window"

    ingest_short = _Ingest(window_s=60)
    requeued = await _run(_row(SESSION, completed_ago_s=1000), ingest_short)
    assert requeued == "requeued"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_per_job_overrun_is_still_never_requeued() -> None:
    """The half of the original reasoning that was correct, pinned so the split cannot erode it."""
    ingest = _Ingest()

    result = await _run(_row(PER_JOB, completed_ago_s=100_000), ingest)

    assert result == "terminal"
    assert ingest.requeued == [], "a deterministic overrun was requeued to reach the same refusal"


# ---------------------------------------------------------------------------
# The health metric stays honest
# ---------------------------------------------------------------------------


class _HealthAdapter:
    """Reports one parked per-job refusal and one self-clearing session refusal."""

    def fetch_memory_overview(self) -> dict[str, object]:
        return {"pending_count": 0, "enriching_count": 0, "failed_count": 5}

    def fetch_failed_error_signatures(self, limit: int = 25) -> list[dict[str, object]]:
        return [
            {"error": PER_JOB, "count": 2, "oldest_at": "2026-08-20T22:29:05+00:00"},
            {"error": SESSION, "count": 3, "oldest_at": "2026-08-23T03:25:15+00:00"},
        ]


class _HealthIngest(_Ingest):
    def get_failed_enrichment_count(self) -> int:
        return 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parked_on_budget_counts_only_refusals_nothing_will_retry() -> None:
    """`parked_on_budget` exists to tell an operator "the cap is too low, raise it and requeue".
    A session refusal now clears itself, so counting it would report a backlog needing no action
    -- the same dilution this metric was split out of the general pile to avoid.

    Exercises `observe_queue_health` rather than restating its predicate: an earlier version of
    this test recomputed the condition locally and would have passed against any implementation,
    including the one it was written to catch.
    """
    from menhir.services.scheduler_tasks import observe_queue_health

    result = await observe_queue_health(_HealthIngest(), _HealthAdapter())

    assert result["parked_on_budget"] == 2, "the self-clearing session refusal was counted as parked"
    assert result["awaiting_manual_review"] == 2
    assert result["failed_by_classification"] == {"manual_review": 2, "retryable": 3}
