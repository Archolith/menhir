"""Blind attach check call (runs under MENHIR_ANCHORED_TIME_RESOLVER): one LLM call, its timeout, and a small cache.

Mirrors anchored_time_resolver: ``attach`` never raises except on cancellation, and every failure
becomes a status. Any non-ok status means no write (V3) in the caller. The prompt and the answer
parser live in anchored_time_attach.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import logging
import time
from typing import Any, Mapping

from menhir.infrastructure.anchored_time_attach import (
    ATTACH_PROMPT_VERSION,
    build_messages,
    parse_answers,
)
from menhir.infrastructure.anchored_time_resolver import _content_of
from menhir.infrastructure.graphiti_llm_adapter import _provider_extra_body
from menhir.infrastructure.observability import LlmUsageControlSignal
from menhir.infrastructure.openai_calls import (
    acreate_chat_completion,
    await_with_backoff_aware_timeout,
)

logger = logging.getLogger(__name__)

MAX_COMPLETION_TOKENS = 3000


@dataclass(frozen=True)
class AttachOutcome:
    status: str  # ok | timeout | error | budget | empty | parse_error
    answers: dict[int, dict] | None = None
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


def _copy_answers(answers: dict[int, dict]) -> dict[int, dict]:
    return {i: dict(a) for i, a in answers.items()}


class AnchoredTimeAttacher:
    def __init__(
        self,
        client: Any,
        *,
        base_url: str,
        model: str,
        timeout_s: float,
        cache_size: int = 256,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        # ``client`` must be unwrapped (no retry of its own): acreate_chat_completion adds the
        # one backoff layer, and a wrapped client would nest a second.
        self._client = client
        self._extra_body: dict[str, Any] = dict(extra_body or {})
        self._base_url = base_url
        self.model = model
        self.prompt_version = ATTACH_PROMPT_VERSION
        self._timeout_s = float(timeout_s)
        self._cache_size = max(0, int(cache_size))
        self._cache: OrderedDict[str, dict[int, dict]] = OrderedDict()

    def _key(self, turn: str, speech_date: date, facts: list[str]) -> str:
        payload = json.dumps(
            [ATTACH_PROMPT_VERSION, self.model, speech_date.isoformat(), turn, list(facts)],
            ensure_ascii=True,
        )
        return hashlib.sha256(payload.encode("ascii")).hexdigest()

    async def attach(self, turn: str, speech_date: date, facts: list[str]) -> AttachOutcome:
        key = self._key(turn, speech_date, facts)
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return AttachOutcome("ok", answers=_copy_answers(hit), cached=True)
        request = build_request(self.model, turn, speech_date, facts)
        extra = {**_provider_extra_body(self.model, self._base_url), **self._extra_body}
        if extra:
            request["extra_body"] = extra
        started = time.monotonic()
        try:
            response = await await_with_backoff_aware_timeout(
                acreate_chat_completion(
                    self._client.chat.completions.create,
                    request,
                    base_url=self._base_url,
                    label="anchored_time_attach",
                ),
                timeout_s=self._timeout_s,
            )
        except TimeoutError:
            return AttachOutcome("timeout", latency_s=time.monotonic() - started)
        except LlmUsageControlSignal as exc:
            return AttachOutcome(
                "budget", latency_s=time.monotonic() - started, error_class=type(exc).__name__
            )
        except Exception as exc:  # provider/network error; never blocks ingest
            return AttachOutcome(
                "error", latency_s=time.monotonic() - started, error_class=type(exc).__name__
            )
        latency = time.monotonic() - started
        content = _content_of(response)
        if not content.strip():
            return AttachOutcome("empty", latency_s=latency)
        try:
            answers = parse_answers(content)
        except (ValueError, TypeError, AttributeError):  # json.JSONDecodeError is a ValueError
            return AttachOutcome("parse_error", latency_s=latency)
        if self._cache_size:
            self._cache[key] = _copy_answers(answers)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return AttachOutcome("ok", answers=answers, latency_s=latency)
