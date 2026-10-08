"""Anchored-time resolver: the one LLM call, its timeout, and a small result cache.

``resolve`` never raises except on cancellation: every failure becomes a status, and the caller
keeps Graphiti's times. The request gets what Graphiti's provider proxy gives its calls (provider
extras, then ``openai_calls`` shaping, Flex tier and 429 backoff, on the instrumented client for
usage accounting) but not MenhirOpenAIGenericClient, which appends a language instruction to the
system prompt and would change the frozen prompt.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import logging
import time
from typing import Any

from menhir.infrastructure.anchored_time import PROMPT_VERSION, build_messages, parse_items
from menhir.infrastructure.graphiti_llm_adapter import _provider_extra_body
from menhir.infrastructure.observability import LlmUsageControlSignal
from menhir.infrastructure.openai_calls import (
    acreate_chat_completion,
    await_with_backoff_aware_timeout,
)

logger = logging.getLogger(__name__)

MAX_COMPLETION_TOKENS = 3000


@dataclass(frozen=True)
class ResolverOutcome:
    status: str  # ok | timeout | error | budget | empty | parse_error
    items: dict[int, dict] | None = None
    missing_events: int = 0
    latency_s: float | None = None
    cached: bool = False
    error_class: str = ""


def build_request(model: str, turn: str, speech_date: date, facts: list[str]) -> dict[str, Any]:
    """Unshaped request; the provider client shapes it (gpt-6: max_completion_tokens, no temperature)."""
    return {
        "model": model,
        "messages": build_messages(turn, speech_date.isoformat(), facts),
        "max_tokens": MAX_COMPLETION_TOKENS,
        "response_format": {"type": "json_object"},
    }


def _content_of(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return str(getattr(message, "content", None) or "")


class AnchoredTimeResolver:
    def __init__(
        self,
        client: Any,
        *,
        base_url: str,
        model: str,
        timeout_s: float,
        cache_size: int = 256,
    ) -> None:
        # ``client`` must be unwrapped (no retry of its own): acreate_chat_completion adds the
        # one backoff layer, and a wrapped client would nest a second.
        self._client = client
        self._base_url = base_url
        self.model = model
        self._timeout_s = float(timeout_s)
        self._cache_size = max(0, int(cache_size))
        self._cache: OrderedDict[str, tuple[dict[int, dict], int]] = OrderedDict()

    def _key(self, turn: str, speech_date: date, facts: list[str]) -> str:
        payload = json.dumps(
            [PROMPT_VERSION, self.model, speech_date.isoformat(), turn, list(facts)],
            ensure_ascii=True,
        )
        return hashlib.sha256(payload.encode("ascii")).hexdigest()

    async def resolve(self, turn: str, speech_date: date, facts: list[str]) -> ResolverOutcome:
        key = self._key(turn, speech_date, facts)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            items, missing = hit
            return ResolverOutcome("ok", items=_copy_items(items), missing_events=missing, cached=True)
        request = build_request(self.model, turn, speech_date, facts)
        extra = _provider_extra_body(self.model, self._base_url)
        if extra:
            request["extra_body"] = extra
        started = time.monotonic()
        try:
            # Backoff sleeps pause this deadline and the enclosing add_episode one (nested clocks
            # forward); test_nested_backoff_deadline covers the resolver-inside-add_episode case.
            response = await await_with_backoff_aware_timeout(
                acreate_chat_completion(
                    self._client.chat.completions.create,
                    request,
                    base_url=self._base_url,
                    label="anchored_time",
                ),
                timeout_s=self._timeout_s,
            )
        except TimeoutError:
            return ResolverOutcome("timeout", latency_s=time.monotonic() - started)
        except LlmUsageControlSignal as exc:
            return ResolverOutcome(
                "budget", latency_s=time.monotonic() - started, error_class=type(exc).__name__
            )
        except Exception as exc:  # provider/network error; never blocks ingest
            return ResolverOutcome(
                "error", latency_s=time.monotonic() - started, error_class=type(exc).__name__
            )
        latency = time.monotonic() - started
        content = _content_of(response)
        if not content.strip():
            return ResolverOutcome("empty", latency_s=latency)
        try:
            items, missing = parse_items(content)
        except (ValueError, TypeError, AttributeError):  # json.JSONDecodeError is a ValueError
            return ResolverOutcome("parse_error", latency_s=latency)
        if self._cache_size:
            self._cache[key] = (_copy_items(items), len(missing))
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return ResolverOutcome("ok", items=items, missing_events=len(missing), latency_s=latency)


def _copy_items(items: dict[int, dict]) -> dict[int, dict]:
    # The clause guard replaces entries in place; the cache must not see that.
    return {i: dict(it) for i, it in items.items()}
