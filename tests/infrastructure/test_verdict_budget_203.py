"""#203: verdict calls must leave a reasoning model room to answer.

On reasoning models ``max_tokens`` covers hidden reasoning as well as the visible answer. At 64
tokens the k=3 identity judge mostly returned empty completions, which ``_chat_text`` turned into
``None`` votes, and the correlation judge routes any ``None`` vote to conflict. The simulator below
behaves like that model: it answers only when the budget clears its reasoning, so the old 64/128
budgets fail these tests and the fix passes them.
"""

from __future__ import annotations

import logging

import pytest

from menhir.infrastructure import llm as llm_module
from menhir.infrastructure.llm import LLMAdapter

pytestmark = [pytest.mark.unit]

_REASONING_TOKENS = 200  # hidden reasoning the simulated model spends before any visible output


class _ReasoningModelBackend:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[dict[str, object]] = []

    async def create_chat_completion(self, *, system_prompt, user_prompt, operation, max_tokens, temperature):
        self.calls.append({"operation": operation, "max_tokens": max_tokens})
        if max_tokens <= _REASONING_TOKENS:
            return ""  # finish_reason=length with no visible content
        return self.answers[operation]


def _adapter(backend: _ReasoningModelBackend) -> LLMAdapter:
    return LLMAdapter(base_url="http://fake.invalid/v1", api_key="k", chat_model="reasoning-model",
                      embed_model="embed", backend=backend)


_NODES = {"name_a": "insured estimates", "content_a": "search for insured estimates",
          "name_b": "% Insured (Estimated)", "content_b": "the quarterly insured measure"}


@pytest.mark.asyncio
async def test_identity_judge_answers_on_a_reasoning_model() -> None:
    backend = _ReasoningModelBackend({"identity_judgment": "SAME"})

    assert await _adapter(backend).confirm_same_entity(**_NODES) is True
    assert backend.calls == [{"operation": "identity_judgment", "max_tokens": llm_module._VERDICT_MAX_TOKENS}]


@pytest.mark.asyncio
async def test_contradiction_check_answers_on_a_reasoning_model() -> None:
    backend = _ReasoningModelBackend({"contradiction_check": "CONFLICT"})

    assert await _adapter(backend).confirm_contradiction(**_NODES) is True
    assert backend.calls[0]["max_tokens"] == llm_module._VERDICT_MAX_TOKENS


@pytest.mark.asyncio
async def test_shadow_tie_break_answers_on_a_reasoning_model() -> None:
    backend = _ReasoningModelBackend({"shadow_context_composition_tie_break": '{"selected": null}'})

    raw = await _adapter(backend).break_shadow_tie("episode", [{"fact_uuid": "u1", "fact": "f"}])

    assert raw == '{"selected": null}'
    assert backend.calls[0]["max_tokens"] == llm_module._VERDICT_MAX_TOKENS


def test_verdict_budget_clears_reasoning_headroom() -> None:
    # Guards against a later "tidy-up" back to a one-word budget.
    assert llm_module._VERDICT_MAX_TOKENS >= 512


@pytest.mark.asyncio
async def test_empty_completion_still_returns_none_and_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    class _Empty(_ReasoningModelBackend):
        async def create_chat_completion(self, **kwargs):
            self.calls.append(kwargs)
            return ""

    with caplog.at_level(logging.WARNING, logger=llm_module.__name__):
        vote = await _adapter(_Empty({})).confirm_same_entity(**_NODES)

    assert vote is None  # fail-safe direction unchanged: no answer is never a merge
    assert any("identity_judgment returned an empty completion" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_non_empty_verdict_is_parsed_without_warning(caplog: pytest.LogCaptureFixture) -> None:
    backend = _ReasoningModelBackend({"identity_judgment": "DIFFERENT"})

    with caplog.at_level(logging.WARNING, logger=llm_module.__name__):
        vote = await _adapter(backend).confirm_same_entity(**_NODES)

    assert vote is False
    assert not [r for r in caplog.records if "empty completion" in r.getMessage()]
