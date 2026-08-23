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

from menhir.config import MemorySettings, redact_uri_credentials
from menhir.infrastructure.llama_endpoint import acquire_llama_url_sync, should_use_scheduler
from menhir.infrastructure.observability import (
    complete_llm_usage_call,
    fail_llm_usage_call,
    start_llm_usage_call,
)
from menhir.infrastructure.providers import DEFAULT_REQUEST_TIMEOUT_S, ProviderConfig

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
        # CF-190: see the note in sync_llm.py. This seam is installed as the maintenance
        # scheduler's `experience_embed` hook with `experience_counter_enabled` defaulting True,
        # so an SDK-default client lets a hung endpoint hold a scheduler worker thread for ~30
        # minutes. The `except Exception` below reports only "View counter embed failed; writing
        # BM25-only" -- the operator never sees the stall, which is why the bound must be here.
        client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=DEFAULT_REQUEST_TIMEOUT_S,
            max_retries=0,
        )
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


def _normalize_embed_stamp_base(base_url: str) -> str:
    """Normalize a base URL for stamping: strip whitespace, any userinfo, and a trailing slash.

    Userinfo (``http://user:pass@host``) is stripped defensively -- base_url comes from operator
    settings and COULD carry it -- and the stamp is persisted on every embedded row, so it must
    never hold a credential. This delegates to the same `redact_uri_credentials` used by the
    CF-35 disclosure boundaries rather than repeating the parse here: one implementation, so an
    IPv6 literal or a malformed port cannot be handled two different ways.
    """
    return redact_uri_credentials(base_url.strip()).rstrip("/")


def view_embedder_version(settings: MemorySettings) -> str | None:
    """The stable identity of the configured embed provider, stamped as `embed_version` on write-time
    observation embeddings so it agrees with `backfill_assertion_embeddings` (which re-embeds rows whose
    stamped version differs). Returns ``<normalized base_url>|<embed_model>`` — the resolved endpoint and
    the model name — or None when no OpenAI-compatible embed provider is configured (so write-time
    embedding is simply skipped and the backfill fills later).

    The endpoint is part of the stamp because the local llama-server serves a GGUF file under an
    operator-chosen alias: swapping the weights while keeping the alias changes the embedding space with
    no change to the model string. Including the resolved base_url means the same model served from a
    different endpoint is a different embedding space. base_url is normalized (whitespace, trailing
    slash, and any userinfo stripped) so two spellings of the same endpoint produce the SAME stamp.

    What this still does NOT catch AUTOMATICALLY: a weight swap behind the SAME alias AND the SAME
    URL — this is not a fingerprint of the weights, only of where they are served from. `MENHIR_EMBED_VERSION`
    is the operator's lever for that case (CF-195): set it to anything new and the stamp changes, so the
    next backfill re-embeds. It is APPENDED rather than substituted, so it can only ever split one
    embedding identity into two -- it can never merge two real endpoints into one, which is what a
    replacement stamp would allow an operator to do by accident. Blank leaves the stamp byte-identical,
    so defining the setting is not itself a migration.

    Migration cost: this stamp format differs from the previous (model-name-only) one, so every
    existing stamped row now mismatches and the next `backfill_assertion_embeddings` run re-embeds the
    whole observation corpus once. That is correct-but-costly; it is a one-time migration.

    Never raises: a partial/misconfigured settings degrades to None, never a consolidation crash."""
    try:
        provider = ProviderConfig.for_graphiti_embedder(settings)
        if not provider.supports_graphiti_openai_contract() or not provider.embed_model:
            return None
        base_url = _normalize_embed_stamp_base(provider.base_url)
        stamp = f"{base_url}|{provider.embed_model}"
        override = str(getattr(settings, "embed_version_override", "") or "").strip()
        if override:
            stamp = f"{stamp}|{override}"
        return stamp
    except Exception:
        logger.warning("embed-version resolution failed; write-time observation embedding disabled",
                       exc_info=True)
        return None
