"""Optional Langfuse wiring for OpenAI-compatible clients.

Facade module: the usage-event/callback machinery lives in ``observability_usage`` and the
provider credential-failure record in ``observability_provider_auth``; every moved symbol is
re-exported here so ``menhir.infrastructure.observability`` keeps its full import surface. The
instrumented client wrappers, ``build_async_openai_client``, and the ``_default_llm_usage_callback``
sink stay in THIS module because tests patch ``AsyncOpenAI`` / ``_LocalAsyncOpenAI`` /
``LangfuseAsyncOpenAI`` / ``_default_llm_usage_callback`` on this namespace, so those lookups must
resolve here at call time.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from openai import AsyncOpenAI

from menhir.config import MemorySettings

from menhir.infrastructure.observability_provider_auth import (
    ProviderAuthFailure as ProviderAuthFailure,
    _AUTH_ERROR_MARKERS as _AUTH_ERROR_MARKERS,
    clear_provider_auth_failure as clear_provider_auth_failure,
    is_provider_auth_error as is_provider_auth_error,
    last_provider_auth_failure as last_provider_auth_failure,
    record_provider_auth_failure as record_provider_auth_failure,
)
from menhir.infrastructure.observability_usage import (
    LLMCallHandle as LLMCallHandle,
    LLMUsageCallback as LLMUsageCallback,
    LLMUsageEvent as LLMUsageEvent,
    LlmUsageControlSignal as LlmUsageControlSignal,
    _emit_llm_usage_event as _emit_llm_usage_event,
    _llm_usage_callback as _llm_usage_callback,
    _nonnegative_int as _nonnegative_int,
    _normalized_usage as _normalized_usage,
    _usage_dict as _usage_dict,
    _value as _value,
    complete_llm_usage_call as complete_llm_usage_call,
    fail_llm_usage_call as fail_llm_usage_call,
    reset_llm_usage_callback as reset_llm_usage_callback,
    set_llm_usage_callback as set_llm_usage_callback,
    start_llm_usage_call as start_llm_usage_call,
)

logger = logging.getLogger(__name__)

try:
    from langfuse.openai import AsyncOpenAI as LangfuseAsyncOpenAI
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    LangfuseAsyncOpenAI = None  # type: ignore[assignment]

_default_llm_usage_callback: LLMUsageCallback | None = None


def set_default_llm_usage_callback(callback: LLMUsageCallback | None) -> None:
    """Install the process-wide sink used outside an episode-scoped callback."""

    global _default_llm_usage_callback
    _default_llm_usage_callback = callback


class _InstrumentedEndpoint:
    """Generic instrumented wrapper for any OpenAI-compatible async endpoint."""

    def __init__(self, inner: Any, *, event_type: str, endpoint: str) -> None:
        self._inner = inner
        self._event_type = event_type
        self._endpoint = endpoint

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        model = kwargs.get("model")
        handle = start_llm_usage_call(
            kind=self._event_type,
            model=model,
            endpoint=self._endpoint,
            operation=self._endpoint,
        )
        try:
            result = await self._inner.create(*args, **kwargs)
        except Exception as exc:
            fail_llm_usage_call(handle, exc)
            raise
        complete_llm_usage_call(handle, result=result)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _InstrumentedNamespace:
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        completions = getattr(inner, "completions", None)
        if completions is not None:
            self.completions = _InstrumentedEndpoint(completions, event_type="chat", endpoint="chat.completions.create")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _CachingEmbeddingsEndpoint:
    """Cache-aware wrapper for embeddings.create() per M6 Phase 4."""

    def __init__(self, inner: Any, cache: Any) -> None:
        self._inner = inner
        self._cache = cache

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        # Extract input texts from the OpenAI embeddings contract
        input_val = kwargs.get("input") or (args[0] if args else None)
        if input_val is None or self._cache is None:
            return await self._inner.create(*args, **kwargs)

        # Normalize to list
        if isinstance(input_val, str):
            texts = [input_val]
        elif isinstance(input_val, list):
            texts = list(input_val)
        else:
            return await self._inner.create(*args, **kwargs)

        # Check cache for each text (model-aware key)
        model = kwargs.get("model", "")
        cached_vectors: dict[int, list[float]] = {}
        miss_indices: list[int] = []
        miss_texts: list[str] = []
        for i, text in enumerate(texts):
            vec = self._cache.get(text, model=model)
            if vec is not None:
                cached_vectors[i] = vec
            else:
                miss_indices.append(i)
                miss_texts.append(text)

        # All hits — build synthetic response
        if not miss_texts:
            return self._build_response(texts, cached_vectors, kwargs.get("model", ""))

        # Call upstream for misses only
        miss_kwargs = dict(kwargs)
        miss_kwargs["input"] = miss_texts if len(miss_texts) > 1 else miss_texts[0]
        upstream_result = await self._inner.create(**miss_kwargs)

        # Extract vectors from upstream and cache them
        try:
            upstream_data = upstream_result.data if hasattr(upstream_result, "data") else []
            seen_positions: set[int] = set()
            for j, item in enumerate(upstream_data):
                vec = item.embedding if hasattr(item, "embedding") else item.get("embedding") if isinstance(item, dict) else None
                # CF-191: map by the `index` the embeddings contract returns, NOT by arrival
                # position. The field exists precisely because response order is not guaranteed,
                # and a reorder preserves the COUNT -- so the length guard below cannot see it.
                # Mis-mapping here caches a text against another text's vector permanently and
                # hands the caller mismatched embeddings, with nothing downstream to detect it.
                reported = item.index if hasattr(item, "index") else (
                    item.get("index") if isinstance(item, dict) else None
                )
                position = j if reported is None else reported
                if not isinstance(position, int) or not (0 <= position < len(miss_texts)):
                    logger.warning(
                        "Embedding cache: upstream item %d reported out-of-range index %r for a "
                        "batch of %d; skipping it rather than guessing a text",
                        j, reported, len(miss_texts),
                    )
                    continue
                if position in seen_positions:
                    logger.warning(
                        "Embedding cache: upstream returned index %d more than once; skipping "
                        "the duplicate rather than overwriting", position,
                    )
                    continue
                if vec is not None:
                    seen_positions.add(position)
                    self._cache.set(miss_texts[position], list(vec), model=model)
                    cached_vectors[miss_indices[position]] = list(vec)
        except (AttributeError, TypeError, IndexError):
            logger.debug("Embedding cache: failed to extract/cache upstream vectors", exc_info=True)

        if len(cached_vectors) != len(texts):
            logger.warning(
                "Embedding cache: upstream returned %d of %d requested vectors; "
                "returning the upstream response unmodified rather than synthesising gaps",
                len(cached_vectors), len(texts),
            )
            return upstream_result

        # If we had no cache hits, just return upstream directly (preserves exact contract)
        if not any(i in cached_vectors for i in range(len(texts)) if i not in miss_indices):
            return upstream_result

        # Rebuild merged response
        return self._build_response(texts, cached_vectors, kwargs.get("model", ""), upstream_result)

    def _build_response(self, texts: list[str], vectors: dict[int, list[float]], model: str, upstream: Any = None) -> Any:
        """Build a response matching the OpenAI embeddings contract."""
        try:
            from types import SimpleNamespace
            data = []
            for i in range(len(texts)):
                vec = vectors.get(i, [])
                data.append(SimpleNamespace(embedding=vec, index=i, object="embedding"))
            usage = SimpleNamespace(prompt_tokens=0, total_tokens=0)
            if upstream and hasattr(upstream, "usage"):
                usage = upstream.usage
            return SimpleNamespace(data=data, model=model or "", object="list", usage=usage)
        except (AttributeError, TypeError):
            logger.debug("Embedding cache: failed to build synthetic response", exc_info=True)
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _InstrumentedAsyncOpenAI:
    def __init__(self, inner: Any, *, embedding_cache: Any = None) -> None:
        self._inner = inner
        chat = getattr(inner, "chat", None)
        if chat is not None:
            self.chat = _InstrumentedNamespace(chat)
        embeddings = getattr(inner, "embeddings", None)
        if embeddings is not None:
            instrumented = _InstrumentedEndpoint(embeddings, event_type="embedding", endpoint="embeddings.create")
            if embedding_cache is not None:
                self.embeddings = _CachingEmbeddingsEndpoint(instrumented, embedding_cache)
            else:
                self.embeddings = instrumented
        responses = getattr(inner, "responses", None)
        if responses is not None:
            self.responses = _InstrumentedEndpoint(responses, event_type="response", endpoint="responses.create")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def langfuse_enabled(settings: MemorySettings) -> bool:
    """Return True when Langfuse credentials are configured."""

    return bool(
        settings.langfuse_host
        and settings.langfuse_public_key
        and settings.langfuse_secret_key
    )


def _should_bypass_local_auth(base_url: str) -> bool:
    parsed = urlparse(base_url or "")
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = (parsed.hostname or "").lower()
    return hostname in {"127.0.0.1", "localhost"}


class _LocalAsyncOpenAI(AsyncOpenAI):
    @property
    def auth_headers(self) -> dict[str, str]:
        return {}


def _build_http_client(base_url: str) -> httpx.AsyncClient | None:
    if not _should_bypass_local_auth(base_url):
        return None

    async def _strip_auth(request: httpx.Request) -> None:
        request.headers.pop("Authorization", None)

    return httpx.AsyncClient(event_hooks={"request": [_strip_auth]})


def build_async_openai_client(
    *,
    base_url: str,
    api_key: str,
    settings: MemorySettings,
    embedding_cache: Any = None,
    request_timeout_s: float | None = None,
) -> Any:
    """Build an AsyncOpenAI-compatible client with optional Langfuse tracing and embedding cache."""
    http_client = _build_http_client(base_url)
    client_cls: type[AsyncOpenAI] = _LocalAsyncOpenAI if _should_bypass_local_auth(base_url) else AsyncOpenAI
    client_kwargs: dict[str, Any] = {"base_url": base_url, "api_key": api_key, "http_client": http_client}
    if request_timeout_s is not None:
        client_kwargs["timeout"] = request_timeout_s

    if not langfuse_enabled(settings):
        client: Any = client_cls(**client_kwargs)
        return _InstrumentedAsyncOpenAI(client, embedding_cache=embedding_cache)

    if LangfuseAsyncOpenAI is None:
        logger.warning(
            "Langfuse credentials are configured but langfuse is not installed; "
            "falling back to plain AsyncOpenAI"
        )
        client = client_cls(**client_kwargs)
        return _InstrumentedAsyncOpenAI(client, embedding_cache=embedding_cache)

    if _should_bypass_local_auth(base_url):
        logger.debug(
            "Langfuse tracing skipped for local endpoint %s — "
            "LangfuseAsyncOpenAI is not used with localhost targets",
            base_url,
        )
        client = client_cls(**client_kwargs)
        return _InstrumentedAsyncOpenAI(client, embedding_cache=embedding_cache)

    client = LangfuseAsyncOpenAI(**client_kwargs)
    return _InstrumentedAsyncOpenAI(client, embedding_cache=embedding_cache)
