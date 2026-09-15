"""Provider abstractions for pluggable chat/embed model backends."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import threading
from typing import Any, Protocol, runtime_checkable

from menhir.config import MemorySettings
from menhir.infrastructure.observability import (
    build_async_openai_client,
    complete_llm_usage_call,
    fail_llm_usage_call,
    start_llm_usage_call,
)

OpenAIClientFactory = Callable[..., Any]
RetrySleep = Callable[[float], Awaitable[None]]

logger = logging.getLogger(__name__)


class ProviderKind(StrEnum):
    # Any OpenAI-compatible endpoint, not necessarily a local one: llama.cpp, Ollama,
    # LM Studio, vLLM, or a hosted gateway such as OpenRouter. The name is kept for
    # config compatibility -- LLM_CHAT_PROVIDER=local is in every existing .env.
    LOCAL = "local"
    OPENAI = "openai"         # OpenAI API
    ANTHROPIC = "anthropic"


def parse_provider_kind(value: str | ProviderKind | None, *, default: ProviderKind = ProviderKind.LOCAL) -> ProviderKind:
    if value is None:
        return default
    if isinstance(value, ProviderKind):
        return value
    normalized = str(value).strip().lower().replace("-", "_")
    if not normalized:
        return default
    # backward compat alias
    if normalized == "openai_compat":
        return ProviderKind.LOCAL
    try:
        return ProviderKind(normalized)
    except ValueError as exc:
        raise ValueError(
            "Unsupported LLM provider. Use one of: local, openai, anthropic."
        ) from exc


@dataclass(frozen=True)
class ProviderConfig:
    kind: ProviderKind
    base_url: str
    api_key: str
    chat_model: str
    embed_model: str = ""

    @classmethod
    def _for_local(cls, settings: MemorySettings) -> "ProviderConfig":
        return cls(
            kind=ProviderKind.LOCAL,
            base_url=settings.local_llm_base_url,
            api_key=settings.local_llm_api_key,
            chat_model=settings.local_llm_chat_model,
            embed_model=settings.local_llm_embed_model,
        )

    @classmethod
    def _for_openai(cls, settings: MemorySettings) -> "ProviderConfig":
        return cls(
            kind=ProviderKind.OPENAI,
            base_url="https://api.openai.com/v1",
            api_key=settings.openai_api_key,
            chat_model=settings.openai_chat_model,
            embed_model=settings.openai_embed_model,
        )

    @classmethod
    def _base_for_kind(cls, kind: ProviderKind, settings: MemorySettings) -> "ProviderConfig":
        if kind is ProviderKind.LOCAL:
            return cls._for_local(settings)
        if kind is ProviderKind.OPENAI:
            return cls._for_openai(settings)
        return cls(
            kind=ProviderKind.ANTHROPIC,
            base_url="",
            api_key="",
            chat_model="",
            embed_model="",
        )

    @classmethod
    def for_chat(cls, settings: MemorySettings) -> "ProviderConfig":
        kind = parse_provider_kind(settings.chat_provider)
        return cls._base_for_kind(kind, settings)

    @classmethod
    def for_graphiti_llm(cls, settings: MemorySettings) -> "ProviderConfig":
        kind = parse_provider_kind(
            settings.graphiti_provider,
            default=parse_provider_kind(settings.chat_provider),
        )
        return cls._base_for_kind(kind, settings)

    @classmethod
    def for_graphiti_embedder(cls, settings: MemorySettings) -> "ProviderConfig":
        kind = parse_provider_kind(
            settings.graphiti_embed_provider or settings.graphiti_provider,
            default=parse_provider_kind(settings.chat_provider),
        )
        base = cls._base_for_kind(kind, settings)
        # Local embed may use a separate base URL (different port/server)
        if kind is ProviderKind.LOCAL and settings.local_llm_embed_base_url:
            return cls(
                kind=base.kind,
                base_url=settings.local_llm_embed_base_url,
                api_key=base.api_key,
                chat_model=base.chat_model,
                embed_model=settings.local_llm_embed_model or base.embed_model,
            )
        return base

    @classmethod
    def for_graphiti_reranker(cls, settings: MemorySettings) -> "ProviderConfig":
        kind = parse_provider_kind(
            settings.graphiti_reranker_provider or settings.graphiti_provider,
            default=parse_provider_kind(settings.chat_provider),
        )
        return cls._base_for_kind(kind, settings)

    def supports_graphiti_openai_contract(self) -> bool:
        return self.kind in {ProviderKind.LOCAL, ProviderKind.OPENAI}


@runtime_checkable
class ChatBackend(Protocol):
    async def create_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        operation: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Return plain text chat completion output.

        An empty or refused completion returns ""; implementations must not
        raise for that case.
        """


#: The package-wide fail-fast budget for one LLM/embedding HTTP call.
#:
#: Named and exported because the OpenAI SDK's defaults are the opposite policy -- 600 s read plus
#: 2 automatic retries, i.e. ~30 minutes worst case per call. Every seam that constructs a client
#: must pass this and `max_retries=0` explicitly; two seams did not, which is CF-190. Compare
#: `llm.py`: "fail fast -- don't block MCP on a down server".
DEFAULT_REQUEST_TIMEOUT_S: float = 30.0

#: Process-wide cache of OpenAI/httpx chat clients, keyed on every input that changes a
#: client's identity (see `build_async_openai_client`). Without it, `create_chat_completion`
#: built a fresh client -- and a fresh `httpx.AsyncClient`/connection pool (or a full TLS
#: handshake for a remote provider) -- on every call. One client is now reused for repeated
#: calls with the same configuration. Reset via `reset_client_cache()`, which the graphiti
#: base-URL rebind path calls so a rotated endpoint never reuses a client built for the old
#: one. It is a module dict, not a singleton class, so tests can reset it freely.
_openai_client_cache: dict[tuple[Any, ...], Any] = {}
_openai_client_cache_lock = threading.Lock()


def reset_client_cache() -> list[Any]:
    """Drop every cached OpenAI/httpx client and RETURN them, so the caller can close them.

    Called by CF-161's base-URL rebind path: a rotated endpoint must not keep reusing a
    client (and its connection pool) built for the previous base URL.

    Returning the evicted clients rather than closing them here is deliberate. Closing is
    async and this function is sync, and `graphiti_client` already owns the one mechanism
    that schedules an async close safely from a sync path. Clearing without returning would
    have traded the reuse defect for a slow pool leak on every rotation -- the same class of
    bug CF-161 is about.
    """
    with _openai_client_cache_lock:
        evicted = list(_openai_client_cache.values())
        _openai_client_cache.clear()
    return evicted


def _chat_client_cache_key(
    *,
    factory: Callable[..., Any],
    base_url: str,
    api_key: str,
    settings: MemorySettings | None,
    request_timeout_s: float | None,
    embedding_cache: Any,
) -> tuple[Any, ...]:
    """Build the cache key from every input that changes a client's identity.

    Beyond the base URL and API key (the two the brief names), `build_async_openai_client`
    also branches on Langfuse config and the request timeout, and the factory itself is part
    of the identity (different injected factories must never share a cached client).
    """
    if settings is None:
        langfuse = (None, None, None)
    else:
        langfuse = (
            settings.langfuse_host,
            settings.langfuse_public_key,
            settings.langfuse_secret_key,
        )
    return (
        factory,
        base_url,
        api_key,
        request_timeout_s,
        embedding_cache,
        *langfuse,
    )


@dataclass(frozen=True)
class ProviderRuntimeDependencies:
    """Runtime hooks for OpenAI-compatible provider I/O."""

    openai_client_factory: OpenAIClientFactory = build_async_openai_client
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    retry_sleep: RetrySleep = asyncio.sleep


@dataclass
class OpenAIStyleChatBackend:
    """OpenAI SDK-backed chat backend for local and openai providers."""

    provider: ProviderConfig
    settings: MemorySettings
    dependencies: ProviderRuntimeDependencies = field(default_factory=ProviderRuntimeDependencies)

    async def _resolve_base_url(self, operation: str) -> str:
        """Return the configured base URL for ``operation``.

        Takes NO prompt: a previous version accepted a ``user_prompt`` it never read, and the
        obvious way to "use" it was to interpolate it into a request label that an external
        service logged -- a prompt-content leak. The parameter stays gone.
        """
        return self.provider.base_url

    async def create_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        operation: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        base_url = await self._resolve_base_url(operation)
        cache_key = _chat_client_cache_key(
            factory=self.dependencies.openai_client_factory,
            base_url=base_url,
            api_key=self.provider.api_key,
            settings=self.settings,
            request_timeout_s=self.dependencies.request_timeout_s,
            embedding_cache=None,
        )
        with _openai_client_cache_lock:
            client = _openai_client_cache.get(cache_key)
        if client is None:
            client = self.dependencies.openai_client_factory(
                base_url=base_url,
                api_key=self.provider.api_key,
                settings=self.settings,
                request_timeout_s=self.dependencies.request_timeout_s,
            )
            with _openai_client_cache_lock:
                _openai_client_cache[cache_key] = client
        # CF-234: this backend announced nothing, so every `LLMAdapter` call it served was
        # invisible to both LLM budgets -- including the judge fan-out (3 calls per proposal per
        # extracted node) that CF-79 was filed to bound. `build_chat_backend` routes both LOCAL
        # and OPENAI here and `chat_provider` defaults to "local", so that was the default
        # configuration.
        #
        # `report_only=True` is deliberate and temporary: it makes the calls VISIBLE without
        # making the budget bind. Enforcing needs a landing zone first -- a refusal currently has
        # no handler and falls into `_process_episode`'s generic `except Exception`, which marks
        # the episode FAILED. Flip this to enforcing only together with that handler and a cap
        # calibrated on what this measurement reports.
        handle = start_llm_usage_call(
            kind="chat",
            model=self.provider.chat_model,
            endpoint="chat.completions.create",
            operation=operation,
            report_only=True,
        )
        try:
            response = await client.chat.completions.create(
                model=self.provider.chat_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except BaseException as exc:
            fail_llm_usage_call(handle, exc)
            raise
        complete_llm_usage_call(handle, result=response)
        choices = response.choices or []
        if not choices:
            return ""
        return choices[0].message.content or ""


@dataclass
class UnimplementedProviderChatBackend:
    """Placeholder backend for non-openai providers until SDK bridges are added."""

    provider: ProviderConfig

    async def create_chat_completion(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        operation: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        raise NotImplementedError(
            f"{self.provider.kind.value} chat backend is scaffolded but not implemented yet."
        )


def build_chat_backend(
    settings: MemorySettings,
    provider: ProviderConfig | None = None,
    dependencies: ProviderRuntimeDependencies | None = None,
) -> ChatBackend:
    provider = provider or ProviderConfig.for_chat(settings)
    if provider.kind in {ProviderKind.LOCAL, ProviderKind.OPENAI}:
        return OpenAIStyleChatBackend(
            provider=provider,
            settings=settings,
            dependencies=dependencies or ProviderRuntimeDependencies(),
        )
    if provider.kind is ProviderKind.ANTHROPIC:
        return UnimplementedProviderChatBackend(provider=provider)
    raise ValueError(f"Unsupported provider kind: {provider.kind}")
