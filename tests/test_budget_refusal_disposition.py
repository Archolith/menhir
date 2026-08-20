"""CF-235 and the per-job budget's missing landing zone.

**Two defects, one root.** A budget refusal is a *decision*; every handler between the reservation
and the operator was written for *faults*.

* `_chat_text` caught it in a retry loop and returned `None` (CF-235), so the judge read "the
  model was unavailable" and routed the merge to conflict. Third instance of the shape after
  CF-227 and CF-231.
* The classifier never mentioned budgets, so a refusal reached `manual_review` by falling through
  every marker list -- the right destination for the wrong reason, and one marker away from
  `retryable`.

**Why `retryable` is the dangerous grade, and why it is asserted here.** A per-job overrun is
deterministic: the same episode re-extracts roughly the same entities and re-runs the same judge
fan-out. Requeueing it spends the budget again to reach the same refusal, turning a cost control
into a cost amplifier. `test_a_budget_refusal_is_never_retryable` is the load-bearing test in this
file.

**What this does NOT do.** Nothing here enables enforcement. The adapter surface still announces
`report_only=True` (CF-234), so no refusal is raised there at all; these paths are reachable today
only on graphiti's instrumented client. That is deliberate -- the refusal now behaves correctly
wherever it appears, which is what has to be true *before* the enforcing half lands, not after.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure.llm import LLMAdapter
from menhir.infrastructure.observability import LlmUsageControlSignal
from menhir.services.enrichment_failures import (
    classify_enrichment_failure,
    is_budget_refusal,
)
from menhir.services.ingest_worker import LlmBudgetExceeded

pytestmark = [pytest.mark.unit]

PER_JOB = "episode ep-1 exceeded its per-job LLM budget (9 calls, limit 2)"
SESSION = "session k exhausted its LLM call budget (limit 50 calls per 3600s)"


# ---------------------------------------------------------------------------
# CF-235: the refusal must survive the adapter's retry loop
# ---------------------------------------------------------------------------


class _RefusingBackend:
    """Announces by refusing, as the budget callback does at reservation."""

    def __init__(self) -> None:
        self.attempts = 0

    async def create_chat_completion(self, **kwargs: Any) -> str:
        self.attempts += 1
        raise LlmBudgetExceeded(PER_JOB)


class _FaultyBackend:
    def __init__(self) -> None:
        self.attempts = 0

    async def create_chat_completion(self, **kwargs: Any) -> str:
        self.attempts += 1
        raise RuntimeError("provider exploded")


def _adapter(backend) -> LLMAdapter:
    """A real adapter whose only fake is the transport and the retry SLEEP.

    `compress_content` is the caller under test rather than `confirm_same_entity`, which passes
    `max_retries=0` and so cannot show retry behaviour either way. This one takes the default 8
    retries -- the same policy as `repair_edge_facts`, which sits on the enrichment path and is
    where CF-235's ~4 minutes of backoff would actually be spent.
    """
    from menhir.infrastructure.providers import ProviderRuntimeDependencies

    async def _no_sleep(_seconds: float) -> None:
        return None

    return LLMAdapter(
        base_url="http://fake.invalid/v1",
        api_key="k",
        chat_model="m",
        embed_model="e",
        backend=backend,
        dependencies=ProviderRuntimeDependencies(retry_sleep=_no_sleep),
    )


@pytest.mark.asyncio
async def test_a_budget_refusal_reaches_the_caller_unretried() -> None:
    """The defect: retried 8 times at exponential backoff, then reported as `None`."""
    backend = _RefusingBackend()

    with pytest.raises(LlmUsageControlSignal):
        await _adapter(backend).compress_content("some memory content")

    assert backend.attempts == 1, (
        f"a refusal was retried {backend.attempts}x -- retrying a deliberate refusal is the one "
        "response guaranteed to be wrong, and on this path it is ~4 minutes of backoff"
    )


@pytest.mark.asyncio
async def test_an_ordinary_provider_fault_is_still_retried_and_degrades() -> None:
    """The counter-assertion. Narrowing the handler must not turn every provider blip into a
    propagating failure -- graceful degradation to `None` is the behaviour the retry loop exists
    for, and only a signal declaring itself control flow may bypass it."""
    backend = _FaultyBackend()

    result = await _adapter(backend).compress_content("some memory content")

    assert result is None
    assert backend.attempts > 1, (
        f"an ordinary provider fault stopped being retried (attempts={backend.attempts})"
    )


# ---------------------------------------------------------------------------
# Disposition: parked by decision, and never retried
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error", [PER_JOB, SESSION, LlmBudgetExceeded(PER_JOB)])
def test_a_budget_refusal_is_recognised(error: object) -> None:
    assert is_budget_refusal(error)


def test_it_is_recognised_by_type_even_if_the_message_is_reworded() -> None:
    """The message is the part most likely to be edited; the type is exact."""
    assert is_budget_refusal(LlmBudgetExceeded("some future wording"))


@pytest.mark.parametrize(
    "error",
    ["graphiti add_episode timed out", "zero_extraction", "connection refused", "invalid json"],
)
def test_ordinary_failures_are_not_mistaken_for_budget_refusals(error: str) -> None:
    """Without this the branch could swallow the whole classifier and every failure would park."""
    assert not is_budget_refusal(error)


def test_a_budget_refusal_is_never_retryable() -> None:
    """**The load-bearing assertion.**

    A per-job overrun is deterministic, so requeueing it spends the budget again to reach the
    same refusal. The message deliberately carries `429` -- a `_RETRYABLE_ERROR_MARKERS` entry --
    because the budget branch is only correct if it runs BEFORE the marker lists. Ordered after
    them, this exact string grades `retryable`.
    """
    assert classify_enrichment_failure("exceeded its per-job LLM budget after a 429") != "retryable"


def test_a_budget_refusal_parks_for_operator_review() -> None:
    """`manual_review`, not `terminal`: an operator CAN make this episode succeed by raising the
    cap, so it is a policy limit rather than a structural impossibility. Both park permanently,
    so this pins intent rather than changing behaviour."""
    assert classify_enrichment_failure(LlmBudgetExceeded(PER_JOB)) == "manual_review"


def test_the_parked_pile_separates_budget_from_faults() -> None:
    """The two need opposite operator actions -- raise the cap and requeue, versus fix a model or
    prompt. Folded together they read as one backlog with no indicated action, which is how a
    196-episode pile once sat unactioned for eight months."""
    from menhir.services import scheduler_tasks as st

    rows = [
        {"error": PER_JOB, "count": 4, "oldest_at": "2026-08-01"},
        {"error": "invalid json", "count": 7, "oldest_at": "2026-08-02"},
    ]
    budget = sum(int(r["count"]) for r in rows if st.is_budget_refusal(str(r["error"])))
    parked = sum(
        int(r["count"])
        for r in rows
        if classify_enrichment_failure(str(r["error"])) in st._NEVER_RETRIED_CLASSIFICATIONS
    )

    assert (budget, parked) == (4, 11), (
        "the budget pile is not separable from the parse-error pile inside the parked total"
    )
