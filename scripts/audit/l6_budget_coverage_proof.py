"""Proof for the L6 finding: the per-job LLM budget does not bind the judge fan-out.

Read-only. Executes the REAL production chain against fake transport and counts what actually
executed, because "how many model calls ran" is the only assertion that can tell a limit from
a meter -- the distinction CF-79 was filed about.

The chain under test, all of it production code:

    ingest_worker installs `_record_episode_llm_usage` as the usage callback   (budget scope)
      -> enrichment_steps: `for node in extracted_nodes: check_correlation(...)`
      -> correlation_service `_handle_merge_proposal`: `for judge_id in range(3)`
      -> LLMAdapter.confirm_same_entity -> LLMAdapter._chat_text
      -> OpenAIStyleChatBackend.create_chat_completion        <- announces NOTHING

Part A asks whether the budget can bind that path at all.
Part B asks what the path does with a refusal if one ever arrives.

Only the transport is faked: `openai_client_factory` for A (the seam directly BELOW the
missing announcement, so the announcement's absence is the thing under test), and a backend
that announces like `GeminiChatBackend` does for B.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from menhir.infrastructure.llm import LLMAdapter  # noqa: E402
from menhir.infrastructure.observability import (  # noqa: E402
    reset_llm_usage_callback,
    set_llm_usage_callback,
    start_llm_usage_call,
)
from menhir.infrastructure.providers import (  # noqa: E402
    OpenAIStyleChatBackend,
    ProviderConfig,
    ProviderKind,
    ProviderRuntimeDependencies,
)
from menhir.services.ingest_worker import IngestWorkerMixin, LlmBudgetExceeded  # noqa: E402

PER_JOB_BUDGET = 2
JUDGE_CALLS = 9  # 3 merge proposals x k=3 judges: one small episode's worth


class _Worker:
    """The real per-job reservation on a minimal host, as `test_llm_budget_execution_count`
    does it: `_record_episode_llm_usage` is bound off the production mixin, so this exercises
    the production callback rather than a re-implementation of it."""

    def __init__(self, *, max_per_job: int) -> None:
        self._job_llm_call_counts: dict[str, int] = {}
        self._budget_settings_max_per_job = max_per_job
        self._record_episode_llm_usage = (
            IngestWorkerMixin._record_episode_llm_usage.__get__(self)
        )

        class _Graph:
            def increment_episode_llm_usage(self, *a: Any, **kw: Any) -> None:
                return None

        self.graph_adapter = _Graph()


class _CountingCompletions:
    def __init__(self, counter: dict[str, int]) -> None:
        self._counter = counter

    async def create(self, **kwargs: Any) -> Any:
        self._counter["executed"] += 1

        class _Msg:
            content = "yes"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]
            usage = None

        return _Resp()


def _client_factory(counter: dict[str, int]):
    def factory(**kwargs: Any) -> Any:
        class _Client:
            chat = type("_Chat", (), {"completions": _CountingCompletions(counter)})()

        return _Client()

    return factory


async def part_a() -> tuple[int, int]:
    """Does the per-job budget bind the judge path? Counts real dispatches."""
    counter = {"executed": 0}
    provider = ProviderConfig(
        kind=ProviderKind.OPENAI,
        base_url="http://fake.invalid/v1",
        api_key="k",
        chat_model="fake-model",
        embed_model="fake-embed",
    )
    backend = OpenAIStyleChatBackend(
        provider=provider,
        settings=None,  # type: ignore[arg-type]  # only passed through to the client factory
        dependencies=ProviderRuntimeDependencies(
            openai_client_factory=_client_factory(counter),
        ),
    )
    llm = LLMAdapter(
        base_url=provider.base_url,
        api_key=provider.api_key,
        chat_model=provider.chat_model,
        embed_model=provider.embed_model,
        backend=backend,
    )

    worker = _Worker(max_per_job=PER_JOB_BUDGET)
    token = set_llm_usage_callback(
        lambda event: worker._record_episode_llm_usage("ep-proof", event, budget_key=None)
    )
    refused = 0
    try:
        for _ in range(JUDGE_CALLS):
            try:
                await llm.confirm_same_entity(
                    name_a="Alice", content_a="a", name_b="Alice", content_b="b",
                )
            except LlmBudgetExceeded:
                refused += 1
                break
    finally:
        reset_llm_usage_callback(token)
    return counter["executed"], refused


class _AnnouncingBackend:
    """A backend that announces its call the way `GeminiChatBackend` does -- i.e. the way the
    OpenAI-style backend WOULD if Part A's gap were closed. Part B is therefore also the
    forward-looking question: what happens to a refusal once one can actually arrive?"""

    def __init__(self, counter: dict[str, int]) -> None:
        self._counter = counter

    async def create_chat_completion(self, **kwargs: Any) -> str:
        start_llm_usage_call(
            kind="chat", model="fake-model", endpoint="chat.completions.create",
            operation=kwargs.get("operation"),
        )
        self._counter["executed"] += 1
        return "yes"


async def part_b() -> tuple[int, int, Any]:
    """What does the production caller do with a refusal? Counts announcements and retries."""
    counter = {"executed": 0}
    backend = _AnnouncingBackend(counter)
    llm = LLMAdapter(
        base_url="http://fake.invalid/v1", api_key="k",
        chat_model="fake-model", embed_model="fake-embed", backend=backend,
    )

    worker = _Worker(max_per_job=PER_JOB_BUDGET)
    announcements = {"n": 0}

    def callback(event: Any) -> None:
        if event.phase == "started":
            announcements["n"] += 1
        worker._record_episode_llm_usage("ep-proof", event, budget_key=None)

    token = set_llm_usage_callback(callback)
    escaped: Any = None
    try:
        for _ in range(JUDGE_CALLS):
            try:
                await llm.confirm_same_entity(
                    name_a="Alice", content_a="a", name_b="Alice", content_b="b",
                )
            except LlmBudgetExceeded as exc:
                escaped = exc
                break
    finally:
        reset_llm_usage_callback(token)
    return counter["executed"], announcements["n"], escaped


async def main() -> int:
    print(f"per-job budget = {PER_JOB_BUDGET} calls; the judge loop asks for {JUDGE_CALLS}\n")

    executed_a, refused_a = await part_a()
    print("PART A -- real OpenAIStyleChatBackend (the production default for local/openai)")
    print(f"  model calls actually executed : {executed_a}")
    print(f"  budget refusals raised        : {refused_a}")
    print(f"  verdict: {'BUDGET BINDS' if executed_a <= PER_JOB_BUDGET else 'BUDGET DOES NOT BIND'}"
          f" -- {executed_a} calls ran against a limit of {PER_JOB_BUDGET}\n")

    executed_b, announced_b, escaped_b = await part_b()
    print("PART B -- a backend that announces (Gemini today; every backend once A is fixed)")
    print(f"  announcements made           : {announced_b}")
    print(f"  model calls actually executed: {executed_b}")
    print(f"  refusal reached the caller   : {escaped_b is not None}")
    verdict = "REFUSAL OBEYED" if escaped_b is not None else "REFUSAL SWALLOWED/RETRIED"
    print(f"  verdict: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
