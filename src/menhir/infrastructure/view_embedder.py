"""Synchronous embedder for View recall surfaces (counter / timeline name_embedding).

The View bridges (failure_counter_bridge, instability_counter_bridge) run inside the maintenance
scheduler and write counters through a SYNC path (ViewRepository.record_counter). To give each
counter a cosine surface — not just BM25 — they need a synchronous ``embed(text) -> list[float]``.
Graphiti's own embedder is async (OpenAIEmbedder.create), which does not compose with the sync
bridges, so this builds an equivalent sync callable from the SAME Graphiti embed provider ingest
uses (base_url / api_key / embed_model), via a plain synchronous ``openai.OpenAI`` client.

``make_view_embedder(settings)`` returns the callable, or ``None`` when no OpenAI-compatible embed
provider is configured — in which case counters degrade to BM25-only surfacing, never an error.
The returned callable itself NEVER raises: on any provider error it logs and returns ``None`` so a
failed embed can never drop the counter write (the bridge writes ``name_embedding=None``).

The OpenAI client is built lazily on first embed (so importing / constructing the scheduler does
no network I/O), and any scheduler-managed embed URL is resolved once via ``acquire_llama_url_sync``
— the same resolution ``repair_embedding_dimensions`` uses.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from menhir.config import MemorySettings
from menhir.infrastructure.llama_endpoint import acquire_llama_url_sync, should_use_scheduler
from menhir.infrastructure.observability import (
    complete_llm_usage_call,
    fail_llm_usage_call,
    start_llm_usage_call,
)
from menhir.infrastructure.providers import ProviderConfig

logger = logging.getLogger(__name__)

ViewEmbedder = Callable[[str], "list[float] | None"]


def make_view_embedder(settings: MemorySettings) -> ViewEmbedder | None:
    """Build a sync ``embed(text) -> list[float] | None`` from the Graphiti embed provider.

    Returns ``None`` when the configured embed provider is not OpenAI-compatible or has no model —
    counters then carry only a BM25 surface. The returned callable resolves its OpenAI client on
    first use and swallows all provider errors (returning ``None``) so surfacing can degrade but a
    counter write is never lost."""
    provider = ProviderConfig.for_graphiti_embedder(settings)
    if not provider.supports_graphiti_openai_contract() or not provider.embed_model:
        logger.info(
            "View embedder disabled: embed provider=%s model=%r not OpenAI-compatible; "
            "experience counters will be BM25-only.",
            provider.kind.value, provider.embed_model,
        )
        return None

    model = provider.embed_model
    api_key = provider.api_key
    configured_base_url = provider.base_url
    client_holder: dict[str, Any] = {}

    def _resolve_client() -> Any:
        client = client_holder.get("client")
        if client is not None:
            return client
        from openai import OpenAI

        base_url = configured_base_url
        if should_use_scheduler(base_url):
            base_url = acquire_llama_url_sync(
                fallback=base_url, task="memory: view counter embed", timeout_s=120.0,
            )
        client = OpenAI(base_url=base_url, api_key=api_key)
        client_holder["client"] = client
        return client

    def embed(text: str) -> list[float] | None:
        handle = None
        try:
            client = _resolve_client()
            handle = start_llm_usage_call(
                kind="embedding",
                model=model,
                endpoint="embeddings.create",
                operation="view_embed",
            )
            response = client.embeddings.create(model=model, input=[text])
            complete_llm_usage_call(handle, result=response)
            return list(response.data[0].embedding)
        except Exception as exc:
            if handle is not None:
                fail_llm_usage_call(handle, exc)
            logger.warning("View counter embed failed; writing BM25-only", exc_info=True)
            return None

    return embed


def view_embedder_version(settings: MemorySettings) -> str | None:
    """The stable identity of the configured embed model, stamped as `embed_version` on write-time
    observation embeddings so it agrees with `backfill_assertion_embeddings` (which re-embeds rows whose
    stamped version differs). Returns the embed model name, or None when no OpenAI-compatible embed
    provider is configured (so write-time embedding is simply skipped and the backfill fills later).
    Never raises: a partial/misconfigured settings degrades to None, never a consolidation crash."""
    try:
        provider = ProviderConfig.for_graphiti_embedder(settings)
    except Exception:
        logger.warning("embed-version resolution failed; write-time observation embedding disabled",
                       exc_info=True)
        return None
    if not provider.supports_graphiti_openai_contract() or not provider.embed_model:
        return None
    return str(provider.embed_model)
