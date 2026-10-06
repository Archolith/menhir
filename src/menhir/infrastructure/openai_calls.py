"""The one way to call OpenAI-compatible chat and embeddings: request shaping plus 429 backoff.

- Shaping is the model profile's (``apply_openai_request_policy``): reasoning models get
  ``max_completion_tokens`` and no ``temperature``, and the opt-in ``MENHIR_OPENAI_SERVICE_TIER``
  (e.g. ``flex``) is added on the OpenAI endpoint with Flex's longer per-request timeout.
- Backoff retries throttling 429s only (``openai_rate_limit``).

``acreate_chat_completion`` / ``create_chat_completion`` do both for code that builds a request;
``create_embedding`` adds backoff to an embeddings call (no shaping applies to embeddings).
``ResilientChatClient`` wraps a client so its ``chat.completions.create`` does the same, for
code that only accepts a client (Graphiti's reranker). Run the enclosing operation under
``await_with_backoff_aware_timeout`` so backoff sleeps don't count against its deadline.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from menhir.infrastructure.openai_rate_limit import (
    acall_with_rate_limit_backoff,
    await_with_backoff_aware_timeout,
    call_with_rate_limit_backoff,
)
from menhir.infrastructure.openai_request_policy import apply_openai_request_policy

__all__ = [
    "ResilientChatClient",
    "acreate_chat_completion",
    "await_with_backoff_aware_timeout",
    "create_chat_completion",
    "create_embedding",
]


def _shaped(request: Mapping[str, Any], base_url: Any, shape: bool) -> dict[str, Any]:
    if shape:
        return apply_openai_request_policy(dict(request), base_url=base_url)
    return dict(request)


async def acreate_chat_completion(
    create: Callable[..., Awaitable[Any]],
    request: Mapping[str, Any],
    *,
    base_url: Any,
    label: str,
    shape: bool = True,
    sleep: Callable[[float], Awaitable[Any]] | None = None,
) -> Any:
    """Shape ``request`` for its model and endpoint, then ``create(**request)`` with 429 backoff."""
    kwargs = _shaped(request, base_url, shape)
    return await acall_with_rate_limit_backoff(lambda: create(**kwargs), label=label, sleep=sleep)


def create_chat_completion(
    create: Callable[..., Any],
    request: Mapping[str, Any],
    *,
    base_url: Any,
    label: str,
    shape: bool = True,
) -> Any:
    """Sync ``acreate_chat_completion``."""
    kwargs = _shaped(request, base_url, shape)
    return call_with_rate_limit_backoff(lambda: create(**kwargs), label=label)


def create_embedding(
    create: Callable[..., Any], request: Mapping[str, Any], *, label: str
) -> Any:
    """``create(**request)`` for an embeddings endpoint, with 429 backoff."""
    kwargs = dict(request)
    return call_with_rate_limit_backoff(lambda: create(**kwargs), label=label)


class ResilientChatClient:
    """Wraps a client so ``chat.completions.create`` gets 429 backoff, and shaping if asked.

    ``shape=False`` passes requests through unchanged (the reranker relies on ``logprobs`` and
    ``max_tokens=1``). Every other attribute is the inner client's.
    """

    def __init__(
        self, inner: Any, *, label: str, base_url: Any = None, shape: bool = False
    ) -> None:
        self._inner = inner
        self._label = label
        self._base_url = base_url
        self._shape = shape

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def chat(self) -> Any:
        return _ResilientChat(self._inner.chat, self)


class _ResilientChat:
    def __init__(self, inner: Any, owner: ResilientChatClient) -> None:
        self._inner = inner
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def completions(self) -> Any:
        return _ResilientCompletions(self._inner.completions, self._owner)


class _ResilientCompletions:
    def __init__(self, inner: Any, owner: ResilientChatClient) -> None:
        self._inner = inner
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def create(self, **kwargs: Any) -> Any:
        owner = self._owner
        return await acreate_chat_completion(
            self._inner.create,
            kwargs,
            base_url=owner._base_url,
            label=owner._label,
            shape=owner._shape,
        )
