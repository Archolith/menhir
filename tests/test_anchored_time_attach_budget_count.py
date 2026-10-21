"""#282 / #106: the anchored-time attach call is announced to and counted by both LLM budgets.

The attacher runs on the instrumented client built by ``build_async_openai_client`` (not
``OpenAIStyleChatBackend``), so the real ``_InstrumentedAsyncOpenAI`` is used here with only the
transport faked. Counts are of calls that EXECUTED, per tests/test_llm_budget_execution_count.py.
"""

from __future__ import annotations

import logging
from datetime import date
from types import SimpleNamespace

import pytest

from menhir.infrastructure import providers as prov
from menhir.infrastructure.anchored_time_attach_client import AnchoredTimeAttacher
from menhir.infrastructure.observability import _InstrumentedAsyncOpenAI
from tests.test_anchored_time_attach import FACTS, GOOD
from tests.test_llm_budget_execution_count import _queue, _under_budget, _Worker

pytestmark = [pytest.mark.unit]

SPEECH_DATE = date(2024, 2, 14)


class _Transport:
    def __init__(self) -> None:
        self.executed = 0

    async def create(self, **kwargs):
        self.executed += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=GOOD))], usage=None
        )


def _attacher() -> tuple[AnchoredTimeAttacher, _Transport]:
    transport = _Transport()
    inner = SimpleNamespace(chat=SimpleNamespace(completions=transport))
    attacher = AnchoredTimeAttacher(
        _InstrumentedAsyncOpenAI(inner),
        base_url="https://api.openai.com/v1",
        model="gpt-6-luna",
        timeout_s=5.0,
    )
    return attacher, transport


async def test_one_attach_call_is_counted_by_the_job_and_session_budgets() -> None:
    q = _queue(max_calls=50, window_s=900)
    worker = _Worker(max_per_job=100, session=q)
    _under_budget(worker, "ep-1", "session-A")
    attacher, transport = _attacher()

    out = await attacher.attach("I sold the bike last week.", SPEECH_DATE, FACTS)

    assert out.status == "ok"
    assert transport.executed == 1
    assert worker._job_llm_call_counts["ep-1"] == 1
    assert len(q._session_llm_call_times["session-A"]) == 1


async def test_a_cached_attach_is_not_a_second_call() -> None:
    q = _queue(max_calls=50, window_s=900)
    worker = _Worker(max_per_job=100, session=q)
    _under_budget(worker, "ep-1", "session-A")
    attacher, transport = _attacher()

    await attacher.attach("I sold the bike last week.", SPEECH_DATE, FACTS)
    again = await attacher.attach("I sold the bike last week.", SPEECH_DATE, FACTS)

    assert again.cached is True
    assert transport.executed == 1
    assert worker._job_llm_call_counts["ep-1"] == 1


async def test_a_refused_attach_call_does_not_execute_and_returns_budget(caplog) -> None:
    q = _queue(max_calls=50, window_s=900)
    worker = _Worker(max_per_job=1, session=q)
    _under_budget(worker, "ep-1", "session-A")
    attacher, transport = _attacher()

    first = await attacher.attach("I sold the bike last week.", SPEECH_DATE, FACTS)
    with caplog.at_level(logging.WARNING):
        refused = await attacher.attach("I sold the car last month.", SPEECH_DATE, FACTS)

    assert first.status == "ok" and refused.status == "budget"
    assert refused.error_class == "LlmBudgetExceeded"
    assert transport.executed == 1
    assert any(
        "Per-job LLM budget exceeded for episode ep-1" in r.getMessage() for r in caplog.records
    )


async def test_chat_backend_call_is_counted_but_not_refused_in_report_only_mode() -> None:
    """The real ChatBackend (providers.py) announces report_only: counted past the cap, never refused."""
    transport = _Transport()
    backend = prov.OpenAIStyleChatBackend(
        provider=prov.ProviderConfig(
            kind=prov.ProviderKind.OPENAI,
            base_url="http://fake.invalid/v1",
            api_key="k",
            chat_model="fake-model",
            embed_model="fake-embed",
        ),
        settings=None,  # type: ignore[arg-type]
        dependencies=prov.ProviderRuntimeDependencies(
            openai_client_factory=lambda **_: SimpleNamespace(
                chat=SimpleNamespace(completions=transport)
            )
        ),
    )
    q = _queue(max_calls=50, window_s=900)
    worker = _Worker(max_per_job=1, session=q)
    _under_budget(worker, "ep-1", "session-A")

    for _ in range(2):
        await backend.create_chat_completion(
            system_prompt="s", user_prompt="u", operation="op", max_tokens=8, temperature=0.0
        )

    assert transport.executed == 2
    assert worker._job_llm_call_counts["ep-1"] == 2
    assert len(q._session_llm_call_times["session-A"]) == 2
