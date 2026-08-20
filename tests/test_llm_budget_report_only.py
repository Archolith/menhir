"""CF-234: the budget must SEE every adapter call, and (for now) must not refuse any.

**What was wrong.** `OpenAIStyleChatBackend` announced nothing, so every `LLMAdapter` call it
served was invisible to both LLM budgets -- including the judge fan-out (3 calls per merge
proposal per extracted node) that CF-79 was filed to bound. `GeminiChatBackend`, behind the same
interface, announced correctly. `chat_provider` defaults to `"local"`, which routes to the silent
one, so this was the default configuration.

**Why the fix does not enforce.** A refusal has no landing zone: `LlmBudgetExceeded` is caught by
nothing, falls into `_process_episode`'s generic `except Exception`, and marks the episode FAILED.
Turning the budget on before that handler exists trades an over-spend for a lost episode. So the
announcement carries `report_only=True`: counted, logged, never refused.

That makes the *counter-assertion* the important test in this file --
`test_a_governed_call_is_still_refused` proves the measurement mode did not disarm the surfaces
that enforce today (graphiti's instrumented client). A change that made everything report-only
would pass every other test here.
"""

from __future__ import annotations

from typing import Any

import pytest

from menhir.infrastructure import providers as prov
from menhir.infrastructure.observability import (
    LLMUsageEvent,
    reset_llm_usage_callback,
    set_llm_usage_callback,
)
from menhir.services.ingest_worker import IngestWorkerMixin, LlmBudgetExceeded

pytestmark = [pytest.mark.unit]


class _Resp:
    class _Choice:
        class _Msg:
            content = "ok"

        message = _Msg()

    choices = [_Choice()]
    usage = None


def _backend(*, fail: bool = False):
    """The real `OpenAIStyleChatBackend` with only its transport faked -- the seam directly below
    the announcement, so the announcement itself is what is under test."""

    class _Completions:
        async def create(self, **kwargs: Any) -> Any:
            if fail:
                raise RuntimeError("provider exploded")
            return _Resp()

    def factory(**kwargs: Any) -> Any:
        chat = type("_Chat", (), {"completions": _Completions()})()
        return type("_Client", (), {"chat": chat})()

    return prov.OpenAIStyleChatBackend(
        provider=prov.ProviderConfig(
            kind=prov.ProviderKind.OPENAI,
            base_url="http://fake.invalid/v1",
            api_key="k",
            chat_model="fake-model",
            embed_model="fake-embed",
        ),
        settings=None,  # type: ignore[arg-type]
        dependencies=prov.ProviderRuntimeDependencies(openai_client_factory=factory),
    )


@pytest.fixture
def events():
    seen: list[LLMUsageEvent] = []
    token = set_llm_usage_callback(seen.append)
    yield seen
    reset_llm_usage_callback(token)


async def _call(backend) -> str:
    return await backend.create_chat_completion(
        system_prompt="s",
        user_prompt="u",
        operation="identity_judgment",
        max_tokens=16,
        temperature=0.0,
    )


@pytest.mark.asyncio
async def test_the_openai_style_backend_announces_its_call(events) -> None:
    """The defect itself: this backend used to announce nothing at all."""
    await _call(_backend())

    assert [e.phase for e in events] == ["started", "completed"], (
        f"the adapter call was not announced to the budget: {[e.phase for e in events]}"
    )


@pytest.mark.asyncio
async def test_the_announcement_is_marked_report_only(events) -> None:
    """Visibility without enforcement, on every phase.

    Asserted on all phases, not just `started`: a call measured at reservation must not look
    governed at completion, or a callback keying off the flag would behave inconsistently within
    one call.
    """
    await _call(_backend())

    assert all(e.report_only for e in events), (
        f"an adapter call was announced as governed: {[(e.phase, e.report_only) for e in events]}"
    )


@pytest.mark.asyncio
async def test_a_provider_failure_is_still_announced(events) -> None:
    """A failed call that is never announced leaves the budget's counter permanently short."""
    with pytest.raises(RuntimeError, match="exploded"):
        await _call(_backend(fail=True))

    assert [e.phase for e in events] == ["started", "failed"]
    assert all(e.report_only for e in events)


def test_both_backends_behind_the_interface_agree() -> None:
    """One interface, one behaviour. The split between the two backends IS the finding, so a fix
    that left them disagreeing in the other direction would not have closed it."""
    import inspect

    src = inspect.getsource(prov.GeminiChatBackend.create_chat_completion)
    assert "report_only=True" in src, (
        "the Gemini backend announces in a different mode from the OpenAI-style one -- that is "
        "the same one-interface-two-behaviours split CF-234 was filed about"
    )


# ---------------------------------------------------------------------------
# The reservation side
# ---------------------------------------------------------------------------


class _Worker:
    """The production reservation bound onto a minimal host, as CF-79's own suite does it."""

    def __init__(self, *, max_per_job: int) -> None:
        self._job_llm_call_counts: dict[str, int] = {}
        self._budget_settings_max_per_job = max_per_job
        self._record_episode_llm_usage = IngestWorkerMixin._record_episode_llm_usage.__get__(self)
        self.graph_adapter = type(
            "_G", (), {"increment_episode_llm_usage": lambda self, *a, **kw: None}
        )()


def _event(*, report_only: bool) -> LLMUsageEvent:
    return LLMUsageEvent(
        kind="chat", phase="started", operation="identity_judgment", report_only=report_only
    )


def test_a_measured_call_over_budget_is_counted_but_not_refused() -> None:
    """Report-only: the overrun is visible and the counter stays truthful past the limit.

    The counter matters as much as the absence of a raise -- a mode that stopped counting once
    over budget would report "2 calls" for an episode that made 9, and the measurement exists to
    size the cap.
    """
    worker = _Worker(max_per_job=2)
    for _ in range(9):
        worker._record_episode_llm_usage("ep-1", _event(report_only=True), budget_key=None)

    assert worker._job_llm_call_counts["ep-1"] == 9, (
        "the report-only branch stopped counting at the limit, so the overrun is unmeasurable"
    )


def test_a_governed_call_is_still_refused() -> None:
    """**The counter-assertion.** Measurement mode must not disarm the surfaces that enforce today.

    Graphiti's instrumented client announces without `report_only`, and its refusals bind. A fix
    that made the budget globally report-only would pass every other test in this file while
    silently removing CF-79's only working enforcement.
    """
    worker = _Worker(max_per_job=2)
    worker._record_episode_llm_usage("ep-2", _event(report_only=False), budget_key=None)
    worker._record_episode_llm_usage("ep-2", _event(report_only=False), budget_key=None)

    with pytest.raises(LlmBudgetExceeded, match="ep-2"):
        worker._record_episode_llm_usage("ep-2", _event(report_only=False), budget_key=None)


def test_the_two_modes_share_one_counter() -> None:
    """A measured call must debit the same budget a governed one does.

    Otherwise an episode could spend its whole allowance through the adapter surface and still
    present a fresh budget to graphiti -- the budget would be per-surface rather than per-job.
    """
    worker = _Worker(max_per_job=2)
    worker._record_episode_llm_usage("ep-3", _event(report_only=True), budget_key=None)
    worker._record_episode_llm_usage("ep-3", _event(report_only=True), budget_key=None)

    with pytest.raises(LlmBudgetExceeded):
        worker._record_episode_llm_usage("ep-3", _event(report_only=False), budget_key=None)
