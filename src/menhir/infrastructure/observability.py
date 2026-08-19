"""Optional Langfuse wiring for OpenAI-compatible clients."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
import logging
from time import perf_counter
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

import httpx
from openai import AsyncOpenAI

from menhir.config import MemorySettings

logger = logging.getLogger(__name__)

try:
    from langfuse.openai import AsyncOpenAI as LangfuseAsyncOpenAI
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    LangfuseAsyncOpenAI = None  # type: ignore[assignment]


@dataclass(frozen=True)
class LLMUsageEvent:
    """One observed LLM endpoint invocation."""

    kind: str
    phase: str
    model: str | None = None
    endpoint: str | None = None
    operation: str | None = None
    call_id: str | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    provider_usage: dict[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class LLMCallHandle:
    """Correlation state for one instrumented provider-client call."""

    call_id: str
    kind: str
    model: str | None
    endpoint: str | None
    operation: str | None
    started_at: float


LLMUsageCallback = Callable[[LLMUsageEvent], None]
_llm_usage_callback: ContextVar[LLMUsageCallback | None] = ContextVar(
    "menhir_llm_usage_callback",
    default=None,
)
_default_llm_usage_callback: LLMUsageCallback | None = None


def set_llm_usage_callback(callback: LLMUsageCallback | None) -> Token[LLMUsageCallback | None]:
    """Install a request-scoped callback for LLM usage instrumentation."""

    return _llm_usage_callback.set(callback)


def reset_llm_usage_callback(token: Token[LLMUsageCallback | None]) -> None:
    """Reset the request-scoped LLM usage callback to a previous token."""

    _llm_usage_callback.reset(token)


def set_default_llm_usage_callback(callback: LLMUsageCallback | None) -> None:
    """Install the process-wide sink used outside an episode-scoped callback."""

    global _default_llm_usage_callback
    _default_llm_usage_callback = callback


class LlmUsageControlSignal(Exception):
    """Raised BY a usage callback to REFUSE the call it is being notified about.

    The emitter below swallows exceptions from usage callbacks, and that is correct for what it
    was written for: an instrumentation fault must never take down the caller it is observing.
    But a budget REFUSAL is not an instrumentation fault -- it is a control decision whose entire
    purpose is to affect the caller, and the blanket `except Exception` swallowed it identically.

    That made CF-79's per-job reservation ineffective in production: `_record_episode_llm_usage`
    raised, this module logged it at DEBUG, and the call proceeded -- which is precisely the
    "counts and warns, never stops a call" behaviour CF-79 was filed about, one layer further
    down than anyone looked.

    Opting in explicitly, rather than narrowing the `except` to a specific type, keeps the
    original principle intact: anything a callback raises by ACCIDENT is still swallowed, and
    only a signal deliberately declaring itself control flow propagates.
    """


def _emit_llm_usage_event(
    kind: str,
    *,
    phase: str,
    model: str | None = None,
    endpoint: str | None = None,
    operation: str | None = None,
    call_id: str | None = None,
    duration_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    cached_input_tokens: int | None = None,
    reasoning_output_tokens: int | None = None,
    provider_usage: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    callback = _llm_usage_callback.get() or _default_llm_usage_callback
    if callback is None:
        return
    try:
        callback(
            LLMUsageEvent(
                kind=kind,
                phase=phase,
                model=model,
                endpoint=endpoint,
                operation=operation,
                call_id=call_id,
                duration_ms=duration_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cached_input_tokens=cached_input_tokens,
                reasoning_output_tokens=reasoning_output_tokens,
                provider_usage=provider_usage,
                error=error,
            )
        )
    except LlmUsageControlSignal:
        # A deliberate refusal, not a fault. It MUST reach the caller -- swallowing it is what
        # left CF-79's reservation unable to stop anything.
        raise
    except Exception:  # pragma: no cover - instrumentation must not affect callers
        logger.debug(
            "LLM usage callback failed for kind=%s phase=%s endpoint=%s",
            kind,
            phase,
            endpoint,
            exc_info=True,
        )


def _value(source: Any, *names: str) -> Any:
    for name in names:
        if isinstance(source, dict) and name in source:
            return source[name]
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def _nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _usage_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        payload = dict(usage)
    elif hasattr(usage, "model_dump"):
        payload = usage.model_dump(exclude_none=True)
    elif hasattr(usage, "__dict__"):
        payload = {name: value for name, value in vars(usage).items() if value is not None}
    else:
        payload = {}
    if not payload:
        payload = {
            name: value
            for name in (
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "input_tokens",
                "output_tokens",
                "promptTokenCount",
                "candidatesTokenCount",
                "totalTokenCount",
            )
            if (value := getattr(usage, name, None)) is not None
        }
    return payload or None


def _normalized_usage(usage: Any) -> dict[str, Any]:
    """Normalize common OpenAI, Gemini, and compatible usage shapes without estimating."""

    raw = _usage_dict(usage)
    if raw is None:
        return {}
    prompt_details = _value(usage, "prompt_tokens_details", "input_tokens_details") or {}
    completion_details = _value(usage, "completion_tokens_details", "output_tokens_details") or {}
    input_tokens = _nonnegative_int(_value(usage, "prompt_tokens", "input_tokens", "promptTokenCount"))
    output_tokens = _nonnegative_int(
        _value(usage, "completion_tokens", "output_tokens", "candidatesTokenCount")
    )
    total_tokens = _nonnegative_int(_value(usage, "total_tokens", "totalTokenCount"))
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    cached_input_tokens = _nonnegative_int(
        _value(usage, "prompt_cache_hit_tokens", "cachedContentTokenCount")
    )
    if cached_input_tokens is None:
        cached_input_tokens = _nonnegative_int(_value(prompt_details, "cached_tokens"))
    reasoning_output_tokens = _nonnegative_int(_value(usage, "thoughtsTokenCount"))
    if reasoning_output_tokens is None:
        reasoning_output_tokens = _nonnegative_int(_value(completion_details, "reasoning_tokens"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_input_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "provider_usage": raw,
    }


def start_llm_usage_call(
    *,
    kind: str,
    model: str | None,
    endpoint: str | None,
    operation: str | None = None,
) -> LLMCallHandle:
    handle = LLMCallHandle(
        call_id=uuid4().hex,
        kind=kind,
        model=model,
        endpoint=endpoint,
        operation=operation,
        started_at=perf_counter(),
    )
    _emit_llm_usage_event(
        kind,
        phase="started",
        model=model,
        endpoint=endpoint,
        operation=operation,
        call_id=handle.call_id,
    )
    return handle


def complete_llm_usage_call(
    handle: LLMCallHandle,
    *,
    result: Any = None,
    usage: Any = None,
) -> None:
    try:
        normalized = _normalized_usage(usage if usage is not None else _value(result, "usage"))
        _emit_llm_usage_event(
            handle.kind,
            phase="completed",
            model=handle.model,
            endpoint=handle.endpoint,
            operation=handle.operation,
            call_id=handle.call_id,
            duration_ms=int((perf_counter() - handle.started_at) * 1000),
            **normalized,
        )
    except Exception:  # pragma: no cover - instrumentation must not affect callers
        logger.debug("Could not normalize LLM usage for call_id=%s", handle.call_id, exc_info=True)
        _emit_llm_usage_event(
            handle.kind,
            phase="completed",
            model=handle.model,
            endpoint=handle.endpoint,
            operation=handle.operation,
            call_id=handle.call_id,
            duration_ms=int((perf_counter() - handle.started_at) * 1000),
        )


def fail_llm_usage_call(handle: LLMCallHandle, error: BaseException) -> None:
    _emit_llm_usage_event(
        handle.kind,
        phase="failed",
        model=handle.model,
        endpoint=handle.endpoint,
        operation=handle.operation,
        call_id=handle.call_id,
        duration_ms=int((perf_counter() - handle.started_at) * 1000),
        error=str(error),
    )


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
            for j, item in enumerate(upstream_data):
                vec = item.embedding if hasattr(item, "embedding") else item.get("embedding") if isinstance(item, dict) else None
                if vec is not None and j < len(miss_texts):
                    self._cache.set(miss_texts[j], list(vec), model=model)
                    cached_vectors[miss_indices[j]] = list(vec)
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
